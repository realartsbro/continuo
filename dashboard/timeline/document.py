"""The persisted timeline envelope and the agent-facing apply summary.

``state/timeline.json`` is ``{schema, project_id, revision, updated_at, label, by,
state}``: the project envelope of ``state/shoot.json`` plus the label and author of
the revision (an undo step reads "Hermes: Blade" in the history; lane w39-words: timeline/words.py). Older revisions
are kept beside it as ``timeline.json.r<revision>`` (last ``KEEP_REVISIONS``).

Since lane w8-agent an envelope also holds ``changes`` (``changes_of``: the created,
removed and changed item ids and their counts) and ``reason`` (why, at most
``REASON_MAX`` characters, "" when none was given). Both are optional on read: an
envelope written before them reads back exactly as it was written.

``summarize`` turns a before/after pair into what an agent needs instead of the
whole document: created/removed ids, the changed items, and ``placed_refs`` (source
refs no timeline referenced before), so Media can mark a take "in cut" and keep it.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .schema import SCHEMA, SCHEMA_1_1, SCHEMAS, TimelineError, fail, schema_of, uses_1_1, validate_state

MAX_BYTES = 4 * 1024 * 1024
KEEP_REVISIONS = 100
_PROJECT_RE = re.compile(r"^prj_[0-9a-f]{32}$")
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ENVELOPE_KEYS = {"schema", "project_id", "revision", "updated_at", "label", "by", "state"}
OPTIONAL_KEYS = {"changes", "reason"}  # lane w8-agent: absent in envelopes written before it
REASON_MAX = 200
_CHANGE_KINDS = ("created", "removed", "changed")

# Stable error codes -> (retryable, what to do next). ``retryable`` means the same
# intent can succeed after the named step, never that the same bytes will.
ERRORS: dict[str, tuple[bool, str]] = {
    "timeline_conflict": (True, "The timeline changed since this revision."),
    "invalid_timeline": (False, "The named field is invalid; the document was not saved."),
    "invalid_op": (False, "The named argument of the op is invalid."),
    "unknown_op": (False, "Verb not among those in the tool description."),
    "not_found": (True, "No such id in the current timeline."),
    "locked": (False, "The artist locked the track."),
    "overlap": (False, "The overlay track holds a clip there; overwrite replaces it."),
    "not_on_storyline": (False, "Overwrite is for overlay tracks; the storyline takes insert or append."),
    "not_adjacent": (True, "Roll needs the clip directly after the left one."),
    "nothing_to_split": (True, "Frame not strictly inside a clip."),
    "out_of_range": (False, "Frame outside the timeline."),
    "take_too_short": (False, "Take shorter than the clip."),
    "no_music_lane": (False, "No music lane (place_music places one)."),
    "linked": (False, "Sound linked to a picture clip (unlink_sound unlinks it)."),
    "track_full": (False, "The cut has three sound tracks (A1-A3), named by track or role."),
    "not_empty": (False, "The track holds sound clips."),
    "no_sound": (False, "The picture clip's take or file has no sound."),
    "already_linked": (False, "The picture's sound is already on a sound track."),
    "no_handles": (False, "The clips have too few unused frames at the cut for this transition."),
    "retimed": (False, "The clip is retimed and its sound is linked; sound is not retimed yet."),
    "music_map_mismatch": (True, "Song map of another song than the placed one."),
    "timeline_too_large": (False, "The document is capped at 4 MiB."),
    "revision_unknown": (True, "No kept revision with this id."),
    "source_offline": (False, "Source not a finished take or picture of this project, and no media_info given."),
    "timeline_unavailable": (True, "The project folder could not be read or written."),
    "project_conflict": (True, "The project changed since it was opened."),
    "project_not_found": (False, "No project open."),
    "project_scope_mismatch": (False, "The timeline belongs to another project."),
    "request_id_reused": (False, "This request_id already applied other ops; a retry repeats the same ops with the same request_id."),
    # lane p6-cut: trim_to_picture (cut decision 13); nothing_in_range (lift_range / extract_range) was answered
    # as timeline_unavailable before because it was missing here.
    "no_picture": (False, "The cut has no picture clip."),
    "nothing_to_trim": (False, "The song already ends with the picture."),
    "nothing_in_range": (True, "No clip lies between IN and OUT on the chosen tracks."),
    # lane w23-bins (CUT-180): cuts as items, nested cuts.
    "nest_cycle": (False, "A cut cannot contain itself, directly or through another cut."),
    "nest_rate": (False, "The nested cut runs at another frame rate than this cut."),
    "nest_empty": (False, "The cut has no item to place."),
    "last_cut": (False, "The project keeps at least one cut."),
    "cut_in_use": (False, "Another cut holds this cut as a nested clip."),
}


def error_body(code: str, message: str, op_index: int | None = None) -> dict[str, Any]:
    retryable, next_step = ERRORS.get(code, (False, "The timeline was not changed."))
    body: dict[str, Any] = {"code": code, "message": message, "retryable": retryable, "next": next_step}
    if op_index is not None:
        body["op_index"] = op_index
    return body


def payload(envelope: dict[str, Any]) -> bytes:
    data = json.dumps(envelope, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_BYTES:
        fail("timeline_too_large", "the timeline document exceeds 4 MiB")
    return data


def build(project_id: str, revision: int, updated_at: str, state: Any, *, label: Any = "", by: Any = "artist",
          changes: Any = None, reason: Any = None) -> dict[str, Any]:
    envelope = {"schema": schema_of(state), "project_id": project_id, "revision": revision, "updated_at": updated_at,
                "label": label, "by": by, "state": state}
    if changes is not None:
        envelope["changes"] = changes
    if reason is not None:
        envelope["reason"] = reason
    return check(envelope, project_id)


def _check_changes(value: Any) -> None:
    if type(value) is not dict or set(value) != {*_CHANGE_KINDS, "counts"}:
        fail("invalid_timeline", "envelope.changes: must hold created, removed, changed and counts")
    for kind in _CHANGE_KINDS:
        ids = value[kind]
        if type(ids) is not list or any(type(i) is not str for i in ids):
            fail("invalid_timeline", f"envelope.changes.{kind}: must be a list of ids")
    counts = value["counts"]
    if type(counts) is not dict or counts != {kind: len(value[kind]) for kind in _CHANGE_KINDS}:
        fail("invalid_timeline", "envelope.changes.counts: must count created, removed and changed")


def check(value: Any, project_id: str) -> dict[str, Any]:
    if type(value) is not dict or not ENVELOPE_KEYS <= set(value) <= ENVELOPE_KEYS | OPTIONAL_KEYS:
        fail("invalid_timeline", "envelope: must hold exactly " + ", ".join(sorted(ENVELOPE_KEYS)) + " (and changes, reason)")
    if "reason" in value and (type(value["reason"]) is not str or len(value["reason"]) > REASON_MAX):
        fail("invalid_timeline", f"envelope.reason: must be text of at most {REASON_MAX} characters")
    if "changes" in value:
        _check_changes(value["changes"])
    if value["schema"] not in SCHEMAS:
        fail("invalid_timeline", f"envelope.schema: must be {SCHEMA} or {SCHEMA_1_1}")
    if type(value["project_id"]) is not str or _PROJECT_RE.fullmatch(value["project_id"]) is None or value["project_id"] != project_id:
        fail("invalid_timeline", "envelope.project_id: belongs to another project")
    if type(value["revision"]) is not int or value["revision"] < 1:
        fail("invalid_timeline", "envelope.revision: must be an integer >= 1")
    if type(value["updated_at"]) is not str or _TIMESTAMP_RE.fullmatch(value["updated_at"]) is None:
        fail("invalid_timeline", "envelope.updated_at: must be an ISO UTC timestamp")
    if type(value["label"]) is not str or len(value["label"]) > 120:
        fail("invalid_timeline", "envelope.label: must be text of at most 120 characters")
    if value["by"] not in ("artist", "hermes"):
        fail("invalid_timeline", "envelope.by: must be artist or hermes")
    validate_state(value["state"])
    if value["schema"] == SCHEMA and uses_1_1(value["state"]):
        fail("invalid_timeline", f"envelope.schema: a cut with retimed clips, transitions or stretch is {SCHEMA_1_1}")
    return value


def refs_of(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every source ref any timeline uses: ref -> {kind, timelines, clips}."""
    out: dict[str, dict[str, Any]] = {}
    for timeline in state["timelines"]:
        for track in timeline["tracks"]:
            for item in track.get("items") or ():
                if item["type"] != "clip" or item["source"]["kind"] == "cut":  # w23-bins: a nested cut is no media
                    continue
                ref = item["source"]["ref"]
                entry = out.setdefault(ref, {"ref": ref, "kind": item["source"]["kind"], "timelines": [], "clips": 0})
                entry["clips"] += 1
                if timeline["id"] not in entry["timelines"]:
                    entry["timelines"].append(timeline["id"])
    return out


def _index(state: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    found = {}
    for timeline in state["timelines"]:
        for track in timeline["tracks"]:
            for item in track.get("items") or ():
                found[item["id"]] = (track["id"], item)
        for marker in timeline["markers"]:
            found[marker["id"]] = ("markers", marker)
        for transition in timeline["transitions"]:
            found[transition["id"]] = ("transitions", transition)
    return found


def summarize(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old, new = _index(before), _index(after)
    created = [i for i in new if i not in old]
    removed = [i for i in old if i not in new]
    changed = [{"track": new[i][0], "item": new[i][1]} for i in new if i in old and new[i] != old[i]]
    changed += [{"track": new[i][0], "item": new[i][1]} for i in created]
    placed = sorted(set(refs_of(after)) - set(refs_of(before)))
    return {"created": created, "removed": removed, "changed": changed, "placed_refs": placed,
            "active": after["active"], "timelines": [t["id"] for t in after["timelines"]]}


def changes_of(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """What a revision changed, for its envelope: created / removed / changed item ids (an item
    is changed when it is in both and differs; created ones are not counted twice) and counts."""
    old, new = _index(before), _index(after)
    out = {"created": [i for i in new if i not in old], "removed": [i for i in old if i not in new],
           "changed": [i for i in new if i in old and new[i] != old[i]]}
    return {**out, "counts": {kind: len(out[kind]) for kind in _CHANGE_KINDS}}


# --- request ids (lane w8-agent item 4): a retried apply never edits twice ----------------
# state/timeline.requests.json keeps the last REQUESTS_KEEP applies that carried a request_id:
# {schema, project_id, entries: [{request_id, ops_sha256, revision, answer}]}, oldest first.
REQUESTS_SCHEMA = "studio.timeline-requests/1"
REQUESTS_KEEP = 200
REQUESTS_MAX_BYTES = 16 * 1024 * 1024
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def ops_sha256(ops: Any) -> str:
    import hashlib

    data = json.dumps(ops, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def check_requests(value: Any, project_id: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {"schema", "project_id", "entries"} or value["schema"] != REQUESTS_SCHEMA:
        fail("invalid_timeline", "timeline.requests.json: must hold schema, project_id and entries")
    if value["project_id"] != project_id:
        fail("invalid_timeline", "timeline.requests.json: belongs to another project")
    entries = value["entries"]
    if type(entries) is not list or len(entries) > REQUESTS_KEEP:
        fail("invalid_timeline", f"timeline.requests.json: entries must be a list of at most {REQUESTS_KEEP}")
    seen = set()
    for entry in entries:
        if (type(entry) is not dict or set(entry) != {"request_id", "ops_sha256", "revision", "answer"}
                or type(entry["request_id"]) is not str or REQUEST_ID_RE.fullmatch(entry["request_id"]) is None
                or type(entry["ops_sha256"]) is not str or _SHA256_RE.fullmatch(entry["ops_sha256"]) is None
                or type(entry["revision"]) is not int or entry["revision"] < 1 or type(entry["answer"]) is not dict
                or entry["request_id"] in seen):
            fail("invalid_timeline", "timeline.requests.json: an entry is malformed")
        seen.add(entry["request_id"])
    return value


def requests_payload(project_id: str, entries: list[dict[str, Any]]) -> bytes:
    """The newest REQUESTS_KEEP entries that fit in REQUESTS_MAX_BYTES (oldest dropped first)."""
    entries = entries[-REQUESTS_KEEP:]
    while True:
        data = json.dumps({"schema": REQUESTS_SCHEMA, "project_id": project_id, "entries": entries},
                          ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(data) <= REQUESTS_MAX_BYTES or len(entries) <= 1:
            return data
        entries = entries[1:]


# --- the short read (lane w8-agent item 1): detail "summary" ------------------------------
# What an agent needs to address any item with one apply, in a fraction of the bytes: per
# timeline id, name, rate, length (frames) and music {track, markers} (the music lane's track
# id, the cut's marker count); per track its items as rows [id, kind, label, start, end, ref,
# flags?] (SUMMARY_FIELDS) in integer record frames [start, end). kind is the source kind
# (take, media, picture) or gap; ref is a short ref id: the first 8 characters after the
# ref's "kind:" prefix (longer only when two refs of the document would read the same; a
# gap has none). flags is present only when set: "s" the clip has a speed (retime), "c" a
# speed curve. Full refs, media facts and paths only in detail "full" (today's answer).
SUMMARY_FIELDS = ("id", "kind", "label", "start", "end", "ref", "flags")
_SHORT_REF = 8


def _short_refs(state: dict[str, Any]) -> dict[str, str]:
    refs = list(refs_of(state))
    for size in range(_SHORT_REF, 80, 4):
        short = {ref: ref.partition(":")[2][:size] or ref[:size] for ref in refs}
        if len(set(short.values())) == len(short):
            return short
    return {ref: ref for ref in refs}


def summary_of(read: dict[str, Any]) -> dict[str, Any]:
    """The detail:"summary" answer of /projects/timeline/read for a read_timeline result."""
    out: dict[str, Any] = {"detail": "summary", "exists": read["exists"], "revision": read["revision"]}
    state = read["state"]
    if state is None:
        return {**out, "active": None, "timelines": []}
    short = _short_refs(state)
    timelines = []
    for timeline in state["timelines"]:
        tracks, music, length = [], None, 0
        for track in timeline["tracks"]:
            if track["kind"] == "music":
                lane = track.get("music")
                music = track["id"]
                if lane is not None:
                    length = max(length, lane["record_start_frame"] + lane["duration_frames"])
                continue
            rows, at = [], 0
            free = track["kind"] == "audio" or track.get("role") == "overlay"
            for item in track.get("items") or ():
                start = item["record_start_frame"] if free else at
                end = at = start + item["duration_frames"]
                length = max(length, end)
                if item["type"] != "clip":
                    rows.append([item["id"], "gap", "", start, end, None])
                    continue
                retime = item.get("retime")
                flags = ("s" if retime else "") + ("c" if isinstance(retime, dict) and retime.get("curve") else "")
                # w28-fix item 0b (HER-089): a nested cut is no media (refs_of leaves it out); its row names the cut id itself.
                ref = item["source"]["ref"]
                row = [item["id"], item["source"]["kind"], item.get("label", ""), start, end, ref if item["source"]["kind"] == "cut" else short[ref]]
                rows.append(row + [flags] if flags else row)
            tracks.append({"id": track["id"], "items": rows})
        timelines.append({"id": timeline["id"], "name": timeline["name"], "rate": timeline["rate"], "length": length,
                          "music": {"track": music, "markers": len(timeline["markers"])}, "tracks": tracks})
    return {**out, "active": state["active"], "timelines": timelines}


__all__ = ["ERRORS", "KEEP_REVISIONS", "MAX_BYTES", "OPTIONAL_KEYS", "REASON_MAX", "SUMMARY_FIELDS", "changes_of", "check_requests", "ops_sha256", "requests_payload", "REQUEST_ID_RE", "REQUESTS_KEEP", "TimelineError", "build", "check", "error_body", "payload", "refs_of", "summarize", "summary_of"]
