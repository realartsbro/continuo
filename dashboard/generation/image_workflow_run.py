"""Batch and LoRAs for every catalog Image target (lane w1-imgout).

Before this, Batch and LoRAs existed for the native Krea 2 create only (krea2_render_spec v2).
Catalog targets (workflow_create / workflow_edit) compile their pinned graph through their own
adapter; this module adds, without touching any pinned manifest or graph:

* Batch 1..8 (the bounds of krea2_render_spec.V2_BATCH_MIN/MAX): N ledger jobs, picture i with
  seed + i, on the unchanged single-image publication seam (the route does the fan-out).
* A LoRA list ``[{file, strength, role?}]``: a chain of core ``LoraLoaderModelOnly`` nodes
  inserted after the target's model node (named per target below, never inferred), every
  consumer of that node rewired to the chain's end.  Files come from the target's own
  ``models/loras`` folder; a LoRA whose base model is known and is another family is refused
  (the Krea browser's family filter, as a rule).

Per target the LoRA support is ``rendered`` (offered: rendered for real with a matching LoRA,
see LORA_EVIDENCE), ``wired`` (offered: the graph takes a chain, no render record yet; lane w39-face 6,
the artist's decision "Alle Models akzeptieren theoretisch LoRAs") or ``unsupported`` (refused
with the reason).  A request without LoRAs keeps the exact
historical request bytes (no ``loras`` key), so every persisted job validates unchanged.
"""
from __future__ import annotations

import copy
import math
import os
import re
from typing import Any, Mapping

BATCH_MIN = 1
BATCH_MAX = 8
LORA_MAX = 32  # krea2_render_spec.LORA_MAX: a sanity bound, not a creative limit
LORA_NODE_BASE = 1200  # above every pinned graph id and Qwen's reference nodes (1000..1015)
STRENGTH_MIN = 0.0
STRENGTH_MAX = 2.0
ROLES = ("style", "character", "subject", "detail", "other")
_NAME = re.compile(r"(?![/.])(?!.*(?:^|/)\.{1,2}(?:/|$))[^\\:*?\"<>|\x00-\x1f]{1,240}\.safetensors")

# The model node a LoRA chain follows, per pinned target (read from the graphs, see the tests):
# the UNETLoader, or the target's own fixed LoRA (Qwen's 8-step, Krea's edit LoRA) when it has one.
LORA_ROOTS: dict[str, str] = {
    "anima-turbo-create-v1": "10",
    "flux2-klein-9b-t2i-v1": "10",
    "flux3-klein-sd-create-v1": "10",
    "moss-image-sft-create-v1": "10",
    "qwen-image-2.1-create-pe-v3-v2": "496",
    "qwen-image-2.1-edit-pe-v3-v2": "496",
    "krea2-general-edit-v2": "13",
    "krea2-identity-edit-v2": "13",
    # w24-h3img: the H3 FL2VA UNETLoader; the artist's LoRAs sit before P-H3-Image's own bypass LoRA (node 13)
    "p-h3-image-lora-t2i-v1": "10",
    "p-h3-image-lora-edit-v1": "10",
    # w39-p2-open: Ideogram 4 renders with two models through one DualModelGuider: the conditional UNETLoader (10,
    # into CFGOverride 16 -> guider ``model``) and the unconditional one (11, guider ``model_negative``). The artist's
    # LoRAs go on both, the same files in the same order at the same strengths (LORA_ALSO), as the community graphs
    # do ("load the same LoRA in both"; kat3ri/ComfyUI-ideogram4-regional-lora README). They are two different
    # weight files, so each loader patches its own model (ComfyUI #14521 is about one file used twice).
    "ideogram4-create-v1": "10",
}
# Further model nodes that take the same chain (a second copy, its own node ids after the first chain).
LORA_ALSO: dict[str, tuple[str, ...]] = {
    "ideogram4-create-v1": ("11",),
}
UNSUPPORTED: dict[str, str] = {
    # w6-gptimage: a subscription model made by OpenAI's service; nothing local to load a LoRA into.
    "gpt-image-2-create-v1": "GPT Image is made by your ChatGPT (Codex) subscription; it takes no LoRA",
    "gpt-image-2-edit-v1": "GPT Image is made by your ChatGPT (Codex) subscription; it takes no LoRA",
}
# One picture per request on a subscription target (no seed to vary, every picture is one paid request).
SINGLE_PICTURE = frozenset({"gpt-image-2-create-v1", "gpt-image-2-edit-v1"})
# Base-model label per catalog family (lora_library.base_family labels).
FAMILY_LABELS: dict[str, str] = {
    "anima": "Anima", "flux2_klein": "FLUX.2 Klein", "flux3_klein": "FLUX.3 Klein", "moss_image": "MOSS-Image",
    "qwen_image_2_1": "Qwen-Image 2.1", "krea2": "Krea 2", "ideogram4": "Ideogram 4",
    "p_h3_image": "MiniMax H3",  # w24-h3img: H3 LoRAs (lora_library names them MiniMax H3) fit the FL2VA transformer
}
# Real renders with one matching LoRA through the Suite (lane w1-imgout, 2026-10-21; seconds = route time).
# w39-face 6: every LORA_ROOTS target is offered; a render record only changes the reason (the tooltip), not the offer.
# w27-catalog: the rows name no LoRA file (the renders used the artist's private LoRAs; the published tree and the
# agent catalog name none of them). The reason says which family was rendered, never which file.
LORA_EVIDENCE: dict[str, dict[str, Any]] = {
    "anima-turbo-create-v1": {"date": "2026-10-21", "seconds": 4.6},
    "qwen-image-2.1-create-pe-v3-v2": {"date": "2026-10-21", "seconds": 6.8},
    "krea2-general-edit-v2": {"date": "2026-10-21", "seconds": 12.5},
}


class RunError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def lora_support(target_id: str, family_id: str) -> dict[str, Any]:
    """What one catalog target says about LoRAs (catalog + agent catalog + refusals)."""
    label = FAMILY_LABELS.get(family_id, family_id)
    if target_id in UNSUPPORTED:
        return {"state": "unsupported", "available": False, "family": label, "reason": UNSUPPORTED[target_id]}
    if target_id not in LORA_ROOTS:
        return {"state": "unsupported", "available": False, "family": label, "reason": "This model's workflow takes no LoRA"}
    proof = LORA_EVIDENCE.get(target_id)
    offered = {"available": True, "family": label, "max": LORA_MAX, "strength": {"min": STRENGTH_MIN, "max": STRENGTH_MAX}, "roles": list(ROLES)}
    if proof is None:
        return {"state": "wired", **offered, "reason": f"No render with {'an' if label[:1].upper() in 'AEIOU' else 'a'} {label} LoRA on record"}
    return {"state": "rendered", **offered,
            "reason": f"Rendered with {'an' if label[:1].upper() in 'AEIOU' else 'a'} {label} LoRA on {proof['date']}"}


def run_descriptor(target_id: str, family_id: str) -> dict[str, Any]:
    """The ``run`` block a catalog descriptor gains: batch bounds and LoRA support."""
    if target_id in SINGLE_PICTURE:
        return {"batch": {"min": 1, "max": 1, "seed": "none"}, "loras": lora_support(target_id, family_id)}
    return {"batch": {"min": BATCH_MIN, "max": BATCH_MAX, "seed": "seed + index"}, "loras": lora_support(target_id, family_id)}


def normalize_loras(value: Any) -> list[dict[str, Any]]:
    """Canonical LoRA list (shape only; installed/family checks happen at admission)."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > LORA_MAX:
        raise RunError("invalid_loras", f"LoRAs are a list of at most {LORA_MAX}")
    out, seen = [], set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) - {"file", "strength", "role"} or "file" not in item:
            raise RunError("invalid_loras", "A LoRA is {file, strength, role?}")
        name = item["file"]
        if type(name) is not str or _NAME.fullmatch(name) is None or any(p != p.strip() or not p for p in name.split("/")) or name in seen:
            raise RunError("invalid_loras", "A LoRA file name is invalid or listed twice")
        seen.add(name)
        strength = item.get("strength", 1.0)
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not math.isfinite(strength) \
                or not STRENGTH_MIN <= float(strength) <= STRENGTH_MAX:
            raise RunError("invalid_loras", "LoRA strength is 0 to 2")
        row: dict[str, Any] = {"file": name, "strength": round(float(strength), 4)}
        if "role" in item:
            if item["role"] not in ROLES:
                raise RunError("invalid_loras", "A LoRA role is " + ", ".join(ROLES))
            row["role"] = item["role"]
        out.append(row)
    return out


def check_batch(value: Any) -> int:
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or not BATCH_MIN <= value <= BATCH_MAX:
        raise RunError("invalid_batch", f"Batch is {BATCH_MIN} to {BATCH_MAX} pictures")
    return value


def admit_loras(target_id: str, family_id: str, loras: list[dict[str, Any]], live_names: list[str] | None,
                base_of: Any) -> None:
    """Refuse what this target cannot take: unsupported, not listed, another family."""
    if not loras:
        return
    support = lora_support(target_id, family_id)
    if not support["available"]:
        raise RunError("loras_unavailable", f"LoRAs are not available for this model: {support['reason']}")
    for item in loras:
        if live_names is not None and item["file"] not in live_names:
            raise RunError("lora_unknown", f"The image engine does not list {item['file']}")
        base = base_of(item["file"]) or ""
        if base and base != support["family"]:
            raise RunError("lora_family_mismatch", f"{item['file']} is a {base} LoRA; this model is {support['family']}")


def apply_loras(graph: dict[str, Any], target_id: str, loras: list[dict[str, Any]]) -> dict[str, Any]:
    """Insert the LoRA chain after the target's model node(s) and rewire their consumers (in place).

    A target with more than one model node (LORA_ALSO, Ideogram 4's conditional and unconditional models) gets one
    chain per node: the same files, order and strengths; the first chain keeps ids LORA_NODE_BASE.. as before."""
    if not loras:
        return graph
    root = LORA_ROOTS.get(target_id)
    roots = ((root,) if root is not None else ()) + LORA_ALSO.get(target_id, ())
    if root is None or any(r not in graph for r in roots):
        raise RunError("loras_unavailable", UNSUPPORTED.get(target_id, "This model's workflow takes no LoRA"))
    ids = [str(LORA_NODE_BASE + i) for i in range(len(loras) * len(roots))]
    if any(node_id in graph for node_id in ids):
        raise RunError("loras_unavailable", "LoRA node collision")
    consumers = {r: [(node_id, key) for node_id, node in graph.items() for key, value in node["inputs"].items()
                     if isinstance(value, list) and len(value) == 2 and value[0] == r and value[1] == 0] for r in roots}
    for index, model_node in enumerate(roots):
        parent = model_node
        for node_id, item in zip(ids[index * len(loras):(index + 1) * len(loras)], loras):
            graph[node_id] = {"class_type": "LoraLoaderModelOnly",
                              "inputs": {"model": [parent, 0], "lora_name": item["file"].replace("/", os.sep), "strength_model": item["strength"]}}
            parent = node_id
        for node_id, key in consumers[model_node]:
            graph[node_id]["inputs"][key] = [parent, 0]
    return graph


def strip(request: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(the adapter's request without ``loras``, the LoRA list)."""
    base = {k: copy.deepcopy(v) for k, v in request.items() if k != "loras"}
    return base, [dict(item) for item in request.get("loras") or []]


def compile_request(adapter: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """The adapter's pinned graph for the request, plus the LoRA chain."""
    base, loras = strip(request)
    if "loras" in request and not loras:
        raise RunError("invalid_loras", "An empty LoRA list is sent as no list")
    graph = adapter.compile(base)
    return apply_loras(graph, request["target_id"], normalize_loras(loras)) if loras else graph


def lora_health(graph: Mapping[str, Any], object_info: Mapping[str, Any]) -> list[str]:
    """LoRA files of the chain that the engine's LoraLoaderModelOnly does not list."""
    names = [n["inputs"]["lora_name"] for k, n in graph.items() if k.isdigit() and int(k) >= LORA_NODE_BASE
             and n.get("class_type") == "LoraLoaderModelOnly"]
    if not names:
        return []
    spec = (((object_info.get("LoraLoaderModelOnly") or {}).get("input") or {}).get("required") or {}).get("lora_name")
    options = spec[0] if isinstance(spec, list) and spec and isinstance(spec[0], list) else []
    norm = {str(o).replace("\\", "/") for o in options}
    return [n for n in names if n.replace("\\", "/") not in norm]


def member_controls(controls: Mapping[str, Any], seed_default: int, index: int) -> dict[str, Any]:
    """Picture ``index`` of a batch: the same controls with seed + index."""
    out = dict(controls)
    out["seed"] = (int(controls.get("seed", seed_default)) + index) % 18_446_744_073_709_551_616
    return out


__all__ = ["BATCH_MAX", "BATCH_MIN", "FAMILY_LABELS", "LORA_ALSO", "LORA_EVIDENCE", "LORA_MAX", "LORA_NODE_BASE", "LORA_ROOTS", "ROLES",
           "RunError", "UNSUPPORTED", "admit_loras", "apply_loras", "check_batch", "compile_request", "lora_health",
           "lora_support", "member_controls", "normalize_loras", "run_descriptor", "strip"]
