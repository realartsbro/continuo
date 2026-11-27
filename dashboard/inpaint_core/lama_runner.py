"""LaMa inpaint runner (lane p13-img): ONE job per process, JSON on stdin, JSON on stdout, exit releases the GPU.

Copied by ``scripts/install-lama-engine.py`` to ``<engine dir>/runner.py`` and run with the engine's own python
(a torch venv; never the Hermes venv, never ComfyUI's python).  The model is the original big-lama TorchScript
read in place and loaded with ``torch.jit.load`` (spandrel 0.4.2 returned broken colours for
this file: inside error 0.62 vs ~0.01 with torch.jit).

Request  (studio.inpaint-runner-request/1): {"model": path, "device": "cuda"|"cpu", "image_png_b64", "mask_png_b64"}
          image RGB, mask L (> 127 = fill), same size.
Result   (studio.inpaint-runner-result/1):  {"image_png_b64", "width", "height", "device", "wall_ms"}
Failure: a message on stderr and exit code 3.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import time

# macOS (PR #2 by @ShawnCRMer): an op Metal (MPS) lacks runs on the CPU instead of failing the job. Set before torch
# is imported (inside main); only on macOS, so Windows and Linux runs see no new variable.
if sys.platform == "darwin":
    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

REQUEST_SCHEMA = "studio.inpaint-runner-request/1"
RESULT_SCHEMA = "studio.inpaint-runner-result/1"


def main() -> int:
    try:
        request = json.loads(sys.stdin.read())
        if not isinstance(request, dict) or request.get("schema") != REQUEST_SCHEMA:
            raise ValueError("not a studio.inpaint-runner-request/1")
        import numpy as np
        import torch
        from PIL import Image
        started = time.monotonic()
        image = Image.open(io.BytesIO(base64.b64decode(request["image_png_b64"]))).convert("RGB")
        mask = Image.open(io.BytesIO(base64.b64decode(request["mask_png_b64"]))).convert("L")
        if image.size != mask.size:
            raise ValueError("image and mask differ in size")
        wanted = request.get("device")
        if wanted == "cuda" and torch.cuda.is_available():
            device = "cuda"
        elif wanted == "mps" and getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"
        width, height = image.size
        pad_w, pad_h = (-width) % 8, (-height) % 8  # big-lama needs sides divisible by 8
        img = np.asarray(image, dtype=np.float32) / 255.0
        msk = (np.asarray(mask, dtype=np.uint8) > 127).astype(np.float32)
        if pad_w or pad_h:
            img = np.pad(img, ((0, pad_h), (0, pad_w), (0, 0)), mode="reflect")
            msk = np.pad(msk, ((0, pad_h), (0, pad_w)), mode="reflect")
        model = torch.jit.load(request["model"], map_location=device)
        model.eval()
        it = torch.from_numpy(img).permute(2, 0, 1)[None].to(device)
        mt = torch.from_numpy(msk)[None, None].to(device)
        with torch.no_grad():
            out = model(it, mt)
        result = (out[0].permute(1, 2, 0).clamp(0, 1).cpu().numpy() * 255.0).round().astype(np.uint8)[:height, :width]
        buffer = io.BytesIO()
        Image.fromarray(result, mode="RGB").save(buffer, format="PNG")
        sys.stdout.write(json.dumps({"schema": RESULT_SCHEMA, "image_png_b64": base64.b64encode(buffer.getvalue()).decode("ascii"),
                                     "width": width, "height": height, "device": device,
                                     "wall_ms": int((time.monotonic() - started) * 1000)}))
        return 0
    except Exception as exc:  # noqa: BLE001 - one finite answer per job
        sys.stderr.write(f"lama runner failed: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    sys.exit(main())
