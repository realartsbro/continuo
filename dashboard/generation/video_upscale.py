"""Video upscale with RelayVSR (lane w23-vsr, VID-153): a finished video take becomes a NEW take.

Graph (pinned, ``workflows/relayvsr-video-upscale-v1``), the native core nodes by Kijai
(ComfyUI 2f8943324, ``comfy_extras/nodes_wan.py``) as the PR example and research
RELAYVSR.md 4.3 wire them:

    LoadVideo -> GetVideoComponents -> ImageFromBatch (IN..OUT) / TrimAudioDuration
    -> UNETLoader (x2 | x4) -> RelayVSRConditioning -> KSampler (1 step, cfg 1, euler/simple,
    negative = positive) -> VAELoader (the matching Flash VAE) -> VSRDecode (detail)
    -> ImageScale (filter, W x H; removed when the size is the model's own) -> CreateVideo
    (source audio, source fps) -> SaveVideo

Every choice is the artist's: detail 0.00-1.00, model x2/x4 (auto: x4 up to 576 px high,
else x2), output size (auto, native, 1080p, 1440p, 2160p, custom W x H), downscale filter
(every ImageScale method), seed and the IN..OUT frames.  Steps 1 / cfg 1 are the node's
contract ("sample with 1 step at cfg 1") and are not settings.  A job whose decoded frames
(fp32, frames x W x H x 12 bytes) would not fit in ComfyUI's free RAM above a floor is
refused before the ledger with ``vsr_memory`` and the fix.

Standalone module (no package-relative imports), loaded by path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import subprocess
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Mapping

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env

TARGET_ID = "relayvsr-video-upscale-v1"
PROVIDER_ID = "comfyui-local"
_PINS = {"manifest_sha256": "98069892c6b04d79d7e91497cb589a1b4ee23743fbe732c11bb307f536b897dd", "api_sha256": "47444ecc2521288789c285a9badb9eb5cbce5d6e273994e9037ded9830c082c2", "node_count": 12}
MODELS = {
    "x2": {"unet": "relayvsr_x2_lightx2v_int8_convrot.safetensors", "vae": "relayvsr_flash_vae_x2_fp32.safetensors", "factor": 2},
    "x4": {"unet": "relayvsr_x4_int8_convrot.safetensors", "vae": "relayvsr_flash_vae_x4_fp32.safetensors", "factor": 4},
}
AUTO_X4_MAX_HEIGHT = 576  # H3's 480p canvas is 960x544 (multiple of 32): it gets x4, a 720p/768p take x2
SIZES = ("auto", "native", "1080p", "1440p", "2160p", "custom")
SIZE_HEIGHTS = {"1080p": 1080, "1440p": 1440, "2160p": 2160}
FILTERS = ("lanczos", "bicubic", "bilinear", "area", "nearest-exact")
DEFAULTS = {"model": "auto", "detail": 0.5, "size": "auto", "filter": "lanczos", "seed": 0}
SEED_MAX = 4_294_967_295
SIDE_MIN, SIDE_MAX = 64, 8192
BYTES_PER_PIXEL = 12  # VSRDecode output: fp32 RGB on the intermediate device
RAM_FLOOR = 4 * 1024 ** 3
SETTING_KEYS = frozenset({"model", "detail", "size", "width", "height", "filter", "seed", "in_frame", "out_frame"})
# Graph nodes the compile writes or removes.
_LOAD, _COMP, _CUT, _CUT_AUDIO, _UNET, _COND, _SAMPLER, _VAE, _DECODE, _SCALE, _VIDEO, _SAVE = (
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12")
_UPLOAD = re.compile(r"^filmsuite-vsr-[0-9a-f]{32}\.mp4 \[temp\]$")
_TAKE_ID = re.compile(r"^studio:[0-9a-f]{32}$")


class VideoUpscaleError(ValueError):
    """Refused before any side effect; ``reason_code`` says why."""

    def __init__(self, message: str, reason_code: str = "invalid_request") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class UpscaleProfile:
    __slots__ = ("target_id", "_manifest_json", "_graph_json")

    def __init__(self, manifest: Mapping[str, Any], graph: Mapping[str, Any]) -> None:
        self.target_id = TARGET_ID
        self._manifest_json = json.dumps(manifest, ensure_ascii=False)
        self._graph_json = json.dumps(graph, ensure_ascii=False)

    @property
    def id(self) -> str:
        return self.target_id

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    @property
    def graph(self) -> dict[str, Any]:
        return json.loads(self._graph_json)


def _resolve(graph: Any, pointer: str) -> Any:
    node = graph
    for part in pointer.strip("/").split("/"):
        if not isinstance(node, dict) or part not in node:
            raise VideoUpscaleError(f"graph is missing pointer {pointer}", "workflow_drift")
        node = node[part]
    return node


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int


def validate_pinned_graph(graph: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    """Fixed assertions, model pointers (one of the pinned pairs), declared pointers, classes, links."""
    for pointer, expected in manifest["fixed_assertions"].items():
        actual = _resolve(graph, pointer)
        if actual != expected or type(actual) is not type(expected):
            raise VideoUpscaleError(f"fixed assertion differs at {pointer}", "workflow_drift")
    allowed: dict[str, set[str]] = {}
    for model in list(manifest["models"].values()) + [m for pair in manifest["model_choices"].values() for m in pair.values()]:
        allowed.setdefault(model["assertion_pointer"], set()).add(model["filename"])
    for pointer, names in allowed.items():
        if _resolve(graph, pointer) not in names:
            raise VideoUpscaleError(f"model at {pointer} is not a pinned file", "workflow_drift")
    for pointer in manifest["mutable_json_pointers"]:
        if _is_link(_resolve(graph, pointer)):
            raise VideoUpscaleError(f"mutable pointer {pointer} names a link", "workflow_drift")
    classes = {node["class_type"] for node in graph.values()}
    if not classes <= set(manifest["required_classes"]):
        raise VideoUpscaleError("graph classes differ from required_classes", "workflow_drift")
    for node_id, node in graph.items():
        for value in node["inputs"].values():
            if _is_link(value) and value[0] not in graph:
                raise VideoUpscaleError(f"node {node_id} links to a missing node", "workflow_drift")


def load_profile(workflow_root: Path | str, *, pins: Mapping[str, Any] | None = None) -> UpscaleProfile:
    pins = pins or _PINS
    directory = Path(workflow_root) / TARGET_ID
    manifest_raw = (directory / "manifest.json").read_bytes()
    api_raw = (directory / "workflow-api.json").read_bytes()
    if hashlib.sha256(manifest_raw).hexdigest() != pins["manifest_sha256"]:
        raise VideoUpscaleError("workflow manifest bytes do not match the pinned SHA-256", "workflow_drift")
    if hashlib.sha256(api_raw).hexdigest() != pins["api_sha256"]:
        raise VideoUpscaleError("workflow API bytes do not match the pinned SHA-256", "workflow_drift")
    manifest, graph = json.loads(manifest_raw), json.loads(api_raw)
    if manifest.get("id") != TARGET_ID or manifest.get("provider_id") != PROVIDER_ID:
        raise VideoUpscaleError("workflow manifest names another target", "workflow_drift")
    if manifest["workflow"]["api_sha256"] != pins["api_sha256"] or len(graph) != pins["node_count"]:
        raise VideoUpscaleError("workflow manifest declares another graph", "workflow_drift")
    if set(graph) != {_LOAD, _COMP, _CUT, _CUT_AUDIO, _UNET, _COND, _SAMPLER, _VAE, _DECODE, _SCALE, _VIDEO, _SAVE}:
        raise VideoUpscaleError("workflow node ids differ from the pin", "workflow_drift")
    validate_pinned_graph(graph, manifest)
    return UpscaleProfile(manifest, graph)


# --------------------------------------------------------------------------- settings


def settings_options() -> dict[str, Any]:
    """What the artist can choose (the page and the agent read the same answer)."""
    return {
        "model": ["auto", "x2", "x4"],
        "detail": {"min": 0.0, "max": 1.0, "step": 0.01},
        "size": list(SIZES),
        "custom": {"min": SIDE_MIN, "max": SIDE_MAX, "even": True},
        "filter": list(FILTERS),
        "seed": {"min": 0, "max": SEED_MAX},
        "auto_rule": {"x4_max_height": AUTO_X4_MAX_HEIGHT, "x4_size": "1080p", "x2_size": "native"},
    }


def _int(value: Any, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise VideoUpscaleError(f"{name} must be a whole number from {low} to {high}")
    return value


def normalize_settings(settings: Any, remembered: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Defaults, then the project's remembered choice, then what this request says; exact allowlist."""
    if not isinstance(settings, Mapping):
        raise VideoUpscaleError("settings must be an object")
    unknown = set(settings) - SETTING_KEYS
    if unknown:
        raise VideoUpscaleError(f"unknown setting {sorted(unknown)[0]}")
    base = {**DEFAULTS}
    for key in ("model", "detail", "size", "filter", "seed", "width", "height"):
        if isinstance(remembered, Mapping) and key in remembered and key not in settings:
            base[key] = remembered[key]
    merged = {**base, **{k: v for k, v in settings.items()}}
    if merged.get("size") != "custom":
        if "width" in settings or "height" in settings:
            raise VideoUpscaleError("width and height are for a custom size")
        merged.pop("width", None)
        merged.pop("height", None)
    out: dict[str, Any] = {}
    if merged["model"] not in ("auto", "x2", "x4"):
        raise VideoUpscaleError("model is auto, x2 or x4")
    out["model"] = merged["model"]
    detail = merged["detail"]
    if type(detail) not in (int, float) or not math.isfinite(detail) or not 0.0 <= detail <= 1.0:
        raise VideoUpscaleError("detail is a number from 0 to 1")
    out["detail"] = round(float(detail), 4)
    if merged["size"] not in SIZES:
        raise VideoUpscaleError("size is auto, native, 1080p, 1440p, 2160p or custom")
    out["size"] = merged["size"]
    if out["size"] == "custom":
        if "width" not in merged or "height" not in merged:
            raise VideoUpscaleError("a custom size needs width and height")
        for key in ("width", "height"):
            value = _int(merged[key], key, SIDE_MIN, SIDE_MAX)
            if value % 2:
                raise VideoUpscaleError(f"{key} must be even (the video encoder needs it)")
            out[key] = value
    if merged["filter"] not in FILTERS:
        raise VideoUpscaleError("filter is " + ", ".join(FILTERS))
    out["filter"] = merged["filter"]
    out["seed"] = _int(merged["seed"], "seed", 0, SEED_MAX)
    if ("in_frame" in merged) != ("out_frame" in merged):
        raise VideoUpscaleError("IN and OUT go together")
    if "in_frame" in merged:
        out["in_frame"] = _int(merged["in_frame"], "in_frame", 0, 1_000_000)
        out["out_frame"] = _int(merged["out_frame"], "out_frame", 0, 1_000_000)
        if out["out_frame"] < out["in_frame"]:
            raise VideoUpscaleError("OUT is before IN")
    return out


def remembered_part(settings: Mapping[str, Any]) -> dict[str, Any]:
    """What the project remembers (never the take's own IN..OUT)."""
    return {k: settings[k] for k in ("model", "detail", "size", "width", "height", "filter", "seed") if k in settings}


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def plan(settings: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    """The concrete job for this source: model, sizes, frames, memory need."""
    width, height = int(source["width"]), int(source["height"])
    frames_total, fps = int(source["frames"]), float(source["fps"])
    if width < 16 or height < 16 or frames_total < 1 or not fps > 0:
        raise VideoUpscaleError("the take could not be read as a video", "invalid_request")
    model = settings["model"]
    if model == "auto":
        model = "x4" if height <= AUTO_X4_MAX_HEIGHT else "x2"
    factor = MODELS[model]["factor"]
    native = {"width": width * factor, "height": height * factor}
    size = settings["size"]
    if size == "auto":
        size = "1080p" if model == "x4" else "native"
    if size == "native":
        canvas = dict(native)
    elif size == "custom":
        canvas = {"width": settings["width"], "height": settings["height"]}
    else:
        h = SIZE_HEIGHTS[size]
        canvas = {"width": _even(h * width / height), "height": h}
    if not (SIDE_MIN <= canvas["width"] <= SIDE_MAX and SIDE_MIN <= canvas["height"] <= SIDE_MAX):
        raise VideoUpscaleError(f"the output {canvas['width']}x{canvas['height']} is outside {SIDE_MIN}-{SIDE_MAX} px")
    first, last = 0, frames_total - 1
    if "in_frame" in settings:
        first, last = settings["in_frame"], settings["out_frame"]
        if last >= frames_total:
            raise VideoUpscaleError(f"OUT {last} is past the take's last frame {frames_total - 1}")
    frames = last - first + 1
    return {
        "model": model, "factor": factor, "unet": MODELS[model]["unet"], "vae": MODELS[model]["vae"],
        "detail": settings["detail"], "size": size, "filter": settings["filter"], "seed": settings["seed"],
        "native": native, "canvas": canvas, "scaled": canvas != native,
        "in_frame": first, "out_frame": last, "frames": frames, "fps": fps, "audio": bool(source.get("audio")),
        "source_canvas": {"width": width, "height": height}, "source_frames": frames_total,
        "memory_bytes": frames * native["width"] * native["height"] * BYTES_PER_PIXEL,
    }


def _gb(value: float) -> str:
    return f"{value / 1024 ** 3:.1f} GB"


def check_memory(job: Mapping[str, Any], *, ram_free: int | None, floor: int = RAM_FLOOR) -> None:
    """Refuse a job whose decoded frames would not fit in the engine's free RAM above the floor."""
    if ram_free is None:
        return
    room = int(ram_free) - floor
    if job["memory_bytes"] > room:
        fits = "x2 or a shorter IN–OUT fits" if job["model"] == "x4" else "a shorter IN–OUT fits"
        raise VideoUpscaleError(
            f"This upscale needs {_gb(job['memory_bytes'])} of memory for {job['frames']} frames at "
            f"{job['native']['width']}x{job['native']['height']}; {_gb(max(room, 0))} is free above the "
            f"{_gb(floor)} reserve ({fits})", "vsr_memory")


def upload_name(source_sha256: str) -> str:
    return f"filmsuite-vsr-{source_sha256[:32]}.mp4 [temp]"


def compile_request(*, graph: Mapping[str, Any], manifest: Mapping[str, Any], plan: Mapping[str, Any], upload: str) -> dict[str, Any]:
    """Clone the pinned graph and write every setting into its declared pointer."""
    validate_pinned_graph(graph, manifest)
    if type(upload) is not str or not _UPLOAD.match(upload):
        raise VideoUpscaleError("the uploaded take does not match the request")
    g = copy.deepcopy(dict(graph))
    g[_LOAD]["inputs"]["file"] = upload
    g[_CUT]["inputs"]["batch_index"] = int(plan["in_frame"])
    g[_CUT]["inputs"]["length"] = int(plan["frames"])
    g[_UNET]["inputs"]["unet_name"] = plan["unet"]
    g[_VAE]["inputs"]["vae_name"] = plan["vae"]
    g[_SAMPLER]["inputs"]["seed"] = int(plan["seed"])
    g[_DECODE]["inputs"]["detail"] = float(plan["detail"])
    if plan["scaled"]:
        g[_SCALE]["inputs"].update(upscale_method=plan["filter"], width=int(plan["canvas"]["width"]), height=int(plan["canvas"]["height"]))
    else:
        g.pop(_SCALE)
        g[_VIDEO]["inputs"]["images"] = [_DECODE, 0]
    if plan["audio"]:
        g[_CUT_AUDIO]["inputs"]["start_index"] = round(plan["in_frame"] / plan["fps"], 6)
        g[_CUT_AUDIO]["inputs"]["duration"] = round(plan["frames"] / plan["fps"], 6)
    else:
        g.pop(_CUT_AUDIO)
        g[_VIDEO]["inputs"].pop("audio")
    required = set(manifest["required_classes"])
    for node_id, node in g.items():
        if node["class_type"] not in required:
            raise VideoUpscaleError(f"node {node_id} class is not pinned")
        for value in node["inputs"].values():
            if _is_link(value) and value[0] not in g:
                raise VideoUpscaleError(f"node {node_id} input points at a removed node")
            if isinstance(value, str) and value.startswith("__"):
                raise VideoUpscaleError(f"node {node_id} still carries a placeholder")
    return g


def take_facts(plan: Mapping[str, Any], source_job: str, source_sha256: str) -> dict[str, Any]:
    """The new take's link to its source and what was done (take facts ``upscale``)."""
    return {"relation": "upscales", "source_job": source_job, "source": source_sha256, "model": plan["model"],
            "detail": plan["detail"], "size": plan["size"], "filter": plan["filter"] if plan["scaled"] else None,
            "seed": plan["seed"], "canvas": dict(plan["canvas"]), "frames": plan["frames"], "fps": plan["fps"],
            "in_frame": plan["in_frame"], "out_frame": plan["out_frame"],
            "source_canvas": dict(plan["source_canvas"]), "source_frames": plan["source_frames"]}


def identity_args(source_job: str, source_sha256: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    if not _TAKE_ID.match(source_job):
        raise VideoUpscaleError("take must be a finished take's studio job id")
    return {"take": source_job, "sha256": source_sha256, **dict(settings)}


# --------------------------------------------------------------------------- source take


STATE_FILE = ("state", "video-upscale.json")
STATE_SCHEMA = "studio.video-upscale-settings/1"
# w39-p2-tester item 4: the one configured ComfyUI address (generation/comfy_endpoint.py, loaded by path).
import importlib.util as _endpoint_util
from pathlib import Path as _EndpointPath
_endpoint_spec = _endpoint_util.spec_from_file_location("film_suite_comfy_endpoint", _EndpointPath(__file__).resolve().parents[1] / "generation" / "comfy_endpoint.py")
_endpoint = _endpoint_util.module_from_spec(_endpoint_spec)
_endpoint_spec.loader.exec_module(_endpoint)
COMFY_ORIGIN = _endpoint.DEFAULT_ORIGIN  # the default; upload_temp reads the configured address
UPLOAD_MAX_BYTES = 2 * 1024 ** 3


def probe(path: str | Path, ffprobe: str | Path) -> dict[str, Any]:
    """Width, height, decoded frame count, fps and whether the take has sound (ffprobe, counted packets)."""
    result = subprocess.run([str(ffprobe), "-v", "error", "-count_packets", "-show_entries",
                             "stream=codec_type,width,height,avg_frame_rate,r_frame_rate,nb_read_packets", "-of", "json", str(path)],
                            capture_output=True, text=True, timeout=120, check=False, env=child_env())
    try:
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except ValueError:
        streams = []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if result.returncode != 0 or video is None:
        raise VideoUpscaleError("the take could not be read as a video")
    rate = str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")
    num, _, den = rate.partition("/")
    try:
        fps = float(num) / float(den or 1)
        width, height, frames = int(video["width"]), int(video["height"]), int(video["nb_read_packets"])
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        raise VideoUpscaleError("the take could not be read as a video") from None
    return {"width": width, "height": height, "frames": frames, "fps": round(fps, 6),
            "audio": any(s.get("codec_type") == "audio" for s in streams)}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def upload_temp(data: bytes, name: str, *, origin: str | None = None, opener: Any = None) -> str:
    origin = origin or _endpoint.comfy_origin()
    """Upload the take byte-exact to ComfyUI temp/ under its content name; return the LoadVideo value."""
    if not _UPLOAD.match(name):
        raise VideoUpscaleError("the upload name does not match the take")
    if len(data) > UPLOAD_MAX_BYTES:
        raise VideoUpscaleError("the take is too large to upload")
    filename = name[: -len(" [temp]")]
    boundary = "----filmsuite" + uuid.uuid4().hex
    crlf = "\r\n"
    head = "".join(f'--{boundary}{crlf}Content-Disposition: form-data; name="{k}"{crlf}{crlf}{v}{crlf}' for k, v in (("type", "temp"), ("overwrite", "true")))
    head += f'--{boundary}{crlf}Content-Disposition: form-data; name="image"; filename="{filename}"{crlf}Content-Type: video/mp4{crlf}{crlf}'
    body = head.encode() + data + f"{crlf}--{boundary}--{crlf}".encode()
    request = urllib.request.Request(origin + "/upload/image", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with (opener or _endpoint._opener.open)(request, timeout=300) as response:
        answer = json.loads(response.read(1 << 20).decode("utf-8"))
    if not isinstance(answer, dict) or answer.get("name") != filename or (answer.get("subfolder") or "") != "" or answer.get("type") != "temp":
        raise VideoUpscaleError("ComfyUI did not keep the take under its name", "unreachable")
    return name


def read_remembered(project_directory: str | Path) -> dict[str, Any]:
    """The project's remembered choice (empty when none or unreadable)."""
    path = Path(project_directory).joinpath(*STATE_FILE)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict) or payload.get("schema") != STATE_SCHEMA or not isinstance(payload.get("settings"), dict):
        return {}
    try:
        return remembered_part(normalize_settings({}, payload["settings"]))
    except VideoUpscaleError:
        return {}


def write_remembered(project_directory: str | Path, settings: Mapping[str, Any]) -> dict[str, Any]:
    remembered = remembered_part(settings)
    path = Path(project_directory).joinpath(*STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps({"schema": STATE_SCHEMA, "settings": remembered}, indent=1), encoding="utf-8")
    temporary.replace(path)
    return remembered


__all__ = ["DEFAULTS", "MODELS", "TARGET_ID", "UpscaleProfile", "VideoUpscaleError", "check_memory", "compile_request",
           "identity_args", "load_profile", "normalize_settings", "plan", "remembered_part", "settings_options",
           "take_facts", "upload_name", "validate_pinned_graph", "probe", "sha256_file", "upload_temp", "read_remembered",
           "write_remembered"]
