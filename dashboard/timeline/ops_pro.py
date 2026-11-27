"""Timeline verbs for sequence format, retime / freeze and transitions (``studio.timeline/1.1``).

Twin of ``desktop/film-suite/src/timeline/ops/timeline-pro.js``; the shared fixture suites
``tests/backend/fixtures/timeline/{format,retime,transitions}.json`` run against both.

- ``set_format {width?, height?, fit?, rate?}``: the cut's frame size (even, 16..8192), the default
  fit of a clip whose shape differs (fit | fill | stretch) and, while the cut holds no clip, its rate.
- ``set_retime {clip, speed {num, den} | percent, reverse?, sampling?}``: constant speed on a picture
  clip. The clip keeps the source frames it shows and its length follows (ripple on V1);
  ``percent: 100`` without reverse removes the retime. ``fit_to_fill {clip, duration_frames}`` keeps
  the source frames and sets the exact speed that fills the new length.
- ``freeze_frame {clip, at_frame, duration_frames}``: holds the frame under ``at_frame`` for N frames
  on V1 (the clip is cut there and the hold is inserted, rippling the rest).
- ``add_transition {left, kind, duration_frames, alignment?, id?}`` on the cut after ``left``,
  ``set_transition {id, kind?, duration_frames?, alignment?}``, ``remove_transition {id}``.

A retimed clip's own sound (lane w6-tempo): at one speed of 50 .. 200 % forward it plays along,
pitch-corrected (the export's atempo); a ramp, hold, reverse or freeze turns ``audio`` off.
``set_retime`` still refuses a clip whose sound is linked on A1-A3 (a linked sound clip is not retimed).

Speed curves (lane w6-tempo; the mapping is ``speed_curve.py``). Key frames are record frames inside
the clip (0 = its first frame). Every verb keeps the clip's source range (``source_in_frame`` and the
source frames it reads) and the clip's length follows from it (ripple on V1):
- ``set_speed_curve {clip, keys [{frame, percent, ease?}], sampling?}``: the whole curve; one key is a
  constant speed (``set_retime``), one key at 100 % removes the retime.
- ``add_speed_key {clip, frame, percent?, ease?}`` (percent defaults to the curve's speed there),
  ``move_speed_key {clip, frame, to_frame?, percent?, ease?}``, ``remove_speed_key {clip, frame}``.
- ``add_hold {clip, frame, duration_frames}``: the picture stops on the frame under ``frame`` for N
  frames (a key pair at 0 %), then goes on at the speed it had.
- ``snap_speed_key {clip, frame, grid beat|downbeat|section, map, every?}``: moves the key onto the
  nearest event of the cut's song (the same grid as ``fit_to_beats``).
Every result carries ``keys`` (each with the source frame it lands on) and ``duration_frames``.
"""
from __future__ import annotations

import copy
from fractions import Fraction
from math import gcd
from typing import Any

from .schema import (ALIGNMENTS, FITS, MAX_SPEED, SAMPLINGS, TRANSITION_KINDS, clip_source_frames, fail,
                     frozen, handles, reversed_clip, sound_follows, transition_split)

# Measured: flow beats blend and nearest on 6 of 6 real takes at 50 %.
DEFAULT_SAMPLING = "flow"
STATE_OPS: tuple[str, ...] = ("duplicate_timeline",)


def _o():
    from . import ops
    return ops


def _speed(args: dict[str, Any]) -> dict[str, int]:
    o = _o()
    if ("speed" in args) == ("percent" in args):
        o._bad("set_retime takes speed {num, den} or percent")
    if "percent" in args:
        num, den = o._int(args["percent"], "percent", 0, MAX_SPEED * 100), 100
    else:
        speed = args["speed"]
        if type(speed) is not dict or set(speed) != {"num", "den"}:
            o._bad("speed must be {num, den}")
        num, den = o._int(speed["num"], "speed.num", 0, 1_000_000), o._int(speed["den"], "speed.den", 1, 1_000_000)
    if num == 0:
        return {"num": 0, "den": 1}
    if num > MAX_SPEED * den or num * 100 < den:
        o._bad("speed must be 1 % .. 10 000 %")
    k = gcd(num, den)
    return {"num": num // k, "den": den // k}


def _picture_on(state: dict[str, Any], args: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], int, dict[str, Any]]:
    o = _o()
    timeline = o._timeline(state, args)
    track, index, item = o._picture(timeline, args["clip"])
    o._unlocked(track)
    if not o._moving(item):
        o._bad(f"{item['id']} is a still; a still has no speed")
    for sound_track in o._audio_tracks(timeline):
        for sound in sound_track["items"]:
            if (sound.get("link") or {}).get("clip") == item["id"]:
                fail("linked", f"{item['id']} has its sound linked on {sound_track['id']} (sound is not retimed yet)")
    return timeline, track, index, item


def _set_speed(item: dict[str, Any], speed: dict[str, int], reverse: bool, sampling: str) -> None:
    if speed["num"] == speed["den"] and not reverse:
        item.pop("retime", None)
    else:
        item["retime"] = {"speed": speed, "reverse": reverse if speed["num"] else False, "sampling": sampling}
    if item.get("retime") is not None and not sound_follows(item):
        item["audio"] = "off"  # lane w6-tempo: one speed of 50 .. 200 % forward keeps its sound (pitch-corrected)


def _rec_floor(frames: int, item: dict[str, Any], rate: dict[str, int]) -> int:
    from .schema import effective_rate
    eff = effective_rate(item)
    return frames * eff["den"] * rate["num"] // (eff["num"] * rate["den"])


def op_set_retime(state, args, ctx):
    o = _o()
    o._args(args, ("clip",), ("speed", "percent", "reverse", "sampling"))
    timeline, track, _index, item = _picture_on(state, args)
    rate = timeline["rate"]
    old = item.get("retime") or {}
    reverse = args.get("reverse", old.get("reverse", False))
    if type(reverse) is not bool:
        o._bad("reverse must be true or false")
    sampling = args.get("sampling", old.get("sampling", DEFAULT_SAMPLING))
    if sampling not in SAMPLINGS:
        o._bad("sampling must be " + " | ".join(SAMPLINGS))
    if old.get("curve") and "speed" not in args and "percent" not in args:
        # Reverse / sampling on a clip with a speed curve: the curve stays, the source range too.
        item["retime"] = {**old, "reverse": reverse, "sampling": sampling}
        return {"op": "set_retime", "duration_frames": item["duration_frames"], "speed": copy.deepcopy(old["speed"]),
                "reverse": reversed_clip(item)}
    speed = _speed(args) if ("speed" in args or "percent" in args) else old.get("speed", {"num": 1, "den": 1})
    if speed["num"] == 0:
        o._bad("a freeze is freeze_frame")
    if frozen(item):
        o._bad(f"{item['id']} is a freeze frame; trim changes its length")
    shown = clip_source_frames(item, rate)
    _set_speed(item, speed, reverse, sampling)
    item["duration_frames"] = max(1, _rec_floor(shown, item, rate))
    if o._free(track):
        o._sort_overlay(track)
    return {"op": "set_retime", "duration_frames": item["duration_frames"],
            "speed": copy.deepcopy(speed), "reverse": reversed_clip(item)}


def op_fit_to_fill(state, args, ctx):
    o = _o()
    o._args(args, ("clip", "duration_frames"), ("sampling",))
    timeline, _track, _index, item = _picture_on(state, args)
    rate = timeline["rate"]
    if frozen(item):
        o._bad(f"{item['id']} is a freeze frame; trim changes its length")
    duration = o._int(args["duration_frames"], "duration_frames", 1)
    shown = clip_source_frames(item, rate)
    source = item["source_rate"]
    # speed * source_rate / rate * duration = shown  ->  speed = shown * rate / (duration * source_rate)
    num, den = shown * rate["num"] * source["den"], duration * rate["den"] * source["num"]
    k = gcd(num, den)
    speed = {"num": num // k, "den": den // k}
    if speed["num"] > MAX_SPEED * speed["den"] or speed["num"] * 100 < speed["den"]:
        o._bad("fit to fill needs 1 % .. 10 000 %")
    sampling = args.get("sampling", (item.get("retime") or {}).get("sampling", DEFAULT_SAMPLING))
    if sampling not in SAMPLINGS:
        o._bad("sampling must be " + " | ".join(SAMPLINGS))
    _set_speed(item, speed, reversed_clip(item), sampling)
    item["duration_frames"] = duration
    return {"op": "fit_to_fill", "speed": copy.deepcopy(speed)}


def op_freeze_frame(state, args, ctx):
    o = _o()
    o._args(args, ("clip", "at_frame", "duration_frames"))
    timeline, track, index, item = _picture_on(state, args)
    if track["role"] != "storyline":
        o._bad("freeze_frame holds a frame on the storyline (V1)")
    rate = timeline["rate"]
    duration = o._int(args["duration_frames"], "duration_frames", 1)
    start = o._starts(track)[index]
    at = o._int(args["at_frame"], "at_frame", start, start + item["duration_frames"] - 1)
    offset = at - start
    # The source frame on screen at ``at``: the frame index a clip shows ``offset`` frames in.
    if frozen(item):
        frame = item["source_in_frame"]
    elif (item.get("retime") or {}).get("curve"):
        from .speed_curve import source_map
        frame = source_map(item, rate, offset, 1)[0]
    else:
        from .schema import effective_rate
        eff = effective_rate(item)
        step = offset * eff["num"] * rate["den"] // (eff["den"] * rate["num"])
        last = item["source_in_frame"] + clip_source_frames(item, rate) - 1
        frame = last - step if reversed_clip(item) else item["source_in_frame"] + step
    reserved: set[str] = set()
    insert_at = index
    created: list[str] = []
    if offset:
        created.append(o._split_item(state, timeline, track, index, at, reserved))
        insert_at = index + 1
    hold = copy.deepcopy(track["items"][index])
    hold["id"] = o.new_id(state, "c", reserved)
    hold.update({"duration_frames": duration, "source_in_frame": frame, "audio": "off",
                 "retime": {"speed": {"num": 0, "den": 1}, "reverse": False, "sampling": "nearest"},
                 "anchors": {"in": None, "out": None}})
    track["items"].insert(insert_at, hold)
    return {"op": "freeze_frame", "created": [hold["id"], *created], "source_frame": frame}


def _cut_after(timeline: dict[str, Any], left: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    o = _o()
    track = o._storyline(timeline)
    for index, item in enumerate(track["items"]):
        if item["id"] == left:
            if item["type"] != "clip" or index + 1 >= len(track["items"]) or track["items"][index + 1]["type"] != "clip":
                fail("not_adjacent", f"{left} has no clip right after it on V1")
            return item, track["items"][index + 1]
    fail("not_found", f"clip {left} is not on the storyline")
    raise AssertionError


def _check_transition(timeline: dict[str, Any], transition: dict[str, Any]) -> None:
    """Readable refusals before validation: the handles each side needs."""
    o = _o()
    track = o._storyline(timeline)
    by_id = {item["id"]: item for item in track["items"]}
    left, right = by_id[transition["left"]], by_id[transition["right"]]
    before, after = transition_split(transition)
    rate = timeline["rate"]
    need_left, need_right = handles(left, rate)[1], handles(right, rate)[0]
    if need_left < after:
        fail("no_handles", f"{left['label'] or left['id']} has {need_left} frames after its out point; "
                           f"this transition needs {after}.")
    if need_right < before:
        fail("no_handles", f"{right['label'] or right['id']} has {need_right} frames before its in point; "
                           f"this transition needs {before}.")


def _transition_args(args: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    o = _o()
    out = dict(current or {})
    for key, options in (("kind", TRANSITION_KINDS), ("alignment", ALIGNMENTS)):
        if key in args:
            if args[key] not in options or type(args[key]) is not str:
                o._bad(f"{key} must be " + " | ".join(options))
            out[key] = args[key]
    if "duration_frames" in args:
        out["duration_frames"] = o._int(args["duration_frames"], "duration_frames", 1, 100_000)
    return out


def op_add_transition(state, args, ctx):
    o = _o()
    o._args(args, ("left", "kind", "duration_frames"), ("alignment", "id"))
    timeline = o._timeline(state, args)
    o._unlocked(o._storyline(timeline))
    left, right = _cut_after(timeline, args["left"])
    if any(t["left"] == left["id"] for t in timeline["transitions"]):
        o._bad(f"the cut after {left['id']} already has a transition (set_transition changes it)")
    ident = args.get("id") or o.new_id(state, "x")
    if ident in o._all_ids(state):
        o._bad(f"transition id {ident} is already used")
    transition = {"id": ident, "left": left["id"], "right": right["id"], "kind": "dissolve", "duration_frames": 1,
                  "alignment": "center"}
    transition.update(_transition_args(args, transition))
    _check_transition(timeline, transition)
    timeline["transitions"].append(transition)
    _sort(timeline)
    return {"op": "add_transition", "created": [ident]}


def _transition(timeline: dict[str, Any], ident: Any) -> dict[str, Any]:
    for transition in timeline["transitions"]:
        if transition["id"] == ident:
            return transition
    fail("not_found", f"transition {ident} does not exist")
    raise AssertionError


def op_set_transition(state, args, ctx):
    o = _o()
    o._args(args, ("id",), ("kind", "duration_frames", "alignment"))
    timeline = o._timeline(state, args)
    o._unlocked(o._storyline(timeline))
    transition = _transition(timeline, args["id"])
    transition.update(_transition_args(args, transition))
    _check_transition(timeline, transition)
    return {"op": "set_transition"}


def op_remove_transition(state, args, ctx):
    o = _o()
    o._args(args, ("id",))
    timeline = o._timeline(state, args)
    o._unlocked(o._storyline(timeline))
    transition = _transition(timeline, args["id"])
    timeline["transitions"] = [t for t in timeline["transitions"] if t is not transition]
    return {"op": "remove_transition", "removed": [args["id"]]}


def _sort(timeline: dict[str, Any]) -> None:
    order = {item["id"]: index for index, item in enumerate(_o()._storyline(timeline)["items"])}
    timeline["transitions"].sort(key=lambda t: order.get(t["left"], 1 << 30))


def fit_transitions(timeline: dict[str, Any], before: dict[str, tuple[int, int]]) -> None:
    """After an edit: a transition stays on its cut. When the left clip was split, the new part
    before the right clip carries it; when the cut is gone (a clip moved, deleted or lifted), the
    transition goes with it."""
    if not timeline["transitions"]:
        return
    items = _o()._storyline(timeline)["items"]
    position = {item["id"]: index for index, item in enumerate(items)}
    kept = []
    for transition in timeline["transitions"]:
        right = position.get(transition["right"])
        if right is None or right == 0 or items[right]["type"] != "clip" or items[right - 1]["type"] != "clip":
            continue
        left = items[right - 1]
        if left["id"] != transition["left"]:
            # Only a split of the left clip hands the cut on: the new part (not there before) that
            # follows the old left clip and shows the same source.
            old = position.get(transition["left"])
            if left["id"] in before or old is None or old != right - 2 or items[old]["source"] != left["source"]:
                continue
        transition["left"] = left["id"]
        kept.append(transition)
    timeline["transitions"] = kept
    _sort(timeline)


def op_set_format(state, args, ctx):
    o = _o()
    o._args(args, (), ("width", "height", "fit", "rate"))
    timeline = o._timeline(state, args)
    if not any(key in args for key in ("width", "height", "fit", "rate")):
        o._bad("set_format takes width, height, fit and/or rate")
    fmt = timeline["format"]
    for key in ("width", "height"):
        if key in args:
            value = o._int(args[key], key, 16, 8192)
            if value % 2:
                o._bad(f"{key} must be even")
            fmt[key] = value
    if "fit" in args:
        if args["fit"] not in FITS or type(args["fit"]) is not str:
            o._bad("fit must be " + " | ".join(FITS))
        fmt["fit"] = args["fit"]
    if "rate" in args:
        rate = args["rate"]
        if type(rate) is not dict or set(rate) != {"num", "den"}:
            o._bad("rate must be {num, den}")
        o._int(rate["num"], "rate.num", 1, 1_000_000)
        o._int(rate["den"], "rate.den", 1, 1_000_000)
        if rate != timeline["rate"]:
            if any(item["type"] == "clip" for track in o._item_tracks(timeline) for item in track["items"]) \
                    or any(t.get("music") is not None for t in timeline["tracks"] if t["kind"] == "music"):
                fail("not_empty", "the cut holds clips; its rate is fixed once clips are in (make a new cut at that rate)")
            timeline["rate"] = copy.deepcopy(rate)
    return {"op": "set_format", "format": copy.deepcopy(fmt), "rate": copy.deepcopy(timeline["rate"])}


def op_set_marker(state, args, ctx):
    """Rename a marker or change its note / colour (move_marker moves it, remove_marker removes it)."""
    o = _o()
    o._args(args, ("id",), ("name", "note", "color"))
    timeline = o._timeline(state, args)
    if not any(key in args for key in ("name", "note", "color")):
        o._bad("set_marker takes name, note and/or color")
    marker = o._marker(timeline, args["id"])
    for key in ("name", "note", "color"):
        if key in args:
            if type(args[key]) is not str:
                o._bad(f"{key} must be text")
            marker[key] = args[key]
    return {"op": "set_marker"}


def op_duplicate_timeline(state, args, ctx):
    """A copy of a cut (every clip, transition and marker; new ids for the cut, its items and
    markers in the copy), named ``name``, made active unless ``activate`` is false."""
    o = _o()
    o._args(args, ("name",), ("source", "id", "activate"))
    source = o._timeline(state, {"timeline": args.get("source", state["active"])})
    ident = args.get("id") or o.new_id(state, "tl_")
    if ident in o._all_ids(state):
        o._bad(f"timeline id {ident} is already used")
    copy_ = copy.deepcopy(source)
    copy_["id"], copy_["name"] = ident, args["name"]
    reserved: set[str] = {ident}
    renamed: dict[str, str] = {}
    for track in copy_["tracks"]:
        for item in track.get("items") or ():
            prefix = "g" if item["type"] == "gap" else "a" if track["kind"] == "audio" else "c"
            renamed[item["id"]] = o.new_id(state, prefix, reserved)
            reserved.add(renamed[item["id"]])
    for marker in copy_["markers"]:
        renamed[marker["id"]] = o.new_id(state, "m", reserved)
        reserved.add(renamed[marker["id"]])
    for transition in copy_["transitions"]:
        renamed[transition["id"]] = o.new_id(state, "x", reserved)
        reserved.add(renamed[transition["id"]])
    for track in copy_["tracks"]:
        for item in track.get("items") or ():
            item["id"] = renamed[item["id"]]
            for key in ("attach", "link"):
                if item.get(key) is not None:
                    item[key]["clip"] = renamed[item[key]["clip"]]
    for marker in copy_["markers"]:
        marker["id"] = renamed[marker["id"]]
        if marker.get("attach_clip") is not None:
            marker["attach_clip"] = renamed[marker["attach_clip"]]
    for transition in copy_["transitions"]:
        transition["id"], transition["left"], transition["right"] = (renamed[transition["id"]], renamed[transition["left"]],
                                                                      renamed[transition["right"]])
    state["timelines"].append(copy_)
    if args.get("activate", True) is True:
        state["active"] = ident
    return {"op": "duplicate_timeline", "created": [ident]}


PRO_OPS = {
    "set_format": op_set_format, "set_retime": op_set_retime, "fit_to_fill": op_fit_to_fill,
    "freeze_frame": op_freeze_frame, "add_transition": op_add_transition, "set_transition": op_set_transition,
    "remove_transition": op_remove_transition, "set_marker": op_set_marker, "duplicate_timeline": op_duplicate_timeline,
}


# --- speed curves (lane w6-tempo) ------------------------------------------------------------------

def _curve_item(state, args):
    o = _o()
    timeline, track, index, item = _picture_on(state, args)
    if frozen(item):
        o._bad(f"{item['id']} is a freeze frame; a freeze has no speed curve")
    return timeline, track, index, item


def _current_keys(item: dict[str, Any]) -> list[dict[str, Any]]:
    """The clip's curve, or its constant speed as one key (whole percent)."""
    from . import speed_curve as sc
    keys = sc.keys_of(item)
    if keys:
        return copy.deepcopy(keys)
    speed = (item.get("retime") or {}).get("speed") or {"num": 1, "den": 1}
    if (speed["num"] * 100) % speed["den"]:
        _o()._bad(f"{item['id']} runs at {speed['num']}/{speed['den']}, not a whole percent")
    return [{"frame": 0, "percent": speed["num"] * 100 // speed["den"], "ease": "linear"}]


def _key_args(raw: Any, name: str) -> list[dict[str, Any]]:
    o = _o()
    if type(raw) is not list or not 1 <= len(raw) <= 200:
        o._bad(f"{name} must be a list of 1..200 keys")
    out = []
    for i, key in enumerate(raw):
        if type(key) is not dict or not {"frame", "percent"} <= set(key) <= {"frame", "percent", "ease"}:
            o._bad(f"{name}[{i}] must be {{frame, percent, ease?}}")
        out.append({"frame": o._int(key["frame"], f"{name}[{i}].frame", 0), "percent": o._int(key["percent"], f"{name}[{i}].percent", 0, 10_000),
                    "ease": _ease(key.get("ease", "linear"))})
    return out


def _ease(value: Any) -> str:
    from .speed_curve import EASES
    if value not in EASES:
        _o()._bad("ease must be " + " | ".join(EASES))
    return value


def _apply_curve(timeline, track, item, keys: list[dict[str, Any]], sampling: str | None, verb: str) -> dict[str, Any]:
    """Write ``keys`` on the clip keeping its source range; the length follows."""
    from . import speed_curve as sc
    o = _o()
    rate = timeline["rate"]
    shown = clip_source_frames(item, rate)
    reverse = reversed_clip(item)
    sampling = sampling or (item.get("retime") or {}).get("sampling", DEFAULT_SAMPLING)
    if sampling not in SAMPLINGS:
        o._bad("sampling must be " + " | ".join(SAMPLINGS))
    keys = sorted(keys, key=lambda k: k["frame"])
    if verb in ("set_speed_curve", "remove_speed_key") and len({k["percent"] for k in keys}) == 1 and keys[0]["frame"] == 0:
        keys = [{"frame": 0, "percent": keys[0]["percent"], "ease": "linear"}]  # one speed throughout: no curve
    if len(keys) == 1:
        if keys[0]["frame"] != 0:
            o._bad("a single key sits on frame 0")
        if keys[0]["percent"] == 0:
            o._bad("a whole-clip hold is freeze_frame")
        k = gcd(keys[0]["percent"], 100)
        _set_speed(item, {"num": keys[0]["percent"] // k, "den": 100 // k}, reverse, sampling)
        item["duration_frames"] = max(1, _rec_floor(shown, item, rate))
    else:
        why = sc.check_keys(keys, "keys")
        if why:
            fail("invalid_op", why)
        item["retime"] = {"speed": {"num": 1, "den": 1}, "reverse": reverse, "sampling": sampling, "curve": keys}
        item["audio"] = "off"
        item["duration_frames"] = sc.duration_for(keys, sc.q_of(item["source_rate"], rate), shown)
        if keys[-1]["frame"] >= item["duration_frames"]:
            fail("speed_curve", f"the key at frame {keys[-1]['frame']} would lie past the clip's end "
                 f"({item['duration_frames']} frames at these speeds)")
    if o._free(track):
        o._sort_overlay(track)
    from .speed_curve import key_rows
    return {"op": verb, "clip": item["id"], "duration_frames": item["duration_frames"], "keys": key_rows(item, rate)}


def _key_index(keys: list[dict[str, Any]], frame: Any) -> int:
    o = _o()
    frame = o._int(frame, "frame", 0)
    for index, key in enumerate(keys):
        if key["frame"] == frame:
            return index
    fail("not_found", f"no speed key at frame {frame} (keys at {', '.join(str(k['frame']) for k in keys)})")


def op_set_speed_curve(state, args, ctx):
    _o()._args(args, ("clip", "keys"), ("sampling",))
    timeline, track, _index, item = _curve_item(state, args)
    return _apply_curve(timeline, track, item, _key_args(args["keys"], "keys"), args.get("sampling"), "set_speed_curve")


def op_add_speed_key(state, args, ctx):
    from .speed_curve import speed_at
    o = _o()
    o._args(args, ("clip", "frame"), ("percent", "ease"))
    timeline, track, _index, item = _curve_item(state, args)
    keys = _current_keys(item)
    frame = o._int(args["frame"], "frame", 1, item["duration_frames"] - 1)
    if any(k["frame"] == frame for k in keys):
        fail("exists", f"a speed key already sits at frame {frame}; move_speed_key changes it")
    here = speed_at(keys, frame)
    percent = o._int(args["percent"], "percent", 0, 10_000) if "percent" in args else int(here + Fraction(1, 2))
    before = max((k for k in keys if k["frame"] < frame), key=lambda k: k["frame"])
    keys.append({"frame": frame, "percent": percent, "ease": _ease(args.get("ease", before["ease"]))})
    return _apply_curve(timeline, track, item, keys, None, "add_speed_key")


def op_move_speed_key(state, args, ctx):
    o = _o()
    o._args(args, ("clip", "frame"), ("to_frame", "percent", "ease"))
    if not {"to_frame", "percent", "ease"} & set(args):
        o._bad("move_speed_key names to_frame, percent or ease")
    timeline, track, _index, item = _curve_item(state, args)
    keys = _current_keys(item)
    index = _key_index(keys, args["frame"])
    key = keys[index]
    if "to_frame" in args:
        to = o._int(args["to_frame"], "to_frame", 0)
        if index == 0 and to != 0:
            o._bad("the first key stays on the clip's first frame")
        low = keys[index - 1]["frame"] + 1 if index else 0
        high = keys[index + 1]["frame"] - 1 if index + 1 < len(keys) else MAX_KEY_FRAME
        if not low <= to <= high:
            fail("out_of_range", f"the key can move between frames {low} and {high} (its neighbours)")
        key["frame"] = to
    if "percent" in args:
        key["percent"] = o._int(args["percent"], "percent", 0, 10_000)
    if "ease" in args:
        key["ease"] = _ease(args["ease"])
    return _apply_curve(timeline, track, item, keys, None, "move_speed_key")


def op_remove_speed_key(state, args, ctx):
    o = _o()
    o._args(args, ("clip", "frame"))
    timeline, track, _index, item = _curve_item(state, args)
    keys = _current_keys(item)
    index = _key_index(keys, args["frame"])
    if index == 0:
        o._bad("the first key stays (set its speed instead)")
    del keys[index]
    return _apply_curve(timeline, track, item, keys, None, "remove_speed_key")


def op_add_hold(state, args, ctx):
    from .speed_curve import speed_at
    o = _o()
    o._args(args, ("clip", "frame", "duration_frames"))
    timeline, track, _index, item = _curve_item(state, args)
    keys = _current_keys(item)
    frame = o._int(args["frame"], "frame", 0, item["duration_frames"] - 1)
    length = o._int(args["duration_frames"], "duration_frames", 1, 100_000)
    after = int(speed_at(keys, frame) + Fraction(1, 2))
    segment = max((k for k in keys if k["frame"] <= frame), key=lambda k: k["frame"])
    if after == 0:
        fail("exists", f"the clip already holds at frame {frame}")
    out = [k for k in keys if k["frame"] < frame]
    out.append({"frame": frame, "percent": 0, "ease": "linear"})
    out.append({"frame": frame + length, "percent": after, "ease": segment["ease"]})
    out += [{**k, "frame": k["frame"] + length} for k in keys if k["frame"] > frame]
    return _apply_curve(timeline, track, item, out, None, "add_hold")


def op_snap_speed_key(state, args, ctx):
    o = _o()
    o._args(args, ("clip", "frame", "grid", "map"), ("every",))
    if args["grid"] not in ("beat", "downbeat", "section"):
        o._bad("grid must be beat, downbeat or section")
    timeline, track, index, item = _curve_item(state, args)
    keys = _current_keys(item)
    k = _key_index(keys, args["frame"])
    if k == 0:
        o._bad("the first key stays on the clip's first frame")
    start = o._starts(track)[index] if not o._free(track) else item["record_start_frame"]
    every = o._int(args.get("every", 1), "every", 1, 1024)
    low = start + keys[k - 1]["frame"] + 1
    high = start + (keys[k + 1]["frame"] - 1 if k + 1 < len(keys) else item["duration_frames"] - 1)
    events = [e for e in o.music_event_frames(timeline, args["map"], args["grid"]) if e["index"] % every == 0 and low <= e["frame"] <= high]
    if not events:
        fail("no_beat", f"no {args['grid']} between the key's neighbours (frames {low}..{high})")
    here = start + keys[k]["frame"]
    best = min(events, key=lambda e: (abs(e["frame"] - here), e["frame"]))
    keys[k]["frame"] = best["frame"] - start
    out = _apply_curve(timeline, track, item, keys, None, "snap_speed_key")
    out.update({"beat": {"kind": args["grid"], "index": best["index"], "frame": best["frame"], "residual_ppm": best["residual_ppm"]}})
    return out


MAX_KEY_FRAME = 2**40
PRO_OPS.update({
    "set_speed_curve": op_set_speed_curve, "add_speed_key": op_add_speed_key, "move_speed_key": op_move_speed_key,
    "remove_speed_key": op_remove_speed_key, "add_hold": op_add_hold, "snap_speed_key": op_snap_speed_key,
})


CURVE_VERBS = frozenset({"set_speed_curve", "add_speed_key", "move_speed_key", "remove_speed_key", "add_hold",
                         "snap_speed_key", "set_retime", "fit_to_fill", "slip", "swap_take"})
CURVE_NEW_OK = frozenset({"append", "insert", "overwrite", "duplicate_timeline", "place_with_parent", "add_timeline"})


def curve_heads(timeline: dict[str, Any] | None) -> dict[str, tuple[int, str]]:
    """Clip id -> (source frame its first record frame shows, its keys) for clips with a speed curve."""
    import json
    from .speed_curve import source_map
    out: dict[str, tuple[int, str]] = {}
    for track in (timeline or {}).get("tracks") or ():
        for item in track.get("items") or ():
            if item.get("type") == "clip" and (item.get("retime") or {}).get("curve"):
                out[item["id"]] = (source_map(item, timeline["rate"], 0, 1)[0], json.dumps(item["retime"]["curve"], sort_keys=True))
    return out


def curve_guard(before: dict[str, tuple[int, str]], timeline: dict[str, Any] | None, verb: str) -> None:
    """Speed keys count from a clip's first frame: an edit that moves that frame's source (a trim of
    its start, a roll or slide into it, a split, a cut over IN-OUT) would shift the ramp, so it is
    refused with the way out. Trims of the end, moves and the curve verbs themselves pass."""
    if verb in CURVE_VERBS or timeline is None:
        return
    for ident, (head, keys) in curve_heads(timeline).items():
        if ident not in before:
            if verb in CURVE_NEW_OK:
                continue
            fail("speed_curve", f"{verb} would cut a clip with a speed curve")
        elif before[ident] != (head, keys):
            fail("speed_curve", f"{ident} has a speed curve (its keys count from its first frame); {verb} would move "
                 "that frame")
