"""Qwen-Image 2.1 Edit (PE v3) adapter: the first family behind the edit contract.

The pinned graph is the artist's own PE v3 edit workbench, extracted node for
node (``workflows/qwen-image-2.1-edit-pe-v3``; editor notes dropped, the
Pixaroma save node swapped for a temp ``PreviewImage``).  A ledger edit job
changes exactly five declared pointers - the uploaded crop, the instruction,
the negative prompt, the sampler seed and the prompt-enhancer switch - and,
when ComfyUI registers it, inserts one live preview node (``900``) between the
model chain and the sampler, with the Qwen tiny VAE decoder when listed.

Outputs: node ``477`` (the edited crop, one temp PNG) and node ``506``
(``PreviewAny``: the prompt the image model actually received, i.e. the
prompt enhancer's output).  Provider correlation is the same seven-field
identity the Krea adapter uses, with this target's id and hashes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import ntpath
import os
import time
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .image_contract import PROVIDER_ID, ContractError, sha256, validate_snapshot
from .image_edit_contract import EDIT_TARGETS, QWEN_TARGET_ID
from .comfy_failure import failure_reason as _comfy_failure_reason  # lane w24-rest (GEM-056)
from . import qwen_enhancer_guard as _guard  # w41-qwen: shared with the catalog targets

TARGET_ID = QWEN_TARGET_ID
_TARGET = EDIT_TARGETS[TARGET_ID]
CAPABILITY_SHA256 = _TARGET["capability_manifest_sha256"]
GRAPH_SHA256 = _TARGET["api_graph_sha256"]

MUTABLE_POINTERS = {
    "image": "/492/inputs/image",
    "instruction": "/502/inputs/value",
    "negative": "/490/inputs/negative_prompt",
    "seed": "/487/inputs/seed",
    "prompt_enhancer": "/505/inputs/switch",
}
# Strength (owner decision 1): below 100 % KSampler 487 denoises the latent of the INPUT picture.
# TextEncodeQwenImage21's third output (490[2], behind switch 488) is torch.zeros on the reference's
# size, not the picture's latent, so below 100 % the compiled graph gains ONE VAEEncode (node 910:
# pixels <- LoadImage 492 = the Suite's crop at work size, vae <- VAELoader 484) and 487.latent_image
# points at it. At 100 % nothing is added or touched: the compiled graph is byte-identical to the
# graph before Strength. Declared here, not in manifest.json: its hash is recorded in every edit job.
STRENGTH_POINTERS = {
    "denoise": "/487/inputs/denoise",
    "latent": "/487/inputs/latent_image",
}
STRENGTH_ENCODE_NODE_ID = "910"
STRENGTH_ENCODE_NODE = {"class_type": "VAEEncode", "inputs": {"pixels": ["492", 0], "vae": ["484", 0]}}
OUTPUT_NODE = "477"
SENT_PROMPT_NODE = "506"
SAMPLER_NODE = "487"
NODE_COUNT = 26
# Enhancer guard (p11-imgb): RegexMatch + lazy ComfySwitchNode on the cleaned rewrite, fallback the plain
# instruction. w41-qwen: the pattern and the insertion moved to qwen_enhancer_guard.py (unchanged), so the
# catalog targets (qwen_image_workflow_adapter) reuse the same guard.
ENHANCER_GUARD_PATTERN = _guard.PATTERN
ENHANCER_GUARD_MATCH_NODE_ID = _guard.MATCH_NODE_ID
ENHANCER_GUARD_SWITCH_NODE_ID = _guard.SWITCH_NODE_ID
ENHANCER_SWITCH_NODE = "505"
ENHANCER_SEED_POINTER = "/504/inputs/sampling_mode.seed"

LIVE_PREVIEW_CLIENT_ID = "filmsuite-image-preview"
LIVE_PREVIEW_METHOD = "latent2rgb"
LIVE_PREVIEW_NODE_CLASS = "ModelPreviewOverrideKJ"
LIVE_PREVIEW_NODE_ID = "900"
LIVE_PREVIEW_TINY_VAE = "TAEQwenImage21_AcademiaSD.safetensors"
LIVE_PREVIEW_NODE_INPUTS = {"max_resolution": 768, "jpeg_quality": 80, "suppress_default_preview": True,
                            "preview_frames": 1, "preview_fps": 12}
_PROBE_TTL = 60.0
_PROBE_RETRY = 10.0
_probe: dict[str, Any] = {"until": 0.0, "present": None, "tiny_vae": None}
_PREVIEW_NODES: "OrderedDict[str, str | None]" = OrderedDict()
_PREVIEW_NODES_MAX = 64

REQUIRED_CLASSES = (
    "BatchImagesNode", "CLIPLoader", "ComfySwitchNode", "EmptyLatentImage", "KSampler", "LoadImage",
    "LoraLoaderModelOnly", "PreviewAny", "PreviewImage", "PrimitiveString", "PrimitiveStringMultiline",
    "QwenImage21Cache", "RegexReplace", "ResolutionSelector", "TextEncodeQwenImage21", "TextGenerate",
    "UNETLoader", "VAEDecode", "VAELoader",
    "DifferentialDiffusion", "SetLatentNoiseMask", "VAEEncode", "SplitSigmas",  # added at compile time (p13-img latent mask; 910)
)
# The pointer table of the latent-mask step on this graph (validated by insert_latent_mask).
LATENT_MASK_POINTERS = {"sampler": "487", "latent_key": "latent_image", "load_node": "492", "vae_node": "484",
                        "model_node": "487", "model_key": "model", "denoise": "/487/inputs/denoise"}


class QwenEditAdapterError(ValueError):
    """The pinned Qwen edit boundary or an input is invalid."""


@dataclass(frozen=True)
class SubmitOutcome:
    kind: str
    provider_prompt_id: str | None = None
    reason: str | None = None

    @property
    def prompt_id(self) -> str | None:
        return self.provider_prompt_id


@dataclass(frozen=True)
class PollOutcome:
    kind: str
    status: str
    output_identity: dict[str, str] | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CancelOutcome:
    kind: str
    reason: str | None = None


def _default_asset_root(module_file: str, target_id: str = TARGET_ID) -> Path:
    here = Path(module_file).resolve()
    repository = here.parents[4] / "workflows"
    if (repository / target_id / "manifest.json").is_file():
        return repository
    installed = here.parents[1] / "workflows"
    if (installed / target_id / "manifest.json").is_file():
        return installed
    return repository


RUNTIME_ASSET_ROOT = _default_asset_root(__file__)


def _read_pinned(root: Path, name: str, expected: str, target_id: str = TARGET_ID) -> dict[str, Any]:
    directory = (root / target_id)
    path = directory / name
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(directory.resolve(strict=True))
        if path.is_symlink() or not resolved.is_file():
            raise QwenEditAdapterError("edit workflow asset unavailable")
        data = resolved.read_bytes()
    except (OSError, RuntimeError, ValueError) as exc:
        raise QwenEditAdapterError("edit workflow asset unavailable") from exc
    if hashlib.sha256(data).hexdigest() != expected:
        raise QwenEditAdapterError("edit workflow asset fingerprint mismatch")
    value = json.loads(data.decode("utf-8"))
    if not isinstance(value, dict):
        raise QwenEditAdapterError("edit workflow asset malformed")
    return value


def get_pointer(value: Any, pointer: str) -> Any:
    current = value
    for token in pointer.split("/")[1:]:
        if not isinstance(current, Mapping) or token not in current:
            raise QwenEditAdapterError("JSON pointer target missing")
        current = current[token]
    return current


def set_pointer(value: Any, pointer: str, new_value: Any) -> None:
    parts = pointer.split("/")[1:]
    current = value
    for token in parts[:-1]:
        if not isinstance(current, dict) or token not in current:
            raise QwenEditAdapterError("mutable JSON pointer target missing")
        current = current[token]
    if not isinstance(current, dict) or parts[-1] not in current:
        raise QwenEditAdapterError("mutable JSON pointer target missing")
    current[parts[-1]] = copy.deepcopy(new_value)


def _equal(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return set(left) == set(right) and all(_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a, b) for a, b in zip(left, right))
    return left == right


def validate_assets(manifest: Mapping[str, Any], graph: Mapping[str, Any]) -> None:
    if manifest.get("schema") != "studio.image-edit-capability/1" or manifest.get("id") != TARGET_ID \
            or manifest.get("family") != _TARGET["family"] or manifest.get("variant") != _TARGET["variant"]:
        raise QwenEditAdapterError("edit capability manifest identity changed")
    if not _equal(manifest.get("mutable_json_pointers"), MUTABLE_POINTERS):
        raise QwenEditAdapterError("edit mutable pointer declarations changed")
    workflow = manifest.get("workflow") or {}
    if workflow.get("api_sha256") != GRAPH_SHA256 or workflow.get("node_count") != NODE_COUNT or len(graph) != NODE_COUNT:
        raise QwenEditAdapterError("edit workflow fingerprint changed")
    for pointer, expected in (manifest.get("fixed_assertions") or {}).items():
        if not _equal(get_pointer(graph, pointer), expected):
            raise QwenEditAdapterError(f"pinned edit graph assertion changed: {pointer}")
    files = _TARGET["files"]
    for pointer, expected in (("/482/inputs/unet_name", files["diffusion_model"]), ("/483/inputs/clip_name", files["text_encoder"]),
                              ("/484/inputs/vae_name", files["vae"]), ("/496/inputs/lora_name", files["lora"]),
                              ("/500/inputs/clip_name", files["prompt_enhancer"])):
        if get_pointer(graph, pointer) != expected:
            raise QwenEditAdapterError("pinned edit graph files changed")
    if graph.get(OUTPUT_NODE, {}).get("inputs") != {"images": ["486", 0]} or graph.get(SENT_PROMPT_NODE, {}).get("inputs") != {"source": ["505", 0]}:
        raise QwenEditAdapterError("pinned edit outputs changed")
    if graph[SAMPLER_NODE]["inputs"].get("model") != ["489", 0] or graph["490"]["inputs"].get("prompt") != ["506", 0]:
        raise QwenEditAdapterError("pinned edit routes changed")
    for node in graph.values():
        if not isinstance(node, Mapping) or not isinstance(node.get("class_type"), str) or not isinstance(node.get("inputs"), Mapping):
            raise QwenEditAdapterError("pinned edit graph node shape changed")
        if node["class_type"] not in REQUIRED_CLASSES:
            raise QwenEditAdapterError("pinned edit graph has an undeclared class")


def insert_enhancer_guard(compiled: dict[str, Any], switch_node: str) -> None:
    """Route ``switch_node``'s on_true (the cleaned enhancer text) through RegexMatch + ComfySwitchNode, with
    its on_false (the plain instruction) as the fallback. In place; refuses a changed insertion point."""
    _guard.insert(compiled, switch_node, QwenEditAdapterError)


# p13-img (IMG-129/131): the selection reaches the sampler as a latent noise mask. Compile-time nodes (the pinned
# graphs and their sha stay unchanged, like node 910): 940 VAEEncode(the uploaded crop), 941 SetLatentNoiseMask(940,
# LoadImage output 1 = the soft mask the Suite put into the crop's alpha), 943 DifferentialDiffusion on the model
# (soft edge: each pixel denoises as far as its mask value). ComfyUI nodes.py 1567-1579 and
# comfy_extras/nodes_differential_diffusion.py; maintainer prototype (graph ids 941/942).
LATENT_ENCODE_NODE_ID = "940"
LATENT_MASK_NODE_ID = "941"
LATENT_SPLIT_NODE_ID = "942"
LATENT_DIFF_NODE_ID = "943"
LATENT_MASK_CLASSES = ("DifferentialDiffusion", "SetLatentNoiseMask", "VAEEncode")


_OUTPUT_CLASSES = frozenset({"PreviewImage", "PreviewAny", "SaveImage"})


def prune_unchosen(compiled: dict[str, Any], switch_node: str) -> None:
    """Lane w24-rest MOD-017: the enhancer OFF is structural. ``switch_node`` (switch False) loses its on_true and every
    node no output needs any more is removed, so the enhancer checkpoint is neither validated nor loaded (an absent
    enhancer file no longer fails the run), as the Create adapters already do. In place; refuses a changed switch."""
    switch = compiled.get(switch_node)
    if not isinstance(switch, dict) or switch.get("class_type") != "ComfySwitchNode" or switch["inputs"].get("switch") is not False:
        raise QwenEditAdapterError("enhancer switch changed")
    switch["inputs"].pop("on_true", None)
    todo = [nid for nid, node in compiled.items() if node.get("class_type") in _OUTPUT_CLASSES]
    keep: set[str] = set()
    while todo:
        nid = todo.pop()
        if nid in keep or nid not in compiled:
            continue
        keep.add(nid)
        todo.extend(v[0] for v in compiled[nid]["inputs"].values()
                    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str) and isinstance(v[1], int))
    for nid in [n for n in compiled if n not in keep]:
        compiled.pop(nid)


def insert_latent_mask(compiled: dict[str, Any], *, sampler: str, latent_key: str, load_node: str, vae_node: str,
                       model_node: str, model_key: str) -> None:
    """Point ``sampler.latent_key`` at SetLatentNoiseMask(VAEEncode(load_node), load_node[1]) and patch
    ``model_node.model_key`` through DifferentialDiffusion. Refuses when an id is taken or a route changed."""
    for node_id in (LATENT_ENCODE_NODE_ID, LATENT_MASK_NODE_ID, LATENT_DIFF_NODE_ID):
        if node_id in compiled:
            raise QwenEditAdapterError("latent mask node id taken")
    if compiled.get(load_node, {}).get("class_type") != "LoadImage" or compiled.get(vae_node, {}).get("class_type") != "VAELoader" \
            or latent_key not in (compiled.get(sampler, {}).get("inputs") or {}) \
            or not isinstance((compiled.get(model_node, {}).get("inputs") or {}).get(model_key), list):
        raise QwenEditAdapterError("latent mask insertion point changed")
    compiled[LATENT_ENCODE_NODE_ID] = {"class_type": "VAEEncode", "inputs": {"pixels": [load_node, 0], "vae": [vae_node, 0]}}
    compiled[LATENT_MASK_NODE_ID] = {"class_type": "SetLatentNoiseMask",
                                     "inputs": {"samples": [LATENT_ENCODE_NODE_ID, 0], "mask": [load_node, 1]}}
    compiled[LATENT_DIFF_NODE_ID] = {"class_type": "DifferentialDiffusion",
                                     "inputs": {"model": list(compiled[model_node]["inputs"][model_key]), "strength": 1.0}}
    compiled[sampler]["inputs"][latent_key] = [LATENT_MASK_NODE_ID, 0]
    compiled[model_node]["inputs"][model_key] = [LATENT_DIFF_NODE_ID, 0]


STRENGTH_START_ENCODE_NODE_ID = "944"
STRENGTH_START_SPLIT_NODE_ID = "945"


def insert_strength_start(compiled: dict[str, Any], snapshot: Mapping[str, Any], *, sampler: str, latent_key: str,
                          load_node: str, vae_node: str, scheduler: str | None = None) -> None:
    """w24-edit2 (IMG-130, curve ``start_v1``): the sampler starts on the crop's own latent at the snapshot's start of
    the model's schedule. KSampler: ``steps`` + ``denoise`` (= steps / (total + 0.5), so ComfyUI runs exactly
    ``total`` sigmas and keeps the last ``steps``). A scheduler node (FLUX.2 Klein): ``total`` steps, split at
    ``total - steps`` (SplitSigmas low half). The latent is VAEEncode(crop) unless the sampler already reads one."""
    eff = snapshot["effective_input"]
    edit = eff["edit"]
    sched = _contract_start(edit, snapshot["target_id"])
    node = compiled.get(sampler) or {}
    if latent_key not in (node.get("inputs") or {}) or compiled.get(vae_node, {}).get("class_type") != "VAELoader" \
            or compiled.get(load_node, {}).get("class_type") != "LoadImage" or STRENGTH_START_ENCODE_NODE_ID in compiled:
        raise QwenEditAdapterError("strength insertion point changed")
    compiled[STRENGTH_START_ENCODE_NODE_ID] = {"class_type": "VAEEncode", "inputs": {"pixels": [load_node, 0], "vae": [vae_node, 0]}}
    node["inputs"][latent_key] = [STRENGTH_START_ENCODE_NODE_ID, 0]
    if scheduler is None:
        node["inputs"]["steps"] = max(1, int(sched["steps"]))  # 0 %: denoise 0 = no sigma, the crop comes back
        node["inputs"]["denoise"] = float(sched["denoise"])
        return
    if compiled.get(scheduler, {}).get("class_type") != "Flux2Scheduler" or STRENGTH_START_SPLIT_NODE_ID in compiled:
        raise QwenEditAdapterError("strength insertion point changed")
    compiled[scheduler]["inputs"]["steps"] = int(sched["total"])
    compiled[STRENGTH_START_SPLIT_NODE_ID] = {"class_type": "SplitSigmas",
                                              "inputs": {"sigmas": [scheduler, 0], "step": int(sched["total"]) - int(sched["steps"])}}
    node["inputs"]["sigmas"] = [STRENGTH_START_SPLIT_NODE_ID, 1]


def _contract_start(edit: Mapping[str, Any], target_id: str) -> dict[str, Any]:
    from .image_edit_contract import EDIT_TARGETS as _T, start_schedule, strength_start
    return start_schedule(strength_start(edit["strength"], target_id), int(_T[target_id]["sampling"]["steps"]))


def insert_live_preview(graph: Mapping[str, Any], tiny_vae: str | None, sampler_node: str = SAMPLER_NODE) -> dict[str, Any]:
    """Copy of ``graph`` with node 900 before the sampler; only the sampler's model input is re-pointed."""
    compiled = copy.deepcopy(dict(graph))
    sampler = compiled.get(sampler_node)
    if LIVE_PREVIEW_NODE_ID in compiled or not isinstance(sampler, dict) or sampler.get("class_type") != "KSampler":
        raise QwenEditAdapterError("live preview insertion point changed")
    model_from = sampler["inputs"].get("model")
    if not (isinstance(model_from, list) and len(model_from) == 2):
        raise QwenEditAdapterError("live preview insertion point changed")
    inputs = {"model": list(model_from), **LIVE_PREVIEW_NODE_INPUTS}
    if tiny_vae:
        inputs["tiny_vae"] = tiny_vae
    compiled[LIVE_PREVIEW_NODE_ID] = {"class_type": LIVE_PREVIEW_NODE_CLASS, "inputs": inputs}
    sampler["inputs"]["model"] = [LIVE_PREVIEW_NODE_ID, 0]
    return compiled


def live_preview_node(prompt_id: Any) -> str | None:
    return _PREVIEW_NODES.get(prompt_id) if isinstance(prompt_id, str) else None


def live_preview_known(prompt_id: Any) -> bool:
    return isinstance(prompt_id, str) and prompt_id in _PREVIEW_NODES


def _remember(prompt_id: str, node: str | None) -> None:
    _PREVIEW_NODES[prompt_id] = node
    _PREVIEW_NODES.move_to_end(prompt_id)
    while len(_PREVIEW_NODES) > _PREVIEW_NODES_MAX:
        _PREVIEW_NODES.popitem(last=False)


# The node that shows the prompt the image model received, per edit target
# (a family module adds its own entry when it loads).
SENT_PROMPT_NODES: dict[str, str] = {TARGET_ID: SENT_PROMPT_NODE}


def sent_prompt_from_history(entry: Any) -> str | None:
    """The prompt the image model received (the target's PreviewAny ui text), or None."""
    if not isinstance(entry, Mapping):
        return None
    meta = _metadata(entry.get("prompt"))
    target = meta.get("target_id") if meta is not None else None
    node = (entry.get("outputs") or {}).get(SENT_PROMPT_NODES.get(target, SENT_PROMPT_NODE) if isinstance(target, str) else SENT_PROMPT_NODE)
    text = node.get("text") if isinstance(node, Mapping) else None
    if isinstance(text, list) and text and isinstance(text[0], str):
        return text[0][:16_384]
    return None


def execution_ms_from_history(entry: Any) -> int | None:
    """ComfyUI execution time of one prompt from its status messages (start -> success)."""
    if not isinstance(entry, Mapping):
        return None
    stamps = {}
    for message in (entry.get("status") or {}).get("messages") or []:
        if isinstance(message, list) and len(message) == 2 and isinstance(message[1], Mapping):
            stamp = message[1].get("timestamp")
            if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
                stamps[message[0]] = stamp
    if "execution_start" in stamps and "execution_success" in stamps:
        return int(stamps["execution_success"] - stamps["execution_start"])
    return None


def _valid_token(value: Any) -> bool:
    return type(value) is str and 1 <= len(value) <= 128 and all(
        c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-" for c in value)


def _valid_prompt_id(value: Any) -> bool:
    return type(value) is str and 1 <= len(value) <= 256 and value == value.strip() and not any(
        ord(c) < 0x20 or c == "\x7f" for c in value)


def _accepted_pins(target_id: str, capability_sha256: str, graph_sha256: str) -> set[tuple[str, str]]:
    """The adapter's current pin plus every earlier pin of its target (w32-fix5: a job stored under one keeps polling)."""
    from .image_edit_contract import earlier_edit_pins
    return {(capability_sha256, graph_sha256), *earlier_edit_pins(target_id)}


def _identity(value: Mapping[str, Any], target_id: str = TARGET_ID, capability_sha256: str = CAPABILITY_SHA256,
              graph_sha256: str = GRAPH_SHA256) -> dict[str, Any]:
    keys = {"provider_id", "image_producer_job_id", "client_request_id", "target_id", "request_snapshot_sha256",
            "capability_manifest_sha256", "api_graph_sha256"}
    if not isinstance(value, Mapping) or set(value) != keys:
        raise QwenEditAdapterError("provider correlation identity shape is invalid")
    if value["provider_id"] != PROVIDER_ID or value["target_id"] != target_id \
            or (value["capability_manifest_sha256"], value["api_graph_sha256"]) not in _accepted_pins(target_id, capability_sha256, graph_sha256):
        raise QwenEditAdapterError("provider correlation identity is not authoritative")
    return dict(value)


def _metadata(prompt: Any) -> Mapping[str, Any] | None:
    if not isinstance(prompt, (list, tuple)) or len(prompt) < 4 or not isinstance(prompt[3], Mapping):
        return None
    studio = prompt[3].get("studio")
    return studio if isinstance(studio, Mapping) else None


def _matches(metadata: Mapping[str, Any] | None, expected: Mapping[str, Any]) -> bool:
    return metadata is not None and set(metadata) == set(expected) and all(metadata.get(k) == v for k, v in expected.items())


def _png_descriptor(entry: Mapping[str, Any], output_node: str = OUTPUT_NODE) -> dict[str, str]:
    outputs = entry.get("outputs")
    if not isinstance(outputs, Mapping) or output_node not in outputs:
        raise QwenEditAdapterError("output node is not authoritative")
    node = outputs[output_node]
    images = node.get("images") if isinstance(node, Mapping) else None
    if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], Mapping):
        raise QwenEditAdapterError("output descriptor count is not one")
    raw = images[0]
    filename, subfolder, kind = raw.get("filename"), raw.get("subfolder"), raw.get("type")
    if (type(filename) is not str or not 1 <= len(filename) <= 255 or subfolder != "" or kind != "temp"
            or filename != ntpath.basename(filename) or "/" in filename or "\\" in filename
            or any(c in filename for c in '<>:"|?*') or not filename.casefold().endswith(".png")
            or any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in filename) or filename != filename.rstrip(" .")):
        raise QwenEditAdapterError("output descriptor is not a safe PNG")
    return {"filename": filename, "subfolder": "", "type": "temp", "mime_type": "image/png", "extension": ".png"}


class QwenImageEditAdapter:
    """Compile and operate one Qwen-Image 2.1 PE v3 edit job through an injected client."""

    target_id = TARGET_ID
    # Per-family facts a subclass replaces (the Qwen values are the module constants).
    capability_sha256 = CAPABILITY_SHA256
    graph_sha256 = GRAPH_SHA256
    output_node = OUTPUT_NODE
    sampler_node = SAMPLER_NODE
    tiny_vae_name: str | None = LIVE_PREVIEW_TINY_VAE

    def __init__(self, client: Any, *, asset_root: os.PathLike[str] | str | None = None, submit_client_id: str | None = None,
                 preview_method: str | None = None, kj_preview: bool = False) -> None:
        self.client = client
        self.asset_root = Path(self._default_root() if asset_root is None else asset_root)
        if submit_client_id is not None and not _valid_token(submit_client_id):
            raise QwenEditAdapterError("submit client id invalid")
        if preview_method not in (None, "latent2rgb", "taesd", "auto", "none"):
            raise QwenEditAdapterError("preview method invalid")
        self.submit_client_id = submit_client_id
        self.preview_method = preview_method
        self.kj_preview = bool(kj_preview)

    def _default_root(self) -> Path:
        # Read at construction (not class definition) so the installed root and test patches apply.
        return RUNTIME_ASSET_ROOT

    def load_assets(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Verified copies of the pinned manifest and graph (re-read and re-hashed every time)."""
        manifest = _read_pinned(self.asset_root, "manifest.json", CAPABILITY_SHA256)
        graph = _read_pinned(self.asset_root, "workflow-api.json", GRAPH_SHA256)
        validate_assets(manifest, graph)
        return manifest, graph

    def compile_graph(self, request_snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """One validated edit snapshot -> the API graph: the five declared pointers (+ Strength: denoise and one VAEEncode)."""
        try:
            snapshot = copy.deepcopy(dict(request_snapshot))
            validate_snapshot(snapshot)
        except (ContractError, TypeError, ValueError, RecursionError) as exc:
            raise QwenEditAdapterError("invalid edit request snapshot") from exc
        if snapshot.get("operation") != "edit" or snapshot.get("target_id") != TARGET_ID:
            raise QwenEditAdapterError("snapshot is not a Qwen edit")
        _manifest, graph = self.load_assets()
        edit = snapshot["effective_input"]["edit"]
        artist = snapshot["artist_input"]
        compiled = copy.deepcopy(graph)
        set_pointer(compiled, MUTABLE_POINTERS["image"], edit["image"])
        set_pointer(compiled, MUTABLE_POINTERS["instruction"], artist["prompt"])
        set_pointer(compiled, MUTABLE_POINTERS["negative"], edit["negative"])
        set_pointer(compiled, MUTABLE_POINTERS["seed"], artist["seed"])
        set_pointer(compiled, MUTABLE_POINTERS["prompt_enhancer"], bool(edit["prompt_enhancer"]))
        if edit["prompt_enhancer"]:
            insert_enhancer_guard(compiled, ENHANCER_SWITCH_NODE)
            # p13-img (IMG-130): the enhancer samples with the run's seed (a fixed 0 collapsed some pictures every time)
            set_pointer(compiled, ENHANCER_SEED_POINTER, artist["seed"])
        if "latent_mask" in edit:  # p13-img: the selection as the latent noise mask (one encoder, not node 910)
            pointers = {k: v for k, v in LATENT_MASK_POINTERS.items() if k != "denoise"}
            insert_latent_mask(compiled, **pointers)
            # the snapshot's effective denoise: Remove (pre-filled) its own 50 %, else Strength through its curve, else 1.0
            set_pointer(compiled, LATENT_MASK_POINTERS["denoise"], snapshot["effective_input"]["denoise"])
        elif edit.get("strength_curve") == "start_v1":  # w24-edit2: the start on the schedule (one encoder, node 944)
            insert_strength_start(compiled, snapshot, sampler=SAMPLER_NODE, latent_key="latent_image", load_node="492", vae_node="484")
        elif "strength" in edit:  # p13-img (IMG-130): the curve-mapped denoise (strength ** 0.2), not the raw slider
            set_pointer(compiled, STRENGTH_POINTERS["denoise"], snapshot["effective_input"]["denoise"])
            if STRENGTH_ENCODE_NODE_ID in compiled:
                raise QwenEditAdapterError("strength encoder node id taken")
            compiled[STRENGTH_ENCODE_NODE_ID] = copy.deepcopy(STRENGTH_ENCODE_NODE)
            set_pointer(compiled, STRENGTH_POINTERS["latent"], [STRENGTH_ENCODE_NODE_ID, 0])
        if not edit["prompt_enhancer"]:
            prune_unchosen(compiled, ENHANCER_SWITCH_NODE)  # w24-rest MOD-017: OFF is structural, never only a switch value
        return compiled

    compile = compile_graph

    def _identity_for(self, job_id: str, client_request_id: str, snapshot_hash: str,
                      pin: tuple[str, str] | None = None) -> dict[str, Any]:
        """The correlation identity; ``pin`` = the job's own (capability, graph) pin when it was stored under an earlier one."""
        capability, graph = pin if pin is not None else (self.capability_sha256, self.graph_sha256)
        return {"provider_id": PROVIDER_ID, "image_producer_job_id": job_id, "client_request_id": client_request_id,
                "target_id": self.target_id, "request_snapshot_sha256": snapshot_hash,
                "capability_manifest_sha256": capability, "api_graph_sha256": graph}

    async def _preview_probe(self) -> tuple[bool, str | None]:
        now = time.monotonic()
        if _probe["present"] is not None and now < _probe["until"]:
            return bool(_probe["present"]), self._tiny_from_probe()
        present, tiny = False, None
        _probe["options"] = []
        probe = getattr(self.client, "object_info", None)
        if probe is not None:
            try:
                info = await probe(LIVE_PREVIEW_NODE_CLASS)
                node = info.get(LIVE_PREVIEW_NODE_CLASS) if isinstance(info, Mapping) else None
                present = isinstance(node, Mapping)
                if present:
                    spec = ((node.get("input") or {}).get("optional") or {}).get("tiny_vae")
                    options = spec[1].get("options") if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], Mapping) else None
                    if options is None and isinstance(spec, list) and spec and isinstance(spec[0], list):
                        options = spec[0]
                    tiny = LIVE_PREVIEW_TINY_VAE if isinstance(options, list) and LIVE_PREVIEW_TINY_VAE in options else None
                    _probe["options"] = list(options) if isinstance(options, list) else []
            except Exception:  # noqa: BLE001 - engine off or old: preview just off
                present, tiny = False, None
        _probe.update(present=present, tiny_vae=tiny, until=now + (_PROBE_TTL if present else _PROBE_RETRY))
        return present, self._tiny_from_probe()

    def _tiny_from_probe(self) -> str | None:
        """This family's tiny VAE when ComfyUI lists it (one shared probe for every family)."""
        if self.tiny_vae_name == LIVE_PREVIEW_TINY_VAE:
            return _probe["tiny_vae"]
        return self.tiny_vae_name if self.tiny_vae_name and self.tiny_vae_name in (_probe.get("options") or []) else None

    async def submit(self, *, image_producer_job_id: str, request_snapshot: Mapping[str, Any], client_request_id: str,
                     request_snapshot_sha256: str | None = None) -> SubmitOutcome:
        try:
            if not (isinstance(image_producer_job_id, str) and len(image_producer_job_id) == 39 and image_producer_job_id.startswith("imgjob_")):
                raise QwenEditAdapterError("job id invalid")
            if not _valid_token(client_request_id):
                raise QwenEditAdapterError("client request id invalid")
            snapshot = copy.deepcopy(dict(request_snapshot))
            snapshot_hash = sha256(validate_snapshot(snapshot))
            if request_snapshot_sha256 is not None and request_snapshot_sha256 != snapshot_hash:
                raise QwenEditAdapterError("request snapshot hash mismatch")
            compiled = self.compile_graph(snapshot)
        except (ContractError, QwenEditAdapterError, TypeError, ValueError, KeyError, RecursionError):
            return SubmitOutcome("malformed", reason="invalid_request")
        node = None
        if self.kj_preview:
            present, tiny = await self._preview_probe()
            if present:
                try:
                    compiled = insert_live_preview(compiled, tiny, self.sampler_node)
                    node = LIVE_PREVIEW_NODE_ID
                except QwenEditAdapterError:
                    node = None
        # w32-fix5: a re-run of a job stored under an earlier pin carries that pin, so its result evidence matches its row.
        pin = (snapshot["capability_manifest_sha256"], snapshot["api_graph_sha256"])
        own = pin if pin in _accepted_pins(self.target_id, self.capability_sha256, self.graph_sha256) else None
        extra: dict[str, Any] = {"studio": self._identity_for(image_producer_job_id, client_request_id, snapshot_hash, own)}
        if self.preview_method is not None:
            extra["preview_method"] = self.preview_method
        try:
            receipt = await self.client.submit(compiled, client_id=self.submit_client_id or image_producer_job_id, extra_data=extra)
        except Exception:
            return SubmitOutcome("transport_error", reason="submission_unknown")
        if not isinstance(receipt, Mapping) or receipt.get("node_errors"):
            return SubmitOutcome("malformed", reason="invalid_request")
        prompt_id = receipt.get("prompt_id")
        if not _valid_prompt_id(prompt_id):
            return SubmitOutcome("malformed", reason="submission_unknown")
        if self.kj_preview:
            _remember(prompt_id, node)
        return SubmitOutcome("accepted", provider_prompt_id=prompt_id)

    async def recover_submit(self, client_request_id: str, *, image_producer_job_id: str | None = None,
                             request_snapshot_sha256: str | None = None) -> SubmitOutcome:
        if not _valid_token(client_request_id):
            return SubmitOutcome("malformed", reason="invalid_request")
        if image_producer_job_id is None or request_snapshot_sha256 is None:
            return SubmitOutcome("unresolved", reason="submission_unknown")
        expected = self._identity_for(image_producer_job_id, client_request_id, request_snapshot_sha256)
        try:
            queue = await self.client.queue()
            history = await self.client.recent_history(max_items=100)
        except Exception:
            return SubmitOutcome("transport_error", reason="submission_unknown")
        found: set[str] = set()
        candidates: list[tuple[Any, Any]] = []
        for key in ("queue_running", "queue_pending"):
            for item in (queue.get(key) or []) if isinstance(queue, Mapping) else []:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    candidates.append((item[1], _metadata(item)))
        for pid, entry in (history.items() if isinstance(history, Mapping) else []):
            prompt = entry.get("prompt") if isinstance(entry, Mapping) else None
            candidates.append((pid, _metadata(prompt)))
        accepted = [expected] + [self._identity_for(image_producer_job_id, client_request_id, request_snapshot_sha256, pin)
                                 for pin in sorted(_accepted_pins(self.target_id, self.capability_sha256, self.graph_sha256))
                                 if pin != (self.capability_sha256, self.graph_sha256)]  # w32-fix5: an earlier pin's submit
        for pid, meta in candidates:
            if meta is not None and (meta.get("client_request_id") == client_request_id or meta.get("image_producer_job_id") == image_producer_job_id):
                if not any(_matches(meta, one) for one in accepted):
                    return SubmitOutcome("malformed", reason="submission_unknown")
                if _valid_prompt_id(pid):
                    found.add(pid)
        if len(found) != 1:
            return SubmitOutcome("unresolved", reason="submission_unknown")
        return SubmitOutcome("accepted", provider_prompt_id=next(iter(found)))

    recover = recover_submit

    async def poll(self, provider_prompt_id: str, *, expected_identity: Mapping[str, Any]) -> PollOutcome:
        if not _valid_prompt_id(provider_prompt_id):
            raise QwenEditAdapterError("provider prompt id invalid")
        expected = _identity(expected_identity, self.target_id, self.capability_sha256, self.graph_sha256)
        try:
            history = await self.client.history(provider_prompt_id)
        except Exception:
            return PollOutcome("transport_error", "missing", reason="unreachable")
        if not isinstance(history, Mapping):
            return PollOutcome("malformed", "missing", reason="invalid_history")
        entry = history.get(provider_prompt_id)
        if entry is None:
            return await self._poll_queue(provider_prompt_id, expected)
        if not isinstance(entry, Mapping):
            return PollOutcome("malformed", "missing", reason="invalid_history")
        prompt = entry.get("prompt")
        if not isinstance(prompt, (list, tuple)) or len(prompt) < 2 or prompt[1] != provider_prompt_id or not _matches(_metadata(prompt), expected):
            return PollOutcome("malformed", "missing", reason="result_authority_invalid")
        status = entry.get("status") if isinstance(entry.get("status"), Mapping) else {}
        status_str = status.get("status_str")
        if status_str in {"error", "failed"}:
            return PollOutcome("failed", "failed", reason=_comfy_failure_reason(entry))
        if status_str in {"interrupted", "cancelled"}:
            return PollOutcome("cancelled", "cancelled", reason="cancelled")
        if status_str == "success" or status.get("completed") is True:
            try:
                return PollOutcome("ready", "ready", output_identity=_png_descriptor(entry, self.output_node))
            except QwenEditAdapterError:
                return PollOutcome("malformed", "missing", reason="output_invalid")
        return await self._poll_queue(provider_prompt_id, expected)

    async def _poll_queue(self, provider_prompt_id: str, expected: Mapping[str, Any]) -> PollOutcome:
        try:
            queue = await self.client.queue()
        except Exception:
            return PollOutcome("transport_error", "missing", reason="unreachable")
        if not isinstance(queue, Mapping):
            return PollOutcome("malformed", "missing", reason="invalid_queue")
        states = []
        for key, name in (("queue_running", "running"), ("queue_pending", "queued")):
            for item in queue.get(key) or []:
                if isinstance(item, (list, tuple)) and len(item) >= 4 and item[1] == provider_prompt_id:
                    if not _matches(_metadata(item), expected):
                        return PollOutcome("malformed", "missing", reason="result_authority_invalid")
                    states.append(name)
        if len(states) > 1:
            return PollOutcome("malformed", "missing", reason="result_authority_invalid")
        if states:
            return PollOutcome(states[0], states[0])
        return PollOutcome("missing", "missing", reason="job_lost")

    async def cancel(self, provider_prompt_id: str) -> CancelOutcome:
        if not _valid_prompt_id(provider_prompt_id):
            raise QwenEditAdapterError("provider prompt id invalid")
        try:
            response = await self.client.cancel_prompt(provider_prompt_id)
        except Exception as exc:
            # A ComfyUI without the jobs API answers 404/405: stop the owned
            # prompt through the queue + targeted interrupt instead.
            if getattr(exc, "http_status", None) not in (404, 405) or not callable(getattr(self.client, "interrupt_prompt", None)):
                return CancelOutcome("transport_error", reason="unreachable")
            try:
                response = await self.client.interrupt_prompt(provider_prompt_id)
            except Exception:
                return CancelOutcome("transport_error", reason="unreachable")
        if isinstance(response, Mapping) and response.get("cancelled") is True:
            return CancelOutcome("accepted")
        return CancelOutcome("malformed", reason="invalid_cancel_receipt")

    cancel_prompt = cancel


class EditRoutingAdapter:
    """One provider seam for the shared ledger: create jobs go to the Krea adapter, edit jobs to their family.

    The coordinator is provider-neutral and holds one adapter; the ledger holds
    create and edit jobs side by side.  Routing is by the job's own target id
    (from the snapshot, the correlation identity, or the ledger row), never by
    a caller.
    """

    def __init__(self, default: Any, routes: Mapping[str, Any], target_of: Any = None) -> None:
        self.default = default
        self.routes = dict(routes)
        self.target_of = target_of
        for name in ("kj_preview", "submit_client_id", "preview_method", "client"):
            if hasattr(default, name):
                setattr(self, name, getattr(default, name))

    def _for_target(self, target: Any) -> Any:
        return self.routes.get(target, self.default) if isinstance(target, str) else self.default

    async def submit(self, **kwargs: Any) -> Any:
        snapshot = kwargs.get("request_snapshot")
        return await self._for_target(snapshot.get("target_id") if isinstance(snapshot, Mapping) else None).submit(**kwargs)

    async def recover_submit(self, client_request_id: str, **kwargs: Any) -> Any:
        target = None
        if self.target_of is not None and kwargs.get("image_producer_job_id"):
            try:
                target = self.target_of(kwargs["image_producer_job_id"])
            except Exception:  # noqa: BLE001 - unknown job: default route answers unresolved
                target = None
        return await self._for_target(target).recover_submit(client_request_id, **kwargs)

    recover = recover_submit

    async def poll(self, provider_prompt_id: str, **kwargs: Any) -> Any:
        identity = kwargs.get("expected_identity")
        return await self._for_target(identity.get("target_id") if isinstance(identity, Mapping) else None).poll(provider_prompt_id, **kwargs)

    async def cancel(self, provider_prompt_id: str) -> Any:
        return await self.default.cancel(provider_prompt_id)

    cancel_prompt = cancel

    def __getattr__(self, name: str) -> Any:
        return getattr(self.default, name)


__all__ = [
    "CAPABILITY_SHA256", "ENHANCER_GUARD_PATTERN", "EditRoutingAdapter", "GRAPH_SHA256", "LIVE_PREVIEW_NODE_ID", "MUTABLE_POINTERS",
    "QwenEditAdapterError", "QwenImageEditAdapter", "insert_strength_start", "REQUIRED_CLASSES", "TARGET_ID", "execution_ms_from_history",
    "SENT_PROMPT_NODES", "STRENGTH_ENCODE_NODE_ID", "insert_enhancer_guard", "insert_latent_mask", "prune_unchosen", "LATENT_MASK_POINTERS", "STRENGTH_POINTERS", "insert_live_preview", "live_preview_known", "live_preview_node", "sent_prompt_from_history",
]
