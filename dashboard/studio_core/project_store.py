"""Project metadata read boundary.

Task 1 intentionally only reads an existing V1 manifest.  Creation, state
writes, recents, and CAS belong to later vertical slices; this module keeps
those concerns out of the first path-safety boundary.
"""

from __future__ import annotations
from .legacy_plugin_migration import state_root, TOMBSTONE_PATTERN


import contextlib
import copy
import ctypes
import ctypes.wintypes as wintypes
import datetime as _datetime
import errno
import hashlib
import importlib.util
import json
import math
import os
import re
import secrets
import stat
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .path_boundary import _local_path  # lane w34-sec: the lexical UNC / device guard (local_path.py)
from .path_boundary import (
    NamedProjectLock,
    PathBoundary,
    ProjectError,
    _flush_private_file,
    _fsync_parent,
    _hash_handle,
    _identity_from_handle,
    _open_child_existing,
    _open_existing,
    _posix_linkat_empty_path,
    ensure_private_node,
    unlink_handle_bound,
    win_link_handle_no_replace,
)


MANIFEST_SCHEMA = "hermes-film-project/v1"
RECENT_REGISTRY_MAX_BYTES = 64 * 1024
# Explicit repair actions may quarantine a bounded oversized/corrupt regular
# file, but never inspect or retain an operationally unbounded object.
RECENT_REGISTRY_HARD_MAX_BYTES = 16 * 1024 * 1024
RECENT_MAX_ENTRIES = 12
RECENT_WARNING = "recents_unavailable"
_RECENT_REGISTRY_NAME = "recent-projects.json"
_CORRUPT_RECENT_RE = re.compile(r"^recent-projects\.corrupt-[0-9TZ-]+-[0-9a-f]{32}\.json$")
MANIFEST_MAX_BYTES = 256 * 1024
_MANIFEST_KEYS = {
    "schema",
    "project_id",
    "name",
    "revision",
    "created_at",
    "updated_at",
    "approved_roots",
}
# Lane p5c-frm (Project charter decision 2): the one output folder of the project, optional. Absent = the legacy
# Shoot state's outputFolder if one is set, else <project>/output. Stored relative to the project folder when inside.
_MANIFEST_OPTIONAL_KEYS = frozenset({"output_folder"})
OUTPUT_DEFAULT_NAME = "output"
_PROJECT_ID_RE = re.compile(r"^prj_[0-9a-f]{32}$")


class ProjectManifestError(ProjectError):
    pass


def _validate_output_folder(value: dict[str, Any]) -> None:
    if "output_folder" not in value:
        return
    folder = value["output_folder"]
    if not isinstance(folder, str) or not folder.strip() or len(folder) > 32767 or "\x00" in folder:
        _invalid("manifest output folder is invalid")
    # Lane w34-sec (finding 13): the output folder is refused like an approved root when its text names a network or
    # device folder (the manifest is invalid, as with a \\\\host root). Where a local folder may receive files ("..",
    # outside the project, a link) is decided per write and per root list by BCK-226 (_project_output_refusal).
    if _local_path.remote_path_reason(folder) is not None:
        _invalid("manifest output folder is invalid")


def output_folder_of(directory: str | Path, manifest: Mapping[str, Any], state_output: Any = None) -> tuple[Path, bool]:
    """The project's output folder and whether it is the default <project>/output.

    Manifest output_folder (relative = inside the project folder) wins; else a legacy Shoot state
    outputFolder (the folder the artist chose in Video before the project setting existed); else the default.
    """
    project_dir = Path(directory)
    stored = manifest.get("output_folder")
    if isinstance(stored, str) and stored.strip():
        candidate = Path(stored)
        folder = candidate if candidate.is_absolute() else project_dir / candidate
    elif (isinstance(state_output, str) and state_output.strip() and Path(state_output.strip()).is_absolute()
          and _local_path.remote_path_reason(state_output) is None):  # lane w34-sec: never a network folder
        folder = Path(state_output.strip())
    else:
        folder = project_dir / OUTPUT_DEFAULT_NAME
    default = os.path.normcase(os.path.abspath(folder)) == os.path.normcase(os.path.abspath(project_dir / OUTPUT_DEFAULT_NAME))
    return folder, default


def _invalid(message: str = "invalid project manifest") -> None:
    raise ProjectError("invalid_project", message)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid("manifest contains duplicate keys")
        result[key] = value
    return result


def _parse_timestamp(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        _invalid(f"manifest {key} is invalid")
    try:
        parsed = _datetime.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _invalid(f"manifest {key} is invalid")
    if parsed.tzinfo is None or parsed.utcoffset() != _datetime.timedelta(0):
        _invalid(f"manifest {key} is invalid")
    return value


def _validate_name(value: Any) -> str:
    if not isinstance(value, str) or not (1 <= len(value) <= 120) or value != value.strip():
        _invalid("manifest name is invalid")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        _invalid("manifest name is invalid")
    return value


def _validate_manifest(value: Any, *, boundary: PathBoundary, directory: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        _invalid()
    if not (_MANIFEST_KEYS <= set(value) <= _MANIFEST_KEYS | _MANIFEST_OPTIONAL_KEYS):
        _invalid("manifest keys are not the V1 allowlist")
    _validate_output_folder(value)
    if value.get("schema") != MANIFEST_SCHEMA:
        _invalid("manifest schema is unsupported")
    project_id = value.get("project_id")
    if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
        _invalid("manifest project id is invalid")
    _validate_name(value.get("name"))
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        _invalid("manifest revision is invalid")
    _parse_timestamp(value.get("created_at"), "created_at")
    _parse_timestamp(value.get("updated_at"), "updated_at")
    roots = _roots_from_disk(value.get("approved_roots"), directory)
    if not isinstance(roots, list) or len(roots) > 64:
        _invalid("manifest approved roots are invalid")
    normalized_roots: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if not isinstance(root, str):
            _invalid("manifest approved roots are invalid")
        try:
            root_path = boundary.validate_project_directory(root, operation="open")
        except ProjectError as exc:
            if not _root_offline(root, exc):  # lane p13-prj item 3: an unplugged folder reads offline, kept verbatim
                raise
            key = "offline:" + os.path.normcase(os.path.normpath(root))
            if key in seen:
                _invalid("manifest approved roots are not unique")
            seen.add(key)
            normalized_roots.append(root)
            continue
        identity = boundary.pin_directory(root_path)
        key = identity.canonical_path
        if key in seen:
            _invalid("manifest approved roots are not unique")
        seen.add(key)
        normalized_roots.append(str(root_path))
    result = dict(value)
    result["approved_roots"] = normalized_roots
    return result


def read_project(
    directory: str | Path,
    project_id: str | None = None,
    expected_project_id: str | None = None,
    hermes_home: str | Path | None = None,
    boundary: PathBoundary | None = None,
) -> dict[str, Any]:
    """Validate and read one existing V1 project manifest.

    The returned mapping is detached from the decoded JSON.  No project or
    media directory is scanned; only the chosen directory and its manifest are
    opened, and approved roots are individually revalidated when declared.
    """

    active_boundary = boundary or PathBoundary(hermes_home=hermes_home)
    value = active_boundary.read_manifest(
        directory,
        max_bytes=MANIFEST_MAX_BYTES,
        object_pairs_hook=_reject_duplicate_keys,
    )
    manifest = _validate_manifest(value, boundary=active_boundary, directory=Path(directory))
    expected = expected_project_id if expected_project_id is not None else project_id
    if expected is not None and manifest["project_id"] != expected:
        raise ProjectError("invalid_project", "project identity does not match")
    return manifest


STATE_SCHEMA = "hermes-film-shoot-state/v1"
STATE_MAX_BYTES = 4 * 1024 * 1024
MUSIC_INTERPRETATION_MAX_BYTES = 4 * 1024 * 1024
JSON_MAX_DEPTH = 12
JSON_MAX_OBJECT_KEYS = 256
JSON_MAX_LIST_ITEMS = 1000
JSON_MAX_STRING_BYTES = 256 * 1024
PATH_TEXT_MAX_CHARS = 32767
_TEMP_RE = re.compile(r"^\.studio-[0-9a-f]{32}\.tmp$")
_TOMBSTONE_RE = re.compile(TOMBSTONE_PATTERN)
_PROJECT_ID_RE = re.compile(r"^prj_[0-9a-f]{32}$")
_MUSIC_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_PATH_TEXT_KEYS = frozenset(
    {
        "outputFolder",
        "image",
        "video",
        "musicTrack",
        "root",
        "source",
        "source_path",
        "prepared_path",
        "folder",
        "saveTo",
        "save_to",
        "input_image",
        "input_images",
        "input_video",
        "keyframeSources",
    }
)
_STATE_KEYS = {
    "mode", "aspectRatio", "durationText", "audio", "grounding", "outputFolder",
    "filename", "prompt", "seed", "version", "image", "video", "parent",
    "approvedRoots", "mediaAudit", "musicTrack", "musicSelection", "keyframes",
    "jobs", "pending", "outgoing", "lastSubmitAt", "sequence",
    "selectedProviderId", "selectedTargetId", "heldQueue", "queueSchemaVersion",
    "drafts", "h3Prompt", "h3Duration", "h3Seed", "h3Aspect", "h3Megapixels",
}
# H3 Slice2B renderer UI fields. Optional and additive: the default/legacy
# state keeps exactly _STATE_KEYS (33 keys, byte-stable); a state may carry
# either none or all three of these, each a free-form draft string.
_H3_SAMPLING_STATE_KEYS = frozenset({"h3Steps", "h3Sampler", "h3Scheduler"})
# Lane w29-fix2 item 1 (P2 VID-166; the M-84 prompt / M-197 length rule for the rest of a catalog target's draft):
# the per-target draft of every catalog target (prompt, values, media, sound, ...) lives in the project's shoot
# state, so a copied project keeps its Sound track. Optional and additive like the sampling trio: a state that never
# had one keeps exactly its keys. {"<provider>|<target>": {prompt?, values?, media?, sound?, h3x?, continueOut?}}.
_CATALOG_DRAFT_STATE_KEYS = frozenset({"catalogDrafts"})
_CATALOG_DRAFT_FIELDS = frozenset({"prompt", "values", "media", "sound", "h3x", "continueOut"})
_CATALOG_DRAFTS_MAX = 64
_H3_LEGACY_DRAFT_KEYS = frozenset({"kind", "prompt", "duration", "seed", "aspect", "megapixels"})
_H3_SAMPLING_DRAFT_KEYS = frozenset({"steps", "sampler_name", "scheduler"})
_H3_SAMPLING_DRAFT_IDENTITY = "comfyui-local|minimax-h3-t2v-sampling-v2"
_FLUX_TARGET_BY_MODE = {
    "text_to_video": "text-to-video",
    "image_to_video": "image-to-video",
    "keyframes_to_video": "keyframes-to-video",
    "video_continuation": "video-continuation",
}
_FLUX_PROVIDER_ID = "flux3-managed"


# Lane w11-fixb item 4 (M-198): a new project's Shoot default is never the paid FLUX target. It is the first
# local catalog target, read from the generation registry (generation/registry.py, the catalog the page shows),
# by the page's M-18 rule (defaultGenerationTarget in desktop/film-suite/src/catalog/descriptors.js): the first
# provider whose location is local, its Frames target (keyframes_to_video, not a continuation) first, else its
# first target. Loaded once under its own module name; never a hard-coded target name.
_DEFAULT_TARGET_LOCK = threading.Lock()
_DEFAULT_TARGET: tuple[str, str] | None = None


def _default_generation_target() -> tuple[str, str]:
    global _DEFAULT_TARGET
    with _DEFAULT_TARGET_LOCK:
        if _DEFAULT_TARGET is not None:
            return _DEFAULT_TARGET
        directory = Path(__file__).resolve().parent.parent / "generation"
        package_name = "film_suite_project_store_generation"
        loaded: dict[str, Any] = {}
        try:
            for leaf in ("contract", "registry"):
                name = f"{package_name}.{leaf}"
                spec = importlib.util.spec_from_file_location(name, directory / f"{leaf}.py")
                if spec is None or spec.loader is None:
                    raise ProjectError("project_unavailable", "generation catalog is unavailable")
                module = importlib.util.module_from_spec(spec)
                module.__package__ = package_name
                sys.modules[name] = module
                loaded[name] = module
                spec.loader.exec_module(module)
            catalog = loaded[f"{package_name}.registry"].catalog()
        finally:
            for name in loaded:
                sys.modules.pop(name, None)
        for provider in catalog.get("providers", []):
            if provider.get("location") != "local":
                continue
            targets = [t for t in provider.get("targets", []) if isinstance(t, dict) and isinstance(t.get("id"), str)]
            frames = [t for t in targets if t.get("mode") == "keyframes_to_video" and not t.get("continuation_role")]
            chosen = (frames or targets or [None])[0]
            if chosen is not None:
                _DEFAULT_TARGET = (provider["id"], chosen["id"])
                return _DEFAULT_TARGET
        raise ProjectError("project_unavailable", "no local generation target in the catalog")
_STATE_ENVELOPE_KEYS = {"schema", "project_id", "revision", "updated_at", "state"}


def _invalid(message: str = "invalid project state") -> None:
    raise ProjectError("invalid_project", message)


def _project_conflict() -> None:
    raise ProjectError("project_conflict", "project revision conflict")


def _validate_tree(value: Any, *, depth: int = 0) -> None:
    if depth > JSON_MAX_DEPTH:
        _invalid("project JSON is too deeply nested")
    if isinstance(value, dict):
        if len(value) > JSON_MAX_OBJECT_KEYS:
            _invalid("project JSON has too many object keys")
        for key, child in value.items():
            if not isinstance(key, str):
                _invalid("project JSON object key is invalid")
            _validate_tree(key, depth=depth + 1)
            _validate_tree(child, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > JSON_MAX_LIST_ITEMS:
            _invalid("project JSON list is too large")
        for child in value:
            _validate_tree(child, depth=depth + 1)
        return
    if isinstance(value, str):
        try:
            if len(value.encode("utf-8")) > JSON_MAX_STRING_BYTES:
                _invalid("project JSON string is too large")
        except UnicodeEncodeError:
            _invalid("project JSON string is invalid")
        return
    if isinstance(value, float) and not math.isfinite(value):
        _invalid("project JSON number is invalid")
    if value is None or isinstance(value, (bool, int, float)):
        return
    _invalid("project JSON value is invalid")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _invalid("project JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    _invalid("project JSON number is invalid")


def _loads_bounded(raw: bytes) -> Any:
    if raw.startswith(bytes((0xEF, 0xBB, 0xBF))):
        _invalid("project JSON must not contain a UTF-8 BOM")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
        )
        _validate_tree(value)
    except ProjectError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError, MemoryError, OverflowError):
        _invalid("project JSON is not valid UTF-8 JSON")
    return value


def _dump_bounded(value: Any) -> bytes:
    _validate_tree(value)
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        _invalid("project JSON value is invalid")
    return payload


def _bounded_payload(value: Any, *, max_bytes: int, label: str) -> bytes:
    payload = _dump_bounded(value)
    if len(payload) > max_bytes:
        _invalid(f"{label} is too large")
    return payload


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not _TIMESTAMP_RE.fullmatch(value):
        _invalid("project timestamp is invalid")
    try:
        parsed = _datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _invalid("project timestamp is invalid")
    if parsed.tzinfo is not None:
        _invalid("project timestamp is invalid")
    return value


def _now_timestamp() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _next_timestamp(previous: str) -> str:
    current = _now_timestamp()
    if current > previous:
        return current
    try:
        value = _datetime.datetime.strptime(previous, "%Y-%m-%dT%H:%M:%SZ") + _datetime.timedelta(seconds=1)
    except (OverflowError, ValueError):
        _invalid("project timestamp is invalid")
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _name(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= 120:
        _invalid("project name is invalid")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in value):
        _invalid("project name is invalid")
    try:
        if len(value.encode("utf-8")) > JSON_MAX_STRING_BYTES:
            _invalid("project name is invalid")
    except UnicodeEncodeError:
        _invalid("project name is invalid")
    return value


def _project_id(value: Any) -> str:
    if not isinstance(value, str) or not _PROJECT_ID_RE.fullmatch(value):
        _invalid("project id is invalid")
    return value


def _read_regular(boundary: PathBoundary, path: Path, *, max_bytes: int) -> bytes:
    try:
        handle, expected = _open_existing(path, directory=False)
    except ProjectError:
        raise
    except OSError as exc:
        raise ProjectError("invalid_project", "project file is unavailable") from exc
    try:
        if expected.size > max_bytes:
            _invalid("project file is too large")
        data = boundary.read_handle_bytes(handle, max_bytes=max_bytes)
        actual = _identity_from_handle(handle, path)
        if not expected.same_object(actual) or expected.size != actual.size:
            raise ProjectError("project_unavailable", "project file identity changed")
        return data
    finally:
        handle.close()


def _normalize_roots(boundary: PathBoundary, roots: Any, *, offline_ok: bool = False) -> list[str]:
    if not isinstance(roots, list) or len(roots) > 64:
        _invalid("approved roots are invalid")
    normalized: list[str] = []
    identities: set[tuple[int, int, str]] = set()
    offline: set[str] = set()
    for root in roots:
        if not isinstance(root, str) or len(root) > 32767:
            _invalid("approved roots are invalid")
        try:
            root_path = boundary.validate_project_directory(root, operation="open")
            identity = boundary.pin_directory(root_path)
        except ProjectError as exc:
            # Lane p13-prj item 3 (PRJ-019; Resolve opens the project and shows media on a missing volume Offline):
            # reading a stored manifest, an absolute approved folder that is not there now (unplugged drive,
            # unreachable share) is kept verbatim and offline; it never blocks the open and is never re-based or
            # dropped (p12-medb). Writes (offline_ok False) still require every folder to exist.
            if not (offline_ok and _root_offline(root, exc)):
                raise
            key_text = os.path.normcase(os.path.normpath(root))
            if key_text in offline:
                _invalid("approved roots are not unique")
            offline.add(key_text)
            normalized.append(root)
            continue
        except Exception:
            _invalid("approved roots are invalid")
        key = (identity.volume_serial, identity.file_id, identity.canonical_path)
        if key in identities:
            _invalid("approved roots are not unique")
        identities.add(key)
        normalized.append(str(root_path))
    return normalized


def _root_offline(root: str, exc: ProjectError) -> bool:
    """True when ``root`` is an absolute folder that cannot be reached now (missing, or its volume unplugged)."""
    if getattr(exc, "code", None) not in ("project_not_found", "project_unavailable") or not os.path.isabs(root):
        return False
    if any(ch in root for ch in "\x00<>\"|?*"):
        return False
    # Lane p13-prjb finding 5 (cold review p13-prj Minor 3): only a folder on a named drive can be offline (Resolve:
    # media is offline when its VOLUME is missing). A rooted path without a drive (\\missing) or a folder below an
    # existing file can never be a folder, so it stays refused as before p13-prj.
    if not os.path.splitdrive(root)[0]:
        return False
    try:
        parent = os.path.dirname(os.path.normpath(root))
        while parent and not os.path.exists(parent):
            above = os.path.dirname(parent)
            if above == parent:
                break
            parent = above
        if parent and os.path.exists(parent) and not os.path.isdir(parent):
            return False
        return not os.path.isdir(root)
    except OSError:  # an unreachable share: offline
        return True


PROJECT_FOLDER_OFFLINE = "project_folder_offline"


def project_folder_offline_message(root: str) -> str:
    """The refusal text of a manifest write while ``root`` is offline (lane p13-prjb finding 1)."""
    return f"{root} is offline. Plug it in to change this project's settings."


PROJECT_REBASE_PENDING = "project_rebase_pending"


def project_rebase_pending_message(root: str) -> str:
    """The refusal text of a write into a moved or copied project's old place while ``root`` is offline (p13-prjc)."""
    return f"Plug in {root} to finish moving this project."


def _enclosing_project_id(folder: str) -> tuple[str, Any] | None:
    """The nearest folder at or above ``folder`` that holds a project.json, with that manifest's project_id."""
    current = os.path.normpath(folder)
    for _level in range(64):
        candidate = os.path.join(current, "project.json")
        try:
            if os.path.isfile(candidate) and os.path.getsize(candidate) <= MANIFEST_MAX_BYTES:
                with open(candidate, "rb") as handle:
                    data = json.loads(handle.read(MANIFEST_MAX_BYTES + 1).decode("utf-8"))
                return current, data.get("project_id") if isinstance(data, dict) else None
        except (OSError, ValueError, UnicodeError):
            return current, None
        above = os.path.dirname(current)
        if above == current:
            return None
        current = above
    return None


def rebase_pending_roots(directory: Any, project_id: Any, roots: Any) -> tuple[str | None, list[str]]:
    """While a moved or copied project's re-base is pending: (the first offline folder as stored, the roots at its old place).

    Lane p13-prjc finding 1 (second cold review p13-prj, Minor 1): ``rebase_moved_project`` keeps the manifest as stored
    while a folder is offline (p13-prjb finding 2), so a copy's folders still name the ORIGINAL. A root lies at the old
    place when the nearest project.json at or above it has this project's id and is not this project's folder (the
    original, with or without a Recent entry). Nothing is pending when no folder is offline: the open re-bases then.
    """
    offline = offline_roots(list(roots) if isinstance(roots, (list, tuple)) else [])
    if not offline or not isinstance(directory, (str, os.PathLike)) or not isinstance(project_id, str):
        return None, []
    own = str(directory)
    blocked: list[str] = []
    for root in roots:
        if not isinstance(root, str) or not root or not os.path.isabs(root) or root in offline:
            continue
        try:
            if not os.path.isdir(root):
                continue
        except OSError:
            continue
        found = _enclosing_project_id(root)
        if found is not None and found[1] == project_id and not _same_path(found[0], own):
            blocked.append(root)
    return (offline[0], blocked) if blocked else (None, [])


def offline_roots(roots: Any) -> list[str]:
    """The approved folders of a read manifest that are offline now (lane p13-prj item 3), in stored order."""
    out: list[str] = []
    for root in roots if isinstance(roots, list) else ():
        try:
            if isinstance(root, str) and root and _local_path.remote_path_reason(root) is not None:
                out.append(root)  # lane w34-sec: a network / device folder is never probed; it is no folder here
            elif isinstance(root, str) and root and not os.path.isdir(root):
                out.append(root)
        except OSError:
            out.append(root)
    return out


def _root_identities(boundary: PathBoundary, roots: list[Any]) -> list[tuple[int, int]] | None:
    keys: list[tuple[int, int]] = []
    for root in roots:
        if not isinstance(root, str) or not root or len(root) > 32767:
            return None
        try:
            identity = boundary.pin_directory(boundary.validate_project_directory(root, operation="open"))
        except Exception:
            return None
        keys.append((identity.volume_serial, identity.file_id))
    return keys


def _mirror_manifest_roots(boundary: PathBoundary, state_roots: Any, manifest_roots: list[str]) -> Any:
    # The Shoot mirror may name the manifest's folders in another spelling
    # (C:/a for C:\\a): the renderer keeps what the picker, a typed path or an
    # agent sent, the manifest keeps its canonical form. The same folders in
    # the same order are the same mirror, so it is stored in the manifest's own
    # form. Anything else is left as sent and fails the mirror check as before.
    if not isinstance(state_roots, list) or state_roots == manifest_roots or len(state_roots) != len(manifest_roots):
        return state_roots
    mine = _root_identities(boundary, state_roots)
    if mine is None or mine != _root_identities(boundary, manifest_roots):
        return state_roots
    return list(manifest_roots)


def _validate_manifest_v20(boundary: PathBoundary, value: Any, directory: Any = None) -> dict[str, Any]:
    _validate_tree(value)
    if not isinstance(value, dict) or not (_MANIFEST_KEYS <= set(value) <= _MANIFEST_KEYS | _MANIFEST_OPTIONAL_KEYS):
        _invalid("manifest keys are not the V1 allowlist")
    _validate_output_folder(value)
    if value["schema"] != MANIFEST_SCHEMA:
        _invalid("manifest schema is unsupported")
    _project_id(value["project_id"])
    _name(value["name"])
    revision = value["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        _invalid("manifest revision is invalid")
    _timestamp(value["created_at"])
    _timestamp(value["updated_at"])
    normalized = _normalize_roots(boundary, _roots_from_disk(value["approved_roots"], directory), offline_ok=directory is not None)
    result = dict(value)
    result["approved_roots"] = normalized
    return result


def _default_state(approved_roots: list[str]) -> dict[str, Any]:
    # This is the 33-field renderer write block.  The three media-view
    # preferences are intentionally global and therefore absent here.
    return {
        "mode": "text_to_video",
        "aspectRatio": "auto",
        "durationText": "",
        "audio": True,
        "grounding": True,
        "outputFolder": "",
        "filename": "",
        "prompt": "",
        "seed": "",
        "version": "",
        "image": "",
        "video": "",
        "parent": None,
        "approvedRoots": list(approved_roots),
        "mediaAudit": [],
        "musicTrack": "",
        "musicSelection": None,
        "keyframes": [{"id": secrets.token_hex(16), "source": "", "frame": 0, "fit": "original"}],
        "jobs": [],
        "pending": [],
        "outgoing": None,
        "lastSubmitAt": 0,
        "sequence": 1,
        "selectedProviderId": _default_generation_target()[0],
        "selectedTargetId": _default_generation_target()[1],
        "heldQueue": [],
        "queueSchemaVersion": 2,
        "drafts": {},
        "h3Prompt": "",
        "h3Duration": 5,
        "h3Seed": "",
        "h3Aspect": "",
        "h3Megapixels": "",
    }


def _finite_number(value: Any, field: str) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid(f"shoot {field} is invalid")
    try:
        finite = math.isfinite(value)
    except (OverflowError, ValueError, TypeError):
        _invalid(f"shoot {field} is invalid")
    if not finite:
        _invalid(f"shoot {field} is invalid")
    return value


def _integer(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid(f"shoot {field} is invalid")
    if minimum is not None and value < minimum:
        _invalid(f"shoot {field} is invalid")
    return value


def _state_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        _invalid(f"shoot {field} is invalid")
    return value


def _exact_object(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _invalid(f"shoot {field} is invalid")
    return value


def _optional_exact_object(value: Any, keys: set[str], field: str) -> None:
    if value is not None:
        _exact_object(value, keys, field)


def _validate_keyframes(value: Any) -> None:
    if not isinstance(value, list):
        _invalid("shoot keyframes are invalid")
    for row in value:
        item = _exact_object(row, {"id", "source", "frame", "fit"}, "keyframe")
        _state_string(item["id"], "keyframe id")
        _state_string(item["source"], "keyframe source")
        _integer(item["frame"], "keyframe frame", minimum=0)
        # The renderer's FIT_OPTIONS: As-is, Crop, Pad (letterbox).
        if item["fit"] not in {"original", "fill", "fit"}:
            _invalid("shoot keyframe fit is invalid")


def _validate_path_text(value: Any, field: str) -> None:
    if isinstance(value, str):
        if len(value) > PATH_TEXT_MAX_CHARS:
            _invalid(f"shoot {field} path is too long")
        return
    if isinstance(value, list):
        for item in value:
            _validate_path_text(item, field)
        return
    _invalid(f"shoot {field} path is invalid")


def _validate_path_texts(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _PATH_TEXT_KEYS:
                _validate_path_text(child, key)
            else:
                _validate_path_texts(child)
    elif isinstance(value, list):
        for child in value:
            _validate_path_texts(child)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _rebase_path(value: str, old: str, new: str) -> str:
    """value moved from under ``old`` to under ``new``; any other value unchanged."""
    if not value:
        return value
    norm = os.path.normpath(value)
    base = os.path.normpath(old)
    if os.path.normcase(norm) == os.path.normcase(base):
        return new
    prefix = base.rstrip("\\/") + os.sep
    if os.path.normcase(norm).startswith(os.path.normcase(prefix)):
        return os.path.join(new, norm[len(prefix):])
    return value


def _rebase_path_fields(value: Any, old: str, new: str, *, path: bool = False) -> Any:
    if isinstance(value, dict):
        return {key: _rebase_path_fields(child, old, new, path=path or key in _PATH_TEXT_KEYS) for key, child in value.items()}
    if isinstance(value, list):
        return [_rebase_path_fields(child, old, new, path=path) for child in value]
    if path and isinstance(value, str):
        return _rebase_path(value, old, new)
    return value


def _validate_range(value: Any, field: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        _invalid(f"shoot {field} is invalid")
    allowed = {"start", "end", "inFrame", "outFrame", "in_frame", "out_frame", "fps", "frame_accurate", "actual_duration", "actual_bytes"}
    if not set(value).issubset(allowed):
        _invalid(f"shoot {field} is invalid")
    for key in ("start", "end", "fps", "actual_duration", "actual_bytes"):
        if key in value:
            _finite_number(value[key], f"{field} {key}")
    for key in ("inFrame", "outFrame", "in_frame", "out_frame"):
        if key in value:
            _integer(value[key], f"{field} {key}", minimum=0)
    if "frame_accurate" in value and not isinstance(value["frame_accurate"], bool):
        _invalid(f"shoot {field} is invalid")


def _validate_parent(value: Any, field: str = "parent") -> None:
    if value is None:
        return
    allowed = {"method", "source_path", "prepared_path", "trim", "job_id", "prepared_sha256", "prepared_frame_count", "derivations"}
    if not isinstance(value, dict) or not set(value).issubset(allowed):
        _invalid(f"shoot {field} is invalid")
    for key in ("method", "source_path", "prepared_path", "job_id"):
        if key in value:
            _state_string(value[key], f"{field} {key}")
    if "prepared_sha256" in value:
        digest = _state_string(value["prepared_sha256"], f"{field} prepared_sha256")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            _invalid(f"shoot {field} is invalid")
    if "prepared_frame_count" in value:
        _integer(value["prepared_frame_count"], f"{field} prepared_frame_count", minimum=1)
    if "trim" in value:
        _validate_range(value["trim"], f"{field} trim")
    if "derivations" in value:
        derivations = value["derivations"]
        if not isinstance(derivations, list):
            _invalid(f"shoot {field} derivations are invalid")
        for derivation in derivations:
            if not isinstance(derivation, dict):
                _invalid(f"shoot {field} derivations are invalid")
            if "params" in derivation and not isinstance(derivation["params"], dict):
                _invalid(f"shoot {field} derivations are invalid")


def _validate_media_audit(value: Any) -> None:
    if not isinstance(value, list):
        _invalid("shoot media audit is invalid")
    for item in value:
        row = _exact_object(item, {"at", "action", "root"}, "media audit entry")
        _state_string(row["at"], "media audit timestamp")
        if row["action"] not in {"added", "removed"}:
            _invalid("shoot media audit action is invalid")
        _state_string(row["root"], "media audit root")


def _validate_identity_object(value: Any, field: str) -> None:
    if not isinstance(value, dict):
        _invalid(f"shoot {field} entry is invalid")
    has_provider = "providerId" in value
    has_target = "targetId" in value
    if has_provider != has_target:
        _invalid(f"shoot {field} identity is invalid")
    if has_provider:
        provider = value["providerId"]
        target = value["targetId"]
        if not isinstance(provider, str) or not provider or not isinstance(target, str) or not target:
            _invalid(f"shoot {field} identity is invalid")
    entry = value.get("entry")
    if entry is not None:
        _validate_identity_object(entry, f"{field} entry")


def _validate_queue(value: Any, field: str) -> None:
    if not isinstance(value, list):
        _invalid(f"shoot {field} is invalid")
    for item in value:
        _validate_identity_object(item, field)


def _validate_drafts(value: Any) -> None:
    if not isinstance(value, dict):
        _invalid("shoot drafts are invalid")
    for identity, draft in value.items():
        if not isinstance(identity, str) or not re.fullmatch(r"[^|]{1,256}\|[^|]{1,256}", identity):
            _invalid("shoot draft identity is invalid")
        if not isinstance(draft, dict) or draft.get("kind") not in {"flux", "h3"}:
            _invalid("shoot draft is invalid")
        if draft["kind"] == "h3":
            # Legacy six-field H3 drafts stay exact for every target; only the
            # sampling-v2 identity may additionally carry all three sampling
            # fields (draft text, not a submit: strings, not re-validated).
            if identity == _H3_SAMPLING_DRAFT_IDENTITY and set(draft) == _H3_LEGACY_DRAFT_KEYS | _H3_SAMPLING_DRAFT_KEYS:
                item = _exact_object(draft, set(_H3_LEGACY_DRAFT_KEYS | _H3_SAMPLING_DRAFT_KEYS), "h3 draft")
                for key in sorted(_H3_SAMPLING_DRAFT_KEYS):
                    _state_string(item[key], f"h3 draft {key}")
            else:
                item = _exact_object(draft, set(_H3_LEGACY_DRAFT_KEYS), "h3 draft")
            _state_string(item["prompt"], "h3 draft prompt")
            _integer(item["duration"], "h3 draft duration", minimum=1)
            _state_string(item["seed"], "h3 draft seed")
            _state_string(item["aspect"], "h3 draft aspect")
            _state_string(item["megapixels"], "h3 draft megapixels")
        else:
            item = _exact_object(draft, {"kind", "mode", "prompt", "image", "video", "parent", "continuationRange", "keyframes", "aspectRatio", "durationText", "audio", "grounding", "version", "seed"}, "flux draft")
            if item["mode"] not in _FLUX_TARGET_BY_MODE:
                _invalid("shoot flux draft mode is invalid")
            for key in ("prompt", "image", "video", "aspectRatio", "durationText", "version", "seed"):
                _state_string(item[key], f"flux draft {key}")
            if not isinstance(item["audio"], bool) or not isinstance(item["grounding"], bool):
                _invalid("shoot flux draft audio flags are invalid")
            _validate_parent(item["parent"], "flux draft parent")
            _validate_range(item["continuationRange"], "flux draft continuation range")
            _validate_keyframes(item["keyframes"])


def _validate_catalog_drafts(value: Any) -> None:
    """Lane w29-fix2 item 1 (VID-166): the catalog targets' drafts (shape only; a submit validates its own args)."""
    if not isinstance(value, dict) or len(value) > _CATALOG_DRAFTS_MAX:
        _invalid("shoot catalog drafts are invalid")
    for identity, draft in value.items():
        if not isinstance(identity, str) or not re.fullmatch(r"[^|]{1,256}\|[^|]{1,256}", identity):
            _invalid("shoot catalog draft identity is invalid")
        if not isinstance(draft, dict) or not set(draft) <= _CATALOG_DRAFT_FIELDS:
            _invalid("shoot catalog draft is invalid")
        if "prompt" in draft:
            _state_string(draft["prompt"], "catalog draft prompt")
        for key in ("values", "media", "sound", "h3x"):
            if key in draft and not isinstance(draft[key], dict):
                _invalid(f"shoot catalog draft {key} is invalid")
        if "continueOut" in draft and draft["continueOut"] is not None and not isinstance(draft["continueOut"], dict):
            _invalid("shoot catalog draft continueOut is invalid")


def _validate_state_v20(
    boundary: PathBoundary,
    project_id: str,
    roots: list[str],
    value: Any,
    *,
    allow_roots_mismatch: bool = False,
) -> dict[str, Any]:
    _validate_tree(value)
    if not isinstance(value, dict) or set(value) != _STATE_ENVELOPE_KEYS:
        _invalid("shoot state keys are not the V1 allowlist")
    if value.get("schema") != STATE_SCHEMA or value.get("project_id") != project_id:
        _invalid("shoot state identity is invalid")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        _invalid("shoot state revision is invalid")
    _timestamp(value.get("updated_at"))
    state = value.get("state")
    if not isinstance(state, dict) or set(state) - _CATALOG_DRAFT_STATE_KEYS not in (_STATE_KEYS, _STATE_KEYS | _H3_SAMPLING_STATE_KEYS):
        _invalid("shoot state fields are not the renderer allowlist")
    if "catalogDrafts" in state:
        _validate_catalog_drafts(state["catalogDrafts"])
    mode = _state_string(state.get("mode"), "mode")
    if mode not in _FLUX_TARGET_BY_MODE:
        _invalid("shoot mode is invalid")
    for field in (
        "aspectRatio", "durationText", "outputFolder", "filename", "prompt", "seed", "version",
        "image", "video", "musicTrack", "h3Prompt", "h3Seed", "h3Aspect", "h3Megapixels",
    ):
        _state_string(state.get(field), field)
    for field in sorted(_H3_SAMPLING_STATE_KEYS & set(state)):
        _state_string(state[field], field)
    if not isinstance(state.get("audio"), bool) or not isinstance(state.get("grounding"), bool):
        _invalid("shoot audio flags are invalid")
    _validate_parent(state.get("parent"))
    state_roots = state.get("approvedRoots")
    if not isinstance(state_roots, list) or len(state_roots) > 64:
        _invalid("shoot approved roots do not match the manifest")
    if any(not isinstance(root, str) or len(root) > 32767 for root in state_roots):
        _invalid("shoot approved roots do not match the manifest")
    if not allow_roots_mismatch and state_roots != roots:
        _invalid("shoot approved roots do not match the manifest")
    _validate_media_audit(state.get("mediaAudit"))
    music_selection = state.get("musicSelection")
    if music_selection is not None:
        item = _exact_object(music_selection, {"start", "end"}, "music selection")
        _finite_number(item["start"], "music selection start")
        _finite_number(item["end"], "music selection end")
    _validate_keyframes(state.get("keyframes"))
    _validate_queue(state.get("jobs"), "jobs")
    _validate_queue(state.get("pending"), "pending")
    if state.get("outgoing") is not None:
        _validate_identity_object(state["outgoing"], "outgoing")
    _validate_queue(state.get("heldQueue"), "held queue")
    _validate_drafts(state.get("drafts"))
    _validate_path_texts(state)
    if len(state["jobs"]) > 40:
        _invalid("shoot jobs are too numerous")
    if len(state["pending"]) > 100 or len(state["heldQueue"]) > 100:
        _invalid("shoot queue is too large")
    provider = _state_string(state.get("selectedProviderId"), "provider")
    target = _state_string(state.get("selectedTargetId"), "target")
    if not provider or len(provider) > 256:
        _invalid("shoot provider is invalid")
    if not target or len(target) > 256 or "|" in target:
        _invalid("shoot target is invalid")
    if provider == _FLUX_PROVIDER_ID and target != _FLUX_TARGET_BY_MODE[mode]:
        _invalid("shoot target does not match mode")
    if target in _FLUX_TARGET_BY_MODE:
        _invalid("shoot target must be an unqualified catalog key")
    _integer(state.get("sequence"), "sequence", minimum=1)
    _finite_number(state.get("lastSubmitAt"), "timestamp marker")
    if state["lastSubmitAt"] < 0:
        _invalid("shoot timestamp marker is invalid")
    _integer(state.get("h3Duration"), "h3 duration", minimum=1)
    if state.get("queueSchemaVersion") != 2:
        _invalid("shoot queue schema is unsupported")
    return dict(value)


def _manifest_payload(project_id: str, name: str, roots: list[str], timestamp: str) -> dict[str, Any]:
    return {
        "schema": MANIFEST_SCHEMA,
        "project_id": project_id,
        "name": name,
        "revision": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
        "approved_roots": list(roots),
    }


# --- Lane p12-med item 1 (BCK-002, MED-017; Project charter decision 1) -----------------------------------
# The manifest stores every approved folder inside the project folder RELATIVE to it ("." is the project folder,
# "sound" its sound folder, posix separators), so a moved or copied folder is complete (Resolve/Premiere/FCP keep
# library-relative paths; Resolve "Change Source Folder" relinks by the original paths). Readers always receive
# absolute folders: a relative root is resolved against the folder the manifest was read from. A folder outside
# the project stays absolute. A relative root can never leave the project folder ("..", a drive, a UNC name).


def _root_from_disk(root: Any, directory: Any) -> Any:
    """The absolute folder of one stored root; anything that is not a relative root is returned unchanged."""
    if not isinstance(root, str) or not root or directory is None:
        return root
    text = root.replace("\\", "/")
    if os.path.isabs(root) or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return root
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        _invalid("manifest approved roots are invalid")
    return os.path.normpath(os.path.join(str(directory), *parts)) if parts else os.path.normpath(str(directory))


def _is_absolute_root(root: str) -> bool:
    text = root.replace("\\", "/")
    return bool(os.path.isabs(root) or text.startswith("/") or re.match(r"^[A-Za-z]:", text))


def _roots_from_disk(roots: Any, directory: Any) -> Any:
    if not isinstance(roots, list) or directory is None:
        return roots
    return [_root_from_disk(root, directory) for root in roots]


def _root_to_disk(root: Any, directory: Path) -> Any:
    """The stored form of one absolute root: relative (posix) when it is the project folder or inside it."""
    if not isinstance(root, str) or not root or not os.path.isabs(root):
        return root
    try:
        base_real, target_real = os.path.realpath(str(directory)), os.path.realpath(root)
    except OSError:  # lane p12-medb: an unreachable network folder is outside the project; stored as it is
        return root
    base, target = os.path.normcase(base_real), os.path.normcase(target_real)
    try:
        if os.path.commonpath([base, target]) != base:
            return root
    except ValueError:  # another drive
        return root
    relative = os.path.relpath(target_real, base_real)
    return "." if relative in (".", "") else Path(relative).as_posix()


def _manifest_to_disk(manifest: Mapping[str, Any], directory: Path) -> dict[str, Any]:
    stored = dict(manifest)
    if isinstance(stored.get("approved_roots"), list):
        stored["approved_roots"] = [_root_to_disk(root, directory) for root in stored["approved_roots"]]
    return stored


# Lane w27-rows item 1 (P1 VID-154 rest; the p12-med rule for approved_roots applied to the song): the shot's
# musicTrack inside the project folder is stored RELATIVE to it (posix, "sound/harbour-pad.mp3"), so a moved or
# renamed project folder keeps its song (Resolve / Premiere / FCP keep library-relative media paths). Readers always
# receive the absolute path, resolved against the folder shoot.json was read from; a track outside the project stays
# absolute. An absolute track inside the project (stored before this lane) reads as it is and is stored relative by
# the next write.
_STATE_RELATIVE_KEYS = ("musicTrack",)


def _state_path_to_disk(value: Any, directory: Path) -> Any:
    if not isinstance(value, str) or not value or not _is_absolute_root(value):
        return value
    stored = _root_to_disk(value, directory)
    return value if stored == "." else stored


def _state_path_from_disk(value: Any, directory: Path) -> Any:
    if not isinstance(value, str) or not value or _is_absolute_root(value):
        return value
    parts = [part for part in value.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return value
    return os.path.normpath(os.path.join(str(directory), *parts))


# Lane w29-fix2 item 1 (VID-166): the files a catalog draft names (media slots, the Sound track, voice/music files,
# clip sound, the Continue source frame) are stored relative to the project folder like musicTrack.
def _catalog_drafts_paths(value: Any, convert: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = {}
    for identity, draft in value.items():
        if not isinstance(draft, dict):
            out[identity] = draft
            continue
        draft = dict(draft)
        media = draft.get("media")
        if isinstance(media, dict):
            draft["media"] = {key: [convert(item) for item in items] if isinstance(items, list) else items for key, items in media.items()}
        sound = draft.get("sound")
        if isinstance(sound, dict):
            sound = dict(sound)
            if isinstance(sound.get("audios"), list):
                sound["audios"] = [{**item, "source": convert(item.get("source"))} if isinstance(item, dict) else item for item in sound["audios"]]
            if isinstance(sound.get("clipSound"), list):
                sound["clipSound"] = [convert(item) for item in sound["clipSound"]]
            if isinstance(sound.get("track"), dict):
                sound["track"] = {**sound["track"], "source": convert(sound["track"].get("source"))}
            draft["sound"] = sound
        if isinstance(draft.get("continueOut"), dict):
            draft["continueOut"] = {**draft["continueOut"], "path": convert(draft["continueOut"].get("path"))}
        out[identity] = draft
    return out


def _state_envelope_to_disk(envelope: Mapping[str, Any], directory: Path) -> dict[str, Any]:
    stored = dict(envelope)
    if isinstance(stored.get("state"), dict):
        state = dict(stored["state"])
        for key in _STATE_RELATIVE_KEYS:
            if key in state:
                state[key] = _state_path_to_disk(state[key], directory)
        if "catalogDrafts" in state:
            state["catalogDrafts"] = _catalog_drafts_paths(state["catalogDrafts"], lambda item: _state_path_to_disk(item, directory))
        stored["state"] = state
    return stored


def _state_envelope_from_disk(envelope: Any, directory: Path) -> Any:
    if not isinstance(envelope, dict) or not isinstance(envelope.get("state"), dict):
        return envelope
    state = dict(envelope["state"])
    for key in _STATE_RELATIVE_KEYS:
        if key in state:
            state[key] = _state_path_from_disk(state[key], directory)
    if "catalogDrafts" in state:
        state["catalogDrafts"] = _catalog_drafts_paths(state["catalogDrafts"], lambda item: _state_path_from_disk(item, directory))
    return {**envelope, "state": state}


def _path_strings(value: Any, *, path: bool = False) -> list[str]:
    """Every path text in a shoot state (the keys _rebase_path_fields re-bases)."""
    if isinstance(value, dict):
        return [text for key, child in value.items() for text in _path_strings(child, path=path or key in _PATH_TEXT_KEYS)]
    if isinstance(value, list):
        return [text for child in value for text in _path_strings(child, path=path)]
    return [value] if path and isinstance(value, str) and value else []


def _infer_previous_directory(project_dir: Path, stored_roots: list[Any], evidence: Iterable[Any] = ()) -> str | None:
    """The folder a project lived in before, read from the absolute folders it stored that no longer exist.

    For each such folder the longest tail of it that exists inside the project folder now names a candidate old
    project folder (``<old>/sound`` with ``<new>/sound`` present -> ``<old>``; ``<old>`` itself -> ``<old>``).
    Lane p12-medb: a candidate counts only when a SECOND, independent path corroborates that it was this project's
    folder: another stored folder or state path that is the candidate itself or lies under it with its tail present
    in the project folder now. An approved folder on an unplugged drive (``E:/Footage``, ``E:/Footage/sound``) has no
    such witness and is never taken for the project's previous place (Resolve relinks only what the user points at
    [RV-RL]). The most common corroborated answer wins; None when there is none.
    """
    witnesses = [os.path.normpath(text) for text in [*stored_roots, *evidence]
                 if isinstance(text, str) and text and os.path.isabs(text) and _local_path.remote_path_reason(text) is None]

    def corroborated(old: str, source: str) -> bool:
        key, source_key = os.path.normcase(old), os.path.normcase(source)
        prefix = key.rstrip("\\/") + os.sep
        for witness in witnesses:
            wkey = os.path.normcase(witness)
            if wkey == source_key:
                continue
            if wkey == key:
                return True
            if wkey.startswith(prefix) and os.path.exists(os.path.join(str(project_dir), witness[len(prefix):])):
                return True
        return False

    votes: dict[str, int] = {}
    order: list[str] = []
    for root in stored_roots:
        if not isinstance(root, str) or not os.path.isabs(root) or _local_path.remote_path_reason(root) is not None:
            continue  # lane w34-sec: a network / device folder is never probed
        if os.path.isdir(root):
            continue
        norm = os.path.normpath(root)
        parts = Path(norm).parts
        for cut in range(1, len(parts) + 1):  # cut = how many leading parts stay in the old prefix
            tail = parts[cut:]
            if os.path.isdir(os.path.join(str(project_dir), *tail)) if tail else True:
                old = os.path.join(*parts[:cut]) if cut > 1 else parts[0]
                if (tail or cut == len(parts)) and corroborated(old, norm):
                    key = os.path.normcase(old)
                    if key not in votes:
                        order.append(old)
                    votes[key] = votes.get(key, 0) + 1
                break
    if not order:
        return None
    best = max(order, key=lambda o: (votes[os.path.normcase(o)], -order.index(o)))
    return None if _same_path(best, str(project_dir)) else best


def _state_payload(project_id: str, state: dict[str, Any], timestamp: str, revision: int = 1) -> dict[str, Any]:
    return {
        "schema": STATE_SCHEMA,
        "project_id": project_id,
        "revision": revision,
        "updated_at": timestamp,
        "state": state,
    }


_MUSIC_CONTRACTS: Any | None = None
_MUSIC_CONTRACTS_LOCK = threading.RLock()
_MUSIC_CONTRACTS_NAME = "film_suite_music_contracts"


def _music_contracts() -> Any:
    global _MUSIC_CONTRACTS
    if _MUSIC_CONTRACTS is not None:
        return _MUSIC_CONTRACTS
    with _MUSIC_CONTRACTS_LOCK:
        if _MUSIC_CONTRACTS is not None:
            return _MUSIC_CONTRACTS
        contracts_path = Path(__file__).resolve().parent.parent / "music_core" / "contracts.py"
        previous = sys.modules.get(_MUSIC_CONTRACTS_NAME)
        spec = importlib.util.spec_from_file_location(_MUSIC_CONTRACTS_NAME, contracts_path)
        if spec is None or spec.loader is None:
            raise ProjectError("music_state_unavailable", "music contracts are unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MUSIC_CONTRACTS_NAME] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(_MUSIC_CONTRACTS_NAME, None)
            if previous is not None:
                sys.modules[_MUSIC_CONTRACTS_NAME] = previous
            raise
        _MUSIC_CONTRACTS = module
        return module


def _music_invalid() -> None:
    raise ProjectError("invalid_music_interpretation", "music interpretation is invalid")


def _music_unavailable() -> None:
    raise ProjectError("music_state_unavailable", "music state is unavailable")


def _music_conflict() -> None:
    raise ProjectError("music_conflict", "music interpretation revision conflict")


def _music_track(value: Any) -> str:
    if not isinstance(value, str) or _MUSIC_SHA256_RE.fullmatch(value) is None:
        raise ProjectError("invalid_music_request", "music track id is invalid")
    return value


def _music_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProjectError("invalid_music_request", "music revision is invalid")
    return value


def _music_body(value: Any) -> dict[str, Any]:
    try:
        return _music_contracts().validate_interpretation_body(value)
    except Exception:
        _music_invalid()
    raise AssertionError("unreachable")


def _music_build(
    *,
    track_sha256: str,
    project_id: str,
    revision: int,
    value: Any,
) -> tuple[dict[str, Any], bytes]:
    try:
        contracts = _music_contracts()
        body = contracts.validate_interpretation_body(value)
        interpretation = contracts.build_interpretation(
            track_sha256=track_sha256,
            scope={"kind": "project", "project_id": project_id},
            revision=revision,
            updated_at=_now_timestamp(),
            value=body,
        )
        interpretation = contracts.validate_interpretation(interpretation)
        payload = contracts.canonical_json_bytes(interpretation)
    except ProjectError:
        raise
    except Exception:
        _music_invalid()
    if len(payload) > MUSIC_INTERPRETATION_MAX_BYTES:
        _music_invalid()
    return interpretation, payload


def _music_result(interpretation: dict[str, Any] | None) -> dict[str, Any]:
    if interpretation is None:
        return {"exists": False, "revision": 0, "interpretation": None}
    return {
        "exists": True,
        "revision": interpretation["revision"],
        "interpretation": interpretation,
    }


def _write_fully(handle: Any, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = handle.write(remaining)
        if written is None:
            written = len(remaining)
        if not written:
            raise OSError("short project write")
        remaining = remaining[written:]
    handle.flush()
    _flush_private_file(handle.fileno())


def _cleanup_staged_temp_path(
    path: Path,
    *,
    expected: Any,
    max_bytes: int,
) -> bool:
    """Clean one staged path through the exact handle-boundary primitive."""

    if expected is None or _TEMP_RE.fullmatch(path.name) is None:
        return False
    parent_handle: Any = None
    try:
        parent_handle, parent_expected = _open_existing(path.parent, directory=True)
        return unlink_handle_bound(
            path,
            expected,
            parent_expected=parent_expected,
            max_bytes=max_bytes,
            allow_size_change=True,
        )
    except (OSError, ProjectError):
        return False
    finally:
        if parent_handle is not None:
            parent_handle.close()


def _stage_temp(directory: Path, payload: bytes) -> Path:
    name = ".studio-" + secrets.token_hex(16) + ".tmp"
    path = directory / name
    fd: int | None = None
    created_identity: Any = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
        if os.name == "nt":
            import msvcrt

            created_identity = _identity_from_handle(
                # The wrapper is non-owning here; the C runtime fd remains
                # responsible for closing the descriptor in the finally block.
                type("_FdHandle", (), {"raw": msvcrt.get_osfhandle(fd)})(),
                path,
            )
        else:
            created_identity = _identity_from_handle(
                type("_FdHandle", (), {"raw": fd})(), path
            )
        with os.fdopen(fd, "wb", buffering=0) as handle:
            fd = None
            _write_fully(handle, payload)
        if os.name != "nt":
            os.chmod(path, 0o600)
        return path
    except FileExistsError:
        raise ProjectError("project_io_failed", "project temp collision")
    except ProjectError:
        _cleanup_staged_temp_path(
            path,
            expected=created_identity,
            max_bytes=STATE_MAX_BYTES,
        )
        raise
    except (OSError, UnicodeError):
        _cleanup_staged_temp_path(
            path,
            expected=created_identity,
            max_bytes=STATE_MAX_BYTES,
        )
        raise ProjectError("project_io_failed", "project temp could not be written")
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)


def _replace_temp(temp: Path, target: Path, *, no_replace: bool) -> None:
    if os.name == "nt":
        from . import path_boundary as _path_boundary

        move = _path_boundary._MoveFileExW
        flags = _path_boundary._MOVEFILE_WRITE_THROUGH
        if not no_replace:
            flags |= 1  # MOVEFILE_REPLACE_EXISTING
        if not move(str(temp), str(target), flags):
            error = ctypes.get_last_error() or errno.EIO
            if no_replace and error in {
                getattr(_path_boundary, "_ERROR_FILE_EXISTS", 80),
                getattr(_path_boundary, "_ERROR_ALREADY_EXISTS", 183),
            }:
                raise FileExistsError(error, "project target already exists", str(target))
            raise OSError(error, "project atomic publication failed")
        return
    if no_replace:
        os.link(temp, target)
        # The hard link is the POSIX publication boundary.  The temp remains
        # caller-owned and is cleaned through tombstone exchange later.
    else:
        os.replace(temp, target)


def _members(directory: Path) -> set[str]:
    try:
        # Tombstones are private crash residue, not project semantics.  Keep
        # ordinary .studio-*.tmp entries visible to the transactional checks;
        # only the dedicated tombstone namespace is ignored here.
        return {
            entry.name
            for entry in directory.iterdir()
            if not _TOMBSTONE_RE.fullmatch(entry.name)
        }
    except OSError:
        raise ProjectError("project_unavailable", "project directory is unavailable")


def _is_reparse_point(info: os.stat_result) -> bool:
    return bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


@dataclass(frozen=True)
class _StagedTempIdentity:
    """Native lstat identity captured before boundary pinning."""

    device: int
    inode: int
    size: int
    mode: int
    file_attributes: int

    def matches(self, info: os.stat_result) -> bool:
        return (
            stat.S_ISREG(info.st_mode)
            and not _is_reparse_point(info)
            and int(info.st_dev) == self.device
            and int(getattr(info, "st_ino", 0)) == self.inode
            and int(info.st_mode) == self.mode
        )


def _capture_staged_temp_identity(path: Path) -> _StagedTempIdentity | None:
    """Capture a temp's native identity without using the path boundary pin."""

    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info):
        return None
    return _StagedTempIdentity(
        device=int(info.st_dev),
        inode=int(getattr(info, "st_ino", 0)),
        size=int(info.st_size),
        mode=int(info.st_mode),
        file_attributes=int(getattr(info, "st_file_attributes", 0)),
    )


def _sweep_temps(boundary: PathBoundary, directory: Path) -> None:
    cutoff = time.time() - 24 * 60 * 60
    directories = [directory]
    state_dir = directory / "state"
    try:
        state_info = state_dir.lstat()
    except FileNotFoundError:
        state_info = None
    except OSError as exc:
        raise ProjectError("project_io_failed", "project temp sweep failed") from exc
    if state_info is not None and stat.S_ISDIR(state_info.st_mode) and not _is_reparse_point(state_info):
        directories.append(state_dir)
    for parent in directories:
        try:
            entries = tuple(parent.iterdir())
        except OSError as exc:
            raise ProjectError("project_io_failed", "project temp sweep failed") from exc
        for entry in entries:
            if not _TEMP_RE.fullmatch(entry.name):
                continue
            try:
                info = entry.lstat()
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ProjectError("project_io_failed", "project temp sweep failed") from exc
            if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info):
                continue
            if info.st_mtime >= cutoff:
                continue
            try:
                expected_native = _StagedTempIdentity(
                    device=int(info.st_dev),
                    inode=int(getattr(info, "st_ino", 0)),
                    size=int(info.st_size),
                    mode=int(info.st_mode),
                    file_attributes=int(getattr(info, "st_file_attributes", 0)),
                )
                candidate_handle, expected = _open_existing(entry, directory=False)
                candidate_handle.close()
                if not expected_native.matches(entry.lstat()):
                    continue
                _unlink_owned_file(
                    boundary,
                    entry,
                    expected=expected,
                    max_bytes=STATE_MAX_BYTES,
                )
            except FileNotFoundError:
                continue
            except (OSError, ProjectError) as exc:
                raise ProjectError("project_io_failed", "project temp sweep failed") from exc


def _sweep_music_temps(boundary: PathBoundary, directory: Path) -> None:
    """Remove only old, regular, owned-name temporary files in music state."""

    cutoff = time.time() - 24 * 60 * 60
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        raise ProjectError("music_state_unavailable", "music state is unavailable") from exc
    for entry in entries:
        if _TEMP_RE.fullmatch(entry.name) is None:
            continue
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProjectError("music_state_unavailable", "music state is unavailable") from exc
        if not stat.S_ISREG(info.st_mode) or _is_reparse_point(info) or info.st_mtime >= cutoff:
            continue
        expected_native = _StagedTempIdentity(
            device=int(info.st_dev),
            inode=int(getattr(info, "st_ino", 0)),
            size=int(info.st_size),
            mode=int(info.st_mode),
            file_attributes=int(getattr(info, "st_file_attributes", 0)),
        )
        try:
            candidate_handle, expected = _open_existing(entry, directory=False)
            candidate_handle.close()
            if not expected_native.matches(entry.lstat()):
                continue
            _unlink_owned_file(
                boundary,
                entry,
                expected=expected,
                max_bytes=MUSIC_INTERPRETATION_MAX_BYTES,
            )
        except FileNotFoundError:
            continue
        except (OSError, ProjectError) as exc:
            raise ProjectError("music_state_unavailable", "music state is unavailable") from exc


def _restore_existing_bytes(
    boundary: PathBoundary,
    directory: Path,
    target: Path,
    payload: bytes,
    *,
    max_bytes: int,
) -> None:
    """Restore a previous file with the same atomic primitive as a normal write."""

    temp = _stage_temp(directory, payload)
    temp_cleanup_identity = _capture_staged_temp_identity(temp)
    temp_identity: Any = None
    try:
        temp_identity = boundary.pin_file(temp)
        _replace_temp(temp, target, no_replace=False)
    finally:
        if temp_identity is None:
            _unlink_staged_temp_without_identity(
                boundary,
                directory,
                temp,
                expected_identity=temp_cleanup_identity,
                payload=payload,
                max_bytes=max_bytes,
            )
        else:
            _unlink_owned_file(
                boundary,
                temp,
                expected=temp_identity,
                max_bytes=max_bytes,
            )


def _restore_owned_target(
    boundary: PathBoundary,
    directory: Path,
    target: Path,
    payload: bytes,
    *,
    expected_target: Any,
    expected_hash: str,
    max_bytes: int,
) -> bool:
    """Restore only a target that is still our published object and bytes."""

    try:
        current_target = boundary.pin_file(target)
        if not expected_target.same_object(current_target):
            return False
        current_payload = _read_regular(boundary, target, max_bytes=max_bytes)
        if hashlib.sha256(current_payload).hexdigest() != expected_hash:
            return False
        _restore_existing_bytes(
            boundary,
            directory,
            target,
            payload,
            max_bytes=max_bytes,
        )
        return True
    except (OSError, ProjectError):
        return False


def _unlink_owned_file(
    boundary: PathBoundary,
    path: Path,
    *,
    expected: Any = None,
    payload: bytes | None = None,
    max_bytes: int = STATE_MAX_BYTES,
) -> bool:
    """Remove a regular file only when its identity and optional bytes match."""

    try:
        if expected is None:
            return False
        current = boundary.pin_file(path)
        if not expected.same_object(current) or expected.size > max_bytes:
            return False
        parent_handle, parent_expected = _open_existing(path.parent, directory=True)
        parent_handle.close()
        expected_sha256 = hashlib.sha256(payload).hexdigest() if payload is not None else None
        return unlink_handle_bound(
            path,
            expected,
            parent_expected=parent_expected,
            expected_sha256=expected_sha256,
            max_bytes=max_bytes,
        )
    except (OSError, ProjectError):
        return False


def _unlink_staged_temp_without_identity(
    boundary: PathBoundary,
    directory: Path,
    path: Path,
    *,
    expected_identity: _StagedTempIdentity | None,
    payload: bytes,
    max_bytes: int,
) -> bool:
    """Clean a temp through the exact-delete primitive after native pre-pin checks."""

    if (
        expected_identity is None
        or path.parent != directory
        or _TEMP_RE.fullmatch(path.name) is None
    ):
        return False
    try:
        info = path.lstat()
        if not expected_identity.matches(info):
            return False
        current_handle, current_identity = _open_existing(path, directory=False)
        current_handle.close()
        if current_identity.size > max_bytes:
            return False
        if not expected_identity.matches(path.lstat()):
            return False
        parent_handle, parent_expected = _open_existing(directory, directory=True)
        parent_handle.close()
        return unlink_handle_bound(
            path,
            current_identity,
            parent_expected=parent_expected,
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            max_bytes=max_bytes,
        )
    except (OSError, ProjectError):
        return False


class ProjectStore:
    """Crash-safe project manifest and shoot-state store."""

    def __init__(
        self,
        hermes_home: str | Path | None = None,
        *,
        boundary: PathBoundary | None = None,
    ) -> None:
        self.boundary = boundary or PathBoundary(hermes_home=hermes_home)

    def read_project(
        self,
        directory: str | Path,
        project_id: str | None = None,
        expected_project_id: str | None = None,
        expected_id: str | None = None,
    ) -> dict[str, Any]:
        return read_project(
            directory,
            project_id=project_id,
            expected_project_id=expected_project_id if expected_project_id is not None else expected_id,
            boundary=self.boundary,
        )

    load_project = read_project
    open_project = read_project

    def _directory(self, directory: str | Path, *, create: bool = False) -> Path:
        return self.boundary.validate_project_directory(
            directory, operation="create" if create else "open"
        )

    def _manifest_locked(self, directory: Path, *, strict: bool = False) -> dict[str, Any]:
        value = _loads_bounded(_read_regular(self.boundary, directory / "project.json", max_bytes=MANIFEST_MAX_BYTES))
        manifest = _validate_manifest_v20(self.boundary, value, directory)
        if strict:
            # Lane p13-prjb finding 1 (cold review p13-prj Important 1): a write that saves project.json refuses while
            # an approved folder is offline, so the stored approval is never re-spelled, re-based or dropped (Resolve
            # changes nothing about an offline volume's media until it is back). The message names the folder as stored.
            gone = set(offline_roots(manifest["approved_roots"]))
            for stored, read in zip(value["approved_roots"], manifest["approved_roots"]):
                if read in gone:
                    raise ProjectError(PROJECT_FOLDER_OFFLINE, project_folder_offline_message(stored))
        return manifest

    def _state_locked(self, directory: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
        state_dir = directory / "state"
        if not state_dir.exists():
            return None
        if state_dir.is_symlink() or not state_dir.is_dir():
            _invalid("project state directory is invalid")
        state_path = state_dir / "shoot.json"
        if not state_path.exists():
            extras = {name for name in _members(state_dir) if not _TEMP_RE.fullmatch(name)}
            if extras:
                _invalid("project state is missing")
            return None
        value = _state_envelope_from_disk(_loads_bounded(_read_regular(self.boundary, state_path, max_bytes=STATE_MAX_BYTES)), directory)
        return _validate_state_v20(
            self.boundary,
            manifest["project_id"],
            manifest["approved_roots"],
            value,
            allow_roots_mismatch=True,
        )

    def _ensure_state_dir(self, directory: Path) -> Path:
        state_dir = directory / "state"
        if not state_dir.exists():
            try:
                state_dir.mkdir()
            except FileExistsError:
                pass
            except OSError:
                raise ProjectError("project_io_failed", "state directory could not be created")
        if state_dir.is_symlink() or not state_dir.is_dir():
            _invalid("project state directory is invalid")
        if os.name != "nt":
            os.chmod(state_dir, 0o700)
        return state_dir

    def _music_directory_locked(
        self,
        directory: Path,
    ) -> tuple[Path, Path, Any, Any] | None:
        state_dir = directory / "state"
        if not os.path.lexists(state_dir):
            return None
        if state_dir.is_symlink() or not state_dir.is_dir():
            _music_invalid()
        try:
            state_identity = self.boundary.pin_directory(state_dir)
        except ProjectError:
            _music_unavailable()
        music_dir = state_dir / "music"
        if not os.path.lexists(music_dir):
            return None
        if music_dir.is_symlink() or not music_dir.is_dir():
            _music_invalid()
        try:
            music_identity = self.boundary.pin_directory(music_dir)
        except ProjectError:
            _music_unavailable()
        return state_dir, music_dir, state_identity, music_identity

    def _ensure_music_state_dir(self, directory: Path) -> Path:
        """Create and pin the project-owned music directory under state."""

        state_dir = self._ensure_state_dir(directory)
        try:
            state_identity = self.boundary.pin_directory(state_dir)
        except ProjectError:
            _music_unavailable()
        music_dir = state_dir / "music"
        if not os.path.lexists(music_dir):
            try:
                music_dir.mkdir()
            except FileExistsError:
                pass
            except OSError:
                _music_unavailable()
        if music_dir.is_symlink() or not music_dir.is_dir():
            _music_invalid()
        try:
            music_identity = self.boundary.pin_directory(music_dir)
            self.boundary.revalidate_directory(state_dir, state_identity)
            self.boundary.revalidate_directory(music_dir, music_identity)
        except ProjectError:
            _music_unavailable()
        if os.name != "nt":
            os.chmod(music_dir, 0o700)
        return music_dir

    def _read_music_locked(
        self,
        directory: Path,
        *,
        project_id: str,
        track_sha256: str,
        sweep: bool,
    ) -> dict[str, Any] | None:
        locations = self._music_directory_locked(directory)
        if locations is None:
            return None
        state_dir, music_dir, state_identity, music_identity = locations
        if sweep:
            _sweep_music_temps(self.boundary, music_dir)
            try:
                self.boundary.revalidate_directory(state_dir, state_identity)
                self.boundary.revalidate_directory(music_dir, music_identity)
            except ProjectError:
                _music_unavailable()
        target = music_dir / f"{track_sha256}.json"
        if not os.path.lexists(target):
            return None
        try:
            raw = _read_regular(
                self.boundary,
                target,
                max_bytes=MUSIC_INTERPRETATION_MAX_BYTES,
            )
            value = _loads_bounded(raw)
            contracts = _music_contracts()
            interpretation = contracts.validate_interpretation(value)
            canonical = contracts.canonical_json_bytes(interpretation)
        except ProjectError:
            _music_unavailable()
        except Exception:
            _music_invalid()
        if interpretation["track_sha256"] != track_sha256:
            raise ProjectError("music_scope_mismatch", "music interpretation belongs to another context")
        if interpretation["scope"] != {"kind": "project", "project_id": project_id}:
            raise ProjectError("music_scope_mismatch", "music interpretation belongs to another context")
        if canonical != raw:
            _music_invalid()
        return interpretation

    def _create_state_dir(self, directory: Path) -> Path:
        state_dir = directory / "state"
        try:
            state_dir.mkdir()
        except FileExistsError as exc:
            raise ProjectError("project_io_failed", "project state directory changed during create") from exc
        except OSError as exc:
            raise ProjectError("project_io_failed", "state directory could not be created") from exc
        if state_dir.is_symlink() or not state_dir.is_dir():
            _invalid("project state directory is invalid")
        if os.name != "nt":
            os.chmod(state_dir, 0o700)
        return state_dir

    def _atomic_replace_file(
        self,
        directory: Path,
        target: Path,
        payload: bytes,
        *,
        max_bytes: int,
        expected_directory_identity: Any | None = None,
    ) -> None:
        parent_identity = (
            expected_directory_identity
            if expected_directory_identity is not None
            else self.boundary.pin_directory(directory)
        )
        target_identity = self.boundary.pin_file(target)
        old = _read_regular(self.boundary, target, max_bytes=max_bytes)
        temp = _stage_temp(directory, payload)
        temp_cleanup_identity = _capture_staged_temp_identity(temp)
        temp_identity: Any = None
        expected_payload_hash = hashlib.sha256(payload).hexdigest()
        published = False
        replacement_attempted = False
        published_target_identity: Any = None
        try:
            temp_identity = self.boundary.pin_file(temp)
            self.boundary.revalidate_directory(directory, parent_identity)
            self.boundary.revalidate_file(target, target_identity)
            replacement_attempted = True
            _replace_temp(temp, target, no_replace=False)
            published = True
            # Replace publishes the exact inode/object already pinned as the
            # temp. Capture ownership before any post-publication probe can
            # fail, then prove the target still names that object and bytes.
            published_target_identity = temp_identity
            observed_target_identity = self.boundary.pin_file(target)
            if not temp_identity.same_object(observed_target_identity):
                raise ProjectError("project_unavailable", "published target identity changed")
            observed_payload = _read_regular(self.boundary, target, max_bytes=max_bytes)
            if hashlib.sha256(observed_payload).hexdigest() != expected_payload_hash:
                raise ProjectError("project_unavailable", "published target bytes changed")
            self.boundary.revalidate_file(target, published_target_identity)
            _fsync_parent(directory)
        except ProjectError:
            if published or replacement_attempted:
                with contextlib.suppress(Exception):
                    if published_target_identity is not None:
                        _restore_owned_target(
                            self.boundary,
                            directory,
                            target,
                            old,
                            expected_target=published_target_identity,
                            expected_hash=expected_payload_hash,
                            max_bytes=max_bytes,
                        )
                        with contextlib.suppress(Exception):
                            _fsync_parent(directory)
            raise
        except OSError as exc:
            if published or replacement_attempted:
                with contextlib.suppress(Exception):
                    if published_target_identity is not None:
                        _restore_owned_target(
                            self.boundary,
                            directory,
                            target,
                            old,
                            expected_target=published_target_identity,
                            expected_hash=expected_payload_hash,
                            max_bytes=max_bytes,
                        )
                        with contextlib.suppress(Exception):
                            _fsync_parent(directory)
            raise ProjectError("project_io_failed", "project file could not be published") from exc
        finally:
            cleaned = _unlink_owned_file(
                self.boundary,
                temp,
                expected=temp_identity,
                max_bytes=max_bytes,
            )
            if not cleaned and temp_identity is None:
                _unlink_staged_temp_without_identity(
                    self.boundary,
                    directory,
                    temp,
                    expected_identity=temp_cleanup_identity,
                    payload=payload,
                    max_bytes=max_bytes,
                )

    def _atomic_create_file(
        self,
        project_dir: Path,
        directory: Path,
        target: Path,
        payload: bytes,
        *,
        project_identity: Any,
        directory_identity: Any,
    ) -> None:
        """Publish a new music object without replacing a foreign target."""

        temp = _stage_temp(directory, payload)
        temp_cleanup_identity = _capture_staged_temp_identity(temp)
        temp_identity: Any = None
        published_target_identity: Any = None
        published = False
        try:
            temp_identity = self.boundary.pin_file(temp)
            self.boundary.revalidate_directory(project_dir, project_identity)
            self.boundary.revalidate_directory(directory, directory_identity)
            _replace_temp(temp, target, no_replace=True)
            published = True
            published_target_identity = self.boundary.pin_file(target)
            if not temp_identity.same_object(published_target_identity):
                raise ProjectError("music_state_unavailable", "music state is unavailable")
            observed_payload = _read_regular(
                self.boundary,
                target,
                max_bytes=MUSIC_INTERPRETATION_MAX_BYTES,
            )
            if observed_payload != payload:
                raise ProjectError("music_state_unavailable", "music state is unavailable")
            self.boundary.revalidate_file(target, published_target_identity)
            self.boundary.revalidate_directory(project_dir, project_identity)
            self.boundary.revalidate_directory(directory, directory_identity)
            _fsync_parent(directory)
        except FileExistsError as exc:
            raise ProjectError("music_conflict", "music interpretation revision conflict") from exc
        except ProjectError:
            if published:
                expected = published_target_identity or temp_identity
                _unlink_owned_file(
                    self.boundary,
                    target,
                    expected=expected,
                    payload=payload,
                    max_bytes=MUSIC_INTERPRETATION_MAX_BYTES,
                )
            raise
        except OSError as exc:
            if published:
                expected = published_target_identity or temp_identity
                _unlink_owned_file(
                    self.boundary,
                    target,
                    expected=expected,
                    payload=payload,
                    max_bytes=MUSIC_INTERPRETATION_MAX_BYTES,
                )
            raise ProjectError("music_state_unavailable", "music state is unavailable") from exc
        finally:
            cleaned = _unlink_owned_file(
                self.boundary,
                temp,
                expected=temp_identity,
                max_bytes=MUSIC_INTERPRETATION_MAX_BYTES,
            )
            if not cleaned and temp_identity is None:
                _unlink_staged_temp_without_identity(
                    self.boundary,
                    directory,
                    temp,
                    expected_identity=temp_cleanup_identity,
                    payload=payload,
                    max_bytes=MUSIC_INTERPRETATION_MAX_BYTES,
                )

    def _create_default_state_locked(self, directory: Path, manifest: dict[str, Any]) -> dict[str, Any]:
        state_dir = self._ensure_state_dir(directory)
        state = _default_state(manifest["approved_roots"])
        envelope = _state_payload(manifest["project_id"], state, _now_timestamp())
        payload = _bounded_payload(envelope, max_bytes=STATE_MAX_BYTES, label="shoot state")
        project_identity = self.boundary.pin_directory(directory)
        state_identity = self.boundary.pin_directory(state_dir)
        temp = _stage_temp(state_dir, payload)
        temp_cleanup_identity = _capture_staged_temp_identity(temp)
        temp_identity: Any = None
        expected_payload_hash = hashlib.sha256(payload).hexdigest()
        try:
            temp_identity = self.boundary.pin_file(temp)
            self.boundary.revalidate_directory(directory, project_identity)
            self.boundary.revalidate_directory(state_dir, state_identity)
            members = _members(state_dir)
            if "shoot.json" in members or any(
                name != temp.name and not _TEMP_RE.fullmatch(name) for name in members
            ):
                _invalid("project state directory changed during recovery")
            self.boundary.revalidate_directory(directory, project_identity)
            self.boundary.revalidate_directory(state_dir, state_identity)
            members = _members(state_dir)
            if "shoot.json" in members or any(
                name != temp.name and not _TEMP_RE.fullmatch(name) for name in members
            ):
                _invalid("project state directory changed during recovery")
            _replace_temp(temp, state_dir / "shoot.json", no_replace=True)
            published_state_identity = self.boundary.pin_file(state_dir / "shoot.json")
            if not temp_identity.same_object(published_state_identity):
                raise ProjectError("project_unavailable", "published state identity changed")
            published_payload = _read_regular(
                self.boundary,
                state_dir / "shoot.json",
                max_bytes=STATE_MAX_BYTES,
            )
            if hashlib.sha256(published_payload).hexdigest() != expected_payload_hash:
                raise ProjectError("project_unavailable", "published state bytes changed")
            self.boundary.revalidate_file(state_dir / "shoot.json", published_state_identity)
            self.boundary.revalidate_directory(directory, project_identity)
            self.boundary.revalidate_directory(state_dir, state_identity)
            members = _members(state_dir)
            if "shoot.json" not in members or any(
                name != "shoot.json" and not _TEMP_RE.fullmatch(name) for name in members
            ):
                _invalid("project state directory changed during recovery")
            _fsync_parent(state_dir)
            _fsync_parent(directory)
        except FileExistsError as exc:
            raise ProjectError("project_unavailable", "project state changed during recovery") from exc
        except OSError as exc:
            raise ProjectError("project_io_failed", "project state could not be recovered") from exc
        finally:
            cleaned = _unlink_owned_file(
                self.boundary,
                temp,
                expected=temp_identity,
                max_bytes=STATE_MAX_BYTES,
            )
            if not cleaned and temp_identity is None:
                _unlink_staged_temp_without_identity(
                    self.boundary,
                    state_dir,
                    temp,
                    expected_identity=temp_cleanup_identity,
                    payload=payload,
                    max_bytes=STATE_MAX_BYTES,
                )
        return envelope

    def create_project(
        self,
        directory: str | Path,
        name: str,
        approved_roots: list[str] | None = None,
        copy_state: Mapping[str, Any] | None = None,
        *,
        allow_building_marker: bool = False,
    ) -> dict[str, Any]:
        project_dir = self._directory(directory, create=not allow_building_marker)
        clean_name = _name(name.strip() if isinstance(name, str) else name)
        roots = _normalize_roots(self.boundary, approved_roots if approved_roots is not None else [])
        project_identity = self.boundary.pin_directory(project_dir)
        lock = self.boundary.project_lock(project_dir)
        root_temp: Path | None = None
        state_temp: Path | None = None
        root_temp_cleanup_identity: _StagedTempIdentity | None = None
        state_temp_cleanup_identity: _StagedTempIdentity | None = None
        root_temp_identity: Any = None
        state_temp_identity: Any = None
        state_dir: Path | None = None
        root_published = False
        state_published = False
        manifest_payload: bytes | None = None
        state_payload_bytes: bytes | None = None
        published_manifest_identity: Any = None
        published_state_identity: Any = None
        try:
            with lock:
                members = _members(project_dir)
                marker_names: set[str] = set()
                if members == {".continuo-building"} and allow_building_marker:
                    marker_path = project_dir / ".continuo-building"
                    try:
                        marker = _loads_bounded(_read_regular(self.boundary, marker_path, max_bytes=4096))
                        if isinstance(marker, dict) and marker.get("sample") is True and type(marker.get("started_ns")) is int:
                            marker_names = {marker_path.name}
                    except (ProjectError, OSError, ValueError, TypeError, UnicodeError):
                        pass
                if members != marker_names:
                    raise ProjectError("project_not_empty", "project directory is not empty")
                self.boundary.revalidate_directory(project_dir, project_identity)
                state_dir = self._create_state_dir(project_dir)
                state_identity = self.boundary.pin_directory(state_dir)
                timestamp = _now_timestamp()
                project_id = "prj_" + secrets.token_hex(16)
                manifest = _manifest_payload(project_id, clean_name, roots, timestamp)
                state = _default_state(roots)
                if copy_state is not None:
                    if not isinstance(copy_state, Mapping):
                        _invalid("copy state is invalid")
                    candidate = dict(copy_state)
                    candidate["approvedRoots"] = list(roots)
                    candidate["jobs"] = []
                    candidate["pending"] = []
                    candidate["outgoing"] = None
                    candidate["heldQueue"] = []
                    candidate["lastSubmitAt"] = 0
                    state = _validate_state_v20(self.boundary, project_id, roots, _state_payload(project_id, candidate, timestamp))["state"]
                manifest_payload = _bounded_payload(_manifest_to_disk(manifest, project_dir), max_bytes=MANIFEST_MAX_BYTES, label="project manifest")
                state_payload_bytes = _bounded_payload(
                    _state_payload(project_id, state, timestamp),
                    max_bytes=STATE_MAX_BYTES,
                    label="shoot state",
                )
                root_temp = _stage_temp(project_dir, manifest_payload)
                root_temp_cleanup_identity = _capture_staged_temp_identity(root_temp)
                root_temp_identity = self.boundary.pin_file(root_temp)
                state_temp = _stage_temp(state_dir, state_payload_bytes)
                state_temp_cleanup_identity = _capture_staged_temp_identity(state_temp)
                state_temp_identity = self.boundary.pin_file(state_temp)
                self.boundary.revalidate_directory(project_dir, project_identity)
                self.boundary.revalidate_directory(state_dir, state_identity)
                if _members(project_dir) != {root_temp.name, "state"} | marker_names or _members(state_dir) != {state_temp.name}:
                    _invalid("project directory changed during create")
                _replace_temp(root_temp, project_dir / "project.json", no_replace=True)
                root_published = True
                observed_manifest_identity = self.boundary.pin_file(project_dir / "project.json")
                if not root_temp_identity.same_object(observed_manifest_identity):
                    raise ProjectError("project_unavailable", "published manifest identity changed")
                observed_manifest_payload = _read_regular(
                    self.boundary,
                    project_dir / "project.json",
                    max_bytes=MANIFEST_MAX_BYTES,
                )
                if hashlib.sha256(observed_manifest_payload).hexdigest() != hashlib.sha256(manifest_payload).hexdigest():
                    raise ProjectError("project_unavailable", "published manifest bytes changed")
                self.boundary.revalidate_file(project_dir / "project.json", observed_manifest_identity)
                published_manifest_identity = observed_manifest_identity
                self.boundary.revalidate_directory(project_dir, project_identity)
                self.boundary.revalidate_directory(state_dir, state_identity)
                project_members = _members(project_dir)
                if (
                    not {"project.json", "state"}.issubset(project_members)
                    or project_members - ({"project.json", "state", root_temp.name} | marker_names)
                    or _members(state_dir) != {state_temp.name}
                ):
                    _invalid("project directory changed during create")
                self.boundary.revalidate_directory(project_dir, project_identity)
                self.boundary.revalidate_directory(state_dir, state_identity)
                project_members = _members(project_dir)
                if (
                    not {"project.json", "state"}.issubset(project_members)
                    or project_members - ({"project.json", "state", root_temp.name} | marker_names)
                    or _members(state_dir) != {state_temp.name}
                ):
                    _invalid("project directory changed during create")
                _replace_temp(state_temp, state_dir / "shoot.json", no_replace=True)
                state_published = True
                observed_state_identity = self.boundary.pin_file(state_dir / "shoot.json")
                if not state_temp_identity.same_object(observed_state_identity):
                    raise ProjectError("project_unavailable", "published state identity changed")
                observed_state_payload = _read_regular(
                    self.boundary,
                    state_dir / "shoot.json",
                    max_bytes=STATE_MAX_BYTES,
                )
                if hashlib.sha256(observed_state_payload).hexdigest() != hashlib.sha256(state_payload_bytes).hexdigest():
                    raise ProjectError("project_unavailable", "published state bytes changed")
                self.boundary.revalidate_file(state_dir / "shoot.json", observed_state_identity)
                published_state_identity = observed_state_identity
                self.boundary.revalidate_directory(project_dir, project_identity)
                self.boundary.revalidate_directory(state_dir, state_identity)
                project_members = _members(project_dir)
                state_members = _members(state_dir)
                if (
                    not {"project.json", "state"}.issubset(project_members)
                    or project_members - ({"project.json", "state", root_temp.name} | marker_names)
                    or "shoot.json" not in state_members
                    or state_members - {"shoot.json", state_temp.name}
                ):
                    _invalid("project state directory changed during create")
                _fsync_parent(state_dir)
                _fsync_parent(project_dir)
                return {
                    "project": {"project_id": project_id, "name": clean_name, "revision": 1},
                    "shoot": {"revision": 1, "state": state},
                }
        except ProjectError:
            raise
        except (OSError, ValueError):
            raise ProjectError("project_io_failed", "project could not be created")
        finally:
            if root_temp is not None:
                cleaned = _unlink_owned_file(
                    self.boundary,
                    root_temp,
                    expected=root_temp_identity,
                    max_bytes=MANIFEST_MAX_BYTES,
                )
                if not cleaned and root_temp_identity is None and manifest_payload is not None:
                    _unlink_staged_temp_without_identity(
                        self.boundary,
                        project_dir,
                        root_temp,
                        expected_identity=root_temp_cleanup_identity,
                        payload=manifest_payload,
                        max_bytes=MANIFEST_MAX_BYTES,
                    )
            if state_temp is not None:
                cleaned = _unlink_owned_file(
                    self.boundary,
                    state_temp,
                    expected=state_temp_identity,
                    max_bytes=STATE_MAX_BYTES,
                )
                if not cleaned and state_temp_identity is None and state_payload_bytes is not None and state_dir is not None:
                    _unlink_staged_temp_without_identity(
                        self.boundary,
                        state_dir,
                        state_temp,
                        expected_identity=state_temp_cleanup_identity,
                        payload=state_payload_bytes,
                        max_bytes=STATE_MAX_BYTES,
                    )
            if root_published and not state_published and manifest_payload is not None and published_manifest_identity is not None:
                manifest_path = project_dir / "project.json"
                _unlink_owned_file(
                    self.boundary,
                    manifest_path,
                    expected=published_manifest_identity,
                    payload=manifest_payload,
                    max_bytes=MANIFEST_MAX_BYTES,
                )
                with contextlib.suppress(Exception):
                    _fsync_parent(project_dir)
            if state_published and not root_published and state_dir is not None and state_payload_bytes is not None and published_state_identity is not None:
                _unlink_owned_file(
                    self.boundary,
                    state_dir / "shoot.json",
                    expected=published_state_identity,
                    payload=state_payload_bytes,
                    max_bytes=STATE_MAX_BYTES,
                )
            if not root_published and state_dir is not None:
                with contextlib.suppress(OSError):
                    if not _members(state_dir):
                        state_dir.rmdir()

    def read_music_interpretation(
        self,
        directory: str | Path,
        *,
        project_id: str,
        track_sha256: str,
    ) -> dict[str, Any]:
        project_id = _project_id(project_id)
        track_sha256 = _music_track(track_sha256)
        project_dir = self._directory(directory)
        project_identity = self.boundary.pin_directory(project_dir)
        lock = self.boundary.project_lock(project_dir)
        with lock:
            self.boundary.revalidate_directory(project_dir, project_identity)
            manifest = self._manifest_locked(project_dir)
            if manifest["project_id"] != project_id:
                raise ProjectError("music_scope_mismatch", "music interpretation belongs to another context")
            interpretation = self._read_music_locked(
                project_dir,
                project_id=project_id,
                track_sha256=track_sha256,
                sweep=True,
            )
            return _music_result(interpretation)

    def write_music_interpretation(
        self,
        directory: str | Path,
        *,
        project_id: str,
        track_sha256: str,
        expected_revision: int,
        value: Any,
    ) -> dict[str, Any]:
        project_id = _project_id(project_id)
        track_sha256 = _music_track(track_sha256)
        expected_revision = _music_revision(expected_revision)
        body = _music_body(value)
        project_dir = self._directory(directory)
        project_identity = self.boundary.pin_directory(project_dir)
        lock = self.boundary.project_lock(project_dir)
        with lock:
            self.boundary.revalidate_directory(project_dir, project_identity)
            manifest = self._manifest_locked(project_dir)
            if manifest["project_id"] != project_id:
                raise ProjectError("music_scope_mismatch", "music interpretation belongs to another context")
            current = self._read_music_locked(
                project_dir,
                project_id=project_id,
                track_sha256=track_sha256,
                sweep=True,
            )
            current_revision = 0 if current is None else current["revision"]
            if expected_revision != current_revision:
                _music_conflict()
            music_dir = self._ensure_music_state_dir(project_dir)
            current = self._read_music_locked(
                project_dir,
                project_id=project_id,
                track_sha256=track_sha256,
                sweep=False,
            )
            current_revision = 0 if current is None else current["revision"]
            if expected_revision != current_revision:
                _music_conflict()
            interpretation, payload = _music_build(
                track_sha256=track_sha256,
                project_id=project_id,
                revision=current_revision + 1,
                value=body,
            )
            try:
                music_identity = self.boundary.pin_directory(music_dir)
                self.boundary.revalidate_directory(project_dir, project_identity)
            except ProjectError:
                _music_unavailable()
            target = music_dir / f"{track_sha256}.json"
            if current is None:
                self._atomic_create_file(
                    project_dir,
                    music_dir,
                    target,
                    payload,
                    project_identity=project_identity,
                    directory_identity=music_identity,
                )
            else:
                self._atomic_replace_file(
                    music_dir,
                    target,
                    payload,
                    max_bytes=MUSIC_INTERPRETATION_MAX_BYTES,
                    expected_directory_identity=music_identity,
                )
            return _music_result(interpretation)

    def read_shoot_state(self, directory: str | Path, project_id: str | None = None) -> dict[str, Any]:
        project_dir = self._directory(directory)
        identity = self.boundary.pin_directory(project_dir)
        lock = self.boundary.project_lock(project_dir)
        with lock:
            self.boundary.revalidate_directory(project_dir, identity)
            manifest = self._manifest_locked(project_dir)
            if project_id is not None and manifest["project_id"] != project_id:
                _invalid("project identity does not match")
            _sweep_temps(self.boundary, project_dir)
            state = self._state_locked(project_dir, manifest)
            if state is None:
                state = self._create_default_state_locked(project_dir, manifest)
            elif state["state"]["approvedRoots"] != manifest["approved_roots"] and _mirror_manifest_roots(
                    self.boundary, state["state"]["approvedRoots"], manifest["approved_roots"]) == manifest["approved_roots"]:
                # Lane w27-rows item 1 (P1 VID-154 rest): the same folders in another spelling (the project folder
                # opened as c:/…/sample film, a relative root resolved against it) are no change: the reader gets the
                # manifest's form, nothing is written (opening a page never makes a revision).
                state = {**state, "state": {**state["state"], "approvedRoots": list(manifest["approved_roots"])}}
            elif state["state"]["approvedRoots"] != manifest["approved_roots"]:
                repaired = copy.deepcopy(state)
                repaired["revision"] += 1
                repaired["updated_at"] = _next_timestamp(state["updated_at"])
                repaired["state"]["approvedRoots"] = list(manifest["approved_roots"])
                _validate_state_v20(self.boundary, manifest["project_id"], manifest["approved_roots"], repaired)
                _atomic_payload = _bounded_payload(
                    _state_envelope_to_disk(repaired, project_dir),
                    max_bytes=STATE_MAX_BYTES,
                    label="shoot state",
                )
                self._atomic_replace_file(project_dir / "state", project_dir / "state" / "shoot.json", _atomic_payload, max_bytes=STATE_MAX_BYTES)
                state = repaired
            return {
                "project_id": manifest["project_id"],
                "revision": state["revision"],
                "updated_at": state["updated_at"],
                "state": state["state"],
            }

    def write_shoot_state(
        self,
        directory: str | Path,
        project_id: str | None = None,
        expected_revision: int | None = None,
        state: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        project_dir = self._directory(directory)
        identity = self.boundary.pin_directory(project_dir)
        lock = self.boundary.project_lock(project_dir)
        with lock:
            self.boundary.revalidate_directory(project_dir, identity)
            manifest = self._manifest_locked(project_dir)
            if project_id is None:
                _invalid("project identity is required")
            if manifest["project_id"] != project_id:
                _invalid("project identity does not match")
            current = self._state_locked(project_dir, manifest)
            if current is None:
                _invalid("shoot state is missing")
            if expected_revision is None or isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                _invalid("shoot expected revision is invalid")
            if current["revision"] != expected_revision:
                _project_conflict()
            if not isinstance(state, Mapping):
                _invalid("shoot state is invalid")
            state = dict(state)
            if "approvedRoots" in state:
                state["approvedRoots"] = _mirror_manifest_roots(self.boundary, state["approvedRoots"], manifest["approved_roots"])
            candidate = _state_payload(manifest["project_id"], state, _next_timestamp(current["updated_at"]), expected_revision + 1)
            validated = _validate_state_v20(self.boundary, manifest["project_id"], manifest["approved_roots"], candidate)
            # Lane w27-rows item 1 (P1 VID-154 rest; Resolve / Final Draft mark a project changed only when something
            # changed): a save whose stored form equals the stored state (the page re-saving what it opened, an absolute
            # in-project song path for its relative form) writes no revision.
            if _state_envelope_to_disk(validated, project_dir)["state"] == _state_envelope_to_disk(current, project_dir)["state"]:
                return {
                    "project_id": manifest["project_id"],
                    "revision": current["revision"],
                    "updated_at": current["updated_at"],
                    "state": current["state"],
                }
            payload = _bounded_payload(_state_envelope_to_disk(validated, project_dir), max_bytes=STATE_MAX_BYTES, label="shoot state")
            self._atomic_replace_file(project_dir / "state", project_dir / "state" / "shoot.json", payload, max_bytes=STATE_MAX_BYTES)
            return {
                "project_id": manifest["project_id"],
                "revision": validated["revision"],
                "updated_at": validated["updated_at"],
                "state": validated["state"],
            }

    def write_metadata(
        self,
        directory: str | Path,
        project_id: str | None = None,
        expected_revision: int | None = None,
        name: str | None = None,
        approved_roots: list[str] | None = None,
    ) -> dict[str, Any]:
        project_dir = self._directory(directory)
        identity = self.boundary.pin_directory(project_dir)
        lock = self.boundary.project_lock(project_dir)
        with lock:
            self.boundary.revalidate_directory(project_dir, identity)
            manifest = self._manifest_locked(project_dir, strict=True)
            if project_id is None:
                _invalid("project identity is required")
            if manifest["project_id"] != project_id:
                _invalid("project identity does not match")
            if expected_revision is None or isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
                _invalid("project expected revision is invalid")
            if manifest["revision"] != expected_revision:
                _project_conflict()
            clean_name = _name(name)
            roots = _normalize_roots(self.boundary, approved_roots if approved_roots is not None else [])
            updated = dict(manifest)
            updated["name"] = clean_name
            updated["approved_roots"] = roots
            updated["revision"] = expected_revision + 1
            updated["updated_at"] = _next_timestamp(manifest["updated_at"])
            payload = _bounded_payload(_manifest_to_disk(updated, project_dir), max_bytes=MANIFEST_MAX_BYTES, label="project manifest")
            self._atomic_replace_file(project_dir, project_dir / "project.json", payload, max_bytes=MANIFEST_MAX_BYTES)
            return {
                "project_id": updated["project_id"],
                "name": updated["name"],
                "revision": updated["revision"],
                "created_at": updated["created_at"],
                "updated_at": updated["updated_at"],
                "approved_roots": updated["approved_roots"],
            }

    # --- Project output folder (lane p5c-frm, Project charter decision 2) -----------
    def write_output_folder(self, directory: str | Path, project_id: str, folder: str | None) -> dict[str, Any]:
        """Set the project's output folder (None = reset to <project>/output). Stored relative when inside."""
        project_dir = self._directory(directory)
        identity = self.boundary.pin_directory(project_dir)
        lock = self.boundary.project_lock(project_dir)
        with lock:
            self.boundary.revalidate_directory(project_dir, identity)
            manifest = self._manifest_locked(project_dir, strict=True)
            if manifest["project_id"] != project_id:
                _invalid("project identity does not match")
            if folder is None:
                stored = OUTPUT_DEFAULT_NAME
                (project_dir / OUTPUT_DEFAULT_NAME).mkdir(exist_ok=True)
            else:
                chosen = self.boundary.validate_project_directory(folder, operation="open")
                try:
                    stored = Path(os.path.relpath(chosen, project_dir)).as_posix()
                    if stored == ".." or stored.startswith("../") or Path(stored).is_absolute():
                        stored = str(chosen)
                except ValueError:  # another drive
                    stored = str(chosen)
            updated = dict(manifest)
            updated["output_folder"] = stored
            updated["revision"] = manifest["revision"] + 1
            updated["updated_at"] = _next_timestamp(manifest["updated_at"])
            payload = _bounded_payload(_manifest_to_disk(updated, project_dir), max_bytes=MANIFEST_MAX_BYTES, label="project manifest")
            self._atomic_replace_file(project_dir, project_dir / "project.json", payload, max_bytes=MANIFEST_MAX_BYTES)
            return updated

    # --- Moved / copied project (lane w11-backend item 2, M-3) ------------------
    # A project folder that was moved or copied still names its OLD folder in
    # approved_roots, Shoot approvedRoots and Shoot path fields. Opening it at
    # the new place re-bases exactly the paths that lay under the old project
    # folder; a path outside it stays as it was (and shows offline there). A
    # re-based root that does not exist at the new place is dropped (it could
    # not be opened either). Returns the old folder when one was re-based.

    def rebase_moved_project(self, directory: str | Path, old_directories: Iterable[str]) -> str | None:
        project_dir = self._directory(directory)
        identity = self.boundary.pin_directory(project_dir)
        lock = self.boundary.project_lock(project_dir)
        with lock:
            self.boundary.revalidate_directory(project_dir, identity)
            raw = _loads_bounded(_read_regular(self.boundary, project_dir / "project.json", max_bytes=MANIFEST_MAX_BYTES))
            if not isinstance(raw, dict) or not isinstance(raw.get("approved_roots"), list):
                return None
            # Lane w34-sec (finding 3): a network / device root is refused on its text before the move detection
            # below probes any stored root (isdir/exists would contact the host); read_project refuses it the same way.
            if any(isinstance(root, str) and _local_path.remote_path_reason(root) is not None for root in raw["approved_roots"]):
                _invalid("approved roots are invalid")
            new_text = str(project_dir)
            state_path = project_dir / "state" / "shoot.json"
            current = None
            if state_path.is_file():
                try:
                    current = _loads_bounded(_read_regular(self.boundary, state_path, max_bytes=STATE_MAX_BYTES))
                except (ProjectError, OSError, ValueError):
                    current = None
            # Lane p12-medb: a manifest that stores any relative folder was written by p12-med or later, so its
            # absolute folders lay OUTSIDE the project when it was written: they never name its old place.
            post_wave = any(isinstance(r, str) and r and not _is_absolute_root(r) for r in raw["approved_roots"])
            old = next((str(item) for item in old_directories
                        if isinstance(item, str) and item and not _same_path(item, new_text)), None)
            stored_absolute = [r for r in _roots_from_disk(raw["approved_roots"], project_dir) if isinstance(r, str)]
            missing = [r for r in stored_absolute if os.path.isabs(r) and not os.path.isdir(r)]
            if old is not None and missing and not any(_same_path(r, old) or _rebase_path(r, old, new_text) != r for r in missing):
                old = None  # lane p12-med: the known place (Recent) is not where these folders were; read them instead
            if old is None:
                # Lane p12-med item 1: a project from another machine or home names no Recent entry here. Its old
                # folder is read from the paths it stored (Resolve relinks by the original paths [RV-RL]).
                state_now = current.get("state") if isinstance(current, dict) and isinstance(current.get("state"), dict) else {}
                mirror = state_now.get("approvedRoots")
                voters = [*([] if post_wave else raw["approved_roots"]), *(mirror if isinstance(mirror, list) else [])]
                old = _infer_previous_directory(project_dir, voters, _path_strings(state_now))
            stored = raw["approved_roots"]
            absolute = _roots_from_disk(stored, project_dir)
            # Lane p12-medb: only an absolute folder of a pre-wave manifest can lie at the old place (this version
            # stores every folder inside the project relative). A folder that is not re-based stays exactly as
            # stored: never replaced by the project folder, never dropped.
            roots = [_rebase_path(root, old, new_text) if old and not post_wave and isinstance(root, str) else root
                     for root in absolute]
            kept = [root for root, before in zip(roots, absolute)
                    if root == before or (isinstance(root, str) and os.path.isdir(root))]
            relative = [_root_to_disk(root, project_dir) for root in kept]
            if relative != stored and offline_roots(roots):
                # Lane p13-prjb finding 2 (cold review p13-prj Important 2): while an approved folder is offline the
                # open never rewrites project.json (Resolve opens with the volume missing and relinks later): the raw
                # manifest is read with the project folder (offline_ok) and nothing is re-based, so Shoot keeps its
                # paths too (its re-base would need the manifest write).
                _validate_manifest_v20(self.boundary, raw, project_dir)
                return None
            if relative != stored:
                updated = dict(raw)
                updated["approved_roots"] = kept
                updated["revision"] = raw["revision"] + 1 if type(raw.get("revision")) is int else raw.get("revision")
                updated["updated_at"] = _next_timestamp(raw["updated_at"]) if isinstance(raw.get("updated_at"), str) else raw.get("updated_at")
                manifest = _validate_manifest_v20(self.boundary, updated)
                payload = _bounded_payload(_manifest_to_disk(updated, project_dir), max_bytes=MANIFEST_MAX_BYTES, label="project manifest")
                self._atomic_replace_file(project_dir, project_dir / "project.json", payload, max_bytes=MANIFEST_MAX_BYTES)
            else:
                manifest = _validate_manifest_v20(self.boundary, raw, project_dir)
            if old is None:
                return None
            if isinstance(current, dict) and isinstance(current.get("state"), dict):
                state = _rebase_path_fields(current["state"], old, new_text)
                state["approvedRoots"] = list(manifest["approved_roots"])
                if state != current["state"]:
                    candidate = dict(current)
                    candidate["state"] = state
                    candidate["revision"] = current["revision"] + 1
                    candidate["updated_at"] = _next_timestamp(current["updated_at"])
                    validated = _validate_state_v20(self.boundary, manifest["project_id"], manifest["approved_roots"], candidate)
                    payload = _bounded_payload(_state_envelope_to_disk(validated, project_dir), max_bytes=STATE_MAX_BYTES, label="shoot state")
                    self._atomic_replace_file(project_dir / "state", state_path, payload, max_bytes=STATE_MAX_BYTES)
            return old

    # --- Media marks (rating, reject, video keep, tags) -----------------------
    # One small document per project, state/media-marks.json, written with
    # expected_revision like the music interpretation. The body is validated by
    # the caller (routes/media/feed_1.py); here it must only be a bounded tree.

    def read_media_marks(self, directory: str | Path, *, project_id: str) -> dict[str, Any]:
        project_id = _project_id(project_id)
        project_dir = self._directory(directory)
        project_identity = self.boundary.pin_directory(project_dir)
        lock = self.boundary.project_lock(project_dir)
        with lock:
            self.boundary.revalidate_directory(project_dir, project_identity)
            manifest = self._manifest_locked(project_dir)
            if manifest["project_id"] != project_id:
                raise ProjectError("project_scope_mismatch", "media marks belong to another project")
            return self._media_marks_locked(project_dir, project_id)

    def write_media_marks(
        self,
        directory: str | Path,
        *,
        project_id: str,
        expected_revision: int,
        items: Mapping[str, Any],
    ) -> dict[str, Any]:
        project_id = _project_id(project_id)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            _invalid("media marks expected revision is invalid")
        if not isinstance(items, Mapping):
            _invalid("media marks are invalid")
        project_dir = self._directory(directory)
        project_identity = self.boundary.pin_directory(project_dir)
        lock = self.boundary.project_lock(project_dir)
        with lock:
            self.boundary.revalidate_directory(project_dir, project_identity)
            manifest = self._manifest_locked(project_dir)
            if manifest["project_id"] != project_id:
                raise ProjectError("project_scope_mismatch", "media marks belong to another project")
            current = self._media_marks_locked(project_dir, project_id)
            if current["revision"] != expected_revision:
                _project_conflict()
            # The state directory holds shoot.json first; a marks file beside a
            # missing shoot state would read as a damaged project.
            if self._state_locked(project_dir, manifest) is None:
                self._create_default_state_locked(project_dir, manifest)
            state_dir = self._ensure_state_dir(project_dir)
            document = {
                "schema": "studio.media-marks/1",
                "project_id": project_id,
                "revision": expected_revision + 1,
                "updated_at": _now_timestamp(),
                "items": dict(items),
            }
            payload = _bounded_payload(document, max_bytes=MUSIC_INTERPRETATION_MAX_BYTES, label="media marks")
            target = state_dir / "media-marks.json"
            if expected_revision == 0 and not os.path.lexists(target):
                try:
                    self._atomic_create_file(
                        project_dir, state_dir, target, payload,
                        project_identity=project_identity,
                        directory_identity=self.boundary.pin_directory(state_dir),
                    )
                except ProjectError as exc:
                    if exc.code == "music_conflict":
                        _project_conflict()
                    raise ProjectError("project_io_failed", "media marks could not be saved") from exc
            else:
                self._atomic_replace_file(state_dir, target, payload, max_bytes=MUSIC_INTERPRETATION_MAX_BYTES)
            return {"exists": True, "revision": document["revision"], "updated_at": document["updated_at"], "items": document["items"]}

    def _media_marks_locked(self, project_dir: Path, project_id: str) -> dict[str, Any]:
        target = project_dir / "state" / "media-marks.json"
        if not os.path.lexists(target):
            return {"exists": False, "revision": 0, "updated_at": None, "items": {}}
        value = _loads_bounded(_read_regular(self.boundary, target, max_bytes=MUSIC_INTERPRETATION_MAX_BYTES))
        if (
            not isinstance(value, dict)
            or value.get("schema") != "studio.media-marks/1"
            or value.get("project_id") != project_id
            or isinstance(value.get("revision"), bool)
            or not isinstance(value.get("revision"), int)
            or value["revision"] < 1
            or not isinstance(value.get("items"), dict)
        ):
            _invalid("media marks are damaged")
        return {"exists": True, "revision": value["revision"], "updated_at": value.get("updated_at"), "items": value["items"]}

    # --- Timeline: state/timeline.json (studio.timeline/1) and kept revisions ------
    # Its own document next to shoot.json and the music documents, never inside the
    # shoot-state allowlist. Same envelope and expected_revision semantics as the music
    # interpretation; the shape and invariants live in dashboard/timeline (loaded by path,
    # pure), the bytes go through this store's atomic create/replace primitives. Each
    # revision is also kept as timeline.json.r<revision>; the newest KEEP_REVISIONS stay.

    _TIMELINE_PACKAGE = "film_suite_timeline_core"
    _TIMELINE_REVISION_RE = re.compile(r"^timeline\.json\.r([1-9][0-9]{0,8})$")

    @classmethod
    def _timeline_document(cls) -> Any:
        with _MUSIC_CONTRACTS_LOCK:
            name = cls._TIMELINE_PACKAGE
            if name not in sys.modules:
                directory = Path(__file__).resolve().parent.parent / "timeline"
                spec = importlib.util.spec_from_file_location(name, directory / "__init__.py", submodule_search_locations=[str(directory)])
                if spec is None or spec.loader is None:
                    raise ProjectError("timeline_unavailable", "timeline core is unavailable")
                package = importlib.util.module_from_spec(spec)
                sys.modules[name] = package
                try:
                    spec.loader.exec_module(package)
                except BaseException:
                    sys.modules.pop(name, None)
                    raise
            import importlib as _importlib

            return _importlib.import_module(name + ".document")

    def _timeline_open(self, directory: str | Path, project_id: Any) -> tuple[str, Path, Any]:
        project_id = _project_id(project_id)
        project_dir = self._directory(directory)
        return project_id, project_dir, self.boundary.pin_directory(project_dir)

    def _timeline_scope_locked(self, project_dir: Path, identity: Any, project_id: str) -> dict[str, Any]:
        self.boundary.revalidate_directory(project_dir, identity)
        manifest = self._manifest_locked(project_dir)
        if manifest["project_id"] != project_id:
            raise ProjectError("project_scope_mismatch", "timeline belongs to another project")
        return manifest

    def _timeline_file_locked(self, path: Path, project_id: str) -> dict[str, Any]:
        document = self._timeline_document()
        raw = _read_regular(self.boundary, path, max_bytes=document.MAX_BYTES)
        try:
            return document.check(_loads_bounded(raw), project_id)
        except document.TimelineError as exc:
            raise ProjectError("invalid_timeline", str(exc)) from exc

    def _timeline_locked(self, project_dir: Path, project_id: str) -> dict[str, Any] | None:
        target = project_dir / "state" / "timeline.json"
        if not os.path.lexists(target):
            return None
        return self._timeline_file_locked(target, project_id)

    def _timeline_revision_files(self, state_dir: Path) -> list[tuple[int, Path]]:
        if not os.path.lexists(state_dir):
            return []
        found = []
        for name in _members(state_dir):
            match = self._TIMELINE_REVISION_RE.fullmatch(name)
            if match:
                found.append((int(match.group(1)), state_dir / name))
        return sorted(found, reverse=True)

    @staticmethod
    def _timeline_public(envelope: dict[str, Any] | None) -> dict[str, Any]:
        if envelope is None:
            return {"exists": False, "revision": 0, "updated_at": None, "label": "", "by": "artist", "state": None}
        return {"exists": True, **{key: envelope[key] for key in ("revision", "updated_at", "label", "by", "state")}}

    def read_timeline(self, directory: str | Path, *, project_id: str, request_id: str | None = None) -> dict[str, Any]:
        """The current document; with request_id also ``request``: the kept apply of that id
        ({request_id, ops_sha256, revision, answer}) or None (lane w8-agent item 4)."""
        project_id, project_dir, identity = self._timeline_open(directory, project_id)
        with self.boundary.project_lock(project_dir):
            self._timeline_scope_locked(project_dir, identity, project_id)
            public = self._timeline_public(self._timeline_locked(project_dir, project_id))
            if request_id is not None:
                public["request"] = self._timeline_request_locked(project_dir, project_id, request_id, landed=public["revision"])
            return public

    _TIMELINE_REQUESTS = "timeline.requests.json"

    def _timeline_requests_locked(self, project_dir: Path, project_id: str) -> list[dict[str, Any]]:
        document = self._timeline_document()
        path = project_dir / "state" / self._TIMELINE_REQUESTS
        if not os.path.lexists(path):
            return []
        raw = _read_regular(self.boundary, path, max_bytes=document.REQUESTS_MAX_BYTES)
        try:
            return document.check_requests(_loads_bounded(raw), project_id)["entries"]
        except document.TimelineError as exc:
            raise ProjectError("invalid_timeline", str(exc)) from exc

    def _timeline_request_locked(self, project_dir: Path, project_id: str, request_id: str, *, landed: int | None = None) -> dict[str, Any] | None:
        document = self._timeline_document()
        if not isinstance(request_id, str) or document.REQUEST_ID_RE.fullmatch(request_id) is None:
            raise ProjectError("invalid_timeline", "request_id must be 8-64 of A-Z a-z 0-9 _ -")
        # Lane w21-rel (CUT-175): a receipt is written BEFORE its revision is published; one above the current
        # revision belongs to a save that never reached its commit point and answers nothing.
        return next((e for e in self._timeline_requests_locked(project_dir, project_id)
                     if e["request_id"] == request_id and (landed is None or e["revision"] <= landed)), None)

    def _timeline_receipts_write_locked(self, project_dir: Path, project_id: str, identity: Any, entries: list[dict[str, Any]]) -> None:
        """Lane w21-rel (CUT-175): publish exactly ``entries`` as state/timeline.requests.json (same format)."""
        document = self._timeline_document()
        state_dir = project_dir / "state"
        payload = document.requests_payload(project_id, entries)
        _loads_bounded(payload)
        state_identity = self.boundary.pin_directory(state_dir)
        target = state_dir / self._TIMELINE_REQUESTS
        if os.path.lexists(target):
            self._atomic_replace_file(state_dir, target, payload, max_bytes=document.REQUESTS_MAX_BYTES, expected_directory_identity=state_identity)
        else:
            self._atomic_create_file(project_dir, state_dir, target, payload, project_identity=identity, directory_identity=state_identity)

    def _timeline_remember_locked(self, project_dir: Path, project_id: str, identity: Any, entry: dict[str, Any]) -> None:
        document = self._timeline_document()
        state_dir = project_dir / "state"
        entries = [e for e in self._timeline_requests_locked(project_dir, project_id) if e["request_id"] != entry["request_id"]]
        payload = document.requests_payload(project_id, [*entries, entry])
        _loads_bounded(payload)
        state_identity = self.boundary.pin_directory(state_dir)
        target = state_dir / self._TIMELINE_REQUESTS
        if os.path.lexists(target):
            self._atomic_replace_file(state_dir, target, payload, max_bytes=document.REQUESTS_MAX_BYTES, expected_directory_identity=state_identity)
        else:
            self._atomic_create_file(project_dir, state_dir, target, payload, project_identity=identity, directory_identity=state_identity)

    def write_timeline(
        self,
        directory: str | Path,
        *,
        project_id: str,
        expected_revision: int,
        state: Any,
        label: str = "",
        by: str = "artist",
        reason: str = "",
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save ``state`` as the next revision. ``request`` {request_id, ops_sha256, answer} (an
        apply that carried a request_id): checked under the lock before the revision, so a retry
        that raced its own first try answers ``{"request": kept entry}`` and writes nothing; else
        the answer is kept with the new revision in state/timeline.requests.json."""
        document = self._timeline_document()
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ProjectError("invalid_timeline", "expected_revision must be an integer >= 0")
        if not isinstance(reason, str) or len(reason) > document.REASON_MAX:
            raise ProjectError("invalid_timeline", f"reason must be text of at most {document.REASON_MAX} characters")
        project_id, project_dir, identity = self._timeline_open(directory, project_id)
        with self.boundary.project_lock(project_dir):
            manifest = self._timeline_scope_locked(project_dir, identity, project_id)
            if self._state_locked(project_dir, manifest) is None:
                self._create_default_state_locked(project_dir, manifest)
            state_dir = project_dir / "state"
            current = self._timeline_locked(project_dir, project_id)
            revision = 0 if current is None else current["revision"]
            if request is not None:
                kept = self._timeline_request_locked(project_dir, project_id, request["request_id"], landed=revision)
                if kept is not None:
                    if kept["ops_sha256"] != request["ops_sha256"]:
                        raise ProjectError("request_id_reused", "this request_id already applied other ops")
                    return {"request": kept}
            if expected_revision != revision:
                raise ProjectError("timeline_conflict", "timeline revision conflict")
            stamp = _now_timestamp() if current is None else _next_timestamp(current["updated_at"])
            try:
                # Lane w8-agent: each revision records what it changed and why (document.changes_of).
                before = current["state"] if current is not None else self._timeline_schema().default_state()
                changes = document.changes_of(before, state) if isinstance(state, dict) and isinstance(state.get("timelines"), list) else None
                envelope = document.build(project_id, revision + 1, stamp, copy.deepcopy(state), label=label, by=by,
                                          changes=changes, reason=reason)
                payload = document.payload(envelope)
            except document.TimelineError as exc:
                raise ProjectError(exc.code, str(exc)) from exc
            _loads_bounded(payload)
            # Lane w21-rel (CUT-175; reference: a write-ahead receipt before the commit record). The ONE commit point
            # is the publication of state/timeline.json. Before it: the receipt of this request (answer of revision
            # n+1) is made durable and receipts of saves that never landed are dropped; any failure there answers the
            # error and nothing is saved. After it the save is committed: the kept revision file and pruning are
            # derivative, a failure there answers committed with history_pending (the next save rebuilds the file).
            entry = None
            try:
                entries = self._timeline_requests_locked(project_dir, project_id)
                live = [e for e in entries if e["revision"] <= revision and (request is None or e["request_id"] != request["request_id"])]
                if request is not None:
                    answer = {**request["answer"], "revision": revision + 1}
                    entry = {"request_id": request["request_id"], "ops_sha256": request["ops_sha256"], "revision": revision + 1, "answer": answer}
                    self._timeline_receipts_write_locked(project_dir, project_id, identity, [*live, entry])
                elif len(live) != len(entries):
                    self._timeline_receipts_write_locked(project_dir, project_id, identity, live)
                state_identity = self.boundary.pin_directory(state_dir)
                target = state_dir / "timeline.json"
                if current is None:
                    self._atomic_create_file(project_dir, state_dir, target, payload, project_identity=identity, directory_identity=state_identity)
                else:
                    self._atomic_replace_file(state_dir, target, payload, max_bytes=document.MAX_BYTES, expected_directory_identity=state_identity)
            except ProjectError as exc:
                if exc.code == "music_conflict":
                    raise ProjectError("timeline_conflict", "timeline revision conflict") from exc
                if exc.code.startswith("music_"):
                    raise ProjectError("timeline_unavailable", "timeline could not be saved") from exc
                raise
            # ---- committed ----
            history_pending = False
            try:
                before_file = state_dir / f"timeline.json.r{revision}"
                if current is not None and not os.path.lexists(before_file):
                    # The revision before lost its kept file to an earlier failure: rebuilt from the document it was.
                    self._atomic_create_file(project_dir, state_dir, before_file, document.payload(current),
                                             project_identity=identity, directory_identity=state_identity)
                kept = state_dir / f"timeline.json.r{revision + 1}"
                if os.path.lexists(kept):  # residue of an older history: the live revision wins
                    self._atomic_replace_file(state_dir, kept, payload, max_bytes=document.MAX_BYTES, expected_directory_identity=state_identity)
                else:
                    self._atomic_create_file(project_dir, state_dir, kept, payload, project_identity=identity, directory_identity=state_identity)
                for _old, path in self._timeline_revision_files(state_dir)[document.KEEP_REVISIONS:]:
                    _unlink_owned_file(self.boundary, path, expected=self.boundary.pin_file(path), max_bytes=document.MAX_BYTES)
            except (ProjectError, OSError):
                history_pending = True
            public = self._timeline_public(envelope)
            if history_pending:
                public["history_pending"] = True
            if entry is not None:
                shown = {**entry, "answer": {**entry["answer"], "history_pending": True}} if history_pending else entry
                return {**public, "request": shown}
            return public

    def timeline_revisions(self, directory: str | Path, *, project_id: str) -> dict[str, Any]:
        project_id, project_dir, identity = self._timeline_open(directory, project_id)
        with self.boundary.project_lock(project_dir):
            self._timeline_scope_locked(project_dir, identity, project_id)
            current = self._timeline_locked(project_dir, project_id)
            rows = []
            for number, path in self._timeline_revision_files(project_dir / "state"):
                envelope = self._timeline_file_locked(path, project_id)
                if envelope["revision"] != number:
                    raise ProjectError("invalid_timeline", f"{path.name} holds revision {envelope['revision']}")
                rows.append(self._timeline_revision_row(envelope))
            return {"revision": 0 if current is None else current["revision"], "revisions": rows}

    def read_timeline_revision(self, directory: str | Path, *, project_id: str, revision: int) -> dict[str, Any]:
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ProjectError("invalid_timeline", "revision must be an integer >= 1")
        project_id, project_dir, identity = self._timeline_open(directory, project_id)
        with self.boundary.project_lock(project_dir):
            self._timeline_scope_locked(project_dir, identity, project_id)
            path = project_dir / "state" / f"timeline.json.r{revision}"
            if not os.path.lexists(path):
                raise ProjectError("revision_unknown", f"revision {revision} is not kept")
            envelope = self._timeline_file_locked(path, project_id)
            if envelope["revision"] != revision:
                raise ProjectError("invalid_timeline", f"{path.name} holds revision {envelope['revision']}")
            return {**self._timeline_public(envelope), **{key: envelope[key] for key in ("reason", "changes") if key in envelope}}

    @classmethod
    def _timeline_schema(cls) -> Any:
        cls._timeline_document()
        import importlib as _importlib

        return _importlib.import_module(cls._TIMELINE_PACKAGE + ".schema")

    @staticmethod
    def _timeline_revision_row(envelope: dict[str, Any]) -> dict[str, Any]:
        """One line of the revision list; an envelope written before lane w8-agent (no reason, no
        changes) answers exactly the four keys it answered before."""
        row = {key: envelope[key] for key in ("revision", "updated_at", "label", "by")}
        if "reason" in envelope:
            row["reason"] = envelope["reason"]
        if "changes" in envelope:
            row["changes"] = dict(envelope["changes"]["counts"])
        return row

    # --- Shot chains: state/chains/<chain_id>.json (studio.shot-chain/1, lane w9-chain) -------------
    # One document per shot chain, beside the timeline, never inside the shoot-state allowlist. Same
    # envelope and expected_revision semantics as the timeline: each revision is also kept as
    # <chain_id>.json.r<revision> (the newest KEEP_REVISIONS stay), and an apply that carried a request_id
    # is remembered in <chain_id>.requests.json with its answer. The shape lives in dashboard/chain
    # (loaded by path, pure); the bytes go through this store's atomic create/replace primitives.

    _CHAIN_PACKAGE = "film_suite_chain_core"
    _CHAIN_FILE_RE = re.compile(r"^(ch-[0-9a-f]{8})\.json$")
    _CHAIN_KEPT_RE = re.compile(r"^(ch-[0-9a-f]{8})\.json\.r([1-9][0-9]{0,8})$")

    @classmethod
    def _chain_module(cls, name: str = "document") -> Any:
        with _MUSIC_CONTRACTS_LOCK:
            package = cls._CHAIN_PACKAGE
            if package not in sys.modules:
                directory = Path(__file__).resolve().parent.parent / "chain"
                spec = importlib.util.spec_from_file_location(package, directory / "__init__.py", submodule_search_locations=[str(directory)])
                if spec is None or spec.loader is None:
                    raise ProjectError("chain_unavailable", "chain core is unavailable")
                module = importlib.util.module_from_spec(spec)
                sys.modules[package] = module
                try:
                    spec.loader.exec_module(module)
                except BaseException:
                    sys.modules.pop(package, None)
                    raise
            import importlib as _importlib

            return _importlib.import_module(f"{package}.{name}")

    def _chain_dir_locked(self, project_dir: Path, *, create: bool) -> Path | None:
        chains = project_dir / "state" / "chains"
        if not os.path.lexists(chains):
            if not create:
                return None
            self._ensure_state_dir(project_dir)
            try:
                chains.mkdir()
            except FileExistsError:
                pass
            except OSError:
                raise ProjectError("chain_unavailable", "the chains folder could not be created")
        if chains.is_symlink() or not chains.is_dir():
            raise ProjectError("invalid_chain", "the chains folder is not a folder")
        return chains

    def _chain_file_locked(self, path: Path, project_id: str) -> dict[str, Any]:
        document = self._chain_module()
        raw = _read_regular(self.boundary, path, max_bytes=document.MAX_BYTES)
        try:
            return document.check_envelope(_loads_bounded(raw), project_id)
        except document.ChainError as exc:
            raise ProjectError("invalid_chain", str(exc)) from exc

    def _chain_id(self, chain_id: Any) -> str:
        if type(chain_id) is not str or self._chain_module().CHAIN_ID_RE.fullmatch(chain_id) is None:
            raise ProjectError("chain_unknown", "chain_id must be ch- and 8 hex digits")
        return chain_id

    def list_chains(self, directory: str | Path, *, project_id: str) -> list[dict[str, Any]]:
        """Every chain envelope of the project, in chain id order."""
        project_id, project_dir, identity = self._timeline_open(directory, project_id)
        with self.boundary.project_lock(project_dir):
            self._timeline_scope_locked(project_dir, identity, project_id)
            chains = self._chain_dir_locked(project_dir, create=False)
            if chains is None:
                return []
            names = sorted(name for name in _members(chains) if self._CHAIN_FILE_RE.fullmatch(name))
            return [self._chain_file_locked(chains / name, project_id) for name in names]

    def read_chain(self, directory: str | Path, *, project_id: str, chain_id: str, request_id: str | None = None) -> Any:
        """The chain's envelope or None; with request_id {envelope, request: the kept apply of that id or None}."""
        chain_id = self._chain_id(chain_id)
        project_id, project_dir, identity = self._timeline_open(directory, project_id)
        with self.boundary.project_lock(project_dir):
            self._timeline_scope_locked(project_dir, identity, project_id)
            chains = self._chain_dir_locked(project_dir, create=False)
            target = None if chains is None else chains / f"{chain_id}.json"
            current = self._chain_file_locked(target, project_id) if target is not None and os.path.lexists(target) else None
            if request_id is None:
                return current
            kept = self._chain_request_locked(chains, project_id, chain_id, request_id) if chains is not None else None
            return {"envelope": current, "request": kept}

    def _chain_requests_locked(self, chains: Path, project_id: str, chain_id: str) -> list[dict[str, Any]]:
        document = self._chain_module()
        path = chains / f"{chain_id}.requests.json"
        if not os.path.lexists(path):
            return []
        raw = _read_regular(self.boundary, path, max_bytes=document.REQUESTS_MAX_BYTES)
        try:
            return document.check_requests(_loads_bounded(raw), project_id, chain_id)
        except document.ChainError as exc:
            raise ProjectError("invalid_chain", str(exc)) from exc

    def _chain_request_locked(self, chains: Path, project_id: str, chain_id: str, request_id: str) -> dict[str, Any] | None:
        document = self._chain_module()
        if not isinstance(request_id, str) or document.REQUEST_ID_RE.fullmatch(request_id) is None:
            raise ProjectError("invalid_chain", "request_id must be 8-64 of A-Z a-z 0-9 _ -")
        return next((e for e in self._chain_requests_locked(chains, project_id, chain_id) if e["request_id"] == request_id), None)

    def _chain_publish_locked(self, project_dir: Path, chains: Path, identity: Any, target: Path, payload: bytes, max_bytes: int) -> None:
        chains_identity = self.boundary.pin_directory(chains)
        if os.path.lexists(target):
            self._atomic_replace_file(chains, target, payload, max_bytes=max_bytes, expected_directory_identity=chains_identity)
        else:
            self._atomic_create_file(project_dir, chains, target, payload, project_identity=identity, directory_identity=chains_identity)

    def write_chain(
        self,
        directory: str | Path,
        *,
        project_id: str,
        chain_id: str,
        expected_revision: int,
        chain: dict[str, Any],
        bindings: dict[str, Any],
        take_inputs: dict[str, Any],
        by: str,
        reason: str = "",
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Save ``chain`` as the next revision (0 creates it). ``request`` {request_id, ops_sha256, answer}:
        checked under the lock first, so a retry that raced its own first try answers ``{"request": kept}``
        and writes nothing; else the answer is kept with the new revision."""
        document = self._chain_module()
        chain_id = self._chain_id(chain_id)
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 0:
            raise ProjectError("invalid_chain", "expected_revision must be an integer >= 0")
        if not isinstance(reason, str) or len(reason) > document.REASON_MAX:
            raise ProjectError("invalid_chain", f"reason must be text of at most {document.REASON_MAX} characters")
        project_id, project_dir, identity = self._timeline_open(directory, project_id)
        with self.boundary.project_lock(project_dir):
            manifest = self._timeline_scope_locked(project_dir, identity, project_id)
            if self._state_locked(project_dir, manifest) is None:
                self._create_default_state_locked(project_dir, manifest)
            chains = self._chain_dir_locked(project_dir, create=True)
            target = chains / f"{chain_id}.json"
            current = self._chain_file_locked(target, project_id) if os.path.lexists(target) else None
            revision = 0 if current is None else current["chain"]["revision"]
            if request is not None:
                kept = self._chain_request_locked(chains, project_id, chain_id, request["request_id"])
                if kept is not None:
                    if kept["ops_sha256"] != request["ops_sha256"]:
                        raise ProjectError("request_id_reused", "this request_id already applied other ops")
                    return {"request": kept}
            if expected_revision != revision:
                raise ProjectError("chain_conflict", f"the chain is at revision {revision}")
            stamp = _now_timestamp() if current is None else _next_timestamp(current["chain"]["updated_at"])
            saved = copy.deepcopy(chain)
            saved.update(chain_id=chain_id, project_id=project_id, revision=revision + 1, updated_at=stamp, by=by)
            try:
                envelope = document.envelope(project_id, saved, bindings=copy.deepcopy(bindings),
                                             take_inputs=copy.deepcopy(take_inputs), reason=reason)
                document.check_envelope(envelope, project_id)
                payload = document.payload(envelope)
            except document.ChainError as exc:
                raise ProjectError(exc.code, str(exc)) from exc
            _loads_bounded(payload)
            try:
                self._chain_publish_locked(project_dir, chains, identity, target, payload, document.MAX_BYTES)
                self._chain_publish_locked(project_dir, chains, identity, chains / f"{chain_id}.json.r{revision + 1}", payload, document.MAX_BYTES)
                olds = []
                for name in _members(chains):
                    match = self._CHAIN_KEPT_RE.fullmatch(name)
                    if match and match.group(1) == chain_id:
                        olds.append((int(match.group(2)), chains / name))
                for _old, path in sorted(olds, reverse=True)[document.KEEP_REVISIONS:]:
                    _unlink_owned_file(self.boundary, path, expected=self.boundary.pin_file(path), max_bytes=document.MAX_BYTES)
                if request is not None:
                    # After the document: a crash in between leaves a retry conflicting, never replaying a revision that did not land.
                    answer = {**request["answer"], "revision": revision + 1}
                    entry = {"request_id": request["request_id"], "ops_sha256": request["ops_sha256"], "revision": revision + 1, "answer": answer}
                    entries = [e for e in self._chain_requests_locked(chains, project_id, chain_id) if e["request_id"] != entry["request_id"]]
                    self._chain_publish_locked(project_dir, chains, identity, chains / f"{chain_id}.requests.json",
                                               document.requests_payload(project_id, chain_id, [*entries, entry]), document.REQUESTS_MAX_BYTES)
                    return {**envelope, "request": entry}
            except ProjectError as exc:
                if exc.code == "music_conflict":
                    raise ProjectError("chain_conflict", "chain revision conflict") from exc
                if exc.code.startswith("music_"):
                    raise ProjectError("chain_unavailable", "the chain could not be saved") from exc
                raise
            return envelope

    def read_chain_revision(self, directory: str | Path, *, project_id: str, chain_id: str, revision: int) -> dict[str, Any]:
        """One kept revision's envelope (<chain_id>.json.r<revision>)."""
        chain_id = self._chain_id(chain_id)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ProjectError("invalid_chain", "revision must be an integer >= 1")
        project_id, project_dir, identity = self._timeline_open(directory, project_id)
        with self.boundary.project_lock(project_dir):
            self._timeline_scope_locked(project_dir, identity, project_id)
            chains = self._chain_dir_locked(project_dir, create=False)
            path = None if chains is None else chains / f"{chain_id}.json.r{revision}"
            if path is None or not os.path.lexists(path):
                raise ProjectError("chain_unknown", f"revision {revision} of {chain_id} is not kept")
            envelope = self._chain_file_locked(path, project_id)
            if envelope["chain"]["revision"] != revision:
                raise ProjectError("invalid_chain", f"{path.name} holds revision {envelope['chain']['revision']}")
            return envelope


def read_music_interpretation(
    directory: str | Path,
    *,
    project_id: str,
    track_sha256: str,
) -> dict[str, Any]:
    return ProjectStore().read_music_interpretation(
        directory,
        project_id=project_id,
        track_sha256=track_sha256,
    )


def write_music_interpretation(
    directory: str | Path,
    *,
    project_id: str,
    track_sha256: str,
    expected_revision: int,
    value: Any,
) -> dict[str, Any]:
    return ProjectStore().write_music_interpretation(
        directory,
        project_id=project_id,
        track_sha256=track_sha256,
        expected_revision=expected_revision,
        value=value,
    )


def _quarantine_link(source: Path, target: Path, expected: Any, parent_identity: Any) -> str | None:
    """Publish a no-replace hard link to the already validated registry object.

    POSIX opens the expected source object and passes its descriptor to Linux
    ``linkat(AT_EMPTY_PATH)``.  A source pathname is never supplied to the
    publication primitive.  Windows opens and validates both the source file
    and parent directory, then passes their handles to the NT
    FileLinkInformation primitive.  The target name is relative to that pinned
    parent and can never be redirected by a pathname rebind.
    """

    if os.name == "nt":
        parent_handle, observed_parent = _open_existing(source.parent, directory=True)
        source_handle = None
        try:
            if not parent_identity.same_object(observed_parent):
                raise ProjectError("project_unavailable", "recent registry parent changed")
            source_handle, observed_source = _open_existing(source, directory=False)
            if (
                not expected.same_object(observed_source)
                or expected.size != observed_source.size
            ):
                raise ProjectError("project_unavailable", "recent registry source changed")
            current_parent = _identity_from_handle(parent_handle, source.parent)
            if not parent_identity.same_object(current_parent):
                raise ProjectError("project_unavailable", "recent registry parent changed")
            win_link_handle_no_replace(source_handle, parent_handle, target.name)
        finally:
            if source_handle is not None:
                source_handle.close()
            parent_handle.close()
        return
    parent_handle, observed_parent = _open_existing(source.parent, directory=True)
    source_handle = None
    try:
        if not parent_identity.same_object(observed_parent):
            raise ProjectError("project_unavailable", "recent registry parent changed")
        source_handle, observed_source = _open_child_existing(
            parent_handle, source.parent, source.name, directory=False
        )
        if not expected.same_object(observed_source) or expected.size != observed_source.size:
            raise ProjectError("project_unavailable", "recent registry source changed")
        if expected.size > RECENT_REGISTRY_HARD_MAX_BYTES:
            raise ProjectError("project_unavailable", "recent registry is too large")
        source_hash, source_size = _hash_handle(
            source_handle, max_bytes=RECENT_REGISTRY_HARD_MAX_BYTES
        )
        if source_size != expected.size:
            raise ProjectError("project_unavailable", "recent registry source changed")
        final_source = _identity_from_handle(source_handle, source)
        if not expected.same_object(final_source) or expected.size != final_source.size:
            raise ProjectError("project_unavailable", "recent registry source changed")
        _posix_linkat_empty_path(
            int(source_handle.raw), int(parent_handle.raw), target.name
        )
        target_handle: Any = None
        try:
            target_handle, target_identity = _open_child_existing(
                parent_handle, source.parent, target.name, directory=False
            )
            if not expected.same_object(target_identity) or expected.size != target_identity.size:
                raise ProjectError("project_unavailable", "recent registry quarantine changed")
            target_hash, target_size = _hash_handle(
                target_handle, max_bytes=RECENT_REGISTRY_HARD_MAX_BYTES
            )
            if target_size != expected.size or target_hash != source_hash:
                raise ProjectError("project_unavailable", "recent registry quarantine changed")
            final_target = _identity_from_handle(target_handle, target)
            if not expected.same_object(final_target) or expected.size != final_target.size:
                raise ProjectError("project_unavailable", "recent registry quarantine changed")
        finally:
            if target_handle is not None:
                target_handle.close()
        return source_hash
    finally:
        if source_handle is not None:
            source_handle.close()
        parent_handle.close()


def _hash_regular_file(
    boundary: PathBoundary,
    path: Path,
    expected: Any,
    *,
    max_bytes: int,
) -> tuple[str, int]:
    """Hash a pinned regular object without retaining its entire contents."""

    handle, observed = _open_existing(path, directory=False)
    try:
        if (
            not expected.same_object(observed)
            or expected.size != observed.size
            or observed.size > max_bytes
        ):
            raise ProjectError("project_unavailable", "recent registry changed")
        digest = hashlib.sha256()
        remaining = observed.size
        while remaining:
            chunk_size = min(1024 * 1024, remaining)
            if os.name == "nt":
                from . import path_boundary as _path_boundary

                buffer = ctypes.create_string_buffer(chunk_size)
                read = wintypes.DWORD()
                if not _path_boundary._ReadFile(
                    handle.raw,
                    buffer,
                    chunk_size,
                    ctypes.byref(read),
                    None,
                ):
                    raise OSError(ctypes.get_last_error() or errno.EIO, "recent registry hash failed")
                chunk = buffer.raw[: read.value]
            else:
                chunk = os.read(int(handle.raw), chunk_size)
            if not chunk:
                raise OSError(errno.EIO, "recent registry hash reached EOF")
            digest.update(chunk)
            remaining -= len(chunk)
        current = _identity_from_handle(handle, path)
        if not expected.same_object(current) or expected.size != current.size:
            raise ProjectError("project_unavailable", "recent registry changed")
        return digest.hexdigest(), observed.size
    finally:
        handle.close()


class RecentProjectStore:
    """Private, crash-safe recent-project pointer registry.

    The registry is intentionally a separate store from project contents.  Its
    only public projection is a list of project IDs, names, and availability;
    the real directory is retained solely inside this backend object.
    """

    def __init__(
        self,
        hermes_home: str | Path | None = None,
        *,
        plugin_data_dir: str | Path | None = None,
        registry_path: str | Path | None = None,
        private_registry_path: str | Path | None = None,
        boundary: PathBoundary | None = None,
    ) -> None:
        self.boundary = boundary or PathBoundary(hermes_home=hermes_home)
        home = self.boundary.hermes_home
        if plugin_data_dir is None:
            plugin_data = state_root(home / "plugin-data")
        else:
            try:
                plugin_data = Path(plugin_data_dir)
            except (TypeError, ValueError):
                raise ProjectError("invalid_project", "recent registry location is invalid")
        selected_registry = registry_path if registry_path is not None else private_registry_path
        if selected_registry is None:
            selected_registry = plugin_data / _RECENT_REGISTRY_NAME
        try:
            registry = Path(selected_registry)
        except (TypeError, ValueError):
            raise ProjectError("invalid_project", "recent registry location is invalid")
        if registry.parent != plugin_data:
            raise ProjectError("invalid_project", "recent registry location is invalid")
        self.plugin_data_dir = plugin_data
        self.registry_path = registry
        self.project_store = ProjectStore(boundary=self.boundary)

    @property
    def private_registry_path(self) -> Path:
        return self.registry_path

    def _ensure_private_space(self) -> None:
        try:
            ensure_private_node(self.boundary.hermes_home, directory=True)
            ensure_private_node(self.boundary.hermes_home / "plugin-data", directory=True)
            ensure_private_node(self.plugin_data_dir, directory=True)
        except ProjectError:
            raise
        except (OSError, ValueError, TypeError):
            raise ProjectError("recents_unavailable", "recent registry is unavailable")

    def _writer_lock(self) -> NamedProjectLock:
        canonical = str(self.plugin_data_dir.resolve(strict=False))
        digest = hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
        name = "Local\\HermesFilmSuite-Recent-" + digest
        if os.name == "nt":
            return NamedProjectLock(name)
        lock_path = self.plugin_data_dir / ".recent-projects.lock"
        ensure_private_node(lock_path, directory=False)
        return NamedProjectLock(name, lock_file=lock_path)

    @staticmethod
    def _directory_text(value: Any) -> str:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ValueError("invalid recent directory")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("invalid recent directory")
        if not os.path.isabs(value):
            raise ValueError("invalid recent directory")
        if any(part == ".." for part in re.split(r"[\\\\/]", value)):
            raise ValueError("invalid recent directory")
        return value

    @staticmethod
    def _recent_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate recent registry key")
            result[key] = value
        return result

    @classmethod
    def _decode_registry(cls, raw: bytes) -> list[dict[str, str]]:
        if len(raw) > RECENT_REGISTRY_MAX_BYTES or raw.startswith(bytes((0xEF, 0xBB, 0xBF))):
            raise ValueError("invalid recent registry")
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=cls._recent_pairs,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError, MemoryError, OverflowError):
            raise ValueError("invalid recent registry")
        if not isinstance(value, list) or len(value) > RECENT_MAX_ENTRIES:
            raise ValueError("invalid recent registry")
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"project_id", "name", "directory", "opened_at"}:
                raise ValueError("invalid recent registry")
            try:
                project_id = _project_id(item["project_id"])
                name = _name(item["name"])
                directory = cls._directory_text(item["directory"])
                opened_at = _timestamp(item["opened_at"])
            except (ProjectError, TypeError, ValueError):
                raise ValueError("invalid recent registry")
            if project_id in seen:
                raise ValueError("invalid recent registry")
            seen.add(project_id)
            entries.append(
                {
                    "project_id": project_id,
                    "name": name,
                    "directory": directory,
                    "opened_at": opened_at,
                }
            )
        return entries

    def _registry_snapshot(self) -> tuple[bytes, Any] | None:
        try:
            handle, expected = _open_existing(self.registry_path, directory=False)
        except ProjectError as exc:
            if exc.code == "project_unavailable":
                return None
            raise
        try:
            if expected.size > RECENT_REGISTRY_MAX_BYTES:
                raise ValueError("recent registry is too large")
            raw = self.boundary.read_handle_bytes(handle, max_bytes=RECENT_REGISTRY_MAX_BYTES)
            actual = _identity_from_handle(handle, self.registry_path)
            if not expected.same_object(actual) or expected.size != actual.size or len(raw) != expected.size:
                raise ValueError("recent registry changed")
            return raw, expected
        except (ProjectError, OSError, ValueError):
            raise ValueError("recent registry is unavailable")
        finally:
            handle.close()

    def _registry_snapshot_for_quarantine(self) -> tuple[bytes | None, Any] | None:
        """Capture bounded ownership metadata for an explicit repair action.

        Normal reads remain capped at ``RECENT_REGISTRY_MAX_BYTES``.  A
        conscious repair may quarantine a larger regular file without decoding
        or retaining it, but only up to the separate operational hard cap.
        """

        try:
            handle, expected = _open_existing(self.registry_path, directory=False)
        except ProjectError as exc:
            if exc.code == "project_unavailable":
                return None
            raise
        try:
            if expected.size > RECENT_REGISTRY_HARD_MAX_BYTES:
                raise ValueError("recent registry exceeds operational hard cap")
            if expected.size <= RECENT_REGISTRY_MAX_BYTES:
                raw = self.boundary.read_handle_bytes(handle, max_bytes=RECENT_REGISTRY_MAX_BYTES)
                actual = _identity_from_handle(handle, self.registry_path)
                if (
                    not expected.same_object(actual)
                    or expected.size != actual.size
                    or len(raw) != expected.size
                ):
                    raise ValueError("recent registry changed")
                return raw, expected
            # For an oversized object identity and size are the ownership
            # snapshot.  The quarantine primitive links that exact object and
            # revalidates the source before removing its original name.
            actual = _identity_from_handle(handle, self.registry_path)
            if not expected.same_object(actual) or expected.size != actual.size:
                raise ValueError("recent registry changed")
            return None, expected
        except (ProjectError, OSError, ValueError):
            raise ValueError("recent registry is unavailable")
        finally:
            handle.close()

    def _read_entries(self) -> tuple[list[dict[str, str]] | None, tuple[bytes, Any] | None]:
        try:
            snapshot = self._registry_snapshot()
        except (ProjectError, ValueError, OSError):
            raise ValueError("invalid recent registry")
        if snapshot is None:
            return None, None
        raw, identity = snapshot
        return self._decode_registry(raw), (raw, identity)

    def _read_boundary_without_key_creation(self) -> PathBoundary:
        boundary = PathBoundary(hermes_home=self.boundary.hermes_home)
        # Availability is a read-only projection.  Path identity checks do not
        # need the profile HMAC, and an empty sentinel prevents a missing key
        # from being published merely because the chooser was displayed.
        boundary._key = b""
        return boundary

    def _entry_available(self, entry: Mapping[str, str]) -> bool:
        try:
            # This is deliberately the complete read boundary: it checks raw
            # text spelling, every component/reparse boundary, requested type,
            # and the physical HERMES_HOME exclusion before opening the child.
            read_boundary = self._read_boundary_without_key_creation()
            directory = read_boundary.validate_project_directory(
                entry["directory"], operation="open"
            )
            manifest = read_boundary.read_manifest(
                directory,
                max_bytes=MANIFEST_MAX_BYTES,
                object_pairs_hook=_reject_duplicate_pairs,
            )
            checked = _validate_manifest_v20(read_boundary, manifest, directory)
            return checked["project_id"] == entry["project_id"] and checked["name"] == entry["name"]
        except (ProjectError, OSError, ValueError, TypeError, UnicodeError):
            return False

    @staticmethod
    def _public_entry(entry: Mapping[str, str], available: bool) -> dict[str, Any]:
        return {
            "project_id": entry["project_id"],
            "name": entry["name"],
            "available": bool(available),
        }

    def list_recent(self) -> dict[str, Any]:
        """Return the read-only public projection without repairing anything."""
        try:
            entries, _snapshot = self._read_entries()
        except (ProjectError, ValueError, OSError, TypeError, UnicodeError):
            return {"projects": [], "warning": RECENT_WARNING}
        if entries is None:
            return {"projects": [], "warning": RECENT_WARNING}
        ordered = sorted(entries, key=lambda item: (item["opened_at"], item["project_id"]), reverse=True)
        return {
            "projects": [self._public_entry(item, self._entry_available(item)) for item in ordered]
        }

    def _quarantine(self, snapshot: tuple[bytes | None, Any], *, parent_identity: Any = None) -> None:
        raw, expected = snapshot
        parent = self.plugin_data_dir
        if parent_identity is None:
            parent_identity = self._private_directory_identity(parent)
        self._private_revalidate_directory(parent, parent_identity)
        current = self._registry_snapshot_for_quarantine()
        if current is None:
            raise ProjectError("project_unavailable", "recent registry changed")
        current_raw, current_identity = current
        if (
            not expected.same_object(current_identity)
            or expected.size != current_identity.size
            or (raw is not None and current_raw != raw)
        ):
            raise ProjectError("project_unavailable", "recent registry changed")
        if raw is not None:
            expected_hash = hashlib.sha256(raw).hexdigest()
        else:
            expected_hash, expected_size = _hash_regular_file(
                self.boundary,
                self.registry_path,
                expected,
                max_bytes=RECENT_REGISTRY_HARD_MAX_BYTES,
            )
            if expected_size != expected.size:
                raise ProjectError("project_unavailable", "recent registry changed")
        stamp = _now_timestamp().replace("-", "").replace(":", "")
        target = parent / f"recent-projects.corrupt-{stamp}-{secrets.token_hex(16)}.json"
        if target.parent != parent or _CORRUPT_RECENT_RE.fullmatch(target.name) is None:
            raise ProjectError("project_unavailable", "recent registry quarantine failed")
        try:
            # This check is immediately adjacent to the no-replace primitive.
            self._private_revalidate_directory(parent, parent_identity)
            linked_hash = _quarantine_link(self.registry_path, target, expected, parent_identity)
            if linked_hash is not None and linked_hash != expected_hash:
                raise ProjectError("project_unavailable", "recent registry source changed")
            # Capture the target identity before any source unlink or later
            # revalidation can fail.  The published artifact is retained on
            # every later failure; cleanup must never trust the target name.
            linked_target_identity = self._private_file_identity(target)
            if (
                not expected.same_object(linked_target_identity)
                or expected.size != linked_target_identity.size
            ):
                raise ProjectError("project_unavailable", "recent registry quarantine changed")
            # Keep the corrupt source name until both the parent and source
            # identities still prove ownership.  A final source swap therefore
            # cannot be deleted through the foreign pathname.
            self._private_revalidate_directory(parent, parent_identity)
            if not self._private_unlink(
                self.registry_path,
                expected,
                payload=raw,
                expected_hash=expected_hash,
            ):
                raise ProjectError("project_unavailable", "recent registry changed")
            self._private_revalidate_directory(parent, parent_identity)
            _fsync_parent(parent)
        except FileExistsError as exc:
            raise ProjectError("project_unavailable", "recent registry quarantine failed") from exc
        except (OSError, ProjectError) as exc:
            # A successfully published quarantine link is explicit repair
            # history.  Preserve it, along with the source, when source unlink
            # or any later revalidation/flush fails: no portable primitive can
            # bind a content check atomically to deletion.
            raise ProjectError("project_unavailable", "recent registry quarantine failed") from exc

    @staticmethod
    def _encode_entries(entries: list[dict[str, str]]) -> bytes:
        if len(entries) > RECENT_MAX_ENTRIES:
            entries = entries[:RECENT_MAX_ENTRIES]
        try:
            payload = json.dumps(
                entries,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ProjectError("recents_unavailable", "recent registry could not be encoded") from exc
        if len(payload) > RECENT_REGISTRY_MAX_BYTES:
            raise ProjectError("recents_unavailable", "recent registry is too large")
        return payload

    def _private_directory_identity(self, path: Path) -> Any:
        try:
            handle, identity = _open_existing(path, directory=True)
        except (ProjectError, OSError) as exc:
            raise ProjectError("recents_unavailable", "recent registry is unavailable") from exc
        handle.close()
        return identity

    def _private_file_identity(self, path: Path) -> Any:
        try:
            handle, identity = _open_existing(path, directory=False)
        except (ProjectError, OSError) as exc:
            raise ProjectError("recents_unavailable", "recent registry is unavailable") from exc
        handle.close()
        return identity

    def _private_revalidate_directory(self, path: Path, expected: Any) -> None:
        current = self._private_directory_identity(path)
        if not expected.same_object(current):
            raise ProjectError("recents_unavailable", "recent registry changed")

    def _private_revalidate_file(self, path: Path, expected: Any) -> None:
        current = self._private_file_identity(path)
        if not expected.same_object(current) or expected.size != current.size:
            raise ProjectError("recents_unavailable", "recent registry changed")

    def _private_unlink(
        self,
        path: Path,
        expected: Any,
        payload: bytes | None = None,
        *,
        expected_hash: str | None = None,
    ) -> bool:
        """Delete an owned registry object through a pinned handle/dir-fd."""

        try:
            if expected_hash is None and payload is not None:
                expected_hash = hashlib.sha256(payload).hexdigest()
            parent_identity = self._private_directory_identity(path.parent)
            return unlink_handle_bound(
                path,
                expected,
                parent_expected=parent_identity,
                expected_sha256=expected_hash,
                max_bytes=(
                    RECENT_REGISTRY_HARD_MAX_BYTES
                    if path == self.registry_path
                    else RECENT_REGISTRY_MAX_BYTES
                ),
            )
        except (ProjectError, OSError, ValueError, TypeError):
            return False

    def _private_cleanup_temp(self, path: Path, expected: Any, payload: bytes) -> None:
        try:
            parent_identity = self._private_directory_identity(path.parent)
            unlink_handle_bound(
                path,
                expected,
                parent_expected=parent_identity,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
                max_bytes=RECENT_REGISTRY_MAX_BYTES,
            )
        except (ProjectError, OSError, ValueError, TypeError):
            return

    def _private_restore(self, payload: bytes) -> None:
        temp = _stage_temp(self.plugin_data_dir, payload)
        temp_identity: Any = None
        try:
            ensure_private_node(temp, directory=False)
            temp_identity = self._private_file_identity(temp)
            _replace_temp(temp, self.registry_path, no_replace=False)
            _fsync_parent(self.plugin_data_dir)
        finally:
            if temp_identity is not None:
                self._private_cleanup_temp(temp, temp_identity, payload)

    def _write_existing_registry(self, payload: bytes) -> None:
        target = self.registry_path
        parent_identity = self._private_directory_identity(self.plugin_data_dir)
        old_handle, old_identity = _open_existing(target, directory=False)
        try:
            old_payload = self.boundary.read_handle_bytes(old_handle, max_bytes=RECENT_REGISTRY_MAX_BYTES)
        finally:
            old_handle.close()
        temp = _stage_temp(self.plugin_data_dir, payload)
        temp_identity: Any = None
        published = False
        try:
            ensure_private_node(temp, directory=False)
            temp_identity = self._private_file_identity(temp)
            self._private_revalidate_directory(self.plugin_data_dir, parent_identity)
            self._private_revalidate_file(target, old_identity)
            _replace_temp(temp, target, no_replace=False)
            published = True
            published_identity = self._private_file_identity(target)
            if not temp_identity.same_object(published_identity):
                raise ProjectError("recents_unavailable", "recent registry publication changed")
            if _read_regular(self.boundary, target, max_bytes=RECENT_REGISTRY_MAX_BYTES) != payload:
                raise ProjectError("recents_unavailable", "recent registry publication changed")
            self._private_revalidate_file(target, published_identity)
            _fsync_parent(self.plugin_data_dir)
        except (OSError, ProjectError) as exc:
            if published:
                with contextlib.suppress(Exception):
                    self._private_restore(old_payload)
            raise ProjectError("recents_unavailable", "recent registry could not be published") from exc
        finally:
            if temp_identity is not None:
                self._private_cleanup_temp(temp, temp_identity, payload)

    def _write_new_registry(self, payload: bytes) -> None:
        parent = self.plugin_data_dir
        parent_identity = self._private_directory_identity(parent)
        temp = _stage_temp(parent, payload)
        temp_identity: Any = None
        published_identity: Any = None
        published = False
        try:
            ensure_private_node(temp, directory=False)
            temp_identity = self._private_file_identity(temp)
            self._private_revalidate_directory(parent, parent_identity)
            _replace_temp(temp, self.registry_path, no_replace=True)
            published = True
            published_identity = self._private_file_identity(self.registry_path)
            if not temp_identity.same_object(published_identity):
                raise ProjectError("recents_unavailable", "recent registry publication changed")
            if _read_regular(self.boundary, self.registry_path, max_bytes=RECENT_REGISTRY_MAX_BYTES) != payload:
                raise ProjectError("recents_unavailable", "recent registry publication changed")
            self._private_revalidate_file(self.registry_path, published_identity)
            _fsync_parent(parent)
        except FileExistsError as exc:
            raise ProjectError("recents_unavailable", "recent registry changed") from exc
        except (OSError, ProjectError) as exc:
            if published and published_identity is not None:
                self._private_unlink(self.registry_path, published_identity, payload)
                with contextlib.suppress(Exception):
                    _fsync_parent(parent)
            raise ProjectError("recents_unavailable", "recent registry could not be published") from exc
        finally:
            if temp_identity is not None:
                self._private_cleanup_temp(temp, temp_identity, payload)
            else:
                cleanup_identity = _capture_staged_temp_identity(temp)
                if cleanup_identity is not None:
                    self._private_cleanup_temp(temp, cleanup_identity, payload)

    def _write_registry(self, entries: list[dict[str, str]]) -> None:
        payload = self._encode_entries(entries)
        if self.registry_path.exists() and not self.registry_path.is_symlink():
            self._write_existing_registry(payload)
        else:
            self._write_new_registry(payload)

    @staticmethod
    def _updated_entries(entries: list[dict[str, str]], entry: dict[str, str]) -> list[dict[str, str]]:
        remaining = [item for item in entries if item["project_id"] != entry["project_id"]]
        remaining.insert(0, entry)
        remaining.sort(key=lambda item: (item["opened_at"], item["project_id"]), reverse=True)
        return remaining[:RECENT_MAX_ENTRIES]

    def _record_locked(self, entry: dict[str, str]) -> None:
        parent_identity = self._private_directory_identity(self.plugin_data_dir)
        try:
            entries, snapshot = self._read_entries()
        except (ProjectError, ValueError, OSError, TypeError, UnicodeError):
            snapshot = None
            try:
                try:
                    initial = self._registry_snapshot()
                except (ProjectError, ValueError, OSError):
                    # The bounded snapshot intentionally rejects oversized
                    # state; only then capture identity/size metadata for a
                    # bounded explicit quarantine.
                    initial = self._registry_snapshot_for_quarantine()
                if initial is None:
                    raise ProjectError("recents_unavailable", "recent registry is unavailable")
                self._quarantine(initial, parent_identity=parent_identity)
            except ProjectError:
                raise
            except (OSError, ValueError, TypeError):
                raise ProjectError("recents_unavailable", "recent registry is unavailable")
            entries = []
        if entries is None:
            entries = []
        if snapshot is None and self.registry_path.exists():
            # A concurrently introduced foreign object must not be replaced.
            raise ProjectError("recents_unavailable", "recent registry changed")
        self._write_registry(self._updated_entries(entries, entry))

    def _record_validated(self, manifest: Mapping[str, Any], directory: Path, *, opened_at: str | None = None) -> str | None:
        timestamp = opened_at or _now_timestamp()
        _timestamp(timestamp)
        entry = {
            "project_id": _project_id(manifest["project_id"]),
            "name": _name(manifest["name"]),
            "directory": self._directory_text(str(directory)),
            "opened_at": timestamp,
        }
        try:
            self._ensure_private_space()
            with self._writer_lock():
                self._record_locked(entry)
        except (ProjectError, OSError, ValueError, TypeError, UnicodeError):
            return RECENT_WARNING
        return None

    def record_recent(
        self,
        project_id: str,
        name: str,
        directory: str | Path,
        opened_at: str | None = None,
    ) -> str | None:
        """Record an already validated conscious create/open action."""
        try:
            manifest = self.project_store.read_project(directory, expected_project_id=project_id)
            if _name(name) != manifest["name"]:
                raise ProjectError("invalid_project", "project identity does not match")
            canonical = self.boundary.validate_project_directory(directory, operation="open")
            return self._record_validated(manifest, canonical, opened_at=opened_at)
        except (ProjectError, OSError, ValueError, TypeError, UnicodeError):
            return RECENT_WARNING

    remember_project = record_recent

    def create_project(self, directory: str | Path, name: str, **kwargs: Any) -> dict[str, Any]:
        result = self.project_store.create_project(directory, name, **kwargs)
        project = result["project"]
        warning = self.record_recent(project["project_id"], project["name"], directory)
        if warning is not None:
            result = dict(result)
            result["warning"] = warning
        return result

    def _moved_from_candidates(self, directory: str | Path) -> list[str]:
        """Where this project lived before: its recent entry, else an approved root holding the same project."""
        try:
            raw = _loads_bounded(_read_regular(self.boundary, self.boundary.validate_project_directory(
                directory, operation="open") / "project.json", max_bytes=MANIFEST_MAX_BYTES))
            project_id = raw.get("project_id") if isinstance(raw, dict) else None
            roots = raw.get("approved_roots") if isinstance(raw, dict) else None
        except (ProjectError, OSError, ValueError, TypeError, UnicodeError):
            return []
        if type(project_id) is not str:
            return []
        candidates: list[str] = []
        try:
            entries, _snapshot = self._read_entries()
        except (ProjectError, OSError, ValueError, TypeError, UnicodeError):
            entries = None
        candidates += [entry["directory"] for entry in entries or () if entry.get("project_id") == project_id]
        for root in roots if isinstance(roots, list) else ():
            if not isinstance(root, str) or not os.path.isabs(root):
                continue  # lane p12-med: a relative root is the project's own folder, never a previous place
            if _local_path.remote_path_reason(root) is not None:
                continue  # lane w34-sec (finding 3): a network / device root is never opened, not even to compare
            try:
                # Lane w34-sec (finding 3): through the boundary (reparse, HERMES_HOME) and its size bound.
                folder = self.boundary.validate_project_directory(root, operation="open")
                other = _loads_bounded(_read_regular(self.boundary, folder / "project.json", max_bytes=MANIFEST_MAX_BYTES))
            except (ProjectError, OSError, ValueError, TypeError, UnicodeError):
                continue
            if isinstance(other, dict) and other.get("project_id") == project_id:
                candidates.append(root)
        return candidates

    def open_project(self, directory: str | Path) -> dict[str, Any]:
        # lane w11-backend M-3: a moved/copied project re-bases its own paths before it is read.
        moved_from = None
        candidates = self._moved_from_candidates(directory)
        # lane p12-med: also without a known previous place (another machine): the stored paths name it.
        moved_from = self.project_store.rebase_moved_project(directory, candidates)
        manifest = self.project_store.read_project(directory)
        shoot = self.project_store.read_shoot_state(directory, project_id=manifest["project_id"])
        canonical = self.boundary.validate_project_directory(directory, operation="open")
        warning = self._record_validated(manifest, canonical)
        result: dict[str, Any] = {
            "project": {"project_id": manifest["project_id"], "name": manifest["name"], "revision": manifest["revision"]},
            "shoot": shoot,
        }
        if warning is not None:
            result["warning"] = warning
        if moved_from is not None:
            result["moved_from"] = moved_from  # backend-only: the route re-bases private indexes; never public
        return result

    def open_recent(self, project_id: str) -> dict[str, Any]:
        if not isinstance(project_id, str) or not _PROJECT_ID_RE.fullmatch(project_id):
            raise ProjectError("invalid_project", "project identity is invalid")
        try:
            self._ensure_private_space()
            with self._writer_lock():
                parent_identity = self._private_directory_identity(self.plugin_data_dir)
                try:
                    entries, snapshot = self._read_entries()
                except (ProjectError, ValueError, OSError, TypeError, UnicodeError) as exc:
                    try:
                        corrupt = self._registry_snapshot()
                    except (ProjectError, ValueError, OSError):
                        corrupt = self._registry_snapshot_for_quarantine()
                    if corrupt is None:
                        raise ProjectError("project_not_found", "recent project is unavailable") from exc
                    try:
                        self._quarantine(corrupt, parent_identity=parent_identity)
                        self._write_new_registry(self._encode_entries([]))
                    except (ProjectError, OSError, ValueError, TypeError, UnicodeError) as quarantine_exc:
                        raise ProjectError("project_unavailable", "recent project is unavailable") from quarantine_exc
                    raise ProjectError("project_unavailable", "recent project is unavailable") from exc
                if entries is None or snapshot is None:
                    raise ProjectError("project_not_found", "recent project is unavailable")
                entry = next((item for item in entries if item["project_id"] == project_id), None)
                if entry is None:
                    raise ProjectError("project_not_found", "recent project is unavailable")
                try:
                    manifest = self.project_store.read_project(
                        entry["directory"], expected_project_id=project_id
                    )
                except (ProjectError, OSError, ValueError, TypeError, UnicodeError) as exc:
                    # Distinguish a truly absent manifest from an offline folder,
                    # malformed project, permission failure or unsafe path.
                    try:
                        directory = self.boundary.validate_project_directory(entry["directory"], operation="open")
                        (directory / "project.json").lstat()
                    except FileNotFoundError:
                        raise ProjectError("project_manifest_missing", "project manifest is missing") from exc
                    except (ProjectError, OSError, ValueError, TypeError, UnicodeError):
                        pass
                    raise ProjectError("project_unavailable", "recent project is unavailable") from exc
                if manifest["name"] != entry["name"]:
                    raise ProjectError("project_unavailable", "recent project is unavailable")
                timestamp = _now_timestamp()
                if timestamp <= entry["opened_at"]:
                    timestamp = _next_timestamp(entry["opened_at"])
                refreshed = dict(entry)
                refreshed["opened_at"] = timestamp
                warning: str | None = None
                try:
                    self._record_locked(refreshed)
                except (ProjectError, OSError, ValueError, TypeError, UnicodeError):
                    warning = RECENT_WARNING
                result: dict[str, Any] = {
                    "project_id": manifest["project_id"],
                    "name": manifest["name"],
                    "revision": manifest["revision"],
                }
                if warning is not None:
                    result["warning"] = warning
                return result
        except ProjectError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            raise ProjectError("project_unavailable", "recent project is unavailable") from exc


__all__ = [
    "MANIFEST_MAX_BYTES",
    "MANIFEST_SCHEMA",
    "ProjectManifestError",
    "ProjectStore",
    "RecentProjectStore",
    "read_project",
]
