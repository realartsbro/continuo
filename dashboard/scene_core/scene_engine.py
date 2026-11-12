"""Scene-analysis engine launcher (lane w6-scene): locate the Film-Suite-owned runner, pick a device, run ONE
subprocess per job with JSON in/out, and turn its output into studio.scene-map/1 on one clock (frames + seconds).

No torch here: torch lives only in the engine's own uv venv (``scripts/install-scene-engine.py``). The runner
exits after each job, so the GPU memory it held is released by the OS; ``nvidia-smi`` memory.used is recorded
before and after. GPU only when the artist's ComfyUI queue is empty, else CPU. Engine absent -> the answer
says "Not installed" (``engine.state == "not_installed"``), never a silent fallback.
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

ENGINE_SCHEMA = "studio.scene-engine/1"
REQUEST_SCHEMA = "studio.scene-runner-request/1"
RESULT_SCHEMA = "studio.scene-runner-result/1"
MAP_SCHEMA = "studio.scene-map/1"
# w39-p2-tester item 4: the one configured ComfyUI address (generation/comfy_endpoint.py, loaded by path).
import importlib.util as _endpoint_util
from pathlib import Path as _EndpointPath
_endpoint_spec = _endpoint_util.spec_from_file_location("film_suite_comfy_endpoint", _EndpointPath(__file__).resolve().parents[1] / "generation" / "comfy_endpoint.py")
_endpoint = _endpoint_util.module_from_spec(_endpoint_spec)
_endpoint_spec.loader.exec_module(_endpoint)
COMFY_QUEUE_URL = _endpoint.DEFAULT_ORIGIN + "/queue"  # the default; the probe reads the configured address


def comfy_root(recorded: str | None = None) -> Path:
    """The Suite's one ComfyUI root (generation/comfy_root.py): FILM_SUITE_COMFY_ROOT, else the root recorded at install."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("film_suite_comfy_root",
                                                  Path(__file__).resolve().parents[1] / "generation" / "comfy_root.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.comfy_root(recorded)
RUN_TIMEOUT_S = 1800
PARTS = ("cuts", "speakers", "sync", "motion")
_LOCK_NAME = "scene-engine.lock"


def engine_dir() -> Path:
    override = os.environ.get("FILM_SUITE_SCENE_ENGINE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "hermes" / "film-suite" / "engines" / "scene-analysis"


def locate() -> dict[str, Any] | None:
    try:
        record = json.loads((engine_dir() / "engine.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != ENGINE_SCHEMA:
        return None
    for key in ("python", "runner"):
        if not isinstance(record.get(key), str) or not Path(record[key]).is_file():
            return None
    return record


def comfy_queue_state() -> str:
    try:
        with _endpoint._opener.open(_endpoint.comfy_origin() + "/queue", timeout=1.5) as response:  # noqa: S310 - configured ComfyUI address, no redirect
            body = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - unknown means "do not assume idle"
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
    forced = os.environ.get("FILM_SUITE_SCENE_DEVICE")
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
                    raise TimeoutError("scene engine is busy") from None
                time.sleep(0.1)

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


def run(video_path: str | Path, *, parts: tuple[str, ...] = PARTS, device: str | None = None) -> dict[str, Any]:
    """Run the engine once. Never raises: {'ok': False, 'reason': ...} on any failure."""
    record = locate()
    if record is None:
        return {"ok": False, "reason": "not_installed"}
    queue = comfy_queue_state() if device is None else "not_checked"
    requested = device or choose_device(queue)
    request = {"schema": REQUEST_SCHEMA, "video_path": str(video_path), "device": requested, "parts": list(parts),
               "weights": record.get("weights") or {}, "weights_sha256": record.get("weights_sha256") or {},
               "comfy_root": str(comfy_root(record.get("comfy_root")))}
    job: dict[str, Any] = {"requested_device": requested, "queue": queue}
    with _Lock(engine_dir() / _LOCK_NAME):
        job["nvidia_smi_before_mb"] = gpu_memory_used_mb()
        started = time.perf_counter()
        try:
            # The engine's own venv, isolated (engine_process): it inherited the backend's PYTHONPATH before.
            proc = subprocess.run(engine_process.engine_command(record["python"], record["runner"]), input=json.dumps(request),
                                  capture_output=True, text=True, encoding="utf-8", timeout=RUN_TIMEOUT_S, env=engine_process.engine_env(),
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
        return {"ok": False, "reason": "runner_failed", "job": job, "detail": (result or {}).get("reason")}
    job.update({"device": result.get("device", requested), "peak_vram_mb": result.get("peak_vram_mb"), "timing": result.get("timing")})
    return {"ok": True, "parts": result.get("parts") or {}, "errors": result.get("errors") or {},
            "versions": {**(record.get("versions") or {}), **(result.get("versions") or {})}, "job": job}


# ------------------------------------------------------------------ map
def _at(frame: float, fps: float) -> dict[str, Any]:
    return {"frame": int(round(frame)), "s": round(float(frame) / fps, 4)}


def sync_text(frames: int, fps: float) -> str:
    """The Take tab's short form: '+2 f · 83 ms', '0 f · 0 ms', '−1 f · 42 ms' (no '.0')."""
    ms = round(abs(frames) * 1000.0 / fps)
    sign = "+" if frames > 0 else ("\u2212" if frames < 0 else "")
    return f"{sign}{abs(int(frames))} f \u00b7 {ms} ms"


def build_map(out: dict[str, Any], *, source: dict[str, Any], fps: float | None = None, n_frames: int | None = None) -> dict[str, Any]:
    """studio.scene-map/1 from one runner answer. Times are frames AND seconds on the take's own clock."""
    parts = out.get("parts") or {}
    errors = out.get("errors") or {}
    fps = float(fps or (parts.get("cuts") or {}).get("fps") or (parts.get("motion") or {}).get("fps") or 24.0)
    if n_frames is None:
        n_frames = (parts.get("cuts") or {}).get("n_frames") or len((parts.get("motion") or {}).get("energy") or [])
    cuts = None
    if "cuts" in parts:
        cuts = []
        for e in parts["cuts"].get("events") or []:
            kind = "hard" if e["kind"] == "hard" else "dissolve" if e["kind"] == "dissolve" else "transition"
            item = {"kind": kind, **{f"at_{k}": v for k, v in _at(e["start_frame"], fps).items()}}
            if kind != "hard":
                item["range"] = {"start": _at(e["start_frame"], fps), "end": _at(e["end_frame"], fps)}
            cuts.append(item)
    speakers = None
    if "speakers" in parts:
        sp = parts["speakers"]
        speakers = {"count": int(sp.get("count") or 0), "provisional": True,
                    "turns": [{"speaker": t["speaker"], "start": _at(t["start_s"] * fps, fps), "end": _at(t["end_s"] * fps, fps)}
                              for t in sp.get("turns") or []]}
    sync = None
    if "sync" in parts:
        s = parts["sync"]
        if not s.get("available"):
            sync = {"available": False, "reason": s.get("reason")}
        else:
            frames = int(s["offset_frames"])
            drift = s.get("drift") or None
            sync = {
                "available": True, "offset_frames": frames, "offset_ms": round(frames * 1000.0 / fps), "text": sync_text(frames, fps),
                "offset_only": bool(s.get("offset_only")), "offset_only_reason": s.get("offset_only_reason"),
                "material": s.get("material"), "foreign": s.get("foreign"),
                "drift": None if s.get("offset_only") or not drift or not drift.get("measurable") else {
                    "frames": drift["frames"], "slope_frames_per_s": drift["slope_frames_per_s"], "found": bool(drift.get("found"))},
                "frozen": [] if s.get("offset_only") and s.get("offset_only_reason") != "drift_not_measurable" else
                [{"start": _at(f["start_frame"], fps), "end": _at(f["end_frame"], fps)} for f in s.get("frozen") or []],
                "face_rate": s.get("face_rate"),
            }
    motion = None
    if "motion" in parts:
        motion = {"per_frame": parts["motion"].get("energy") or [], "measure": "mean absolute luma difference to the previous frame (0-1)"}
    return {"schema": MAP_SCHEMA, "source": source, "clock": {"fps": round(fps, 3), "frames": int(n_frames or 0),
            "seconds": round((n_frames or 0) / fps, 4)}, "cuts": cuts, "speakers": speakers, "sync": sync, "motion": motion,
            "errors": errors, "engine": {"id": "scene-analysis", "state": "ok", "versions": out.get("versions"), "job": out.get("job")}}


def not_installed_map(source: dict[str, Any]) -> dict[str, Any]:
    return {"schema": MAP_SCHEMA, "source": source, "clock": None, "cuts": None, "speakers": None, "sync": None, "motion": None,
            "errors": {}, "engine": {"id": "scene-analysis", "state": "not_installed", "label": "Not installed"}}
