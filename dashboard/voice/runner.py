"""Speak one line N times: the take runner (lane fs-voice).

Each take is one ledger row of the Shoot job store (generation/job_store.py),
exactly as out/fs-msnd-SPEC-generated-sound.md asks, so Media lists it as a
sound (kind audio) and its recipe sidecar is the take's recipe link:

1. ``create_or_get_job`` (state queued, ``save_to`` = <project folder>/Voice/<line>/<name>.flac,
   ``args`` = {prompt "<Character>: <line>", seed, duration_seconds}).
2. The voice clip is cut to its IN/OUT (ffmpeg, 16-bit WAV) and uploaded to
   ComfyUI ``temp/`` under its sha256; the pinned graph is compiled with only
   declared pointers; ``GET /queue`` is read before the submit (the artist's
   jobs stay first: we only append, never cancel, interrupt or reorder).
3. The finished FLAC is read through ``/view``, written to ``save_to``, measured
   (ffmpeg ebur128: integrated LUFS, true peak) and published through the
   publication lease; the recipe sidecar is written next to it.

The ComfyUI side is an injected object (``comfy``) so the runner is tested
offline; the route passes the real one.  Standalone (stdlib only).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env

TAKE_SUFFIX = ".flac"
POLL_SECONDS = 1.0
TAKE_TIMEOUT_SECONDS = 1800.0


class VoiceRunError(RuntimeError):
    """A take failed; the message is shown on the take."""


def safe_name(text: str, limit: int = 40) -> str:
    keep = "".join(ch if ch.isalnum() or ch in " -_" else " " for ch in text)
    return "-".join(keep.split())[:limit] or "line"


def cut_clip(source: Path, start: float, end: float | None, ffmpeg: str | os.PathLike[str], scratch: Path) -> bytes:
    """The voice clip from IN to OUT as 16-bit PCM WAV bytes (mono kept as is)."""
    scratch.mkdir(parents=True, exist_ok=True)
    out = scratch / f"clip-{uuid.uuid4().hex}.wav"
    argv = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}", "-i", str(source)]
    if end is not None:
        argv += ["-t", f"{max(0.0, end - start):.3f}"]
    argv += ["-map", "0:a:0", "-c:a", "pcm_s16le", str(out)]
    try:
        done = subprocess.run(argv, capture_output=True, timeout=120, check=False, shell=False, env=child_env())
        if done.returncode != 0 or not out.is_file() or out.stat().st_size <= 44:
            raise VoiceRunError("The voice clip could not be read")
        return out.read_bytes()
    finally:
        out.unlink(missing_ok=True)


def apply_gain(path: Path, gain_db: float, ffmpeg: str | os.PathLike[str], scratch: Path) -> bytes:
    """The take with its (non-destructive) gain applied, as FLAC bytes, for listening."""
    if abs(gain_db) < 0.05:
        return path.read_bytes()
    scratch.mkdir(parents=True, exist_ok=True)
    out = scratch / f"gain-{uuid.uuid4().hex}.flac"
    argv = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-af", f"volume={gain_db:.2f}dB", str(out)]
    try:
        done = subprocess.run(argv, capture_output=True, timeout=120, check=False, shell=False, env=child_env())
        if done.returncode != 0 or not out.is_file():
            raise VoiceRunError("The take could not be read")
        return out.read_bytes()
    finally:
        out.unlink(missing_ok=True)


def match_gains(loudness: Mapping[str, float | None], target_lufs: float | None = None) -> dict[str, float]:
    """Gain (dB, 0.1 steps) per take so every measured take of a line sits at one loudness.

    ``target_lufs`` None = the median of the line's takes (the takes move toward each
    other, none is pushed far).  An unmeasured (silent) take keeps 0 dB.  Clamped to
    +-24 dB.  Non-destructive: the file never changes; the gain is stored on the take
    and applied when it is played or placed.
    """
    measured = sorted(v for v in loudness.values() if isinstance(v, (int, float)))
    if not measured:
        return {key: 0.0 for key in loudness}
    if target_lufs is None:
        middle = len(measured) // 2
        target_lufs = measured[middle] if len(measured) % 2 else (measured[middle - 1] + measured[middle]) / 2
    out = {}
    for key, value in loudness.items():
        if not isinstance(value, (int, float)):
            out[key] = 0.0
        else:
            out[key] = round(max(-24.0, min(24.0, target_lufs - value)), 1)
    return out


# Lane w27-rows item 6 (VOICE-1): the reason a free-text OmniVoice voice description gets, naming what it accepts.
DESCRIPTION_WORDS_REASON = ("Voice description not understood: the voice model takes only gender, age, pitch and accent "
                            "words (e.g. female, young adult, low pitch, british accent).")


def run_take(
    *,
    store: Any,
    studio_job_id: str,
    target: Mapping[str, Any],
    compile_graph: Callable[..., dict[str, Any]],
    text: str,
    values: Mapping[str, Any],
    clip_wav: bytes | None,
    words: str | None,
    description: str | None,
    save_path: Path,
    comfy: Any,
    measure: Callable[[Path], dict[str, Any]],
    write_recipe: Callable[[Path, str], None],
    on_progress: Callable[[str, float | None], None] = lambda _id, _p: None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = TAKE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One take, blocking.  Returns {seconds, loudness, took_seconds}; failures mark the row failed."""
    started = clock()
    try:
        reference = None
        if clip_wav is not None:
            name = hashlib.sha256(clip_wav).hexdigest() + ".wav"
            reference = comfy.upload(clip_wav, name)
        graph = compile_graph(target, text=text, values=values, reference=reference, words=words, description=description)
        comfy.queue()  # read before every submit; we only append behind whatever is there
        prompt_id = comfy.submit(graph)
        store.transition(studio_job_id, "running")
        output_node = target["manifest"]["output"]["node_id"]
        while True:
            done = comfy.history(prompt_id)
            if done is not None:
                break
            on_progress(studio_job_id, comfy.progress(prompt_id))
            if clock() - started > timeout:
                raise VoiceRunError("The take took too long")
            sleep(POLL_SECONDS)
        status = done.get("status") or {}
        if status.get("status_str") == "error":
            messages = [m for m in status.get("messages") or [] if isinstance(m, list) and m and m[0] == "execution_error"]
            detail = (messages[0][1] or {}).get("exception_message", "") if messages else ""
            # Lane w27-rows item 6 (VOICE-1): OmniVoice's voice design reads a fixed vocabulary of voice traits and
            # refuses free text with "Unsupported instruct items found in ..."; the artist gets the words it takes.
            if "unsupported instruct" in str(detail).lower():
                raise VoiceRunError(DESCRIPTION_WORDS_REASON)
            raise VoiceRunError(("The engine stopped: " + str(detail).strip().splitlines()[0][:160]) if detail else "The engine stopped")
        files = ((done.get("outputs") or {}).get(output_node) or {}).get("audio") or []
        if not files:
            raise VoiceRunError("The engine made no sound")
        data = comfy.view(files[0])
        save_path.parent.mkdir(parents=True, exist_ok=True)
        if save_path.exists():
            raise VoiceRunError("A file of that name is already there")
        temporary = save_path.with_name(f".{save_path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, save_path)
        facts = measure(save_path)
        if not facts.get("seconds"):
            raise VoiceRunError("The take is empty")
        store.set_output_identity(studio_job_id, {"filename": save_path.name, "prompt_id": prompt_id})
        store.transition(studio_job_id, "ready")
        lease = store.acquire_publication_lease(studio_job_id, "fs-voice", lease_seconds=120)
        digest = hashlib.sha256(data).hexdigest()
        store.release_publication_lease(studio_job_id, lease.owner_id, lease.token, published_path=str(save_path),
                                        source_media_sha256=digest, published_file_sha256=digest)
        write_recipe(save_path, studio_job_id)
        on_progress(studio_job_id, 1.0)
        return {"seconds": facts["seconds"], "loudness": facts.get("loudness") or {}, "took_seconds": round(clock() - started, 2)}
    except Exception as exc:  # noqa: BLE001 - every failure ends the row with a reason
        try:
            store.transition(studio_job_id, "failed", reason_code="unknown")
        except Exception:  # noqa: BLE001 - a row already terminal stays as it is
            pass
        on_progress(studio_job_id, None)
        if isinstance(exc, VoiceRunError):
            raise
        message = str(exc) if isinstance(exc, ValueError) and str(exc) else "The take failed"
        raise VoiceRunError(message) from exc


# w39-p2-tester item 4: the one configured ComfyUI address (generation/comfy_endpoint.py, loaded by path).
import importlib.util as _endpoint_util
from pathlib import Path as _EndpointPath
_endpoint_spec = _endpoint_util.spec_from_file_location("film_suite_comfy_endpoint", _EndpointPath(__file__).resolve().parents[1] / "generation" / "comfy_endpoint.py")
_endpoint = _endpoint_util.module_from_spec(_endpoint_spec)
_endpoint_spec.loader.exec_module(_endpoint)

class ComfyHTTP:
    """The real ComfyUI side over the configured ComfyUI HTTP API (comfy_endpoint)."""

    def __init__(self, origin: str | None = None, *, client_id: str | None = None, progress: Callable[[str], float | None] | None = None):
        resolved = _endpoint.comfy_origin()
        if origin is not None and origin != resolved:
            raise ValueError("voice takes run on the configured engine only")
        self.origin = resolved
        self.client_id = client_id or f"fs-voice-{uuid.uuid4().hex[:12]}"
        self._progress = progress

    def _json(self, method: str, path: str, body: bytes | None = None, headers: Mapping[str, str] | None = None) -> Any:
        import urllib.request
        request = urllib.request.Request(self.origin + path, data=body, method=method, headers=dict(headers or {}))
        with _endpoint._opener.open(request, timeout=60) as response:  # noqa: S310 - configured origin, no redirect
            return json.loads(response.read(8 << 20).decode("utf-8"))

    def queue(self) -> dict[str, Any]:
        return self._json("GET", "/queue")

    def upload(self, data: bytes, filename: str) -> str:
        boundary = "----fsvoice" + uuid.uuid4().hex
        body = b"".join([
            f'--{boundary}\r\nContent-Disposition: form-data; name="type"\r\n\r\ntemp\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'.encode(),
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
            data, f"\r\n--{boundary}--\r\n".encode()])
        answer = self._json("POST", "/upload/image", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if answer.get("name") != filename or answer.get("type") != "temp" or answer.get("subfolder"):
            raise VoiceRunError("The engine did not keep the voice clip")
        return f"{filename} [temp]"

    def submit(self, graph: Mapping[str, Any]) -> str:
        body = json.dumps({"prompt": graph, "client_id": self.client_id}).encode("utf-8")
        try:
            answer = self._json("POST", "/prompt", body, {"Content-Type": "application/json"})
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "read", lambda: b"")()
            raise VoiceRunError("The engine refused the take: " + detail.decode("utf-8", "replace")[:200]) from exc
        prompt_id = answer.get("prompt_id")
        if type(prompt_id) is not str:
            raise VoiceRunError("The engine refused the take")
        return prompt_id

    def history(self, prompt_id: str) -> dict[str, Any] | None:
        entry = self._json("GET", f"/history/{prompt_id}").get(prompt_id)
        if not entry:
            return None
        status = entry.get("status") or {}
        return entry if status.get("completed") or status.get("status_str") in ("success", "error") else None

    def progress(self, prompt_id: str) -> float | None:
        return self._progress(prompt_id) if self._progress else None

    def view(self, ref: Mapping[str, Any]) -> bytes:
        import urllib.parse
        import urllib.request
        query = urllib.parse.urlencode({"filename": ref["filename"], "subfolder": ref.get("subfolder", ""), "type": ref.get("type", "output")})
        with _endpoint._opener.open(f"{self.origin}/view?{query}", timeout=120) as response:  # noqa: S310 - no redirect
            return response.read(256 << 20)


__all__ = ["ComfyHTTP", "TAKE_SUFFIX", "VoiceRunError", "apply_gain", "cut_clip", "match_gains", "run_take", "safe_name"]
