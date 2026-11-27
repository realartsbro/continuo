"""OpenTimelineIO JSON (``Timeline.1``) written without the library, validated structurally.

``opentimelineio`` is not installed here (and installs are not allowed), so the
document is written from the documented schema (OTIO "Timeline Structure" and
file-format docs: Timeline.1 / Stack.1 / Track.1 / Clip.2 / Gap.1 / Marker.2 /
ExternalReference.1 / TimeRange.1 / RationalTime.1) and checked by
``validate_otio``.  Whether a given OTIO reader accepts it is UNVERIFIED here.

Times are ``RationalTime(value, rate)`` with whole-number values: record
durations at the timeline rate, source starts at the source's own rate (a
48 fps source keeps its exact frame), never a rounded float second.

``studio.timeline/1.1``: a retimed clip carries ``LinearTimeWarp.1`` (``time_scalar`` = speed,
negative when reversed) or ``FreezeFrame.1`` in its ``effects``; its ``source_range`` keeps the
record duration (OTIO: the effect maps it to the media). A speed curve (lane w7-export): one speed
over the clip is that ``LinearTimeWarp.1``, a pure hold ``FreezeFrame.1``; a ramp is not in the
OTIO core schema, so it is its exact average speed and its keys are in
``metadata["film_suite"]["speed_curve"]`` (``exact`` false; the package warns). A V1 transition is a ``Transition.1``
(``SMPTE_Dissolve``; a dip is ``Custom_Transition`` named "Dip to Black/White" with the colour
in metadata) between the two clips with ``in_offset`` = its frames before the cut and
``out_offset`` = after.
"""
from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from .export_verify import uri_to_path
from .model import Plan, PlanClip

MARKER_COLORS = {"red", "pink", "orange", "yellow", "green", "cyan", "blue", "purple", "magenta", "black", "white"}


def _rt(value: int, rate: Fraction) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": float(rate), "value": float(value)}


def _range(start: int, start_rate: Fraction, duration: int, duration_rate: Fraction) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start, start_rate), "duration": _rt(duration, duration_rate)}


def _item_common(name: str, *, enabled: bool = True) -> dict[str, Any]:
    return {"name": name, "metadata": {}, "effects": [], "markers": [], "enabled": enabled}


def _gap(frames: int, rate: Fraction) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "Gap.1", **_item_common(""), "source_range": _range(0, rate, frames, rate)}


def native_path(path: Path) -> str:
    """``C:/a b/c.mp4``: the absolute path with forward slashes, no URL scheme, no percent-encoding.
    DaVinci Resolve 21.0.3 refuses an OTIO whose target_url is ``file:///C:/...`` (import returns
    None) and imports the plain path."""
    text = str(Path(path).resolve()).replace("\\", "/")
    return text[4:] if text.startswith("//?/") else text


def target_path(target: str) -> Path:
    """The file an OTIO target_url names: a ``file:`` URI (older packages) or a native path."""
    if not target:
        raise ValueError("empty target_url")
    return uri_to_path(target) if target.lower().startswith("file:") else Path(target)


def _reference(path: Path, frames: int, rate: Fraction) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "ExternalReference.1", "name": path.name, "metadata": {}, "target_url": native_path(path),
            "available_range": _range(0, rate, frames, rate), "available_image_bounds": None}


def _clip(name: str, ref: dict[str, Any], source_start: int, source_rate: Fraction, frames: int, rate: Fraction,
          *, enabled: bool, metadata: dict[str, Any], effects: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    clip = {"OTIO_SCHEMA": "Clip.2", **_item_common(name, enabled=enabled),
            "source_range": _range(source_start, source_rate, frames, rate),
            "media_references": {"DEFAULT_MEDIA": ref}, "active_media_reference_key": "DEFAULT_MEDIA"}
    clip["metadata"] = metadata
    if effects:
        clip["effects"] = effects
    return clip


def _warp(clip: PlanClip, rate: Fraction) -> list[dict[str, Any]]:
    retime = clip.retime
    if retime is None:
        return []
    meta = {"studio": {"speed": {"num": retime.speed.numerator, "den": retime.speed.denominator},
                       "reverse": retime.reverse, "sampling": retime.sampling}}
    if retime.curve:
        # OTIO's core schema has one speed per clip (lane w7-export): a curve that is one speed over
        # the clip is that LinearTimeWarp, a pure hold a FreezeFrame; a ramp is written at its exact
        # average speed (the source range it reads) and its keys go to metadata film_suite.speed_curve.
        from .export_curve import average_speed, constant_speed
        speed = constant_speed(clip, rate)
        if speed == 0:
            return [{"OTIO_SCHEMA": "FreezeFrame.1", "name": "Freeze Frame", "effect_name": "FreezeFrame", "metadata": meta, "time_scalar": 0.0}]
        scalar = float(speed if speed is not None else average_speed(clip, rate)) * (-1 if retime.reverse else 1)
        return [{"OTIO_SCHEMA": "LinearTimeWarp.1", "name": "Speed" if speed is not None else "Speed (average of a ramp)",
                 "effect_name": "LinearTimeWarp", "metadata": meta, "time_scalar": scalar}]
    if retime.freeze:
        return [{"OTIO_SCHEMA": "FreezeFrame.1", "name": "Freeze Frame", "effect_name": "FreezeFrame", "metadata": meta, "time_scalar": 0.0}]
    scalar = float(retime.speed) * (-1 if retime.reverse else 1)
    return [{"OTIO_SCHEMA": "LinearTimeWarp.1", "name": "Speed", "effect_name": "LinearTimeWarp", "metadata": meta, "time_scalar": scalar}]


_OTIO_TRANSITION = {"dissolve": ("SMPTE_Dissolve", "Cross Dissolve"), "dip_black": ("Custom_Transition", "Dip to Black"),
                    "dip_white": ("Custom_Transition", "Dip to White")}


def _transition(transition: Any, rate: Fraction) -> dict[str, Any]:
    kind, name = _OTIO_TRANSITION[transition.kind]
    return {"OTIO_SCHEMA": "Transition.1", "name": name, "metadata": {"studio": {"id": transition.id, "kind": transition.kind}},
            "transition_type": kind, "in_offset": _rt(transition.before, rate), "out_offset": _rt(transition.after, rate)}


def _track(name: str, kind: str, children: list[dict[str, Any]], *, enabled: bool = True) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "Track.1", **_item_common(name, enabled=enabled), "source_range": None, "kind": kind, "children": children}


def build_otio(plan: Plan, remuxes: dict[str, Path], *, name: str) -> dict[str, Any]:
    rate = plan.rate

    def picture(clip: PlanClip) -> dict[str, Any]:
        info = clip.info
        meta = {"studio": {"id": clip.id, "source": dict(clip.source or {})}}
        if clip.frame is not None:  # lane w46-d: the clip's own framing, exact, for a reader that knows it
            meta.setdefault("film_suite", {})["frame"] = copy.deepcopy(clip.frame)  # move.part (lane w47-p3) included
            if clip.frame.get("move"):
                # Record-frame domain of the original move; ordinary OTIO readers do not apply it.
                meta["film_suite"]["frame_span"] = {"version": 1, "offset": clip.frame_offset,
                                                     "whole": clip.frame_whole if clip.frame_whole is not None else clip.duration}
        if info.is_still:
            ref = _reference(info.path, clip.duration, rate)
            return _clip(clip.label or info.path.stem, ref, 0, rate, clip.duration, rate, enabled=clip.enabled, metadata=meta)
        ref = _reference(remuxes[clip.source["ref"]], info.frames, clip.source_rate)
        start = clip.source_in
        if clip.retime is not None and clip.retime.reverse:
            start = clip.source_in + clip.source_frames_used(rate) - 1  # a reversed clip starts on its last frame
        if clip.retime is not None and clip.retime.curve:
            from .export_curve import constant_speed
            meta.setdefault("film_suite", {}).update({"speed_curve": [dict(k) for k in clip.keys()], "exact": constant_speed(clip, rate) is not None})
            if constant_speed(clip, rate) == 0 and clip.retime.reverse:
                start = clip.source_in  # a hold shows its one frame
        return _clip(clip.label or info.path.stem, ref, start, clip.source_rate, clip.duration, rate,
                     enabled=clip.enabled, metadata=meta, effects=_warp(clip, rate))

    after_clip = {t.left: t for t in plan.transitions}
    v1 = []
    # Lane w23-bins (CUT-180): a nested cut is a nested Stack (OTIO's nested timeline) holding its V1 pieces.
    from .export_nest import storyline_runs
    for nest_id, nest_name, run in storyline_runs(plan.storyline, plan.nest_ids):
        children = []
        for c in run:
            children.append(_gap(c.duration, rate) if c.kind == "gap" else picture(c))
            if c.id in after_clip:
                children.append(_transition(after_clip[c.id], rate))
        if nest_id is None:
            v1.extend(children)
            continue
        frames = sum(c.duration for c in run)
        inner = _track("V1", "Video", children)
        v1.append({"OTIO_SCHEMA": "Stack.1", **_item_common(nest_name), "metadata": {"studio": {"id": nest_id, "nested_cut": nest_name}},
                   "source_range": _range(0, rate, frames, rate), "children": [inner]})
    tracks = [_track("V1", "Video", v1)]
    total = plan.total_frames
    if plan.overlays:
        children, cursor = [], 0
        for clip in plan.overlays:
            if clip.record_start > cursor:
                children.append(_gap(clip.record_start - cursor, rate))
            children.append(picture(clip))
            cursor = clip.record_end
        tracks.append(_track("V2", "Video", children, enabled=plan.overlay_enabled))
    if plan.music is not None:
        music = plan.music
        frames = int(music.info.duration * rate)
        children = [_gap(music.record_start, rate)] if music.record_start else []
        ref = _reference(music.info.link or music.info.path, frames, rate)
        children.append(_clip(music.info.path.stem, ref, music.source_in, rate, music.duration, rate, enabled=True,
                              metadata={"studio": {"role": "music", "track_sha256": music.track_sha256, "map_id": music.map_id}}))
        tracks.append(_track("M1", "Audio", children))
    voiced = [c for c in [*plan.storyline, *plan.overlays] if c.kind == "clip" and c.audio and c.enabled and c.info is not None]
    if voiced:
        children, cursor = [], 0
        for clip in sorted(voiced, key=lambda c: c.record_start):
            if clip.record_start > cursor:
                children.append(_gap(clip.record_start - cursor, rate))
            ref = _reference(clip.info.path, clip.info.frames, clip.source_rate)
            children.append(_clip((clip.label or clip.info.path.stem) + " audio", ref, clip.source_in, clip.source_rate,
                                  clip.duration, rate, enabled=True, metadata={"studio": {"id": clip.id, "role": "clip audio"}}))
            cursor = clip.record_end
        tracks.append(_track("A1", "Audio", children))
    for track in plan.sound_tracks:
        # A1-A3: one Audio track each; OTIO has no standard volume effect, so gain and fades are in
        # the clip's studio metadata (gain_db = clip + track gain, as the mix applies it).
        children, cursor = [], 0
        for sound in track.clips:
            if sound.record_start >= total:
                continue
            length = min(sound.duration, total - sound.record_start)
            if sound.record_start > cursor:
                children.append(_gap(sound.record_start - cursor, rate))
            info = sound.info
            available = int(Fraction(info.samples, info.sample_rate) * sound.source_rate) if info.samples else sound.source_in + length
            ref = _reference(info.link or info.path, available, sound.source_rate)  # p12-cut (CUT-021)
            meta = {"studio": {"id": sound.id, "role": track.role, "track": track.id, "gain_db": _number(sound.gain_db),
                               "fade_in_frames": sound.fade_in, "fade_out_frames": sound.fade_out}}
            if sound.volume_keys:  # lane fs-duck: source frames of the clip, dB added to gain_db
                meta["studio"]["volume_keys"] = [{"source_frame": frame, "gain_db": _number(db)} for frame, db in sound.volume_keys]
            children.append(_clip(sound.label or sound.id, ref, sound.source_in, sound.source_rate, length, rate,
                                  enabled=track.audible, metadata=meta))
            cursor = sound.record_start + length
        tracks.append(_track(f"{track.id} {track.role}", "Audio", children, enabled=track.audible))
    markers = []
    for marker in plan.markers:
        if marker.frame < total:
            markers.append({"OTIO_SCHEMA": "Marker.2", "name": marker.name or marker.id, "metadata": {"studio": {"id": marker.id}},
                            "marked_range": _range(marker.frame, rate, marker.duration, rate), "color": "BLUE", "comment": marker.note})
    stack = {"OTIO_SCHEMA": "Stack.1", **_item_common("tracks"), "source_range": None, "children": tracks}
    stack["markers"] = markers
    return {"OTIO_SCHEMA": "Timeline.1", "metadata": {"studio": {"timeline_id": plan.timeline_id, "revision": plan.revision,
                                                                   "width": plan.width, "height": plan.height}},
            "name": name, "global_start_time": _rt(plan.start_tc_frame, rate), "tracks": stack}


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def dumps(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=4, ensure_ascii=False) + "\n"


_ITEM_KEYS = {"OTIO_SCHEMA", "name", "metadata", "effects", "markers", "enabled", "source_range"}


def validate_otio(path: Path, *, rate: Fraction, expected_storyline_clips: int, expected_total_frames: int,
                  rebase: tuple[Path, Path] | None = None, expected_sound_clips: int | None = None,
                  expected_transitions: int | None = None, expected_retimed: int | None = None,
                  expected_nested: int | None = None) -> dict[str, Any]:
    failures: list[str] = []
    report: dict[str, Any] = {"otio": str(path), "failures": failures}
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        failures.append(f"parse: {exc}")
        report["ok"] = False
        return report

    def rt(value: Any, where: str) -> Fraction | None:
        if not isinstance(value, dict) or value.get("OTIO_SCHEMA") != "RationalTime.1":
            failures.append(f"{where} is not a RationalTime.1")
            return None
        r, v = value.get("rate"), value.get("value")
        if not isinstance(r, (int, float)) or r <= 0 or not isinstance(v, (int, float)) or float(v) != int(v):
            failures.append(f"{where} needs a positive rate and a whole-number value")
            return None
        return Fraction(int(v)) / Fraction(r).limit_denominator(1001)

    def time_range(value: Any, where: str) -> tuple[Fraction, Fraction] | None:
        if not isinstance(value, dict) or value.get("OTIO_SCHEMA") != "TimeRange.1":
            failures.append(f"{where} is not a TimeRange.1")
            return None
        start, duration = rt(value.get("start_time"), where + ".start_time"), rt(value.get("duration"), where + ".duration")
        if start is None or duration is None:
            return None
        return start, duration

    if doc.get("OTIO_SCHEMA") != "Timeline.1":
        failures.append("root OTIO_SCHEMA is not Timeline.1")
    rt(doc.get("global_start_time"), "global_start_time")
    stack = doc.get("tracks")
    if not isinstance(stack, dict) or stack.get("OTIO_SCHEMA") != "Stack.1" or not isinstance(stack.get("children"), list):
        failures.append("tracks is not a Stack.1 with children")
        report["ok"] = False
        return report
    lengths: dict[str, Fraction] = {}
    clip_counts: dict[str, int] = {}
    music_clips = 0
    sound_clips = 0
    transitions = 0
    retimed = 0
    nested_stacks = 0
    pending_handles: dict[int, tuple[Fraction, Fraction]] = {}
    missing: list[str] = []
    for index, track in enumerate(stack["children"]):
        where = f"track {index + 1}"
        if not isinstance(track, dict) or track.get("OTIO_SCHEMA") != "Track.1" or track.get("kind") not in {"Video", "Audio"}:
            failures.append(f"{where} is not a Track.1 of kind Video or Audio")
            continue
        length, clips = Fraction(0), 0
        children = []
        # Lane w23-bins (CUT-180): a nested Stack.1 (a nested cut) holds one Video track; its range must equal that
        # track's length, and its items are checked as the track's own (they play in its place).
        for position, item in enumerate(track.get("children") or []):
            if not (isinstance(item, dict) and item.get("OTIO_SCHEMA") == "Stack.1"):
                children.append(item)
                continue
            inner = item.get("children") or []
            span = time_range(item.get("source_range"), f"{where} item {position + 1}.source_range")
            if len(inner) != 1 or not isinstance(inner[0], dict) or inner[0].get("OTIO_SCHEMA") != "Track.1" or inner[0].get("kind") != track.get("kind"):
                failures.append(f"{where} item {position + 1} is a nested Stack.1 without one {track.get('kind')} track")
                continue
            got = Fraction(0)
            for sub in inner[0].get("children") or []:
                sub_span = time_range(sub.get("source_range"), f"{where} nested item") if isinstance(sub, dict) and sub.get("OTIO_SCHEMA") in {"Clip.2", "Gap.1"} else None
                got += sub_span[1] if sub_span else 0
            if span is None or span[0] != 0 or span[1] != got:
                failures.append(f"{where} item {position + 1}: the nested Stack.1 lasts {span and span[1]} s, its track {got} s")
            nested_stacks += 1
            children.extend(inner[0].get("children") or [])
        for position, item in enumerate(children):
            item_where = f"{where} item {position + 1}"
            schema = item.get("OTIO_SCHEMA") if isinstance(item, dict) else None
            if schema == "Transition.1":
                transitions += 1
                before, after = rt(item.get("in_offset"), item_where + ".in_offset"), rt(item.get("out_offset"), item_where + ".out_offset")
                prev = children[position - 1] if position else None
                nxt = children[position + 1] if position + 1 < len(children) else None
                if before is None or after is None or not (isinstance(prev, dict) and prev.get("OTIO_SCHEMA") == "Clip.2"
                                                           and isinstance(nxt, dict) and nxt.get("OTIO_SCHEMA") == "Clip.2"):
                    failures.append(f"{item_where} is a Transition.1 that does not sit between two clips")
                    continue
                pending_handles[id(prev)] = (pending_handles.get(id(prev), (Fraction(0), Fraction(0)))[0], after)
                pending_handles[id(nxt)] = (before, pending_handles.get(id(nxt), (Fraction(0), Fraction(0)))[1])
                continue
            if schema not in {"Clip.2", "Gap.1"} or not _ITEM_KEYS <= set(item):
                failures.append(f"{item_where} is not a complete Clip.2 or Gap.1")
                continue
            span = time_range(item["source_range"], item_where + ".source_range")
            if span is None:
                continue
            if span[1] <= 0:
                failures.append(f"{item_where} has no positive duration")
            length += span[1]
            if schema == "Clip.2":
                clips += 1
                role = ((item.get("metadata") or {}).get("studio") or {}).get("role") if isinstance(item.get("metadata"), dict) else None
                if track.get("kind") == "Audio" and role == "music" and "track" not in ((item.get("metadata") or {}).get("studio") or {}):
                    music_clips += 1
                if track.get("kind") == "Audio" and "track" in ((item.get("metadata") or {}).get("studio") or {}):
                    sound_clips += 1
                refs = item.get("media_references")
                ref = refs.get(item.get("active_media_reference_key")) if isinstance(refs, dict) else None
                if not isinstance(ref, dict) or ref.get("OTIO_SCHEMA") != "ExternalReference.1":
                    failures.append(f"{item_where} has no active ExternalReference.1")
                    continue
                try:
                    file_path = target_path(ref.get("target_url") or "")
                except ValueError as exc:
                    failures.append(f"{item_where}: {exc}")
                    continue
                if rebase is not None:
                    try:
                        file_path = rebase[1] / file_path.relative_to(rebase[0])
                    except ValueError:
                        pass
                if not file_path.is_file():
                    missing.append(str(file_path))
                available = time_range(ref.get("available_range"), item_where + ".available_range")
                warps = [e for e in item.get("effects") or [] if isinstance(e, dict) and e.get("OTIO_SCHEMA") in {"LinearTimeWarp.1", "FreezeFrame.1"}]
                if warps:
                    retimed += 1
                scalar = Fraction(0) if any(e["OTIO_SCHEMA"] == "FreezeFrame.1" for e in warps) else \
                    (Fraction(str(warps[0].get("time_scalar", 1))) if warps else Fraction(1))
                before, after = pending_handles.get(id(item), (Fraction(0), Fraction(0)))
                if available is not None:
                    tail = (span[1] + after) * abs(scalar)
                    if scalar >= 0:
                        lo, hi = span[0] - before * scalar, span[0] + tail
                    else:
                        one = Fraction(1) / Fraction(item["source_range"]["start_time"]["rate"]).limit_denominator(1001)
                        lo, hi = span[0] + one - tail, span[0] + one + before * abs(scalar)
                    if scalar == 0:
                        lo, hi = span[0], span[0]
                    if lo < available[0] or hi > available[0] + available[1]:
                        failures.append(f"{item_where} reads past its media's available range" + (" (the transition needs handles)" if before or after else ""))
        lengths[track.get("name") or where] = length
        clip_counts[track.get("name") or where] = clips
    for marker in stack.get("markers") or []:
        if not isinstance(marker, dict) or marker.get("OTIO_SCHEMA") != "Marker.2" or str(marker.get("color", "")).lower() not in MARKER_COLORS:
            failures.append("a stack marker is not a Marker.2 with a known color")
        else:
            time_range(marker.get("marked_range"), "marker")
    if missing:
        failures.append(f"missing files: {missing}")
    first = stack["children"][0] if stack["children"] else {}
    v1_name = first.get("name") if isinstance(first, dict) else None
    if clip_counts.get(v1_name, -1) != expected_storyline_clips:
        failures.append(f"track 1 has {clip_counts.get(v1_name)} clips, the storyline {expected_storyline_clips}")
    if lengths.get(v1_name) != Fraction(expected_total_frames) / rate:
        failures.append(f"track 1 lasts {lengths.get(v1_name)} s, the storyline {Fraction(expected_total_frames) / rate} s")
    if expected_sound_clips is not None and sound_clips != expected_sound_clips:
        failures.append(f"{sound_clips} sound clips on A1-A3, expected {expected_sound_clips}")
    if expected_sound_clips is not None:
        report["sound_clips"] = sound_clips
    if expected_transitions is not None and transitions != expected_transitions:
        failures.append(f"{transitions} transitions, expected {expected_transitions}")
    if expected_retimed is not None and retimed != expected_retimed:
        failures.append(f"{retimed} clips with a time warp, expected {expected_retimed}")
    report["transitions"] = transitions
    report["retimed_clips"] = retimed
    report["nested_stacks"] = nested_stacks
    if expected_nested is not None and nested_stacks != expected_nested:
        failures.append(f"{nested_stacks} nested stacks on V1, expected {expected_nested}")
    report.update({"storyline_clips": clip_counts.get(v1_name), "music_clips": music_clips, "tracks": {k: str(v) for k, v in lengths.items()}, "missing_paths": missing})
    report["ok"] = not failures
    return report
