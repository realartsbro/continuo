"""FCP7 XML (xmeml 5) verifier; runs inside the export, which fails closed when it fails.

Gates (artist decision 14:35, same idea as the FCPXML verifier):
1. the XML parses; root ``xmeml version="5"``; one sequence;
2. the sequence rate is the timeline's (``timebase`` 24, ``ntsc FALSE`` for 24/1);
3. every ``file`` with a ``pathurl`` exists; video files decode to more than one
   frame and their declared ``duration`` equals the decoded count; files used
   on video tracks carry no audio stream (video-only remux);
4. every ``clipitem`` has integer ``start/end/in/out/duration`` with
   ``in < out <= duration`` and ``end - start == out - in``; clips on one track
   do not overlap;
5. video track 1 holds exactly the storyline's clip count; audio track 1 the
   music clip count.
"""
from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .export_fcp7 import timebase
from .resolver import STILL_SUFFIXES, count_frames
from .ffmpeg_run import child_env  # w39-env: one minimal environment for every child process


def pathurl_to_path(url: str) -> Path:
    parsed = urlparse(url)
    if parsed.scheme != "file":
        raise ValueError(f"unsupported pathurl scheme: {url}")
    raw = unquote(parsed.path)
    if parsed.netloc not in {"", "localhost"}:
        raw = f"//{parsed.netloc}{raw}"
    elif os.name == "nt" and len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)


def _int(node: ET.Element, tag: str, failures: list[str], where: str) -> int | None:
    child = node.find(tag)
    text = None if child is None else (child.text or "").strip()
    if not text or not text.lstrip("-").isdigit():
        failures.append(f"{where} has no whole-number <{tag}>")
        return None
    return int(text)


def _has_audio(ffprobe: Path, path: Path) -> bool:
    proc = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True, encoding="utf-8", timeout=120, env=child_env())
    return bool(proc.stdout.strip())


def _ceil(value: Fraction) -> int:
    value = Fraction(value)
    return -(-value.numerator // value.denominator)


def _round_half(value: Fraction) -> int:
    value = Fraction(value) + Fraction(1, 2)
    return value.numerator // value.denominator


def _graph(item: ET.Element) -> list[tuple[int, Fraction]]:
    """The Time Remap ``graphdict`` keyframes ``(when, value)`` of a clipitem (empty without)."""
    for effect in item.findall("filter/effect"):
        if effect.findtext("effectid") != "timeremap":
            continue
        for parameter in effect.findall("parameter"):
            if parameter.findtext("parameterid") == "graphdict":
                try:
                    return [(int(k.findtext("when")), Fraction(k.findtext("value"))) for k in parameter.findall("keyframe")]
                except (TypeError, ValueError):
                    return []
    return []


def verify_fcp7(
    path: Path,
    *,
    ffprobe: Path,
    rate: Fraction,
    expected_storyline_clips: int,
    expected_music_clips: int,
    decode: bool = True,
    rebase: tuple[Path, Path] | None = None,
    expected_sound_clips: int | None = None,
    expected_transitions: int | None = None,
    expected_retimed: int | None = None,
    expected_nested: int | None = None,
) -> dict[str, Any]:
    """``expected_transitions`` / ``expected_retimed`` (studio.timeline/1.1): video track 1
    transitionitems (each on a cut between two clipitems, inside both, with the handles it needs
    in their files) and clipitems with a Time Remap filter (their in/out span = length * speed)."""
    failures: list[str] = []
    report: dict[str, Any] = {"xml": str(path), "failures": failures}
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        failures.append(f"parse: {exc}")
        report["ok"] = False
        return report
    if root.tag != "xmeml" or root.attrib.get("version") != "5":
        failures.append('root is not <xmeml version="5">')
    sequences = root.findall("sequence")
    if len(sequences) != 1:
        failures.append("expected exactly one sequence")
        report["ok"] = False
        return report
    sequence = sequences[0]
    tb, ntsc = timebase(rate)
    seq_rate = sequence.find("rate")
    if seq_rate is None or (seq_rate.findtext("timebase"), seq_rate.findtext("ntsc")) != (str(tb), "TRUE" if ntsc else "FALSE"):
        failures.append(f"sequence rate is not timebase {tb} ntsc {'TRUE' if ntsc else 'FALSE'}")

    # Files: defined once (with children), referenced elsewhere by id only.
    definitions: dict[str, ET.Element] = {}
    for node in root.iter("file"):
        if node.find("pathurl") is not None:
            definitions[node.attrib.get("id", "")] = node
    video_track_files: set[str] = set()
    tracks = {kind: sequence.findall(f"media/{kind}/track") for kind in ("video", "audio")}
    # Lane w23-bins (CUT-180): nested sequences' video tracks count as video tracks too.
    for track in [t for s in root.iter("sequence") for t in s.findall("media/video/track")]:
        for item in track.findall("clipitem"):
            file_node = item.find("file")
            if file_node is not None:
                video_track_files.add(file_node.attrib.get("id", ""))
    missing: list[str] = []
    nonmoving: list[dict[str, Any]] = []
    checked = 0
    for file_id, node in definitions.items():
        try:
            file_path = pathurl_to_path(node.findtext("pathurl") or "")
        except ValueError as exc:
            failures.append(f"file {file_id}: {exc}")
            continue
        if rebase is not None:
            try:
                file_path = rebase[1] / file_path.relative_to(rebase[0])
            except ValueError:
                pass
        if not file_path.is_file():
            missing.append(str(file_path))
            continue
        declared = _int(node, "duration", failures, f"file {file_id}")
        if not decode or file_path.suffix.lower() in STILL_SUFFIXES or node.find("media/video") is None:
            continue
        checked += 1
        frames = count_frames(ffprobe, file_path)
        if frames <= 1:
            nonmoving.append({"path": str(file_path), "decoded_frames": frames})
        elif declared is not None and declared != frames:
            failures.append(f"file {file_id} declares {declared} frames but decodes to {frames}")
        if file_id in video_track_files and _has_audio(ffprobe, file_path):
            failures.append(f"file {file_id} on a video track carries audio (not a video-only remux)")
    if missing:
        failures.append(f"missing files: {missing}")
    if nonmoving:
        failures.append(f"moving files with <= 1 decoded frame: {nonmoving}")
    for item in root.iter("clipitem"):
        if item.find("sequence") is not None:  # a nested sequence holds the media (lane w23-bins)
            continue
        file_node = item.find("file")
        if file_node is None or file_node.attrib.get("id") not in definitions:
            failures.append(f"clipitem {item.attrib.get('id')} has no defined file")

    remapped = [0]
    transition_count = [0]
    nested = [0]

    def speed_of(item: ET.Element) -> tuple[Fraction, bool] | None:
        for effect in item.findall("filter/effect"):
            if effect.findtext("effectid") == "timeremap":
                params = {p.findtext("parameterid"): p for p in effect.findall("parameter")}
                frozen = (params.get("variablespeed") is not None and params["variablespeed"].findtext("value") == "1")
                speed = Fraction(params["speed"].findtext("value")) / 100 if params.get("speed") is not None else Fraction(1)
                return speed, frozen
        return None

    def check_track(track: ET.Element, label: str) -> int:
        count = 0
        previous_end: int | None = None
        children = list(track)
        for position, item in enumerate(children):
            if item.tag == "transitionitem":
                transition_count[0] += 1
                where = f"{label} transitionitem {transition_count[0]}"
                start, end = _int(item, "start", failures, where), _int(item, "end", failures, where)
                prev = next((c for c in reversed(children[:position]) if c.tag == "clipitem"), None)
                nxt = next((c for c in children[position + 1:] if c.tag == "clipitem"), None)
                if None in (start, end, prev, nxt) or item.find("effect") is None:
                    failures.append(f"{where} needs start, end, an effect and a clipitem on each side")
                    continue
                cut = _int(nxt, "start", failures, where)
                prev_values = {t: _int(prev, t, failures, where) for t in ("start", "end", "in", "out", "duration")}
                next_values = {t: _int(nxt, t, failures, where) for t in ("start", "end", "in", "out", "duration")}
                if cut is None or None in prev_values.values() or None in next_values.values():
                    continue
                if not (prev_values["start"] <= start <= cut <= end <= next_values["end"]) or prev_values["end"] != cut:
                    failures.append(f"{where} does not lie on the cut inside its two clips ({start}..{end}, cut {cut})")
                    continue
                before, after = cut - start, end - cut
                ps, ns = speed_of(prev), speed_of(nxt)
                need_prev = after if ps is None else (0 if ps[1] else _ceil(after * ps[0]))
                need_next = before if ns is None else (0 if ns[1] else _ceil(before * ns[0]))
                if prev_values["out"] + need_prev > prev_values["duration"] or next_values["in"] - need_next < 0:
                    failures.append(f"{where} needs handles its clips' files do not have")
                continue
            if item.tag != "clipitem":
                continue
            where = f"{label} clipitem {item.attrib.get('id')}"
            values = {tag: _int(item, tag, failures, where) for tag in ("start", "end", "in", "out", "duration")}
            inner = item.find("sequence")
            if inner is not None:
                # Lane w23-bins (CUT-180): a nested sequence. It plays from its own frame `in`; its one video track
                # ends inside its duration; its clips are checked (and counted) as the storyline's own.
                nested[0] += 1
                inner_tracks = inner.findall("media/video/track")
                length_text = inner.findtext("duration")
                if None in values.values() or len(inner_tracks) != 1 or length_text is None:
                    failures.append(f"{where} is a nested sequence without one video track and its duration")
                    continue
                if not (0 <= values["in"] < values["out"] <= values["duration"] == int(length_text)) or values["end"] - values["start"] != values["out"] - values["in"]:
                    failures.append(f"{where} nested sequence range is not inside its duration ({values}, {length_text})")
                last = max((int(c.findtext("end") or 0) for c in inner_tracks[0].findall("clipitem")), default=0)
                if last > int(length_text):
                    failures.append(f"{where} nested sequence's clips end at {last}, after its duration {length_text}")
                count += check_track(inner_tracks[0], f"{label} nested {item.attrib.get('id')}")
                if previous_end is not None and values["start"] < previous_end:
                    failures.append(f"{where} overlaps the clip before it")
                previous_end = values["end"]
                continue
            count += 1
            if None in values.values():
                continue
            if not (0 <= values["in"] < values["out"] <= values["duration"]):
                failures.append(f"{where} needs 0 <= in < out <= duration ({values})")
            remap = speed_of(item)
            length = values["end"] - values["start"]
            graph = _graph(item)
            if remap is not None and remap[1] and len(graph) >= 2 and len({v for _w, v in graph}) > 1:
                # Variable speed (a speed curve): keys from 0 to the clip's length in rising time,
                # in/out span the media frames the graph reads, inside the file.
                remapped[0] += 1
                whens = [w for w, _v in graph]
                if whens[0] != 0 or whens[-1] != length or any(b <= a for a, b in zip(whens, whens[1:])):
                    failures.append(f"{where} Time Remap graphdict must run from 0 to {length} in rising time ({whens[:3]}..{whens[-1:]})")
                lo, hi = min(v for _w, v in graph), max(v for _w, v in graph)
                if lo < values["in"] or _ceil(hi) > values["out"]:
                    failures.append(f"{where} Time Remap graphdict reads {lo}..{hi}, outside in/out {values['in']}..{values['out']}")
            elif remap is not None:
                remapped[0] += 1
                want = 1 if remap[1] else max(1, _round_half(length * remap[0]))
                if values["out"] - values["in"] != want:
                    failures.append(f"{where} Time Remap {remap[0] * 100}% reads {values['out'] - values['in']} frames, expected {want}")
            elif length != values["out"] - values["in"]:
                failures.append(f"{where} record length differs from source length ({values})")
            if previous_end is not None and values["start"] < previous_end:
                failures.append(f"{where} overlaps the clip before it")
            previous_end = values["end"]
        return count

    counts = {f"{kind}{index + 1}": check_track(track, f"{kind} track {index + 1}")
              for kind in ("video", "audio") for index, track in enumerate(tracks[kind])}
    storyline = counts.get("video1", 0)
    music = counts.get("audio1", 0) if expected_music_clips else 0
    if storyline != expected_storyline_clips:
        failures.append(f"video track 1 has {storyline} clips, the storyline {expected_storyline_clips}")
    if expected_music_clips and music != expected_music_clips:
        failures.append(f"audio track 1 has {music} clips, expected {expected_music_clips} music clip(s)")
    if expected_sound_clips is not None:
        sound = sum(len(track.findall("clipitem")) for track in tracks["audio"] if "MZ.TrackName" in track.attrib)
        if sound != expected_sound_clips:
            failures.append(f"{sound} sound clips on A1-A3, expected {expected_sound_clips}")
        report["sound_clips"] = sound
    if expected_transitions is not None and transition_count[0] != expected_transitions:
        failures.append(f"{transition_count[0]} transitionitems, expected {expected_transitions}")
    if expected_retimed is not None and remapped[0] != expected_retimed:
        failures.append(f"{remapped[0]} clipitems with Time Remap, expected {expected_retimed}")
    if expected_nested is not None and nested[0] != expected_nested:
        failures.append(f"{nested[0]} nested sequences on V1, expected {expected_nested}")
    report["nested_sequences"] = nested[0]
    report["transitions"] = transition_count[0]
    report["retimed_clips"] = remapped[0]
    report.update({"storyline_clips": storyline, "music_clips": music, "moving_files_checked": checked,
                   "missing_paths": missing, "nonmoving_files": nonmoving, "tracks": counts})
    report["ok"] = not failures
    return report
