"""In-between frames by RIFE through the local ComfyUI (lane w6-tempo, sampling ``ai``).

Export-time job: the clip's source frames (already fitted to the sequence format, raw yuv420p) are
written as PNGs, one prompt runs ``VHS_LoadImagesPath -> FrameInterpolationModelLoader
(models/frame_interpolation/rife_v4.26.safetensors) -> FrameInterpolate (multiplier) -> PreviewImage``
on the local ComfyUI, and the frames come back through ``/view``. Measured on this install: the
ComfyUI-VFI node ``RIFEInterpolation`` only loads its own ``flownet.pkl`` (and would try a download
when it is missing), so the RIFE v4.26 file the owner named is run by ComfyUI's own frame
interpolation nodes. ``FrameInterpolate`` puts source frame ``i`` at output ``i * mult`` and the
in-betweens ``t = j / mult`` between them (``(n - 1) * mult + 1`` frames); the caller uses the source
frames themselves at whole positions and only takes the in-betweens from here. Nothing is fetched.

ComfyUI is shared with the artist: ``GET /queue`` before the submit, the prompt waits its turn behind
theirs, nothing is ever cancelled, interrupted or cleared; ``/free`` only when the queue is empty.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from .errors import ExportError
from .ffmpeg_run import child_env  # w39-env: one minimal environment for every child process

# w22-dlv (BCK-215): the one ComfyUI origin of the plugin (generation/comfyui_client.py is fixed to it too); no
# environment override, so the README names exactly what runs.
COMFY_URL = "http://127.0.0.1:8188"
NODE = "FrameInterpolate"
MODEL_LOADER = "FrameInterpolationModelLoader"
LOADER = "VHS_LoadImagesPath"
MODEL = "rife_v4.26.safetensors"  # models/frame_interpolation (local)
TIMEOUT_S = 3600


def _get(path: str, timeout: float = 30) -> Any:
    with urllib.request.urlopen(COMFY_URL + path, timeout=timeout) as response:  # noqa: S310 (local ComfyUI only)
        body = response.read()
    return json.loads(body) if body[:1] in (b"{", b"[") else body


def _post(path: str, payload: dict[str, Any], timeout: float = 30) -> Any:
    request = urllib.request.Request(COMFY_URL + path, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        body = response.read()
    return json.loads(body) if body[:1] in (b"{", b"[") else None


def available() -> tuple[bool, str]:
    """(True, "") when the local ComfyUI answers and has the RIFE and loader nodes; else the reason."""
    try:
        info = _get("/object_info/" + NODE, timeout=10)
        loader = _get("/object_info/" + LOADER, timeout=10)
        models = _get("/object_info/" + MODEL_LOADER, timeout=10)
    except OSError as exc:
        return False, f"ComfyUI does not answer at {COMFY_URL} ({exc.__class__.__name__})"
    if NODE not in (info or {}) or MODEL_LOADER not in (models or {}):
        return False, f"ComfyUI has no {NODE} / {MODEL_LOADER} node"
    if LOADER not in (loader or {}):
        return False, f"ComfyUI has no {LOADER} node (VideoHelperSuite)"
    options = models[MODEL_LOADER]["input"]["required"]["model_name"][1].get("options") or []
    if MODEL not in options:
        return False, f"models/frame_interpolation has no {MODEL}"
    return True, ""


def workflow(directory: Path, frames: int, mult: int, prefix: str) -> dict[str, Any]:
    return {
        "1": {"class_type": LOADER, "inputs": {"directory": str(directory), "image_load_cap": frames, "skip_first_images": 0,
                                               "select_every_nth": 1}},
        "4": {"class_type": MODEL_LOADER, "inputs": {"model_name": MODEL}},
        "2": {"class_type": NODE, "inputs": {"interp_model": ["4", 0], "images": ["1", 0], "multiplier": mult}},
        "3": {"class_type": "PreviewImage", "inputs": {"images": ["2", 0]}},
    }


def queue_size() -> int:
    queue = _get("/queue", timeout=10)
    return len(queue.get("queue_running") or []) + len(queue.get("queue_pending") or [])


def interpolate(ffmpeg: Path, src: Path, size: tuple[int, int], frames: int, mult: int, out: Path, work: Path, *,
                should_cancel: Callable[[], bool] | None = None, sleep: Callable[[float], None] = time.sleep) -> int:
    """Write ``(frames - 1) * mult + 1`` raw yuv420p frames to ``out`` (source frame i at i * mult)."""
    ok, why = available()
    if not ok:
        raise ExportError("ai_frames_unavailable", f"AI frames need the local ComfyUI: {why}. Optical flow needs no ComfyUI.")
    width, height = size
    pngs = work / "rife-in"
    pngs.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(ffmpeg), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}",
                    "-i", str(src), "-start_number", "0", str(pngs / "f%06d.png")], check=True, timeout=1800,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=child_env())
    _get("/queue", timeout=10)  # read the queue first; the prompt waits behind the artist's
    prompt = workflow(pngs, frames, mult, "film-suite-rife")
    answer = _post("/prompt", {"prompt": prompt, "client_id": "film-suite-" + uuid.uuid4().hex[:12]})
    prompt_id = (answer or {}).get("prompt_id")
    if not prompt_id:
        raise ExportError("ai_frames_failed", f"ComfyUI refused the RIFE prompt: {json.dumps(answer)[:300]}")
    deadline = time.monotonic() + TIMEOUT_S
    entry = None
    while time.monotonic() < deadline:
        history = _get(f"/history/{prompt_id}", timeout=30)
        entry = (history or {}).get(prompt_id)
        if entry and (entry.get("status") or {}).get("completed") is not None and (entry.get("outputs") or entry["status"].get("status_str") == "error"):
            break
        if should_cancel is not None and should_cancel():
            # Never cancel on the shared ComfyUI: the prompt finishes on its own, its frames are dropped.
            raise ExportError("cancelled", "The export was cancelled.")
        sleep(1.0)
    if not entry or not entry.get("outputs"):
        status = (entry or {}).get("status") or {}
        raise ExportError("ai_frames_failed", f"RIFE did not finish ({status.get('status_str', 'timeout')}).")
    images = entry["outputs"]["3"]["images"]
    want = (frames - 1) * mult + 1
    rife_out = work / "rife-out"
    rife_out.mkdir(exist_ok=True)
    for index, image in enumerate(images[:want]):
        query = urllib.parse.urlencode({"filename": image["filename"], "subfolder": image.get("subfolder", ""), "type": image.get("type", "temp")})
        (rife_out / f"r{index:06d}.png").write_bytes(_get("/view?" + query, timeout=120))
    got = min(len(images), want)
    subprocess.run([str(ffmpeg), "-v", "error", "-y", "-start_number", "0", "-i", str(rife_out / "r%06d.png"),
                    "-frames:v", str(got), "-vf", f"scale={width}:{height},format=yuv420p", "-f", "rawvideo", str(out)],
                   check=True, timeout=1800, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=child_env())
    if got != want:
        raise ExportError("ai_frames_failed", f"RIFE answered {got} frames, expected {want}.")
    return got


def free_when_idle() -> bool:
    """POST /free (unload models, free memory) only when nothing is running or waiting."""
    try:
        if queue_size() == 0:
            _post("/free", {"unload_models": True, "free_memory": True})
            return True
    except OSError:
        pass
    return False
