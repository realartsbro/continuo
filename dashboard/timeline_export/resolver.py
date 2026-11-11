"""Source resolver interface: ``ref -> {path, frames, rate, width, height, has_audio}``.

Lane fs-tl owns the project resolver (Shoot takes, Image pictures, Media files).
The export depends only on this protocol, so either side can merge first:

    class SourceResolver(Protocol):
        def resolve(self, source: dict) -> SourceInfo | None   # None = offline
        def resolve_music(self, music: dict) -> MusicInfo | None

``source`` is the clip's ``source`` object from ``studio.timeline/1``
(``{kind, ref, slot?}``); ``music`` is the music track's ``music`` object
(``{track_sha256, map_id, ...}``).  Two implementations live here:
``MappingResolver`` (fixtures and tests: facts given), ``ProbeResolver``
(route: ``ref -> path`` given by the caller inside approved roots, facts
measured with ffprobe and the music file's SHA-256 checked against the pin) and
``ResolvedResolver`` (route, the default: the project's own resolver bound each
ref to its published file; facts measured with ffprobe).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .errors import ExportError
from .ffmpeg_run import child_env  # w39-env: one minimal environment for every child process

STILL_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"})
MOVING_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm"})


@dataclass(frozen=True)
class SourceInfo:
    """Facts about one picture source. ``frames is None`` means a still (any length)."""

    path: Path
    frames: int | None
    rate: Fraction | None
    width: int
    height: int
    has_audio: bool
    provenance: Mapping[str, Any] | None = None  # optional take provenance for the manifest
    # Variable frame rate (a phone clip): ``rate`` is the standard rate nearest its average and the
    # export conforms it BY FRAME INDEX (every recorded frame once, at ``rate``), never by time.
    vfr: bool = False
    # ffprobe's colour tags of the picture stream (None = untagged); see colour.py.
    color_primaries: str | None = None
    color_transfer: str | None = None
    color_space: str | None = None

    @property
    def is_still(self) -> bool:
        return self.frames is None


@dataclass(frozen=True)
class MusicInfo:
    path: Path
    duration: Fraction  # seconds
    sample_rate: int
    channels: int
    # Lane p12-cut (CUT-021): the file a hand-over links when ``path`` is a playback copy (the analysis proxy).
    link: Path | None = None


@dataclass(frozen=True)
class SoundInfo:
    """A sound clip's source on A1-A3: a sound file, or the sound of a take / video file."""

    path: Path
    sample_rate: int
    channels: int
    samples: int | None = None  # per channel, decoded (None: not counted, e.g. a take's own sound)
    # Lane p12-cut (CUT-021, charter cut decision 8): the original the hand-over links when ``path`` is the Music
    # analysis cache's playback copy of the same recording (the render reads ``path``; FCPXML / FCP7 / OTIO ``link``).
    link: Path | None = None


class SourceResolver(Protocol):
    def resolve(self, source: Mapping[str, Any]) -> SourceInfo | None: ...

    def resolve_music(self, music: Mapping[str, Any]) -> MusicInfo | None: ...

    # Optional (sound tracks A1-A3): ``resolve_sound(source) -> SoundInfo | None``.


class MappingResolver:
    """Facts given up front, keyed by ``source.ref`` and ``music.track_sha256``.

    ``sounds`` maps a sound clip's ``source.ref`` to its SoundInfo; a ref only in ``sources``
    (a take with sound) is read as the sound of that file (48 kHz stereo assumed)."""

    def __init__(self, sources: Mapping[str, SourceInfo], music: Mapping[str, MusicInfo] | None = None,
                 sounds: Mapping[str, SoundInfo] | None = None) -> None:
        self._sources = dict(sources)
        self._music = dict(music or {})
        self._sounds = dict(sounds or {})

    def resolve(self, source: Mapping[str, Any]) -> SourceInfo | None:
        return self._sources.get(source.get("ref"))

    def resolve_music(self, music: Mapping[str, Any]) -> MusicInfo | None:
        return self._music.get(music.get("track_sha256"))

    def resolve_sound(self, source: Mapping[str, Any]) -> SoundInfo | None:
        ref = source.get("ref")
        if ref in self._sounds:
            return self._sounds[ref]
        info = self._sources.get(ref)
        return None if info is None or not info.has_audio else SoundInfo(info.path, 48000, 2)


def _ffprobe_json(ffprobe: Path, path: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [str(ffprobe), "-v", "error", "-show_entries",
             "format=duration:stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames,duration,sample_rate,channels,color_primaries,color_transfer,color_space",
             "-of", "json", str(path)],
            capture_output=True, text=True, encoding="utf-8", timeout=120, check=True, env=child_env(),
        )
        return json.loads(proc.stdout)
    except FileNotFoundError as exc:
        raise ExportError("ffmpeg_unavailable", "ffprobe is not available at the configured path.") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ExportError("source_unreadable", f"ffprobe could not read {path.name}.") from exc


def count_frames(ffprobe: Path, path: Path, should_cancel: Any = None) -> int:
    """Decoded video frame count (exact, slower than the container's nb_frames).

    w28-fix (CUT-182): with ``should_cancel`` the count is killed when it answers True (polled every 0.2 s), and
    raises ``cancelled``; a whole film takes about a minute at 7200 frames."""
    args = [str(ffprobe), "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", str(path)]
    if should_cancel is None:
        try:
            proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=600, check=True, env=child_env())
        except FileNotFoundError as exc:
            raise ExportError("ffmpeg_unavailable", "ffprobe is not available at the configured path.") from exc
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ExportError("source_unreadable", f"ffprobe could not count frames of {path.name}.") from exc
        stdout = proc.stdout
    else:
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=child_env())
        except FileNotFoundError as exc:
            raise ExportError("ffmpeg_unavailable", "ffprobe is not available at the configured path.") from exc
        deadline = time.monotonic() + 600
        while True:
            try:
                stdout, _stderr = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if should_cancel():
                    proc.kill(); proc.communicate()
                    raise ExportError("cancelled", "The export was cancelled.")
                if time.monotonic() > deadline:
                    proc.kill(); proc.communicate()
                    raise ExportError("source_unreadable", f"ffprobe could not count frames of {path.name}.")
        if proc.returncode != 0:
            raise ExportError("source_unreadable", f"ffprobe could not count frames of {path.name}.")
    value = stdout.strip().splitlines()[0] if stdout.strip() else ""
    return int(value) if value.isdigit() else 0


def _rational(text: Any) -> Fraction | None:
    if not isinstance(text, str) or "/" not in text:
        return None
    a, b = text.split("/", 1)
    try:
        value = Fraction(int(a), int(b))
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


# The standard rates a variable-frame-rate source is conformed to (nearest its average). The
# same list as dashboard/timeline/footage.py STANDARD_RATES (the cut's source_rate must match).
STANDARD_RATES = (Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
                  Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60), Fraction(120))
VFR_TOLERANCE = Fraction(1, 1000)


def nominal_rate(avg: Fraction | None, real: Fraction | None) -> tuple[Fraction | None, bool]:
    """(rate, vfr): the file's own rate when avg_frame_rate and r_frame_rate agree, else the
    standard rate nearest the average and vfr=True."""
    if avg is None and real is None:
        return None, False
    if avg is None or real is None or abs(avg / real - 1) <= VFR_TOLERANCE:
        return (avg or real).limit_denominator(1_000_000), False
    return min(STANDARD_RATES, key=lambda rate: abs(rate - avg)), True


def probe_source(ffprobe: Path, path: Path) -> SourceInfo:
    payload = _ffprobe_json(ffprobe, path)
    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not isinstance(video, dict):
        raise ExportError("source_unreadable", f"{path.name} has no picture.")
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ExportError("source_unreadable", f"{path.name} has no picture size.")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if path.suffix.lower() in STILL_SUFFIXES:
        return SourceInfo(path, None, None, width, height, False)
    rate, vfr = nominal_rate(_rational(video.get("avg_frame_rate")), _rational(video.get("r_frame_rate")))
    if rate is None:
        raise ExportError("source_unreadable", f"{path.name} has no frame rate.")
    frames = count_frames(ffprobe, path)
    if frames <= 0:
        raise ExportError("source_unreadable", f"{path.name} has no decodable frames.")
    def tag(key: str) -> str | None:
        value = video.get(key)
        return value if isinstance(value, str) and value and value not in ("unknown", "reserved", "unspecified") else None

    return SourceInfo(path, frames, rate, width, height, has_audio, vfr=vfr, color_primaries=tag("color_primaries"),
                      color_transfer=tag("color_transfer"), color_space=tag("color_space"))


def probe_music(ffprobe: Path, path: Path) -> MusicInfo:
    payload = _ffprobe_json(ffprobe, path)
    audio = next((s for s in payload.get("streams") or [] if s.get("codec_type") == "audio"), None)
    if not isinstance(audio, dict):
        raise ExportError("source_unreadable", f"{path.name} has no audio.")
    try:
        duration = Fraction(str(audio.get("duration") or (payload.get("format") or {}).get("duration")))
    except (TypeError, ValueError):
        raise ExportError("source_unreadable", f"{path.name} has no duration.") from None
    return MusicInfo(path, duration, int(audio.get("sample_rate") or 48000), int(audio.get("channels") or 2))


def probe_sound(ffprobe: Path, path: Path) -> SoundInfo:
    """Sample rate and channels of the first audio stream; samples per channel by a full decode."""
    payload = _ffprobe_json(ffprobe, path)
    audio = next((s for s in payload.get("streams") or [] if s.get("codec_type") == "audio"), None)
    if not isinstance(audio, dict):
        raise ExportError("source_unreadable", f"{path.name} has no sound.")
    rate, channels = int(audio.get("sample_rate") or 0), int(audio.get("channels") or 0)
    if rate < 1000 or channels < 1:
        raise ExportError("source_unreadable", f"{path.name} has no sample rate or channels.")
    ffmpeg = Path(ffprobe).with_name(Path(ffprobe).name.replace("ffprobe", "ffmpeg"))
    try:
        proc = subprocess.run([str(ffmpeg), "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-af",
                               "astats=measure_perchannel=none:measure_overall=Number_of_samples", "-f", "null", "-"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600, check=True, env=child_env())
    except FileNotFoundError as exc:
        raise ExportError("ffmpeg_unavailable", "ffmpeg is not available next to ffprobe.") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ExportError("source_unreadable", f"ffmpeg could not decode {path.name}.") from exc
    found = [line.rsplit(":", 1)[1].strip() for line in proc.stderr.splitlines() if "Number of samples:" in line]
    samples = int(found[-1]) if found and found[-1].isdigit() else 0
    if samples < 1:
        raise ExportError("source_unreadable", f"{path.name} decodes to no samples.")
    return SoundInfo(path, rate, channels, samples)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class ProbeResolver:
    """``ref -> path`` and ``track_sha256 -> path`` supplied by the caller; facts are measured.

    ``confine`` maps a caller path to a real file inside the project's approved
    roots (the route passes ``_confined_local_path``) and raises otherwise.
    A music file whose SHA-256 differs from the pinned ``track_sha256`` is
    offline: the timeline pins an exact recording.
    """

    def __init__(
        self,
        paths: Mapping[str, str],
        music_paths: Mapping[str, str],
        *,
        ffprobe: Path,
        confine: Callable[[str], Path] = lambda raw: Path(raw),
    ) -> None:
        self._paths = dict(paths)
        self._music_paths = dict(music_paths)
        self._ffprobe = ffprobe
        self._confine = confine
        self._cache: dict[str, SourceInfo] = {}

    def resolve(self, source: Mapping[str, Any]) -> SourceInfo | None:
        ref = source.get("ref")
        raw = self._paths.get(ref) if isinstance(ref, str) else None
        if raw is None:
            return None
        if ref not in self._cache:
            path = self._confine(raw)
            if not path.is_file():
                return None
            self._cache[ref] = probe_source(self._ffprobe, path)
        return self._cache[ref]

    def resolve_music(self, music: Mapping[str, Any]) -> MusicInfo | None:
        sha = music.get("track_sha256")
        raw = self._music_paths.get(sha) if isinstance(sha, str) else None
        if raw is None:
            return None
        path = self._confine(raw)
        if not path.is_file() or sha256_file(path) != sha:
            return None
        return probe_music(self._ffprobe, path)

    def resolve_sound(self, source: Mapping[str, Any]) -> SoundInfo | None:
        ref = source.get("ref")
        raw = self._paths.get(ref) if isinstance(ref, str) else None
        if raw is None:
            return None
        path = self._confine(raw)
        return probe_sound(self._ffprobe, path) if path.is_file() else None


class ResolvedResolver:
    """Files already bound by the project's own resolver (dashboard/timeline/resolver.py).

    The export route builds this when the request names no files: ``paths`` maps each
    online clip ``source.ref`` (Shoot take by ledger id, Image picture by job id) to the
    file that ledger published, ``music_paths`` maps a pinned ``track_sha256`` to the
    Music analysis cache's playback file stored under that very hash. The binding is
    the backend's, so no confinement or re-hash happens here (the playback file is a
    WAV transcode of the pinned recording and cannot carry its hash); facts are still
    measured with ffprobe, exactly like ``ProbeResolver``.
    """

    def __init__(self, paths: Mapping[str, Path], music_paths: Mapping[str, Path], *, ffprobe: Path,
                 links: Mapping[str, Path] | None = None) -> None:
        self._links = {ref: Path(path) for ref, path in (links or {}).items()}
        self._paths = {ref: Path(path) for ref, path in paths.items()}
        self._music_paths = {sha: Path(path) for sha, path in music_paths.items()}
        self._ffprobe = ffprobe
        self._cache: dict[str, SourceInfo] = {}
        self._sounds: dict[str, SoundInfo] = {}

    def resolve(self, source: Mapping[str, Any]) -> SourceInfo | None:
        ref = source.get("ref")
        path = self._paths.get(ref) if isinstance(ref, str) else None
        if path is None or not path.is_file():
            return None
        if ref not in self._cache:
            self._cache[ref] = probe_source(self._ffprobe, path)
        return self._cache[ref]

    def resolve_music(self, music: Mapping[str, Any]) -> MusicInfo | None:
        sha = music.get("track_sha256")
        path = self._music_paths.get(sha) if isinstance(sha, str) else None
        if path is None or not path.is_file():
            return None
        link = self._links.get("file:" + sha)
        return replace(probe_music(self._ffprobe, path), link=link) if link is not None else probe_music(self._ffprobe, path)

    def resolve_sound(self, source: Mapping[str, Any]) -> SoundInfo | None:
        ref = source.get("ref")
        path = self._paths.get(ref) if isinstance(ref, str) else None
        if path is None or not path.is_file():
            return None
        key = "sound:" + ref
        if key not in self._sounds:
            info = probe_sound(self._ffprobe, path)
            self._sounds[key] = replace(info, link=self._links[ref]) if ref in self._links else info
        return self._sounds[key]


def default_tool(name: str) -> Path:
    """ffmpeg/ffprobe next to the configured FFMPEG_PATH, else PATH, else <SystemDrive>\\ffmpeg\\bin
    (generation/ffmpeg_locate.py, one rule for every caller; w20-cat, CATALOG.md #23)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "film_suite_ffmpeg_locate", Path(__file__).resolve().parents[1] / "generation" / "ffmpeg_locate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return Path(module.locate(name))
