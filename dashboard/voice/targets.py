"""Voice targets: one pinned ComfyUI graph + manifest per (engine, operation).

A voice target is data only:

* ``workflows/<target>/workflow-api.json`` is the pinned graph; ``manifest.json``
  (schema ``studio.voice-workflow/1``) names its sha256, the JSON pointers the
  Suite may write (text, words, reference clip, description, declared controls)
  and ``fixed`` values the graph must hold.  Nothing else of the graph changes.
* ``keep_model_loaded`` is pinned False in every graph that has the input
  (fs-tf5 RESULT item 8: True keeps about 6 GB of RAM per take); a manifest that
  does not assert it, or a graph that holds anything else, is refused.
* Health fails closed: unreachable ComfyUI, a missing node class, a model file
  ComfyUI does not list, or a declared input the live node no longer has, each
  give one readable reason and ``ready: False``.

Standalone module (stdlib only, no package-relative imports).
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "studio.voice-workflow/1"
_HERE = Path(__file__).resolve()
# Repository layout first (workflows/ at the root), else the installed plugin's copy (dashboard/workflows),
# the same lookup as generation/catalog_create_workflow_adapter.py.
WORKFLOW_ROOT = _HERE.parents[4] / "workflows" if (_HERE.parents[4] / "workflows" / "drama3-clone-v1").is_dir() else _HERE.parents[1] / "workflows"
TARGET_IDS = ("omnivoice-clone-v1", "omnivoice-design-v1", "drama3-clone-v1")
VOICE_FROM = frozenset({"clip", "description"})
SECTIONS = ("delivery", "pauses", "performance")
CONTROL_KINDS = frozenset({"integer", "number", "boolean", "choice"})
_MANIFEST_KEYS = frozenset({
    "schema", "id", "version", "kind", "provider_id", "api_file", "api_sha256", "engine", "engine_label", "operation",
    "label", "voice_from", "cue_dialect", "direction", "sample_rate", "text", "words", "reference", "description",
    "parameters", "controls", "fixed", "required_classes", "models", "output", "source", "policy",
})
_POLICY = {"graph_mutation": "declared_pointers_only", "custom_node_install": "never", "model_download": "never_automatic",
           "submission_requires_explicit_user_intent": True}
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PLACEHOLDER = re.compile(r"__[A-Z]+__")

# Neutral cues ({cue:sigh}, {pause:0.5}) -> engine syntax.  A cue the engine lacks is
# dropped with a warning on the take, never invented (contract §4).
CUE_DIALECTS: dict[str, dict[str, str]] = {
    "omnivoice_bracket": {"laugh": "[laughter]", "sigh": "[sigh]"},
    "drama3": {"laugh": "[laughs]", "sigh": "[sighs]", "whisper": "[whispers]", "pause": "[pause]"},
}


class VoiceTargetError(ValueError):
    """A voice manifest, graph or request is invalid (public message)."""


def _fail(message: str) -> None:
    raise VoiceTargetError(message)


def _pointer_parts(pointer: str) -> list[str]:
    if type(pointer) is not str or not pointer.startswith("/") or len(pointer) > 200:
        _fail("invalid pointer")
    return [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]


def read_pointer(graph: Mapping[str, Any], pointer: str) -> Any:
    node: Any = graph
    for part in _pointer_parts(pointer):
        if not isinstance(node, Mapping) or part not in node:
            _fail(f"pointer {pointer} is not in the graph")
        node = node[part]
    return node


def write_pointer(graph: dict[str, Any], pointer: str, value: Any) -> None:
    parts = _pointer_parts(pointer)
    node: Any = graph
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            _fail(f"pointer {pointer} is not in the graph")
        node = node[part]
    if not isinstance(node, dict) or parts[-1] not in node:
        _fail(f"pointer {pointer} is not in the graph")
    if isinstance(node[parts[-1]], list):
        _fail(f"pointer {pointer} is a graph link")
    node[parts[-1]] = value


def _check_control(control: Any, ids: set[str]) -> None:
    keys = {"id", "label", "section", "kind", "default"}
    if type(control) is not dict or not keys <= set(control) or set(control) - keys - {"min", "max", "step", "options"}:
        _fail("invalid control")
    if control["kind"] not in CONTROL_KINDS or control["section"] not in SECTIONS or control["id"] in ids:
        _fail("invalid control")
    ids.add(control["id"])
    if control["kind"] == "choice":  # w41-audio F17: a closed list of words ("" = not set), written into a text input
        options = control.get("options")
        if set(control) != keys | {"options"} or type(options) is not list or not all(type(o) is str for o in options) \
                or control["default"] not in options or len(set(options)) != len(options):
            _fail("invalid control")
        return
    if "options" in control:
        _fail("invalid control")
    if control["kind"] == "boolean":
        if type(control["default"]) is not bool or set(control) != keys:
            _fail("invalid control")
        return
    integer = control["kind"] == "integer"
    for key in ("default", "min", "max", "step"):
        value = control.get(key)
        if type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value):
            _fail("invalid control")
    if not control["min"] <= control["default"] <= control["max"] or control["step"] <= 0:
        _fail("invalid control")


def validate(manifest: Mapping[str, Any], graph: Mapping[str, Any], api_bytes: bytes) -> None:
    """Every rule the pinned pair must hold; raises VoiceTargetError."""
    if type(manifest) is not dict or set(manifest) != _MANIFEST_KEYS:
        _fail("manifest keys are not the voice schema")
    if manifest["schema"] != SCHEMA or manifest["kind"] != "voice" or manifest["provider_id"] != "comfyui-local":
        _fail("manifest is not a local voice workflow")
    if not _ID.fullmatch(str(manifest["id"])) or manifest["policy"] != _POLICY:
        _fail("manifest id or policy is invalid")
    if hashlib.sha256(api_bytes).hexdigest() != manifest["api_sha256"]:
        _fail("pinned graph does not match its sha256")
    if manifest["voice_from"] not in VOICE_FROM or manifest["cue_dialect"] not in CUE_DIALECTS:
        _fail("voice source or cue dialect is unknown")
    if (manifest["voice_from"] == "clip") != (manifest["reference"] is not None) or (manifest["voice_from"] == "description") != (manifest["description"] is not None):
        _fail("voice source and its pointers disagree")
    for key in ("text", "reference", "description", "words"):
        entry = manifest[key]
        if entry is not None:
            if type(read_pointer(graph, entry["pointer"])) is not str:
                _fail(f"{key} pointer must name a text input")
    ids: set[str] = set()
    for control in manifest["controls"]:
        _check_control(control, ids)
    if set(manifest["parameters"]) != ids:
        _fail("every control needs exactly one pointer")
    for control in manifest["controls"]:
        value = read_pointer(graph, manifest["parameters"][control["id"]]["pointer"])
        if control["kind"] == "choice":
            if type(value) is not str:
                _fail(f"control {control['id']} points at the wrong input type")
            continue
        if control["kind"] == "boolean" and type(value) is not bool or control["kind"] != "boolean" and (type(value) is bool or type(value) not in (int, float)):
            _fail(f"control {control['id']} points at the wrong input type")
    shared = {}
    for control in manifest["controls"]:
        shared.setdefault(manifest["parameters"][control["id"]]["pointer"], []).append(control["kind"])
    if any(len(kinds) > 1 and set(kinds) != {"choice"} for kinds in shared.values()):
        _fail("only choice controls may share one pointer")
    for pointer, expected in manifest["fixed"].items():
        if read_pointer(graph, pointer) != expected or type(read_pointer(graph, pointer)) is not type(expected):
            _fail(f"graph does not hold its fixed value at {pointer}")
    # keep_model_loaded: every node that has the input must hold False and the manifest must assert it.
    for node_id, node in graph.items():
        inputs = node.get("inputs") if isinstance(node, Mapping) else None
        if isinstance(inputs, Mapping) and "keep_model_loaded" in inputs:
            pointer = f"/{node_id}/inputs/keep_model_loaded"
            if inputs["keep_model_loaded"] is not False or manifest["fixed"].get(pointer) is not False:
                _fail("keep_model_loaded must be pinned false")
    classes = {node.get("class_type") for node in graph.values() if isinstance(node, Mapping)}
    if classes != set(manifest["required_classes"]):
        _fail("required classes do not match the graph")
    output = manifest["output"]
    if graph.get(output["node_id"], {}).get("class_type") != "SaveAudio" or output["extension"] != ".flac":
        _fail("output must be the graph's SaveAudio (flac)")


def load_target(target_id: str, root: Path | None = None) -> dict[str, Any]:
    """The verified manifest and graph of one target (fresh copies)."""
    if target_id not in TARGET_IDS:
        _fail("unknown voice target")
    folder = (root or WORKFLOW_ROOT) / target_id
    try:
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        api_bytes = (folder / str(manifest.get("api_file", ""))).read_bytes()
        graph = json.loads(api_bytes.decode("utf-8"))
    except (OSError, ValueError, AttributeError) as exc:
        raise VoiceTargetError("voice workflow is unreadable") from exc
    validate(manifest, graph, api_bytes)
    if manifest["id"] != target_id:
        _fail("manifest id does not match its folder")
    return {"manifest": manifest, "graph": graph}


def public_target(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """What the page and the agent see: no pointers, files or graph."""
    return {
        "id": manifest["id"], "label": manifest["label"], "engine": manifest["engine"], "engine_label": manifest["engine_label"],
        "operation": manifest["operation"], "voice_from": manifest["voice_from"], "direction": manifest["direction"],
        "words": None if manifest["words"] is None else {"required": bool(manifest["words"].get("required"))},
        "cues": sorted(CUE_DIALECTS[manifest["cue_dialect"]]), "sample_rate": manifest["sample_rate"],
        "controls": copy.deepcopy(manifest["controls"]), "media_label": manifest["engine_label"],
    }


_CUE = re.compile(r"\{cue:([a-z]+)\}")


def render_text(text: str, direction: str, manifest: Mapping[str, Any]) -> tuple[str, list[str]]:
    """The line as the engine reads it, and warnings for cues it lacks.

    ``direction`` ([softer, to himself]) is inlined for engines that read free
    directions (Drama 3) and dropped with a warning for the others.
    """
    table = CUE_DIALECTS[manifest["cue_dialect"]]
    warnings: list[str] = []

    def cue(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in table:
            return table[name]
        warnings.append(f"{manifest['engine_label']} has no {name} cue; it was left out")
        return ""

    spoken = re.sub(r"\s{2,}", " ", _CUE.sub(cue, text)).strip()
    direction = direction.strip()
    if direction:
        if manifest["direction"] == "inline":
            spoken = f"[{direction.strip('[]')}] {spoken}"
        else:
            warnings.append(f"{manifest['engine_label']} does not read directions; [{direction.strip('[]')}] was left out")
    if not spoken:
        _fail("The line is empty")
    return spoken, warnings


def compile_graph(target: Mapping[str, Any], *, text: str, values: Mapping[str, Any], reference: str | None = None,
                  words: str | None = None, description: str | None = None) -> dict[str, Any]:
    """The pinned graph with only declared pointers written; fixed values re-checked."""
    manifest = target["manifest"]
    graph = copy.deepcopy(target["graph"])
    write_pointer(graph, manifest["text"]["pointer"], text)
    if manifest["voice_from"] == "clip":
        if type(reference) is not str or not reference.endswith(" [temp]"):
            _fail("This voice needs its clip")
        write_pointer(graph, manifest["reference"]["pointer"], reference)
        if manifest["words"] is not None:
            words = (words or "").strip()
            if manifest["words"].get("required") and not words:
                _fail(f"{manifest['engine_label']} needs the words of the voice clip")
            write_pointer(graph, manifest["words"]["pointer"], words)
    else:
        if not (description or "").strip():
            _fail("This voice needs a description")
        write_pointer(graph, manifest["description"]["pointer"], description.strip())
    unknown = set(values) - set(manifest["parameters"])
    if unknown:
        _fail(f"{manifest['engine_label']} has no setting {sorted(unknown)[0]}")
    words_at: dict[str, list[str]] = {}
    for control in manifest["controls"]:
        value = values.get(control["id"], control["default"])
        pointer = manifest["parameters"][control["id"]]["pointer"]
        if control["kind"] == "choice":
            if type(value) is not str or value not in control["options"]:
                _fail(f"{control['label']} has no value {value!r}")
            words_at.setdefault(pointer, [])
            if value:
                words_at[pointer].append(value)
            continue
        if control["kind"] == "boolean":
            if type(value) is not bool:
                _fail(f"{control['label']} must be on or off")
        else:
            if type(value) is bool or type(value) not in (int, float) or not math.isfinite(value):
                _fail(f"{control['label']} must be a number")
            if control["kind"] == "integer" and value != int(value):
                _fail(f"{control['label']} must be a whole number")
            value = int(value) if control["kind"] == "integer" else float(value)
            if not control["min"] <= value <= control["max"]:
                _fail(f"{control['label']} must be {control['min']} to {control['max']}")
        write_pointer(graph, pointer, value)
    for pointer, words in words_at.items():
        # w41-audio F17: OmniVoice instruct = the chosen traits, comma separated (full-width with a Chinese dialect, the
        # model's own separator); an accent is for English speech and a dialect for Chinese: the model refuses both.
        dialect = any(w.endswith("\u8bdd") for w in words)
        if dialect and any(w.endswith(" accent") for w in words):
            _fail("Accent and Dialect cannot be combined: an accent is for English speech, a dialect for Chinese")
        write_pointer(graph, pointer, ("\uff0c" if dialect else ", ").join(words))
    for pointer, expected in manifest["fixed"].items():
        if read_pointer(graph, pointer) != expected:
            _fail("a fixed value changed")
    if _PLACEHOLDER.search(json.dumps(graph)):
        _fail("the graph still holds a placeholder")
    return graph


def health(manifest: Mapping[str, Any], object_info: Mapping[str, Any] | None, model_names: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fail-closed readiness from live /object_info and /models/<folder> answers.

    ``object_info``: class -> schema for the required classes (None = ComfyUI
    unreachable).  ``model_names``: folder -> list of names ComfyUI lists.
    """
    reasons: list[str] = []
    if object_info is None:
        return {"ready": False, "reasons": ["The local engine is not running"]}
    for name in manifest["required_classes"]:
        if name not in object_info:
            reasons.append(f"Node {name} is not installed")
    graph = load_target(manifest["id"])["graph"]
    for node in graph.values():
        info = object_info.get(node["class_type"])
        if not isinstance(info, Mapping):
            continue
        declared = {**(info.get("input", {}).get("required") or {}), **(info.get("input", {}).get("optional") or {})}
        for key in node["inputs"]:
            if key not in declared and not (key.startswith("voices.") and "voices" in declared):
                reasons.append(f"Node {node['class_type']} no longer has the input {key}")
    for model in manifest["models"]:
        names = (model_names or {}).get(model["folder"])
        names = None if names is None else {str(name).replace("\\", "/") for name in names}
        if names is None:
            reasons.append(f"Model folder {model['folder']} could not be read")
        elif model["name"] not in names:
            reasons.append(f"Model file {model['name']} is missing")
    return {"ready": not reasons, "reasons": reasons}


__all__ = ["CUE_DIALECTS", "SCHEMA", "TARGET_IDS", "VoiceTargetError", "compile_graph", "health", "load_target",
           "public_target", "read_pointer", "render_text", "validate", "write_pointer"]
