"""The artist's own footage as a timeline source: ``file:<sha256 of the content>``.

A video or still from one of the project's approved folders enters the cut by a
stable id, never by its path. ``FootageIndex`` keeps, per project and in the
backend's private state (never in the renderer, never in a route answer), the
file each id was read from, its size and mtime, and its probe:

* ``add(path)``: the caller has already confined ``path`` to the project's
  approved roots. The content is hashed once (SHA-256, streamed) and cached by
  ``(path, size, mtime_ns)``; a re-add of an unchanged file does not hash again.
  The id is the content hash, so the same file added again (another folder,
  another computer) is the same source.
* ``locate(ref, roots)``: the file of an id for the resolver, or a reason:
  ``not_found`` (never added to this project), ``outside_roots`` (its folder is
  no longer approved), ``file_missing`` (moved or deleted), ``changed`` (the
  bytes at that path are no longer the ones the id names). An offline clip is
  reported, never dropped.
* Relink (lane p5d-med, CUT-079): an id with no entry here (a project copied or
  moved from another home: the index is per machine) or whose file is gone is
  searched for INSIDE the approved roots by content: same name first, then same
  size, then the rest; only media files are candidates, each is hashed once and
  remembered by ``(path, size, mtime_ns)`` in ``<index>.seen.json``, the walk
  stops at the first match and the file goes back into the index (the next read
  is an index hit). ``changed`` and ``outside_roots`` keep their meaning.

``probe_footage`` measures what the cut needs: frames (packet count), the
rational rate (23.976 / 29.97 / 48 / 50 / 60 ... as exact fractions), size,
audio, codec. A variable-frame-rate file (phones) is conformed BY FRAME INDEX:
every recorded frame is kept once and played at the nearest standard rate to
its average (``vfr: true``); the export renders and hands over the same frames
(dashboard/timeline_export: render_film / export_package).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Iterable

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env

FILE_REF_RE = re.compile(r"^file:[0-9a-f]{64}$")
STILL_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"})
MOVING_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm"})
# Sound files (lane fs-a2): the same ids, index and offline reasons as footage; facts are
# samples (exact decoded count), sample rate, channels, codec and loudness (ebur128).
AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".flac", ".opus", ".ogg", ".oga", ".m4a", ".aac"})
STANDARD_RATES = (Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
                  Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60), Fraction(120))
# avg_frame_rate further than this from r_frame_rate = variable frame rate (CFR files agree to ~1e-5).
VFR_TOLERANCE = Fraction(1, 1000)
INDEX_SCHEMA = "studio.footage-index/1"
INDEX_MAX_FILES = 5000
# Relink walks at most this many media files under the roots (listing is stat only; hashes are cached).
RELINK_MAX_FILES = 20000
MEDIA_SUFFIXES = STILL_SUFFIXES | MOVING_SUFFIXES | AUDIO_SUFFIXES


class FootageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fraction(text: Any) -> Fraction | None:
    if not isinstance(text, str) or "/" not in text:
        return None
    a, b = text.split("/", 1)
    try:
        value = Fraction(int(a), int(b))
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def nominal_rate(avg: Fraction | None, real: Fraction | None) -> tuple[Fraction | None, bool]:
    """(rate, vfr): the file's own rate when avg and r agree, else the standard rate nearest the average."""
    if avg is None and real is None:
        return None, False
    if avg is None or real is None or abs(avg / real - 1) <= VFR_TOLERANCE:
        return (avg or real).limit_denominator(1_000_000), False
    return min(STANDARD_RATES, key=lambda rate: abs(rate - avg)), True


def rate_dict(rate: Fraction | None) -> dict[str, int] | None:
    return None if rate is None else {"num": rate.numerator, "den": rate.denominator}


def probe_footage(path: str | Path, *, ffprobe: str | Path, timeout: float = 120.0) -> dict[str, Any]:
    """kind, frames, rate, width, height, has_audio, codec, vfr of one approved file (fixed argv, no shell)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in STILL_SUFFIXES and suffix not in MOVING_SUFFIXES:
        raise FootageError("unsupported", "Only videos (MP4, MOV, M4V, MKV, WebM), pictures and sound files (WAV, MP3, FLAC, Opus, Ogg, M4A, AAC) go into the cut.")
    argv = [str(ffprobe), "-v", "error", *(["-count_packets"] if suffix in MOVING_SUFFIXES else []), "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames,nb_read_packets", "-of", "json", str(path)]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False, shell=False, env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FootageError("unreadable", "ffprobe could not read the file.") from exc
    if done.returncode != 0:
        raise FootageError("unreadable", "ffprobe could not read the file.")
    streams = json.loads(done.stdout or "{}").get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise FootageError("unreadable", "The file has no picture.")
    width, height = int(video.get("width") or 0) or None, int(video.get("height") or 0) or None
    codec = str(video.get("codec_name") or "")[:40]
    if suffix in STILL_SUFFIXES:
        return {"kind": "still", "frames": None, "rate": None, "width": width, "height": height,
                "has_audio": False, "codec": codec, "vfr": False}
    frames = 0
    for key in ("nb_read_packets", "nb_frames"):
        try:
            frames = int(video.get(key))
        except (TypeError, ValueError):
            continue
        if frames > 0:
            break
    rate, vfr = nominal_rate(_fraction(video.get("avg_frame_rate")), _fraction(video.get("r_frame_rate")))
    if frames < 1 or rate is None:
        raise FootageError("unreadable", "The file has no frame count or frame rate.")
    return {"kind": "video", "frames": frames, "rate": rate_dict(rate), "width": width, "height": height,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams), "codec": codec, "vfr": vfr}


def _ffmpeg_of(ffprobe: str | Path) -> Path:
    probe = Path(ffprobe)
    return probe.with_name(probe.name.replace("ffprobe", "ffmpeg"))


_SAMPLES_RE = re.compile(r"Number of samples:\s+(\d+)")
_LUFS_RE = re.compile(r"I:\s+(-?[\d.]+|-inf) LUFS")
_LRA_RE = re.compile(r"LRA:\s+(-?[\d.]+|-inf) LU\b")
_PEAK_RE = re.compile(r"Peak:\s+(-?[\d.]+|-inf) dBFS")


def _db(text: str | None) -> float | None:
    if text is None or text == "-inf":
        return None
    return round(float(text), 1)


def measure_loudness(path: str | Path, *, ffmpeg: str | Path, timeout: float = 600.0) -> dict[str, Any]:
    """One decode: exact samples per channel (astats) and EBU R128 loudness (ebur128, true peak).

    ``integrated_lufs`` / ``true_peak_dbtp`` are what ffmpeg's ebur128 summary prints (0.1 LU
    resolution); silence reads ``None`` (-inf). A fixed argv, no shell."""
    argv = [str(ffmpeg), "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-af",
            "astats=measure_perchannel=none:measure_overall=Number_of_samples,ebur128=peak=true:framelog=quiet",
            "-f", "null", "-"]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              check=False, shell=False, env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FootageError("unreadable", "ffmpeg could not read the sound.") from exc
    text = done.stderr or ""
    if done.returncode != 0 or "Summary:" not in text:
        raise FootageError("unreadable", "ffmpeg could not read the sound.")
    summary = text[text.rindex("Summary:"):]
    samples = _SAMPLES_RE.findall(text)
    lufs, lra, peak = _LUFS_RE.search(summary), _LRA_RE.search(summary), _PEAK_RE.search(summary)
    lufs_value = _db(lufs.group(1) if lufs else None)
    return {
        "samples": int(samples[-1]) if samples else 0,
        # ebur128 floors an unmeasurable (silent) programme at -70 LUFS: no loudness.
        "integrated_lufs": None if lufs_value is None or lufs_value <= -70.0 else lufs_value,
        "true_peak_dbtp": _db(peak.group(1) if peak else None),
        "lra_lu": _db(lra.group(1) if lra else None),
    }


def probe_audio(path: str | Path, *, ffprobe: str | Path, ffmpeg: str | Path | None = None, timeout: float = 600.0) -> dict[str, Any]:
    """kind audio: samples (per channel, decoded), sample_rate, channels, codec and loudness."""
    path = Path(path)
    argv = [str(ffprobe), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name,sample_rate,channels",
            "-of", "json", str(path)]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False, shell=False, env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FootageError("unreadable", "ffprobe could not read the file.") from exc
    streams = json.loads(done.stdout or "{}").get("streams") or [] if done.returncode == 0 else []
    if not streams:
        raise FootageError("unreadable", "The file has no sound.")
    stream = streams[0]
    try:
        sample_rate, channels = int(stream.get("sample_rate") or 0), int(stream.get("channels") or 0)
    except (TypeError, ValueError):
        sample_rate = channels = 0
    if sample_rate < 1000 or channels < 1:
        raise FootageError("unreadable", "The sound has no sample rate or channels.")
    measured = measure_loudness(path, ffmpeg=ffmpeg or _ffmpeg_of(ffprobe), timeout=timeout)
    if measured["samples"] < 1:
        raise FootageError("unreadable", "The sound decodes to no samples.")
    return {"kind": "audio", "frames": None, "rate": None, "width": None, "height": None, "has_audio": True,
            "codec": str(stream.get("codec_name") or "")[:40], "vfr": False, "samples": measured["samples"],
            "sample_rate": sample_rate, "channels": channels,
            "loudness": {k: measured[k] for k in ("integrated_lufs", "true_peak_dbtp", "lra_lu")}}


def probe_media(path: str | Path, *, ffprobe: str | Path) -> dict[str, Any]:
    """Footage (video, still) or a sound file, by suffix."""
    if Path(path).suffix.lower() in AUDIO_SUFFIXES:
        return probe_audio(path, ffprobe=ffprobe)
    return probe_footage(path, ffprobe=ffprobe)


def audio_frames(facts: dict[str, Any], rate: dict[str, int]) -> int:
    """A sound file's whole frames at the timeline rate (the frames a clip may read)."""
    return facts["samples"] * rate["num"] // (facts["sample_rate"] * rate["den"])


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, roots: Iterable[str]) -> bool:
    candidate = os.path.normcase(os.path.realpath(str(path)))
    for root in roots:
        base = os.path.normcase(os.path.realpath(str(root)))
        try:
            if os.path.commonpath([candidate, base]) == base:
                return True
        except ValueError:
            continue
    return False


class FootageIndex:
    """One project's footage ids -> (file, size, mtime_ns, probe), as private JSON, written atomically."""

    _locks: dict[str, threading.Lock] = {}
    _guard = threading.Lock()

    def __init__(self, path: str | Path, project_id: str, *, probe: Callable[[Path], dict[str, Any]],
                 hasher: Callable[[Path], str] = sha256_file, relink_probe: Callable[[Path], dict[str, Any]] | None = None) -> None:
        self.path = Path(path)
        self.project_id = project_id
        self._probe = probe
        self._relink_probe = relink_probe
        self._hash = hasher
        self._listing: dict[tuple[str, ...], list[tuple[Path, int, int]]] = {}
        with self._guard:
            self._lock = self._locks.setdefault(str(self.path).lower(), threading.Lock())

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}  # a damaged private index is rebuilt by the next add; clips read offline meanwhile
        if not isinstance(value, dict) or value.get("schema") != INDEX_SCHEMA or value.get("project_id") != self.project_id:
            return {}
        files = value.get("files")
        return files if isinstance(files, dict) else {}

    def _write(self, files: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"schema": INDEX_SCHEMA, "project_id": self.project_id, "files": files}, sort_keys=True, ensure_ascii=False)
        fd, name = tempfile.mkstemp(prefix=".footage-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(name, self.path)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    def add(self, path: str | Path) -> dict[str, Any]:
        """Register one confined file: {ref, name, facts}. Hashes only a new or changed file."""
        path = Path(path)
        stat = path.stat()
        with self._lock:
            files = self._read()
            sha = next((sha for sha, entry in files.items() if entry.get("path") == str(path)
                        and entry.get("size") == stat.st_size and entry.get("mtime_ns") == stat.st_mtime_ns), None)
            if sha is None:
                sha = self._hash(path)
                entry = files.get(sha)
                facts = entry["facts"] if isinstance(entry, dict) and isinstance(entry.get("facts"), dict) else self._probe(path)
                if sha not in files and len(files) >= INDEX_MAX_FILES:
                    raise FootageError("too_many", f"A project holds at most {INDEX_MAX_FILES} footage files.")
                files[sha] = {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "name": path.name[:200], "facts": facts}
                self._write(files)
            entry = files[sha]
            return {"ref": "file:" + sha, "name": entry["name"], "facts": entry["facts"]}

    def locate(self, ref: str, roots: Iterable[str]) -> tuple[Path, dict[str, Any]]:
        """(file, facts) of one id, or FootageError with the offline reason."""
        if not isinstance(ref, str) or not FILE_REF_RE.fullmatch(ref):
            raise FootageError("unknown_ref", "Not a footage id.")
        sha = ref[5:]
        roots = list(roots)
        with self._lock:
            files = self._read()
        entry = files.get(sha)
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("facts"), dict):
            found = self._relink(sha, roots, None)
            if found is None:
                raise FootageError("not_found", "This footage was never added to this project here.")
            return found
        path = Path(entry["path"])
        if not _inside(path, roots):
            # Lane p12-med item 1 (BCK-002): a moved or copied project names its files at the old place. The same
            # bytes inside the project's folders are the same file (relink by identity, Resolve [RV-RL], filmcraft
            # [FC-P:255-262]); only when they are not there is the file outside the project's folders.
            found = self._relink(sha, roots, entry)
            if found is None:
                raise FootageError("outside_roots", "Its folder is no longer one of the project's folders.")
            return found
        try:
            stat = path.stat()
            missing = not path.is_file()
        except OSError:
            missing = True
        if missing:
            found = self._relink(sha, roots, entry)
            if found is None:
                raise FootageError("file_missing", "The file was moved or deleted.")
            return found
        if (stat.st_size, stat.st_mtime_ns) != (entry.get("size"), entry.get("mtime_ns")):
            if stat.st_size != entry.get("size") or self._hash(path) != sha:
                raise FootageError("changed", "The file at that place now holds other bytes.")
            with self._lock:  # touched, same bytes: remember the new mtime, never hash it again
                files = self._read()
                if sha in files:
                    files[sha]["mtime_ns"] = stat.st_mtime_ns
                    self._write(files)
        return path, entry["facts"]

    # -- relink by content inside the approved roots (CUT-079) ----------------------------------
    def _seen_path(self) -> Path:
        return self.path.with_name(self.path.stem + ".seen.json")

    def _read_seen(self) -> dict[str, Any]:
        try:
            value = json.loads(self._seen_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_seen(self, seen: dict[str, Any]) -> None:
        if len(seen) > RELINK_MAX_FILES:  # keep the newest half; a dropped file is hashed again if needed
            seen = dict(list(seen.items())[-RELINK_MAX_FILES // 2:])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".footage-seen-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(seen, ensure_ascii=False))
            os.replace(name, self._seen_path())
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    def _media_files(self, roots: list[str]) -> list[tuple[Path, int, int]]:
        """(path, size, mtime_ns) of every media file under the roots, listed once per index object."""
        key = tuple(sorted(os.path.normcase(os.path.realpath(str(r))) for r in roots))
        if key in self._listing:
            return self._listing[key]
        out: list[tuple[Path, int, int]] = []
        seen_dirs: set[str] = set()
        for root in key:
            for folder, dirs, names in os.walk(root):
                real = os.path.normcase(os.path.realpath(folder))
                if real in seen_dirs:
                    dirs[:] = []
                    continue
                seen_dirs.add(real)
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(names):
                    if Path(name).suffix.lower() not in MEDIA_SUFFIXES:
                        continue
                    file = Path(folder) / name
                    try:
                        stat = file.stat()
                    except OSError:
                        continue
                    out.append((file, stat.st_size, stat.st_mtime_ns))
                    if len(out) >= RELINK_MAX_FILES:
                        break
                if len(out) >= RELINK_MAX_FILES:
                    break
        self._listing[key] = out
        return out

    def _relink(self, sha: str, roots: list[str], stale: dict[str, Any] | None) -> tuple[Path, dict[str, Any]] | None:
        """The file under the roots whose bytes are ``sha``, re-added to the index; None when there is none."""
        if not roots:
            return None
        name = Path(str((stale or {}).get("path") or "")).name.lower()
        size = (stale or {}).get("size")
        candidates = self._media_files(roots)
        order = sorted(range(len(candidates)), key=lambda i: (candidates[i][0].name.lower() != name if name else True,
                                                               candidates[i][1] != size if size is not None else True, i))
        with self._lock:
            seen = self._read_seen()
        dirty = False
        match: Path | None = None
        try:
            for i in order:
                file, file_size, mtime = candidates[i]
                if size is not None and file_size != size:
                    continue  # a known size names the bytes: another size cannot be them
                key = str(file)
                known = seen.get(key)
                if isinstance(known, list) and len(known) == 3 and known[0] == file_size and known[1] == mtime:
                    digest = known[2]
                else:
                    try:
                        digest = self._hash(file)
                    except OSError:
                        continue
                    seen.pop(key, None)
                    seen[key] = [file_size, mtime, digest]
                    dirty = True
                if digest == sha:
                    match = file
                    break
        finally:
            if dirty:
                with self._lock:
                    merged = self._read_seen()
                    merged.update(seen)
                    self._write_seen(merged)
        if match is None or not _inside(match, roots):
            return None
        stat = match.stat()
        facts = (stale or {}).get("facts")
        if not isinstance(facts, dict):
            facts = (self._relink_probe or self._probe)(match)
        with self._lock:
            files = self._read()
            if sha not in files and len(files) >= INDEX_MAX_FILES:
                return match, facts
            files[sha] = {"path": str(match), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "name": match.name[:200], "facts": facts}
            self._write(files)
        return match, facts

    def entries(self) -> dict[str, Any]:
        with self._lock:
            return {"file:" + sha: {"name": e.get("name", ""), "facts": e.get("facts")} for sha, e in self._read().items() if isinstance(e, dict)}
