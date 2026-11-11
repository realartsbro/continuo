from __future__ import annotations


# --- Media knows sound files (lane fs-msnd) ----------------------------------------------------
#
# Sound files of the approved folders are listed by /media/list as kind "audio" (the suffix set is
# the timeline's own AUDIO_SUFFIXES, see shared/constants_1.py: one list for Media and audio/add).
# The listing reads a sound file's header once (ffprobe: seconds, samples, rate, channels, codec)
# and its content hash (the mark id file:<sha256>, the same id /projects/timeline/audio/add gives
# it), cached in MEDIA_PROBE_CACHE by (path, mtime_ns, size) like a video's probe. That work has
# its own budget (MEDIA_LIST_SOUND_PROBE_BUDGET_S, MEDIA_LIST_SOUND_PROBE_MAX per listing): what is
# left over is listed without facts and read later by /media/probe. A file that holds no sound is
# listed with ``probe_error`` (a plain reason), never dropped and never failing the listing.
# /media/peaks answers a small waveform (min/max per bucket) of one sound file of the approved
# folders, or of a finished generated sound of the scope (owner_scope + studio_job_id, checked like
# /media/feed/poster); read-only, cached per content hash, never a path in the answer.

MEDIA_LIST_SOUND_PROBE_BUDGET_S = 1.5
MEDIA_LIST_SOUND_PROBE_MAX = 64
MEDIA_SOUND_HASH_MAX_BYTES = 512 * 1024 * 1024
MEDIA_PEAKS_CACHE_MAX_ENTRIES = 256
MEDIA_PEAKS_CACHE: "collections.OrderedDict[tuple[str, int], dict[str, Any]]" = collections.OrderedDict()
_MEDIA_PEAKS_LOCK = _thread.allocate_lock()
_MEDIA_SOUND_WORDS = {
    "empty": "The file is empty.",
    "no_sound": "No sound could be read from this file.",
    "unreadable": "The file could not be read.",
}


class MediaPeaksRequest(BaseModel):
    """One sound file of the approved folders (path + approved_roots) or one finished generated
    sound of the scope (owner_scope + studio_job_id)."""
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    approved_roots: list[str] = Field(default_factory=list)
    project_ref: ProjectReference | None = None
    owner_scope: str | None = Field(default=None, pattern=_OWNER_SCOPE_PATTERN, min_length=18, max_length=47)
    studio_job_id: str | None = Field(default=None, pattern=_take_ids_module.STUDIO_JOB_ID_PATTERN, min_length=39, max_length=43)
    buckets: int = Field(default=96, ge=8, le=400)


def _media_sound_header(path: Path) -> dict[str, Any]:
    """Blocking: seconds, samples, rate, channels and codec of the first sound stream (header only).

    Raises RecipeError with a code in ``_MEDIA_SOUND_WORDS``. ``samples`` is the stream's own
    count when its time base is the sample clock (wav, flac), else seconds x rate rounded."""
    if path.stat().st_size == 0:
        raise RecipeError("empty")
    done = subprocess.run(
        [str(FFPROBE_PATH), "-v", "error", "-select_streams", "a:0", "-show_entries",
         "format=duration:stream=codec_name,sample_rate,channels,duration_ts,time_base,duration", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False, shell=False, env=_child_env(),
    )
    try:
        payload = json.loads(done.stdout or "{}") if done.returncode == 0 else {}
    except json.JSONDecodeError:
        payload = {}
    streams = payload.get("streams") or []
    if not streams:
        raise RecipeError("no_sound")
    stream = streams[0]
    try:
        rate, channels = int(stream.get("sample_rate") or 0), int(stream.get("channels") or 0)
    except (TypeError, ValueError):
        rate = channels = 0
    if rate < 1000 or channels < 1:
        raise RecipeError("no_sound")
    seconds = 0.0
    for raw in (stream.get("duration"), (payload.get("format") or {}).get("duration")):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            seconds = value
            break
    samples = 0
    try:
        base = _parse_frame_rate(stream.get("time_base"))  # "1/48000" -> 1/48000 as a float
        ticks = int(stream.get("duration_ts"))
        if base > 0 and abs(base * rate - 1.0) < 1e-9 and ticks > 0:
            samples = ticks
    except (TypeError, ValueError):
        samples = 0
    if samples <= 0 and seconds > 0:
        samples = int(round(seconds * rate))
    if samples <= 0:
        raise RecipeError("no_sound")
    if seconds <= 0:
        seconds = samples / rate
    return {"duration": round(seconds, 4), "samples": samples, "sample_rate": rate, "channels": channels,
            "codec": str(stream.get("codec_name") or "")[:40]}


def _media_sound_facts(path: Path) -> dict[str, Any]:
    """Blocking: the header facts plus the mark id file:<sha256>, or {"probe_error": reason}."""
    try:
        facts = _media_sound_header(path)
    except RecipeError as exc:
        return {"probe_error": _MEDIA_SOUND_WORDS.get(str(exc), _MEDIA_SOUND_WORDS["no_sound"])}
    except (OSError, subprocess.TimeoutExpired):
        return {"probe_error": _MEDIA_SOUND_WORDS["unreadable"]}
    try:
        if path.stat().st_size <= MEDIA_SOUND_HASH_MAX_BYTES:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                    digest.update(block)
            facts["item_id"] = "file:" + digest.hexdigest()
    except OSError:
        pass
    return facts


def _media_sound_cached(path: Path) -> dict[str, Any]:
    """Blocking: the cached facts of one sound file, read once per (path, mtime_ns, size)."""
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    facts = MEDIA_PROBE_CACHE.get(key)
    if facts is None:
        facts = _media_sound_facts(path)
        while len(MEDIA_PROBE_CACHE) >= MEDIA_PROBE_CACHE_MAX_ENTRIES:
            MEDIA_PROBE_CACHE.pop(next(iter(MEDIA_PROBE_CACHE)), None)
        MEDIA_PROBE_CACHE[key] = facts
    return facts


def _media_sound_fill(pending: list[tuple[dict[str, Any], tuple[str, int, int]]]) -> int:
    """Blocking, inside the listing: cached facts for every listed sound file, new ones read until
    the sound budget is spent (newest first). Returns how many stayed unread."""
    deadline = time.monotonic() + max(0.0, float(MEDIA_LIST_SOUND_PROBE_BUDGET_S))
    probes = 0
    unread = 0
    for item, key in sorted(pending, key=lambda entry: -entry[0]["modified"]):
        # Lane w36-audio item 2: a generated sound (music or voice take) names its take like a folder clip does (its
        # recipe sidecar's job id), so Audio opens a take's tile as its file.
        if (take_id := _media_folder_take_id(item["path"])) is not None:
            item["take_id"] = take_id
        cached = MEDIA_PROBE_CACHE.get(key)
        if cached is None and probes < int(MEDIA_LIST_SOUND_PROBE_MAX) and time.monotonic() < deadline:
            probes += 1
            try:
                cached = _media_sound_cached(Path(item["path"]))
            except OSError:
                cached = {"probe_error": _MEDIA_SOUND_WORDS["unreadable"]}
        if cached is None:
            unread += 1
            continue
        item.update(cached)
    return unread


def _media_peaks_of(path: Path, buckets: int) -> dict[str, Any]:
    """Blocking: min/max per bucket over every channel, cached per content hash and bucket count."""
    facts = _media_sound_cached(path)
    if facts.get("probe_error"):
        raise RecipeError(facts["probe_error"])
    item_id = facts.get("item_id")
    key = (item_id or f"{path}|{path.stat().st_mtime_ns}", buckets)
    with _MEDIA_PEAKS_LOCK:
        hit = MEDIA_PEAKS_CACHE.get(key)
        if hit is not None:
            MEDIA_PEAKS_CACHE.move_to_end(key)
            return hit
    samples, rate, channels = _timeline_peaks_decode(str(path))  # the Timeline's own decode (timeline_peaks_1.py)
    count = int(samples.shape[0])
    peaks: list[list[float]] = []
    for index in range(buckets if count else 0):
        block = samples[index * count // buckets: max(index * count // buckets + 1, (index + 1) * count // buckets)]
        peaks.append([round(float(block.min()), 4), round(float(block.max()), 4)] if len(block) else [0.0, 0.0])
    out = {"item_id": item_id, "samples": count, "sample_rate": rate, "channels": channels,
           "duration_seconds": round(count / rate, 3) if rate else 0.0, "buckets": len(peaks), "peaks": peaks}
    with _MEDIA_PEAKS_LOCK:
        MEDIA_PEAKS_CACHE[key] = out
        MEDIA_PEAKS_CACHE.move_to_end(key)
        while len(MEDIA_PEAKS_CACHE) > MEDIA_PEAKS_CACHE_MAX_ENTRIES:
            MEDIA_PEAKS_CACHE.popitem(last=False)
    return out


def _media_peaks_source(request: MediaPeaksRequest) -> Path:
    if request.studio_job_id:
        if not request.owner_scope:
            raise RecipeError("A generated sound needs its owner_scope.")
        suffix = request.owner_scope[len("film_owner:"):]
        try:
            record = _shoot_take(request.studio_job_id, None if suffix == "scratch" else suffix)  # lane p8-cut (CUT-104)
        except Exception:  # noqa: BLE001 - unknown or unreadable: not found
            raise RecipeError("That sound is not in this project.") from None
        owner = getattr(record, "project_id", None)
        if owner is None:  # a Contract-3 row carries its project only inside the frozen request (as the resolver reads it)
            try:
                owner = (getattr(record, "request", None) or {}).get("project_id") or None
            except Exception:  # noqa: BLE001 - an unreadable request owns nothing
                owner = None
        if owner != (None if suffix == "scratch" else suffix):
            raise RecipeError("That sound is not in this project.")
        if record.state != "ready" or not record.published_path:
            raise RecipeError("The sound has no finished file yet.")
        path = Path(record.published_path)
        if MEDIA_EXTENSIONS.get(path.suffix.lower()) != "audio" or not path.is_file():
            raise RecipeError("That take has no sound file.")
        return path
    if not request.path:
        raise RecipeError("A path or a studio_job_id is required.")
    path = _confined_local_path(request.path, _request_roots(request.approved_roots, request.project_ref, path=request.path, server_list=True))
    if MEDIA_EXTENSIONS.get(path.suffix.lower()) != "audio":
        raise RecipeError("Only sound files have a waveform here.")
    return path


@router.post("/media/peaks")
async def media_peaks(request: MediaPeaksRequest) -> dict[str, Any]:
    """Read-only waveform of one sound file (approved folders) or one finished generated sound."""
    try:
        path = _media_peaks_source(request)
        return await asyncio.to_thread(_media_peaks_of, path, request.buckets)
    except (RecipeError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, _FolderNotApproved):
            return _folder_refusal()
        message = str(exc) if isinstance(exc, RecipeError) else "The sound could not be read."
        return {"error": f"Media peaks failed: {message}"}
    except Exception:  # noqa: BLE001 - finite plugin boundary (decode errors are ProjectError)
        return {"error": "Media peaks failed: The sound could not be read."}
