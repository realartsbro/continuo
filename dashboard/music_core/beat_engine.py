"""Beat This! engine launcher (lane fs-beat): locate the Film-Suite-owned runner, pick a
device, run ONE subprocess per job with JSON in/out, and turn its output into tempo and
meter with honest status.

No torch here: torch lives only in the engine's own uv venv (created by
``scripts/install-beat-engine.py``).  The runner process exits after each job, so the GPU
memory it held is released by the OS; ``nvidia-smi`` memory.used is recorded before and
after in the job record.  Decision and numbers: ``out/fs-beat/DECODER.md``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any

# One way to start an engine python (lane p14-sam, AUD-062): dashboard/engine_process, loaded by path like its siblings.
_engine_process_spec = importlib.util.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
engine_process = importlib.util.module_from_spec(_engine_process_spec)
_engine_process_spec.loader.exec_module(engine_process)

import numpy as np

try:  # package import (backend) or flat file load (tests, CLI)
    from . import beat_calibration as cal
except ImportError:  # pragma: no cover - exercised by flat loaders
    import importlib.util as _ilu

    _spec = _ilu.spec_from_file_location("film_suite_beat_calibration", Path(__file__).with_name("beat_calibration.py"))
    cal = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(cal)

ENGINE_ID = "beat_this"
DECODER = "minimal"
CHECKPOINT = "final0"
CHECKPOINT_SHA256 = "8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331"
ENGINE_SCHEMA = "studio.beat-engine/1"
RESULT_SCHEMA = "studio.beat-runner-result/1"
COMFY_QUEUE_URL = "http://127.0.0.1:8188/queue"
RUN_TIMEOUT_S = 600
# One job at a time: a second analysis waits for the first runner to exit.
_LOCK_NAME = "beat-engine.lock"


def engine_dir() -> Path:
    override = os.environ.get("FILM_SUITE_BEAT_ENGINE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "hermes" / "film-suite" / "engines" / "beat-this"


def locate() -> dict[str, Any] | None:
    """The installed engine record, or None when it is absent or incomplete."""
    record_path = engine_dir() / "engine.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != ENGINE_SCHEMA:
        return None
    for key in ("python", "runner", "checkpoint"):
        if not isinstance(record.get(key), str):
            return None
    if not Path(record["python"]).is_file() or not Path(record["runner"]).is_file():
        return None
    return record


def comfy_queue_state() -> str:
    """'empty', 'busy' or 'unreachable' (ComfyUI shares the GPU with the artist)."""
    try:
        with urllib.request.urlopen(COMFY_QUEUE_URL, timeout=1.5) as response:  # noqa: S310 - fixed loopback URL
            body = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - any failure means "do not assume it is idle"
        return "unreachable"
    running = body.get("queue_running") or []
    pending = body.get("queue_pending") or []
    return "busy" if running or pending else "empty"


def gpu_memory_used_mb() -> int | None:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=engine_process.child_env())
        return int(out.stdout.strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


def choose_device(queue: str) -> str:
    """GPU only when the artist's ComfyUI queue is empty; CPU Beat This! otherwise.
    Measured: CPU 0.70-0.89 s per minute of audio (8 threads), identical beats."""
    forced = os.environ.get("FILM_SUITE_BEAT_DEVICE")
    if forced in {"cuda", "cpu"}:
        return forced
    return "cuda" if queue == "empty" else "cpu"


class _Lock:
    def __init__(self, path: Path, timeout: float = RUN_TIMEOUT_S):
        self.path, self.timeout, self.fd = path, timeout, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.timeout:
                        self.path.unlink(missing_ok=True)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError("beat engine is busy")
                time.sleep(0.1)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


def run(audio_path: str | Path, *, device: str | None = None) -> dict[str, Any]:
    """Run the engine once on ``audio_path``.  Never raises: returns {'ok': False, ...}."""
    record = locate()
    if record is None:
        return {"ok": False, "reason": "not_installed"}
    queue = comfy_queue_state() if device is None else "not_checked"
    requested = device or choose_device(queue)
    request = {"schema": "studio.beat-runner-request/1", "audio_path": str(audio_path), "device": requested,
               "checkpoint": record["checkpoint"], "checkpoint_sha256": record.get("checkpoint_sha256", CHECKPOINT_SHA256)}
    job: dict[str, Any] = {"requested_device": requested, "queue": queue}
    with _Lock(engine_dir() / _LOCK_NAME):
        job["nvidia_smi_before_mb"] = gpu_memory_used_mb()
        started = time.perf_counter()
        try:
            # The engine's own venv, isolated (engine_process): else every request falls back to librosa (p13b-aud).
            proc = subprocess.run(engine_process.engine_command(record["python"], record["runner"]), input=json.dumps(request),
                                  capture_output=True, text=True, encoding="utf-8", timeout=RUN_TIMEOUT_S, env=engine_process.engine_env(),
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            code, stdout, stderr = None, "", "timeout"
        except OSError as exc:
            code, stdout, stderr = None, "", f"{type(exc).__name__}"
        job["wall_s"] = round(time.perf_counter() - started, 3)
        job["exit_code"] = code
        job["process_exited"] = code is not None
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
    job["peak_vram_mb"] = result.get("peak_vram_mb")
    job["timing"] = result.get("timing")
    versions = dict(record.get("versions") or {})
    versions.update(result.get("versions") or {})
    return {"ok": True, "beats": [float(x) for x in result["beats"]], "downbeats": [float(x) for x in result["downbeats"]],
            "beat_prob": result.get("beat_prob") or [], "mid_prob": result.get("mid_prob") or [],
            "down_prob": result.get("down_prob") or [], "versions": versions, "job": job}


# ---------------------------------------------------------------- derivation
def local_tempo_curve(beats: np.ndarray, half: int = cal.LOCAL_TEMPO_HALF_WINDOW_BEATS) -> tuple[list[dict[str, float]], float | None]:
    """60 / running median IBI (2*half beats), with its IQR in % of the median (ends trimmed)."""
    ibi = np.diff(beats)
    if len(ibi) < 2:
        return [], None
    loc = 60.0 / np.array([np.median(ibi[max(0, i - half):i + half]) for i in range(len(ibi))])
    mids = (beats[:-1] + beats[1:]) / 2
    curve = [{"time": round(float(t), 3), "bpm": round(float(b), 2)} for t, b in zip(mids, loc)]
    core = loc[half:-half] if len(loc) > 2 * half + 4 else (loc if len(loc) >= 8 else None)
    if core is None:
        return curve, None
    iqr = float(np.subtract(*np.percentile(core, [75, 25])) / np.median(core) * 100.0)
    return curve, iqr


def tempo_status(beats: np.ndarray, beat_prob: np.ndarray, mid_prob: np.ndarray, iqr: float | None,
                 *, variable_iqr: float = cal.TEMPO_VARIABLE_IQR_PCT) -> tuple[str, str]:
    if len(beats) < 8:
        return "none", "too_few_beats"
    if iqr is not None and iqr > variable_iqr:
        return "variable", "tempo_changes"
    if len(beat_prob) and len(mid_prob):
        if float(np.mean(mid_prob)) / max(float(np.mean(beat_prob)), 1e-6) >= cal.TEMPO_DOUBLE_RIVAL_RATIO:
            return "ambiguous", "double_time_rival"
        if float(np.percentile(beat_prob, 10)) < cal.TEMPO_WEAK_BEAT_P10:
            return "ambiguous", "weak_beats"
    return "fixed", "steady"


def meter_fold(down_prob: np.ndarray) -> tuple[int | None, float, dict[int, float]]:
    """Downbeat probability at each beat folded over 3 and 4 (best phase each)."""
    if len(down_prob) < 12:
        return None, 0.0, {}
    scores: dict[int, float] = {}
    for m in cal.METER_FOLD_CHOICES:
        best = -1.0
        for phase in range(m):
            on = down_prob[phase::m]
            off = np.delete(down_prob, np.arange(phase, len(down_prob), m))
            best = max(best, float(on.mean() - off.mean()))
        scores[m] = best
    value = max(scores, key=lambda k: (scores[k], k == 4))
    margin = abs(scores[3] - scores[4])
    return value, margin, scores


def bar_agreement(beats: np.ndarray, downbeats: np.ndarray, meter: int) -> float:
    if len(downbeats) < 2 or not len(beats):
        return 0.0
    idx = [int(np.argmin(np.abs(beats - d))) for d in downbeats]
    bars = [b - a for a, b in zip(idx, idx[1:]) if b - a > 1]
    return float(np.mean([n == meter for n in bars])) if bars else 0.0


def derive(out: dict[str, Any], *, duration: float) -> dict[str, Any]:
    beats = np.asarray(out.get("beats") or [], dtype=float)
    downs = np.asarray(out.get("downbeats") or [], dtype=float)
    bp = np.asarray(out.get("beat_prob") or [], dtype=float)
    mp = np.asarray(out.get("mid_prob") or [], dtype=float)
    dp = np.asarray(out.get("down_prob") or [], dtype=float)
    curve, iqr = local_tempo_curve(beats)
    bpm = float(60.0 / np.median(np.diff(beats))) if len(beats) > 2 else None
    status, reason = tempo_status(beats, bp, mp, iqr)
    value, margin, scores = meter_fold(dp) if len(dp) == len(beats) else (None, 0.0, {})
    agree = bar_agreement(beats, downs, value) if value else 0.0
    fixed = value is not None and margin >= cal.METER_FOLD_MARGIN_MIN and agree >= cal.METER_BAR_AGREEMENT_MIN
    if value is None:
        meter = {"value": None, "status": "none", "candidate": None, "reason": "no_downbeats"}
    elif fixed:
        meter = {"value": value, "status": "fixed", "candidate": value, "reason": "clear_downbeats"}
    else:
        meter = {"value": None, "status": "ambiguous", "candidate": value,
                 "reason": "weak_downbeats" if margin < cal.METER_FOLD_MARGIN_MIN else "bars_disagree"}
    meter.update({"fold_margin": round(margin, 4), "bar_agreement": round(agree, 4),
                  "fold_scores": {str(k): round(v, 4) for k, v in scores.items()},
                  "confidence": cal.METER_CONFIDENCE.get(meter["status"], 0.0)})
    return {
        "tempo": {"bpm": None if bpm is None else round(bpm, 2), "status": status, "reason": reason,
                  "confidence": cal.TEMPO_CONFIDENCE.get(status, 0.0),
                  "local_iqr_pct": None if iqr is None else round(iqr, 3), "curve": curve},
        "meter": meter,
        "beats": [round(float(t), 4) for t in beats if 0 <= t <= duration],
        "downbeats": [round(float(t), 4) for t in downs if 0 <= t <= duration],
    }
