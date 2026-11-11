"""Shared model & capability layer (Film Suite masterplan package 1).

One typed, versioned catalog of model families -> variants -> operations,
input slots, required model files per role, prompt grammar and presets
(``model_catalog.v1.json``; every value carries a source line from
``docs/references/FILM-SUITE-CAPABILITY-INVENTORY.md``).  This module:

* loads and strictly validates the catalog (closed keys, finite numbers,
  integer/float kinds fixed per key so ``1`` and ``1.0`` never drift);
* discovers model file NAMES per ComfyUI folder kind from the ComfyUI root and
  ``extra_model_paths.yaml`` without following links, bounded by depth and
  entry count;
* reports per-variant health (present/missing files by exact name, missing
  node classes when ``/object_info`` was reachable);
* projects a path-free public catalog for ``POST /models/catalog``.

Stdlib only, plus PyYAML when available (the Hermes runtime ships it).  No
relative imports, so plugin_api can load this file without registering it in
the synthetic generation package.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = "studio.model-catalog/1"
IMAGE_WORKFLOW_ADAPTERS = {
    "qwen_image_2_1": "qwen_image_workflow_adapter",
    "anima": "catalog_create_workflow_adapter",
    "flux2_klein": "catalog_create_workflow_adapter",
    "ideogram4": "catalog_create_workflow_adapter",
    "moss_image": "catalog_create_workflow_adapter",
    "flux3_klein": "catalog_create_workflow_adapter",
    "p_h3_image": "p_h3_image_workflow_adapter",  # w24-h3img: P-H3-Image LoRA on the H3 FL2VA transformer (create + edit)
    "krea2": "krea2_edit_workflow_adapter",
    # w6-gptimage: GPT Image through the artist's ChatGPT (Codex) subscription (Hermes' openai-codex provider).
    "gpt_image": "paid.subscription_image_provider",  # w20-cat: in generation/paid/ (absent in the catalog build)
}

CATALOG_PATH = Path(__file__).resolve().with_name("model_catalog.v1.json")
_RUN_MODULE: list[Any] = []


def _workflow_run() -> Any:
    """image_workflow_run.py by path (this module is also loaded standalone by the agent tools)."""
    if not _RUN_MODULE and __package__:
        from importlib import import_module
        _RUN_MODULE.append(import_module(__package__ + ".image_workflow_run"))  # one module: one LORA_EVIDENCE
    if not _RUN_MODULE:
        import importlib.util
        spec = importlib.util.spec_from_file_location("film_suite_image_workflow_run", Path(__file__).resolve().with_name("image_workflow_run.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RUN_MODULE.append(module)
    return _RUN_MODULE[0]
# Real renders through the Suite (studio.model-evidence/1): a pickable Image
# model needs a rendered entry; a failed or absent entry means not offered.
EVIDENCE_PATH = Path(__file__).resolve().with_name("model_evidence.v1.json")
EVIDENCE_SCHEMA = "studio.model-evidence/1"
EVIDENCE_STATES = frozenset({"rendered", "failed"})
MAX_EVIDENCE_BYTES = 64 * 1024
# Same install the Krea render spec checks (krea2_render_spec.MODEL_ROOT.parent).
if __package__:
    from .comfy_root import comfy_root  # FILM_SUITE_COMFY_ROOT: the Suite's one ComfyUI root (w7-rest)
else:  # loaded by file path (plugin_api's route bootstrap, the agent tools)
    import importlib.util as _root_util
    _root_spec = _root_util.spec_from_file_location("film_suite_comfy_root", Path(__file__).resolve().with_name("comfy_root.py"))
    _root_module = _root_util.module_from_spec(_root_spec)
    _root_spec.loader.exec_module(_root_module)
    comfy_root = _root_module.comfy_root
MAX_CATALOG_BYTES = 512 * 1024
MAX_YAML_BYTES = 64 * 1024
MAX_DEPTH = 3
MAX_ENTRIES = 4000
MAX_SCAN = 20000
MODEL_SUFFIXES = frozenset({".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft"})

# ComfyUI folder kinds this layer reads, with ComfyUI's default sub-folders
# (comfy folder_paths: diffusion_models also reads unet, text_encoders clip).
FOLDER_KINDS: dict[str, tuple[str, ...]] = {
    "diffusion_models": ("diffusion_models", "unet"),
    "text_encoders": ("text_encoders", "clip"),
    "vae": ("vae",),
    "loras": ("loras",),
    "model_patches": ("model_patches",),
    "latent_upscale_models": ("latent_upscale_models",),
    "frame_interpolation": ("frame_interpolation",),
    # Voice (lane fs-voice): OmniVoice weights, Whisper (audio_encoders).
    "omnivoice": ("omnivoice",),
    "audio_encoders": ("audio_encoders",),
}
OPERATIONS = frozenset({"create", "edit", "multi_reference", "i2i", "inpaint", "control", "outpaint", "t2v", "i2v", "fflf", "r2v",
                        "speak_clone", "speak_design"})
INPUT_KINDS = frozenset({"image", "mask", "video", "audio"})
FAMILY_KINDS = frozenset({"image", "video", "voice"})
PROMPT_GRAMMARS = frozenset({
    "krea_prose", "krea_edit_instruction", "anima_booru_hybrid", "ideogram_json",
    "klein_image_n", "qwen_image_n_enhancer", "h3_picture_n_literal", "moss_prose", "flux3_upsampled_prose",
    "spoken_line",
    "gpt_image_prose",
})
_INT_VALUES = frozenset({"steps", "steps_max", "grounding_px", "top_k"})
_FLOAT_VALUES = frozenset({"cfg", "denoise", "mu", "std", "lora_strength", "ref_boost", "sigma_shift_video", "sigma_shift_audio", "megapixels",
                           "speed", "temperature", "top_p"})
_STR_VALUES = frozenset({"sampler", "scheduler"})
HEALTH_STATES = frozenset({"ready", "unchecked", "missing"})

_ID = re.compile(r"^[a-z0-9][a-z0-9_]{0,39}$")
_TARGET = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CLASS = re.compile(r"^[A-Za-z0-9_]{1,80}$")
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._+-]{0,159}"
_NAME = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT}){{0,2}}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_FAMILY_KEYS = frozenset({"id", "label", "kind", "prompt_grammar", "variants"})
# w6-gptimage: a family served by a subscription provider (no model files) names its provider group.
_PROVIDER_GROUP_KEYS = frozenset({"id", "label", "billing"})
BILLINGS = frozenset({"subscription"})
_VARIANT_KEYS = frozenset({"id", "label", "operations", "prompt_grammar", "inputs", "files", "required_classes",
                           "presets", "lora_max", "resolution", "stages", "notes", "suite_targets", "source"})
_INPUT_KEYS = frozenset({"id", "kind", "role", "min", "max", "notes"})
_FILE_KEYS = frozenset({"folder", "names", "required"})
_PRESET_KEYS = frozenset({"id", "label", "values", "source"})
_RESOLUTION_KEYS = frozenset({"min_px", "max_px", "multiple", "default_megapixels"})
_STAGE_KEYS = frozenset({"id", "label", "options", "classes", "notes"})


class CatalogError(ValueError):
    """The model catalog (or a request against it) is invalid."""


# --- strict parsing -----------------------------------------------------------

def _fail(where: str) -> None:
    raise CatalogError(f"model catalog invalid at {where}")


def _closed(value: Any, keys: frozenset[str], where: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        _fail(where)
    return value


def _text(value: Any, where: str, *, max_len: int = 400, allow_empty: bool = False) -> str:
    if type(value) is not str or len(value) > max_len or _CONTROL.search(value) or (not allow_empty and not value.strip()):
        _fail(where)
    return value


def _ident(value: Any, where: str, pattern: re.Pattern[str] = _ID) -> str:
    if type(value) is not str or not pattern.fullmatch(value):
        _fail(where)
    return value


def _int(value: Any, where: str, lo: int, hi: int, *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or not lo <= value <= hi:
        _fail(where)
    return value


def _finite(value: Any, where: str, *, nullable: bool = False) -> float | int | None:
    if value is None and nullable:
        return None
    if type(value) not in (int, float) or not math.isfinite(value):
        _fail(where)
    return value


def _unique_list(value: Any, where: str, *, min_len: int = 0, max_len: int = 64) -> list[Any]:
    if type(value) is not list or not min_len <= len(value) <= max_len:
        _fail(where)
    seen = [json.dumps(item, sort_keys=True) for item in value]
    if len(set(seen)) != len(seen):
        _fail(where)
    return value


def _parse_inputs(value: Any, where: str) -> None:
    ids = set()
    for index, slot in enumerate(_unique_list(value, where, max_len=16)):
        at = f"{where}[{index}]"
        _closed(slot, _INPUT_KEYS, at)
        _ident(slot["id"], f"{at}.id")
        _ident(slot["role"], f"{at}.role")
        if slot["kind"] not in INPUT_KINDS:
            _fail(f"{at}.kind")
        maximum = _int(slot["max"], f"{at}.max", 1, 16)
        _int(slot["min"], f"{at}.min", 0, maximum)
        _text(slot["notes"], f"{at}.notes", allow_empty=True)
        if slot["id"] in ids:
            _fail(f"{at}.id")
        ids.add(slot["id"])


def _parse_files(value: Any, where: str) -> None:
    if type(value) is not dict or not value or len(value) > 16:
        _fail(where)
    for role, spec in value.items():
        at = f"{where}.{role}"
        _ident(role, at)
        _closed(spec, _FILE_KEYS, at)
        if spec["folder"] not in FOLDER_KINDS:
            _fail(f"{at}.folder")
        for index, name in enumerate(_unique_list(spec["names"], f"{at}.names", min_len=1, max_len=16)):
            if type(name) is not str or not _NAME.fullmatch(name) or ".." in name.split("/"):
                _fail(f"{at}.names[{index}]")
        if type(spec["required"]) is not bool:
            _fail(f"{at}.required")


def _parse_classes(value: Any, where: str) -> None:
    for index, entry in enumerate(_unique_list(value, where, max_len=64)):
        at = f"{where}[{index}]"
        if type(entry) is list:
            if not 2 <= len(entry) <= 4 or len(set(map(str, entry))) != len(entry):
                _fail(at)
            for alt in entry:
                _ident(alt, at, _CLASS)
        else:
            _ident(entry, at, _CLASS)


def _parse_presets(value: Any, where: str) -> None:
    ids = set()
    for index, preset in enumerate(_unique_list(value, where, min_len=1, max_len=16)):
        at = f"{where}[{index}]"
        _closed(preset, _PRESET_KEYS, at)
        _ident(preset["id"], f"{at}.id")
        _text(preset["label"], f"{at}.label", max_len=60)
        _text(preset["source"], f"{at}.source")
        values = preset["values"]
        if type(values) is not dict or not values:
            _fail(f"{at}.values")
        for key, item in values.items():
            if key in _INT_VALUES:
                _int(item, f"{at}.values.{key}", 0, 10_000)
            elif key in _FLOAT_VALUES:
                if type(item) is not float or not math.isfinite(item):
                    _fail(f"{at}.values.{key}")
            elif key in _STR_VALUES:
                _ident(item, f"{at}.values.{key}")
            else:
                _fail(f"{at}.values.{key}")
        if preset["id"] in ids:
            _fail(f"{at}.id")
        ids.add(preset["id"])


def _parse_resolution(value: Any, where: str) -> None:
    if value is None:
        return
    _closed(value, _RESOLUTION_KEYS, where)
    _int(value["min_px"], f"{where}.min_px", 1, 16384, nullable=True)
    _int(value["max_px"], f"{where}.max_px", 1, 16384, nullable=True)
    _int(value["multiple"], f"{where}.multiple", 1, 256, nullable=True)
    mp = _finite(value["default_megapixels"], f"{where}.default_megapixels", nullable=True)
    if mp is not None and not 0 < mp <= 16:
        _fail(f"{where}.default_megapixels")
    if value["min_px"] is not None and value["max_px"] is not None and value["min_px"] > value["max_px"]:
        _fail(where)


def _parse_stages(value: Any, where: str) -> None:
    ids = set()
    for index, stage in enumerate(_unique_list(value, where, max_len=16)):
        at = f"{where}[{index}]"
        _closed(stage, _STAGE_KEYS, at)
        _ident(stage["id"], f"{at}.id")
        _text(stage["label"], f"{at}.label", max_len=60)
        for option in _unique_list(stage["options"], f"{at}.options", min_len=1, max_len=16):
            _ident(option, f"{at}.options")
        for name in _unique_list(stage["classes"], f"{at}.classes", max_len=32):
            _ident(name, f"{at}.classes", _CLASS)
        for note in _unique_list(stage["notes"], f"{at}.notes", max_len=8):
            _text(note, f"{at}.notes")
        if stage["id"] in ids:
            _fail(f"{at}.id")
        ids.add(stage["id"])


def _parse_workflow(value: Any, where: str) -> None:
    """Optional additive Image descriptor; private asset pins never project."""
    _closed(value, frozenset({"manifest_sha256", "api_sha256", "descriptor"}), where)
    for key in ("manifest_sha256", "api_sha256"):
        if type(value[key]) is not str or not re.fullmatch(r"[0-9a-f]{64}", value[key]):
            _fail(f"{where}.{key}")
    desc = _closed(value["descriptor"], frozenset({"descriptor_version", "target_id", "request_schema", "availability",
                   "reason", "controls", "media_roles", "capabilities", "output"}), where)
    if type(desc["descriptor_version"]) is not int or desc["descriptor_version"] != 1:
        _fail(where)
    # Image targets historically contain version dots (Qwen 2.1); keep the
    # older Shoot suite-target grammar unchanged and reject path syntax.
    _ident(desc["target_id"], where, re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$"))
    if len(desc["target_id"]) > 64:
        _fail(where)
    if desc["request_schema"] != "studio.image-workflow-request/1" or desc["availability"] != "backend_only":
        _fail(where)
    _text(desc["reason"], where)
    caps = _closed(desc["capabilities"], frozenset({"create", "edit", "multi_reference", "prompt_enhancer", "effective_prompt", "native_mask"}), where)
    if any(type(v) is not bool for v in caps.values()) or caps["create"] == caps["edit"]:
        _fail(where)
    output = _closed(desc["output"], frozenset({"kind", "format", "size_policy"}), where)
    if output["kind"] != "image" or output["format"] != "png" or output["size_policy"] not in ("explicit", "first_reference"):
        _fail(where)
    ids = set()
    for control in _unique_list(desc["controls"], where, min_len=1, max_len=32):
        mandatory = {"id", "label", "kind", "default"}
        if type(control) is not dict or not mandatory <= set(control) or set(control) - mandatory - {"min", "max", "step", "options"}:
            _fail(where)
        _ident(control["id"], where)
        _text(control["label"], where, max_len=80)
        if control["id"] in ids:
            _fail(where)
        ids.add(control["id"])
        kind, default = control["kind"], control["default"]
        if kind in ("integer", "number"):
            lo, hi = _finite(control.get("min"), where), _finite(control.get("max"), where)
            _finite(default, where)
            if lo > hi or not lo <= default <= hi or "options" in control:
                _fail(where)
            if kind == "integer" and any(type(v) is not int for v in (lo, hi, default, control.get("step", 1))):
                _fail(where)
            if "step" in control and _finite(control["step"], where) <= 0:
                _fail(where)
        elif kind == "select":
            options = _unique_list(control.get("options"), where, min_len=1, max_len=128)
            for option in options:
                _text(option, where, max_len=100)
            if type(default) is not str or default not in options or set(control) != mandatory | {"options"}:
                _fail(where)
        elif kind == "boolean":
            if type(default) is not bool or set(control) != mandatory:
                _fail(where)
        elif kind == "textarea":
            _text(default, where, max_len=16384, allow_empty=True)
            if set(control) != mandatory:
                _fail(where)
        else:
            _fail(where)
    role_ids = set()
    for role in _unique_list(desc["media_roles"], where, max_len=16):
        _closed(role, frozenset({"id", "kind", "min", "max", "roles", "label"}), where)
        _ident(role["id"], where)
        _text(role["label"], where, max_len=80)
        if role["kind"] != "image" or role["id"] in role_ids:
            _fail(where)
        role_ids.add(role["id"])
        maximum = _int(role["max"], where, 1, 16)
        _int(role["min"], where, 0, maximum)
        for name in _unique_list(role["roles"], where, min_len=1, max_len=16):
            _ident(name, where)


def _parse_variant(variant: Any, where: str, *, subscription: bool = False) -> None:
    optional = {"workflow"} if type(variant) is dict and "workflow" in variant else set()
    _closed(variant, _VARIANT_KEYS | optional, where)
    if optional:
        _parse_workflow(variant["workflow"], f"{where}.workflow")
    _ident(variant["id"], f"{where}.id")
    _text(variant["label"], f"{where}.label", max_len=60)
    for operation in _unique_list(variant["operations"], f"{where}.operations", min_len=1, max_len=16):
        if operation not in OPERATIONS:
            _fail(f"{where}.operations")
    if variant["prompt_grammar"] not in PROMPT_GRAMMARS:
        _fail(f"{where}.prompt_grammar")
    _parse_inputs(variant["inputs"], f"{where}.inputs")
    if subscription:  # nothing local: no files, no nodes
        if variant["files"] != {} or variant["required_classes"] != []:
            _fail(f"{where}.files")
    else:
        _parse_files(variant["files"], f"{where}.files")
    _parse_classes(variant["required_classes"], f"{where}.required_classes")
    if not (subscription and variant["presets"] == []):  # a subscription model has no sampler presets
        _parse_presets(variant["presets"], f"{where}.presets")
    _int(variant["lora_max"], f"{where}.lora_max", 0, 64, nullable=True)
    _parse_resolution(variant["resolution"], f"{where}.resolution")
    _parse_stages(variant["stages"], f"{where}.stages")
    for note in _unique_list(variant["notes"], f"{where}.notes", max_len=8):
        _text(note, f"{where}.notes")
    for target in _unique_list(variant["suite_targets"], f"{where}.suite_targets", max_len=8):
        _ident(target, f"{where}.suite_targets", _TARGET)
    _text(variant["source"], f"{where}.source")


def parse_catalog(value: Any) -> dict[str, Any]:
    """Validate a decoded catalog and return a private deep copy."""
    _closed(value, frozenset({"schema", "families"}), "catalog")
    if value["schema"] != SCHEMA:
        _fail("schema")
    families = _unique_list(value["families"], "families", min_len=1, max_len=32)
    family_ids = set()
    for index, family in enumerate(families):
        at = f"families[{index}]"
        group = family.get("provider_group") if type(family) is dict else None
        _closed(family, _FAMILY_KEYS | ({"provider_group"} if group is not None else set()), at)
        if group is not None:
            _closed(group, _PROVIDER_GROUP_KEYS, f"{at}.provider_group")
            _ident(group["id"], f"{at}.provider_group.id", re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$"))
            _text(group["label"], f"{at}.provider_group.label", max_len=60)
            if group["billing"] not in BILLINGS:
                _fail(f"{at}.provider_group.billing")
        _ident(family["id"], f"{at}.id")
        _text(family["label"], f"{at}.label", max_len=60)
        if family["kind"] not in FAMILY_KINDS:
            _fail(f"{at}.kind")
        if family["prompt_grammar"] not in PROMPT_GRAMMARS:
            _fail(f"{at}.prompt_grammar")
        variant_ids = set()
        for v_index, variant in enumerate(_unique_list(family["variants"], f"{at}.variants", min_len=1, max_len=16)):
            _parse_variant(variant, f"{at}.variants[{v_index}]", subscription=group is not None)
            if variant["id"] in variant_ids:
                _fail(f"{at}.variants[{v_index}].id")
            variant_ids.add(variant["id"])
        if family["id"] in family_ids:
            _fail(f"{at}.id")
        family_ids.add(family["id"])
    return copy.deepcopy(value)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in pairs:
        if key in out:
            raise CatalogError("model catalog has a duplicate key")
        out[key] = item
    return out


def _reject_constant(token: str) -> Any:
    raise CatalogError("model catalog has a non-finite number")


def load_catalog(path: Path | None = None) -> dict[str, Any]:
    """Read, strictly decode and validate the catalog file (deep copy).

    The copy is the caller's to mutate.  Read-only hot paths use
    :func:`catalog_view` instead, which shares one validated instance.
    """
    if path is None:
        return copy.deepcopy(catalog_view())
    return _load(path)


def _load(path: Path) -> dict[str, Any]:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise CatalogError("model catalog unavailable") from exc
    if len(data) > MAX_CATALOG_BYTES:
        raise CatalogError("model catalog too large")
    try:
        decoded = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except CatalogError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise CatalogError("model catalog is not valid JSON") from exc
    return parse_catalog(decoded)


_VIEW_LOCK = threading.Lock()
_VIEW: tuple[tuple[str, int, int], dict[str, Any]] | None = None


def catalog_view() -> dict[str, Any]:
    """The default catalog, validated once per file version and SHARED.

    Read-only: callers must not mutate the result (use :func:`load_catalog`
    for a private copy).  The cache key is ``(path, mtime_ns, size)`` of
    ``CATALOG_PATH``, so an edited catalog file is picked up on the next call.
    """
    global _VIEW
    path = Path(CATALOG_PATH)
    try:
        status = os.stat(path)
    except OSError as exc:
        raise CatalogError("model catalog unavailable") from exc
    key = (str(path), status.st_mtime_ns, status.st_size)
    cached = _VIEW
    if cached is not None and cached[0] == key:
        return cached[1]
    with _VIEW_LOCK:
        cached = _VIEW
        if cached is not None and cached[0] == key:
            return cached[1]
        catalog = _load(path)
        _VIEW = (key, catalog)
        return catalog


def find_variant(catalog: Mapping[str, Any], family_id: str, variant_id: str) -> dict[str, Any]:
    for family in catalog["families"]:
        if family["id"] == family_id:
            for variant in family["variants"]:
                if variant["id"] == variant_id:
                    return variant
    raise CatalogError("unknown model family or variant")


def krea_profile(profile: str) -> dict[str, Any]:
    """Krea 2 Turbo/Raw file roles and preset, the single source for the
    existing ``krea2-turbo-t2i-create`` target."""
    if profile not in ("turbo", "raw"):
        raise CatalogError("unknown Krea profile")
    variant = find_variant(catalog_view(), "krea2", profile)
    files = variant["files"]
    values = variant["presets"][0]["values"]
    return copy.deepcopy({
        "model": files["diffusion_model"]["names"][0],
        "encoder": files["text_encoder"]["names"][0],
        "vae": files["vae"]["names"][0],
        "preset": {k: values[k] for k in ("steps", "cfg", "sampler", "scheduler") if k in values},
    })


# --- file discovery ---------------------------------------------------------

def _is_link(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _current_root() -> Path:
    return comfy_root()


def _read_extra_paths(comfy_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    config = comfy_root / "extra_model_paths.yaml"
    try:
        if _is_link(config) or not config.is_file() or config.stat().st_size > MAX_YAML_BYTES:
            return []
        text = config.read_text(encoding="utf-8")
        import yaml  # PyYAML ships with the Hermes runtime; absent -> no extra roots.
        data = yaml.safe_load(text)
    except Exception:  # noqa: BLE001 - unreadable/invalid config only removes extra roots
        return []
    if type(data) is not dict:
        return []
    out = []
    for section in list(data.values())[:16]:
        if type(section) is not dict or type(section.get("base_path")) is not str:
            continue
        base = Path(os.path.expandvars(os.path.expanduser(section["base_path"].strip())))
        if not base.is_absolute():
            base = comfy_root / base
        out.append((base, section))
    return out


def model_roots(root: Path | None = None) -> dict[str, tuple[Path, ...]]:
    """Folder roots per kind: ComfyUI defaults first, then extra_model_paths (root: FILM_SUITE_COMFY_ROOT when None)."""
    comfy_root = Path(root) if root is not None else _current_root()
    roots: dict[str, list[Path]] = {
        kind: [comfy_root / "models" / sub for sub in subs] for kind, subs in FOLDER_KINDS.items()
    }
    for base, section in _read_extra_paths(comfy_root):
        for kind in FOLDER_KINDS:
            value = section.get(kind)
            if type(value) is not str:
                continue
            for line in value.splitlines()[:8]:
                line = line.strip()
                if line:
                    candidate = base / line
                    if candidate not in roots[kind]:
                        roots[kind].append(candidate)
    return {kind: tuple(paths) for kind, paths in roots.items()}


def _scan_root(root: Path, names: set[str], max_entries: int, budget: list[int]) -> bool:
    """Collect model file names under ``root``; True when bounds cut the scan."""
    if _is_link(root) or not root.is_dir():
        return False
    stack: list[tuple[Path, str, int]] = [(root, "", 1)]
    while stack:
        directory, prefix, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                listing = sorted(entries, key=lambda e: e.name)
        except OSError:
            continue
        for entry in listing:
            budget[0] -= 1
            if budget[0] < 0:
                return True
            try:
                if entry.is_symlink() or _is_link(Path(entry.path)):
                    continue
                relative = f"{prefix}{entry.name}"
                if entry.is_dir(follow_symlinks=False):
                    if depth < MAX_DEPTH:
                        stack.append((Path(entry.path), relative + "/", depth + 1))
                elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in MODEL_SUFFIXES:
                    if relative not in names:
                        if len(names) >= max_entries:
                            return True
                        names.add(relative)
            except OSError:
                continue
    return False


def discover_model_files(
    roots: Mapping[str, Iterable[Path]],
    *,
    max_entries: int = MAX_ENTRIES,
    max_scan: int = MAX_SCAN,
) -> dict[str, dict[str, Any]]:
    """Model file NAMES (relative, ``/``-separated) per folder kind."""
    found: dict[str, dict[str, Any]] = {}
    for kind in FOLDER_KINDS:
        names: set[str] = set()
        budget = [max_scan]
        truncated = False
        for root in roots.get(kind, ()):
            if _scan_root(Path(root), names, max_entries, budget):
                truncated = True
                break
        found[kind] = {"names": sorted(names), "truncated": truncated}
    return found


# --- health -----------------------------------------------------------------

def _present(found: Mapping[str, Any], folder: str, name: str) -> bool:
    return name in set(found.get(folder, {}).get("names", ()))


def variant_files(variant: Mapping[str, Any], found: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        role: [{"name": name, "present": _present(found, spec["folder"], name)} for name in spec["names"]]
        for role, spec in variant["files"].items()
    }


def variant_health(
    variant: Mapping[str, Any],
    found: Mapping[str, Any],
    classes: frozenset[str] | set[str] | None,
    checked_at: str,
) -> dict[str, Any]:
    missing_files = []
    for spec in variant["files"].values():
        if spec["required"] and not any(_present(found, spec["folder"], n) for n in spec["names"]):
            missing_files.append(" | ".join(spec["names"]))
    missing_classes: list[str] | None = None
    if classes is not None:
        missing_classes = []
        for entry in variant["required_classes"]:
            options = entry if type(entry) is list else [entry]
            if not any(name in classes for name in options):
                missing_classes.append(" | ".join(options))
    if missing_files or missing_classes:
        state = "missing"
    elif missing_classes is None:
        state = "unchecked"
    else:
        state = "ready"
    return {
        "state": state,
        "selectable": state != "missing",
        "missing_files": missing_files,
        "missing_classes": missing_classes,
        "checked_at": checked_at,
    }


def required_classes(catalog: Mapping[str, Any]) -> list[str]:
    """Every node class any variant or stage names (flat, unique, sorted)."""
    names: set[str] = set()
    for family in catalog["families"]:
        for variant in family["variants"]:
            for entry in variant["required_classes"]:
                names.update(entry if type(entry) is list else [entry])
            for stage in variant["stages"]:
                names.update(stage["classes"])
    return sorted(names)


def engine_file_names(infos: Iterable[Any]) -> frozenset[str]:
    """Model file names the engine's own loader nodes list (``object_info`` input options; w39-p2-tester item 4).

    A ComfyUI on another port (the Windows desktop app) keeps its models in its own folder; its loader lists are the
    truth for what it can load. Only names with a model suffix count; the folder kind is not known from a list."""
    names: set[str] = set()
    for info in infos:
        if not isinstance(info, dict):
            continue
        for node in info.values():
            inputs = node.get("input") if isinstance(node, dict) else None
            for group in (inputs or {}).values() if isinstance(inputs, dict) else ():
                for spec in (group or {}).values() if isinstance(group, dict) else ():
                    options = spec[0] if isinstance(spec, (list, tuple)) and spec else None
                    for option in options if isinstance(options, list) else ():
                        if isinstance(option, str) and len(option) < 400 and Path(option).suffix.lower() in MODEL_SUFFIXES:
                            names.add(option.replace("\\", "/"))
    return frozenset(names)


async def probe_engine(client: Any, names: Iterable[str], *, timeout: float = 20.0, concurrency: int = 8) -> tuple[frozenset[str] | None, frozenset[str]]:
    """``(classes, file names)`` the engine registers / lists; ``(None, empty)`` when it is unreachable. Never queues work."""
    wanted = sorted(set(names))
    gate = asyncio.Semaphore(concurrency)
    infos: list[Any] = []

    async def one(name: str) -> str | None:
        async with gate:
            info = await client.object_info(name)
        infos.append(info)
        return name if isinstance(info, dict) and name in info else None

    async def run() -> frozenset[str]:
        await client.system_stats()
        results = await asyncio.gather(*(one(name) for name in wanted))
        return frozenset(name for name in results if name)

    try:
        classes = await asyncio.wait_for(run(), timeout)
    except Exception:  # noqa: BLE001 - unreachable means unchecked, not missing
        return None, frozenset()
    return classes, engine_file_names(infos)


def with_engine_files(found: Mapping[str, Any], engine_files: Iterable[str]) -> dict[str, Any]:
    """Folder scan plus the engine's loader lists (a name the engine lists counts as present in every folder kind)."""
    extra = set(engine_files or ())
    if not extra:
        return dict(found)
    return {kind: {**entry, "names": sorted(set(entry.get("names", ())) | extra)} for kind, entry in found.items()}


async def probe_classes(client: Any, names: Iterable[str], *, timeout: float = 20.0, concurrency: int = 8) -> frozenset[str] | None:
    """Which of ``names`` ComfyUI registers, via GET /object_info/{class}.

    Returns ``None`` (unchecked) when ComfyUI is unreachable or any probe fails;
    never queues work.
    """
    wanted = sorted(set(names))
    gate = asyncio.Semaphore(concurrency)

    async def one(name: str) -> str | None:
        async with gate:
            info = await client.object_info(name)
        return name if isinstance(info, dict) and name in info else None

    async def run() -> frozenset[str]:
        await client.system_stats()
        results = await asyncio.gather(*(one(name) for name in wanted))
        return frozenset(name for name in results if name)

    try:
        return await asyncio.wait_for(run(), timeout)
    except Exception:  # noqa: BLE001 - unreachable means unchecked, not missing
        return None


# --- evidence -----------------------------------------------------------------

_EVIDENCE_KEYS = frozenset({"target_id", "state", "date", "seconds", "comfyui", "output", "operations", "note", "error"})
_EVIDENCE_TARGET = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")  # the workflow routes' target pattern
_DATE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")


def _parse_evidence_entry(value: Any, where: str) -> dict[str, Any]:
    if type(value) is not dict or not set(value) <= _EVIDENCE_KEYS:
        _fail(where)
    entry = value
    if "output" in entry:
        out = entry["output"]
        if type(out) is not dict or set(out) != {"width", "height", "sha256"} or not re.fullmatch(r"[0-9a-f]{64}", str(out["sha256"])):
            _fail(where + ".output")
        _int(out["width"], where + ".output.width", 16, 8192)
        _int(out["height"], where + ".output.height", 16, 8192)
    if entry.get("state") not in EVIDENCE_STATES:
        _fail(where + ".state")
    if not isinstance(entry.get("date"), str) or not _DATE.match(entry["date"]):
        _fail(where + ".date")
    if "target_id" in entry:
        _ident(entry["target_id"], where + ".target_id", _EVIDENCE_TARGET)
    if "seconds" in entry:
        _finite(entry["seconds"], where + ".seconds")
    for key in ("note", "error", "comfyui"):
        if key in entry:
            _text(entry[key], f"{where}.{key}")
    if entry["state"] == "failed" and not entry.get("error"):
        _fail(where + ".error")
    if "operations" in entry:
        ops = entry["operations"]
        if type(ops) is not dict or any(op not in OPERATIONS_EDIT or state not in EVIDENCE_STATES for op, state in ops.items()):
            _fail(where + ".operations")
    return entry


# Edit operations of /image/edit (image_edit_contract.OPERATIONS) evidence may name.
OPERATIONS_EDIT = frozenset({"fill", "replace", "remove", "outpaint", "restyle", "relight", "control", "upscale", "move",
                             "replace_character", "remove_background", "box"})


def load_evidence(path: Path | None = None) -> dict[str, Any]:
    """The render evidence file, strictly validated.  Unreadable means no evidence (nothing proven)."""
    try:
        data = Path(path or EVIDENCE_PATH).read_bytes()
        if len(data) > MAX_EVIDENCE_BYTES:
            raise CatalogError("model evidence too large")
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
        if type(value) is not dict or value.get("schema") != EVIDENCE_SCHEMA:
            raise CatalogError("model evidence schema")
        variants = {key: _parse_evidence_entry(entry, "evidence." + key) for key, entry in (value.get("variants") or {}).items()}
        edits = {key: _parse_evidence_entry(entry, "evidence." + key) for key, entry in (value.get("edit_targets") or {}).items()}
    except (OSError, UnicodeDecodeError, ValueError):
        return {"schema": EVIDENCE_SCHEMA, "variants": {}, "edit_targets": {}}
    return {"schema": EVIDENCE_SCHEMA, "variants": variants, "edit_targets": edits}


# w39-p2-open (the artist, 11 Nov: "es gibt keinen Grund ihn davor zu schuetzen"): a wired model without a render
# record is offered; this fact is only its tooltip (and the agent's reason), never a refusal.
UNRENDERED = "No render through the Suite on record"


def variant_evidence(evidence: Mapping[str, Any], family_id: str, variant_id: str) -> dict[str, Any]:
    """Public evidence of one variant: rendered / failed / unproven, with a plain reason."""
    entry = (evidence.get("variants") or {}).get(f"{family_id}/{variant_id}")
    if not entry:
        return {"state": "unproven", "reason": UNRENDERED}
    out = {"state": entry["state"], "date": entry["date"]}
    if "seconds" in entry:
        out["seconds"] = entry["seconds"]
    if entry["state"] == "rendered":
        out["reason"] = f"Rendered {entry['date']}" + (f" in {entry['seconds']:g} s" if "seconds" in entry else "")
    else:
        out["reason"] = "Fails in the image engine: " + entry["error"]
    return out


def edit_target_evidence(evidence: Mapping[str, Any], target_id: str, operation: str | None = None) -> dict[str, Any]:
    """Evidence of one /image/edit target (and one operation when given)."""
    entry = (evidence.get("edit_targets") or {}).get(target_id)
    if not entry:
        return {"state": "unproven", "reason": UNRENDERED}
    ops = entry.get("operations") or {}
    if operation is not None and ops.get(operation) == "failed":
        return {"state": "failed", "date": entry["date"], "reason": "Fails in the image engine: the grey area comes back unchanged"}
    if entry["state"] == "failed":
        return {"state": "failed", "date": entry["date"], "reason": "Fails in the image engine: " + entry["error"]}
    return {"state": "rendered", "date": entry["date"], "reason": f"Rendered {entry['date']}"}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# Kinds a caller sees when it names none: the Image and Shoot pickers predate voice
# (lane fs-voice) and list every family they get, so voice families are opt-in.
DEFAULT_KINDS = ("image", "video")


def public_catalog(
    catalog: Mapping[str, Any],
    found: Mapping[str, Any],
    classes: frozenset[str] | set[str] | None,
    checked_at: str,
    evidence: Mapping[str, Any] | None = None,
    kinds: Iterable[str] = DEFAULT_KINDS,
    subscription: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Path-free projection: file NAMES with presence, never folders/sources.

    Every wired variant with its files present is offered (``runnable``); a variant
    without a render record says so in its reason (the tooltip), never by being
    withheld (w39-p2-open).  Missing files or a render that failed for real keep no
    ``suite_targets`` and say ``availability="unavailable"`` with the plain reason.
    """
    if evidence is None:
        evidence = load_evidence()
    wanted = set(kinds)
    # w6-gptimage: a subscription family is offered iff its provider is connected (never by render evidence).
    connected = bool(subscription and subscription.get("connected"))
    families = []
    for family in catalog["families"]:
        if family["kind"] not in wanted:
            continue
        variants = []
        for variant in family["variants"]:
            workflow = copy.deepcopy(variant.get("workflow", {}).get("descriptor"))
            targets = list(variant["suite_targets"])
            health = variant_health(variant, found, classes, checked_at)
            proof = variant_evidence(evidence, family["id"], variant["id"])
            if workflow and family["id"] in IMAGE_WORKFLOW_ADAPTERS:
                targets = [workflow["target_id"]]
            offered = bool(targets) and family["kind"] == "image"
            if offered and family.get("provider_group"):
                availability, reason = ("runnable", "Connected") if connected else ("unavailable", "Not connected")
            elif offered and health["missing_files"]:
                availability, reason = "unavailable", "Missing model files: " + ", ".join(health["missing_files"])
            elif offered and proof["state"] == "failed":
                availability, reason = "unavailable", proof["reason"]
            elif offered:
                availability, reason = "runnable", proof["reason"]
            else:
                availability, reason = None, None
            if availability == "unavailable":
                targets = []
            if workflow and family["id"] in IMAGE_WORKFLOW_ADAPTERS:
                workflow.update(availability=availability, reason=reason)
                # w1-imgout: batch bounds and LoRA support of this target (offered / untested / unsupported + reason).
                workflow["run"] = _workflow_run().run_descriptor(workflow["target_id"], family["id"])
            variants.append({
                "id": variant["id"],
                "label": variant["label"],
                "operations": list(variant["operations"]),
                "prompt_grammar": variant["prompt_grammar"],
                "inputs": copy.deepcopy(variant["inputs"]),
                "presets": [{"id": p["id"], "label": p["label"], "values": dict(p["values"])} for p in variant["presets"]],
                "files": variant_files(variant, found),
                "health": health,
                "evidence": {**proof, **({"availability": availability, "reason": reason} if availability else {})},
                "lora_max": variant["lora_max"],
                "resolution": copy.deepcopy(variant["resolution"]),
                "stages": copy.deepcopy(variant["stages"]),
                "notes": list(variant["notes"]),
                "suite_targets": targets,
                **({"workflow": workflow} if workflow else {}),
            })
        families.append({
            "id": family["id"],
            "label": family["label"],
            "kind": family["kind"],
            "prompt_grammar": family["prompt_grammar"],
            "variants": variants,
            **({"provider_group": {**family["provider_group"], "connected": connected}} if family.get("provider_group") else {}),
        })
    return {"schema": SCHEMA, "families": families}


def build_public_catalog(
    classes: frozenset[str] | None,
    *,
    comfy_root: Path | None = None,
    catalog_path: Path | None = None,
    kinds: Iterable[str] = DEFAULT_KINDS,
    engine_files: Iterable[str] = (),
) -> dict[str, Any]:
    """Blocking: load catalog, scan folders, project. Run off the event loop."""
    catalog = load_catalog(catalog_path)
    found = discover_model_files(model_roots(comfy_root))  # None: FILM_SUITE_COMFY_ROOT, read now
    found = with_engine_files(found, engine_files)
    return public_catalog(catalog, found, classes, _now(), kinds=kinds, subscription=_subscription_state())


def _subscription_state() -> dict[str, Any]:
    """Whether Hermes' openai-codex image provider is connected (w6-gptimage); never raises.

    Asking Hermes imports its image providers, and gateway/platforms/base.py prepends the Hermes root
    to sys.path on import. In the host and in tests that reorders every later import (24.10.: spawned
    children found Hermes' own ``tests`` package). The catalog is a read: hand sys.path back unchanged."""
    import sys
    saved = list(sys.path)
    try:
        return _subscription_state_unguarded()
    finally:
        sys.path[:] = saved


def _subscription_state_unguarded() -> dict[str, Any]:
    try:
        if __package__:
            from importlib import import_module
            module = import_module(__package__ + ".paid.subscription_image_provider")
        else:
            import importlib.util
            spec = importlib.util.spec_from_file_location("film_suite_subscription_image_provider", Path(__file__).resolve().with_name("paid") / "subscription_image_provider.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module.connection_state()
    except Exception:  # noqa: BLE001 - no provider: not connected
        return {"connected": False, "reason": "Not connected"}


__all__ = [
    "CATALOG_PATH", "CatalogError", "comfy_root", "DEFAULT_KINDS", "FAMILY_KINDS", "catalog_view", "FOLDER_KINDS", "MAX_DEPTH", "MAX_ENTRIES",
    "MAX_YAML_BYTES", "OPERATIONS", "SCHEMA", "build_public_catalog", "discover_model_files", "find_variant",
    "krea_profile", "load_catalog", "model_roots", "parse_catalog", "probe_classes", "probe_engine", "public_catalog",
    "engine_file_names", "with_engine_files",
    "required_classes", "variant_files", "variant_health", "EVIDENCE_PATH", "load_evidence", "variant_evidence",
    "edit_target_evidence",
]
