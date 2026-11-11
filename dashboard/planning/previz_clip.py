"""The previz guide clip (lane w20-plan2 item B6, contract 5): the seam for the H3 structure guide, no UI.

The page renders the active shot camera's NORMAL pass with the one pure ``evaluate(scene, shot, t)`` the live view uses
(src/planning/previz-eval.js), sampling t = i / (frames - 1) end-inclusive, at an EXPLICIT request: width, height,
rational fps (num/den) and frame count (the take target's canvas and frame grid, never the shot's decimal duration).
This module checks every frame is a PNG of exactly width x height, encodes them lossless (FFV1 in Matroska, rgb24, the
given rational rate) and verifies the written clip with ffprobe (size, frame count). Next to it a sidecar JSON carries
the request, the source revisions, the resolved figure/element ids and the normal convention. Kind ``motion_guide``:
never a keyframe and never a ``control_video``.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env

KIND = "motion_guide"
NORMAL_CONVENTION = "view-space normals, +x right, +y up, +z toward the camera; rgb = n * 0.5 + 0.5 (three.js MeshNormalMaterial)"
MAX_FRAMES = 1201
_PNG = b"\x89PNG\r\n\x1a\n"


class ClipRefusal(Exception):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code, self.message = reason_code, message


def png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[:8] != _PNG or data[12:16] != b"IHDR":
        raise ClipRefusal("frame_invalid", "A frame is a PNG.")
    return struct.unpack(">II", data[16:24])


def _frame(raw: str) -> bytes:
    raw = re.sub(r"^data:image/png;base64,", "", raw)
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        raise ClipRefusal("frame_invalid", "A frame is base64 PNG data.") from None


def probe(path: Path, ffmpeg: str) -> dict[str, Any]:
    ffprobe = str(Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix))
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                          "stream=width,height,nb_read_frames,r_frame_rate,codec_name,pix_fmt", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=300, check=True, env=child_env())
    s = json.loads(out.stdout)["streams"][0]
    return {"width": int(s["width"]), "height": int(s["height"]), "frames": int(s["nb_read_frames"]), "rate": s["r_frame_rate"],
            "codec": s["codec_name"], "pix_fmt": s["pix_fmt"]}


def encode(folder: Path, shot: str, width: int, height: int, fps_num: int, fps_den: int, frames: list[str],
           sidecar: dict[str, Any], ffmpeg: str) -> dict[str, Any]:
    if not re.fullmatch(r"sh-[0-9a-f]{8}", shot):
        raise ClipRefusal("shot_invalid", "A shot id is sh-<8 hex>.")
    if not 1 <= len(frames) <= MAX_FRAMES:
        raise ClipRefusal("frames_invalid", f"1 to {MAX_FRAMES} frames.")
    if width % 2 or height % 2:
        raise ClipRefusal("size_invalid", "Width and height are even numbers.")
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{shot}-normal-{width}x{height}-{len(frames)}f-{uuid.uuid4().hex[:8]}"
    target = folder / f"{name}.mkv"
    with tempfile.TemporaryDirectory(prefix="previz-clip-") as tmp:
        for i, raw in enumerate(frames):
            data = _frame(raw)
            if png_size(data) != (width, height):
                raise ClipRefusal("frame_size", f"Frame {i} is {png_size(data)[0]}x{png_size(data)[1]}, not {width}x{height}.")
            (Path(tmp) / f"{i:05d}.png").write_bytes(data)
        done = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", f"{fps_num}/{fps_den}",
                               "-start_number", "0", "-i", str(Path(tmp) / "%05d.png"), "-frames:v", str(len(frames)),
                               "-c:v", "ffv1", "-level", "3", "-pix_fmt", "rgb24", "-r", f"{fps_num}/{fps_den}", str(target)],
                              capture_output=True, text=True, timeout=600, env=child_env())
        if done.returncode != 0:
            raise ClipRefusal("encode_failed", (done.stderr or "ffmpeg failed").strip()[-300:])
    facts = probe(target, ffmpeg)
    if (facts["width"], facts["height"], facts["frames"]) != (width, height, len(frames)):
        target.unlink(missing_ok=True)
        raise ClipRefusal("encode_mismatch", f"The clip came out {facts}; nothing kept.")
    meta = {"schema": "studio.previz-guide/1", "kind": KIND, "shot": shot, "width": width, "height": height,
            "fps": {"num": fps_num, "den": fps_den}, "frames": len(frames), "sampling": "t = i / (frames - 1), end-inclusive",
            "normal_convention": NORMAL_CONVENTION, "file": target.name, **{k: v for k, v in sidecar.items() if k not in ("kind", "file")}}
    (folder / f"{name}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return {"kind": KIND, "file": f"exports/previz/{target.name}", "sidecar": f"exports/previz/{name}.json", "probe": facts}
