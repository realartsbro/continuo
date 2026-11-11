"""Read-only source resolution: ``source.ref`` -> media facts.

A clip names what it shows by id, never by path. This module turns those ids
into facts the timeline (and the export/render lane) needs: frame count,
rational rate, size, audio, and the backend-private file path. It only reads,
through lookups the caller injects, so it never edits another area's store:

* Shoot takes (``studio:<uuid>``): ``video_job(ref)`` is the generation ledger's
  ``get_job`` (one row by id, so a take older than any listing window still
  resolves). The row must belong to the project, be ``ready`` and published.
* Image pictures (``imgjob_<32 hex>``): ``image_job(ref)`` returns the published
  producer job of the owner scope (ownership is the caller's check) and the
  material path; a picture is a still (``frames: None``).
* The artist's footage (``file:<sha256>``, dashboard/timeline/footage.py):
  ``footage(ref)`` returns ``(path, facts)`` of a file the artist added from one
  of the project's approved folders, or raises with the offline reason
  (``not_found``, ``outside_roots``, ``file_missing``, ``changed``). A video is
  a moving source like a take (kind ``media``), a picture a still.
* Sound files (``file:<sha256>`` too, kind ``audio`` in the index): online with an ``audio``
  block (samples, sample rate, channels, codec, loudness) instead of picture facts.
* Music (``resolve_music``): the pinned ``track_sha256`` -> the analysis cache's
  playback file, through ``music_file(sha)``.
* Anything else, or a lookup that fails, resolves ``offline`` with a reason.
  An offline clip is reported, never dropped.

``probe(path)`` reads the moving facts of a file (``ffprobe_facts`` below is
the default, a fixed argv without a shell).
"""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env



def _take_id_re() -> re.Pattern[str]:
    """The ledger's one take-id definition (generation/take_ids.py, both spellings the ledger
    mints), loaded by path: this core is loaded by path too and imports no dashboard package."""
    path = Path(__file__).resolve().parent.parent / "generation" / "take_ids.py"
    spec = importlib.util.spec_from_file_location("film_suite_timeline_take_ids", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"take id definition is unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.STUDIO_JOB_ID_RE


TAKE_RE = _take_id_re()
FILE_RE = re.compile(r"^file:[0-9a-f]{64}$")
PICTURE_RE = re.compile(r"^imgjob_[0-9a-f]{32}$")

Lookup = Callable[[str], Any]


def _offline(ref: str, kind: str | None, reason: str) -> dict[str, Any]:
    return {"ref": ref, "kind": kind, "status": "offline", "reason": reason, "media_info": None, "source_rate": None, "path": None}


def _rate(value: Any) -> dict[str, int] | None:
    try:
        fraction = Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if fraction <= 0:
        return None
    fraction = fraction.limit_denominator(1_000_000)
    return {"num": fraction.numerator, "den": fraction.denominator}


def ffprobe_facts(path: str | Path, *, ffprobe: str | Path, timeout: float = 30.0) -> dict[str, Any]:
    """frames, rate, width, height, has_audio of one file (exact frame count via -count_packets)."""
    argv = [str(ffprobe), "-v", "error", "-count_packets", "-show_entries",
            "stream=codec_type,width,height,avg_frame_rate,r_frame_rate,nb_frames,nb_read_packets", "-of", "json", str(path)]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False, shell=False, env=child_env())
    if done.returncode != 0:
        raise ValueError("ffprobe could not read the file")
    streams = json.loads(done.stdout or "{}").get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("no video stream")
    frames = None
    for key in ("nb_frames", "nb_read_packets"):
        try:
            frames = int(video.get(key))
        except (TypeError, ValueError):
            continue
        if frames > 0:
            break
    rate = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    if not frames or frames < 1 or rate is None:
        raise ValueError("no frame count or rate")
    return {"frames": frames, "rate": rate, "width": int(video.get("width") or 0) or None,
            "height": int(video.get("height") or 0) or None,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def resolve_ref(
    ref: Any,
    *,
    project_id: str | None,
    video_job: Lookup | None,
    image_job: Lookup | None,
    probe: Callable[[str], dict[str, Any]] | None,
    footage: Lookup | None = None,
) -> dict[str, Any]:
    """Facts for one ref: ``status`` is ``online`` or ``offline`` (with ``reason``)."""
    if type(ref) is not str:
        return _offline(str(ref), None, "unknown_ref")
    if TAKE_RE.fullmatch(ref):
        if video_job is None:
            return _offline(ref, "take", "ledger_unavailable")
        try:
            record = video_job(ref)
        except Exception:  # noqa: BLE001 - unknown or unreadable: offline
            return _offline(ref, "take", "not_found")
        owner = getattr(record, "project_id", None)
        if owner is None:  # Contract-3 rows carry their project only inside the frozen request
            try:
                requested = record.request.get("project_id")
            except Exception:  # noqa: BLE001 - an unreadable request owns nothing
                requested = None
            owner = requested if isinstance(requested, str) and requested else None
        if owner != project_id:
            return _offline(ref, "take", "not_found")  # another project's take reads like an unknown one
        path = getattr(record, "published_path", None)
        if getattr(record, "state", None) != "ready" or not path:
            return _offline(ref, "take", "not_ready")
        if not Path(path).is_file():
            return _offline(ref, "take", "file_missing")
        if probe is None:
            return _offline(ref, "take", "probe_unavailable")
        try:
            facts = probe(path)
        except Exception:  # noqa: BLE001 - unreadable media: offline
            return _offline(ref, "take", "unreadable")
        return {"ref": ref, "kind": "take", "status": "online", "reason": None, "path": str(path),
                "source_rate": facts["rate"],
                "media_info": {"frames": facts["frames"], "width": facts["width"], "height": facts["height"],
                               "has_audio": bool(facts["has_audio"])}}
    if PICTURE_RE.fullmatch(ref):
        if image_job is None:
            return _offline(ref, "picture", "ledger_unavailable")
        try:
            found = image_job(ref)
        except Exception:  # noqa: BLE001 - not owned / unknown: offline
            return _offline(ref, "picture", "not_found")
        if not found:
            return _offline(ref, "picture", "not_found")
        job, path = found
        if job.get("state") != "published":
            return _offline(ref, "picture", "not_ready")
        artifact = job.get("artifact") or {}
        if path is None or not Path(path).is_file():
            return _offline(ref, "picture", "file_missing")
        width, height = artifact.get("width"), artifact.get("height")
        return {"ref": ref, "kind": "picture", "status": "online", "reason": None, "path": str(path),
                "source_rate": None,
                "media_info": {"frames": None, "width": width if type(width) is int else None,
                               "height": height if type(height) is int else None, "has_audio": False}}
    if FILE_RE.fullmatch(ref):
        return _resolve_footage(ref, footage)
    return _offline(ref, None, "unknown_ref")


def _resolve_footage(ref: str, footage: Lookup | None) -> dict[str, Any]:
    if footage is None:
        return _offline(ref, "media", "ledger_unavailable")
    try:
        path, facts = footage(ref)
    except Exception as exc:  # noqa: BLE001 - the index's reason, else not_found
        code = getattr(exc, "code", None)
        return _offline(ref, "media", code if code in {"not_found", "outside_roots", "file_missing", "changed", "ledger_unavailable"} else "not_found")
    if facts.get("kind") == "audio":
        # A sound file: no picture facts; the clip's frames follow from samples at the timeline rate.
        keys = ("samples", "sample_rate", "channels", "codec", "loudness")
        return {"ref": ref, "kind": "media", "status": "online", "reason": None, "path": str(path),
                "source_rate": None, "media_info": None, "audio": {key: facts.get(key) for key in keys},
                "footage": {"name": str(facts.get("name") or Path(path).name)[:200], "codec": str(facts.get("codec") or ""), "vfr": False}}
    frames = facts.get("frames")
    still = facts.get("kind") == "still" or frames is None
    width, height = facts.get("width"), facts.get("height")
    return {"ref": ref, "kind": "media", "status": "online", "reason": None, "path": str(path),
            "source_rate": None if still else facts.get("rate"),
            "media_info": {"frames": None if still else frames, "width": width if type(width) is int else None,
                           "height": height if type(height) is int else None, "has_audio": bool(facts.get("has_audio")) and not still},
            "footage": {"name": str(facts.get("name") or Path(path).name)[:200], "codec": str(facts.get("codec") or ""),
                        "vfr": bool(facts.get("vfr"))}}


def resolve_music(track_sha256: Any, *, music_file: Callable[[str], Any] | None) -> dict[str, Any]:
    """Facts for the music lane's pinned recording: ``music_file(sha)`` is its playable file
    (the analysis cache's playback proxy) or None. Online music carries no frame facts."""
    if type(track_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", track_sha256):
        return _offline(str(track_sha256), "music", "unknown_ref")
    if music_file is None:
        return _offline(track_sha256, "music", "ledger_unavailable")
    try:
        path = music_file(track_sha256)
    except Exception:  # noqa: BLE001 - unreadable cache: offline
        path = None
    if path is None or not Path(path).is_file():
        return _offline(track_sha256, "music", "file_missing")
    return {"ref": track_sha256, "kind": "music", "status": "online", "reason": None, "path": str(path),
            "source_rate": None, "media_info": None}


def resolve_refs(refs: Iterable[Any], **lookups: Any) -> dict[str, dict[str, Any]]:
    """Resolve each distinct ref once, in first-seen order."""
    out: dict[str, dict[str, Any]] = {}
    for ref in refs:
        key = str(ref)
        if key not in out:
            out[key] = resolve_ref(ref, **lookups)
    return out


# The desktop shell's hermes-media://stream/ door (apps/desktop/electron/media-protocol.ts)
# serves only these suffixes, with Range support; anything else would answer 415.
STREAM_SCHEME = "hermes-media://stream/"
STREAMABLE_SUFFIXES = frozenset({".avi", ".flac", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".opus", ".wav", ".webm"})


def stream_url(fact: dict[str, Any]) -> str | None:
    """The playable URL of an online moving source (take, footage video or music); None for stills/offline.

    The same form the renderer builds for Shoot takes and the Music proxy
    (src/shared/ui/thumbnails/media-stream-url.js): the shell resolves it and
    streams with Range. A plugin route cannot serve a <video> element here,
    because the element cannot carry the dashboard session header.
    """
    path = fact.get("path")
    if fact.get("status") != "online" or fact.get("kind") not in {"take", "music", "media"} or type(path) is not str:
        return None
    if fact.get("kind") == "media" and not (fact.get("media_info") or {}).get("frames") and not fact.get("audio"):
        return None
    if Path(path).suffix.lower() not in STREAMABLE_SUFFIXES:
        return None
    return STREAM_SCHEME + quote(path, safe="!~*'()")  # = encodeURIComponent


def public(fact: dict[str, Any]) -> dict[str, Any]:
    """The projection a route may return: no ``path`` field; online moving sources gain ``stream_url``."""
    out = {key: value for key, value in fact.items() if key != "path"}
    url = stream_url(fact)
    if url is not None:
        out["stream_url"] = url
    return out
