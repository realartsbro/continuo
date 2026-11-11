"""Song map (lane w6-song): one clock, many measured tracks, schema ``studio.song-map/1``.

No torch here.  The analysis runs in the Film-Suite-owned song engine
(``scripts/install-song-engine.py`` -> ``engines/song-analysis/engine.json``, schema
``studio.song-engine/1``), one subprocess per job (``song_map_runner.py``), JSON on
stdin/stdout; the process exits after the job, which releases its GPU memory.  GPU only
when the shared ComfyUI ``GET /queue`` is empty, else CPU; device, wall time and nvidia-smi
memory before/after are recorded per job.

Engine choices (eval/bench/RESULTS.md, binding): sections SongFormer (B1), drums ADTOF
Frame_RNN (B2), melody RMVPE on the vocal stem (B3), words Qwen3-ForcedAligner-0.6B with the
known lyrics (B4); novelty Foote on the beat grid, loudness and silence/risers librosa.
Never a silent fallback: an absent engine names every track "Not installed", a failing one
"Failed", each with its reason.

The clock is the existing beat grid (``music_analysis`` beats/downbeats of the same file
content): every time is in seconds and carries ``bar`` = 1-based bar position (bar 3, beat 2
of 4 = 3.25).  A map is cached per (audio sha256, engine.json sha256); the lyrics hash is
stored with it so new lyrics recompute the words.
"""
from __future__ import annotations

import bisect
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

# One way to start an engine python (lane p14-sam, AUD-062): dashboard/engine_process, loaded by path like its siblings.
_engine_process_spec = importlib.util.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
engine_process = importlib.util.module_from_spec(_engine_process_spec)
_engine_process_spec.loader.exec_module(engine_process)

# Lane w18-amap (AUD-135): the local recognizer (voice/asr.py, faster-whisper large-v3 on the CPU) for songs without
# pasted lyrics; loaded by path and only when a song needs it.
_ASR = None


def _asr_module() -> Any:
    global _ASR
    if _ASR is None:
        spec = importlib.util.spec_from_file_location("film_suite_voice_asr_songmap", Path(__file__).resolve().parents[1] / "voice" / "asr.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ASR = module
    return _ASR

SCHEMA = "studio.song-map/1"
ENGINE_SCHEMA = "studio.song-engine/1"
REQUEST_SCHEMA = "studio.song-runner-request/1"
RESULT_SCHEMA = "studio.song-runner-result/1"
COMFY_QUEUE_URL = "http://127.0.0.1:8188/queue"
RUN_TIMEOUT_S = 1800
TASKS = ("drums", "melody", "words", "sections", "novelty", "loudness", "events")

# Track rows in display order: id, name (the row label), runner task, how the data is read.
TRACKS = (
    ("sections", "Sections", "sections"),
    ("loudness_mix", "Level", "loudness"),
    ("loudness_instrumental", "Level (instr.)", "loudness"),
    ("novelty", "Novelty", "novelty"),
    ("kick", "Kick", "drums"),
    ("snare", "Snare", "drums"),
    ("hat", "Hat", "drums"),
    ("crash", "Crash", "drums"),
    ("silence", "Silence", "events"),
    ("risers", "Risers", "events"),
    ("melody", "Melody", "melody"),
    ("words", "Words", "words"),
)
STATUS_LABELS = {"ready": "Ready", "not_installed": "Not installed", "failed": "Failed", "no_lyrics": "No lyrics", "no_vocals": "No vocals"}
REASON_TEXT = {
    "not_installed": "The song engine is not installed",
    "runner_failed": "The song engine stopped",
    "no_lyrics": "The project has no lyrics for this song",
    "no_vocal_stem": "No vocal stem (separation unavailable)",
    "no_instrumental_stem": "No instrumental stem (separation unavailable)",
    "no_beat_grid": "No beat grid",
    "task_failed": "The analysis failed",
    "no_vocals": "Nothing is sung in the vocal stem",
    "transcriber_unavailable": "The local recognizer could not run",
    "no_stored_vocal_stem": "Made before word recognition · no stored vocal stem",
}


def engine_dir() -> Path:
    override = os.environ.get("FILM_SUITE_SONG_ENGINE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "hermes" / "film-suite" / "engines" / "song-analysis"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def locate() -> tuple[dict[str, Any] | None, str | None]:
    """(engine record, sha256 of engine.json) or (None, None) when absent or incomplete."""
    path = engine_dir() / "engine.json"
    try:
        raw = path.read_bytes()
        record = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError):
        return None, None
    if not isinstance(record, dict) or record.get("schema") != ENGINE_SCHEMA:
        return None, None
    if not all(isinstance(record.get(k), str) for k in ("python", "runner")):
        return None, None
    if not Path(record["python"]).is_file() or not Path(record["runner"]).is_file():
        return None, None
    return record, hashlib.sha256(raw).hexdigest()


def comfy_queue_state() -> str:
    try:
        with urllib.request.urlopen(COMFY_QUEUE_URL, timeout=1.5) as response:  # noqa: S310 - fixed loopback URL
            body = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - unreachable means "do not assume it is idle"
        return "unreachable"
    return "busy" if (body.get("queue_running") or body.get("queue_pending")) else "empty"


def gpu_memory_used_mb() -> int | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True,
                             timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=engine_process.child_env())
        return int(out.stdout.strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


def choose_device(queue: str) -> str:
    forced = os.environ.get("FILM_SUITE_SONG_DEVICE")
    if forced in {"cuda", "cpu"}:
        return forced
    return "cuda" if queue == "empty" else "cpu"


def _weights(record: dict[str, Any]) -> dict[str, str]:
    w = record.get("weights") or {}

    def f(key, rel):
        return (((w.get(key) or {}).get("files") or {}).get(rel) or {}).get("path")
    return {"adtof": f("adtof", "adtof_frame_rnn_pytorch_weights.pth"), "rmvpe": f("rmvpe", "rmvpe.onnx"),
            "qwen3_aligner_dir": (w.get("qwen3_aligner") or {}).get("dir"), "songformer_dir": (w.get("songformer") or {}).get("dir")}


def run_engine(record: dict[str, Any], *, audio_path: str, beats: list[float], stems: dict[str, str | None],
               lyrics: str | None, tasks: tuple[str, ...] = TASKS, device: str | None = None) -> dict[str, Any]:
    """One runner subprocess.  Never raises: {'ok': False, 'reason', 'job'} on failure."""
    forced = os.environ.get("FILM_SUITE_SONG_DEVICE") if os.environ.get("FILM_SUITE_SONG_DEVICE") in {"cuda", "cpu"} else None
    queue = comfy_queue_state() if device is None and forced is None else "not_checked"
    requested = device or forced or choose_device(queue)
    request = {"schema": REQUEST_SCHEMA, "audio_path": str(audio_path), "device": requested, "tasks": list(tasks),
               "beats": [float(b) for b in beats], "stems": {k: v for k, v in stems.items() if v}, "lyrics": lyrics or "",
               "weights": _weights(record)}
    job: dict[str, Any] = {"requested_device": requested, "queue": queue, "nvidia_smi_before_mb": gpu_memory_used_mb()}
    started = time.perf_counter()
    # The engine's own venv, isolated (engine_process; live 01.11. it imported the backend's NumPy and failed); -B for
    # the PYTHONDONTWRITEBYTECODE that -I ignores.
    env = engine_process.engine_env({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    try:
        proc = subprocess.run(engine_process.engine_command(record["python"], record["runner"], flags=("-B",)), input=json.dumps(request),
                              capture_output=True, text=True, encoding="utf-8", timeout=RUN_TIMEOUT_S, env=env,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        code, stdout, stderr = None, "", "timeout"
    except OSError as exc:
        code, stdout, stderr = None, "", type(exc).__name__
    job["wall_s"] = round(time.perf_counter() - started, 3)
    job["exit_code"] = code
    job["nvidia_smi_after_mb"] = gpu_memory_used_mb()
    result = None
    for line in reversed((stdout or "").strip().splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and candidate.get("schema") == RESULT_SCHEMA:
            result = candidate
            break
    if code != 0 or result is None or result.get("ok") is not True:
        job["stderr_tail"] = (stderr or "")[-400:]
        return {"ok": False, "reason": "runner_failed", "job": job}
    job["device"] = result.get("device", requested)
    job["tasks"] = {t: {"wall_s": r.get("wall_s"), "peak_vram_mb": r.get("peak_vram_mb"), "device": r.get("device")}
                    for t, r in (result.get("tasks") or {}).items()}
    return {"ok": True, "tasks": result.get("tasks") or {}, "versions": result.get("versions") or {}, "job": job}


# ---------------------------------------------------------------- clock
class Clock:
    """Seconds -> 1-based bar position on the beat grid (beats between downbeats split evenly)."""

    def __init__(self, beats: list[float], downbeats: list[float]):
        self.beats = sorted(float(b) for b in beats)
        self.downbeats = sorted(float(d) for d in downbeats)

    def bar(self, t: float) -> float | None:
        d = self.downbeats
        if len(d) < 2:
            return None
        if t < d[0]:
            span = d[1] - d[0]
            return round(1 + (t - d[0]) / span, 4)
        i = bisect.bisect_right(d, t) - 1
        if i >= len(d) - 1:
            span = d[-1] - d[-2]
            return round(len(d) + (t - d[-1]) / span, 4)
        a, b = d[i], d[i + 1]
        inside = [x for x in self.beats if a - 1e-6 <= x < b - 1e-6]
        n = max(1, len(inside))
        k = bisect.bisect_right(inside, t + 1e-9) - 1
        if 0 <= k < n:
            lo = inside[k]
            hi = inside[k + 1] if k + 1 < n else b
            frac = (k + (t - lo) / max(1e-9, hi - lo)) / n
        else:
            frac = (t - a) / max(1e-9, b - a)
        return round(i + 1 + frac, 4)

    def public(self) -> dict[str, Any]:
        return {"unit": "seconds", "beats": [round(b, 4) for b in self.beats], "downbeats": [round(d, 4) for d in self.downbeats],
                "bars": [{"n": i + 1, "t": round(d, 4)} for i, d in enumerate(self.downbeats)],
                "bar_rule": "bar = 1-based bar number + fraction of the bar (beats between downbeats split evenly)"}


# ---------------------------------------------------------------- assembly
def _track(tid: str, name: str, engine_id: str | None, version: str | None, status: str, *, reason: str | None = None,
           confidence: float | None = None, data: Any = None, kind: str = "events") -> dict[str, Any]:
    return {"id": tid, "name": name, "kind": kind, "status": status, "status_label": STATUS_LABELS[status],
            "reason": reason, "reason_text": REASON_TEXT.get(reason or "", reason) if reason else None,
            "engine": {"id": engine_id, "version": version}, "confidence": confidence, "data": data}


def _kind(tid: str) -> str:
    if tid in {"sections", "silence", "risers"}:
        return "spans"
    if tid in {"loudness_mix", "loudness_instrumental", "novelty", "melody"}:
        return "curve"
    return "events"


def assemble(*, track_sha256: str, duration: float, clock: Clock, engine_sha256: str | None, record: dict[str, Any] | None,
             run: dict[str, Any] | None, lyrics_sha256: str | None, stems_state: dict[str, Any]) -> dict[str, Any]:
    engines = (record or {}).get("engines") or {}
    versions = dict((record or {}).get("versions") or {})
    versions.update((run or {}).get("versions") or {})

    def version_for(task):
        return {"drums": versions.get("adtof_pytorch"), "melody": versions.get("onnxruntime"), "words": versions.get("transformers"),
                "sections": versions.get("transformers"), "novelty": versions.get("librosa"), "loudness": versions.get("librosa"),
                "events": versions.get("librosa")}.get(task)
    tracks = []
    for tid, name, task in TRACKS:
        eid = engines.get(task) if record else None
        ver = version_for(task) if record else None
        kind = _kind(tid)
        if record is None:
            tracks.append(_track(tid, name, None, None, "not_installed", reason="not_installed", kind=kind))
            continue
        if not run or not run.get("ok"):
            tracks.append(_track(tid, name, eid, ver, "failed", reason="runner_failed", kind=kind))
            continue
        res = (run.get("tasks") or {}).get(task) or {"ok": False, "reason": "task_failed"}
        if not res.get("ok"):
            reason = res.get("reason") or "task_failed"
            if tid == "words" and reason in {"no_lyrics", "no_vocals", "transcriber_unavailable"}:
                # No pasted lyrics and no recognizer that could run: still "No lyrics", with the recognizer's reason.
                status = "no_lyrics" if reason == "transcriber_unavailable" else reason
                row = _track(tid, name, (res.get("engine") or {}).get("id") or eid, (res.get("engine") or {}).get("version") or ver,
                             status, reason=reason, kind=kind)
                if reason == "no_vocals":
                    row["source"], row["lines"] = "transcribed", []
                tracks.append(row)
            else:
                if reason == "no_vocal_stem" and stems_state.get("error"):
                    reason = "no_vocal_stem"
                tracks.append(_track(tid, name, eid, ver, "failed", reason=reason, kind=kind))
            continue
        conf = res.get("confidence")
        data: Any
        if task == "drums":
            hits = (res.get("classes") or {}).get(tid) or []
            data = [{"t": h["t"], "bar": clock.bar(h["t"]), "value": h.get("confidence")} for h in hits]
            conf = round(sum(h.get("confidence") or 0 for h in hits) / len(hits), 3) if hits else 0.0
        elif tid == "sections":
            data = [{"start": s["start"], "end": s["end"], "bar": clock.bar(s["start"]), "label": s["label"],
                     "value": s.get("confidence")} for s in res.get("sections") or []]
        elif tid.startswith("loudness"):
            series = res.get("mix") if tid == "loudness_mix" else res.get("instrumental")
            if series is None:
                tracks.append(_track(tid, name, eid, ver, "failed", reason="no_instrumental_stem", kind=kind))
                continue
            hop = float(res.get("hop_s") or 0.1)
            data = {"hop_s": hop, "unit": "dBFS", "values": series}
        elif tid == "novelty":
            data = {"points": [{"t": p["t"], "bar": clock.bar(p["t"]), "value": p["value"]} for p in res.get("curve") or []],
                    "peaks": [{"t": p["t"], "bar": clock.bar(p["t"]), "value": p["value"]} for p in res.get("peaks") or []],
                    "threshold": res.get("threshold")}
        elif tid in {"silence", "risers"}:
            spans = res.get(tid) or []
            data = [{"start": s["start"], "end": s["end"], "bar": clock.bar(s["start"]), "value": s.get("confidence")} for s in spans]
            if tid == "silence":
                conf = 1.0
        elif tid == "melody":
            data = {"hop_s": res.get("hop_s", 0.01), "unit": "Hz", "f0_hz": res.get("f0_hz") or [],
                    "notes": [{**n, "bar": clock.bar(n["start"])} for n in res.get("notes") or []],
                    "peaks": [{"t": p["start"], "end": p["end"], "bar": clock.bar(p["start"]), "midi": p["midi"], "hz": p["hz"],
                               "value": p.get("confidence")} for p in res.get("peaks") or []]}
        else:  # words
            data = [{"t": w["start"], "end": w["end"], "bar": clock.bar(w["start"]), "word": w["word"], "value": w.get("confidence")}
                    for w in res.get("words") or []]
            # w18-amap (AUD-135): where the words came from and their lines (lyric lines or recognizer segments).
            row = _track(tid, name, (res.get("engine") or {}).get("id") or eid, (res.get("engine") or {}).get("version") or ver,
                         "ready", confidence=conf, data=data, kind=kind)
            row["source"] = res.get("source") or "lyrics"
            if isinstance(res.get("language"), str):  # w24-rest AUD-066: the recognizer's language (the lyrics draft names it)
                row["language"] = res["language"]
            row["lines"] = [{"t": ln["start"], "end": ln["end"], "bar": clock.bar(ln["start"]), "text": ln["text"]} for ln in res.get("lines") or []]
            tracks.append(row)
            continue
        tracks.append(_track(tid, name, eid, ver, "ready", confidence=conf, data=data, kind=kind))
    return {"schema": SCHEMA, "track_sha256": track_sha256, "engine_sha256": engine_sha256, "lyrics_sha256": lyrics_sha256,
            "duration": round(float(duration), 4), "clock": clock.public(), "tracks": tracks,
            "stems": stems_state, "job": (run or {}).get("job"), "made_at": time.time()}


# ---------------------------------------------------------------- cache + build
def cache_path(cache_dir: Path, track_sha256: str, engine_sha256: str) -> Path:
    return Path(cache_dir) / f"{track_sha256}-{engine_sha256[:16]}.json"


def read_cached(cache_dir: Path, track_sha256: str) -> dict[str, Any] | None:
    """The map of this file for the engine that would run now (None when absent or made by another engine)."""
    _record, engine_sha = locate()
    if engine_sha is None:
        return None
    try:
        data = json.loads(cache_path(cache_dir, track_sha256, engine_sha).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(data, dict) and data.get("schema") == SCHEMA and data.get("track_sha256") == track_sha256 \
        and data.get("engine_sha256") == engine_sha
    if not ok:
        return None
    # Owner decision 3: what the page and Hermes read carries a region for every sung stretch (the map's own clock).
    clk = data.get("clock") or {}
    return with_sung_lines(data, Clock(clk.get("beats") or [], clk.get("downbeats") or []))


def lyrics_sha(lyrics: str | None) -> str | None:
    return hashlib.sha256(lyrics.encode("utf-8")).hexdigest() if lyrics and lyrics.strip() else None


def build(*, cache_dir: Path, audio_path: Path, track_sha256: str, duration: float, beats: list[float], downbeats: list[float],
          lyrics: str | None, stems: Callable[[], dict[str, Any]], progress: Callable[[float, str], None] = lambda f, s: None,
          device: str | None = None) -> dict[str, Any]:
    """Cached map of (audio, engine) with the same lyrics, else one engine run.  ``stems`` is only
    called when the engine runs (it may queue the Suite's audio-stems-v1 workflow on ComfyUI)."""
    clock = Clock(beats, downbeats)
    record, engine_sha = locate()
    want_lyrics = lyrics_sha(lyrics)
    if record is None:
        return assemble(track_sha256=track_sha256, duration=duration, clock=clock, engine_sha256=None, record=None, run=None,
                        lyrics_sha256=want_lyrics, stems_state={})
    cached = read_cached(cache_dir, track_sha256)
    # The line rule is part of the words' cache key: a map whose lines came from an older rule gets its words redone
    # (the recognizer on the stored vocal stem; sections, drums and melody stay).
    tid_now = None if want_lyrics else transcriber_id()
    want_transcriber = f"{tid_now}/{LINES_RULE}" if tid_now else None
    if cached is not None and cached.get("lyrics_sha256") == want_lyrics:
        if want_lyrics or cached.get("words_transcriber") == want_transcriber:
            cached["cached"] = True
            return with_sung_lines(cached, clock)
        # w18-amap: a map made before the recognizer (or by another recognizer version): only the words are redone,
        # on the stored vocal stem; sections, drums and melody stay as measured (no second engine run).
        progress(0.1, "stems")
        stems_state = stems()
        return migrate_words(cache_dir=cache_dir, cached=cached, clock=clock, vocals=stems_state.get("vocals"), progress=progress)
    if cached is not None and want_lyrics:
        # w24-rest AUD-066: lyrics written (or a transcription draft corrected) after the map: only the words are aligned
        # again to the new text (the engine's aligner, words task alone); sections, drums and melody stay as measured.
        progress(0.1, "stems")
        stems_state = stems()
        paths = {"vocals": stems_state.get("vocals"), "instrumental": stems_state.get("instrumental")}
        progress(0.5, "words")
        run = run_engine(record, audio_path=str(audio_path), beats=beats, stems=paths, lyrics=lyrics, tasks=("words",), device=device)
        if run.get("ok") and ((run.get("tasks") or {}).get("words") or {}).get("ok"):
            run["tasks"]["words"] = {**run["tasks"]["words"], "source": "lyrics",
                                     "lines": lyric_lines(lyrics, run["tasks"]["words"].get("words") or [])}
        fresh = assemble(track_sha256=track_sha256, duration=duration, clock=clock, engine_sha256=engine_sha, record=record,
                         run={"ok": bool(run.get("ok")), "tasks": run.get("tasks") or {}, "versions": run.get("versions") or {}},
                         lyrics_sha256=want_lyrics, stems_state={})
        words = next(t for t in fresh["tracks"] if t["id"] == "words")
        out = {k: v for k, v in cached.items() if k != "cached"}
        out["tracks"] = [words if t.get("id") == "words" else t for t in out.get("tracks") or []]
        out["lyrics_sha256"], out["words_transcriber"] = want_lyrics, None
        if run.get("ok"):
            _write_cache(cache_dir, track_sha256, engine_sha, out)
        out["cached"] = False
        return with_sung_lines(out, clock)
    progress(0.1, "stems")
    stems_state = stems()
    progress(0.4, "analysing")
    paths = {"vocals": stems_state.get("vocals"), "instrumental": stems_state.get("instrumental")}
    run = run_engine(record, audio_path=str(audio_path), beats=beats, stems=paths, lyrics=lyrics, device=device)
    if run.get("ok") and not want_lyrics and (((run.get("tasks") or {}).get("words") or {}).get("reason") == "no_lyrics"):
        progress(0.85, "words")
        run["tasks"]["words"] = words_from_vocals(paths.get("vocals"))
    elif run.get("ok") and want_lyrics and ((run.get("tasks") or {}).get("words") or {}).get("ok"):
        run["tasks"]["words"] = {**run["tasks"]["words"], "source": "lyrics",
                                 "lines": lyric_lines(lyrics, run["tasks"]["words"].get("words") or [])}
    out = assemble(track_sha256=track_sha256, duration=duration, clock=clock, engine_sha256=engine_sha, record=record, run=run,
                   lyrics_sha256=want_lyrics, stems_state={k: v for k, v in stems_state.items() if k in {"split", "error", "source"}})
    out["words_transcriber"] = want_transcriber
    if run.get("ok"):
        _write_cache(cache_dir, track_sha256, engine_sha, out)
    out["cached"] = False
    return with_sung_lines(out, clock)


def words_key(lyrics_sha256: str | None) -> str | None:
    """The words' cache key a map needs now: <transcriber>/<LINES_RULE> without lyrics (None: lyrics or no recognizer)."""
    tid = None if lyrics_sha256 else transcriber_id()
    return f"{tid}/{LINES_RULE}" if tid else None


def words_outdated(cached: dict[str, Any] | None) -> str | None:
    """w18-amapc (AUD-139): the words key a stored map without lyrics must be migrated to, or None when its words are
    current, it was made with lyrics, or a migration to this key already failed (stored, never retried on every read)."""
    if not isinstance(cached, dict) or cached.get("lyrics_sha256"):
        return None
    want = words_key(None)
    if want is None or cached.get("words_transcriber") == want:
        return None
    if (cached.get("words_migration") or {}).get("key") == want:
        return None
    return want


def migrate_words(*, cache_dir: Path, cached: dict[str, Any], clock: "Clock", vocals: str | None,
                  progress: Callable[[float, str], None] = lambda f, s: None) -> dict[str, Any]:
    """Words-only cache migration (w18-amap's build() branch, w18-amapc's read): the words of a stored map are redone
    from the vocal stem with the current recognizer and line rule; every other track stays as measured; stored.  A
    failure is stored under ``words_migration`` {key, error} and raised (the job reports it; the key is not retried)."""
    track_sha256, engine_sha = cached["track_sha256"], cached["engine_sha256"]
    record, _sha = locate()
    want = words_key(cached.get("lyrics_sha256"))
    progress(0.5, "words")
    try:
        res = words_from_vocals(vocals)
    except Exception as exc:
        failed = {k: v for k, v in cached.items() if k != "cached"}
        failed["words_migration"] = {"key": want, "error": type(exc).__name__, "at": time.time()}
        _write_cache(cache_dir, track_sha256, engine_sha, failed)
        raise
    fresh = assemble(track_sha256=track_sha256, duration=float(cached.get("duration") or 0.0), clock=clock, engine_sha256=engine_sha,
                     record=record, run={"ok": True, "tasks": {"words": res}, "versions": {}}, lyrics_sha256=cached.get("lyrics_sha256"),
                     stems_state={})
    words = next(t for t in fresh["tracks"] if t["id"] == "words")
    cached["tracks"] = [words if t.get("id") == "words" else t for t in cached.get("tracks") or []]
    cached["words_transcriber"] = want
    cached.pop("cached", None)
    cached.pop("words_migration", None)
    _write_cache(cache_dir, track_sha256, engine_sha, cached)
    cached["cached"] = False
    return with_sung_lines(cached, clock)


def _write_cache(cache_dir: Path, track_sha256: str, engine_sha: str, out: dict[str, Any]) -> None:
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    target = cache_path(cache_dir, track_sha256, engine_sha)
    tmp = target.with_suffix(".part")
    tmp.write_text(json.dumps({k: v for k, v in out.items() if k != "cached"}), encoding="utf-8")
    tmp.replace(target)


# ---------------------------------------------------------------- words without lyrics (lane w18-amap, AUD-135)
_TRANSCRIBER_ID: list[str | None] = []


def transcriber_id() -> str | None:
    """faster-whisper-<version>/large-v3/cpu-int8 of the local recognizer (None when none can run); part of the cache key."""
    if not _TRANSCRIBER_ID:
        asr = _asr_module()
        found = asr.song_transcriber_id(asr.whisper_version())
        if found is None:
            return None          # asked again next time: a recognizer installed later is found without a restart
        _TRANSCRIBER_ID.append(found)
    return _TRANSCRIBER_ID[0]


def transcribe_vocals(path: str) -> dict[str, Any] | None:
    """The recognizer's answer for one vocal stem (voice/asr.transcribe_song); None when it could not run."""
    return _asr_module().transcribe_song(path)


def words_from_vocals(vocals: str | None) -> dict[str, Any]:
    """The words task result for a song without lyrics: words + lines (source transcribed), no_vocals, or a failure."""
    if not vocals:
        return {"ok": False, "reason": "no_vocal_stem"}
    tid = transcriber_id()
    engine = {"id": f"faster-whisper-{_asr_module().SONG_MODEL}", "version": (tid or "").split("/")[0].replace("faster-whisper-", "") or None}
    out = transcribe_vocals(vocals) if tid else None
    if not isinstance(out, dict):
        return {"ok": False, "reason": "transcriber_unavailable", "engine": engine}
    segments = [s for s in out.get("segments") or [] if s.get("words")]
    if float(out.get("speech_s") or 0.0) < _asr_module().SONG_MIN_SPEECH_S or not segments:
        return {"ok": False, "reason": "no_vocals", "engine": engine}
    words = [{"word": w["w"], "start": float(w["start"]), "end": float(w["end"]), "confidence": w.get("p")} for s in segments for w in s["words"]]
    lines = lyric_lines_from_segments(segments)
    # w18-amapc (AUD-138): every line starts where its singing starts in the vocal stem (LINES_RULE lines-4; w18-amapd
    # D4 lines-5: the previous line's last word may end 20 ms before it).
    snap_line_starts(lines, words, vocal_onsets(vocals))
    # Owner decision 3 (every sung line has a region): where the vocal stem is sung but the recognizer heard no word, a
    # line without text marks it (the lane draws it as an empty region, its tooltip says so); never an invented word.
    lines = sorted(lines + unrecognized_sung(sung_spans(vocals), lines), key=lambda ln: ln["start"])
    probs = [w["confidence"] for w in words if isinstance(w["confidence"], (int, float))]
    return {"ok": True, "source": "transcribed", "words": words, "lines": lines, "engine": engine, "language": out.get("language"),
            "confidence": round(sum(probs) / len(probs), 3) if probs else None,
            "confidence_basis": "mean word probability of the recognizer"}


LINES_RULE = "lines-5"  # w18-amapb lyric-line rule + w18-amapc line starts on the vocal onsets + w18-amapd D4 previous word yields (part of the words cache key)
LINE_GAP_S = 0.6        # a sung pause this long ends a line where a sentence or clause ends (owner decision w18-amapb)
LINE_MAX_S = 8.0        # a longer line splits at its longest pause between two clauses
_SENTENCE_END = (".", "?", "!")
_CLAUSE_END = (",", ";", ":") + _SENTENCE_END
_FIRST_PERSON = {"I", "I'm", "I'll", "I've", "I'd"}


def _word(w: dict[str, Any]) -> str:
    return str(w.get("w", "")).strip()


def _starts_sentence(w: dict[str, Any]) -> bool:
    """The recognizer writes a sentence's first word capitalised; "I" is capitalised anywhere, so it says nothing."""
    s = _word(w).lstrip("\"'(-")
    return bool(s) and s[0].isupper() and s.rstrip(",.;:!?") not in _FIRST_PERSON


def lyric_lines_from_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lyric lines from recognized words (owner decision w18-amapb 1, as a lyric sheet breaks a song):
    a line ends after sentence-ending punctuation (. ? !) or at the end of a recognizer segment (the recognizer's own
    sentence), and at a sung pause >= 0.6 s where a clause ends (the word before ends in , ; :) or a sentence starts
    (the next word is capitalised); a pause inside a clause is a breath, never a line end, so a line never starts with
    the tail of the previous sentence. A line longer than 8 s splits at its longest pause between two clauses; a
    sentence without such a place stays one line (never split in the middle of a sentence)."""
    lines: list[dict[str, Any]] = []
    for seg in segments or []:
        ws = sorted((w for w in seg.get("words") or [] if _word(w)), key=lambda w: float(w["start"]))
        if not ws:
            continue

        def gap(i: int) -> float:
            return float(ws[i]["start"]) - float(ws[i - 1]["end"])

        def clause_end(i: int) -> bool:          # a line may start at word i
            return _word(ws[i - 1]).endswith(_CLAUSE_END) or _starts_sentence(ws[i])
        starts = [0] + [i for i in range(1, len(ws))
                        if _word(ws[i - 1]).endswith(_SENTENCE_END) or (gap(i) >= LINE_GAP_S and clause_end(i))]
        runs: list[tuple[int, int]] = []

        def split(a: int, b: int) -> None:
            inner = [i for i in range(a + 1, b) if clause_end(i)]
            if float(ws[b - 1]["end"]) - float(ws[a]["start"]) <= LINE_MAX_S or not inner:
                runs.append((a, b))
                return
            mid = (a + b) / 2
            j = max(inner, key=lambda i: (round(gap(i), 2), -abs(i - mid)))
            split(a, j)
            split(j, b)
        for a, b in zip(starts, starts[1:] + [len(ws)]):
            split(a, b)
        lines += [{"text": " ".join(_word(w) for w in ws[a:b]), "start": float(ws[a]["start"]), "end": float(ws[b - 1]["end"])}
                  for a, b in runs]
    return lines


# ---------------------------------------------------------------- line starts on the singing (lane w18-amapc, AUD-138)
ONSET_RISE_DB = 28.0      # an onset: the stem's 20 ms RMS rises above (max - 28 dB) ...
ONSET_QUIET_DB = 36.0     # ... after >= 0.12 s below (max - 36 dB)
ONSET_QUIET_S = 0.12
ONSET_HOP_S = 0.02
SNAP_BEFORE_S = 1.6       # a line start moves to the latest onset in [start - 1.6 s, start + 0.3 s]
SNAP_AFTER_S = 0.3


def vocal_onsets(vocals: str | None) -> list[float]:
    """Where singing starts in the vocal stem (owner's measurement, cache/scratch/intb/vocon2.py): the 20 ms RMS rises
    above max - 28 dB after >= 0.12 s below max - 36 dB.  [] when the stem cannot be read."""
    try:
        import numpy as np
        import soundfile as sf
        x, sr = sf.read(str(vocals), dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001 - no stem, no onsets: the line starts stay as recognized
        return []
    x = x.mean(axis=1)
    hop = max(1, int(round(sr * ONSET_HOP_S)))
    n = len(x) // hop
    if n == 0:
        return []
    db = 20 * np.log10(np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1)) + 1e-9)
    top = float(db.max())
    need = int(round(ONSET_QUIET_S / ONSET_HOP_S))
    out: list[float] = []
    below = 0
    for i, v in enumerate(db):
        if v < top - ONSET_QUIET_DB:
            below += 1
            continue
        if v > top - ONSET_RISE_DB:
            if below >= need:
                out.append(round(i * ONSET_HOP_S, 2))
            below = 0
    return out


def snap_line_starts(lines: list[dict[str, Any]], words: list[dict[str, Any]], onsets: list[float]) -> list[dict[str, Any]]:
    """AUD-138 (Logic's Flex / Melodyne onset snap): the recognizer's word starts come up to ~1.5 s late against the vocal
    stem.  Each LINE's first word start moves to the latest vocal onset in [start - 1.6 s, start + 0.3 s] that lies after
    the previous line's last word end (and before the first word's own end); the line starts there too.  Words keep
    their order; no word end moves.  ``lines`` [{text, start, end}] and ``words`` [{word, start, end}] change in place.

    w18-amapd D4 (owner decision, LINES_RULE lines-5): the recognizer's words touch, so an onset before the previous
    line's last word end is allowed when that word can end at onset - SNAP_GAP_S (20 ms) without ending before its own
    start + SNAP_MIN_WORD_S (80 ms); that word (and its line, when it ended there) then ends at onset - 20 ms.  A start
    never moves later than recognised and never to or before the previous line's start (no reorder)."""
    ons = sorted(float(o) for o in onsets or [])
    order = sorted(range(len(words)), key=lambda i: float(words[i]["start"]))
    prev_line: dict[str, Any] | None = None
    prev_end = float("-inf")
    for ln in sorted(lines, key=lambda x: float(x["start"])):
        start = float(ln["start"])
        first = next((i for i in order if abs(float(words[i]["start"]) - start) < 1e-6), None)
        if first is not None:
            k = order.index(first)
            last = order[k - 1] if k > 0 else None
            # The earliest start the previous last word allows: its own start + 80 ms + the 20 ms gap.
            yield_floor = float(words[last]["start"]) + SNAP_MIN_WORD_S + SNAP_GAP_S if last is not None else float("-inf")
            older = max((float(x["end"]) for x in lines if x is not ln and x is not prev_line and float(x["start"]) < start), default=float("-inf"))
            floor = max(yield_floor, older, float(prev_line["start"]) if prev_line is not None else float("-inf"))
            cand = [o for o in ons if start - SNAP_BEFORE_S <= o <= min(start, start + SNAP_AFTER_S) and o > floor and o < float(words[first]["end"])]
            if cand:
                new = cand[-1]
                if last is not None and float(words[last]["end"]) > new - SNAP_GAP_S:
                    cut = round(new - SNAP_GAP_S, 3)
                    if prev_line is not None and abs(float(prev_line["end"]) - float(words[last]["end"])) < 1e-6:
                        prev_line["end"] = cut
                    words[last]["end"] = cut
                words[first]["start"] = new
                ln["start"] = new
        prev_end = max(prev_end, float(ln["end"]))
        prev_line = ln
    return lines


SNAP_GAP_S = 0.02         # w18-amapd D4: the shortened previous word ends 20 ms before the new start
SNAP_MIN_WORD_S = 0.08    # ... and never before its own start + 80 ms


SUNG_FLOOR_DB = 30.0     # the vocal stem counts as sung within 30 dB of its loudest 50 ms (bleed and reverb sit lower)
SUNG_MIN_S = 1.0         # a sung stretch the recognizer missed becomes a line from 1 s on


def sung_spans(vocals: str | None, hop_s: float = 0.05) -> list[tuple[float, float]]:
    """Stretches of the vocal stem within SUNG_FLOOR_DB of its peak, pauses < 0.6 s bridged; [] when it cannot be read."""
    try:
        import numpy as np
        import soundfile as sf
        x, sr = sf.read(str(vocals), dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001 - no stem reader: no extra lines, the recognized ones stand
        return []
    x = x.mean(axis=1)
    hop = max(1, int(sr * hop_s))
    n = len(x) // hop
    if n == 0:
        return []
    db = 20 * np.log10(np.sqrt(np.mean(x[:n * hop].reshape(n, hop) ** 2, axis=1)) + 1e-9)
    on = db >= db.max() - SUNG_FLOOR_DB
    spans: list[list[float]] = []
    i = 0
    while i < n:
        if on[i]:
            j = i
            while j < n and on[j]:
                j += 1
            a, b = i * hop_s, j * hop_s
            if spans and a - spans[-1][1] < LINE_GAP_S:
                spans[-1][1] = b
            else:
                spans.append([a, b])
            i = j
        else:
            i += 1
    return [(round(a, 2), round(b, 2)) for a, b in spans]


def unrecognized_sung(spans: list[tuple[float, float]], lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The parts of the sung stretches no recognized line covers (each >= SUNG_MIN_S), as lines with empty text."""
    covered = sorted((float(ln["start"]), float(ln["end"])) for ln in lines)
    out: list[dict[str, Any]] = []
    for a, b in spans:
        cur = a
        for s, e in covered:
            if e <= cur or s >= b:
                continue
            if s - cur >= SUNG_MIN_S:
                out.append({"text": "", "start": round(cur, 2), "end": round(s, 2)})
            cur = max(cur, e)
        if b - cur >= SUNG_MIN_S:
            out.append({"text": "", "start": round(cur, 2), "end": round(b, 2)})
    return out


def voiced_spans(melody: Any) -> list[tuple[float, float]]:
    """Stretches the map's melody track hears a sung pitch in (f0 > 0 on the vocal stem), pauses < 0.6 s bridged."""
    if not isinstance(melody, dict) or not isinstance(melody.get("f0_hz"), list) or not melody.get("hop_s"):
        return []
    hop, spans = float(melody["hop_s"]), []
    for i, f in enumerate(melody["f0_hz"]):
        if not f:
            continue
        a = i * hop
        if spans and a - spans[-1][1] < LINE_GAP_S:
            spans[-1][1] = a + hop
        else:
            spans.append([a, a + hop])
    return [(round(a, 2), round(b, 2)) for a, b in spans]


def with_sung_lines(out: dict[str, Any], clock: "Clock") -> dict[str, Any]:
    """Owner decision 3 (every sung line has a region): a transcribed words track gets a line without text for every
    stretch >= 1 s the melody track hears sung and no line covers (the recognizer heard no word there)."""
    tracks = out.get("tracks") or []
    words = next((t for t in tracks if t.get("id") == "words"), None)
    melody = next((t for t in tracks if t.get("id") == "melody"), None)
    if not words or words.get("status") != "ready" or words.get("source") != "transcribed" or not melody:
        return out
    have = [{"start": ln["t"], "end": ln["end"]} for ln in words.get("lines") or []]
    extra = unrecognized_sung(voiced_spans(melody.get("data")), have)
    if extra:
        words["lines"] = sorted((words.get("lines") or []) + [{"t": e["start"], "end": e["end"], "bar": clock.bar(e["start"]), "text": ""} for e in extra],
                                key=lambda ln: ln["t"])
    return out


def sung_lines(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One recognizer segment's words as lyric lines (lyric_lines_from_segments)."""
    return lyric_lines_from_segments([{"words": words}])


def lyric_lines(lyrics: str, words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pasted lyrics: each non-empty line spans its aligned words, the lines taken from the text itself.

    Lane w20-song (AUD-143; as LRC editors and forced aligners such as ctc-forced-aligner / WhisperX map tokens back
    onto the text): the aligner tokenises by its own rules (a hyphen word as two words, "It's" as "It" + "'s", a lone
    dash dropped), so a line is never cut by word count. Every aligned word is found by its letters in the lyric text,
    in order from the previous word on (letters and digits only, case-folded); it belongs to the line its first letter
    stands on. Lane w24-rest (AUD-143 residual): a word matches only where a lyric word starts or where the previous
    match stopped inside a word (the rest of a split word), and at most two lines ahead; a token the text does not hold
    there (an "uh", a stray letter) stays on the current line and moves nothing, so one extra token can no longer pull
    every later line onto the wrong times."""
    letters: list[str] = []
    line_of: list[int] = []
    starts: set[int] = set()
    texts: list[str] = []
    for raw in (lyrics or "").splitlines():
        if not raw.split():
            continue
        prev = False
        for ch in raw:
            if ch.isalnum():
                if not prev:
                    starts.add(len(letters))
                letters.append(ch.casefold())
                line_of.append(len(texts))
                prev = True
            elif ch in "'’" and prev:
                continue  # an apostrophe inside a word (It's, rock 'n') keeps the word whole
            else:
                prev = False
        texts.append(raw.strip())
    stream = "".join(letters)
    spans: dict[int, list[float]] = {}
    pos = 0
    for w in words or []:
        key = "".join(ch.casefold() for ch in str(w.get("word") or "") if ch.isalnum())
        here = line_of[min(max(pos - 1, 0), len(line_of) - 1)] if line_of else None  # the line the last match is on
        at = -1
        if key:
            if pos < len(stream) and pos not in starts and stream.startswith(key, pos):
                at = pos  # the rest of a word the aligner split (Butter|flies, It|'s, Twenty|-one)
            else:
                cand = stream.find(key, pos)
                while cand >= 0 and cand not in starts:
                    cand = stream.find(key, cand + 1)
                if cand >= 0 and line_of[cand] <= here + 2:
                    at = cand
        if at >= 0:
            line, pos = line_of[at], at + len(key)
        elif here is not None:
            line = here
        else:
            continue
        span = spans.setdefault(line, [float(w["start"]), float(w["end"])])
        span[0], span[1] = min(span[0], float(w["start"])), max(span[1], float(w["end"]))
    return [{"text": texts[k], "start": spans[k][0], "end": spans[k][1]} for k in sorted(spans)]


def track_value(track: dict[str, Any], t: float) -> Any:
    """The value a row shows on hover at time t (the same rule the UI uses)."""
    data = track.get("data")
    if track.get("status") != "ready" or data is None:
        return track.get("status_label")
    if isinstance(data, dict) and "values" in data:
        i = int(t / data["hop_s"])
        return data["values"][i] if 0 <= i < len(data["values"]) else None
    if isinstance(data, dict) and "points" in data:
        pts = data["points"]
        best = max((p for p in pts if p["t"] <= t), key=lambda p: p["t"], default=None)
        return None if best is None else best["value"]
    if isinstance(data, dict) and "f0_hz" in data:
        i = int(round(t / data["hop_s"]))
        return data["f0_hz"][i] if 0 <= i < len(data["f0_hz"]) else None
    return None
