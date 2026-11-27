"""The Continuo frame's agent bridge: which page is open, the project, and the Script page.

Same transport idea as the Image and Music bridges (file-backed under the profile's private
plugin data, path-free, bounded): the renderer publishes one small snapshot per window and
acknowledges named actions it applied with the handler its own bar button uses. Nothing here
starts work. Actions: ``open_page`` (Start or one of the six pages); the Script page's
``script_*`` commands (the rows it adds to Hermes' palette, run by the same handler); the Media column (lane
w2-shell, G1) ``media_filter`` {bin?, types?, query?, sort?}, ``media_select`` {id} and
``media_toggle`` {visible}, applied by the handlers its own controls use. The state carries
``media`` {visible, bin, types, query, sort, min_rating, count, total, selected} on the pages
that have the column (Image, Video, Audio, Cut), else null.
"""
from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
# Lane w47-q3: the one Windows file-sharing retry rule for replaced state files (studio_core/win_io.py, stdlib, by path).
import importlib.util as _win_io_import
_win_io_spec = _win_io_import.spec_from_file_location("film_suite_suite_bridge_win_io", Path(__file__).resolve().parents[2] / "studio_core" / "win_io.py")
_win_io = _win_io_import.module_from_spec(_win_io_spec)
_win_io_spec.loader.exec_module(_win_io)
from typing import Any, Callable

STATE_SCHEMA = "studio.suite-ui-state/1"
ACTION_SCHEMA = "studio.suite-ui-action/1"
ACK_SCHEMA = "studio.suite-ui-ack/1"
PAGES = ("start", "script", "music", "image", "shoot", "timeline", "deliver")
# The approved names the bar prints for the internal ids (Shoot -> Video, Music -> Audio, Timeline -> Cut).
PAGE_LABELS = {"start": "Start", "script": "Script", "music": "Audio", "image": "Image", "shoot": "Video", "timeline": "Cut", "deliver": "Deliver"}
# open_project / no_project (FRM-028): the Start screen's Recent tile and Loose work, the same handlers as their clicks.
# New project and Open folder need the artist's own folder pick and have no action (consent rule).
ACTIONS = frozenset({"open_page", "media_filter", "media_select", "media_toggle", "open_project", "no_project"})
# lane w17-pcut (CUT-121): the Cut page's docked inspector, hidden or shown like its ✕ / Ctrl+[ / bar button.
ACTIONS = ACTIONS | frozenset({"cut_inspector"})
MEDIA_BINS = ("all", "kept", "cut", "uncut")
MEDIA_TYPES = ("image", "video", "audio")
MEDIA_SORTS = ("newest", "oldest", "rating", "kept", "name")  # w24-media: Name (Premiere's Icon View Name sort)
# Lane p11-med item 1 (MED-053): a folder file is selected by its path-free ref folder:<32 hex> (/media/list ref).
_MEDIA_ITEM_RE = re.compile(r"^(?:imgjob_[0-9a-f]{32}|folder:[0-9a-f]{32}|studio:(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}))$")
# The Script page's commands (lane w2-script): action -> payload keys (required, optional).
SCRIPT_ELEMENTS = ("heading", "action", "character", "parenthetical", "dialogue", "transition", "shot")
SCRIPT_ACTIONS: dict[str, tuple[set[str], set[str]]] = {
    "script_set_element": ({"type"}, set()), "script_toggle_dual": (set(), set()), "script_previous_scene": (set(), set()),
    "script_next_scene": (set(), set()), "script_go_to_scene": ({"scene"}, set()), "script_find": (set(), set()),
    "script_find_next": ({"query"}, {"backwards"}), "script_replace": ({"query", "replacement"}, set()),
    "script_replace_all": ({"query", "replacement"}, set()), "script_undo": (set(), set()), "script_redo": (set(), set()),
    "script_paste_fountain": ({"text"}, set()), "script_focus_mode": (set(), {"on"}), "script_typewriter": (set(), {"on"}),
    "script_shots": (set(), {"on"}),  # View > Shots: the shots at the margin (lane p5b-scr, charter Script decision 5)
    "script_export": ({"format"}, set()),
    "script_frame": ({"shot"}, set()),  # lane w5-script: + Frame of a shot without picture (opens Image Create)
    # lane p13-scr (HER-005): the "Hermes did" line's Undo and Dismiss on the Script page.
    "script_hermes_undo": (set(), set()), "script_hermes_dismiss": (set(), set()),
    "script_inspector": (set(), {"on"}),  # lane w17-pgem (SCR-075): the Script inspector shown or hidden (✕, Ctrl+[)
}
# lane w14-scr (SCR-071, board D-1 Tafel 1): Hermes' changes reviewed one by one ({change}: an id of
# script.hermes_did.changes; none = the current change) and all at once.
SCRIPT_ACTIONS.update({"script_hermes_undo": (set(), {"change"}), "script_hermes_accept": (set(), {"change"}),
                       "script_hermes_accept_all": (set(), set()), "script_hermes_undo_all": (set(), set())})
SCRIPT_VIEWS = ("write", "board", "previz")  # w28-fix item 5c (HER-TXT-3): the one table of Script views (check + description)
SCRIPT_ACTIONS.update({"script_view": ({"view"}, set()), "script_shot": ({"shot"}, set())})  # lane w20-plan: Write | Board (planning board round 2, variant A)
SCRIPT_ACTIONS.update({"script_previz_keep": ({"shot"}, set())})  # lane w20-plan2: Previz Keep frame (the shot's view as a Previz picture)
ALL_ACTIONS = ACTIONS | frozenset(SCRIPT_ACTIONS)
_ACTION_SEQ = itertools.count(time.perf_counter_ns())
ACTION_TTL_SECONDS = 30.0
MAX_SCENES = 2000
# The Start screen in the snapshot (FRM-028): its Recent tiles and whether a project is being opened.
START_BUSY = ("", "restore", "open")
MAX_RECENT = 50
# A window silent this long is gone (HER-045): its state file and its action folders are removed on read.
STALE_WINDOW_SECONDS = 3600.0
_WINDOW_RE = re.compile(r"^win_[0-9a-f]{32}$")
_ACTION_RE = re.compile(r"^act_[0-9a-f]{32}$")
_PROJECT_RE = re.compile(r"^prj_[0-9a-f]{32}$")
_SCENE_ID_RE = re.compile(r"^s-[0-9a-f]{8}$")
_ABSOLUTE_PATH_RE = re.compile(r"(^|[\s\"'(=])([A-Za-z]:[\\/]|\\\\[^\\]|/(Users|home|tmp|var|mnt)/)")


class SuiteBridgeError(ValueError):
    """A bridge envelope is malformed."""


def _fail(message: str) -> None:
    raise SuiteBridgeError(message)


def _keys(value: Any, where: str, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - set(optional):
        _fail(f"{where} fields are invalid")
    return value


def _text(value: Any, where: str, maximum: int, *, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value):
        _fail(f"{where} is invalid")
    if _ABSOLUTE_PATH_RE.search(value):
        _fail(f"{where} must not contain a filesystem path")
    return value


def _int(value: Any, where: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        _fail(f"{where} is invalid")
    return value


def _media_types(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > len(MEDIA_TYPES) or len(set(value)) != len(value) or any(v not in MEDIA_TYPES for v in value):
        _fail(f"{where} is invalid")
    return value


def _validate_media(media: Any) -> None:
    _keys(media, "media", {"visible", "bin", "types", "query", "sort", "min_rating", "count", "total", "selected"})
    if not isinstance(media["visible"], bool) or media["bin"] not in MEDIA_BINS or media["sort"] not in MEDIA_SORTS:
        _fail("media is invalid")
    _media_types(media["types"], "media.types")
    _text(media["query"], "media.query", 200)
    _int(media["min_rating"], "media.min_rating", 0, 5)
    _int(media["count"], "media.count", 0, 2**31)
    _int(media["total"], "media.total", 0, 2**31)
    if media["selected"] is not None and (not isinstance(media["selected"], str) or not _MEDIA_ITEM_RE.fullmatch(media["selected"])):
        _fail("media.selected is invalid")


def _validate_start(start: Any) -> None:
    _keys(start, "start", {"recent", "busy"})
    if start["busy"] not in START_BUSY:
        _fail("start.busy is invalid")
    if not isinstance(start["recent"], list) or len(start["recent"]) > MAX_RECENT:
        _fail("start.recent is invalid")
    for index, project in enumerate(start["recent"]):
        _keys(project, f"start.recent[{index}]", {"project_id", "name", "available"})
        if not isinstance(project["project_id"], str) or not _PROJECT_RE.fullmatch(project["project_id"]):
            _fail(f"start.recent[{index}].project_id is invalid")
        _text(project["name"], f"start.recent[{index}].name", 200)
        if not isinstance(project["available"], bool):
            _fail(f"start.recent[{index}].available is invalid")


def validate_state(state: Any) -> dict[str, Any]:
    _keys(state, "state", {"schema", "page", "screen", "project", "surface_visible", "script"}, {"media", "start", "cut", "video"})
    if state["schema"] != STATE_SCHEMA:
        _fail("state schema is invalid")
    if state["page"] not in PAGES or state["screen"] not in {"start", "page", "media"}:
        _fail("page is invalid")
    if not isinstance(state["surface_visible"], bool):
        _fail("surface_visible is invalid")
    project = state["project"]
    if project is not None:
        _keys(project, "project", {"project_id", "name"})
        if not isinstance(project["project_id"], str) or not _PROJECT_RE.fullmatch(project["project_id"]):
            _fail("project.project_id is invalid")
        _text(project["name"], "project.name", 200)
    script = state["script"]
    if script is not None:
        _keys(script, "script", {"state", "revision", "scenes"}, {"title", "error", "current_scene", "element", "focus_mode", "typewriter", "shots", "inspector", "numbers_locked", "hermes_did"} | {"view", "shot"})  # view, shot: lane w20-plan (appended)
        if "current_scene" in script and script["current_scene"] is not None and (not isinstance(script["current_scene"], str) or not _SCENE_ID_RE.fullmatch(script["current_scene"])):
            _fail("script.current_scene is invalid")
        if "element" in script and script["element"] not in SCRIPT_ELEMENTS + ("centered", "lyric", "page_break"):
            _fail("script.element is invalid")
        if "view" in script and script["view"] not in ("write", "board", "previz"):  # lane w20-plan: the Script centre view (w20-plan2: previz)
            _fail("script.view is invalid")
        if script.get("shot") is not None and (not isinstance(script["shot"], str) or not re.fullmatch(r"sh-[0-9a-f]{8}", script["shot"])):  # lane w20-plan
            _fail("script.shot is invalid")
        for flag in ("focus_mode", "typewriter", "shots", "numbers_locked", "inspector"):
            if flag in script and not isinstance(script[flag], bool):
                _fail(f"script.{flag} is invalid")
        if script["state"] not in {"loading", "none", "ready", "error", "no_project"}:
            _fail("script.state is invalid")
        _int(script["revision"], "script.revision", 0, 2**53 - 1)
        if "title" in script:
            _text(script["title"], "script.title", 400)
        if "error" in script:
            _text(script["error"], "script.error", 240)
        if script.get("hermes_did") is not None:  # lane p13-scr: the "Hermes did" line {label, at, before, after}
            did = _keys(script["hermes_did"], "script.hermes_did", {"label", "at", "before", "after"}, {"scene", "current", "changes"})  # scene: lane p13b-scr, the scene carrying the H mark
            _text(did["label"], "script.hermes_did.label", 120)
            _text(did["at"], "script.hermes_did.at", 5)
            _text(did["before"], "script.hermes_did.before", 80)
            _text(did["after"], "script.hermes_did.after", 80)
            if did.get("scene") is not None and (not isinstance(did["scene"], str) or not _SCENE_ID_RE.fullmatch(did["scene"])):
                _fail("script.hermes_did.scene is invalid")
            # lane w14-scr (SCR-071): the open changes [{id, scene, before, after}] and the current one's index.
            if "current" in did:
                _int(did["current"], "script.hermes_did.current", 0, 199)
            if "changes" in did:
                if not isinstance(did["changes"], list) or len(did["changes"]) > 200:
                    _fail("script.hermes_did.changes is invalid")
                for k, change in enumerate(did["changes"]):
                    _keys(change, f"script.hermes_did.changes[{k}]", {"id", "scene", "before", "after"})
                    _text(change["id"], f"script.hermes_did.changes[{k}].id", 24, empty=False)
                    if not isinstance(change["scene"], str) or not _SCENE_ID_RE.fullmatch(change["scene"]):
                        _fail(f"script.hermes_did.changes[{k}].scene is invalid")
                    _text(change["before"], f"script.hermes_did.changes[{k}].before", 200)
                    _text(change["after"], f"script.hermes_did.changes[{k}].after", 200)
        if not isinstance(script["scenes"], list) or len(script["scenes"]) > MAX_SCENES:
            _fail("script.scenes is invalid")
        for index, scene in enumerate(script["scenes"]):
            _keys(scene, f"script.scenes[{index}]", {"id", "number", "heading"}, {"omitted", "act"})  # act: lane p5d-scr, decision 14
            if "act" in scene:
                _text(scene["act"], f"script.scenes[{index}].act", 120)
            if not isinstance(scene["id"], str) or not _SCENE_ID_RE.fullmatch(scene["id"]):
                _fail(f"script.scenes[{index}].id is invalid")
            _text(scene["number"], f"script.scenes[{index}].number", 16)
            _text(scene["heading"], f"script.scenes[{index}].heading", 400)
            if "omitted" in scene and not isinstance(scene["omitted"], bool):
                _fail(f"script.scenes[{index}].omitted is invalid")
    if state.get("media") is not None:
        _validate_media(state["media"])
    if state.get("start") is not None:
        _validate_start(state["start"])
    if state.get("cut") is not None:
        _validate_cut(state["cut"])
    if state.get("video") is not None:
        _validate_video(state["video"])
    return json.loads(json.dumps(state, ensure_ascii=False, sort_keys=True))


_TAKE_ID_MAX = 120


def _validate_video(video: Any) -> None:
    """lane w35-ui (GEM-222): the take on the Video stage {take_id, barely_moves, motion_activity} or null. The stage no
    longer prints "This take barely moves"; Hermes reads it here and re-rolls the seed itself."""
    _keys(video, "video", {"take"})
    take = video["take"]
    if take is None:
        return
    _keys(take, "video.take", {"take_id", "barely_moves", "motion_activity"})
    _text(take["take_id"], "video.take.take_id", _TAKE_ID_MAX, empty=False)
    if not isinstance(take["barely_moves"], bool):
        _fail("video.take.barely_moves is invalid")
    motion = take["motion_activity"]
    if motion is not None and (isinstance(motion, bool) or not isinstance(motion, (int, float)) or motion != motion or abs(motion) > 1e9):
        _fail("video.take.motion_activity is invalid")


def _validate_cut(cut: Any) -> None:
    """lane w17-pcut: the Cut page's view state {inspector_visible, inspector_docked} (CUT-121) and its counts
    {clip_count, sound_count, song} (CUT-124: clips = picture clips, sounds named apart)."""
    _keys(cut, "cut", {"inspector_visible", "inspector_docked"}, {"clip_count", "sound_count", "song"})
    for flag in ("inspector_visible", "inspector_docked", "song"):
        if flag in cut and not isinstance(cut[flag], bool):
            _fail(f"cut.{flag} is invalid")
    for count in ("clip_count", "sound_count"):
        if count in cut:
            _int(cut[count], f"cut.{count}", 0, 100000)


def _script_payload(action: str, payload: Any) -> dict[str, Any]:
    required, optional = SCRIPT_ACTIONS[action]
    _keys(payload, "payload", required, optional)
    if "type" in payload and payload["type"] not in SCRIPT_ELEMENTS:
        _fail(f"type must be one of {', '.join(SCRIPT_ELEMENTS)}")
    if "scene" in payload and (not isinstance(payload["scene"], str) or not _SCENE_ID_RE.fullmatch(payload["scene"])):
        _fail("scene must be a scene id (s-xxxxxxxx)")
    if "shot" in payload and (not isinstance(payload["shot"], str) or not re.fullmatch(r"sh-[0-9a-f]{8}", payload["shot"])):
        _fail("shot must be a shot id (sh-xxxxxxxx)")
    if action == "script_view" and payload.get("view") not in SCRIPT_VIEWS:  # lane w20-plan (w20-plan2: previz)
        _fail("view must be write, board or previz")
    if "format" in payload and payload["format"] not in ("pdf", "fountain", "fdx"):
        _fail("format must be pdf, fountain or fdx")
    for key, maximum in (("query", 400), ("replacement", 400), ("text", 65536)):
        if key in payload and (not isinstance(payload[key], str) or len(payload[key]) > maximum or (key == "query" and not payload[key])):
            _fail(f"{key} is invalid")
    for key in ("on", "backwards"):
        if key in payload and not isinstance(payload[key], bool):
            _fail(f"{key} must be true or false")
    return dict(payload)


def validate_action(action: Any, payload: Any) -> dict[str, Any]:
    if action in SCRIPT_ACTIONS:
        return _script_payload(action, payload)
    if action not in ACTIONS:
        _fail("action is unsupported")
    if action == "media_filter":
        _keys(payload, "payload", set(), {"bin", "types", "query", "sort"})
        if not payload:
            _fail("media_filter needs bin, types, query or sort")
        if "bin" in payload and payload["bin"] not in MEDIA_BINS:
            _fail(f"bin must be one of {', '.join(MEDIA_BINS)}")
        if "types" in payload:
            _media_types(payload["types"], "types")
        if "query" in payload:
            _text(payload["query"], "query", 200)
        if "sort" in payload and payload["sort"] not in MEDIA_SORTS:
            _fail(f"sort must be one of {', '.join(MEDIA_SORTS)}")
        return dict(payload)
    if action == "media_select":
        _keys(payload, "payload", {"id"})
        if not isinstance(payload["id"], str) or not _MEDIA_ITEM_RE.fullmatch(payload["id"]):
            _fail("id must be a Media item id (imgjob_…, studio:… or folder:…)")
        return {"id": payload["id"]}
    if action == "open_project":
        _keys(payload, "payload", {"project_id"})
        if not isinstance(payload["project_id"], str) or not _PROJECT_RE.fullmatch(payload["project_id"]):
            _fail("project_id must be a project id (prj_…) from start.recent")
        return {"project_id": payload["project_id"]}
    if action == "no_project":
        _keys(payload, "payload", set())
        return {}
    if action == "cut_inspector":  # lane w17-pcut (CUT-121)
        _keys(payload, "payload", {"visible"})
        if not isinstance(payload["visible"], bool):
            _fail("visible must be true or false")
        return {"visible": payload["visible"]}
    if action == "media_toggle":
        _keys(payload, "payload", {"visible"})
        if not isinstance(payload["visible"], bool):
            _fail("visible must be true or false")
        return {"visible": payload["visible"]}
    _keys(payload, "payload", {"page"})
    if payload["page"] not in PAGES:
        _fail(f"page must be one of {', '.join(PAGES)}")
    return {"page": payload["page"]}


def validate_ack(ack: Any) -> dict[str, Any]:
    _keys(ack, "ack", {"ok", "reason_code", "message", "applied"})
    if not isinstance(ack["ok"], bool):
        _fail("ack.ok is invalid")
    if ack["reason_code"] is not None and (not isinstance(ack["reason_code"], str) or not re.fullmatch(r"[a-z0-9_]{1,64}", ack["reason_code"])):
        _fail("ack.reason_code is invalid")
    if ack["message"] is not None:
        _text(ack["message"], "ack.message", 240)
    if ack["applied"] is not None and (not isinstance(ack["applied"], dict) or len(json.dumps(ack["applied"])) > 2048):
        _fail("ack.applied is invalid")
    return json.loads(json.dumps(ack))


def _window(value: Any) -> str:
    if not isinstance(value, str) or not _WINDOW_RE.fullmatch(value):
        _fail("window_id is invalid")
    return value


def _action_id(value: Any) -> str:
    if not isinstance(value, str) or not _ACTION_RE.fullmatch(value):
        _fail("action_id is invalid")
    return value


def _read(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(_win_io.read_text(path, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        _win_io.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class SuiteBridgeStore:
    def __init__(self, root: Path, clock: Callable[[], float] | None = None) -> None:
        self.root = Path(root)
        self.clock = clock or time.time

    def _state(self, window: str) -> Path:
        return self.root / "states" / f"{window}.json"

    def _pending(self, window: str) -> Path:
        return self.root / "actions" / window / "pending"

    def _ack(self, window: str, action_id: str) -> Path:
        return self.root / "actions" / window / "acks" / f"{action_id}.json"

    def write_snapshot(self, window_id: str, state: dict[str, Any]) -> dict[str, Any]:
        window = _window(window_id)
        state = validate_state(state)
        previous = _read(self._state(window))
        revision = int(previous.get("revision", 0)) if previous else 0
        if previous is None or previous.get("state") != state:
            revision += 1
        record = {"schema": STATE_SCHEMA, "window_id": window, "revision": revision, "observed_at": float(self.clock()), "state": state}
        _write(self._state(window), record)
        return record

    def _forget(self, path: Path) -> None:
        """Remove a gone window: its state file and its pending actions and acks."""
        path.unlink(missing_ok=True)
        if _WINDOW_RE.fullmatch(path.stem):
            shutil.rmtree(self.root / "actions" / path.stem, ignore_errors=True)

    def snapshots(self, max_age_seconds: float = 20.0, forget_after_seconds: float = STALE_WINDOW_SECONDS) -> list[dict[str, Any]]:
        folder = self.root / "states"
        now = float(self.clock())
        rows = []
        for path in sorted(folder.glob("win_*.json")) if folder.is_dir() else []:
            record = _read(path)
            try:
                observed = float(record["observed_at"]) if record else path.stat().st_mtime
            except (OSError, KeyError, TypeError, ValueError):
                observed = now
            if now - observed > forget_after_seconds:
                self._forget(path)
                continue
            if not record or record.get("schema") != STATE_SCHEMA:
                continue
            try:
                state = validate_state(record.get("state"))
                age = max(0.0, now - float(record["observed_at"]))
                rows.append({"window_id": _window(record.get("window_id")), "revision": int(record["revision"]), "age_seconds": round(age, 3),
                             "connected": age <= max_age_seconds, "state": state})
            except (SuiteBridgeError, KeyError, TypeError, ValueError):
                continue
        rows.sort(key=lambda row: (row["connected"], -row["age_seconds"]), reverse=True)
        return rows

    def enqueue(self, window_id: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        window = _window(window_id)
        payload = validate_action(action, payload)
        now = float(self.clock())
        # seq: actions queued within one clock tick (Windows clocks tick in ~1-16 ms) keep their order.
        seq = next(_ACTION_SEQ)
        record = {"schema": ACTION_SCHEMA, "action_id": "act_" + uuid.uuid4().hex, "window_id": window, "action": action,
                  "payload": payload, "created_at": now, "seq": seq, "expires_at": now + ACTION_TTL_SECONDS}
        _write(self._pending(window) / f"{record['action_id']}.json", record)
        return record

    def pending(self, window_id: str) -> list[dict[str, Any]]:
        window = _window(window_id)
        folder = self._pending(window)
        now = float(self.clock())
        rows = []
        for path in sorted(folder.glob("act_*.json")) if folder.is_dir() else []:
            record = _read(path)
            try:
                if not record or record.get("schema") != ACTION_SCHEMA or float(record["expires_at"]) <= now:
                    path.unlink(missing_ok=True)
                    continue
                validate_action(record.get("action"), record.get("payload"))
            except (SuiteBridgeError, KeyError, TypeError, ValueError):
                continue
            rows.append(record)
        rows.sort(key=lambda row: (row["created_at"], row.get("seq", 0)))
        return rows

    def ack(self, window_id: str, action_id: str, ack: dict[str, Any]) -> dict[str, Any]:
        window, action = _window(window_id), _action_id(action_id)
        record = {"schema": ACK_SCHEMA, "action_id": action, "window_id": window, **validate_ack(ack)}
        _write(self._ack(window, action), record)
        (self._pending(window) / f"{action}.json").unlink(missing_ok=True)
        return record

    def consume_ack(self, window_id: str, action_id: str) -> dict[str, Any] | None:
        path = self._ack(_window(window_id), _action_id(action_id))
        record = _read(path)
        if record is not None:
            path.unlink(missing_ok=True)
        return record

    def withdraw(self, window_id: str, action_id: str) -> bool:
        path = self._pending(_window(window_id)) / f"{_action_id(action_id)}.json"
        existed = path.exists()
        path.unlink(missing_ok=True)
        return existed


# --- GPU line (read-only) ----------------------------------------------------------------------
GPU_SCHEMA = "studio.gpu-status/1"
GPU_CACHE_SECONDS = 2.0
GPU_TIMEOUT_SECONDS = 3.0
_GPU_CACHE: dict[str, Any] = {"at": None, "value": None}


def parse_nvidia_smi(text: str) -> list[dict[str, Any]]:
    """``name, memory.used, memory.total`` CSV lines (noheader, nounits) -> GPUs in MiB."""
    gpus = []
    for line in str(text or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        name, used, total = parts
        try:
            used_mb, total_mb = int(float(used)), int(float(total))
        except ValueError:
            continue
        if total_mb <= 0 or used_mb < 0:
            continue
        gpus.append({"name": name[:120], "memory_used_mb": used_mb, "memory_total_mb": total_mb})
    return gpus


def parse_apple_gpu(ioreg_text: str | None, memsize_text: str | None) -> list[dict[str, Any]]:
    """Apple Silicon (PR #2 by @ShawnCRMer): ``ioreg -r -d1 -c IOAccelerator`` + ``sysctl -n hw.memsize`` -> one GPU
    in MiB.  The GPU shares the machine's unified memory: the total is the RAM size, the used figure the
    accelerator's "In use system memory" (what Metal holds, ComfyUI included).  Anything unreadable -> no GPU."""
    text = str(ioreg_text or "")
    name_match = re.search(r'"model"\s*=\s*"([^"]{1,120})"', text)
    used_match = re.search(r'"In use system memory"\s*=\s*(\d+)', text)
    try:
        total_mb = int(str(memsize_text or "").strip()) // 2**20
    except ValueError:
        return []
    if not name_match or not used_match or total_mb <= 0:
        return []
    used_mb = int(used_match.group(1)) // 2**20
    return [{"name": name_match.group(1), "memory_used_mb": min(used_mb, total_mb), "memory_total_mb": total_mb,
             "unified": True}]


def gpu_status(run: Callable[[list[str], float], str | None], clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """The VRAM line, cached 2 s. ``run(argv, timeout)`` returns stdout or None (missing/failed)."""
    now = clock()
    cached_at = _GPU_CACHE["at"]
    if cached_at is not None and 0 <= now - cached_at < GPU_CACHE_SECONDS and _GPU_CACHE["value"] is not None:
        return _GPU_CACHE["value"]
    if sys.platform == "darwin":
        gpus = parse_apple_gpu(run(["/usr/sbin/ioreg", "-r", "-d1", "-c", "IOAccelerator"], GPU_TIMEOUT_SECONDS),
                               run(["/usr/sbin/sysctl", "-n", "hw.memsize"], GPU_TIMEOUT_SECONDS))
        source = "ioreg"
    else:
        out = run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total", "--format=csv,noheader,nounits"], GPU_TIMEOUT_SECONDS)
        gpus = parse_nvidia_smi(out) if out else []
        source = "nvidia-smi"
    value = {"schema": GPU_SCHEMA, "available": bool(gpus), "gpus": gpus, "source": source, "cached_seconds": GPU_CACHE_SECONDS}
    _GPU_CACHE["at"], _GPU_CACHE["value"] = now, value
    return value



# Lane w19-g7hermes item 4 (HER-052; reference HER-051 music_bridge.ACTION_PAYLOADS): ONE table of every frame action's
# payload, plus SCRIPT_ACTIONS (the Script commands' own key table). validate_action checks key sets against them before
# the per-action checks, and the continuo_suite_action description is built from them (payload_description).
ACTION_PAYLOADS: dict[str, tuple[tuple[str, bool, str], ...]] = {
    "open_page": (("page", True, "|".join(PAGES)),),
    "open_project": (("project_id", True, "prj_... from start.recent"),),
    "no_project": (),
    "media_filter": (("bin", False, "|".join(MEDIA_BINS)), ("types", False, "[" + "|".join(MEDIA_TYPES) + "]"),
                     ("query", False, "text"), ("sort", False, "|".join(MEDIA_SORTS))),
    "media_select": (("id", True, "imgjob_...|studio:...|folder:<32 hex>"),),
    "media_toggle": (("visible", True, "bool"),),
    "cut_inspector": (("visible", True, "bool"),),
}
_SCRIPT_VALUES = {"type": "|".join(SCRIPT_ELEMENTS), "scene": "s-xxxxxxxx", "shot": "sh-xxxxxxxx", "format": "pdf|fountain|fdx",
                  "query": "text", "replacement": "text", "text": "Fountain", "on": "bool", "backwards": "bool",
                  "change": "id from script.hermes_did.changes", "view": "|".join(SCRIPT_VIEWS)}


def _action_keys(action: str) -> tuple[tuple[str, bool, str], ...]:
    if action in SCRIPT_ACTIONS:
        required, optional = SCRIPT_ACTIONS[action]
        return tuple((key, key in required, _SCRIPT_VALUES.get(key, "value")) for key in (*sorted(required), *sorted(optional)))
    return ACTION_PAYLOADS.get(action, ())


def payload_spec(action: str) -> str:
    keys = ", ".join(f"{key}{'' if required else '?'}: {value}" for key, required, value in _action_keys(action))
    return f"{action} {{{keys}}}"


def payload_description() -> str:
    """The payload of every suite action, for the tool description (actions in name order)."""
    with_payload = "; ".join(payload_spec(action) for action in sorted(ALL_ACTIONS) if _action_keys(action))
    without = ", ".join(sorted(action for action in ALL_ACTIONS if not _action_keys(action)))
    return f"Payload per action (? = optional): {with_payload}. No payload ({{}}): {without}."


_validate_action_before_w19 = validate_action


def validate_action(action: Any, payload: Any) -> dict[str, Any]:  # noqa: F811 - append-only extension
    if action in ALL_ACTIONS and isinstance(payload, dict):
        spec = _action_keys(action)
        keys, required = set(payload), {key for key, needed, _value in spec if needed}
        if not required <= keys or keys - {key for key, _needed, _value in spec}:
            _fail(f"{action} payload is invalid: {payload_spec(action)}")
    return _validate_action_before_w19(action, payload)
