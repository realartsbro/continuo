"""ffmpeg process runner with progress, cancellation and a finite error."""
from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

from .errors import ExportError

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env

ProgressFn = Callable[[int], None]
CancelFn = Callable[[], bool]


class Cancelled(ExportError):
    def __init__(self) -> None:
        super().__init__("cancelled", "The export was cancelled.")


def _stop(proc: subprocess.Popen) -> None:
    """Kill and reap the child; never raises (the process may be gone already)."""
    try:
        if proc.poll() is None:
            proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


# How often the supervisor looks at Stop and the deadline while ffmpeg prints nothing.
_POLL_SECONDS = 0.1


def run_ffmpeg(
    command: Sequence[str],
    *,
    on_frame: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
    timeout: float = 3600.0,
    what: str = "ffmpeg",
) -> None:
    """Run ffmpeg with ``-progress pipe:1``; report ``frame=`` lines; kill on cancel.

    w32-rel (CUT-187, review R4; Media Encoder / Resolve keep Stop live whatever the encoder prints): the output is read
    by reader threads, and this thread supervises the PROCESS: Stop and the deadline are checked every _POLL_SECONDS,
    also while ffmpeg prints nothing or has closed its output but still runs. The child is killed and reaped in
    ``finally`` whatever ends the run."""
    args = [str(command[0]), "-hide_banner", "-nostdin", "-loglevel", "error", "-progress", "pipe:1", *map(str, command[1:])]
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=child_env(),
        )
    except FileNotFoundError as exc:
        raise ExportError("ffmpeg_unavailable", "ffmpeg is not available at the configured path.") from exc
    stderr_chunks: list[str] = []
    frames: queue.SimpleQueue[int] = queue.SimpleQueue()

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if sum(map(len, stderr_chunks)) < 16_000:
                stderr_chunks.append(line)

    def progress() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            if line.startswith("frame="):
                value = line.split("=", 1)[1].strip()
                if value.isdigit():
                    frames.put(int(value))

    readers = [threading.Thread(target=drain, daemon=True), threading.Thread(target=progress, daemon=True)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout

    def report() -> None:
        while True:
            try:
                frame = frames.get_nowait()
            except queue.Empty:
                return
            if on_frame is not None:
                on_frame(frame)

    try:
        while True:
            report()
            if should_cancel is not None and should_cancel():
                raise Cancelled()
            if proc.poll() is not None:
                break
            if time.monotonic() > deadline:
                raise ExportError("ffmpeg_timeout", f"{what} timed out.")
            try:
                proc.wait(timeout=_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        for reader in readers:
            reader.join(timeout=5)
        report()
        # A Stop that came while ffmpeg finished is still a Stop (nothing is kept).
        if should_cancel is not None and should_cancel():
            raise Cancelled()
    finally:
        _stop(proc)
    if proc.returncode != 0:
        tail = "".join(stderr_chunks).strip().splitlines()[-3:]
        raise ExportError("ffmpeg_failed", f"{what} failed.", {"stderr": tail})


def ffmpeg_version(ffmpeg: Path) -> str:
    try:
        proc = subprocess.run([str(ffmpeg), "-version"], capture_output=True, text=True, encoding="utf-8", timeout=30, env=child_env())
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    first = proc.stdout.splitlines()[0] if proc.stdout else ""
    return first[:120] or "unknown"
