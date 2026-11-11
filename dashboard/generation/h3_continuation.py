"""MiniMax H3 continuation targets on OBVPM sidecars (lane fs-obvb, fs-obvpm RESULT §3).

``minimax-h3-extend-mctx-v1``   continue after a take (its tail pinned ``masked``)
``minimax-h3-prepend-mctx-v1``  lead into a take (its head pinned ``both``)
``minimax-h3-bridge-mctx-v1``   fill between two takes (source tail ``masked``, target head ``both``)

A continuation never fakes: every source must be a take pair whose sidecar
pairs with its mp4 (``h3_mctx.verify_pair``); otherwise the request is refused
before the ledger with the readable reason.  The node pack lists clips under
``ComfyUI/output`` only, so the job stages each source pair byte-exact into
``output/studio-h3/staged/`` right before ``/prompt`` and removes the staged
copies once the job is over.  Lengths sit on the shared AV grid: raw frames
``39 + 51k`` (masked windows of 39 frames = 12 video / 65 audio steps).

Standalone module (no package-relative imports, no dataclasses); the sidecar
reader is loaded by path.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

EXTEND_TARGET_ID = "minimax-h3-extend-mctx-v1"
PREPEND_TARGET_ID = "minimax-h3-prepend-mctx-v1"
BRIDGE_TARGET_ID = "minimax-h3-bridge-mctx-v1"
ROLES = {EXTEND_TARGET_ID: "extend", PREPEND_TARGET_ID: "prepend", BRIDGE_TARGET_ID: "bridge"}
TARGET_IDS = tuple(ROLES)
PROVIDER_ID = "comfyui-local"
# w39-p2-tester item 4: the one configured ComfyUI address (generation/comfy_endpoint.py, loaded by path).
import importlib.util as _endpoint_util
from pathlib import Path as _EndpointPath
_endpoint_spec = _endpoint_util.spec_from_file_location("film_suite_comfy_endpoint", _EndpointPath(__file__).resolve().parents[1] / "generation" / "comfy_endpoint.py")
_endpoint = _endpoint_util.module_from_spec(_endpoint_spec)
_endpoint_spec.loader.exec_module(_endpoint)
COMFY_ORIGIN = _endpoint.DEFAULT_ORIGIN  # the default; every request reads _endpoint.comfy_origin()
FOLDER = "studio-h3"
STAGED = FOLDER + "/staged"
PROMPT_SENTINEL = "__STUDIO_PROMPT__"
WINDOW, GRID, GROUP = 39, 51, 17
MAX_RAW_FRAMES = 396
SAMPLERS = ("res_multistep", "er_sde", "euler")
SCHEDULERS = ("simple", "beta")
SCALAR_KEYS = frozenset({"prompt", "duration_seconds", "seed", "steps", "sampler_name", "scheduler"})
MEDIA_KEYS = {EXTEND_TARGET_ID: ("source",), PREPEND_TARGET_ID: ("source",), BRIDGE_TARGET_ID: ("source", "target")}
MAX_SIDECAR_BYTES = 96 * 1024 * 1024  # ComfyUI's default upload limit is 100 MB
MAX_TAKE_BYTES = 512 * 1024 * 1024
NOT_CONTINUABLE = "This take was made before continuation existed"
# Lane p7-vid (VID-097): Continue from an artist-chosen frame of the take (the OUT frame of the IN window).
SOURCE_FRAME_ROLES = ("extend", "prepend")
SOURCE_FRAME_OUT_OF_RANGE = "source_frame_out_of_range"
SOURCE_FRAME_MESSAGE = "The chosen frame leaves no room for the held window of this take"
DEFAULT_FPS = 24  # an H3 take's rate when its sidecar names none (lane p10-vid)

_PINS = {
    EXTEND_TARGET_ID: {"manifest_sha256": "d13c5e4ec11e69d6556ba0b91b20df24ed720439d3e65fc84e0bf942f6b39ad9",
                       "api_sha256": "30877f4c54e2711252502d814040183f6eb668fd4dd051d410315f335a2d02b6", "node_count": 19},
    PREPEND_TARGET_ID: {"manifest_sha256": "ffd596ff1552fc1f26d88bbf6d0a988cf3688782b4a7492cbf0542fbe39d5f18",
                        "api_sha256": "025f14655e40cb95544b74df5510d221462096163f34c1815908503408979319", "node_count": 19},
    BRIDGE_TARGET_ID: {"manifest_sha256": "a18a98c65cf6a327b8d5b332e36ab7cbdbcec4d6530e7b5c8df79f55e9858f40",
                       "api_sha256": "e1ad1eb33f77fc2e8fe6cdd901bc679a336451e468971c5ab25263c9e9fa30e7", "node_count": 21},
}
# Graph nodes the compile writes (declared in each manifest's mutable_json_pointers).
_GEN, _SEED, _SAMPLER, _SCHEDULER = "8", "9", "11", "12"
_LOAD_SOURCE, _LOAD_TARGET = "40", "41"
_SPEC_LAST, _SPEC_BRIDGE_SOURCE = "50", "49"


class H3ContinuationError(ValueError):
    """Refused before any side effect; ``reason_code`` says why."""

    def __init__(self, message: str, reason_code: str = "invalid_request") -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _mctx_module() -> Any:
    spec = importlib.util.spec_from_file_location("film_suite_h3_mctx", Path(__file__).with_name("h3_mctx.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mctx = _mctx_module()


# --------------------------------------------------------------------------- profiles


class ContinuationProfile:
    """Validated pinned profile; every read returns a fresh copy."""

    __slots__ = ("target_id", "_manifest_json", "_graph_json")

    def __init__(self, target_id: str, manifest: Mapping[str, Any], graph: Mapping[str, Any]) -> None:
        self.target_id = target_id
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
            raise H3ContinuationError(f"graph is missing pointer {pointer}", "workflow_drift")
        node = node[part]
    return node


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int


def validate_pinned_graph(graph: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    for pointer, expected in manifest["fixed_assertions"].items():
        actual = _resolve(graph, pointer)
        if actual != expected or type(actual) is not type(expected):
            raise H3ContinuationError(f"fixed assertion differs at {pointer}", "workflow_drift")
    for name, model in manifest["models"].items():
        if _resolve(graph, model["assertion_pointer"]) != model["filename"]:
            raise H3ContinuationError(f"model {name} differs from its pinned file", "workflow_drift")
    for pointer in manifest["mutable_json_pointers"]:
        if _is_link(_resolve(graph, pointer)):
            raise H3ContinuationError(f"mutable pointer {pointer} names a link", "workflow_drift")
    if sorted({node["class_type"] for node in graph.values()}) != sorted(manifest["required_classes"]):
        raise H3ContinuationError("graph classes differ from required_classes", "workflow_drift")
    for node_id, node in graph.items():
        for value in node["inputs"].values():
            if _is_link(value) and value[0] not in graph:
                raise H3ContinuationError(f"node {node_id} links to a missing node", "workflow_drift")


def load_profile(target_id: str, workflow_root: Path | str) -> ContinuationProfile:
    if target_id not in _PINS:
        raise H3ContinuationError("unknown H3 continuation target")
    pins = _PINS[target_id]
    directory = Path(workflow_root) / target_id
    manifest_raw = (directory / "manifest.json").read_bytes()
    api_raw = (directory / "workflow-api.json").read_bytes()
    if hashlib.sha256(manifest_raw).hexdigest() != pins["manifest_sha256"]:
        raise H3ContinuationError("workflow manifest bytes do not match the pinned SHA-256", "workflow_drift")
    if hashlib.sha256(api_raw).hexdigest() != pins["api_sha256"]:
        raise H3ContinuationError("workflow API bytes do not match the pinned SHA-256", "workflow_drift")
    manifest, graph = json.loads(manifest_raw), json.loads(api_raw)
    if manifest.get("id") != target_id or manifest.get("provider_id") != PROVIDER_ID:
        raise H3ContinuationError("workflow manifest names another target", "workflow_drift")
    if manifest["workflow"]["api_sha256"] != pins["api_sha256"] or len(graph) != pins["node_count"]:
        raise H3ContinuationError("workflow manifest declares another graph", "workflow_drift")
    validate_pinned_graph(graph, manifest)
    return ContinuationProfile(target_id, manifest, graph)


# --------------------------------------------------------------------------- frames and windows


def raw_frames(role: str, duration_seconds: int) -> int:
    """Raw frames of the new take on the AV grid ``39 + 51k``.

    Extend / prepend: ``51k`` new frames beside the 39 held ones (6 s -> 192
    raw).  Bridge: the gap is ``51k - 39`` new frames between two held windows
    (k >= 2; 3 s -> 141 raw, a 63-frame gap).
    """
    wanted = duration_seconds * 24 + (WINDOW if role == "bridge" else 0)
    k = max(2 if role == "bridge" else 1, round(wanted / GRID))
    while WINDOW + GRID * k > MAX_RAW_FRAMES:
        k -= 1
    return WINDOW + GRID * k


def new_frames(role: str, raw: int) -> int:
    return raw - (2 * WINDOW if role == "bridge" else WINDOW)


def window_for(facts: Mapping[str, Any], side: str, source_frame: int | None = None) -> tuple[str, int]:
    """PinSpec ``take_from`` / ``take_from_frame`` for a take's ``tail`` or ``head``.

    The same latent-grade shift the pack's convenience pins apply: a
    continuation take's edge can sit off the 17-frame grid (its pinned head
    or tail moved it), and the explicit PinSpec refuses off-grid cuts.

    Lane p7-vid (VID-097): ``source_frame`` is the take's frame the artist
    picked (0-based).  Extend: the new take continues AFTER it, so the held
    window ends just past it, snapped DOWN onto the grid.  Prepend: the new
    take leads INTO it, so the held window starts at it, snapped UP.  A frame
    that leaves no whole window inside the take is refused with
    ``source_frame_out_of_range``.  The take file is never trimmed.
    """
    head = int(facts.get("pinned_head_frames", 0))
    tail = int(facts.get("pinned_tail_frames", 0))
    delivered = int(facts.get("delivered_frames", 0))
    if source_frame is not None:
        try:
            return _window_at(head, tail, delivered, side, source_frame)
        except H3ContinuationError as error:
            raise _bound_refusal(error, head, tail, delivered, side, facts.get("fps")) from None
    if side == "tail":
        end = delivered
        off = (head + end - WINDOW) % GROUP
        if not off:
            return "tail", 0
        end -= off
        if head + end < WINDOW:
            raise H3ContinuationError("this take is too short to continue")
        return "at_frame", end
    end = WINDOW
    off = (head + end - WINDOW) % GROUP
    if not off:
        return "head", 0
    end += GROUP - off
    if end > delivered + tail:
        raise H3ContinuationError("this take is too short to lead into")
    return "at_frame", end


def _window_at(head: int, tail: int, delivered: int, side: str, source_frame: int) -> tuple[str, int]:
    refuse = H3ContinuationError(SOURCE_FRAME_MESSAGE, SOURCE_FRAME_OUT_OF_RANGE)
    if type(source_frame) is not int or not 0 <= source_frame < delivered:
        raise refuse
    if side == "tail":
        end = source_frame + 1
        end -= (head + end - WINDOW) % GROUP
        if end < WINDOW or head + end < WINDOW:
            raise refuse
        return ("tail", 0) if end == delivered else ("at_frame", end)
    end = source_frame + WINDOW
    end += (GROUP - (head + end - WINDOW) % GROUP) % GROUP
    if end > delivered + tail:
        raise refuse
    return ("head", 0) if end == WINDOW else ("at_frame", end)


def window_bound(head: int, tail: int, delivered: int, side: str) -> int | None:
    """Lane p10-vid (VID-116): the bound OUT may sit at, the page's ``windowBound`` (window-rule.js): Extend = the
    earliest frame ``_window_at`` accepts, Prepend = the latest; None = no frame leaves room."""
    frames = range(delivered) if side == "tail" else range(delivered - 1, -1, -1)
    for frame in frames:
        try:
            _window_at(head, tail, delivered, side, frame)
        except H3ContinuationError:
            continue
        return frame
    return None


def frame_timecode(frame: int, fps: Any) -> str:
    """SMPTE ``HH:MM:SS:FF`` of a take frame, the page's ``formatFrameTimecode`` (rate rounded, never below 1)."""
    try:
        rate = max(1, round(float(fps)))
    except (TypeError, ValueError):
        rate = 1
    seconds = frame // rate
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}:{frame % rate:02d}"


def _bound_refusal(error: H3ContinuationError, head: int, tail: int, delivered: int, side: str, fps: Any) -> H3ContinuationError:
    """A refused OUT names its bound as frame and timecode of the take ("Extend needs OUT at frame 54 (00:00:02:06)
    or later"), the same fact the page shows on its disabled Generate; the reason code stays."""
    if error.reason_code != SOURCE_FRAME_OUT_OF_RANGE:
        return error
    bound = window_bound(head, tail, delivered, side)
    if bound is None:
        return error
    rate = fps if type(fps) in (int, float) and fps > 0 else DEFAULT_FPS
    what, way = ("Extend", "later") if side == "tail" else ("Prepend", "earlier")
    return H3ContinuationError(f"{what} needs OUT at frame {bound} ({frame_timecode(bound, rate)}) or {way}",
                               SOURCE_FRAME_OUT_OF_RANGE)


def used_source_frame(facts: Mapping[str, Any], side: str, source_frame: int | None = None) -> int:
    """The take's frame the continuation really joins at (after the grid snap): extend = the last frame the
    parent plays, prepend = the first frame the parent resumes at."""
    take_from, frame = window_for(facts, side, source_frame)
    delivered = int(facts.get("delivered_frames", 0))
    if side == "tail":
        return (delivered if take_from == "tail" else frame) - 1
    return 0 if take_from == "head" else frame - WINDOW


# --------------------------------------------------------------------------- request validation


def _media_path(value: Any, field_name: str) -> str:
    if type(value) is not str or not value.strip() or not value.lower().endswith(".mp4"):
        raise H3ContinuationError(f"{field_name} must name a take (.mp4)")
    return value


def validate_args(target_id: str, args: Mapping[str, Any]) -> dict[str, Any]:
    if target_id not in ROLES:
        raise H3ContinuationError("unknown H3 continuation target")
    if not isinstance(args, Mapping):
        raise H3ContinuationError("args must be an object")
    role = ROLES[target_id]
    allowed = SCALAR_KEYS | set(MEDIA_KEYS[target_id])
    optional = {"source_frame"} if role in SOURCE_FRAME_ROLES else set()
    names = set(args)
    if names - allowed - optional:
        raise H3ContinuationError("args contain an unknown field: " + ", ".join(sorted(map(str, names - allowed - optional))))
    source_frame = args.get("source_frame")
    if "source_frame" in names and (type(source_frame) is not int or source_frame < 0):
        raise H3ContinuationError("source_frame must be a whole number from 0", SOURCE_FRAME_OUT_OF_RANGE)
    missing = allowed - names
    if missing:
        raise H3ContinuationError("args are missing: " + ", ".join(sorted(missing)))
    prompt = args["prompt"]
    if type(prompt) is not str or not prompt.strip() or PROMPT_SENTINEL in prompt:
        raise H3ContinuationError("prompt must contain text")
    duration = args["duration_seconds"]
    low = 2 if role == "bridge" else 2
    if type(duration) is not int or not low <= duration <= 14:
        raise H3ContinuationError("duration_seconds must be a whole number from 2 to 14")
    seed = args["seed"]
    if type(seed) is not int or not 0 <= seed <= 4294967295:
        raise H3ContinuationError("seed must be a whole number from 0 to 4294967295")
    steps = args["steps"]
    if type(steps) is not int or not 1 <= steps <= 100:
        raise H3ContinuationError("steps must be a whole number from 1 to 100")
    if type(args["sampler_name"]) is not str or args["sampler_name"] not in SAMPLERS:
        raise H3ContinuationError("sampler_name is not an approved preset")
    if type(args["scheduler"]) is not str or args["scheduler"] not in SCHEDULERS:
        raise H3ContinuationError("scheduler is not an approved preset")
    raw = raw_frames(role, duration)
    plan = {
        "target_id": target_id, "role": role, "prompt": prompt, "duration_seconds": duration, "seed": seed,
        "steps": steps, "sampler_name": args["sampler_name"], "scheduler": args["scheduler"],
        "raw_frames": raw, "new_frames": new_frames(role, raw),
        "source": _media_path(args["source"], "source"),
        "target": _media_path(args["target"], "target") if role == "bridge" else None,
        "source_frame": source_frame if "source_frame" in names else None,
    }
    return plan


def media_items(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = [{"key": "source/0", "role": "source", "kind": "take", "source": plan["source"]}]
    if plan.get("target"):
        items.append({"key": "target/0", "role": "target", "kind": "take", "source": plan["target"]})
    return items


def identity_args(args: Mapping[str, Any], resolved: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Durable identity: each take becomes ``{path: HMAC, sha256: content}``."""
    result = {key: value for key, value in args.items() if key not in ("source", "target")}
    for item in resolved:
        result[item["role"]] = {"path": item["path_hmac"], "sha256": item["source_sha256"]}
    return result


def check_pairs(plan: Mapping[str, Any], resolved: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Each resolved take must be a verified pair; a bridge needs one canvas.  Returns the facts."""
    facts = []
    for item in resolved:
        path = Path(item["path"])
        try:
            if path.stat().st_size > MAX_TAKE_BYTES:
                raise H3ContinuationError("the take is too large to continue")
            if mctx.sidecar_path(path).stat().st_size > MAX_SIDECAR_BYTES:
                raise H3ContinuationError("the take's continuation data is too large to send", "invalid_request")
        except FileNotFoundError:
            raise H3ContinuationError(NOT_CONTINUABLE, "no_sidecar") from None
        except OSError:
            raise H3ContinuationError("the take cannot be read") from None
        try:
            pair = mctx.verify_pair(path)
        except mctx.MctxError as exc:
            raise H3ContinuationError(str(exc), exc.reason_code) from None
        if pair["self_id"] != item["source_sha256"]:
            raise H3ContinuationError("the take changed after it was checked", "pair_broken")
        facts.append({**pair, "role": item["role"]})
    if len(facts) == 2:
        if (facts[0]["width"], facts[0]["height"]) != (facts[1]["width"], facts[1]["height"]):
            raise H3ContinuationError("both takes must have the same picture size")
        if facts[0]["self_id"] == facts[1]["self_id"]:
            raise H3ContinuationError("both sides are the same take; a bridge needs two different takes")
    for side, entry in zip(("tail", "head"), facts if len(facts) == 2 else facts * 2):
        if plan["role"] == "prepend" and side == "tail":
            continue
        if plan["role"] == "extend" and side == "head":
            continue
        window_for(entry, side, plan.get("source_frame"))  # raises when the take is too short / the frame does not fit
    return facts


# --------------------------------------------------------------------------- staging


def staged_names(studio_job_id: str, count: int) -> list[str]:
    tag = hashlib.sha256(studio_job_id.encode("utf-8")).hexdigest()[:16]
    return [f"{tag}_{index}" for index in range(count)]


def _multipart(fields: Mapping[str, str], filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----studio" + uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
             for name, value in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\n'
                 f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def upload_output(data: bytes, filename: str, *, opener: Callable[..., Any] | None = None) -> str:
    """Write one file byte-exact to ``ComfyUI/output/studio-h3/staged/<filename>``; return its output path."""
    body, boundary = _multipart({"type": "output", "subfolder": STAGED, "overwrite": "true"}, filename, data)
    request = urllib.request.Request(_endpoint.comfy_origin() + "/upload/image", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with (opener or urllib.request.urlopen)(request, timeout=300) as response:
        answer = json.loads(response.read(1 << 20).decode("utf-8"))
    if not isinstance(answer, dict) or answer.get("name") != filename or answer.get("type") != "output" \
            or answer.get("subfolder") != STAGED:
        raise H3ContinuationError("ComfyUI did not keep the staged take under its name", "unreachable")
    return f"{STAGED}/{filename}"


def stage_pairs(facts_and_paths: list[tuple[Mapping[str, Any], str]], names: list[str],
                upload: Callable[[bytes, str], str]) -> list[str]:
    """Upload each verified pair (mp4 then sidecar, same basename); return the staged mp4 paths."""
    staged = []
    for (facts, path), name in zip(facts_and_paths, names):
        video = Path(path).read_bytes()
        if hashlib.sha256(video).hexdigest() != facts["self_id"]:
            raise H3ContinuationError("the take changed after it was checked", "pair_broken")
        sidecar = mctx.sidecar_path(path).read_bytes()
        if mctx.take_facts(mctx.parse_header(sidecar))["self_id"] != facts["self_id"]:
            raise H3ContinuationError("the take changed after it was checked", "pair_broken")
        staged.append(upload(video, name + ".mp4"))
        upload(sidecar, name + mctx.SIDECAR_SUFFIX)
    return staged


def release_staged(paths: list[str], *, opener: Callable[..., Any] | None = None) -> list[str]:
    """Remove staged pairs through the node pack's own delete route (mp4 + sidecar, output-confined)."""
    removed = []
    for path in paths:
        if type(path) is not str or not path.startswith(STAGED + "/") or ".." in path or not path.endswith(".mp4"):
            continue
        body = json.dumps({"path": path}).encode("utf-8")
        request = urllib.request.Request(_endpoint.comfy_origin() + "/obvpm/h3/delete_take", data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with (opener or urllib.request.urlopen)(request, timeout=30) as response:
                answer = json.loads(response.read(1 << 16).decode("utf-8"))
        except Exception:  # noqa: BLE001 - a staged copy that is already gone is fine
            continue
        removed.extend(answer.get("removed", []) if isinstance(answer, dict) else [])
    return removed


# --------------------------------------------------------------------------- compile


def compile_request(*, graph: Mapping[str, Any], manifest: Mapping[str, Any], plan: Mapping[str, Any],
                    facts: list[Mapping[str, Any]], staged: list[str]) -> dict[str, Any]:
    validate_pinned_graph(graph, manifest)
    role = plan["role"]
    if len(facts) != (2 if role == "bridge" else 1) or len(staged) != len(facts):
        raise H3ContinuationError("staged takes do not match the request")
    g = copy.deepcopy(dict(graph))
    width, height = int(facts[0]["width"]), int(facts[0]["height"])
    gen = g[_GEN]["inputs"]
    gen.update({"prompt": plan["prompt"], "width": width, "height": height, "length": plan["raw_frames"]})
    g[_SEED]["inputs"]["value"] = plan["seed"]
    g[_SCHEDULER]["inputs"]["steps"] = plan["steps"]
    g[_SCHEDULER]["inputs"]["scheduler"] = plan["scheduler"]
    g[_SAMPLER]["inputs"]["sampler_name"] = plan["sampler_name"]
    g[_LOAD_SOURCE]["inputs"]["clip"] = staged[0]
    if role == "bridge":
        g[_LOAD_TARGET]["inputs"]["clip"] = staged[1]
        sides = ((_SPEC_BRIDGE_SOURCE, facts[0], "tail"), (_SPEC_LAST, facts[1], "head"))
    else:
        sides = ((_SPEC_LAST, facts[0], "tail" if role == "extend" else "head"),)
    chosen = plan.get("source_frame") if role in SOURCE_FRAME_ROLES else None
    for node_id, entry, side in sides:
        take_from, frame = window_for(entry, side, chosen)
        g[node_id]["inputs"]["take_from"] = take_from
        g[node_id]["inputs"]["take_from_frame"] = frame
    for node_id, node in g.items():
        if node["class_type"] not in set(manifest["required_classes"]):
            raise H3ContinuationError(f"node {node_id} class is not pinned")
        for value in node["inputs"].values():
            if isinstance(value, str) and (value.startswith("__") or PROMPT_SENTINEL in value):
                raise H3ContinuationError(f"node {node_id} still carries a placeholder")
    return {
        "graph": g,
        "effective_frames": plan["raw_frames"],
        "canvas": {"width": width, "height": height},
        "media": [{"role": entry["role"], "key": f"{entry['role']}/0", "sha256": entry["self_id"]} for entry in facts],
        "prompt_sent": plan["prompt"],
        "continuation": {"relation": {"extend": "extends", "prepend": "prepends", "bridge": "bridges"}[role],
                         "sources": [entry["self_id"] for entry in facts], "new_frames": plan["new_frames"],
                         # lane p7-vid (VID-097): the take's frame the join really sits at (after the grid snap)
                         **({"source_frame": used_source_frame(facts[0], "tail" if role == "extend" else "head", chosen)}
                            if role in SOURCE_FRAME_ROLES else {})},
    }


__all__ = [
    "BRIDGE_TARGET_ID",
    "EXTEND_TARGET_ID",
    "H3ContinuationError",
    "NOT_CONTINUABLE",
    "PREPEND_TARGET_ID",
    "ROLES",
    "SOURCE_FRAME_OUT_OF_RANGE",
    "TARGET_IDS",
    "check_pairs",
    "compile_request",
    "identity_args",
    "load_profile",
    "media_items",
    "new_frames",
    "raw_frames",
    "release_staged",
    "stage_pairs",
    "staged_names",
    "upload_output",
    "used_source_frame",
    "validate_args",
    "validate_pinned_graph",
    "window_for",
]
