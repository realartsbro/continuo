"""Runtime policy for the pinned local H3 targets: schema fingerprint, not a version list.

A ComfyUI / frontend / templates update must not lock a target whose nodes did
not change.  The manifest's version lists say what WE tested ("tested on"), not
what is allowed.  Health is decided by what the pinned graph really needs:

* ``derive_fingerprint`` records, at pin time, exactly the live ``/object_info``
  facts the graph depends on: per node class, every input the graph sets (and
  every declared mutable pointer) with its type, the combo values the graph or
  the manifest uses, the numeric range the manifest allows, autogrow members and
  dynamic-combo nesting.  The result is stored in the manifest as
  ``schema_fingerprint`` (``the development schema fingerprint tool``) and a test
  re-derives it from the captured fixture so it cannot drift silently.
* ``schema_problem`` compares a live answer with the fingerprint and names the
  first node class + input that drifted.
* ``runtime_verdict`` says ``tested`` (listed in the manifest or recorded on
  this install), ``untested`` (schema_verified policy: runnable, the first take
  checks it) or ``blocked`` (exact policy of a manifest not migrated, unknown
  policy).
* ``read_tested`` / ``record_tested`` keep the per-install record of runtimes on
  which a published take passed the output checks (``verify_take_output``).

Pure apart from the state file and ffprobe/ffmpeg in ``verify_take_output``; no
package-relative imports, loaded by path (the generation package's module tree
is pinned by tests/backend/test_route_loader.py).
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env

SCHEMA_VERIFIED = "schema_verified"
EXACT_TESTED = "exact_tested_version_required"
POLICIES = frozenset({SCHEMA_VERIFIED, EXACT_TESTED})
RUNTIME_KEYS = ("comfyui_version", "required_frontend_version", "installed_templates_version", "required_templates_version")
_LIST_FOR_KEY = {
    "comfyui_version": "comfyui_versions",
    "required_frontend_version": "frontend_versions",
    "installed_templates_version": "workflow_templates_versions",
    "required_templates_version": "workflow_templates_versions",
}
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,39}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+-]{0,79}$")
AUTOGROW = "COMFY_AUTOGROW_V3"
DYNAMIC = "COMFY_DYNAMICCOMBO_V3"
TESTED_SCHEMA = "studio.h3-runtime-tested/1"
TESTED_MAX = 256
_TESTED_LOCK = threading.Lock()


# --------------------------------------------------------------------------- wire shapes


def _inputs(info: Any, class_type: str) -> dict[str, Any] | None:
    """Merged required+optional inputs of one class, or None when the class is absent."""
    node = info.get(class_type) if isinstance(info, Mapping) else None
    if not isinstance(node, Mapping):
        return None
    sections = node.get("input")
    merged: dict[str, Any] = {}
    if isinstance(sections, Mapping):
        for part in ("required", "optional"):
            section = sections.get(part)
            if isinstance(section, Mapping):
                merged.update(section)
    return merged


def _kind(spec: Any) -> str | None:
    """Normalized input type; None for an untagged legacy shape (bare bounds dict)."""
    if not isinstance(spec, list) or not spec:
        return None
    head = spec[0]
    if isinstance(head, list):
        return "COMBO"  # legacy combo: [[options], {...}]
    if not isinstance(head, str):
        return None
    if len(spec) > 1 and isinstance(spec[1], list):
        return "COMBO"  # legacy combo: [default, [options]]
    return head


def _options(spec: Any) -> list[Any] | None:
    if not isinstance(spec, list) or not spec:
        return None
    if isinstance(spec[0], list):
        return list(spec[0])
    if len(spec) > 1 and isinstance(spec[1], list):
        return list(spec[1])
    if len(spec) > 1 and isinstance(spec[1], Mapping) and isinstance(spec[1].get("options"), list):
        values = spec[1]["options"]
        if spec[0] == DYNAMIC:
            return [item.get("key") for item in values if isinstance(item, Mapping)]
        return list(values)
    return None


def _bounds(spec: Any) -> Mapping[str, Any]:
    if isinstance(spec, Mapping):
        return spec
    if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], Mapping):
        return spec[1]
    return {}


def _dynamic_inputs(spec: Any, key: Any) -> dict[str, Any] | None:
    """Inputs nested under one option of a dynamic combo."""
    if _kind(spec) != DYNAMIC or not isinstance(spec[1], Mapping):
        return None
    for option in spec[1].get("options") or []:
        if isinstance(option, Mapping) and option.get("key") == key:
            sections = option.get("inputs")
            merged: dict[str, Any] = {}
            if isinstance(sections, Mapping):
                for part in ("required", "optional"):
                    if isinstance(sections.get(part), Mapping):
                        merged.update(sections[part])
            return merged
    return None


def _template(spec: Any) -> Mapping[str, Any] | None:
    if _kind(spec) != AUTOGROW or not isinstance(spec[1], Mapping):
        return None
    template = spec[1].get("template")
    return template if isinstance(template, Mapping) else None


def _member_problem(template: Mapping[str, Any], member: str) -> bool:
    names = template.get("names")
    if isinstance(names, list):
        return member not in names
    prefix, maximum = template.get("prefix"), template.get("max")
    if not isinstance(prefix, str) or not member.startswith(prefix) or type(maximum) is not int:
        return True
    rest = member[len(prefix):]
    return not rest.isdigit() or int(rest) >= maximum


# --------------------------------------------------------------------------- derivation (pin time)


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int


def _pointer(pointer: str) -> tuple[str, str] | None:
    parts = pointer.strip("/").split("/")
    return (parts[0], parts[2]) if len(parts) == 3 and parts[1] == "inputs" else None


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _class_needs(graph: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, dict[str, dict[str, Any]]]:
    """What the graph and the manifest need from each class input (no live facts)."""
    model_pointers = {_pointer(m["assertion_pointer"]) for m in manifest.get("models", {}).values()}
    mutable: dict[tuple[str, str], dict[str, Any]] = {}
    for pointer in manifest.get("mutable_json_pointers", []):
        mutable[_pointer(pointer)] = {}
    for declaration in list(manifest.get("parameters", {}).values()) + list(manifest.get("sampling", {}).values()):
        key = _pointer(declaration["pointer"])
        entry = mutable.setdefault(key, {})
        if "enum" in declaration:
            entry.setdefault("enum", []).extend(declaration["enum"])
        for bound in ("minimum", "maximum"):
            if bound in declaration:
                entry.setdefault("range", []).append(declaration[bound])
    needs: dict[str, dict[str, dict[str, Any]]] = {}
    for node_id, node in graph.items():
        class_type = node["class_type"]
        per_class = needs.setdefault(class_type, {})
        for name, value in node["inputs"].items():
            key = (node_id, name)
            entry = per_class.setdefault(name, {"values": [], "numbers": [], "members": set(), "set_by": set()})
            entry["set_by"].add((node_id, value if not _is_link(value) and not isinstance(value, (list, dict)) else None))
            declared = mutable.get(key)
            if key in model_pointers:
                entry["model_or_media"] = True  # model files are checked against /models
            elif declared is None and not _is_link(value) and not isinstance(value, (list, dict)):
                entry["values"].append(value)  # a fixed literal ComfyUI must still accept
            if declared:
                entry["values"].extend(declared.get("enum", []))
                entry["numbers"].extend(declared.get("range", []))
    for rule in manifest.get("live_schema", []):
        entry = needs.setdefault(rule["class_type"], {}).setdefault(
            rule["input"], {"values": [], "numbers": [], "members": set(), "set_by": set()})
        if rule["kind"] == "combo":
            entry["rule_options"] = list(rule["includes"])
        elif rule["kind"] == "range":
            entry["numbers"].extend(rule["covers"])
        elif rule["kind"] == "autogrow":
            entry["members"].update(f"{rule['prefix']}{i}" for i in range(rule["max_at_least"]))
    return needs


def derive_fingerprint(graph: Mapping[str, Any], manifest: Mapping[str, Any], object_info: Mapping[str, Any]) -> dict[str, Any]:
    """The compact schema fingerprint of exactly the inputs this graph needs."""
    needs = _class_needs(graph, manifest)
    nodes: dict[str, dict[str, Any]] = {}
    for class_type in sorted(needs):
        inputs = _inputs(object_info, class_type)
        if inputs is None:
            raise ValueError(f"object_info lacks pinned class {class_type}")
        record: dict[str, Any] = {}
        grouped: dict[str, set[str]] = {}
        for name, need in needs[class_type].items():
            parent = name.split(".", 1)[0] if "." in name else None
            if parent in inputs and _kind(inputs[parent]) == DYNAMIC:
                # A DynamicCombo child (``mode.megapixels``): the nested input of the chosen option.
                child, spec = name.split(".", 1)[1], None
                for _node_id, chosen in needs[class_type].get(parent, {}).get("set_by", ()):
                    nested = _dynamic_inputs(inputs[parent], chosen)
                    if nested and child in nested:
                        spec = nested[child]
                        break
                if spec is None:
                    record[name] = {"ignored": True}
                    continue
                entry = {"type": _kind(spec), "within": [parent, chosen], "nested": child}
                numbers = [v for v in need["values"] + need["numbers"] if _number(v)]
                if entry["type"] in ("INT", "FLOAT") and numbers:
                    entry["covers"] = [min(numbers), max(numbers)]
                record[name] = entry
                continue
            if "." in name and name.split(".", 1)[0] in inputs:
                grouped.setdefault(name.split(".", 1)[0], set()).add(name.split(".", 1)[1])
                continue
            if name in inputs and _kind(inputs[name]) == AUTOGROW:
                grouped.setdefault(name, set()).update(need["members"])
                continue
            spec, within = inputs.get(name), None
            if spec is None:
                for parent, parent_spec in inputs.items():
                    for node_id, chosen in needs[class_type].get(parent, {}).get("set_by", ()):
                        nested = _dynamic_inputs(parent_spec, chosen)
                        if nested and name in nested:
                            spec, within = nested[name], [parent, chosen]
                            break
                    if spec is not None:
                        break
            if spec is None:
                record[name] = {"ignored": True}  # ComfyUI drops an input its schema does not know
                continue
            entry: dict[str, Any] = {"type": _kind(spec)}
            if within:
                entry["within"] = within
            if entry["type"] in ("COMBO", DYNAMIC) and not need.get("model_or_media"):
                wanted = need["values"] + need.get("rule_options", [])
                if wanted:
                    entry["options"] = sorted(set(wanted), key=lambda v: (str(type(v)), str(v)))
            elif entry["type"] in ("INT", "FLOAT"):
                numbers = [v for v in need["values"] + need["numbers"] if _number(v)]
                if numbers:
                    entry["covers"] = [min(numbers), max(numbers)]
            record[name] = entry
        for group, members in grouped.items():
            template = _template(inputs[group]) or {}
            slots = template.get("input", {}).get("required", {}) if isinstance(template.get("input"), Mapping) else {}
            record[group] = {"type": AUTOGROW, "members": sorted(members), "slots": sorted(slots)}
        nodes[class_type] = dict(sorted(record.items()))
    return {"nodes": nodes}


def extend_fingerprint(fingerprint: Mapping[str, Any], graph: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Add a versioned projection's extra needs (sampling v2) to the pinned fingerprint."""
    result = json.loads(json.dumps(fingerprint))
    for declaration in manifest.get("sampling", {}).values():
        key = _pointer(declaration["pointer"])
        class_type = graph[key[0]]["class_type"]
        entry = result["nodes"][class_type][key[1]]
        if "enum" in declaration and entry.get("type") in ("COMBO", DYNAMIC):
            entry["options"] = sorted(set(entry.get("options", [])) | set(declaration["enum"]), key=lambda v: (str(type(v)), str(v)))
        elif "enum" in declaration and entry.get("type") in ("INT", "FLOAT"):
            numbers = list(entry.get("covers", [])) + list(declaration["enum"])
            entry["covers"] = [min(numbers), max(numbers)]
        if "minimum" in declaration:
            numbers = list(entry.get("covers", [])) + [declaration["minimum"], declaration["maximum"]]
            entry["covers"] = [min(numbers), max(numbers)]
    return result


def effective_fingerprint(manifest: Mapping[str, Any], graph: Mapping[str, Any]) -> dict[str, Any] | None:
    """The manifest's pinned fingerprint, widened by a versioned projection's ``sampling``."""
    fingerprint = manifest.get("schema_fingerprint")
    if not isinstance(fingerprint, Mapping) or not isinstance(fingerprint.get("nodes"), Mapping):
        return None
    if isinstance(manifest.get("sampling"), Mapping):
        return extend_fingerprint(fingerprint, graph, manifest)
    return json.loads(json.dumps(fingerprint))


# --------------------------------------------------------------------------- live comparison


def schema_problem(fingerprint: Mapping[str, Any], infos: Mapping[str, Any]) -> tuple[str, str | None, str] | None:
    """First drift between the pinned fingerprint and live object_info: (class, input, problem)."""
    nodes = fingerprint.get("nodes") if isinstance(fingerprint, Mapping) else None
    if not isinstance(nodes, Mapping) or not nodes:
        return ("", None, "no_fingerprint")
    for class_type, record in nodes.items():
        inputs = _inputs(infos, class_type)
        if inputs is None:
            return (class_type, None, "missing_node")
        for name, entry in record.items():
            if entry.get("ignored"):
                continue
            if entry.get("within"):
                parent, chosen = entry["within"]
                nested = _dynamic_inputs(inputs.get(parent), chosen)
                spec = nested.get(entry.get("nested", name)) if nested else None
            else:
                spec = inputs.get(name)
            if spec is None:
                return (class_type, name, "missing_input")
            live_kind = _kind(spec)
            if live_kind is not None and entry.get("type") is not None and live_kind != entry["type"]:
                return (class_type, name, "type_changed")
            if entry.get("type") == AUTOGROW:
                template = _template(spec)
                slots = template.get("input", {}).get("required") if isinstance(template, Mapping) and isinstance(template.get("input"), Mapping) else None
                if template is None or not isinstance(slots, Mapping) or not set(entry.get("slots", [])) <= set(slots):
                    return (class_type, name, "group_changed")
                if any(_member_problem(template, member) for member in entry.get("members", [])):
                    return (class_type, name, "group_changed")
            if "options" in entry:
                live = _options(spec) or []
                if any(value not in live for value in entry["options"]):
                    return (class_type, name, "options_removed")
            if "covers" in entry:
                bounds = _bounds(spec)
                low, high = bounds.get("min"), bounds.get("max")
                for bound in (low, high):
                    if bound is not None and not _number(bound):
                        return (class_type, name, "range_narrowed")
                if (low is not None and low > entry["covers"][0]) or (high is not None and high < entry["covers"][1]):
                    return (class_type, name, "range_narrowed")
    return None


def detail(class_type: str, name: str | None) -> str | None:
    """``Class.input`` for the public health answer, or None when it is not a plain identifier."""
    text = class_type if name is None else f"{class_type}.{name}"
    return text if _NAME_RE.match(text) else None


# --------------------------------------------------------------------------- runtime verdict


def runtime_identity(stats: Any) -> dict[str, str] | None:
    """The four runtime version strings from /system_stats, or None when unreadable."""
    system = stats.get("system") if isinstance(stats, Mapping) else None
    if not isinstance(system, Mapping):
        return None
    identity = {key: system.get(key) for key in RUNTIME_KEYS}
    if not all(type(value) is str and _VERSION_RE.match(value) for value in identity.values()):
        return None
    return identity


def _listed(manifest: Mapping[str, Any], identity: Mapping[str, str]) -> bool:
    compatibility = manifest.get("runtime_compatibility", {})
    return all(identity[key] in compatibility.get(_LIST_FOR_KEY[key], []) for key in RUNTIME_KEYS)


def runtime_verdict(manifest: Mapping[str, Any], identity: Mapping[str, str] | None, tested: Iterable[Mapping[str, Any]] = ()) -> str:
    """``tested`` | ``untested`` (runnable, schema decides) | ``blocked``."""
    policy = manifest.get("runtime_compatibility", {}).get("policy")
    if identity is None or policy not in POLICIES:
        return "blocked"
    if _listed(manifest, identity):
        return "tested"
    if policy == EXACT_TESTED:
        return "blocked"
    target_id = manifest.get("id")
    for entry in tested:
        if entry.get("target_id") == target_id and entry.get("versions") == dict(identity):
            return "tested"
    return "untested"


# --------------------------------------------------------------------------- per-install tested record


def read_tested(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Entries of the install record; a missing or unreadable file reads as none (fail to 'untested')."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict) or payload.get("schema") != TESTED_SCHEMA or not isinstance(payload.get("entries"), list):
        return []
    return [entry for entry in payload["entries"] if _valid_entry(entry)]


def _valid_entry(entry: Any) -> bool:
    if not isinstance(entry, dict) or set(entry) != {"target_id", "versions", "studio_job_id", "recorded_at"}:
        return False
    versions = entry["versions"]
    return (type(entry["target_id"]) is str and type(entry["studio_job_id"]) is str and _number(entry["recorded_at"])
            and isinstance(versions, dict) and runtime_identity({"system": versions}) == versions and set(versions) == set(RUNTIME_KEYS))


def record_tested(path: str | os.PathLike[str], *, target_id: str, versions: Mapping[str, str], studio_job_id: str,
                  now: float | None = None) -> dict[str, Any]:
    """Append one tested runtime (idempotent per target + versions); atomic replace, nothing else lost.

    An unreadable existing file is kept beside the new one (``.unreadable-<time>``), never overwritten.
    """
    if runtime_identity({"system": dict(versions)}) != dict(versions) or type(target_id) is not str or type(studio_job_id) is not str:
        raise ValueError("tested runtime entry is invalid")
    target = Path(path)
    with _TESTED_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        raw: Any = None
        if target.exists():
            try:
                raw = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
            if not (isinstance(raw, dict) and raw.get("schema") == TESTED_SCHEMA and isinstance(raw.get("entries"), list)):
                os.replace(target, target.with_name(f"{target.name}.unreadable-{int(time.time() * 1000)}"))
                raw = None
        entries = [entry for entry in (raw or {}).get("entries", []) if _valid_entry(entry)]
        for entry in entries:
            if entry["target_id"] == target_id and entry["versions"] == dict(versions):
                return entry
        entry = {"target_id": target_id, "versions": dict(versions), "studio_job_id": studio_job_id,
                 "recorded_at": float(time.time() if now is None else now)}
        entries = (entries + [entry])[-TESTED_MAX:]
        handle, temporary = tempfile.mkstemp(prefix=".h3runtime-", suffix=".json", dir=target.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump({"schema": TESTED_SCHEMA, "entries": entries}, stream, ensure_ascii=False, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return entry


# --------------------------------------------------------------------------- first-take output check


def expected_frames(manifest: Mapping[str, Any], duration_seconds: int) -> int:
    duration = manifest["capabilities"]["duration"]
    rule = duration["effective_frames"]
    base = max(rule["minimum_frames"], round(duration_seconds * duration["fps"]))
    return base + ((rule["remainder"] - base) % rule["modulus"])


def verify_take_output(path: str | os.PathLike[str], *, manifest: Mapping[str, Any], frames: int,
                       ffmpeg: str | os.PathLike[str]) -> str | None:
    """None when the published take is what the manifest says; otherwise the failed check.

    Video stream with exactly ``frames`` frames at the manifest fps, an audio
    stream when the manifest requires generated audio, and a decodable first
    frame (the poster).  The DownloadDigest was verified by the delivery.
    """
    ffmpeg = Path(ffmpeg)
    ffprobe = ffmpeg.with_name("ffprobe.exe" if ffmpeg.suffix.lower() == ".exe" else "ffprobe")
    try:
        probe = subprocess.run([str(ffprobe), "-v", "error", "-count_frames", "-show_entries",
                                "stream=codec_type,nb_read_frames,avg_frame_rate", "-of", "json", str(path)],
                               capture_output=True, timeout=300, check=False, env=child_env())
        streams = json.loads(probe.stdout.decode("utf-8", "replace")).get("streams", []) if probe.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return "unreadable"
    video = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None)
    if video is None:
        return "no_video"
    try:
        counted = int(video.get("nb_read_frames"))
        numerator, denominator = str(video.get("avg_frame_rate", "0/1")).split("/", 1)
        fps = float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return "unreadable"
    if counted != frames:
        return "frame_count"
    if abs(fps - float(manifest["capabilities"]["duration"]["fps"])) > 0.01:
        return "fps"
    if manifest["capabilities"].get("audio") == "required_generated" and not any(
            isinstance(s, dict) and s.get("codec_type") == "audio" for s in streams):
        return "no_audio"
    try:
        poster = subprocess.run([str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(path), "-frames:v", "1",
                                 "-f", "image2pipe", "-vcodec", "png", "pipe:1"], capture_output=True, timeout=120, check=False, env=child_env())
    except (OSError, subprocess.SubprocessError):
        return "poster"
    if poster.returncode != 0 or not poster.stdout.startswith(b"\x89PNG"):
        return "poster"
    return None


__all__ = [
    "SCHEMA_VERIFIED", "EXACT_TESTED", "RUNTIME_KEYS", "derive_fingerprint", "extend_fingerprint", "effective_fingerprint",
    "schema_problem",
    "detail", "runtime_identity", "runtime_verdict", "read_tested", "record_tested", "expected_frames",
    "verify_take_output",
]
