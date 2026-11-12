"""MiniMax H3 media delivery and take facts (SPEC fs-h3a §4.3-§4.4, §6).

* Pre-fit: every keyframe still and the control clip are centre cover-cropped
  to the exact canvas, so the node's own resize is an identity; reference
  images and clips keep their aspect (``ref_image_size: match``).
* Clips are re-encoded at 24 fps and cut to the take's effective frames;
  fewer than 5 frames is refused (the node raises on that).
* Upload: ``POST /upload/image`` with ``type=temp`` on the fixed loopback
  ComfyUI, name ``<sha256>.<ext>``; the graph references ``"<name> [temp]"``.
  Nothing is written into ``ComfyUI/input``.
* Sound (lane fs-h3aud): audio is decoded to 16-bit PCM WAV, cut to its
  ``start``/``end`` (a sound track also to the shot length: it starts at frame
  0); a reference audio must last 2-15 s and all of them together <= 15 s
  (official guidance; the node does not check it).  A reference clip whose own
  sound is used keeps its first audio stream (cut with the picture); a clip
  without sound is refused for that.
* ``motion_activity``: mean absolute grey difference between consecutive
  frames on a 640-px proxy (fs-cam2 ``act``), 0..255; below 1.0 the take
  barely moves (``frozen``).
* Take facts: one small private JSON file per job beside the ledger.

Standalone module (no package-relative imports, no dataclasses).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tempfile
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env

# w39-p2-tester item 4: the one configured ComfyUI address (generation/comfy_endpoint.py, loaded by path).
import importlib.util as _endpoint_util
from pathlib import Path as _EndpointPath
_endpoint_spec = _endpoint_util.spec_from_file_location("film_suite_comfy_endpoint", _EndpointPath(__file__).resolve().parents[1] / "generation" / "comfy_endpoint.py")
_endpoint = _endpoint_util.module_from_spec(_endpoint_spec)
_endpoint_spec.loader.exec_module(_endpoint)
COMFY_ORIGIN = _endpoint.DEFAULT_ORIGIN  # the default; every request reads _endpoint.comfy_origin()
FROZEN_BELOW = 1.0
PROXY_WIDTH = 640
MIN_CLIP_FRAMES = 5
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_CLIP_BYTES = 512 * 1024 * 1024
MAX_AUDIO_BYTES = 256 * 1024 * 1024
AUDIO_MIN_SECONDS, AUDIO_MAX_SECONDS, AUDIO_TOTAL_SECONDS = 2.0, 15.0, 15.0
_FFMPEG_TIMEOUT = 300
TAKE_FACTS_SCHEMA = "studio.h3-take-facts/1"


class H3MediaError(ValueError):
    reason_code = "invalid_request"


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------- pre-fit


def cover_box(src_w: int, src_h: int, width: int, height: int) -> tuple[int, int, int, int]:
    """Centre crop box (left, top, right, bottom) in source pixels for a cover fit."""
    if src_w <= 0 or src_h <= 0:
        raise H3MediaError("picture has no size")
    scale = max(width / src_w, height / src_h)
    crop_w, crop_h = width / scale, height / scale
    left, top = (src_w - crop_w) / 2, (src_h - crop_h) / 2
    return round(left), round(top), round(left + crop_w), round(top + crop_h)


def prefit_image(path: str | os.PathLike[str], width: int, height: int, *, cover: bool) -> bytes:
    """PNG bytes: cover-cropped to ``width x height`` or (``cover=False``) unchanged pixels."""
    from PIL import Image, ImageOps  # type: ignore[import-not-found]

    if os.path.getsize(path) > MAX_IMAGE_BYTES:
        raise H3MediaError("picture is too large")
    with Image.open(path) as opened:
        picture = ImageOps.exif_transpose(opened).convert("RGB")
    if cover:
        box = cover_box(picture.width, picture.height, width, height)
        picture = picture.resize((width, height), Image.Resampling.LANCZOS, box=box)
    out = io.BytesIO()
    picture.save(out, format="PNG", optimize=False)
    return out.getvalue()


def _ffprobe_for(ffmpeg: str | os.PathLike[str]) -> str:
    path = Path(ffmpeg)
    name = "ffprobe.exe" if path.suffix.lower() == ".exe" else "ffprobe"
    return str(path.with_name(name))


def count_frames(path: str | os.PathLike[str], ffmpeg: str | os.PathLike[str]) -> int:
    out = subprocess.run(
        [_ffprobe_for(ffmpeg), "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT, check=False, env=child_env(),
    )
    try:
        return int(out.stdout.strip().split(",")[0])
    except (ValueError, IndexError):
        raise H3MediaError("clip cannot be read") from None


def prefit_clip(
    path: str | os.PathLike[str],
    width: int,
    height: int,
    frames: int,
    *,
    cover: bool,
    ffmpeg: str | os.PathLike[str],
    scratch: str | os.PathLike[str],
    sound: bool = False,
) -> bytes:
    """MP4 bytes at 24 fps holding the first ``frames`` frames; cover-cropped or aspect kept.

    ``sound``: keep the clip's first audio stream (AAC, cut with the picture);
    without ``sound`` the clip is silent, as before.
    """
    if os.path.getsize(path) > MAX_CLIP_BYTES:
        raise H3MediaError("clip is too large")
    if cover:
        vf = f"fps=24,scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,crop={width}:{height},setsar=1"
    else:
        vf = "fps=24,scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1"
    if sound and not audio_streams(path, ffmpeg):
        raise H3MediaError("this clip has no sound to use")
    Path(scratch).mkdir(parents=True, exist_ok=True)
    target = Path(scratch) / f"h3clip-{uuid.uuid4().hex}.mp4"
    audio = (["-map", "0:v:0", "-map", "0:a:0", "-c:a", "aac", "-b:a", "192k", "-t", f"{frames / 24:.6f}"]
             if sound else ["-an"])
    try:
        run = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-vf", vf,
             "-frames:v", str(frames), *audio, "-c:v", "libx264", "-preset", "veryfast", "-crf", "12",
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)],
            capture_output=True, timeout=_FFMPEG_TIMEOUT, check=False, env=child_env(),
        )
        if run.returncode != 0 or not target.is_file():
            raise H3MediaError("clip cannot be converted")
        if count_frames(target, ffmpeg) < MIN_CLIP_FRAMES:
            raise H3MediaError("clip must have at least 5 frames")
        return target.read_bytes()
    finally:
        try:
            target.unlink()
        except OSError:
            pass


def audio_streams(path: str | os.PathLike[str], ffmpeg: str | os.PathLike[str]) -> int:
    out = subprocess.run(
        [_ffprobe_for(ffmpeg), "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT, check=False, env=child_env(),
    )
    return len([line for line in out.stdout.splitlines() if line.strip()])


def audio_seconds(path: str | os.PathLike[str], ffmpeg: str | os.PathLike[str]) -> float:
    """Duration of the first audio stream (container duration as fallback)."""
    out = subprocess.run(
        [_ffprobe_for(ffmpeg), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT, check=False, env=child_env(),
    )
    try:
        payload = json.loads(out.stdout or "{}")
    except ValueError:
        payload = {}
    streams = payload.get("streams") or []
    if out.returncode != 0 or not streams:
        raise H3MediaError("sound file cannot be read")
    for value in (streams[0].get("duration"), (payload.get("format") or {}).get("duration")):
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds
    raise H3MediaError("sound file has no length")


def audio_span(item: Mapping[str, Any], total: float) -> tuple[float, float]:
    """The used part ``(start, end)`` of an audio item in seconds; a sound track ends with the shot."""
    start = float(item.get("start") or 0.0)
    end = item.get("end")
    end = total if end is None else min(float(end), total)
    if item.get("fit") == "track":
        end = min(end, start + float(item["limit_seconds"]))
    if end - start <= 0:
        raise H3MediaError("the chosen part of the sound file is empty")
    return start, end


def check_audio_lengths(spans: Iterable[tuple[Mapping[str, Any], float]]) -> None:
    """Reference audio: each 2-15 s, together at most 15 s (official guidance)."""
    total = 0.0
    for item, seconds in spans:
        if item.get("fit") != "reference":
            continue
        if not AUDIO_MIN_SECONDS - 0.01 <= seconds <= AUDIO_MAX_SECONDS + 0.01:
            raise H3MediaError("each voice or music reference must last 2 to 15 seconds")
        total += seconds
    if total > AUDIO_TOTAL_SECONDS + 0.01:
        raise H3MediaError("voice and music references may last at most 15 seconds together")


def prefit_audio(path: str | os.PathLike[str], start: float, end: float, *, ffmpeg: str | os.PathLike[str],
                 scratch: str | os.PathLike[str]) -> bytes:
    """16-bit PCM WAV of ``start``..``end`` seconds (sample rate and channels kept)."""
    if os.path.getsize(path) > MAX_AUDIO_BYTES:
        raise H3MediaError("sound file is too large")
    Path(scratch).mkdir(parents=True, exist_ok=True)
    target = Path(scratch) / f"h3audio-{uuid.uuid4().hex}.wav"
    try:
        run = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-map", "0:a:0",
             "-ss", f"{start:.6f}", "-t", f"{end - start:.6f}", "-vn", "-c:a", "pcm_s16le", "-f", "wav", str(target)],
            capture_output=True, timeout=_FFMPEG_TIMEOUT, check=False, env=child_env(),
        )
        if run.returncode != 0 or not target.is_file() or target.stat().st_size <= 44:
            raise H3MediaError("sound file cannot be converted")
        return target.read_bytes()
    finally:
        try:
            target.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- request media


def resolve_media(
    items: Iterable[Mapping[str, Any]],
    *,
    roots: tuple[str, ...],
    boundary: Any,
    local_path: Callable[[str], str],
    path_identity: Callable[[str, bytes], str],
    staged_root: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Confine each source to the approved roots; add path, path HMAC and content SHA-256.

    ``staged_root`` is Image's backend-chosen opening-frame folder (``prepared/opening-frames``, inside
    HERMES_HOME). A picture the page staged for the shot (keyframe drop, Frame -> Keyframe, the shot chain) is a
    direct child of exactly that folder and is checked with ``validate_staged_input``, as the GMI submit does;
    every other file inside HERMES_HOME stays refused. Callers pass the folder, never a caller-supplied path.
    """
    items = list(items)
    if not items:
        return []
    staged = os.path.normcase(os.path.abspath(staged_root)) if staged_root else None

    def is_staged(path: str) -> bool:
        return staged is not None and os.path.normcase(os.path.dirname(path)) == staged

    if not roots and not all(is_staged(os.path.abspath(local_path(item["source"]))) for item in items):
        raise H3MediaError("local media require an approved root")
    key = boundary.profile_identity_key()
    resolved = []
    for item in items:
        local = os.path.abspath(local_path(item["source"]))
        from_stage = is_staged(local)
        try:
            identity = boundary.validate_staged_input(local, staged_root) if from_stage else boundary.validate_input(local, roots)
        except ValueError as exc:
            # w35-ui-3 (critic run 3 #19): the boundary's literal ("path is outside approved roots") is the same for an
            # input and the output folder; the refusal carries which input it was (role, number, path) so the route names
            # it and never blames the output folder.
            exc.refused_side, exc.refused_path, exc.refused_key = "input", local, str(item.get("key") or "")
            raise
        try:
            content = sha256_file(local)
        except OSError:
            raise H3MediaError("a medium cannot be read") from None
        # The hashed file must still be the confined object that was validated.
        if from_stage:
            again = boundary.validate_staged_input(local, staged_root)
            if not identity.same_object(again) or identity.size != again.size:
                raise H3MediaError("a medium changed while it was read")
        else:
            boundary.revalidate_input(local, identity)
        resolved.append({**item, "path": local, "path_hmac": path_identity(identity.canonical_path, key), "source_sha256": content})
    return resolved


def identity_args(args: Mapping[str, Any], resolved: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Durable identity args: every source becomes ``{path: HMAC, sha256: content}``."""
    by_key = {item["key"]: {"path": item["path_hmac"], "sha256": item["source_sha256"]} for item in resolved}
    result = {name: value for name, value in args.items()
              if name not in {"keyframes", "reference_images", "reference_videos", "control_video", "reference_audios", "sound_track"}}
    if "keyframes" in args:
        result["keyframes"] = [{"frame": entry["frame"], "source": by_key[f"keyframes/{index}"]} for index, entry in enumerate(args["keyframes"])]
    for name in ("reference_images", "reference_videos"):
        if name in args:
            result[name] = [by_key[f"{name}/{index}"] for index in range(len(args[name]))]
    if "control_video" in args:
        result["control_video"] = by_key["control_video/0"]
    if "reference_audios" in args:
        result["reference_audios"] = [{**entry, "source": by_key[f"reference_audios/{index}"]} for index, entry in enumerate(args["reference_audios"])]
    if "sound_track" in args:
        result["sound_track"] = {**args["sound_track"], "source": by_key["sound_track/0"]}
    return result


# --------------------------------------------------------------------------- upload


def _multipart(fields: Mapping[str, str], file_field: str, filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----studio" + uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def upload_temp(data: bytes, filename: str, *, opener: Callable[..., Any] | None = None) -> str:
    """Upload bytes to ComfyUI ``temp/``; return the graph name ``"<name> [temp]"``."""
    body, boundary = _multipart({"type": "temp", "overwrite": "true"}, "image", filename, data)
    request = urllib.request.Request(
        _endpoint.comfy_origin() + "/upload/image", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    open_ = opener or _endpoint._opener.open  # comfy_endpoint._opener: never follows a redirect
    with open_(request, timeout=120) as response:
        raw = response.read(1 << 20)
    answer = json.loads(raw.decode("utf-8"))
    stored = answer.get("name") if isinstance(answer, dict) else None
    folder = answer.get("type") if isinstance(answer, dict) else None
    subfolder = answer.get("subfolder") if isinstance(answer, dict) else None
    if stored != filename or folder != "temp" or subfolder:
        raise H3MediaError("ComfyUI did not keep the medium under its content name")
    return f"{stored} [temp]"


def prefit_media(
    items: Iterable[Mapping[str, Any]],
    *,
    width: int,
    height: int,
    frames: int,
    ffmpeg: str | os.PathLike[str],
    scratch: str | os.PathLike[str],
) -> list[dict[str, Any]]:
    """Pre-fit every resolved medium (no side effect outside ``scratch``).

    ``items`` carry the confined ``path`` and the ``source_sha256`` captured
    at validation; a file that changed since is refused.
    """
    items = list(items)
    spans: dict[str, tuple[float, float]] = {}
    for item in items:
        if item["kind"] == "audio":
            spans[item["key"]] = audio_span(item, audio_seconds(item["path"], ffmpeg))
    check_audio_lengths((item, spans[item["key"]][1] - spans[item["key"]][0]) for item in items if item["key"] in spans)
    prepared: list[dict[str, Any]] = []
    for item in items:
        path = item["path"]
        if sha256_file(path) != item["source_sha256"]:
            raise H3MediaError("a medium changed after it was checked")
        cover = item["fit"] == "cover"
        if item["kind"] == "image":
            data, ext = prefit_image(path, width, height, cover=cover), ".png"
        elif item["kind"] == "audio":
            data, ext = prefit_audio(path, *spans[item["key"]], ffmpeg=ffmpeg, scratch=scratch), ".wav"
        else:
            data, ext = prefit_clip(path, width, height, frames, cover=cover, ffmpeg=ffmpeg, scratch=scratch,
                                    sound=bool(item.get("sound"))), ".mp4"
        entry = {"key": item["key"], "data": data, "ext": ext, "sha256": hashlib.sha256(data).hexdigest(),
                 "source_sha256": item["source_sha256"]}
        if item["key"] in spans:
            entry["seconds"] = round(spans[item["key"]][1] - spans[item["key"]][0], 3)
        prepared.append(entry)
    return prepared


def upload_prefit(prepared: Iterable[Mapping[str, Any]], upload: Callable[[bytes, str], str]) -> dict[str, dict[str, str]]:
    """Upload each pre-fitted medium as ``<sha256>.<ext>``; return ``{key: {name, sha256, source_sha256}}``."""
    delivered: dict[str, dict[str, str]] = {}
    for item in prepared:
        name = upload(item["data"], item["sha256"] + item["ext"])
        delivered[item["key"]] = {"name": name, "sha256": item["sha256"], "source_sha256": item["source_sha256"]}
    return delivered


# --------------------------------------------------------------------------- motion activity


def _probe_size(path: str | os.PathLike[str], ffmpeg: str | os.PathLike[str]) -> tuple[int, int]:
    out = subprocess.run(
        [_ffprobe_for(ffmpeg), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT, check=False, env=child_env(),
    )
    try:
        width, height = (int(v) for v in out.stdout.strip().split(",")[:2])
    except ValueError:
        raise H3MediaError("video cannot be read") from None
    if width <= 0 or height <= 0:
        raise H3MediaError("video cannot be read")
    return width, height


def motion_activity(path: str | os.PathLike[str], ffmpeg: str | os.PathLike[str], *, skip_frames: int = 0) -> float:
    """Mean absolute grey difference of consecutive frames on a 640-px proxy (0..255)."""
    import numpy as np  # type: ignore[import-not-found]

    width, height = _probe_size(path, ffmpeg)
    proxy_h = max(2, int(round(height * PROXY_WIDTH / width / 2)) * 2)
    run = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0",
         "-vf", f"scale={PROXY_WIDTH}:{proxy_h}:flags=area,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
        capture_output=True, timeout=_FFMPEG_TIMEOUT, check=False, env=child_env(),
    )
    frame_bytes = PROXY_WIDTH * proxy_h
    if run.returncode != 0 or len(run.stdout) < 2 * frame_bytes:
        raise H3MediaError("video cannot be decoded")
    count = len(run.stdout) // frame_bytes
    stack = np.frombuffer(run.stdout[: count * frame_bytes], dtype=np.uint8).reshape(count, proxy_h, PROXY_WIDTH)
    stack = stack[skip_frames:]
    if len(stack) < 2:
        raise H3MediaError("video is too short to measure")
    diffs = np.abs(np.diff(stack.astype(np.int16), axis=0)).mean(axis=(1, 2))
    return round(float(diffs.mean()), 2)


def local_motion_activity(path: str | os.PathLike[str], ffmpeg: str | os.PathLike[str]) -> float:
    """Largest mean frame difference in an 8x8 region; suppress single-pixel noise."""
    import numpy as np  # type: ignore[import-not-found]

    width, height = _probe_size(path, ffmpeg)
    proxy_h = max(2, int(round(height * PROXY_WIDTH / width / 2)) * 2)
    run = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0",
         "-vf", f"scale={PROXY_WIDTH}:{proxy_h}:flags=area,format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
        capture_output=True, timeout=_FFMPEG_TIMEOUT, check=False, env=child_env(),
    )
    frame_bytes = PROXY_WIDTH * proxy_h
    if run.returncode != 0 or len(run.stdout) < 2 * frame_bytes:
        raise H3MediaError("video cannot be decoded")
    count = len(run.stdout) // frame_bytes
    stack = np.frombuffer(run.stdout[:count * frame_bytes], dtype=np.uint8).reshape(count, proxy_h, PROXY_WIDTH)
    diff = np.abs(np.diff(stack.astype(np.int16), axis=0)).mean(axis=0)
    tiles = [tile.mean() for row in np.array_split(diff, 8, axis=0) for tile in np.array_split(row, 8, axis=1) if tile.size]
    return round(float(max(tiles)), 2)


def motion_facts(value: float) -> dict[str, Any]:
    return {"motion_activity": value, "frozen": value < FROZEN_BELOW}


def hold_end(path: str | os.PathLike[str], ffmpeg: str | os.PathLike[str]) -> float:
    """Mean absolute RGB difference (0..255) between the take's last and first frame (640-px proxy).

    fs-cam2 ``hold_end`` compares the last frame with the keyframe still; the
    take's own frame 0 holds that still (frame-0 PSNR 30.8-33.1 in every arm),
    so frame 0 stands in for it and a take without a keyframe is measured the
    same way.  Frozen runs measured <= 6.6, moving runs 14-58.
    """
    import numpy as np  # type: ignore[import-not-found]

    width, height = _probe_size(path, ffmpeg)
    proxy_h = max(2, int(round(height * PROXY_WIDTH / width / 2)) * 2)
    run = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0",
         "-vf", f"scale={PROXY_WIDTH}:{proxy_h}:flags=area,format=rgb24", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        capture_output=True, timeout=_FFMPEG_TIMEOUT, check=False, env=child_env(),
    )
    frame_bytes = PROXY_WIDTH * proxy_h * 3
    if run.returncode != 0 or len(run.stdout) < 2 * frame_bytes:
        raise H3MediaError("video cannot be decoded")
    count = len(run.stdout) // frame_bytes
    first = np.frombuffer(run.stdout[:frame_bytes], dtype=np.uint8).astype(np.int16)
    last = np.frombuffer(run.stdout[(count - 1) * frame_bytes: count * frame_bytes], dtype=np.uint8).astype(np.int16)
    return round(float(np.abs(last - first).mean()), 2)


FREEZE_CHECK_SCHEMA = "studio.h3-freeze-check/1"
# fs-cam2 RESULT §1 table (14 local jobs): the two signals and their measured ranges.
FREEZE_REFERENCE = {"act_frozen": [0.14, 0.63], "act_moving": [2.6, 11.6], "hold_end_frozen_max": 6.6,
                    "hold_end_moving": [14.0, 58.0]}


def freeze_check(act: float, hold: float | None, local_act: float | None = None) -> dict[str, Any]:
    """Mark a take frozen only when both global and measured local motion are low."""
    frozen = act < FROZEN_BELOW and (local_act is None or local_act < FROZEN_BELOW)
    return {"schema": FREEZE_CHECK_SCHEMA, "act": act, "local_act": local_act, "hold_end": hold, "frozen": frozen,
            "verdict": "frozen" if frozen else "moving", "act_below": FROZEN_BELOW, "reference": dict(FREEZE_REFERENCE),
            "advice": "Little motion: check the take" if frozen else None}


# --------------------------------------------------------------------------- take facts


def _facts_path(root: str | os.PathLike[str], studio_job_id: str) -> Path:
    name = hashlib.sha256(studio_job_id.encode("utf-8")).hexdigest()
    return Path(root) / f"{name}.json"


def read_take_facts(root: str | os.PathLike[str], studio_job_id: str) -> dict[str, Any] | None:
    path = _facts_path(root, studio_job_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != TAKE_FACTS_SCHEMA or payload.get("studio_job_id") != studio_job_id:
        return None
    return payload


def merge_take_facts(root: str | os.PathLike[str], studio_job_id: str, update: Mapping[str, Any]) -> dict[str, Any]:
    """Atomically merge write-once facts (an existing key is never overwritten)."""
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    current = read_take_facts(root, studio_job_id) or {"schema": TAKE_FACTS_SCHEMA, "studio_job_id": studio_job_id}
    for key, value in update.items():
        current.setdefault(key, value)
    handle, temporary = tempfile.mkstemp(prefix=".h3facts-", suffix=".json", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(current, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _facts_path(root, studio_job_id))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return current


def public_take_facts(facts: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Path-free projection for poll receipts and the jobs list."""
    if not facts:
        return None
    keys = ("effective_frames", "canvas", "media", "motion_activity", "frozen", "motion_state", "prompt_sent",
            "runtime", "runtime_tested", "runtime_check",
            # lane fs-obvb: the take's sidecar lineage and, for a continuation, its relation and relative motion
            "mctx", "mctx_state", "continuation", "motion_relative", "barely_moves",
            # lane w1-h3: the freeze verdict (act + hold_end) and a finished take's link to its source
            "freeze_check", "finish",
            # lane w2-h3ctl: the inspector controls that differ from the pinned graph, and Generate x N
            "controls", "batch",
            # lane w23-vsr: an upscaled take's link to its source and the settings it was made with
            "upscale")
    return {key: facts[key] for key in keys if key in facts}


__all__ = [
    "FREEZE_CHECK_SCHEMA",
    "FROZEN_BELOW",
    "H3MediaError",
    "freeze_check",
    "hold_end",
    "audio_seconds",
    "audio_span",
    "audio_streams",
    "check_audio_lengths",
    "identity_args",
    "resolve_media",
    "count_frames",
    "cover_box",
    "merge_take_facts",
    "motion_activity",
    "motion_facts",
    "prefit_audio",
    "prefit_clip",
    "prefit_image",
    "prefit_media",
    "public_take_facts",
    "read_take_facts",
    "sha256_file",
    "upload_prefit",
    "upload_temp",
]
