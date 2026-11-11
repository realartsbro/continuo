"""Control layers as derived media (lane fs-ctrl).

One source (a finished take, a published picture, or a file of the approved folders) gives up to five
layers: pose, depth, edges, line art and camera move. The extractors are the ones measured in
out/fs-ctrl-CHOICE.md:

* pose   SDPose + RT-DETR (ComfyUI core nodes, template settings), drawn frames + keypoint JSON;
* depth  Depth Anything V2 Large (comfyui_controlnet_aux), then levelled per frame on static pixels;
* edges  Canny (comfyui_controlnet_aux, 100/200);
* lineart  the ffmpeg chain of Imagination Studio's lineart.py ("medium"), CPU, no model;
* camera   the subject-masked camera solve of control/camera.py, CPU, uses the pose keypoints.

Everything that runs in ComfyUI goes over its HTTP API: the graph reads the source by path, the
result is a TEMP output (nothing piles up in ComfyUI/output) fetched through /view. Before every
submit the queue is read; nothing here cancels, interrupts or reorders it. Results are cached per
(source content sha256, layer, extractor, range): a second ask is free.

This module has no FastAPI and no global state: the route file passes the ComfyUI client, ffmpeg and
the store folder in, so the tests drive it with a fake ComfyUI.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import numpy as np

from . import camera as camera_solve

# Lane w34-sec (scan finding 4): the one lexical guard against UNC / device paths (studio_core/local_path.py, stdlib).
import importlib.util as _import_util

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env
_local_path_spec = _import_util.spec_from_file_location(
    "film_suite_control_local_path", Path(__file__).resolve().parents[1] / "studio_core" / "local_path.py")
_local_path = _import_util.module_from_spec(_local_path_spec)
_local_path_spec.loader.exec_module(_local_path)

SCHEMA = "studio.control-layers/1"
LAYERS = ("pose", "depth", "edges", "lineart", "camera")
LAYER_LABELS = {"pose": "Pose", "depth": "Depth", "edges": "Edges", "lineart": "Line art", "camera": "Camera move"}
EXTRACTORS = {"pose": "sdpose-rtdetr/1", "depth": "da2-vitl-levelled/2", "edges": "canny-aux-100-200/1",
              "lineart": "is-ffmpeg-medium/1", "camera": "subject-masked-solve/3"}
MAX_SECONDS = 60.0
# Depth levelling: below this spread (0-255) the static pixels carry no gain; a gain stays within these bounds.
LEVEL_MIN_SPREAD = 6.0
LEVEL_GAIN = (0.5, 2.0)
CAMERA_SAMPLE_FPS = 12.0
POSE_MODELS = {"checkpoint": "sdpose_wholebody_fp16.safetensors", "detector": "rt_detr_v4-x-hgnet_fp16.safetensors"}
DEPTH_MODEL = "depth_anything_v2_vitl.pth"
# The ffmpeg line art of Imagination Studio (app/lineart.py, Apache-2.0), detail "medium".
LINEART_CHAIN = ("[0:v]bilateral=sigmaS=6:sigmaR=0.12,format=gray,gblur=sigma=0.8,split[a][b];[a]dilation[d];"
                 "[d][b]blend=all_mode=difference,lutyuv=y='clip((val-16-5)*9,0,255)':u=128:v=128,format=yuv420p[v]")

WORDS = {
    "comfy_down": "ComfyUI is not running.",
    "node_missing": "This install lacks the {what} nodes ({names}).",
    "model_missing": "The {what} model is missing ({name}).",
    "no_person": "No person was found in this {kind}.",
    "too_long": "Range longer than 60 s for control layers.",
    "bad_range": "The range is outside the clip.",
    "no_frames": "No frames could be read from this {kind}.",
    "camera_picture": "A picture has no camera move.",
    "job_failed": "ComfyUI could not make the {what} layer: {why}",
    "unreadable": "The source could not be read.",
    "wrong_use": "{why}",
}


class ControlError(Exception):
    """A plain refusal; ``code`` is stable, ``str()`` is the sentence the artist reads."""

    def __init__(self, code: str, **words: Any) -> None:
        self.code = code
        super().__init__(WORDS.get(code, code).format(**words))


@dataclass
class Source:
    source_id: str          # studio:..., imgjob_..., or file:<sha256>
    kind: str               # "video" | "image"
    path: Path              # a readable local file (a take, a staged picture, a file of the folders)
    label: str = ""
    sha256: str = ""
    fps: float = 0.0
    frames: int = 0
    width: int = 0
    height: int = 0
    seconds: float = 0.0


@dataclass
class Job:
    job_id: str
    owner_scope: str
    source_id: str
    layers: list[str]
    state: str = "waiting"          # waiting | running | done | failed | stopped
    current: str | None = None
    done: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    problems: list[dict[str, str]] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    stop: bool = False

    def public(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "job_id": self.job_id, "owner_scope": self.owner_scope, "source_id": self.source_id,
                "layers": list(self.layers), "state": self.state, "current": self.current, "done": list(self.done),
                "items": [public_item(item) for item in self.items], "problems": list(self.problems),
                "started_at": self.started_at, "finished_at": self.finished_at}


# ----------------------------------------------------------------- the store

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def range_key(range_in: float | None, range_out: float | None) -> str:
    if range_in is None and range_out is None:
        return "all"
    return f"{(range_in or 0):.3f}-{'end' if range_out is None else f'{range_out:.3f}'}"


def item_id_for(source_sha: str, layer: str, rkey: str) -> str:
    """The cache key of one layer (source content, layer, extractor, range). The Media item id is
    ``file:<sha256 of the layer file>``: the id Media marks (Kept, rating, tags) already take for a file."""
    return "ctrl_" + hashlib.sha256(f"{source_sha}|{layer}|{EXTRACTORS[layer]}|{rkey}".encode()).hexdigest()


def public_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """What Media and the agent see: the item without private bookkeeping (``keypoints`` stays inside)."""
    return {key: value for key, value in item.items() if key not in {"keypoints"}}


class Store:
    """Derived layers of one scope: files under ``root/<source sha 16>/`` and one index.json."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def own_file(self, value: Any) -> Path | None:
        """Lane w34-sec (finding 4): index.json travels with a project folder, so its paths are untrusted. A path is
        used only when its text is local and it resolves inside this store's root; anything else is no file here,
        decided before any stat (a network path is never touched)."""
        if not isinstance(value, str) or not value.strip() or _local_path.remote_path_reason(value) is not None:
            return None
        try:
            root = os.path.normcase(os.path.realpath(self.root))
            target = os.path.realpath(value)
            if os.path.normcase(os.path.commonpath([os.path.normcase(target), root])) != root:
                return None
        except (OSError, ValueError):
            return None
        return Path(target)

    def _alive(self, item: Mapping[str, Any]) -> bool:
        if item.get("kind") == "text":
            return True
        path = self.own_file(item.get("path"))
        return path is not None and path.is_file()

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict) or value.get("schema") != SCHEMA or not isinstance(value.get("items"), dict):
            raise ControlError("unreadable")
        items = value["items"]
        # An index written before layers had file ids (ctrl_ ids, no key): the cache key is the index key and
        # the Media id is file:<sha256 of the layer file>, as for every layer made now.
        for key, item in items.items():
            if isinstance(item, dict) and "key" not in item:
                item["key"] = key
                own = self.own_file(item.get("path"))
                if not item.get("sha256") and own is not None and own.is_file():
                    # A camera sentence of that time had no hash: its json file is its content.
                    item["sha256"] = sha256_file(own)
                if item.get("sha256"):
                    item["item_id"] = "file:" + item["sha256"]
        # Lane w34-sec (finding 4): an item naming a file outside this store (another folder, a network path) or
        # pose keypoints outside it is dropped from what this store answers; nothing of it is read.
        return {key: item for key, item in items.items() if isinstance(item, dict)
                and self.own_file(item.get("path")) is not None
                and (not item.get("keypoints") or self.own_file(item.get("keypoints")) is not None)}

    def items(self, source_id: str | None = None) -> list[dict[str, Any]]:
        found = [item for item in self.read().values() if source_id is None or item.get("derived_from") == source_id]
        # Only items whose file is still there are listed (a deleted project folder loses its layers).
        alive = [item for item in found if self._alive(item)]
        # One layer per source, layer and range: a layer made again by a newer extractor replaces the older one.
        newest: dict[tuple[Any, ...], dict[str, Any]] = {}
        for item in alive:
            slot = (item.get("derived_from"), item.get("layer"), item.get("range_in"), item.get("range_out"))
            if slot not in newest or (item.get("created_at") or 0) >= (newest[slot].get("created_at") or 0):
                newest[slot] = item
        alive = list(newest.values())
        return sorted(alive, key=lambda item: (item.get("derived_from") or "", LAYERS.index(item["layer"]), item.get("created_at") or 0))

    def get(self, key: str) -> dict[str, Any] | None:
        """One layer by its cache key (ctrl_...) or its Media item id (file:<sha256>)."""
        items = self.read()
        item = items.get(key) or next((value for value in items.values() if value.get("item_id") == key), None)
        if item and self._alive(item):
            return item
        return None

    def put(self, item: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        items = self.read()
        items[item["key"]] = item
        document = {"schema": SCHEMA, "items": items}
        temporary = self.index_path.with_name(f".index.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temporary, self.index_path)
        finally:
            temporary.unlink(missing_ok=True)

    def folder_for(self, source_sha: str) -> Path:
        """All layers of a scope sit in ONE folder, so Shoot approves one folder for every motion guide."""
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root


# ----------------------------------------------------------------- ffmpeg helpers

def probe(path: Path, ffprobe: Path) -> dict[str, Any]:
    done = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "v:0", "-show_entries",
                           "stream=width,height,r_frame_rate,nb_frames,duration:format=duration", "-of", "json", str(path)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, check=False, env=child_env())
    try:
        payload = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        payload = {}
    streams = payload.get("streams") or []
    if done.returncode or not streams:
        raise ControlError("unreadable")
    stream = streams[0]
    num, _, den = str(stream.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    seconds = float(stream.get("duration") or (payload.get("format") or {}).get("duration") or 0) or 0.0
    frames = int(stream.get("nb_frames") or 0) or int(round(seconds * fps))
    return {"width": int(stream.get("width") or 0), "height": int(stream.get("height") or 0), "fps": fps, "seconds": seconds, "frames": frames}


def _run(args: list[str], timeout: float = 600) -> None:
    done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False, env=child_env())
    if done.returncode:
        raise RuntimeError((done.stderr or "ffmpeg failed").strip()[-300:])


def _range_args(range_in: float | None, range_out: float | None) -> list[str]:
    args: list[str] = []
    if range_in:
        args += ["-ss", f"{range_in:.3f}"]
    if range_out is not None:
        args += ["-to", f"{range_out:.3f}"]
    return args


def read_gray_frames(path: Path, ffmpeg: Path, width: int, height: int, *, fps: float | None = None,
                     range_in: float | None = None, range_out: float | None = None) -> list[np.ndarray]:
    """Gray frames of a clip (or a picture) at width x height, as float arrays."""
    vf = f"scale={width}:{height}:flags=area,format=gray" + (f",fps={fps}" if fps else "")
    done = subprocess.run([str(ffmpeg), "-v", "error", *_range_args(range_in, range_out), "-i", str(path), "-vf", vf,
                           "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, timeout=600, check=False, env=child_env())
    if done.returncode:
        raise ControlError("no_frames", kind="clip")
    size = width * height
    data = np.frombuffer(done.stdout, np.uint8)
    count = len(data) // size
    return [data[i * size:(i + 1) * size].reshape(height, width).astype(np.float64) for i in range(count)]


def encode_gray(frames: list[np.ndarray], target: Path, ffmpeg: Path, fps: float) -> None:
    height, width = frames[0].shape
    part = target.with_name(target.stem + ".part" + target.suffix)
    process = subprocess.Popen([str(ffmpeg), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}",
                                "-r", f"{fps:.6f}", "-i", "-", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p",
                                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", str(part)], stdin=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env())
    try:
        for frame in frames:
            process.stdin.write(np.clip(frame, 0, 255).astype(np.uint8).tobytes())
        process.stdin.close()
        if process.wait(timeout=600):
            raise RuntimeError((process.stderr.read() or b"").decode("utf-8", "replace")[-300:])
    finally:
        if process.poll() is None:
            process.kill()
    part.replace(target)


# ----------------------------------------------------------------- ComfyUI graphs

def _load_node(source: Source, range_in: float | None, range_out: float | None) -> dict[str, Any]:
    if source.kind == "image":
        return {"class_type": "VHS_LoadImagePath", "inputs": {"image": str(source.path), "custom_width": 0, "custom_height": 0}}
    skip = int(round((range_in or 0) * source.fps))
    cap = int(round(((range_out if range_out is not None else source.seconds) - (range_in or 0)) * source.fps))
    return {"class_type": "VHS_LoadVideoPath", "inputs": {"video": str(source.path), "force_rate": 0, "custom_width": 0, "custom_height": 0,
                                                            "frame_load_cap": max(0, cap), "skip_first_frames": max(0, skip), "select_every_nth": 1}}


def _out_node(source: Source, image_ref: list[Any]) -> dict[str, Any]:
    if source.kind == "image":
        return {"class_type": "PreviewImage", "inputs": {"images": image_ref}}
    return {"class_type": "VHS_VideoCombine", "inputs": {"images": image_ref, "frame_rate": source.fps or 24, "loop_count": 0,
                                                          "filename_prefix": "fs-ctrl/layer", "format": "video/h264-mp4", "pix_fmt": "yuv420p",
                                                          "crf": 14, "save_metadata": False, "trim_to_audio": False, "pingpong": False, "save_output": False}}


def comfy_graph(layer: str, source: Source, range_in: float | None, range_out: float | None) -> dict[str, Any]:
    g: dict[str, Any] = {"1": _load_node(source, range_in, range_out)}
    if layer == "pose":
        g["3"] = {"class_type": "ResizeImageMaskNode", "inputs": {"input": ["1", 0], "resize_type": "scale longer dimension",
                                                                   "resize_type.longer_size": 1024, "scale_method": "lanczos"}}
        g["4"] = {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": POSE_MODELS["checkpoint"]}}
        g["5"] = {"class_type": "UNETLoader", "inputs": {"unet_name": POSE_MODELS["detector"], "weight_dtype": "default"}}
        g["6"] = {"class_type": "RTDETR_detect", "inputs": {"model": ["5", 0], "image": ["3", 0], "threshold": 0.5, "class_name": "person", "max_detections": 2}}
        g["7"] = {"class_type": "SDPoseKeypointExtractor", "inputs": {"model": ["4", 0], "vae": ["4", 2], "image": ["3", 0], "batch_size": 16, "bboxes": ["6", 0]}}
        g["2"] = {"class_type": "SDPoseDrawKeypoints", "inputs": {"keypoints": ["7", 0], "draw_body": True, "draw_hands": True, "draw_face": True,
                                                                   "draw_feet": True, "stick_width": 4, "face_point_size": 2, "score_threshold": 0.51, "draw_head": True}}
        g["8"] = {"class_type": "PreviewAny", "inputs": {"source": ["7", 0]}}
    elif layer == "depth":
        g["2"] = {"class_type": "DepthAnythingV2Preprocessor", "inputs": {"image": ["1", 0], "ckpt_name": DEPTH_MODEL, "resolution": 512 if source.kind == "video" else 1024}}
    elif layer == "edges":
        g["2"] = {"class_type": "CannyEdgePreprocessor", "inputs": {"image": ["1", 0], "low_threshold": 100, "high_threshold": 200,
                                                                     "resolution": 512 if source.kind == "video" else 1024}}
    else:
        raise ValueError(layer)
    g["9"] = _out_node(source, ["2", 0])
    return g


NEEDED_NODES = {
    "pose": ("ResizeImageMaskNode", "CheckpointLoaderSimple", "UNETLoader", "RTDETR_detect", "SDPoseKeypointExtractor", "SDPoseDrawKeypoints", "PreviewAny"),
    "depth": ("DepthAnythingV2Preprocessor",),
    "edges": ("CannyEdgePreprocessor",),
}
NODE_WORDS = {"pose": "pose", "depth": "depth", "edges": "edge"}
LOADER_NODES = ("VHS_LoadVideoPath", "VHS_LoadImagePath", "VHS_VideoCombine", "PreviewImage")


async def check_comfy(comfy: Any, layer: str) -> None:
    """Fail closed before any submit: the nodes and the model files this layer needs."""
    names = (*NEEDED_NODES[layer], *LOADER_NODES)
    missing: list[str] = []
    infos: dict[str, Any] = {}
    for name in names:
        try:
            info = await comfy.object_info(name)
        except Exception as exc:  # noqa: BLE001 - unreachable vs. unknown node
            if getattr(exc, "reason_code", "") == "unreachable" or isinstance(exc, (ConnectionError, OSError)):
                raise ControlError("comfy_down") from None
            info = {}
        if name not in (info or {}):
            missing.append(name)
        else:
            infos[name] = info[name]
    if missing:
        raise ControlError("node_missing", what=NODE_WORDS[layer] if any(m in NEEDED_NODES[layer] for m in missing) else "video loader", names=", ".join(missing))

    def options(node: str, key: str) -> list[str]:
        spec = ((infos[node].get("input") or {}).get("required") or {}).get(key) or ((infos[node].get("input") or {}).get("optional") or {}).get(key) or []
        first = spec[0] if spec else []
        if isinstance(first, list):
            return first
        if isinstance(first, str) and first == "COMBO" and len(spec) > 1:
            return list((spec[1] or {}).get("options") or [])
        return []

    if layer == "pose":
        if POSE_MODELS["checkpoint"] not in options("CheckpointLoaderSimple", "ckpt_name"):
            raise ControlError("model_missing", what="pose", name=POSE_MODELS["checkpoint"])
        if POSE_MODELS["detector"] not in options("UNETLoader", "unet_name"):
            raise ControlError("model_missing", what="person detector", name=POSE_MODELS["detector"])
    if layer == "depth" and DEPTH_MODEL not in options("DepthAnythingV2Preprocessor", "ckpt_name"):
        raise ControlError("model_missing", what="depth", name=DEPTH_MODEL)


async def run_comfy(comfy: Any, graph: dict[str, Any], *, should_stop: Callable[[], bool], poll_s: float = 1.0, timeout_s: float = 1800) -> dict[str, Any]:
    """Submit one graph (after reading the queue) and wait for its history. Never cancels anything:
    a Stop lets the running prompt finish."""
    try:
        await comfy.queue()
        answer = await comfy.submit(graph, client_id="film-suite-control")
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "reason_code", "") == "unreachable" or isinstance(exc, (ConnectionError, OSError)):
            raise ControlError("comfy_down") from None
        raise ControlError("job_failed", what="control", why=str(exc)[:200]) from None
    prompt_id = answer.get("prompt_id") if isinstance(answer, dict) else None
    if not prompt_id:
        raise ControlError("job_failed", what="control", why=json.dumps(answer)[:200])
    started = time.monotonic()
    while True:
        history = await comfy.history(prompt_id)
        if isinstance(history, dict) and prompt_id in history:
            entry = history[prompt_id]
            status = entry.get("status") or {}
            if status.get("status_str") not in (None, "success"):
                why = next((m[1].get("exception_message") for m in status.get("messages") or [] if m and m[0] == "execution_error"), None) or "it stopped with an error"
                raise ControlError("job_failed", what="control", why=str(why).strip()[:200])
            if status.get("completed", True):
                return entry
        if time.monotonic() - started > timeout_s:
            raise ControlError("job_failed", what="control", why="it did not finish in 30 minutes (still in the ComfyUI queue)")
        await asyncio.sleep(poll_s)


def _outputs(entry: Mapping[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    files: list[dict[str, str]] = []
    texts: list[str] = []
    for node in (entry.get("outputs") or {}).values():
        for key in ("gifs", "images", "videos"):
            for ref in node.get(key) or []:
                if isinstance(ref, dict) and ref.get("filename"):
                    files.append({"filename": ref["filename"], "subfolder": ref.get("subfolder", ""), "type": ref.get("type", "temp")})
        for text in node.get("text") or []:
            texts.append(text)
    return files, texts


# ----------------------------------------------------------------- per-layer work

def keypoint_people(keypoints: Any) -> list[list[list[list[float]]]]:
    """OpenPose-frame JSON -> per frame, per person, 18 points (x, y, conf) in 0..1 units."""
    frames = []
    for frame in keypoints if isinstance(keypoints, list) else []:
        width = float(frame.get("canvas_width") or 1)
        height = float(frame.get("canvas_height") or 1)
        people = []
        for person in frame.get("people") or []:
            flat = person.get("pose_keypoints_2d") or []
            people.append([[flat[i] / width, flat[i + 1] / height, flat[i + 2]] for i in range(0, len(flat) - 2, 3)])
        frames.append(people)
    return frames


def level_depth(depth: list[np.ndarray], gray: list[np.ndarray], people: list[Any] | None) -> list[np.ndarray]:
    """Each depth frame re-levelled (gain + offset, least squares) to the previous levelled frame on pixels
    that do not change in the source and lie outside the subject: the frame-to-frame flicker of a
    per-frame depth model drops 38-76 % on the measured clips (CHOICE §2)."""
    if len(depth) < 2:
        return depth
    h, w = depth[0].shape
    out = [depth[0]]
    for i in range(1, len(depth)):
        a, b = gray[min(i - 1, len(gray) - 1)], gray[min(i, len(gray) - 1)]
        if a.shape != (h, w):
            a = np.asarray(_resize(a, w, h))
            b = np.asarray(_resize(b, w, h))
        mask = np.abs(a - b) < 3
        persons = people[i - 1] if people and i - 1 < len(people) else None
        box = camera_solve.subject_mask(w, h, persons)
        if box is not None:
            mask &= box > 0
        if mask.sum() > 50:
            here, before = depth[i][mask], out[-1][mask]
            if float(here.std()) < LEVEL_MIN_SPREAD:
                # Too little spread to fit a gain (a flat wall, noise only): a gain fitted there tends to 0
                # and, chained over frames, flattens the clip. Move the level only.
                gain, offset = 1.0, float(before.mean() - here.mean())
            else:
                design = np.column_stack([here, np.ones(int(mask.sum()))])
                gain, offset = np.linalg.lstsq(design, before, rcond=None)[0]
                if not LEVEL_GAIN[0] <= gain <= LEVEL_GAIN[1]:
                    gain = min(max(float(gain), LEVEL_GAIN[0]), LEVEL_GAIN[1])
                    offset = float(before.mean() - gain * here.mean())
            out.append(np.clip(depth[i] * gain + offset, 0, 255))
        else:
            out.append(depth[i])
    return out


def _resize(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    ys = (np.arange(height) + 0.5) * frame.shape[0] / height - 0.5
    xs = (np.arange(width) + 0.5) * frame.shape[1] / width - 0.5
    return camera_solve._sample(frame, xs[None, :].repeat(height, 0), ys[:, None].repeat(width, 1))


def camera_layer(source: Source, ffmpeg: Path, range_in: float | None, range_out: float | None, people: list[Any] | None) -> dict[str, Any]:
    if source.kind != "video":
        raise ControlError("camera_picture")
    aw, ah = camera_solve.analysis_size(source.width, source.height)
    sample = min(CAMERA_SAMPLE_FPS, source.fps or CAMERA_SAMPLE_FPS)
    frames = read_gray_frames(source.path, ffmpeg, aw, ah, fps=sample, range_in=range_in, range_out=range_out)
    if len(frames) < 2:
        raise ControlError("no_frames", kind="clip")
    step = (source.fps or sample) / sample

    def people_at(index: int) -> Any:
        if not people:
            return None
        at = int(round(index * step))
        return people[at] if at < len(people) else None

    solved = camera_solve.analyze_camera(frames, sample_fps=sample, width=source.width, height=source.height, people_at=people_at if people else None)
    said = camera_solve.describe_camera(solved)
    return {"sentence": said["sentence"], "moves": said["moves"], "confidence": said["confidence"], "reliable": said["reliable"],
            "shake": said["shake"], "subject_masked": bool(people), "samples": len(frames), "summary": solved["summary"]}


# ----------------------------------------------------------------- the service

class ControlService:
    def __init__(self, *, comfy_factory: Callable[[], Any], ffmpeg: Path, ffprobe: Path, clock: Callable[[], float] = time.time) -> None:
        self.comfy_factory = comfy_factory
        self.ffmpeg = Path(ffmpeg)
        self.ffprobe = Path(ffprobe)
        self.clock = clock
        self.jobs: dict[str, Job] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.lock = threading.Lock()  # one extraction at a time: ComfyUI gets one control job after the other

    # -- sources
    def describe_source(self, source: Source) -> Source:
        if not source.sha256:
            source.sha256 = sha256_file(source.path)
        if source.kind == "video":
            facts = probe(source.path, self.ffprobe)
            source.fps, source.frames, source.width, source.height, source.seconds = facts["fps"], facts["frames"], facts["width"], facts["height"], facts["seconds"]
            if not source.frames or not source.fps:
                raise ControlError("no_frames", kind="clip")
        else:
            facts = probe(source.path, self.ffprobe)
            source.width, source.height = facts["width"], facts["height"]
        return source

    @staticmethod
    def check_range(source: Source, range_in: float | None, range_out: float | None) -> tuple[float | None, float | None]:
        if source.kind == "image":
            return None, None
        start = float(range_in or 0)
        end = float(range_out) if range_out is not None else source.seconds
        if start < 0 or end <= start or start >= source.seconds + 1e-3 or end > source.seconds + 0.05:
            raise ControlError("bad_range")
        if end - start > MAX_SECONDS + 1e-6:
            raise ControlError("too_long")
        return (start or None), (None if range_out is None or abs(end - source.seconds) < 1e-3 else end)

    # -- jobs
    def start(self, store: Store, owner_scope: str, source: Source, layers: list[str], range_in: float | None, range_out: float | None) -> Job:
        """Start (or answer from the cache) one extraction. ``source`` must already be described."""
        wanted = [layer for layer in LAYERS if layer in set(layers)]
        range_in, range_out = self.check_range(source, range_in, range_out)
        if source.kind == "image" and "camera" in wanted:
            wanted.remove("camera")
            problem = [{"layer": "camera", "code": "camera_picture", "message": str(ControlError("camera_picture"))}]
        else:
            problem = []
        job = Job(job_id="ctrljob_" + uuid.uuid4().hex, owner_scope=owner_scope, source_id=source.source_id, layers=wanted, problems=problem)
        self.jobs[job.job_id] = job
        rkey = range_key(range_in, range_out)
        cached = [self._placed(store, store.get(item_id_for(source.sha256, layer, rkey)), source) for layer in wanted]
        if all(cached):
            job.items, job.done, job.state, job.finished_at = [c for c in cached if c], list(wanted), "done", self.clock()
            return job
        # A thread with its own loop: an agent call's loop may end before the job does.
        def run() -> None:
            with self.lock:
                asyncio.run(self._run(job, store, source, range_in, range_out))
        thread = threading.Thread(target=run, name=f"control-{job.job_id}", daemon=True)
        self.threads[job.job_id] = thread
        thread.start()
        return job

    def stop(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job and job.state in ("waiting", "running"):
            job.stop = True
        return job

    async def wait(self, job_id: str, wait_ms: int) -> Job | None:
        job = self.jobs.get(job_id)
        deadline = time.monotonic() + max(0, wait_ms) / 1000
        while job and job.state in ("waiting", "running") and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        return job

    async def _run(self, job: Job, store: Store, source: Source, range_in: float | None, range_out: float | None) -> None:
        job.state = "running"
        rkey = range_key(range_in, range_out)
        people: list[Any] | None = None
        pose_item = store.get(item_id_for(source.sha256, "pose", rkey))
        if pose_item and pose_item.get("keypoints"):
            people = keypoint_people(_read_json(Path(pose_item["keypoints"])))
        try:
            for layer in job.layers:
                if job.stop:
                    job.state = "stopped"
                    break
                job.current = layer
                item_id = item_id_for(source.sha256, layer, rkey)
                cached = self._placed(store, store.get(item_id), source)
                if cached:
                    job.items.append(cached)
                    job.done.append(layer)
                    continue
                try:
                    item = await self._make(layer, store, source, range_in, range_out, item_id, people)
                except ControlError as exc:
                    # Line art and camera still run on the CPU when ComfyUI is down or a model is missing.
                    job.problems.append({"layer": layer, "code": exc.code, "message": str(exc)})
                    continue
                if layer == "pose" and item.get("keypoints"):
                    people = keypoint_people(_read_json(Path(item["keypoints"])))
                store.put(item)
                job.items.append(item)
                job.done.append(layer)
            if job.state != "stopped":
                job.state = "done" if job.done else "failed"
        except Exception as exc:  # noqa: BLE001 - a job ends readable, never with a traceback
            job.problems.append({"layer": job.current or "", "code": "unexpected", "message": f"Extraction stopped: {str(exc)[:200]}"})
            job.state = "failed"
        finally:
            job.current = None
            job.finished_at = self.clock()

    @staticmethod
    def _placed(store: Store, item: dict[str, Any] | None, source: Source) -> dict[str, Any] | None:
        """A cached layer of a file made before layers carried ``source_path``: asked again from that file,
        it learns the path, so Media groups it under the file."""
        if item and source.source_id.startswith("file:") and item.get("source_path") != str(source.path):
            item = {**item, "source_path": str(source.path)}
            store.put(item)
        return item

    async def _make(self, layer: str, store: Store, source: Source, range_in: float | None, range_out: float | None,
                    item_id: str, people: list[Any] | None) -> dict[str, Any]:
        folder = store.folder_for(source.sha256)
        rkey = range_key(range_in, range_out)
        base = f"{source.sha256[:16]}-{layer}-{rkey}"
        item: dict[str, Any] = {"key": item_id, "item_id": "", "layer": layer, "label": LAYER_LABELS[layer], "derived_from": source.source_id,
                                "source_label": source.label, "source_kind": source.kind, "extractor": EXTRACTORS[layer],
                                "range_in": range_in, "range_out": range_out, "created_at": self.clock(),
                                "width": source.width, "height": source.height}
        if source.source_id.startswith("file:"):
            # A file of the artist's folders: its own path (never a ledger's) lets Media group the layer under it.
            item["source_path"] = str(source.path)
        started = time.monotonic()
        if layer == "camera":
            facts = await asyncio.to_thread(camera_layer, source, self.ffmpeg, range_in, range_out, people)
            target = folder / f"{base}.json"
            part = target.with_name(target.stem + ".part.json")
            part.write_text(json.dumps(facts, indent=1), encoding="utf-8")
            part.replace(target)
            item.update({"kind": "text", "path": str(target), "sentence": facts["sentence"], "camera": {k: facts[k] for k in ("moves", "confidence", "reliable", "shake", "subject_masked")}})
        elif layer == "lineart":
            suffix = ".mp4" if source.kind == "video" else ".png"
            target = folder / f"{base}{suffix}"
            part = target.with_name(target.stem + ".part" + suffix)
            codec = ["-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p"] if source.kind == "video" else ["-frames:v", "1"]
            chain = LINEART_CHAIN if source.kind == "video" else LINEART_CHAIN.replace(",format=yuv420p[v]", ",format=gray[v]")
            await asyncio.to_thread(_run, [str(self.ffmpeg), "-v", "error", "-y", *_range_args(range_in, range_out), "-i", str(source.path),
                                           "-filter_complex", chain, "-map", "[v]", "-an", *codec, str(part)])
            part.replace(target)
            item.update({"kind": source.kind, "path": str(target)})
        else:
            comfy = self.comfy_factory()
            await check_comfy(comfy, layer)
            entry = await run_comfy(comfy, comfy_graph(layer, source, range_in, range_out), should_stop=lambda: False)
            files, texts = _outputs(entry)
            if not files:
                raise ControlError("job_failed", what=LAYER_LABELS[layer].lower(), why="it returned no file")
            suffix = ".mp4" if source.kind == "video" else ".png"
            target = folder / f"{base}{suffix}"
            data = await comfy.view(files[0])
            if layer == "pose":
                keypoints = json.loads(texts[0]) if texts else []
                if not any((frame or {}).get("people") for frame in keypoints):
                    raise ControlError("no_person", kind="clip" if source.kind == "video" else "picture")
                kp_path = folder / f"{base}.keypoints.json"
                kp_path.write_text(json.dumps(keypoints), encoding="utf-8")
                item["keypoints"] = str(kp_path)
                item["people_frames"] = sum(1 for frame in keypoints if (frame or {}).get("people"))
            if layer == "depth" and source.kind == "video":
                await asyncio.to_thread(self._level_and_write, data, target, source, range_in, range_out, people)
                item["levelled"] = True
            else:
                part = target.with_name(target.stem + ".part" + suffix)
                part.write_bytes(data)
                part.replace(target)
            item.update({"kind": source.kind, "path": str(target)})
        item["seconds_to_make"] = round(time.monotonic() - started, 2)
        if item["kind"] != "text":
            try:
                facts = probe(Path(item["path"]), self.ffprobe)
                item.update({"width": facts["width"], "height": facts["height"]})
                if source.kind == "video":
                    item.update({"fps": facts["fps"], "frames": facts["frames"], "duration_seconds": round(facts["seconds"], 3)})
            except ControlError:
                pass
        item["sha256"] = sha256_file(Path(item["path"]))
        item["item_id"] = "file:" + item["sha256"]
        return item

    def _level_and_write(self, data: bytes, target: Path, source: Source, range_in: float | None, range_out: float | None, people: list[Any] | None) -> None:
        with tempfile.TemporaryDirectory(prefix="fs-ctrl-depth-") as scratch:
            raw = Path(scratch) / "depth.mp4"
            raw.write_bytes(data)
            facts = probe(raw, self.ffprobe)
            w, h = facts["width"] // 2 * 2, facts["height"] // 2 * 2
            depth = read_gray_frames(raw, self.ffmpeg, w, h)
            gw, gh = max(64, w // 2), max(64, h // 2)
            gray = read_gray_frames(source.path, self.ffmpeg, gw, gh, range_in=range_in, range_out=range_out)
            small = [_resize(d, gw, gh) for d in depth]
            levelled_small = level_depth(small, gray, people)
            # gain/offset solved small, applied full size
            out = []
            for d, s, l in zip(depth, small, levelled_small):
                if np.allclose(s, l):
                    out.append(d)
                    continue
                design = np.column_stack([s.ravel(), np.ones(s.size)])
                gain, offset = np.linalg.lstsq(design, l.ravel(), rcond=None)[0]
                out.append(np.clip(d * gain + offset, 0, 255))
            encode_gray(out, target, self.ffmpeg, facts["fps"] or source.fps or 24)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ----------------------------------------------------------------- hand-off (Shoot / Image)

def handoff(item: Mapping[str, Any], use: str) -> dict[str, Any]:
    """The one hand-off Media and the agent use: what goes where.

    ``motion_guide``: the References target's control_video slot takes the layer clip by its path.
    ``camera``: the camera sentence goes into the prompt's camera part.
    ``image_control``: a picture layer for Image's control input (spec out/fs-ctrl-SPEC-image-control.md)."""
    layer = item.get("layer")
    if use == "motion_guide":
        if item.get("kind") != "video" or layer not in ("pose", "depth", "edges", "lineart"):
            raise ControlError("wrong_use", why="Only a pose, depth, edges or line-art clip guides motion.") from None
        return {"use": use, "slot": "control_video", "path": item["path"], "label": f"{LAYER_LABELS[layer]} · {item.get('source_label') or 'clip'}",
                "layer": layer, "item_id": item["item_id"],
                # What a References take needs (continuo_video_submit args): the same file the slot holds.
                "submit_args": {"control_video": item["path"]}}
    if use == "camera":
        if layer != "camera" or not item.get("sentence"):
            raise ControlError("wrong_use", why="Only a camera-move layer carries a camera sentence.") from None
        return {"use": use, "prompt_part": "camera", "text": item["sentence"], "reliable": bool((item.get("camera") or {}).get("reliable")),
                "item_id": item["item_id"]}
    if use == "image_control":
        if item.get("kind") != "image" or layer not in ("pose", "depth", "edges", "lineart"):
            raise ControlError("wrong_use", why="Only a pose, depth, edges or line-art picture controls a picture.") from None
        return {"use": use, "control": layer, "path": item["path"], "item_id": item["item_id"], "width": item.get("width"), "height": item.get("height")}
    raise ControlError("wrong_use", why=f"Unknown use {use!r}.")


__all__ = ["ControlError", "ControlService", "EXTRACTORS", "LAYERS", "LAYER_LABELS", "Source", "Store", "comfy_graph", "handoff",
           "item_id_for", "keypoint_people", "level_depth", "public_item", "range_key"]
