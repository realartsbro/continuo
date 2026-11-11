"""LaMa inpaint engine client (lane p13-img, IMG-131/133): the pre-fill of Remove.

Pattern of the Suite's owned engines (scene_core/scene_engine.py): locate the record ``engine.json`` written by
``scripts/install-lama-engine.py``, run ONE subprocess per job with JSON in/out (``lama_runner.py``), exit
releases the GPU.  No torch here.  GPU only when the artist's ComfyUI queue is empty, else CPU.

Reference: Krita AI Diffusion Remove = ``FillMode.inpaint`` pre-fill with big-lama before diffusion
(ai_diffusion/backend/workflow.py 913-915, 961-962; resources.py 821).  An absent record or a failing run is a
NAMED refusal (``inpaint_engine_missing`` / ``inpaint_engine_failed``), never a silent fallback to the grey path.

Loaded unregistered by the edit route (no bare relative imports).
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

# One way to start an engine python (lane p14-sam, AUD-062): dashboard/engine_process, loaded by path like its siblings.
_engine_process_spec = importlib.util.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
engine_process = importlib.util.module_from_spec(_engine_process_spec)
_engine_process_spec.loader.exec_module(engine_process)

ENGINE_SCHEMA = "studio.inpaint-engine/1"
REQUEST_SCHEMA = "studio.inpaint-runner-request/1"
RESULT_SCHEMA = "studio.inpaint-runner-result/1"
ENGINE_NAME = "lama-inpaint"
# w39-p2-tester item 4: the one configured ComfyUI address (generation/comfy_endpoint.py, loaded by path).
import importlib.util as _endpoint_util
from pathlib import Path as _EndpointPath
_endpoint_spec = _endpoint_util.spec_from_file_location("film_suite_comfy_endpoint", _EndpointPath(__file__).resolve().parents[1] / "generation" / "comfy_endpoint.py")
_endpoint = _endpoint_util.module_from_spec(_endpoint_spec)
_endpoint_spec.loader.exec_module(_endpoint)
COMFY_QUEUE_URL = _endpoint.DEFAULT_ORIGIN + "/queue"  # the default; the probe reads the configured address
RUN_TIMEOUT_S = 300
MODEL_SHA256 = "344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9"  # big-lama.pt (TorchScript)


class InpaintEngineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def engine_dir() -> Path:
    override = os.environ.get("FILM_SUITE_INPAINT_ENGINE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "hermes" / "film-suite" / "engines" / ENGINE_NAME


def locate() -> dict[str, Any] | None:
    """The engine record, or None when it is absent or points at files that are gone."""
    try:
        record = json.loads((engine_dir() / "engine.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("schema") != ENGINE_SCHEMA:
        return None
    for key in ("python", "runner", "model"):
        if not isinstance(record.get(key), str) or not Path(record[key]).is_file():
            return None
    return record


def comfy_queue_state() -> str:
    try:
        with urllib.request.urlopen(_endpoint.comfy_origin() + "/queue", timeout=1.5) as response:  # noqa: S310 - configured ComfyUI address
            body = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - unknown means "do not assume idle"
        return "unreachable"
    return "busy" if (body.get("queue_running") or body.get("queue_pending")) else "empty"


def choose_device(queue: str) -> str:
    forced = os.environ.get("FILM_SUITE_INPAINT_DEVICE")
    if forced in {"cuda", "cpu"}:
        return forced
    return "cuda" if queue == "empty" else "cpu"


def missing() -> InpaintEngineError:
    return InpaintEngineError("inpaint_engine_missing", "Remove needs the LaMa inpaint engine, which is not installed")


def inpaint(image_png: bytes, mask_png: bytes, *, device: str | None = None) -> dict[str, Any]:
    """Fill the mask (> 127) of ``image_png`` with LaMa. Returns {"image_png", "device", "wall_ms", "engine"}."""
    record = locate()
    if record is None:
        raise missing()
    request = {"schema": REQUEST_SCHEMA, "model": record["model"], "device": device or choose_device(comfy_queue_state()),
               "image_png_b64": base64.b64encode(image_png).decode("ascii"),
               "mask_png_b64": base64.b64encode(mask_png).decode("ascii")}
    try:
        # The engine's own python, isolated: the backend runs with PYTHONPATH set (engine_process).
        proc = subprocess.run(engine_process.engine_command(record["python"], record["runner"]), input=json.dumps(request),
                              capture_output=True, text=True, timeout=RUN_TIMEOUT_S, env=engine_process.engine_env(),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        code, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        code, stdout, stderr = None, "", "timeout"
    except OSError as exc:
        code, stdout, stderr = None, "", type(exc).__name__
    tail = (stderr or "").strip()[-300:]
    if code != 0:
        raise InpaintEngineError("inpaint_engine_failed", f"The LaMa inpaint engine failed (exit {code}): {tail}".rstrip(": "))
    try:
        out = json.loads(stdout)
        if out.get("schema") != RESULT_SCHEMA:
            raise ValueError("schema")
        filled = base64.b64decode(out["image_png_b64"], validate=True)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise InpaintEngineError("inpaint_engine_failed", "The LaMa inpaint engine answered with something unreadable") from None
    return {"image_png": filled, "device": out.get("device"), "wall_ms": out.get("wall_ms"),
            "engine": {"id": ENGINE_NAME, "runner_sha256": record.get("runner_sha256"), "model_sha256": record.get("model_sha256")}}


__all__ = ["ENGINE_SCHEMA", "InpaintEngineError", "MODEL_SHA256", "engine_dir", "inpaint", "locate", "missing"]
