from __future__ import annotations


# --- Timeline peaks: waveform peaks and preview sound of one cut source (lane fs-a3) ------------
#
# /projects/timeline/peaks answers, for one source the timeline uses (a sound file file:<sha256>, a
# take's own sound studio:..., or the pinned song by its sha256), min/max peaks per bucket of the
# decoded sound (extremes over every channel), the decoded
# sample count, rate and channels. ``sound: true`` adds the sound as lossless 16-bit FLAC (base64):
# the desktop's hermes-media:// door is not CORS-enabled, so the renderer's Web Audio preview mix
# cannot read a stream URL; it decodes these bytes instead. Read-only, path-free, through the same
# resolver as /projects/timeline/read (a source of another project or not ready reads offline).

_TIMELINE_PEAKS_MAX_SECONDS = 1800
_TIMELINE_PEAKS_SOUND_MAX_BYTES = 96 * 1024 * 1024


class TimelinePeaksRequest(_ProjectRequest):
    project_ref: ProjectReference
    ref: str = Field(min_length=1, max_length=200)
    buckets_per_second: int = Field(default=100, ge=1, le=1000)
    sound: bool = False


def _timeline_peaks_source(project_id: str, ref: str) -> dict[str, Any]:
    """The resolved fact of one ref (with its private path), or a ProjectError."""
    resolver = _timeline_module("resolver")
    lookups = _timeline_lookups(project_id)
    music_file = lookups.pop("music_file", None)
    if re.fullmatch(r"[0-9a-f]{64}", ref):
        fact = resolver.resolve_music(ref, music_file=music_file)
    else:
        fact = resolver.resolve_ref(ref, **lookups)
    if fact.get("status") != "online" or not fact.get("path"):
        raise studio_core.ProjectError("source_offline", f"{ref} is not available ({fact.get('reason') or 'offline'})")
    if fact.get("kind") in {"take", "media"} and not fact.get("audio") and not (fact.get("media_info") or {}).get("has_audio"):
        raise studio_core.ProjectError("no_sound", f"{ref} has no sound of its own")
    return fact


def _timeline_peaks_decode(path: str) -> tuple[Any, int, int]:
    """(float32 samples [n, channels], sample rate, channels) of the first sound stream, by ffmpeg."""
    probe = subprocess.run([str(FFPROBE_PATH), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels",
                            "-of", "json", path], capture_output=True, text=True, encoding="utf-8", timeout=120, check=False, shell=False, env=_child_env())
    streams = json.loads(probe.stdout or "{}").get("streams") or [] if probe.returncode == 0 else []
    if not streams:
        raise studio_core.ProjectError("no_sound", "the source has no sound")
    sample_rate, channels = int(streams[0].get("sample_rate") or 0), int(streams[0].get("channels") or 0)
    if sample_rate < 1 or channels < 1:
        raise studio_core.ProjectError("no_sound", "the source has no sound")
    argv = [str(FFMPEG_PATH), "-v", "error", "-i", path, "-map", "0:a:0", "-t", str(_TIMELINE_PEAKS_MAX_SECONDS),
            "-f", "f32le", "-acodec", "pcm_f32le", "-"]
    done = subprocess.run(argv, capture_output=True, timeout=600, check=False, shell=False, env=_child_env())
    if done.returncode != 0:
        raise studio_core.ProjectError("timeline_unavailable", "the sound could not be decoded")
    numpy = importlib.import_module("numpy")
    data = numpy.frombuffer(done.stdout, "<f4")
    return data[: len(data) - len(data) % channels].reshape(-1, channels), sample_rate, channels


def _timeline_peaks_flac(path: str, channels: int) -> dict[str, Any]:
    argv = [str(FFMPEG_PATH), "-v", "error", "-i", path, "-map", "0:a:0", "-vn", "-t", str(_TIMELINE_PEAKS_MAX_SECONDS),
            "-c:a", "flac", "-sample_fmt", "s16", "-compression_level", "0", "-map_metadata", "-1", "-f", "flac", "-"]
    done = subprocess.run(argv, capture_output=True, timeout=600, check=False, shell=False, env=_child_env())
    if done.returncode != 0 or done.stdout[:4] != b"fLaC":
        raise studio_core.ProjectError("timeline_unavailable", "the sound could not be prepared for the preview")
    if len(done.stdout) > _TIMELINE_PEAKS_SOUND_MAX_BYTES:
        raise studio_core.ProjectError("timeline_too_large", "the sound is too long for the preview")
    return {"mime": "audio/flac", "bytes": len(done.stdout), "channels": channels, "data_base64": base64.b64encode(done.stdout).decode("ascii")}


def _timeline_peaks(project_id: str, ref: str, per_second: int, sound: bool) -> dict[str, Any]:
    fact = _timeline_peaks_source(project_id, ref)
    path = str(fact["path"])
    samples, rate, channels = _timeline_peaks_decode(path)
    count = int(samples.shape[0])
    buckets = -(-count * per_second // rate) if count else 0
    # Each bucket's extremes over every channel (what a stereo waveform shows at its loudest side).
    peaks: list[list[float]] = []
    for index in range(buckets):
        block = samples[(index * rate) // per_second: min(((index + 1) * rate) // per_second, count)]
        peaks.append([round(float(block.min()), 5), round(float(block.max()), 5)])
    audio = fact.get("audio") or {}
    out: dict[str, Any] = {"ref": ref, "kind": fact.get("kind"), "samples": count, "sample_rate": rate, "channels": channels,
                           "duration_seconds": round(count / rate, 3), "buckets_per_second": per_second, "peaks": peaks,
                           "loudness": dict(audio.get("loudness") or {}) or None}
    if sound:
        out["sound"] = {"sample_rate": rate, **_timeline_peaks_flac(path, channels)}
    return out


@project_router.post("/projects/timeline/peaks")
async def projects_timeline_peaks(request: TimelinePeaksRequest) -> dict[str, Any]:
    try:
        _project_reference_directory(request.project_ref)
        return await asyncio.to_thread(_timeline_peaks, request.project_ref.project_id, request.ref, request.buckets_per_second, request.sound)
    except (studio_core.ProjectError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, (OSError, subprocess.TimeoutExpired)):
            exc = studio_core.ProjectError("timeline_unavailable", "the sound could not be read")
        return _timeline_error(exc)
