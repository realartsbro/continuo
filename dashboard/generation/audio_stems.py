"""Stems of one sound file on the local ComfyUI (lane w3-audio, board E15 Edit ⑤/⑥).

Two pinned graphs in workflows/audio-stems-v1 (manifest + sha256), only node packs already installed in the
artist's ComfyUI: "Vocals + rest" = Mel-Band RoFormer, "4 stems" = Hybrid Demucs (AudioSeparation). The source is
uploaded as a temp input, the graph is submitted after GET /queue (appended behind whatever runs; never cancelled,
never reordered), the stems come back through /view, are decoded with ffmpeg and stored per (content sha256, split)
under the Suite's state folder, with what the separation measured: every stem as long as the mix, and the residual
of mix - sum(stems) in dB below the mix. The ComfyUI side is injected (``comfy``) so this is tested offline.
"""
from __future__ import annotations

import hashlib
import json
import math
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



class _LazyNumpy:
    """numpy on first use (lane w19-g1fresh, BCK-007): on a fresh machine Hermes' package manager publishes the
    plugin's dependency environment after the first boot, and this module is imported by ``plugin_api.py`` at
    mount, so a top-level ``import numpy`` took every Continuo route down until a restart."""

    def __getattr__(self, name: str) -> Any:
        import numpy  # noqa: PLC0415 - deliberately lazy, see class docstring

        return getattr(numpy, name)


np = _LazyNumpy()

SCHEMA = "studio.audio-stems/1"
SPLITS = ("vocals_rest", "four")
_HERE = Path(__file__).resolve()
WORKFLOW_ROOT = next((p / "workflows" / "audio-stems-v1" for p in (_HERE.parents[4], _HERE.parents[1]) if (p / "workflows" / "audio-stems-v1").is_dir()), _HERE.parents[4] / "workflows" / "audio-stems-v1")
POLL_SECONDS = 1.0
TIMEOUT_SECONDS = 1800.0


class StemsError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def manifest(root: Path | None = None) -> dict[str, Any]:
    folder = root or WORKFLOW_ROOT
    try:
        return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StemsError("stems_workflow_unreadable", "The stems workflow is unreadable") from exc


def load_split(split: str, root: Path | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """The verified (split entry, graph) of one split; the graph's bytes must match the pinned sha256."""
    if split not in SPLITS:
        raise StemsError("stems_split_unknown", "Split is 'vocals_rest' or 'four'")
    folder = root or WORKFLOW_ROOT
    entry = manifest(folder)["splits"][split]
    raw = (folder / entry["api_file"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != entry["api_sha256"]:
        raise StemsError("stems_workflow_changed", "The pinned stems graph does not match its sha256")
    return entry, json.loads(raw.decode("utf-8"))


def compile_graph(split: str, source_name: str, root: Path | None = None) -> dict[str, Any]:
    entry, graph = load_split(split, root)
    _blank, node, _inputs, key = entry["source_pointer"].split("/")
    graph[node]["inputs"][key] = source_name
    for item in graph.values():
        item.pop("_meta", None)
    return graph


def health(split: str, object_info: Mapping[str, Any] | None, models: Mapping[str, Any] | None) -> dict[str, Any]:
    """Ready when every class is live in ComfyUI and every model file is listed (never a download)."""
    entry, _graph = load_split(split)
    if object_info is None:
        return {"ready": False, "reasons": ["The local engine is not reachable"]}
    reasons = [f"{name} is not installed in ComfyUI" for name in entry["required_classes"] if name not in object_info]
    for model in entry["models"]:
        listed = (models or {}).get(model["folder"]) or []
        if model["name"] not in listed:
            reasons.append(f"{model['name']} is not in {model['folder']}")
    return {"ready": not reasons, "reasons": reasons}


def decode(path: Path, ffmpeg: str | os.PathLike[str], rate: int | None = None, channels: int | None = None) -> tuple[np.ndarray, int]:
    """Float32 samples (frames x channels) of a sound file, optionally resampled / remixed by ffmpeg."""
    probe = [str(ffmpeg), "-v", "error", "-i", str(path)]
    args = probe + (["-ar", str(rate)] if rate else []) + (["-ac", str(channels)] if channels else []) + ["-f", "f32le", "-acodec", "pcm_f32le", "-"]
    done = subprocess.run(args, capture_output=True, timeout=600, check=False, env=child_env())
    if done.returncode != 0:
        raise StemsError("stems_decode_failed", "A sound file could not be decoded")
    if rate is None or channels is None:
        info = subprocess.run([str(Path(ffmpeg).with_name("ffprobe" + Path(ffmpeg).suffix)), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels", "-of", "json", str(path)],
                              capture_output=True, text=True, timeout=60, check=False, env=child_env())
        stream = (json.loads(info.stdout or "{}").get("streams") or [{}])[0]
        rate, channels = rate or int(stream.get("sample_rate") or 44100), channels or int(stream.get("channels") or 2)
    data = np.frombuffer(done.stdout, dtype="<f4")
    return data[: (data.size // channels) * channels].reshape(-1, channels), int(rate)


def peaks(samples: np.ndarray, buckets: int = 400) -> list[list[float]]:
    mono = samples.mean(axis=1) if samples.ndim == 2 else samples
    if mono.size == 0:
        return []
    edges = np.linspace(0, mono.size, num=min(buckets, mono.size) + 1).astype(int)
    return [[round(float(mono[a:b].min()), 4), round(float(mono[a:b].max()), 4)] for a, b in zip(edges[:-1], edges[1:]) if b > a]


def measure(mix: np.ndarray, stems: Mapping[str, np.ndarray], rate: int = 48000) -> dict[str, Any]:
    """Each stem's length against the mix, and the residual of mix - sum(stems) in dB below the mix (RMS)."""
    frames = {sid: int(arr.shape[0]) for sid, arr in stems.items()}
    n = min([mix.shape[0], *frames.values()])
    total = sum(arr[:n] for arr in stems.values())
    rms = lambda a: float(np.sqrt(np.mean(np.square(a, dtype=np.float64)))) if a.size else 0.0
    mix_rms, res_rms = rms(mix[:n]), rms(mix[:n] - total)
    residual_db = round(20 * math.log10(res_rms / mix_rms), 1) if mix_rms > 0 and res_rms > 0 else (-200.0 if mix_rms > 0 else None)
    return {"mix_frames": int(mix.shape[0]), "stem_frames": frames, "lengths_equal": all(f == mix.shape[0] for f in frames.values()),
            "max_length_difference_ms": round(max(abs(f - mix.shape[0]) for f in frames.values()) * 1000 / rate, 3) if frames else None,
            "residual_db": residual_db}


def write_flac(path: Path, samples: np.ndarray, rate: int, ffmpeg: str | os.PathLike[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp.flac")
    done = subprocess.run([str(ffmpeg), "-v", "error", "-y", "-f", "f32le", "-ar", str(rate), "-ac", str(samples.shape[1]), "-i", "-", "-c:a", "flac", "-sample_fmt", "s32", str(temporary)],
                          input=np.ascontiguousarray(samples, dtype="<f4").tobytes(), capture_output=True, timeout=600, check=False, env=child_env())
    if done.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise StemsError("stems_write_failed", "A stem could not be written")
    os.replace(temporary, path)


def run(*, source: Path, split: str, comfy: Any, out_dir: Path, ffmpeg: str | os.PathLike[str],
        on_progress: Callable[[float, str], None] = lambda _f, _s: None, should_stop: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """One separation, blocking. Returns the stored result (stems with seconds and peaks, measured)."""
    started = clock()
    entry, _graph = load_split(split)
    data = source.read_bytes()
    name = hashlib.sha256(data).hexdigest() + source.suffix.lower()
    on_progress(0.02, "uploading")
    uploaded = comfy.upload(data, name)
    graph = compile_graph(split, uploaded)
    comfy.queue()  # read before the submit; we only ever append behind what is there
    prompt_id = comfy.submit(graph)
    on_progress(0.05, "waiting")
    while True:
        done = comfy.history(prompt_id)
        if done is not None:
            break
        if should_stop():
            raise StemsError("stems_stopped", "Stopped waiting (the engine finishes it on its own)")
        live = comfy.progress(prompt_id)
        on_progress(0.05 + 0.8 * (live if isinstance(live, (int, float)) else 0.0), "separating")
        if clock() - started > TIMEOUT_SECONDS:
            raise StemsError("stems_timeout", "The separation took too long")
        sleep(POLL_SECONDS)
    status = done.get("status") or {}
    if status.get("status_str") == "error":
        messages = [m for m in status.get("messages") or [] if isinstance(m, list) and m and m[0] == "execution_error"]
        detail = (messages[0][1] or {}).get("exception_message", "") if messages else ""
        raise StemsError("stems_engine_error", ("The engine stopped: " + str(detail).strip().splitlines()[0][:160]) if detail else "The engine stopped")
    on_progress(0.9, "measuring")
    out_dir.mkdir(parents=True, exist_ok=True)
    stems: dict[str, np.ndarray] = {}
    rate = None
    for output in entry["outputs"]:
        files = ((done.get("outputs") or {}).get(output["node_id"]) or {}).get("audio") or []
        if not files:
            raise StemsError("stems_missing", f"The engine made no {output['label']}")
        raw = out_dir / f".{output['id']}.download.flac"
        raw.write_bytes(comfy.view(files[0]))
        samples, rate_here = decode(raw, ffmpeg)
        raw.unlink(missing_ok=True)
        rate = rate or rate_here
        stems[output["id"]] = samples
        write_flac(out_dir / f"{output['id']}.flac", samples, rate_here, ffmpeg)
    channels = next(iter(stems.values())).shape[1]
    mix, _r = decode(source, ffmpeg, rate=rate, channels=channels)
    result = {"schema": SCHEMA, "split": split, "label": entry["label"], "model_label": entry["model_label"], "sample_rate": rate, "channels": channels,
              "seconds": round(mix.shape[0] / rate, 3), "prompt_id": prompt_id, "took_seconds": round(clock() - started, 2),
              "stems": [{"id": o["id"], "label": o["label"], "seconds": round(stems[o["id"]].shape[0] / rate, 3), "peaks": peaks(stems[o["id"]])} for o in entry["outputs"]],
              "measured": measure(mix, stems, rate), "made_at": time.time()}
    (out_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
    on_progress(1.0, "done")
    return result


def stored(out_dir: Path) -> dict[str, Any] | None:
    try:
        result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return result if all((out_dir / f"{s['id']}.flac").is_file() for s in result.get("stems") or []) else None


def mix_audible(out_dir: Path, result: Mapping[str, Any], audible: list[str], ffmpeg: str | os.PathLike[str], scratch: Path) -> bytes:
    """The sum of the audible stems as FLAC bytes (Solo / Mute playback)."""
    ids = [s["id"] for s in result["stems"] if s["id"] in audible]
    if not ids:
        raise StemsError("stems_all_muted", "Every stem is muted")
    total = None
    for sid in ids:
        samples, rate = decode(out_dir / f"{sid}.flac", ffmpeg)
        total = samples if total is None else total[: min(len(total), len(samples))] + samples[: min(len(total), len(samples))]
    target = scratch / f"stems-{uuid.uuid4().hex}.flac"
    write_flac(target, total, int(result["sample_rate"]), ffmpeg)
    try:
        return target.read_bytes()
    finally:
        target.unlink(missing_ok=True)


__all__ = ["SCHEMA", "SPLITS", "StemsError", "compile_graph", "decode", "health", "load_split", "manifest", "measure", "mix_audible", "peaks", "run", "stored", "write_flac"]
