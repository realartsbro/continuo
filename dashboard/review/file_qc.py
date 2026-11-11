"""File QC of a written movie (lane w5-review item 2): measured on the file, with ffprobe and ffmpeg.

* A/V start offset: the audio stream's start_time minus the video stream's, in ms and in frames.
* Freeze spans: ``freezedetect`` (noise FREEZE_NOISE_DB, minimum FREEZE_MIN_S).
* Black spans: ``blackdetect`` (picture threshold BLACK_PIXEL_TH, minimum BLACK_MIN_S).
* Loudness: integrated LUFS and true peak, with the export's own measure (timeline_export/render_mix.measure).
* Duration of the video stream vs the audio stream.

Every finding carries its start (seconds, frame, timecode) so the page can jump the viewer there. The
thresholds below are the only knobs; the answer names them so the agent sees the same rules as the page.
"""
from __future__ import annotations

import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env

SCHEMA = "studio.file-qc/1"
OFFSET_MAX_FRAMES = 0.5  # |audio start - video start| above half a frame is a finding (0 frames is the pass)
FREEZE_NOISE_DB = -60.0  # freezedetect noise tolerance
FREEZE_MIN_S = 0.5  # a held picture shorter than this is no finding
BLACK_MIN_S = 0.25  # black shorter than this is no finding
BLACK_PIXEL_TH = 0.10  # blackdetect pix_th: a pixel this dark (0-1) counts as black
BLACK_PICTURE_TH = 0.98  # blackdetect pic_th: this share of black pixels makes a black picture
DURATION_MAX_FRAMES = 1.0  # |video - audio duration| above one frame is a finding
TRUE_PEAK_MAX_DBTP = -1.0  # true peak above the delivery ceiling is a finding
LOUDNESS_MIN_LUFS = -30.0  # integrated loudness quieter than this is a finding (nearly silent mix)
TIMEOUT_S = 1800

THRESHOLDS = {
    "offset_max_frames": OFFSET_MAX_FRAMES, "freeze_noise_db": FREEZE_NOISE_DB, "freeze_min_s": FREEZE_MIN_S,
    "black_min_s": BLACK_MIN_S, "black_pixel_th": BLACK_PIXEL_TH, "black_picture_th": BLACK_PICTURE_TH,
    "duration_max_frames": DURATION_MAX_FRAMES, "true_peak_max_dbtp": TRUE_PEAK_MAX_DBTP, "loudness_min_lufs": LOUDNESS_MIN_LUFS,
}


SYNC_Z_MIN = 2.4  # lane w24-rest: sync_eval.FOREIGN_Z_MIN, the confidence below which the engine calls no match
SYNC_SOURCES = ("scene_engine", "file_start")  # lane w6-scene: which measure the av_offset finding comes from


def scene_sync(path: Path) -> dict[str, Any] | None:
    """Sound vs picture as the scene engine measures it on the mouth (lane w6-scene), or None when the engine
    is not installed, finds no face/sound, or calls the sound foreign: then the stream start times decide."""
    import importlib.util
    import sys

    name = "film_suite_scene_engine"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "scene_core" / "scene_engine.py")
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    if module.locate() is None:
        return None
    out = module.run(path, parts=("sync",))
    sync = ((out or {}).get("parts") or {}).get("sync") or {}
    if not out.get("ok") or not sync.get("available") or sync.get("foreign"):
        return None
    # Lane w24-rest (Deliver QC b): only a mouth measure on SPEECH with a confident peak is a sync measure. On music
    # the engine's offset is offset-only (its z is unusable on songs, sync_eval MUSIC_LOW_ENERGY_MAX) and a peak below
    # the foreign threshold is no match: the file's own start times decide (no finding on a film without dialogue).
    z = sync.get("offset_z")
    speech = sync.get("material") == "speech" and sync.get("offset_only_reason") not in ("music", "foreign")
    if not speech or not isinstance(z, (int, float)) or z < SYNC_Z_MIN:
        return None
    return {"frames": int(sync["offset_frames"]), "offset_only": bool(sync.get("offset_only"))}


class QcError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "retryable": False}


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_S, check=False, env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QcError("qc_failed", "ffmpeg could not read the file.") from exc


def probe(ffprobe: Path, path: Path) -> dict[str, Any]:
    done = _run([str(ffprobe), "-v", "error", "-show_entries",
                 "stream=index,codec_type,start_time,duration,r_frame_rate,avg_frame_rate,nb_frames:format=duration,start_time",
                 "-of", "json", str(path)])
    if done.returncode != 0:
        raise QcError("not_a_movie", "The file could not be read as a movie.")
    try:
        return json.loads(done.stdout or "{}")
    except ValueError:
        raise QcError("not_a_movie", "The file could not be read as a movie.") from None


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _rate(stream: dict[str, Any]) -> Fraction | None:
    for key in ("r_frame_rate", "avg_frame_rate"):
        raw = str(stream.get(key) or "")
        try:
            rate = Fraction(raw)
        except (ValueError, ZeroDivisionError):
            continue
        if 0 < rate < 1000:
            return rate
    return None


def _frames_text(frames: float) -> str:
    """'2 frames', '1 frame', '0.5 frames' (no trailing .0)."""
    value = round(float(frames), 1)
    text = str(int(value)) if value == int(value) else f"{value:g}"
    return f"{text} frame" if text == '1' else f"{text} frames"


def timecode(seconds: float, fps: float) -> str:
    whole = max(0, int(round(seconds * fps)))
    base = max(1, int(round(fps)))
    s, f = divmod(whole, base)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


def _spans(stderr: str, start_key: str, end_key: str) -> list[tuple[float, float]]:
    """Pairs of (start, end) from freezedetect/blackdetect lines; an unclosed span runs to the end (None)."""
    starts = [float(m) for m in re.findall(rf"{start_key}[:=]\s*(-?[\d.]+)", stderr)]
    ends = [float(m) for m in re.findall(rf"{end_key}[:=]\s*(-?[\d.]+)", stderr)]
    return [(s, ends[i] if i < len(ends) else float("nan")) for i, s in enumerate(starts)]


def detect_spans(ffmpeg: Path, path: Path) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    chain = (f"freezedetect=n={FREEZE_NOISE_DB:g}dB:d={FREEZE_MIN_S:g},"
             f"blackdetect=d={BLACK_MIN_S:g}:pix_th={BLACK_PIXEL_TH:g}:pic_th={BLACK_PICTURE_TH:g}")
    done = _run([str(ffmpeg), "-hide_banner", "-nostats", "-i", str(path), "-map", "0:v:0", "-vf", chain, "-an", "-f", "null", "-"])
    if done.returncode != 0:
        raise QcError("qc_failed", "ffmpeg could not read the picture.")
    text = done.stderr or ""
    return _spans(text, r"lavfi\.freezedetect\.freeze_start", r"lavfi\.freezedetect\.freeze_end"), _spans(text, "black_start", "black_end")


def check_file(path: Path, *, ffmpeg: Path, ffprobe: Path, measure_loudness: Callable[[Path, Path], dict[str, Any]] | None = None,
               sync_measure: Callable[[Path], dict[str, Any] | None] | None = scene_sync) -> dict[str, Any]:
    """The QC answer for one written movie: facts, findings (one per issue, with times) and ok."""
    info = probe(ffprobe, path)
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise QcError("no_picture", "The file has no picture.")
    rate = _rate(video) or Fraction(24)
    fps = float(rate)
    fmt_duration = _num((info.get("format") or {}).get("duration"))
    v_start = _num(video.get("start_time")) or 0.0
    v_dur = _num(video.get("duration")) or fmt_duration
    a_start = _num(audio.get("start_time")) if audio else None
    a_dur = (_num(audio.get("duration")) or fmt_duration) if audio else None
    findings: list[dict[str, Any]] = []

    def at(seconds: float) -> dict[str, Any]:
        frame = max(0, int(round((seconds - v_start) * fps)))
        return {"at_s": round(seconds - v_start, 4), "frame": frame, "timecode": timecode(seconds - v_start, fps)}

    offset = None
    source = None
    if audio is not None and a_start is not None:
        delta = a_start - v_start
        source = "file_start"
        engine = None
        if sync_measure is not None:
            try:
                engine = sync_measure(path)
            except Exception:  # noqa: BLE001 - the engine failing never hides the file's own measure
                engine = None
        if engine is not None:
            delta, source = engine["frames"] / fps, "scene_engine"
        offset = {"ms": round(delta * 1000, 1), "frames": round(delta * fps, 2)}
        if abs(delta * fps) > OFFSET_MAX_FRAMES:
            by = " (scene engine, on the mouth)" if source == "scene_engine" else ""
            findings.append({"code": "av_offset", "severity": "warn", **at(v_start),
                             "ms": offset["ms"], "frames": offset["frames"], "source": source,
                             "message": f"Sound {'late' if delta > 0 else 'early'} by {_frames_text(abs(offset['frames']))} ({abs(offset['ms']):.0f} ms){by}."})

    freezes, blacks = detect_spans(ffmpeg, path)
    end = v_start + (v_dur or 0.0)
    for start, stop in freezes:
        stop = end if stop != stop else stop
        findings.append({"code": "freeze", "severity": "warn", **at(start), "duration_s": round(stop - start, 3),
                         "frames": int(round((stop - start) * fps)), "message": f"Picture holds for {stop - start:.2f} s."})
    for start, stop in blacks:
        stop = end if stop != stop else stop
        findings.append({"code": "black", "severity": "warn", **at(start), "duration_s": round(stop - start, 3),
                         "frames": int(round((stop - start) * fps)), "message": f"Black for {stop - start:.2f} s."})

    duration = {"video_s": None if v_dur is None else round(v_dur, 4), "audio_s": None if a_dur is None else round(a_dur, 4)}
    if v_dur is not None and a_dur is not None:
        delta = a_dur - v_dur
        duration["delta_frames"] = round(delta * fps, 2)
        if abs(delta * fps) > DURATION_MAX_FRAMES:
            findings.append({"code": "duration_mismatch", "severity": "warn", **at(v_start + min(v_dur, a_dur)),
                             "message": f"Sound is {abs(delta):.2f} s {'longer' if delta > 0 else 'shorter'} than the picture."})
    if audio is None:
        findings.append({"code": "no_sound", "severity": "info", **at(v_start), "message": "The file has no sound."})

    loudness = None
    if audio is not None and measure_loudness is not None:
        try:
            measured = measure_loudness(ffmpeg, path)
        except Exception:  # noqa: BLE001 - loudness unreadable: said, never guessed
            measured = None
        if measured is not None:
            loudness = {"integrated_lufs": measured.get("integrated_lufs"), "true_peak_dbtp": measured.get("true_peak_dbtp")}
            peak, lufs = loudness["true_peak_dbtp"], loudness["integrated_lufs"]
            if peak is not None and peak > TRUE_PEAK_MAX_DBTP:
                findings.append({"code": "true_peak", "severity": "warn", **at(v_start), "dbtp": peak,
                                 "message": f"True peak {peak:+.1f} dBTP is above {TRUE_PEAK_MAX_DBTP:+.1f} dBTP."})
            if lufs is None or lufs < LOUDNESS_MIN_LUFS:
                findings.append({"code": "quiet", "severity": "warn", **at(v_start), "lufs": lufs,
                                 "message": "The sound is silent." if lufs is None else f"Integrated loudness {lufs:.1f} LUFS is below {LOUDNESS_MIN_LUFS:.0f} LUFS."})
    findings.sort(key=lambda f: (f["at_s"], f["code"]))
    blocking = [f for f in findings if f["severity"] != "info"]
    return {"schema": SCHEMA, "ok": not blocking, "fps": round(fps, 3), "offset": offset, "offset_source": source, "duration": duration,
            "loudness": loudness, "findings": findings, "thresholds": dict(THRESHOLDS)}
