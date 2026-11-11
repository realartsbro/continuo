"""FCPXML verifier that runs inside every export; the export fails closed when it fails.

Port of our archived ``nle-timeline-interchange verification tool``
with the gates of TIMELINE-INPUT.md 6.2:

1. the XML parses and is ``fcpxml`` version 1.9 with one sequence and spine;
2. every ``src`` URI is a ``file:`` URI of an existing file;
3. every moving asset (hasVideo=1 on a movie file, or any audio asset) has an
   explicit rational ``duration`` (no decimals);
4. every moving picture asset decodes to more than one frame and its declared
   duration equals the decoded frame count; it has no audio stream when the
   asset says ``hasAudio="0"`` (video-only remux);
5. the spine's ``asset-clip`` count equals the storyline clip count, and the
   music clip count equals the plan's;
6. every clip's source range lies inside its asset's duration; the spine
   offsets are contiguous and sum to the sequence duration;
7. FCPXML 1.9 structure of what we write (fail closed, ``dtd_valid``): every
   ``<asset>`` has one or more ``<media-rep src=...>`` children and no ``src``
   attribute of its own (``<!ELEMENT asset (media-rep+, metadata?)>``), every
   ``format``/``ref`` names an existing resource, IDs are unique.  The full
   Apple DTD runs in the gate (``checks/fcpxml-dtd.cjs``); the DTD text is
   Apple's and is not shipped.

``verify`` returns a report dict; ``report["ok"]`` is the verdict and
``report["failures"]`` names each failed gate.
"""
from __future__ import annotations

import re

import os
import subprocess
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .resolver import MOVING_SUFFIXES, count_frames
from .ffmpeg_run import child_env  # w39-env: one minimal environment for every child process
from .timebase import parse_fcpx_time


def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"unsupported media URI scheme: {uri}")
    raw = unquote(parsed.path)
    if parsed.netloc and parsed.netloc not in {"", "localhost"}:
        raw = f"//{parsed.netloc}{raw}"
    elif os.name == "nt" and len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)


def _has_audio_stream(ffprobe: Path, path: Path) -> bool:
    proc = subprocess.run(
        [str(ffprobe), "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, encoding="utf-8", timeout=120, env=child_env(),
    )
    return bool(proc.stdout.strip())


def _time(node: ET.Element, attr: str, failures: list[str], where: str) -> Fraction | None:
    raw = node.attrib.get(attr)
    if raw is None:
        return None
    if "." in raw:
        failures.append(f"{where} {attr} is a decimal ({raw}); rational frames only")
        return None
    try:
        return parse_fcpx_time(raw)
    except (ValueError, ZeroDivisionError):
        failures.append(f"{where} {attr} is not an FCPXML time ({raw})")
        return None


_MEDIA_REP_KINDS = {"original-media", "proxy-media"}


def _structure_failures(root: ET.Element) -> list[str]:
    """FCPXML 1.9 DTD rules for the elements this exporter writes (asset, media-rep, IDs, IDREFs)."""
    out: list[str] = []
    ids: set[str] = set()
    for node in root.iter():
        node_id = node.attrib.get("id")
        if node_id is not None and node.tag in {"format", "asset", "effect", "media", "text-style-def"}:  # w23-subs: caption styles
            if node_id in ids:
                out.append(f"<{node.tag} id=\"{node_id}\"> uses an ID twice")
            ids.add(node_id)
    for asset in root.findall("./resources/asset"):
        where = f"<asset id=\"{asset.attrib.get('id', '?')}\">"
        if "src" in asset.attrib:
            out.append(f"{where} carries src; FCPXML 1.9 names the file in a <media-rep> child")
        reps = [child for child in asset if child.tag == "media-rep"]
        others = [child.tag for child in asset if child.tag not in {"media-rep", "metadata"}]
        if not reps:
            out.append(f"{where} has no <media-rep> (FCPXML 1.9: asset = media-rep+, metadata?)")
        if others or [child.tag for child in asset][: len(reps)] != ["media-rep"] * len(reps):
            out.append(f"{where} children are not media-rep+, metadata?")
        for rep in reps:
            if not rep.attrib.get("src"):
                out.append(f"{where} <media-rep> has no src")
            if rep.attrib.get("kind", "original-media") not in _MEDIA_REP_KINDS:
                out.append(f"{where} <media-rep kind=\"{rep.attrib.get('kind')}\"> is not original-media or proxy-media")
        fmt = asset.attrib.get("format")
        if fmt is not None and fmt not in ids:
            out.append(f"{where} format=\"{fmt}\" names no resource")
    for node in root.iter():
        for attr in ("ref", "format"):
            value = node.attrib.get(attr)
            if value is not None and node.tag != "asset" and value not in ids:
                out.append(f"<{node.tag} {attr}=\"{value}\"> names no resource")
    return out


_SOUND_ROLE = re.compile(r"(dialogue|music|effects)\.A[1-3]")


def verify(
    fcpxml_path: Path,
    *,
    ffprobe: Path,
    expected_spine_clips: int,
    expected_music_clips: int,
    decode: bool = True,
    rebase: tuple[Path, Path] | None = None,
    expected_sound_clips: int | None = None,
    expected_transitions: int | None = None,
    expected_retimed: int | None = None,
    expected_compounds: int | None = None,
) -> dict[str, Any]:
    """``rebase=(final, staging)``: URIs name the final folder while the package is still staged.
    ``expected_sound_clips``: the A1-A3 clips (audioRole ``<role>.A<n>``), checked when given.
    ``expected_transitions`` / ``expected_retimed``: spine transitions and clips with a timeMap
    (studio.timeline/1.1), checked when given; a retimed clip's media range is read through its
    timeMap and a transition must lie inside its two neighbours with media under it (handles)."""
    failures: list[str] = []
    report: dict[str, Any] = {"fcpxml": str(fcpxml_path), "failures": failures}
    try:
        root = ET.parse(fcpxml_path).getroot()
    except (ET.ParseError, OSError) as exc:
        failures.append(f"parse: {exc}")
        report["ok"] = False
        return report
    if root.tag != "fcpxml" or root.attrib.get("version") != "1.9":
        failures.append("root is not <fcpxml version=\"1.9\">")
    sequences = root.findall("./library/event/project/sequence")
    spines = [sequence.find("spine") for sequence in sequences]
    if len(sequences) != 1 or spines[0] is None:
        failures.append("expected exactly one project sequence with a spine")
        report["ok"] = False
        return report
    sequence, spine = sequences[0], spines[0]
    formats = {node.attrib.get("id"): node for node in root.findall("./resources/format")}
    structure = _structure_failures(root)
    failures.extend(structure)
    assets: dict[str, dict[str, Any]] = {}
    moving_checked = 0
    missing: list[str] = []
    durationless: list[str] = []
    nonmoving: list[dict[str, Any]] = []
    for asset in root.findall("./resources/asset"):
        asset_id = asset.attrib.get("id", "?")
        where = f"asset {asset_id}"
        rep = asset.find("media-rep")
        src = rep.attrib.get("src") if rep is not None else None
        src = src or asset.attrib.get("src")  # a 1.8-style src still resolves, so the report names every fault
        if not src:
            failures.append(f"{where} has no src")
            continue
        try:
            path = uri_to_path(src)
        except ValueError as exc:
            failures.append(f"{where}: {exc}")
            continue
        if rebase is not None:
            try:
                path = rebase[1] / path.relative_to(rebase[0])
            except ValueError:
                pass
        duration = _time(asset, "duration", failures, where)
        start = _time(asset, "start", failures, where) or Fraction(0)
        has_video = asset.attrib.get("hasVideo") == "1"
        has_audio = asset.attrib.get("hasAudio") == "1"
        is_movie = path.suffix.lower() in MOVING_SUFFIXES
        moving = (has_video and is_movie) or (has_audio and not has_video)
        assets[asset_id] = {"path": path, "duration": duration, "start": start, "moving": moving, "has_video": has_video}
        if not path.is_file():
            missing.append(str(path))
            continue
        if not moving:
            continue
        if duration is None:
            durationless.append(str(path))
            continue
        if has_video and is_movie:
            moving_checked += 1
            fmt = formats.get(asset.attrib.get("format"))
            frame_duration = parse_fcpx_time(fmt.attrib["frameDuration"]) if fmt is not None and fmt.attrib.get("frameDuration") else None
            if decode:
                frames = count_frames(ffprobe, path)
                if frames <= 1:
                    nonmoving.append({"path": str(path), "decoded_frames": frames})
                elif frame_duration is not None and duration != frames * frame_duration:
                    failures.append(f"{where} declares {duration}s but decodes to {frames} frames of {frame_duration}s")
                if asset.attrib.get("hasAudio") == "0" and _has_audio_stream(ffprobe, path):
                    failures.append(f"{where} says hasAudio=0 but its file carries audio (not a video-only remux)")
    if missing:
        failures.append(f"missing files: {missing}")
    if durationless:
        failures.append(f"moving assets without duration (Resolve imports them as stills): {durationless}")
    if nonmoving:
        failures.append(f"moving assets with <= 1 decoded frame: {nonmoving}")

    def media_range(node: ET.Element, start: Fraction, end: Fraction, where: str) -> tuple[Fraction, Fraction]:
        """Media times a clip-time range reads: through the timeMap when there is one."""
        time_map = node.find("timeMap")
        if time_map is None:
            return start, end
        points = []
        for point in time_map.findall("timept"):
            t, v = _time(point, "time", failures, where + " timept"), _time(point, "value", failures, where + " timept")
            if t is None or v is None:
                return start, end
            points.append((t, v))
        if len(points) < 2 or any(b[0] <= a[0] for a, b in zip(points, points[1:])):
            failures.append(f"{where} timeMap needs two or more timept in rising time")
            return start, end

        def at(t: Fraction) -> Fraction:
            for (t0, v0), (t1, v1) in zip(points, points[1:]):
                if t <= t1 or (t1, v1) == points[-1]:
                    return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
            return points[-1][1]
        a, b = at(start), at(end)
        return min(a, b), max(a, b)

    def check_clip(node: ET.Element, where: str, *, before: Fraction = Fraction(0), after: Fraction = Fraction(0)) -> None:
        ref = node.attrib.get("ref")
        asset = assets.get(ref or "")
        if asset is None:
            failures.append(f"{where} references unknown asset {ref}")
            return
        start = _time(node, "start", failures, where) or Fraction(0)
        duration = _time(node, "duration", failures, where)
        if duration is None or duration <= 0:
            failures.append(f"{where} has no positive duration")
            return
        lo, hi = media_range(node, start - before, start + duration + after, where)
        if asset["duration"] is not None and asset["moving"] and (lo < asset["start"] - Fraction(1, 1_000_000) or hi > asset["start"] + asset["duration"]):
            what = "its asset's end" if not before and not after else "its asset (the transition needs handles)"
            failures.append(f"{where} reads past {what} ({lo}..{hi} of {asset['duration']})")

    spine_clips = [node for node in spine if node.tag == "asset-clip"]
    compounds_by_id = {node.attrib.get("id", ""): node for node in root.findall("./resources/media")}
    compounds = [0]
    nested_clips: list[ET.Element] = []
    cursor: Fraction | None = None
    total = Fraction(0)
    items = list(spine)
    # A transition overlaps its neighbours: the clip before reads ``after`` past its end, the one
    # after reads ``before`` ahead of its start (FCPXML: transition offset = cut - before).
    handles: dict[int, list[Fraction]] = {}
    transitions = 0
    for index, node in enumerate(items):
        if node.tag != "transition":
            continue
        transitions += 1
        where = f"spine transition {transitions} ({node.attrib.get('name', '')})"
        offset, duration = _time(node, "offset", failures, where), _time(node, "duration", failures, where)
        prev = items[index - 1] if index else None
        nxt = items[index + 1] if index + 1 < len(items) else None
        if offset is None or duration is None or prev is None or nxt is None or prev.tag != "asset-clip" or nxt.tag != "asset-clip":
            failures.append(f"{where} must sit between two clips with an offset and a duration")
            continue
        cut = _time(nxt, "offset", failures, where) or Fraction(0)
        before, after = cut - offset, offset + duration - cut
        prev_start = _time(prev, "offset", failures, where) or Fraction(0)
        nxt_end = cut + (_time(nxt, "duration", failures, where) or Fraction(0))
        if before < 0 or after < 0 or offset < prev_start or offset + duration > nxt_end:
            failures.append(f"{where} does not lie on the cut inside its two clips")
            continue
        handles.setdefault(index - 1, [Fraction(0), Fraction(0)])[1] = after
        handles.setdefault(index + 1, [Fraction(0), Fraction(0)])[0] = before
        if node.find("filter-video") is None:
            failures.append(f"{where} has no video effect")
    for index, node in enumerate(items):
        where = f"spine element {index + 1} ({node.attrib.get('name', node.tag)})"
        if node.tag == "transition":
            continue
        if node.tag == "ref-clip":
            # Lane w23-bins (CUT-180): a compound clip (a nested cut). Its <media> holds one sequence whose spine
            # starts at 0s, is contiguous and lasts what the ref-clip shows; its clips are checked as the storyline's.
            media = compounds_by_id.get(node.attrib.get("ref", ""))
            inner = media.find("sequence/spine") if media is not None else None
            offset = _time(node, "offset", failures, where)
            duration = _time(node, "duration", failures, where) or Fraction(0)
            if inner is None:
                failures.append(f"{where} names no compound clip media with a sequence")
                continue
            if cursor is not None and offset != cursor:
                failures.append(f"{where} offset {offset} is not contiguous (expected {cursor})")
            cursor = (offset or Fraction(0)) + duration
            total += duration
            compounds[0] += 1
            inner_items, inner_cursor = list(inner), Fraction(0)
            if (_time(node, "start", failures, where) or Fraction(0)) != 0:
                failures.append(f"{where} starts inside its compound clip; the export plays it from 0s")
            for sub_index, sub in enumerate(inner_items):
                sub_where = f"{where} > {sub.attrib.get('name', sub.tag)}"
                if sub.tag == "transition":
                    continue
                if sub.tag not in {"asset-clip", "gap"}:
                    failures.append(f"{sub_where} is a {sub.tag}; a compound clip holds clips, gaps and transitions only")
                    continue
                sub_offset = _time(sub, "offset", failures, sub_where)
                if sub_offset != inner_cursor:
                    failures.append(f"{sub_where} offset {sub_offset} is not contiguous (expected {inner_cursor})")
                inner_cursor = (sub_offset or Fraction(0)) + (_time(sub, "duration", failures, sub_where) or Fraction(0))
                if sub.tag == "asset-clip":
                    nested_clips.append(sub)
                    check_clip(sub, sub_where)
            if inner_cursor != duration:
                failures.append(f"{where} lasts {duration}s, its compound clip's spine {inner_cursor}s")
            for child in node:
                if child.tag == "asset-clip":
                    check_clip(child, f"{where} connected {child.attrib.get('name', '')}")
            continue
        if node.tag not in {"asset-clip", "gap"}:
            failures.append(f"{where} is a {node.tag}; the export writes clips, gaps and transitions only")
            continue
        offset = _time(node, "offset", failures, where)
        duration = _time(node, "duration", failures, where) or Fraction(0)
        if cursor is not None and offset != cursor:
            failures.append(f"{where} offset {offset} is not contiguous (expected {cursor})")
        cursor = (offset or Fraction(0)) + duration
        total += duration
        if node.tag == "asset-clip":
            before, after = handles.get(index, [Fraction(0), Fraction(0)])
            check_clip(node, where, before=before, after=after)
        for child in node:
            if child.tag == "asset-clip":
                check_clip(child, f"{where} connected {child.attrib.get('name', '')}")
    retimed = sum(1 for node in root.iter("asset-clip") if node.find("timeMap") is not None)
    if expected_transitions is not None and transitions != expected_transitions:
        failures.append(f"{transitions} transitions on the spine, expected {expected_transitions}")
    if expected_retimed is not None and retimed != expected_retimed:
        failures.append(f"{retimed} clips with a timeMap, expected {expected_retimed}")
    report["transitions"] = transitions
    report["retimed_clips"] = retimed
    sequence_duration = _time(sequence, "duration", failures, "sequence")
    tc_start = _time(sequence, "tcStart", failures, "sequence") or Fraction(0)
    first_offset = _time(spine[0], "offset", failures, "spine start") if len(spine) else None
    if first_offset is not None and first_offset != tc_start:
        failures.append(f"spine starts at {first_offset}, not at tcStart {tc_start}")
    if sequence_duration is not None and sequence_duration != total:
        failures.append(f"spine lasts {total}s, sequence says {sequence_duration}s")
    # Lane p12-cut (CUT-021): the song on the music track (audioRole music.A<n>) is a music clip too.
    music = [node for node in root.iter("asset-clip") if node.attrib.get("audioRole") == "music" or node.attrib.get("audioRole", "").startswith("music.")]
    if len(spine_clips) + len(nested_clips) != expected_spine_clips:
        failures.append(f"spine has {len(spine_clips) + len(nested_clips)} clips (compound clips' included), the storyline {expected_spine_clips}")
    if expected_compounds is not None and compounds[0] != expected_compounds:
        failures.append(f"{compounds[0]} compound clips on the spine, expected {expected_compounds}")
    report["compound_clips"] = compounds[0]
    if len(music) != expected_music_clips:
        failures.append(f"{len(music)} music clips, expected {expected_music_clips}")
    if expected_sound_clips is not None:
        sound = [node for node in root.iter("asset-clip") if _SOUND_ROLE.fullmatch(node.attrib.get("audioRole", ""))]
        if len(sound) != expected_sound_clips:
            failures.append(f"{len(sound)} sound clips on A1-A3, expected {expected_sound_clips}")
        report["sound_clips"] = len(sound)
    report.update({
        "primary_spine_clips": len(spine_clips),
        "music_clips": len(music),
        "moving_assets_checked": moving_checked,
        "missing_paths": missing,
        "durationless_video_assets": durationless,
        "nonmoving_assets": nonmoving,
        "dtd_valid": not structure,
    })
    report["ok"] = not failures
    return report
