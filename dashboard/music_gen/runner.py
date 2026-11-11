"""Music takes: run one take of a music target, stage by stage (lane w16-mus).

The pattern is the voice take runner (dashboard/voice/runner.py); music differs in four ways:

1. A take may run several graphs in order (YuE2: notes, then song).  The notes stage answers text
   (ABC) through ``PreviewAny``; it is stored on the take so the artist and Hermes can read and edit it,
   and the song stage reads it from there (or from given notes).
2. GPU rule: before every submit ``GET /queue`` must be empty (running 0, pending 0).  While anything
   else runs or waits the runner waits and asks again (``IDLE_POLL_SECONDS``); it never cancels,
   interrupts or reorders anything.
3. Outputs come from ``PreviewAudio`` (ComfyUI ``temp/``), are copied into the project and measured
   on the stored file: seconds (decoded samples / rate), sample peak (decoded float), clipped
   samples, loudness.
4. Every failure ends the take with a reason code and a readable message.

The ComfyUI side is an injected object so the runner is tested offline.  Standalone (stdlib only).
"""
from __future__ import annotations

import array
import json
import os
import subprocess
import sys
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
IDLE_POLL_SECONDS = 60.0
TAKE_TIMEOUT_SECONDS = 3600.0
IDLE_TIMEOUT_SECONDS = 6 * 3600.0
CLIP_LEVEL = 0.9999  # a decoded sample at or above this is a clipped sample (16/24-bit full scale)


class MusicRunError(RuntimeError):
    """A take failed; ``code`` is the reason code, str() the message shown on the take."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def safe_name(text: str, limit: int = 40) -> str:
    keep = "".join(ch if ch.isalnum() or ch in " -_" else " " for ch in text)
    return "-".join(keep.split())[:limit] or "music"


def queue_is_empty(answer: Any) -> bool:
    return isinstance(answer, Mapping) and not (answer.get("queue_running") or []) and not (answer.get("queue_pending") or [])


def wait_for_idle(comfy: Any, *, clock: Callable[[], float], sleep: Callable[[float], None], on_wait: Callable[[], None] = lambda: None,
                  timeout: float = IDLE_TIMEOUT_SECONDS) -> None:
    """Block until GET /queue is 0/0 (the artist's jobs first; nothing is cancelled or reordered)."""
    started = clock()
    while True:
        if queue_is_empty(comfy.queue()):
            return
        on_wait()
        if clock() - started > timeout:
            raise MusicRunError("engine_busy", "The engine stayed busy")
        sleep(IDLE_POLL_SECONDS)


def measure_samples(path: Path, ffmpeg: str | os.PathLike[str]) -> dict[str, Any]:
    """Decoded float samples of the stored file: samples per channel, sample rate, channels, peak, clipped."""
    probe = subprocess.run([str(ffmpeg), "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=120, check=False, env=child_env())
    rate = channels = 0
    for line in probe.stderr.splitlines():
        if "Audio:" in line:
            parts = [p.strip() for p in line.split("Audio:", 1)[1].split(",")]
            for part in parts:
                if part.endswith(" Hz"):
                    rate = int(part.split()[0])
                if part in ("mono", "stereo") or part.endswith(" channels"):
                    channels = 1 if part == "mono" else 2 if part == "stereo" else int(part.split()[0])
            break
    if rate < 1000 or channels < 1:
        raise MusicRunError("unreadable", "The take has no sample rate")
    argv = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=child_env())
    total = 0
    peak = 0.0
    clipped = 0
    try:
        assert process.stdout is not None
        rest = b""
        while True:
            chunk = process.stdout.read(1 << 20)
            if not chunk:
                break
            chunk = rest + chunk
            usable = len(chunk) - len(chunk) % 4
            rest = chunk[usable:]
            values = array.array("f")
            values.frombytes(chunk[:usable])
            if sys.byteorder != "little":
                values.byteswap()
            total += len(values)
            if values:
                high, low = max(values), min(values)
                peak = max(peak, high, -low)
                if high >= CLIP_LEVEL or -low >= CLIP_LEVEL:
                    clipped += sum(1 for v in values if v >= CLIP_LEVEL or v <= -CLIP_LEVEL)
    finally:
        process.wait(timeout=600)
    if process.returncode != 0 or total == 0:
        raise MusicRunError("unreadable", "The take decodes to no samples")
    samples = total // channels
    return {"samples": samples, "sample_rate": rate, "channels": channels, "seconds": round(samples / rate, 6),
            "peak": round(peak, 6), "clipped_samples": clipped}


def _finish(comfy: Any, prompt_id: str, *, clock: Callable[[], float], sleep: Callable[[float], None], started: float, timeout: float,
            on_progress: Callable[[float | None], None]) -> dict[str, Any]:
    while True:
        done = comfy.history(prompt_id)
        if done is not None:
            break
        on_progress(comfy.progress(prompt_id))
        if clock() - started > timeout:
            raise MusicRunError("take_timeout", "The take took too long")
        sleep(POLL_SECONDS)
    status = done.get("status") or {}
    if status.get("status_str") == "error":
        messages = [m for m in status.get("messages") or [] if isinstance(m, list) and m and m[0] == "execution_error"]
        detail = (messages[0][1] or {}).get("exception_message", "") if messages else ""
        raise MusicRunError("engine_error", ("The engine stopped: " + str(detail).strip().splitlines()[0][:160]) if detail else "The engine stopped")
    return done


def run_take(
    *,
    target: Mapping[str, Any],
    stages: list[str],
    values: Mapping[str, Any],
    compile_graph: Callable[..., dict[str, Any]],
    expected_seconds: Callable[..., float | None],
    save_path: Path,
    comfy: Any,
    ffmpeg: str | os.PathLike[str],
    loudness: Callable[[Path], dict[str, Any]] = lambda _p: {},
    on_stage: Callable[[str, dict[str, Any]], None] = lambda _s, _f: None,
    on_progress: Callable[[float | None], None] = lambda _p: None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = TAKE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """One take, blocking.  Returns its facts; raises MusicRunError with a reason code."""
    started = clock()
    values = dict(values)
    facts: dict[str, Any] = {"stages": [], "notes": None}
    manifest = target["manifest"]
    try:
        for index, stage in enumerate(stages):
            graph = compile_graph(target, stage, values)
            on_stage("waiting_for_engine", facts)
            wait_for_idle(comfy, clock=clock, sleep=sleep)
            prompt_id = comfy.submit(graph)
            facts["stages"].append({"stage": stage, "prompt_id": prompt_id})
            on_stage(stage, facts)
            done = _finish(comfy, prompt_id, clock=clock, sleep=sleep, started=started, timeout=timeout,
                           on_progress=lambda p: on_progress(None if p is None else round((index + p) / len(stages), 3)))
            entry = manifest["graphs"][stage]["output"]
            produced = (done.get("outputs") or {}).get(entry["node_id"]) or {}
            if entry["kind"] == "text":
                text = produced.get("text") or []
                notes = text[0] if text and isinstance(text[0], str) else ""
                if not notes.strip():
                    raise MusicRunError("no_notes", "The engine wrote no notes")
                values["notes"] = notes
                facts["notes"] = notes
                continue
            files = produced.get("audio") or []
            if not files:
                raise MusicRunError("no_sound", "The engine made no sound")
            if files[0].get("type") != "temp":
                raise MusicRunError("not_temp", "The engine wrote outside its temp folder")
            data = comfy.view(files[0])
            save_path.parent.mkdir(parents=True, exist_ok=True)
            if save_path.exists():
                raise MusicRunError("name_taken", "A file of that name is already there")
            temporary = save_path.with_name(f".{save_path.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, save_path)
            measured = measure_samples(save_path, ffmpeg)
            expected = expected_seconds(target, stage, values)
            facts.update(measured)
            facts["expected_seconds"] = expected
            facts["length_exact"] = None if expected is None else measured["samples"] == round(expected * measured["sample_rate"])
            facts["loudness"] = loudness(save_path)
            facts["file"] = str(save_path)
        facts["took_seconds"] = round(clock() - started, 2)
        on_progress(1.0)
        return facts
    except MusicRunError:
        on_progress(None)
        raise
    except Exception as exc:  # noqa: BLE001 - every failure ends the take with a reason
        on_progress(None)
        code = getattr(exc, "code", None) or "take_failed"
        message = str(exc) if isinstance(exc, ValueError) and str(exc) else "The take failed"
        raise MusicRunError(code, message) from exc


class ComfyHTTP:
    """The real ComfyUI side over the loopback HTTP API (127.0.0.1:8188 only)."""

    def __init__(self, origin: str = "http://127.0.0.1:8188", *, client_id: str | None = None,
                 progress: Callable[[str], float | None] | None = None):
        if origin != "http://127.0.0.1:8188":
            raise ValueError("music takes run on the local engine only")
        self.origin = origin
        self.client_id = client_id or f"fs-music-{uuid.uuid4().hex[:12]}"
        self._progress = progress

    def _json(self, method: str, path: str, body: bytes | None = None, headers: Mapping[str, str] | None = None) -> Any:
        import urllib.request
        request = urllib.request.Request(self.origin + path, data=body, method=method, headers=dict(headers or {}))
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - fixed loopback origin
            return json.loads(response.read(8 << 20).decode("utf-8"))

    def queue(self) -> dict[str, Any]:
        return self._json("GET", "/queue")

    def submit(self, graph: Mapping[str, Any]) -> str:
        body = json.dumps({"prompt": graph, "client_id": self.client_id}).encode("utf-8")
        try:
            answer = self._json("POST", "/prompt", body, {"Content-Type": "application/json"})
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "read", lambda: b"")()
            raise MusicRunError("engine_refused", "The engine refused the take: " + detail.decode("utf-8", "replace")[:200]) from exc
        prompt_id = answer.get("prompt_id")
        if type(prompt_id) is not str:
            raise MusicRunError("engine_refused", "The engine refused the take")
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
        query = urllib.parse.urlencode({"filename": ref["filename"], "subfolder": ref.get("subfolder", ""), "type": ref.get("type", "temp")})
        with urllib.request.urlopen(f"{self.origin}/view?{query}", timeout=120) as response:  # noqa: S310
            return response.read(512 << 20)


__all__ = ["ComfyHTTP", "IDLE_POLL_SECONDS", "MusicRunError", "TAKE_SUFFIX", "measure_samples", "queue_is_empty", "run_take",
           "safe_name", "wait_for_idle"]
