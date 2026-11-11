"""Speech regions of a sound file for dialogue ducking (lane fs-duck).

The detector is Silero VAD 6.2 (MIT, ONNX, CPU through onnxruntime), chosen by measurement
(``out/fs-duck-DESIGN.md``): on German dialogue over real effects it reaches F1 0.944 and
triggers on effects alone in 0.22 % of frames, against 57 % for the 200-3500 Hz band energy key.
It is installed beside Beat This! in ``<LOCALAPPDATA>/hermes/film-suite/engines/silero-vad``
(``silero_vad.onnx`` + ``engine.json`` with its sha256); ``FILM_SUITE_SPEECH_ENGINE_DIR`` overrides.

``regions_ms(path)`` answers ``[[start_ms, end_ms], ...]`` in the file's own time: 16 kHz mono, a
probability per 32 ms window, threshold 0.40 (tuned on held-out speakers), gaps under 200 ms
closed, regions under 100 ms dropped. The answer is cached as JSON per content sha256 + detector
id in a cache folder the caller names, so a re-run (or a second cut using the file) is instant.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env

ENGINE_ID = "silero-vad-6.2"
MODEL_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
THRESHOLD = 0.40
SAMPLE_RATE = 16000
WINDOW = 512  # 32 ms
CONTEXT = 64
GAP_MS = 200
MIN_MS = 100
MAX_SECONDS = 4 * 3600
DETECTOR = f"{ENGINE_ID}:t{THRESHOLD}:g{GAP_MS}:m{MIN_MS}"


class SpeechError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def engine_dir() -> Path:
    override = os.environ.get("FILM_SUITE_SPEECH_ENGINE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "hermes" / "film-suite" / "engines" / "silero-vad"


def model_path() -> Path:
    path = engine_dir() / "silero_vad.onnx"
    if not path.is_file():
        raise SpeechError("speech_engine_missing", "The speech detector is not installed (film-suite/engines/silero-vad).")
    return path


_SESSION: dict[str, Any] = {}


def _session() -> Any:
    path = model_path()
    key = str(path)
    if key not in _SESSION:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != MODEL_SHA256:
            raise SpeechError("speech_engine_mismatch", "The installed speech detector is not the pinned Silero VAD 6.2 model.")
        # Lane p12-cutc: a runtime that cannot be imported or loaded is the detector missing, never a crash
        # (onnxruntime is not a declared dependency; an environment without it must still edit).
        try:
            ort = importlib.import_module("onnxruntime")
            options = ort.SessionOptions()
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            options.log_severity_level = 4
            session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=options)
        except Exception as exc:  # noqa: BLE001 - ImportError, a broken DLL, a runtime that refuses the model
            raise SpeechError("speech_engine_missing", "The speech detector's runtime (onnxruntime) is not available.") from exc
        _SESSION.clear()
        _SESSION[key] = session
    return _SESSION[key]


_AVAILABLE: dict[tuple[str, int, int], bool] = {}


def available() -> bool:
    """Cheap, inference-free check (cached per process per model file): the pinned model is installed and the runtime
    imports. Used by the read to keep naming the ducking notice after a restart (lane p12-cutc)."""
    try:
        path = model_path()
        stat = path.stat()
    except (SpeechError, OSError):
        return False
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if str(path) in _SESSION:
        return True
    if key not in _AVAILABLE:
        try:
            ok = hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA256
            if ok:
                importlib.import_module("onnxruntime")
        except Exception:  # noqa: BLE001 - any failure here means the detector cannot run
            ok = False
        _AVAILABLE[key] = ok
    return _AVAILABLE[key]


def decode(path: str | Path, ffmpeg: str | Path) -> Any:
    """The first sound stream as 16 kHz mono float32 (ffmpeg)."""
    argv = [str(ffmpeg), "-v", "error", "-i", str(path), "-map", "0:a:0", "-vn", "-t", str(MAX_SECONDS), "-ac", "1",
            "-ar", str(SAMPLE_RATE), "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    try:
        done = subprocess.run(argv, capture_output=True, timeout=900, check=False, shell=False, env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpeechError("speech_failed", "The sound could not be read for speech detection.") from exc
    if done.returncode != 0:
        raise SpeechError("speech_failed", "The sound could not be read for speech detection.")
    numpy = importlib.import_module("numpy")
    return numpy.frombuffer(done.stdout, "<f4").copy()


def probabilities(samples: Any) -> Any:
    """Speech probability per 32 ms window (Silero's streaming state carried across windows)."""
    numpy = importlib.import_module("numpy")
    session = _session()
    pad = (-len(samples)) % WINDOW
    chunks = numpy.concatenate([samples, numpy.zeros(pad, numpy.float32)]).reshape(-1, WINDOW)
    state = numpy.zeros((2, 1, 128), numpy.float32)
    context = numpy.zeros((1, CONTEXT), numpy.float32)
    rate = numpy.array(SAMPLE_RATE, numpy.int64)
    out = numpy.empty(len(chunks), numpy.float32)
    for index, chunk in enumerate(chunks):
        frame = numpy.concatenate([context, chunk[None]], 1)
        prob, state = session.run(None, {"input": frame, "state": state, "sr": rate})
        out[index] = float(prob[0, 0])
        context = chunk[None, -CONTEXT:]
    return out


def regions_from(probs: Any, total_samples: int) -> list[list[int]]:
    """Thresholded windows -> [start_ms, end_ms] regions (gaps < GAP_MS closed, regions < MIN_MS dropped)."""
    window_ms = WINDOW * 1000 / SAMPLE_RATE
    total_ms = int(total_samples * 1000 // SAMPLE_RATE)
    regions: list[list[int]] = []
    start = None
    for index, prob in enumerate(list(probs) + [0.0]):
        if prob >= THRESHOLD and start is None:
            start = index
        elif prob < THRESHOLD and start is not None:
            a, b = int(start * window_ms), min(total_ms, int(round(index * window_ms)))
            if regions and a - regions[-1][1] < GAP_MS:
                regions[-1][1] = b
            else:
                regions.append([a, b])
            start = None
    return [r for r in regions if r[1] - r[0] >= MIN_MS and r[1] > r[0]]


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def regions_ms(path: str | Path, *, ffmpeg: str | Path, cache_dir: Path | None = None, sha256: str | None = None) -> dict[str, Any]:
    """{sha256, detector, regions: [[start_ms, end_ms], ...], cached} of one sound file."""
    sha = sha256 or file_sha256(path)
    cache = cache_dir / f"{sha}.json" if cache_dir is not None else None
    if cache is not None and cache.is_file():
        try:
            record = json.loads(cache.read_text(encoding="utf-8"))
            if record.get("detector") == DETECTOR and record.get("sha256") == sha and isinstance(record.get("regions"), list):
                return {**record, "cached": True}
        except (OSError, ValueError):
            pass
    samples = decode(path, ffmpeg)
    record = {"sha256": sha, "detector": DETECTOR, "regions": regions_from(probabilities(samples), len(samples))}
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, cache)
    return {**record, "cached": False}
