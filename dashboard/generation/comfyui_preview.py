"""Live render preview from ComfyUI's websocket, for the Suite's own prompts.

One websocket connection for the whole plugin
(the configured ComfyUI address, ``comfy_endpoint.comfy_ws_url``) is opened lazily and
read only: the hub never sends anything except ComfyUI's documented
``feature_flags`` hello.  It keeps, per *watched* prompt id, the latest preview
frame and the latest ``progress`` value - nothing else, bounded in count, size
and age.  Prompts are watched only when the ledger names them (submit/preview
routes resolve the prompt id from the owner-checked job), so frames of the
artist's own ComfyUI runs that ComfyUI broadcasts are ignored.

Wire formats (ComfyUI ``server.py``/``protocol.py``):
- binary, event 1 ``PREVIEW_IMAGE``: ``>I event`` + ``>I format`` (1 JPEG,
  2 PNG) + image bytes; carries no prompt id, so it belongs to the prompt the
  preceding ``progress``/``executing`` message named.
- binary, event 4 ``PREVIEW_IMAGE_WITH_METADATA``: ``>I event`` + ``>I len`` +
  JSON metadata (``prompt_id``, ``image_type``) + image bytes.
- text JSON ``{"type": "progress", "data": {value, max, prompt_id, node}}``,
  ``executing`` (``node`` None = finished), ``execution_success``,
  ``execution_error``, ``execution_interrupted``.
No network access happens at import time.  Stdlib + optional ``websockets``
and optional Pillow; both missing is reported honestly, never raised.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import os
import re
import secrets
import shutil
import struct
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

PREVIEW_CLIENT_ID = "filmsuite-image-preview"
# w39-p2-tester item 4: the one configured ComfyUI address (generation/comfy_endpoint.py, loaded by path).
import importlib.util as _endpoint_util
from pathlib import Path as _EndpointPath
_endpoint_spec = _endpoint_util.spec_from_file_location("film_suite_comfy_endpoint", _EndpointPath(__file__).resolve().parents[1] / "generation" / "comfy_endpoint.py")
_endpoint = _endpoint_util.module_from_spec(_endpoint_spec)
_endpoint_spec.loader.exec_module(_endpoint)
DEFAULT_URL = "ws://127.0.0.1:8188/ws?clientId=" + PREVIEW_CLIENT_ID  # the default address; the hub reads the configured one
EVENT_PREVIEW_IMAGE = 1
EVENT_PREVIEW_IMAGE_WITH_METADATA = 4
FORMAT_MIME = {1: "image/jpeg", 2: "image/png"}
MAX_WATCHED = 32
MAX_FRAME_BYTES = 4 * 1024 * 1024
EXPIRE_SECONDS = 120.0
LONG_SIDE_PX = 512
RAW_FALLBACK_MAX_BYTES = 256 * 1024
BACKOFF_SECONDS = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)
_TERMINAL = frozenset({"done", "error", "interrupted"})
# R2d step history: every step's preview frame of a take, kept until the
# take is persisted (StepHistoryStore), bounded per take and per hub.
HISTORY_MAX_FRAMES = 64
HISTORY_MAX_BYTES = 12 * 1024 * 1024
HISTORY_HOLD_SECONDS = 1800.0
HISTORY_HUB_MAX_BYTES = 128 * 1024 * 1024


def history_keep_steps(total: int, limit: int = HISTORY_MAX_FRAMES) -> set[int]:
    """Steps whose frame is kept: all of 0..total, or ``limit`` evenly spaced (first and last always)."""
    if type(total) is not int or total < 0:
        return set()
    if total + 1 <= limit:
        return set(range(total + 1))
    return {round(i * total / (limit - 1)) for i in range(limit)}


def _thin(keys: list[int]) -> list[int]:
    """Interior keys to drop so about half remain; first and last stay."""
    ordered = sorted(keys)
    return ordered[1:-1:2] if len(ordered) > 2 else []


def parse_binary(message: bytes) -> tuple[int, str | None, bytes, str | None] | None:
    """Return ``(event, mime, image_bytes, prompt_id)`` or None if not a preview."""
    if type(message) not in (bytes, bytearray) or len(message) < 8:
        return None
    event = struct.unpack(">I", message[:4])[0]
    if event == EVENT_PREVIEW_IMAGE:
        mime = FORMAT_MIME.get(struct.unpack(">I", message[4:8])[0])
        return (event, mime, bytes(message[8:]), None) if mime else None
    if event == EVENT_PREVIEW_IMAGE_WITH_METADATA:
        length = struct.unpack(">I", message[4:8])[0]
        if length > 65_536 or 8 + length > len(message):
            return None
        try:
            metadata = json.loads(bytes(message[8:8 + length]).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(metadata, dict):
            return None
        mime = metadata.get("image_type")
        prompt_id = metadata.get("prompt_id")
        if mime not in ("image/jpeg", "image/png"):
            return None
        return event, mime, bytes(message[8 + length:]), prompt_id if type(prompt_id) is str else None
    return None


class _Entry:
    # Plain class: this module is loaded unregistered in sys.modules (like the
    # model catalog), where @dataclass cannot resolve its own module.
    __slots__ = ("state", "value", "maximum", "node", "frame", "mime", "frame_seq", "touched", "updated_at", "encoded",
                 "rev", "kj", "kj_image", "kj_mime", "sigmas", "deltas", "step_times", "progress_at",
                 "history", "history_bytes", "history_total", "history_wh", "persisted", "announced")

    def __init__(self) -> None:
        self.state: str = "waiting"
        self.value: int | None = None
        self.maximum: int | None = None
        self.node: str | None = None
        self.frame: bytes | None = None
        self.mime: str | None = None
        self.frame_seq: int = 0
        self.touched: float = 0.0
        self.updated_at: str | None = None
        self.encoded: tuple[int, str | None] | None = None
        # R2 live step preview (KJNodes 'kj_preview_override'): the latest
        # picture as sent (base64 JPEG) and small bounded per-step arrays.
        self.rev: int = 0
        self.kj: dict[str, Any] | None = None
        self.kj_image: str | None = None
        self.kj_mime: str | None = None
        self.sigmas: list[float] = []
        self.deltas: list[float | None] = []
        self.step_times: list[float | None] = []
        self.progress_at: float | None = None
        # R2d: step -> (image_b64, mime, sigma, delta, step_ms, bytes); kept
        # past completion until the take is persisted.
        self.history: dict[int, tuple[str, str, float | None, float | None, float | None, int]] = {}
        self.history_bytes: int = 0
        self.history_total: int | None = None
        self.history_wh: tuple[int | None, int | None] = (None, None)
        self.persisted: bool = False
        # (rev, frame_seq, state) last handed to PreviewHub.on_change.
        self.announced: tuple[int, int, str] | None = None

    def pending(self) -> bool:
        """Holds a step history nobody has persisted yet."""
        return bool(self.history) and not self.persisted


KJ_EVENT = "kj_preview_override"
# The node id the Krea adapter gives the inserted preview node
# (krea2_create_adapter.LIVE_PREVIEW_NODE_ID; pinned equal by a test).
KJ_NODE_ID = "500"
# R4: the Qwen edit adapter inserts its preview node as "900" (its graph
# already uses node id 500 for the prompt-enhancer CLIP loader).
KJ_NODE_IDS = frozenset({KJ_NODE_ID, "900"})
KJ_MAX_IMAGE_CHARS = 4 * 1024 * 1024
KJ_MAX_STEPS = 1000


def _num(value: Any) -> float | None:
    if type(value) in (int, float) and value == value and abs(value) < 1e12:
        return float(value)
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def encode_data_url(frame: bytes | None, mime: str | None) -> str | None:
    """Small data URL: Pillow re-encode to JPEG <= 512 px long side, else raw if small."""
    if not frame or mime not in ("image/jpeg", "image/png"):
        return None
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - Pillow is optional
        Image = None
    if Image is not None:
        try:
            with Image.open(io.BytesIO(frame)) as image:
                image.draft("RGB", (LONG_SIDE_PX, LONG_SIDE_PX))
                image = image.convert("RGB")
                image.thumbnail((LONG_SIDE_PX, LONG_SIDE_PX))
                out = io.BytesIO()
                image.save(out, format="JPEG", quality=80)
            return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
        except Exception:  # noqa: BLE001 - a broken frame is no frame
            return None
    if len(frame) <= RAW_FALLBACK_MAX_BYTES:
        return f"data:{mime};base64," + base64.b64encode(frame).decode("ascii")
    return None


class PreviewHub:
    """Latest frame + progress per watched prompt; one reader task per loop."""

    def __init__(
        self,
        url: str | None = None,
        *,
        connect: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        backoff: tuple[float, ...] = BACKOFF_SECONDS,
    ) -> None:
        self.url = url if url is not None else _endpoint.comfy_ws_url(PREVIEW_CLIENT_ID)
        self._connect = connect
        self._clock = clock
        self._backoff = backoff
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._current: str | None = None
        self._task: asyncio.Task | None = None
        self._handled: str | None = None
        # Weg A1: called as on_change(prompt_id, rev, state) after a message
        # gave a watched prompt a new step/frame or a new state. Runs on the
        # reader's loop; must be cheap and must not raise (guarded anyway).
        self.on_change: Callable[[str, int, str], Any] | None = None
        self.connected = False
        self.available = True
        self.connects = 0

    # -- lifecycle ---------------------------------------------------------
    def ensure_started(self) -> None:
        """Start the single reader on the running loop; never blocks."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = self._task
        if task is not None and not task.done() and task.get_loop() is loop:
            return
        self.connected = False
        self._task = loop.create_task(self._run(), name="filmsuite-comfyui-preview")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:  # noqa: BLE001 - cancellation of our own task
                pass

    def _open(self) -> Any:
        if self._connect is not None:
            return self._connect(self.url)
        from websockets.asyncio.client import connect  # type: ignore[import-not-found]

        return connect(self.url, max_size=MAX_FRAME_BYTES + 65_536, open_timeout=3, proxy=None,
                       ping_interval=20, ping_timeout=20, compression=None)

    async def _run(self) -> None:
        attempt = 0
        while True:
            try:
                opener = self._open()
            except ImportError:
                self.available = False
                return
            try:
                async with opener as socket:
                    self.connected = True
                    self.connects += 1
                    attempt = 0
                    await socket.send(json.dumps({"type": "feature_flags",
                                                  "data": {"supports_preview_metadata": True}}))
                    async for message in socket:
                        self.handle(message)
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception:  # noqa: BLE001 - ComfyUI off or dropped: retry later
                pass
            self.connected = False
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    # -- bookkeeping -------------------------------------------------------
    def watch(self, prompt_id: str) -> None:
        if type(prompt_id) is not str or not 1 <= len(prompt_id) <= 256:
            return
        self._expire()
        entry = self._entries.get(prompt_id)
        if entry is None:
            entry = self._entries[prompt_id] = _Entry()
        entry.touched = self._clock()
        self._entries.move_to_end(prompt_id)
        while len(self._entries) > MAX_WATCHED:
            # Prefer an entry whose history is persisted (or empty).
            victim = next((k for k, e in self._entries.items() if k != prompt_id and not e.pending()), None)
            if victim is None:
                self._entries.popitem(last=False)
            else:
                del self._entries[victim]

    def _expire(self) -> None:
        now = self._clock()
        for key in [k for k, e in self._entries.items() if now - e.touched > EXPIRE_SECONDS]:
            entry = self._entries[key]
            if entry.pending() and now - entry.touched <= HISTORY_HOLD_SECONDS:
                continue  # the take's steps are not on disk yet
            del self._entries[key]

    def _entry(self, prompt_id: Any) -> _Entry | None:
        if type(prompt_id) is not str:
            return None
        entry = self._entries.get(prompt_id)
        if entry is not None:
            entry.touched = self._clock()
            entry.updated_at = _utc_now()
            self._handled = prompt_id
        return entry

    def handle(self, message: Any) -> None:
        """Apply one websocket message; foreign prompts are ignored."""
        self._handled = None
        self._apply(message)
        prompt_id, callback = self._handled, self.on_change
        if prompt_id is None or callback is None:
            return
        entry = self._entries.get(prompt_id)
        if entry is None:
            return
        mark = (entry.rev, entry.frame_seq, entry.state)
        if mark == entry.announced:
            return
        entry.announced = mark
        try:
            callback(prompt_id, entry.rev, entry.state)
        except Exception:  # noqa: BLE001 - a listener never breaks the reader
            pass

    def _apply(self, message: Any) -> None:
        if isinstance(message, (bytes, bytearray)):
            parsed = parse_binary(message)
            if parsed is None:
                return
            _event, mime, image, prompt_id = parsed
            entry = self._entry(prompt_id if prompt_id is not None else self._current)
            if entry is None or entry.state in _TERMINAL or not image or len(image) > MAX_FRAME_BYTES:
                return
            entry.frame, entry.mime = image, mime
            entry.frame_seq += 1
            entry.encoded = None
            entry.state = "running"
            if entry.kj is None and type(entry.value) is int and type(entry.maximum) is int:
                # No preview node: the engine's own preview frames are the history.
                self._record(entry, entry.value, entry.maximum, base64.b64encode(image).decode("ascii"), mime,
                             None, None, entry.step_times[-1] if entry.step_times else None)
            return
        try:
            payload = json.loads(message) if isinstance(message, str) else None
        except ValueError:
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            return
        kind, data = payload.get("type"), payload["data"]
        if kind == KJ_EVENT:
            # Sent by the preview node to our client id; it names its node,
            # not the prompt: it belongs to the prompt ComfyUI is executing.
            entry = self._entry(self._current)
            if entry is not None and entry.state not in _TERMINAL:
                self._handle_kj(entry, data)
            return
        prompt_id = data.get("prompt_id")
        if kind in ("execution_start", "executing", "progress") and type(prompt_id) is str:
            self._current = prompt_id
        entry = self._entry(prompt_id)
        if entry is None:
            return
        if kind == "progress":
            value, maximum = data.get("value"), data.get("max")
            if type(maximum) is int and maximum > KJ_MAX_STEPS:
                # Not a sampler: e.g. the Qwen edit's prompt enhancer reports
                # token progress (max 8192).  Running, but no step to show.
                if entry.state not in _TERMINAL:
                    entry.state = "running"
                return
            if type(value) is int and type(maximum) is int and 0 <= value <= maximum <= 1_000_000:
                now = self._clock()
                if entry.kj is None and entry.value is not None and value == entry.value + 1 and entry.progress_at is not None:
                    # Without the preview node: step times from progress alone.
                    if len(entry.step_times) < KJ_MAX_STEPS:
                        entry.step_times.append(round((now - entry.progress_at) * 1000.0, 1))
                if entry.value != value:
                    entry.progress_at = now
                    entry.rev += 1
                entry.value, entry.maximum = value, maximum
                node = data.get("node")
                entry.node = node if type(node) is str and len(node) <= 64 else None
                if entry.state not in _TERMINAL:
                    entry.state = "running"
        elif kind in ("execution_start", "executing", "execution_cached"):
            if kind == "executing" and data.get("node") is None:
                self._finish(entry, "done")
            elif entry.state == "waiting":
                entry.state = "running"
        elif kind == "execution_success":
            self._finish(entry, "done")
        elif kind == "execution_error":
            self._finish(entry, "error")
        elif kind == "execution_interrupted":
            self._finish(entry, "interrupted")

    def _finish(self, entry: _Entry, state: str) -> None:
        entry.state = state
        # The live picture goes; the step history stays until it is persisted.
        entry.frame = entry.mime = entry.encoded = None
        entry.kj_image = entry.kj_mime = None
        entry.rev += 1

    def _record(self, entry: _Entry, step: int, total: int, image_b64: str, mime: str,
                sigma: float | None, delta: float | None, step_ms: float | None) -> None:
        """Keep one step frame, bounded: <= 64 frames, <= 12 MB per take, <= 128 MB per hub."""
        if entry.persisted or step not in history_keep_steps(total):
            return
        size = len(image_b64) * 3 // 4
        if size <= 0 or size > HISTORY_MAX_BYTES:
            return
        if entry.history_total != total:
            # A new sigma schedule (or the first frame): steps outside the new keep set go.
            keep = history_keep_steps(total)
            for key in [k for k in entry.history if k not in keep]:
                entry.history_bytes -= entry.history.pop(key)[5]
            entry.history_total = total
        old = entry.history.pop(step, None)
        if old is not None:
            entry.history_bytes -= old[5]
        entry.history[step] = (image_b64, mime, sigma, delta, step_ms, size)
        entry.history_bytes += size
        while entry.history_bytes > HISTORY_MAX_BYTES and len(entry.history) > 2:
            for key in _thin(list(entry.history)):
                entry.history_bytes -= entry.history.pop(key)[5]
        if entry.history_bytes > HISTORY_MAX_BYTES:
            entry.history.clear()
            entry.history_bytes = 0
        held = sum(e.history_bytes for e in self._entries.values())
        for other in list(self._entries.values()):
            if held <= HISTORY_HUB_MAX_BYTES:
                break
            if other is not entry and other.history:
                held -= other.history_bytes
                other.history.clear()
                other.history_bytes = 0

    def history(self, prompt_id: Any) -> dict[str, Any] | None:
        """The step history of one prompt (frames as received), or None."""
        entry = self._entries.get(prompt_id) if type(prompt_id) is str else None
        if entry is None or not entry.history:
            return None
        w, h = entry.history_wh
        frames = [{"step": k, "image_b64": v[0], "mime": v[1], "sigma": v[2], "delta": v[3], "step_ms": v[4], "bytes": v[5]}
                  for k, v in sorted(entry.history.items())]
        return {"total": entry.history_total, "w": w, "h": h, "sigmas": list(entry.sigmas), "deltas": list(entry.deltas),
                "step_times": list(entry.step_times), "frames": frames, "done": entry.state in _TERMINAL}

    def mark_persisted(self, prompt_id: Any) -> None:
        """The take's steps are on disk: free the memory, never record again."""
        entry = self._entries.get(prompt_id) if type(prompt_id) is str else None
        if entry is not None:
            entry.persisted = True
            entry.history.clear()
            entry.history_bytes = 0

    def _handle_kj(self, entry: _Entry, data: dict[str, Any]) -> None:
        """One 'kj_preview_override' payload: picture + per-step stats, bounded."""
        node = data.get("node_id")
        if type(node) is not str or node not in KJ_NODE_IDS:
            return
        step, total = data.get("step"), data.get("total")
        if type(step) is not int or type(total) is not int or not 0 <= step <= total <= KJ_MAX_STEPS:
            return
        sigmas = data.get("sigmas")
        if isinstance(sigmas, list) and sigmas:
            entry.sigmas = [v for v in (_num(s) for s in sigmas[:KJ_MAX_STEPS + 1]) if v is not None]
        if step == 0:
            entry.deltas, entry.step_times = [], []
        elif step == len(entry.deltas) + 1:
            entry.deltas.append(_num(data.get("delta")))
            entry.step_times.append(_num(data.get("step_ms")))
        image, mime = data.get("image"), data.get("mime") or "image/jpeg"
        w, h = data.get("w"), data.get("h")
        if type(image) is str and 0 < len(image) <= KJ_MAX_IMAGE_CHARS and mime in ("image/jpeg", "image/png", "image/webp"):
            entry.kj_image, entry.kj_mime = image, mime
            entry.history_wh = (w if type(w) is int and 0 < w <= 16384 else None, h if type(h) is int and 0 < h <= 16384 else None)
            self._record(entry, step, total, image, mime, _num(data.get("sigma")), _num(data.get("delta")), _num(data.get("step_ms")))
        entry.kj = {"node_id": node, "step": step, "total": total,
                    "w": w if type(w) is int and 0 < w <= 16384 else None,
                    "h": h if type(h) is int and 0 < h <= 16384 else None,
                    "sigma": _num(data.get("sigma")), "delta": _num(data.get("delta")),
                    "step_ms": _num(data.get("step_ms")), "avg_step_ms": _num(data.get("avg_step_ms"))}
        entry.rev += 1
        if entry.state == "waiting":
            entry.state = "running"

    def live(self, prompt_id: str) -> dict[str, Any]:
        """The live step view of one prompt (KJ node if it reported, else progress)."""
        self._expire()
        entry = self._entries.get(prompt_id)
        base = self.snapshot(prompt_id)
        if entry is None:
            return {**base, "rev": 0, "source": None, "step": None, "total": None, "w": None, "h": None, "sigma": None,
                    "delta": None, "step_ms": None, "avg_step_ms": None, "eta_ms": None, "sigmas": [], "deltas": [],
                    "step_times": [], "image_b64": None, "mime": None}
        kj = entry.kj
        step = kj["step"] if kj else entry.value
        total = kj["total"] if kj else entry.maximum
        avg = kj["avg_step_ms"] if kj and kj["avg_step_ms"] is not None else None
        if avg is None:
            known = [t for t in entry.step_times[-8:] if t is not None]
            avg = round(sum(known) / len(known), 1) if known else None
        eta = round(avg * (total - step), 1) if avg is not None and type(step) is int and type(total) is int else None
        return {**base, "rev": entry.rev, "source": "kj" if kj else ("progress" if entry.value is not None else None),
                "step": step, "total": total, "w": kj["w"] if kj else None, "h": kj["h"] if kj else None,
                "sigma": kj["sigma"] if kj else None, "delta": kj["delta"] if kj else None,
                "step_ms": kj["step_ms"] if kj else (entry.step_times[-1] if entry.step_times else None),
                "avg_step_ms": avg, "eta_ms": eta, "sigmas": list(entry.sigmas), "deltas": list(entry.deltas),
                "step_times": list(entry.step_times), "image_b64": entry.kj_image, "mime": entry.kj_mime}

    # -- reads -------------------------------------------------------------
    def snapshot(self, prompt_id: str) -> dict[str, Any]:
        """Public, path-free state for one prompt; never encodes (cheap)."""
        self._expire()
        entry = self._entries.get(prompt_id)
        if not self.available:
            state = "unavailable"
        elif entry is None:
            state = "waiting"
        elif entry.state == "running" and entry.frame is None and entry.kj_image is None and (entry.value or 0) >= 1:
            # Sampling advanced but ComfyUI sent no picture: its preview
            # method is 'none' for this prompt.  Say so instead of waiting.
            state = "no_preview"
        else:
            state = entry.state
        if state == "waiting" and not self.connected:
            state = "connecting"
        return {
            "state": state,
            "step": entry.value if entry else None,
            "steps": entry.maximum if entry else None,
            "node": entry.node if entry else None,
            "frame_seq": entry.frame_seq if entry else 0,
            "updated_at": entry.updated_at if entry else None,
        }

    async def data_url(self, prompt_id: str) -> str | None:
        entry = self._entries.get(prompt_id)
        if entry is None or entry.frame is None:
            return None
        if entry.encoded is not None and entry.encoded[0] == entry.frame_seq:
            return entry.encoded[1]
        seq, frame, mime = entry.frame_seq, entry.frame, entry.mime
        url = await asyncio.to_thread(encode_data_url, frame, mime)
        if entry.frame_seq == seq:
            entry.encoded = (seq, url)
        return url

    def size(self) -> int:
        return len(self._entries)


# --- R2d: persisted step history ---------------------------------------------
STEP_HISTORY_SCHEMA = "studio.image-step-history/1"
STORE_MAX_BYTES = 500 * 1024 * 1024
_JOB_DIR_RE = re.compile(r"^imgjob_[0-9a-f]{32}$")
_FRAME_FILE_RE = re.compile(r"^s(\d{4})\.(jpg|png|webp)$")
_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_MIME = {v: k for k, v in _EXT.items()}


class StepHistoryStore:
    """One folder per take in the plugin's own data dir: frames + ``steps.json``.

    Write-once and atomic (built in a temp folder, then renamed into place);
    the whole store is bounded (``max_bytes``) with least-recently-used
    eviction, a read touches the take.  Never writes outside ``root``.
    """

    def __init__(self, root: Path | str, *, max_bytes: int = STORE_MAX_BYTES,
                 max_take_bytes: int = HISTORY_MAX_BYTES, max_frames: int = HISTORY_MAX_FRAMES) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.max_take_bytes = max_take_bytes
        self.max_frames = max_frames

    def _dir(self, job_id: str) -> Path:
        if type(job_id) is not str or not _JOB_DIR_RE.match(job_id):
            raise ValueError("invalid job id")
        return self.root / job_id

    def has(self, job_id: str) -> bool:
        try:
            return (self._dir(job_id) / "steps.json").is_file()
        except ValueError:
            return False

    def save(self, job_id: str, history: dict[str, Any]) -> bool:
        """Persist one take's history; False if there is nothing valid or it exists."""
        final = self._dir(job_id)
        if (final / "steps.json").is_file():
            return False
        frames: list[tuple[dict[str, Any], bytes]] = []
        total_bytes = 0
        for frame in sorted(history.get("frames") or [], key=lambda f: f.get("step", -1)):
            step, mime = frame.get("step"), frame.get("mime")
            if type(step) is not int or not 0 <= step <= 9999 or mime not in _EXT:
                continue
            try:
                data = base64.b64decode(frame.get("image_b64") or "", validate=True)
            except (binascii.Error, ValueError):
                continue
            if not data:
                continue
            frames.append(({"step": step, "file": f"s{step:04d}.{_EXT[mime]}", "mime": mime, "bytes": len(data),
                            "sigma": _num(frame.get("sigma")), "delta": _num(frame.get("delta")),
                            "step_ms": _num(frame.get("step_ms"))}, data))
            total_bytes += len(data)
        while frames and (len(frames) > self.max_frames or total_bytes > self.max_take_bytes) and len(frames) > 2:
            drop = set(_thin(list(range(len(frames)))))
            frames = [f for i, f in enumerate(frames) if i not in drop]
            total_bytes = sum(len(f[1]) for f in frames)
        if not frames or total_bytes > self.max_take_bytes:
            return False
        meta = {"schema": STEP_HISTORY_SCHEMA, "image_producer_job_id": job_id,
                "total": history.get("total") if type(history.get("total")) is int else None,
                "w": history.get("w") if type(history.get("w")) is int else None,
                "h": history.get("h") if type(history.get("h")) is int else None,
                "sigmas": [_num(v) for v in (history.get("sigmas") or [])[:KJ_MAX_STEPS + 1]],
                "deltas": [_num(v) for v in (history.get("deltas") or [])[:KJ_MAX_STEPS]],
                "step_times": [_num(v) for v in (history.get("step_times") or [])[:KJ_MAX_STEPS]],
                "frames": [f[0] for f in frames], "bytes": total_bytes, "saved_at": _utc_now()}
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.root / f".tmp-{job_id}-{secrets.token_hex(4)}"
        try:
            temp.mkdir()
            for info, data in frames:
                with open(temp / info["file"], "wb") as out:
                    out.write(data)
            with open(temp / "steps.json", "w", encoding="utf-8") as out:
                json.dump(meta, out, separators=(",", ":"))
            try:
                os.replace(temp, final)
            except OSError:
                if (final / "steps.json").is_file():
                    return False  # another writer won; write-once
                if final.exists():
                    shutil.rmtree(final, ignore_errors=True)  # a torn folder with no steps.json
                os.replace(temp, final)
        finally:
            if temp.exists():
                shutil.rmtree(temp, ignore_errors=True)
        self.evict(keep=job_id)
        return True

    def _take_size(self, folder: Path) -> int:
        return sum(item.stat().st_size for item in folder.iterdir() if item.is_file())

    def evict(self, keep: str | None = None) -> list[str]:
        """Drop least-recently-used takes until the store fits; stale temp folders go too."""
        if not self.root.is_dir():
            return []
        takes: list[tuple[float, str, int]] = []
        total = 0
        for folder in self.root.iterdir():
            if not folder.is_dir():
                continue
            if folder.name.startswith(".tmp-"):
                try:
                    if time.time() - folder.stat().st_mtime > 3600:
                        shutil.rmtree(folder, ignore_errors=True)
                except OSError:
                    pass
                continue
            if not _JOB_DIR_RE.match(folder.name):
                continue
            try:
                size = self._take_size(folder)
                used = (folder / "steps.json").stat().st_mtime
            except OSError:
                continue
            takes.append((used, folder.name, size))
            total += size
        dropped: list[str] = []
        for used, name, size in sorted(takes):
            if total <= self.max_bytes:
                break
            if name == keep:
                continue
            shutil.rmtree(self.root / name, ignore_errors=True)
            total -= size
            dropped.append(name)
        return dropped

    def read(self, job_id: str) -> dict[str, Any] | None:
        """The stored meta of one take (touches it for LRU), or None."""
        try:
            path = self._dir(job_id) / "steps.json"
            with open(path, encoding="utf-8") as handle:
                meta = json.load(handle)
        except (ValueError, OSError):
            return None
        if not isinstance(meta, dict) or meta.get("schema") != STEP_HISTORY_SCHEMA or not isinstance(meta.get("frames"), list):
            return None
        try:
            os.utime(path, None)
        except OSError:
            pass
        return meta

    def frame(self, job_id: str, step: int) -> tuple[bytes, str] | None:
        meta = self.read(job_id)
        if meta is None:
            return None
        for info in meta["frames"]:
            name = info.get("file") if isinstance(info, dict) else None
            match = _FRAME_FILE_RE.match(name) if type(name) is str else None
            if match and info.get("step") == step and int(match.group(1)) == step:
                try:
                    return (self._dir(job_id) / name).read_bytes(), _MIME[match.group(2)]
                except OSError:
                    return None
        return None


__all__ = [
    "DEFAULT_URL", "EVENT_PREVIEW_IMAGE", "EVENT_PREVIEW_IMAGE_WITH_METADATA", "EXPIRE_SECONDS", "MAX_WATCHED",
    "HISTORY_MAX_BYTES", "HISTORY_MAX_FRAMES", "PREVIEW_CLIENT_ID", "PreviewHub", "STEP_HISTORY_SCHEMA",
    "StepHistoryStore", "encode_data_url", "history_keep_steps", "parse_binary",
]
