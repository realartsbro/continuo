from __future__ import annotations


def _local_path(path: str) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise RecipeError("A local path is required.")
    if path.strip().lower().startswith(("http://", "https://", "data:")):
        raise RecipeError("Recipe metadata requires a local video file, not a URL.")
    candidate = Path(os.path.expanduser(path.strip()))
    if not candidate.is_file():
        raise RecipeError("The local video file does not exist.")
    return candidate.resolve()


def _run(command: list[str], *, failure_message: str = "Media command failed.") -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=True, timeout=120, env=_child_env())
    except FileNotFoundError as exc:
        raise RecipeError("ffmpeg/ffprobe is not available at the configured path.") from exc
    except subprocess.CalledProcessError as exc:
        raise RecipeError(failure_message) from exc
    except subprocess.TimeoutExpired as exc:
        raise RecipeError("The media operation timed out.") from exc


def _parse_frame_rate(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value) if float(value) > 0 else 0.0
    if not isinstance(value, str) or not value.strip():
        return 0.0
    numerator, separator, denominator = value.partition("/")
    try:
        if separator:
            result = float(numerator) / float(denominator)
        else:
            result = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0
    return result if result > 0 else 0.0


def probe_media(path: Path | str) -> dict[str, Any]:
    """Return stream count, duration, real video FPS, and frame count."""
    candidate = Path(path)
    result = _run(
        [
            str(FFPROBE_PATH),
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,avg_frame_rate,r_frame_rate,nb_frames,duration",
            "-of",
            "json",
            str(candidate),
        ]
    )
    try:
        payload = json.loads(result.stdout)
        format_payload = payload.get("format", {})
        streams = payload.get("streams", [])
        video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        if not isinstance(video_stream, dict):
            raise RecipeError("ffprobe found no video stream.")

        duration = 0.0
        for raw_duration in (video_stream.get("duration"), format_payload.get("duration")):
            try:
                candidate_duration = float(raw_duration)
            except (TypeError, ValueError):
                continue
            if candidate_duration > 0:
                duration = candidate_duration
                break

        fps = _parse_frame_rate(video_stream.get("avg_frame_rate")) or _parse_frame_rate(
            video_stream.get("r_frame_rate")
        )
        if fps <= 0:
            raise RecipeError("ffprobe did not report a video frame rate.")

        frame_count = 0
        raw_frame_count = video_stream.get("nb_frames")
        try:
            frame_count = int(float(raw_frame_count))
        except (TypeError, ValueError):
            frame_count = 0
        if frame_count <= 0 and duration > 0:
            frame_count = max(1, round(duration * fps))
        if frame_count <= 0:
            raise RecipeError("ffprobe did not report a video frame count.")

        stream_count = len(streams)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecipeError("ffprobe returned unreadable media information.") from exc
    return {"duration": duration, "stream_count": stream_count, "fps": fps, "frame_count": frame_count}
