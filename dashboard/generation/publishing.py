"""Publish validated ComfyUI outputs as Studio-owned files.

The ledger owns publication state and leases (job_store).  This module owns
the safety-critical pipeline that converts a declared output descriptor into a
validated, hash-recorded file inside the frozen destination:

  require_unique_output -> validate_frozen_destination -> fetch_output_to_staging
  -> probe_media -> copy to sibling part -> no-clobber rename -> release lease

Every path here is a Studio-owned name created by this process; the ComfyUI
original is never modified or deleted.  On any failure only Studio temporary
files are removed.
"""

from __future__ import annotations
from .legacy_plugin_migration import state_root


import json
import ntpath
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env

try:
    from hermes_constants import get_hermes_home
except ImportError:  # pragma: no cover - repository tests install Hermes' root
    def get_hermes_home() -> Path:
        return Path.home() / "AppData" / "Local" / "hermes"


from .ffmpeg_locate import locate as _locate_ffmpeg_tool  # w20-cat: env, PATH, <SystemDrive>\\ffmpeg\\bin

DEFAULT_FFPROBE = _locate_ffmpeg_tool("ffprobe")
DEFAULT_STAGING_ROOT = state_root(Path(get_hermes_home()) / "cache") / "staging"
DEFAULT_MAX_STAGED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
STALE_STAGING_MAX_AGE_SECONDS = 24 * 3600
LEASE_RECLAIM_SECONDS = 300.0
OUTPUT_NODE_ID = "92"
OUTPUT_ACCEPTED_KEYS = ("images", "videos", "video")
EXPECTED_FPS = 24.0
FPS_TOLERANCE = 0.01
DURATION_TOLERANCE_FRAMES = 2


class PublishingError(ValueError):
    """Stable failure with a finite public reason code."""

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        self.reason_code = reason_code
        self.public_message = message or reason_code
        ValueError.__init__(self, message or reason_code)


@dataclass(frozen=True)
class MediaProbe:
    container_mp4: bool
    has_video_stream: bool
    has_audio_stream: bool
    average_fps: float
    duration_seconds: float
    frame_count: int


def _output_section(manifest: Mapping[str, Any]) -> dict[str, Any]:
    output = manifest.get("output")
    if not isinstance(output, dict):
        raise PublishingError("output_invalid", "workflow manifest declares no output contract")
    return output


def _descriptor_key(descriptor: Mapping[str, Any]) -> tuple[str, str, str]:
    filename = descriptor.get("filename")
    subfolder = descriptor.get("subfolder")
    kind = descriptor.get("type")
    if not all(isinstance(value, str) for value in (filename, subfolder, kind)):
        raise PublishingError("output_invalid", "output descriptor fields must be text")
    return (filename, subfolder, kind)


def _reject_traversal(filename: str) -> None:
    if not filename or filename != os.path.basename(filename):
        raise PublishingError("output_invalid", "output filename must be a bare basename")
    if filename in (".", "..") or ntpath.basename(filename) != filename:
        raise PublishingError("output_invalid", "output filename must be a bare basename")
    if "\x00" in filename or "\\" in filename or "/" in filename:
        raise PublishingError("output_invalid", "output filename must be a bare basename")


def require_unique_output(history_entry: Any, manifest: Mapping[str, Any]) -> dict[str, str]:
    """Read node-92 descriptors, dedupe identical tuples, require exactly one.

    Accepted keys come from the manifest output contract (images/videos/video).
    The single surviving descriptor is validated against the manifest contract
    before it is returned.
    """
    output = _output_section(manifest)
    node_id = str(output.get("node_id", OUTPUT_NODE_ID))
    accepted_keys = tuple(output.get("accepted_keys", OUTPUT_ACCEPTED_KEYS))
    legacy = output.get("legacy_output")
    if output.get("path_text_folder") is not None:
        # lane fs-obvb: Contract-3 rows only ever ran the SaveVideo graph; read them by its output.
        if not isinstance(legacy, Mapping):
            raise PublishingError("output_invalid", "workflow manifest declares no SaveVideo output")
        node_id = str(legacy.get("node_id"))
        accepted_keys = tuple(legacy.get("accepted_keys") or OUTPUT_ACCEPTED_KEYS)

    if not isinstance(history_entry, dict):
        raise PublishingError("output_missing", "no ComfyUI history entry for the prompt")
    outputs = history_entry.get("outputs")
    if not isinstance(outputs, dict):
        raise PublishingError("output_missing", "history entry carries no outputs")
    node_outputs = outputs.get(node_id)
    if not isinstance(node_outputs, dict):
        raise PublishingError("output_missing", "history entry carries no node output")

    seen: set[tuple[str, str, str]] = set()
    unique: list[dict[str, str]] = []
    for key in accepted_keys:
        descriptors = node_outputs.get(key)
        if not isinstance(descriptors, list):
            continue
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                raise PublishingError("output_invalid", "output descriptor must be an object")
            key_tuple = _descriptor_key(descriptor)
            if key_tuple in seen:
                continue
            seen.add(key_tuple)
            unique.append({name: descriptor.get(name) for name in ("filename", "subfolder", "type")})

    if not unique:
        raise PublishingError("output_missing", "no declared output in node output")
    if len(unique) > 1:
        raise PublishingError("output_invalid", "ambiguous: multiple distinct declared outputs")

    descriptor = unique[0]
    _reject_traversal(descriptor["filename"])
    required_type = str(output.get("required_folder_type", "output"))
    if descriptor["type"] != required_type:
        raise PublishingError("output_invalid", "output descriptor type is not the required folder type")
    extension = str(output.get("extension", ".mp4"))
    if not descriptor["filename"].endswith(extension):
        raise PublishingError("output_invalid", "output filename does not end with the required extension")
    return descriptor


def validate_frozen_destination(path_text: str, *, now: float | None = None) -> Path:
    """Require an existing absolute local directory; reject UNC/device/reparse."""
    if not isinstance(path_text, str) or not path_text.strip():
        raise PublishingError("invalid_request", "frozen destination must be a non-empty path")
    if path_text.startswith("\\\\") or path_text.startswith("//"):
        raise PublishingError("invalid_request", "UNC paths are not accepted as frozen destinations")
    candidate = Path(path_text)
    drive, tail = ntpath.splitdrive(path_text)
    if not candidate.is_absolute():
        raise PublishingError("invalid_request", "frozen destination must be absolute")
    if not drive or not tail:
        raise PublishingError("invalid_request", "frozen destination must be a local drive path")
    if not candidate.exists():
        raise PublishingError("invalid_request", "frozen destination does not exist")
    if not candidate.is_dir():
        raise PublishingError("invalid_request", "frozen destination is not a directory")
    # Fail closed on any symlink / reparse point: the resolved location must be
    # exactly the destination itself.
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PublishingError("invalid_request", "frozen destination cannot be resolved") from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate.absolute())):
        raise PublishingError("invalid_request", "frozen destination resolves through a reparse point")
    return candidate


def _next_candidate(destination: Path, basename: str) -> Path:
    """Return the first non-existent candidate with a collision suffix."""
    if not basename or basename != os.path.basename(basename):
        raise PublishingError("invalid_request", "requested basename must be a bare filename")
    stem, dot, suffix = basename.rpartition(".")
    base = stem if dot else basename
    ext = f".{suffix}" if dot else ""
    candidate = destination / basename
    index = 1
    while candidate.exists():
        candidate = destination / f"{base}-{index}{ext}"
        index += 1
    return candidate


async def fetch_output_to_staging(
    client: Any,
    descriptor: Mapping[str, str],
    staging_root: Path,
    *,
    max_staged_bytes: int = DEFAULT_MAX_STAGED_BYTES,
) -> Path:
    """Fetch /view output into a UUID staging file; never keep partials."""
    staging_root = Path(staging_root)
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PublishingError("invalid_request", "staging directory cannot be created") from exc
    staged = staging_root / f"{uuid.uuid4().hex}.mp4"
    try:
        payload = await client.view(
            {
                "filename": descriptor["filename"],
                "subfolder": descriptor.get("subfolder", ""),
                "type": descriptor["type"],
            }
        )
        if not isinstance(payload, (bytes, bytearray)) or not payload:
            raise PublishingError("output_missing", "view returned no media bytes")
        if len(payload) > max_staged_bytes:
            raise PublishingError("output_invalid", "staged output exceeds the maximum allowed size")
        staged.write_bytes(payload)
        return staged
    except PublishingError:
        staged.unlink(missing_ok=True)
        raise
    except Exception as exc:
        staged.unlink(missing_ok=True)
        raise PublishingError("unreachable", "output could not be fetched from ComfyUI") from exc


def _parse_frame_rate(raw: Any) -> float:
    if not isinstance(raw, str) or "/" not in raw:
        return 0.0
    try:
        numerator, denominator = raw.split("/", 1)
        denom = float(denominator)
        if denom == 0:
            return 0.0
        return float(numerator) / denom
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def probe_media(
    path: Path,
    *,
    expected_duration_seconds: float,
    ffprobe_path: str | None = None,
) -> MediaProbe:
    """Probe staged media; any failed criterion raises output_invalid."""
    ffprobe = ffprobe_path or DEFAULT_FFPROBE
    try:
        proc = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration:stream=codec_type,avg_frame_rate,r_frame_rate,nb_frames,duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=60, env=child_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PublishingError("output_invalid", "ffprobe could not run") from exc
    if proc.returncode != 0:
        raise PublishingError("output_invalid", "ffprobe rejected the staged media")
    try:
        payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except (TypeError, ValueError) as exc:
        raise PublishingError("output_invalid", "ffprobe output was unreadable") from exc

    format_payload = payload.get("format") if isinstance(payload, dict) else None
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        raise PublishingError("output_invalid", "ffprobe reported no streams")

    container_mp4 = False
    if isinstance(format_payload, dict):
        format_name = format_payload.get("format_name")
        container_mp4 = isinstance(format_name, str) and "mp4" in format_name.lower()

    video_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "video"),
        None,
    )
    audio_stream = next(
        (stream for stream in streams if isinstance(stream, dict) and stream.get("codec_type") == "audio"),
        None,
    )
    if not isinstance(video_stream, dict):
        raise PublishingError("output_invalid", "no decodable video stream")
    if not isinstance(audio_stream, dict):
        raise PublishingError("output_invalid", "no audio stream for H3")

    average_fps = (
        _parse_frame_rate(video_stream.get("avg_frame_rate"))
        or _parse_frame_rate(video_stream.get("r_frame_rate"))
    )
    if average_fps <= 0:
        raise PublishingError("output_invalid", "video frame rate could not be read")

    duration_seconds = 0.0
    for raw_duration in (video_stream.get("duration"), format_payload.get("duration") if isinstance(format_payload, dict) else None):
        try:
            candidate_duration = float(raw_duration)
        except (TypeError, ValueError):
            continue
        if candidate_duration > 0:
            duration_seconds = candidate_duration
            break
    if duration_seconds <= 0:
        raise PublishingError("output_invalid", "media duration could not be read")

    frame_count = 0
    try:
        frame_count = int(float(video_stream.get("nb_frames")))
    except (TypeError, ValueError):
        frame_count = 0

    if not container_mp4:
        raise PublishingError("output_invalid", "staged media is not an MP4 container")
    fps_epsilon = 1e-9
    if abs(average_fps - EXPECTED_FPS) > FPS_TOLERANCE + fps_epsilon:
        raise PublishingError("output_invalid", "video frame rate differs from the expected 24 fps")
    frame_tolerance = DURATION_TOLERANCE_FRAMES / EXPECTED_FPS + 1e-9
    if abs(duration_seconds - expected_duration_seconds) > frame_tolerance:
        raise PublishingError("output_invalid", "media duration differs from the recorded effective duration")

    return MediaProbe(
        container_mp4=container_mp4,
        has_video_stream=True,
        has_audio_stream=True,
        average_fps=average_fps,
        duration_seconds=duration_seconds,
        frame_count=frame_count,
    )


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_file(staged: Path, destination: Path, requested_basename: str) -> tuple[Path, str]:
    """Copy to a flushed sibling part, then no-clobber rename to the candidate.

    If the candidate appears between selection and rename, choose the next
    collision suffix and retry.  The final candidate is never overwritten.
    """
    attempts = 0
    while True:
        candidate = _next_candidate(destination, requested_basename)
        part = destination / f".{uuid.uuid4().hex}.part"
        try:
            with open(staged, "rb") as src, open(part, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            # Same-directory rename is the atomic publication boundary.  Never
            # use os.replace: a pre-existing candidate must never be
            # overwritten.
            os.rename(part, candidate)
            break
        except FileExistsError:
            part.unlink(missing_ok=True)
            attempts += 1
            if attempts > 100:
                raise PublishingError(
                    "invalid_request", "media could not be published into the destination"
                ) from None
            continue
        except OSError as exc:
            part.unlink(missing_ok=True)
            raise PublishingError(
                "invalid_request", "media could not be published into the destination"
            ) from exc
    return candidate, _sha256(candidate)


def cleanup_stale_staging(
    staging_root: Path,
    *,
    now: float | None = None,
    max_age_seconds: float = STALE_STAGING_MAX_AGE_SECONDS,
) -> int:
    """Remove only UUID-named staging files older than max_age_seconds."""
    timestamp = now if now is not None else time.time()
    removed = 0
    staging_root = Path(staging_root)
    if not staging_root.is_dir():
        return 0
    for entry in staging_root.iterdir():
        if not entry.is_file():
            continue
        stem = entry.stem
        if len(stem) != 32:
            continue
        try:
            int(stem, 16)
        except ValueError:
            continue
        try:
            if timestamp - entry.stat().st_mtime > max_age_seconds:
                entry.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def _expected_duration_seconds(manifest: Mapping[str, Any], duration_seconds: Any) -> float:
    """Return the recorded effective duration for the requested seconds.

    The manifest records ``effective_frames`` via the named rule
    ``round_fps_then_align_modulo``; the probe baseline is
    ``effective_frames / fps``, not the raw requested seconds.
    """
    from .comfyui_provider import _round_fps_then_align_modulo

    try:
        requested = float(duration_seconds)
    except (TypeError, ValueError):
        raise PublishingError("invalid_request", "recorded duration is not numeric")
    duration_capability = manifest.get("capabilities", {}).get("duration", {})
    try:
        fps = float(duration_capability.get("fps", 24.0))
    except (TypeError, ValueError):
        fps = 24.0
    if fps <= 0:
        raise PublishingError("invalid_request", "manifest fps is invalid")
    frames_rule = duration_capability.get("effective_frames")
    if not isinstance(frames_rule, dict):
        raise PublishingError("invalid_request", "manifest effective_frames rule is missing")
    try:
        effective_frames = _round_fps_then_align_modulo(
            requested,
            fps=fps,
            minimum_frames=frames_rule.get("minimum_frames", 1),
            modulus=frames_rule.get("modulus", 1),
            remainder=frames_rule.get("remainder", 0),
        )
    except (TypeError, ValueError) as exc:
        raise PublishingError("invalid_request", "manifest effective_frames rule is invalid") from exc
    return effective_frames / fps


async def publish_ready_output(
    *,
    job_store: Any,
    client: Any,
    manifest: Mapping[str, Any],
    studio_job_id: str,
    staging_root: Path,
    ffprobe_path: str | None = None,
    owner_id: str = "studio-publisher",
    now: float | None = None,
    max_staged_bytes: int = DEFAULT_MAX_STAGED_BYTES,
):
    """Publish one ready Studio job's declared output safely.

    Publication is a ledger transition guarded by a transactional lease.  The
    frozen destination is read from the job record and validated here.  A
    repeated call returns the stored publication without touching Comfy.
    """
    record = job_store.get_job(studio_job_id)
    if record.publication_state == "published":
        return record
    if record.state != "ready":
        return record

    destination_path = validate_frozen_destination(record.frozen_destination, now=now)
    stale = job_store.get_job(studio_job_id)
    lease_held = (
        stale.publication_lease_owner is not None
        and stale.publication_lease_token is not None
        and stale.publication_lease_expires_at is not None
    )
    timestamp = now if now is not None else time.time()
    if lease_held and stale.publication_lease_expires_at <= timestamp:
        # A crashed worker's lease has expired.  The plan allows reclaim only
        # after checking/removing its Studio-owned temp: sweep UUID staging
        # files older than the lease window before taking the lease.
        cleanup_stale_staging(
            staging_root, now=timestamp, max_age_seconds=LEASE_RECLAIM_SECONDS
        )
    lease = job_store.acquire_publication_lease(
        studio_job_id, owner_id, now=now, lease_seconds=LEASE_RECLAIM_SECONDS
    )
    if lease is None:
        refreshed = job_store.get_job(studio_job_id)
        if refreshed.publication_state == "published":
            return refreshed
        return refreshed

    try:
        history = await client.history(record.provider_job_id)
        entry = history.get(record.provider_job_id)
        if entry is None:
            raise PublishingError("output_missing", "history entry is missing for the owned prompt")
        descriptor = require_unique_output(entry, manifest)

        staged = await fetch_output_to_staging(
            client,
            descriptor,
            staging_root,
            max_staged_bytes=max_staged_bytes,
        )
        try:
            expected_duration = _expected_duration_seconds(
                manifest, record.request.get("args", {}).get("duration_seconds", 5)
            )
            probe_media(
                staged,
                expected_duration_seconds=expected_duration,
                ffprobe_path=ffprobe_path,
            )
            source_sha = _sha256(staged)
            published_path, published_sha = _publish_file(
                staged, destination_path, record.requested_basename
            )
        finally:
            staged.unlink(missing_ok=True)

        return job_store.release_publication_lease(
            studio_job_id,
            owner_id,
            lease.token,
            now=now,
            published_path=str(published_path),
            source_media_sha256=source_sha,
            published_file_sha256=published_sha,
        )
    except Exception:
        try:
            job_store.release_publication_lease(
                studio_job_id, owner_id, lease.token, now=now
            )
        except Exception:
            pass
        raise


__all__ = [
    "DEFAULT_FFPROBE",
    "DEFAULT_STAGING_ROOT",
    "DEFAULT_MAX_STAGED_BYTES",
    "STALE_STAGING_MAX_AGE_SECONDS",
    "EXPECTED_FPS",
    "PublishingError",
    "MediaProbe",
    "require_unique_output",
    "validate_frozen_destination",
    "fetch_output_to_staging",
    "probe_media",
    "cleanup_stale_staging",
    "publish_ready_output",
    "_next_candidate",
    "_sha256",
    "_expected_duration_seconds",
]
