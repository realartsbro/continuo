"""Camera transfer, measured (lane w9-chain item 4): studio.camera-flow/1.

The measure is the fs-cam camflow, ported with its definitions: features are
tracked between frames i and i+STEP (STEP = 2; Shi-Tomasi corners, pyramidal Lucas-Kanade with a forward-backward
check < 1 px), a similarity transform about the frame centre is fitted with RANSAC (reprojection 2 px) and
accumulated over the clip on a 640 px wide grey proxy:

  zoom     cumulative scale (push-in / dolly-in > 1, pull-out < 1)
  tx, ty   cumulative translation of the picture CONTENT in frame widths / frame heights
           (+tx = content moves right = the camera pans/trucks left; +ty = content moves down)
  rot_deg  cumulative in-plane rotation in degrees
  inliers  mean RANSAC inlier ratio (low = no coherent rigid motion)

It runs in the scene engine's venv (OpenCV 4.12 + numpy are there; the python comes from the engine's engine.json),
started as ``python camera_flow.py <clip>`` and answering one JSON line. OpenCV is imported only inside ``measure``,
so the backend loads this file for ``transfer``/``note``/``run`` without OpenCV. Nothing is installed or downloaded.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

# One way to start an engine python (lane p14-sam, AUD-062): dashboard/engine_process, loaded by path like its siblings.
_engine_process_spec = importlib.util.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
engine_process = importlib.util.module_from_spec(_engine_process_spec)
_engine_process_spec.loader.exec_module(engine_process)

SCHEMA = "studio.camera-flow/1"
STEP = 2
PROXY_WIDTH = 640
AXES = ("zoom", "tx", "ty", "rot_deg")
# An axis counts as moved by the guide above these amounts (zoom as |ln zoom|: 0.02 = 2 % scale).
MOVED = {"zoom": 0.02, "tx": 0.02, "ty": 0.02, "rot_deg": 1.0}
RUN_TIMEOUT_S = 600


def _frames(src: str, width: int = PROXY_WIDTH) -> list[Any]:
    import cv2  # the engine venv's OpenCV

    capture = cv2.VideoCapture(src)
    out = []
    try:
        while True:
            ok, image = capture.read()
            if not ok:
                break
            height = int(image.shape[0] * width / image.shape[1])
            out.append(cv2.cvtColor(cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY))
    finally:
        capture.release()
    return out


def measure(src: str) -> dict[str, Any]:
    """zoom, tx, ty, rot_deg, inliers and the frame count of one clip (camflow's similarity part)."""
    import cv2
    import numpy as np

    frames = _frames(src)
    if len(frames) <= STEP:
        raise ValueError("the clip has too few frames to measure")
    height, width = frames[0].shape
    centre = np.array([width / 2, height / 2])
    zoom, tx, ty, rot = 1.0, 0.0, 0.0, 0.0
    inliers: list[float] = []
    for i in range(0, len(frames) - STEP, STEP):
        a, b = frames[i], frames[i + STEP]
        p0 = cv2.goodFeaturesToTrack(a, 600, 0.01, 7)
        if p0 is None or len(p0) < 12:
            continue
        p1, status, _ = cv2.calcOpticalFlowPyrLK(a, b, p0, None, winSize=(21, 21), maxLevel=3)
        back, _, _ = cv2.calcOpticalFlowPyrLK(b, a, p1, None, winSize=(21, 21), maxLevel=3)
        good = (status.ravel() == 1) & (np.linalg.norm((back - p0).reshape(-1, 2), axis=1) < 1.0)
        q0, q1 = p0.reshape(-1, 2)[good], p1.reshape(-1, 2)[good]
        if len(q0) < 12:
            continue
        matrix, mask = cv2.estimateAffinePartial2D(q0 - centre, q1 - centre, method=cv2.RANSAC, ransacReprojThreshold=2.0)
        if matrix is None:
            continue
        zoom *= float(np.hypot(matrix[0, 0], matrix[1, 0]))
        rot += float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0])))
        tx += float(matrix[0, 2]) / width
        ty += float(matrix[1, 2]) / height
        inliers.append(float(mask.mean()))
    return {"zoom": round(zoom, 4), "tx": round(tx, 4), "ty": round(ty, 4), "rot_deg": round(rot, 3),
            "inliers": round(float(np.mean(inliers)) if inliers else 0.0, 3), "frames": len(frames)}


def public(part: Mapping[str, Any]) -> dict[str, Any]:
    return {key: part[key] for key in (*AXES, "inliers")}


def _amount(axis: str, value: float) -> float:
    return math.log(value) if axis == "zoom" else value


def transfer(guide: Mapping[str, Any], take: Mapping[str, Any]) -> dict[str, Any] | None:
    """take / guide per axis the guide moved on (zoom compared as ln zoom), else None for that axis; None when the
    guide moved on no axis."""
    out: dict[str, Any] = {}
    for axis in AXES:
        g = _amount(axis, float(guide[axis]))
        out[axis] = round(_amount(axis, float(take[axis])) / g, 3) if abs(g) > MOVED[axis] else None
    return out if any(v is not None for v in out.values()) else None


_WORDS = {"zoom": ("push-in", "pull-out"), "tx": ("sideways move", "sideways move"), "ty": ("vertical move", "vertical move"),
          "rot_deg": ("roll", "roll")}


def note(guide: Mapping[str, Any], take: Mapping[str, Any], moved: Mapping[str, Any] | None) -> str:
    if moved is None:
        return "The guide holds still, so there is no camera move to transfer."
    parts = []
    for axis in AXES:
        ratio = moved.get(axis)
        if ratio is None:
            continue
        word = _WORDS[axis][0 if _amount(axis, float(guide[axis])) > 0 else 1]
        if ratio < 0:
            parts.append(f"the take moves against the guide's {word}")
        else:
            parts.append(f"the take keeps {round(ratio * 100):d} % of the guide's {word}")
    return (parts[0][0].upper() + "; ".join(parts)[1:] + ".") if parts else "The guide holds still."


def result(guide: Mapping[str, Any], take: Mapping[str, Any]) -> dict[str, Any]:
    moved = transfer(guide, take)
    return {"schema": SCHEMA, "guide": public(guide), "take": public(take), "transfer": moved, "note": note(guide, take, moved)}


# --------------------------------------------------------------------------- backend side: run in the engine venv


class CameraFlowUnavailable(RuntimeError):
    """The engine's python is missing: a plain refusal, never a crash."""


def engine_python(engine_dir: Path) -> Path:
    try:
        record = json.loads((Path(engine_dir) / "engine.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CameraFlowUnavailable("The scene engine is not installed, so the camera move cannot be measured.") from None
    python = record.get("python") if isinstance(record, dict) else None
    if not isinstance(python, str) or not Path(python).is_file():
        raise CameraFlowUnavailable("The scene engine's Python is missing, so the camera move cannot be measured.")
    return Path(python)


def run(clip: str | os.PathLike[str], engine_dir: Path, *, timeout: float = RUN_TIMEOUT_S) -> dict[str, Any]:
    """Measure one clip with the engine's python (isolated mode: no user site, no environment paths)."""
    python = engine_python(engine_dir)
    env = engine_process.engine_env({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "OPENCV_LOG_LEVEL": "ERROR"})
    done = subprocess.run(engine_process.engine_command(python, Path(__file__).resolve(), clip), capture_output=True, text=True,
                          timeout=timeout, check=False, env=env)
    line = (done.stdout or "").strip().splitlines()[-1:] or [""]
    try:
        answer = json.loads(line[0])
    except ValueError:
        raise CameraFlowUnavailable("The camera measure did not answer.") from None
    if not answer.get("ok"):
        raise ValueError(answer.get("error") or "the clip could not be measured")
    return answer["camera"]


if __name__ == "__main__":
    try:
        print(json.dumps({"ok": True, "camera": measure(sys.argv[1])}), flush=True)
    except Exception as exc:  # noqa: BLE001 - one JSON line either way
        print(json.dumps({"ok": False, "error": str(exc)[:300]}), flush=True)
