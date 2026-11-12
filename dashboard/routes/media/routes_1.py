from __future__ import annotations


async def _media_trim(
    request: MediaTrimRequest,
    *,
    operation: str = "trim",
    derivation_params: dict[str, Any] | None = None,
    stable_source: Path | None = None,
) -> dict[str, Any]:
    temp_path: Path | None = None
    owned_source_snapshot: Path | None = None
    try:
        if isinstance(request.start, bool) or isinstance(request.end, bool):
            raise RecipeError("Trim points must be numbers.")
        if request.start < 0 or request.end <= request.start:
            raise RecipeError("Trim end must be greater than trim start, and both must be non-negative.")
        roots = _request_roots(request.approved_roots, request.project_ref, path=[request.src, request.dest], server_list=True)
        source = _confined_local_path(request.src, roots)
        destination = _confined_output_path(request.dest, roots)
        if source == destination:
            raise RecipeError("Trim output must be a new file; the source is never overwritten.")
        _trim_destination_new(destination)  # lane w34-sec (finding 17): a new .mp4, never an existing file

        if stable_source is None:
            owned_source_snapshot, _ = await asyncio.to_thread(_snapshot_derivation_source, source)
            processing_source = owned_source_snapshot
        else:
            processing_source = Path(stable_source)
            if not processing_source.is_file():
                raise RecipeError("The stable derivation source no longer exists.")

        if operation not in {"trim", "prepare"}:
            raise RecipeError("Unknown media derivation operation.")
        derivation = _derivation_envelope(
            processing_source,
            operation,
            derivation_params
            or {
                "start": float(request.start),
                "end": float(request.end),
                "frame_accurate": bool(request.frame_accurate),
            },
        )
        derivation_comment = json.dumps(derivation, ensure_ascii=False, separators=(",", ":"))
        temp_path = destination.parent / f".{destination.name}.trim-{uuid.uuid4().hex}.mp4"
        if request.frame_accurate:
            command = [
                str(FFMPEG_PATH),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(processing_source),
                "-ss",
                str(request.start),
                "-to",
                str(request.end),
                "-c:v",
                "libx264",
                "-crf",
                "16",
                "-preset",
                "fast",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-map_metadata",
                "-1",
                "-metadata",
                f"comment={derivation_comment}",
                "-movflags",
                "+faststart+use_metadata_tags",
                str(temp_path),
            ]
        else:
            command = [
                str(FFMPEG_PATH),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(request.start),
                "-to",
                str(request.end),
                "-i",
                str(processing_source),
                "-c",
                "copy",
                "-map_metadata",
                "-1",
                "-metadata",
                f"comment={derivation_comment}",
                "-movflags",
                "use_metadata_tags",
                str(temp_path),
            ]
        await asyncio.to_thread(_run, command)
        media = await asyncio.to_thread(probe_media, temp_path)
        if media["stream_count"] <= 0 or media["duration"] <= 0:
            raise RecipeError("ffmpeg produced an unreadable trim.")
        if await asyncio.to_thread(_read_embedded_recipe, temp_path) != derivation:
            raise RecipeError("ffmpeg did not write fresh derivation metadata.")
        derivation_response = _derivation_response(derivation, temp_path)
        _publish_new_file(temp_path, destination)
        temp_path = None
        return {
            "trimmed": True,
            "src": str(source),
            "dest": str(destination),
            "start": request.start,
            "end": request.end,
            "frame_accurate": request.frame_accurate,
            "duration": media["duration"],
            "stream_count": media["stream_count"],
            "fps": media["fps"],
            "frame_count": media["frame_count"],
            "bytes": destination.stat().st_size,
            **derivation_response,
        }
    except (RecipeError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, _RebasePending):  # lane p13c-safe item 2: the destination lies at a pending re-base's old place
            return {"error": str(exc), "reason_code": exc.reason_code}
        return _folder_refusal() if isinstance(exc, _FolderNotApproved) else _error(f"Media trim failed: {exc}")
    finally:
        if temp_path is not None:
            _safe_unlink(temp_path)
        if owned_source_snapshot is not None:
            _safe_unlink(owned_source_snapshot)


@router.post("/media/trim")
async def media_trim(request: MediaTrimRequest) -> dict[str, Any]:
    return await _media_trim(request)


@router.post("/media/prepare")
async def media_prepare(request: MediaPrepareRequest) -> dict[str, Any]:
    stable_source: Path | None = None
    cache_lock: PrepareKeyLock | None = None
    try:
        if isinstance(request.start, bool) or isinstance(request.end, bool):
            raise RecipeError("Trim points must be numbers.")
        if request.start < 0 or request.end <= request.start:
            raise RecipeError("Trim end must be greater than trim start, and both must be non-negative.")
        if (request.in_frame is None) != (request.out_frame is None):
            raise RecipeError("Both prepared frame bounds are required together.")
        if any(isinstance(value, bool) for value in (request.in_frame, request.out_frame) if value is not None):
            raise RecipeError("Prepared frame bounds must be whole numbers.")

        roots = _request_roots(request.approved_roots, request.project_ref, path=request.src, server_list=True)
        source = _confined_local_path(request.src, roots)
        stable_source, stable_source_hash = await asyncio.to_thread(_snapshot_derivation_source, source)
        media = await asyncio.to_thread(probe_media, stable_source)
        fps = float(media["fps"])
        duration = float(media["duration"])
        if (
            not math.isfinite(float(request.start))
            or not math.isfinite(float(request.end))
            or request.start >= duration
        ):
            raise RecipeError("Prepared trim points are outside the source clip.")
        if request.in_frame is None:
            in_frame = max(0, int(request.start * fps + 0.5))
            out_frame = max(in_frame, int(min(request.end, duration) * fps + 0.5) - 1)
            trim_start = request.start
            trim_end = min(request.end, duration)
        else:
            in_frame = int(request.in_frame)
            out_frame = int(request.out_frame)
            if in_frame < 0 or out_frame < in_frame or out_frame >= media["frame_count"]:
                raise RecipeError("Prepared frame bounds are outside the source clip.")
            trim_start = in_frame / fps
            trim_end = min((out_frame + 1) / fps, duration)

        if out_frame < in_frame or trim_end <= trim_start:
            raise RecipeError("The prepared range does not contain a video frame.")

        prepare_params = {
            "start": float(trim_start),
            "end": float(trim_end),
            "in_frame": in_frame,
            "out_frame": out_frame,
            "fps": fps,
            "frame_accurate": True,
        }
        expected_envelope = _derivation_envelope(stable_source, "prepare", prepare_params)
        expected_derivation = expected_envelope["derivation"]
        cache_dir = prepared_cache_dir()
        cache_key = prepared_cache_key(source, in_frame, out_frame, fps, stable_source_hash)
        cache_path = cache_dir / f"{cache_key}.mp4"
        cache_lock = PrepareKeyLock(cache_dir, cache_key)
        await cache_lock.acquire()
        evict_prepared_cache(cache_dir, protected={cache_path})

        if cache_path.is_file() and cache_path.stat().st_size > 0:
            try:
                cached_media = await asyncio.to_thread(probe_media, cache_path)
                cached_recipe = await asyncio.to_thread(_read_embedded_recipe, cache_path)
                if (
                    not isinstance(cached_recipe, dict)
                    or cached_recipe.get("schema") != "studio.derivation/1"
                    or cached_recipe.get("derivation") != expected_derivation
                    or cached_recipe.get("derivations") != expected_envelope.get("derivations")
                    or cached_recipe.get("root_job_id") != expected_envelope.get("root_job_id")
                ):
                    raise RecipeError("Prepared cache has stale derivation metadata.")
            except RecipeError:
                cache_path.unlink(missing_ok=True)
            else:
                os.utime(cache_path, None)
                return {
                    "prepared": True,
                    "cached": True,
                    "prepared_path": str(cache_path),
                    "dest": str(cache_path),
                    "source_path": str(source),
                    "start": trim_start,
                    "end": trim_end,
                    "in_frame": in_frame,
                    "out_frame": out_frame,
                    "frame_accurate": True,
                    "bytes": cache_path.stat().st_size,
                    "fps": cached_media["fps"],
                    "duration": cached_media["duration"],
                    "frame_count": cached_media["frame_count"],
                    **_derivation_response(cached_recipe, cache_path),
                }
        elif cache_path.exists():
            cache_path.unlink(missing_ok=True)

        cache_roots = list(dict.fromkeys([*roots, str(cache_dir)]))
        trimmed = await _media_trim(
            MediaTrimRequest(
                src=str(source),
                start=trim_start,
                end=trim_end,
                dest=str(cache_path),
                approved_roots=cache_roots, project_ref=request.project_ref,
                frame_accurate=True,
            ),
            operation="prepare",
            derivation_params=prepare_params,
            stable_source=stable_source,
        )
        if trimmed.get("error"):
            cache_path.unlink(missing_ok=True)
            return _error(f"Media preparation failed: {trimmed['error']}")
        if not cache_path.is_file() or cache_path.stat().st_size == 0:
            raise RecipeError("The prepared file was not created.")

        return {
            "prepared": True,
            "cached": False,
            "prepared_path": str(cache_path),
            "dest": str(cache_path),
            "source_path": str(source),
            "start": trim_start,
            "end": trim_end,
            "in_frame": in_frame,
            "out_frame": out_frame,
            "frame_accurate": True,
            "bytes": trimmed["bytes"],
            "fps": trimmed["fps"],
            "duration": trimmed["duration"],
            "frame_count": trimmed["frame_count"],
            "derivation": trimmed["derivation"],
            "derivations": trimmed["derivations"],
            "root_job_id": trimmed["root_job_id"],
            "source_sha256": trimmed["source_sha256"],
            "prepared_sha256": trimmed["prepared_sha256"],
        }
    except (RecipeError, OSError, TypeError, ValueError) as exc:
        return _folder_refusal() if isinstance(exc, _FolderNotApproved) else _error(f"Media preparation failed: {exc}")
    finally:
        if cache_lock is not None:
            await cache_lock.release()
        if stable_source is not None:
            _safe_unlink(stable_source)


@router.post("/media/probe")
async def media_probe(request: MediaProbeRequest) -> dict[str, Any]:
    """Probe ONE approved file (ffprobe, cached). Listing never probes; callers ask when a file is selected."""
    try:
        path = _confined_local_path(request.path, _request_roots(request.approved_roots, request.project_ref, path=request.path, server_list=True))
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
        if MEDIA_EXTENSIONS.get(path.suffix.lower()) == "audio":
            sound = await asyncio.to_thread(_media_sound_cached, path)
            if sound.get("probe_error"):
                raise RecipeError(sound["probe_error"])
            return {"path": str(path), "bytes": stat.st_size, "kind": "audio", **sound}
        media = MEDIA_PROBE_CACHE.get(cache_key)
        if media is None:
            media = await asyncio.to_thread(probe_media, path)
            while len(MEDIA_PROBE_CACHE) >= MEDIA_PROBE_CACHE_MAX_ENTRIES:
                MEDIA_PROBE_CACHE.pop(next(iter(MEDIA_PROBE_CACHE)), None)
            MEDIA_PROBE_CACHE[cache_key] = media
        return {
            "path": str(path),
            "bytes": stat.st_size,
            **media,
        }
    except (RecipeError, OSError, TypeError, ValueError) as exc:
        return _folder_refusal() if isinstance(exc, _FolderNotApproved) else _error(f"Media probe failed: {exc}")


@router.post("/media/frame")
async def media_frame(request: MediaFrameRequest) -> dict[str, Any]:
    temporary: Path | None = None
    try:
        if isinstance(request.frame, bool) or request.frame < 0:
            raise RecipeError("Frame must be a non-negative whole number.")
        if isinstance(request.fps, bool) or request.fps <= 0:
            raise RecipeError("FPS must be a positive number.")
        if isinstance(request.width, bool) or request.width <= 0:
            raise RecipeError("Frame width must be a positive whole number.")

        source = _confined_local_path(request.path, _request_roots(request.approved_roots, request.project_ref, path=request.path, server_list=True))
        media = await asyncio.to_thread(probe_media, source)
        if request.frame >= media["frame_count"]:
            raise RecipeError(
                f"Frame {request.frame} is outside the clip; valid frames are 0 through {media['frame_count'] - 1}."
            )

        target_frame = min(request.frame, max(0, media["frame_count"] - 1))
        width = max(16, min(640, int(request.width)))
        frame_half_duration = 0.5 / float(request.fps)
        timestamp = min((target_frame + 0.5) / float(request.fps), media["duration"] - frame_half_duration)
        # With input-side -ss, FFmpeg needs one full frame of decode headroom at EOF.
        timestamp = max(0.0, min(timestamp, media["duration"] - (1.0 / float(request.fps))))

        fd, name = tempfile.mkstemp(prefix="flux3-frame-", suffix=".jpg")
        os.close(fd)
        temporary = Path(name)
        try:
            await asyncio.to_thread(_run, 
                [
                    str(FFMPEG_PATH),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    str(timestamp),
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={width}:-2",
                    "-q:v",
                    "4",
                    "-f",
                    "image2",
                    "-vcodec",
                    "mjpeg",
                    "-pix_fmt",
                    "yuvj420p",
                    "-y",
                    str(temporary),
                ]
            )
        except RecipeError as exc:
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise RecipeError("No frame at that position.") from exc
            raise
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RecipeError("No frame at that position.")
        jpeg = temporary.read_bytes()
        if not jpeg or not jpeg.startswith(b"\xff\xd8"):
            raise RecipeError("ffmpeg produced an empty or invalid JPEG frame.")
        return {
            "path": str(source),
            "frame": target_frame,
            "fps": float(request.fps),
            "width": width,
            "media_time": timestamp,
            "data_url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
        }
    except (RecipeError, OSError, TypeError, ValueError) as exc:
        return _folder_refusal() if isinstance(exc, _FolderNotApproved) else _error(f"Media frame failed: {exc}")
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _media_entry_is_link(entry: "os.DirEntry[str]") -> bool:
    """True for symlinks and Windows junctions; cloud placeholders (OneDrive) are not links."""
    try:
        if entry.is_symlink():
            return True
        if os.name == "nt":
            info = entry.stat(follow_symlinks=False)
            if getattr(info, "st_file_attributes", 0) & _WIN_ATTRIBUTE_REPARSE_POINT:
                return getattr(info, "st_reparse_tag", 0) in _WIN_REPARSE_TAGS_NOT_FOLLOWED
    except OSError:
        return True  # cannot tell -> do not follow
    return False


def _media_dir_is_skipped(entry: "os.DirEntry[str]") -> bool:
    name = entry.name
    if name.startswith(".") or name.lower() in MEDIA_LIST_SKIP_DIR_NAMES:
        return True
    if _media_entry_is_link(entry):
        return True
    if os.name == "nt":
        try:
            attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        except OSError:
            return True
        if attributes & (_WIN_ATTRIBUTE_HIDDEN | _WIN_ATTRIBUTE_SYSTEM):
            return True
    return False


# Lane w27-rows item 4 (P2 AUD-151): a Deliver package (manifest.json schema studio.timeline-export/1) is ONE output:
# Media lists its film only, never its stems / staged copies as loose sounds (Resolve's render lands as one clip).
def _media_dir_is_export_package(directory: str) -> bool:
    try:
        with open(os.path.join(directory, "manifest.json"), "rb") as handle:
            return b"studio.timeline-export/1" in handle.read(512)
    except OSError:
        return False


def _scan_media_roots(approved_roots: list[str]) -> dict[str, Any]:
    """Bounded, blocking walk of the approved roots. Run it off the event loop.

    Depth <= MEDIA_LIST_MAX_DEPTH (root = 0), no link following, hidden/system/heavy
    folders skipped, at most MEDIA_LIST_SCAN_BUDGET directory entries and
    MEDIA_LIST_TIME_BUDGET_S seconds, then the newest MEDIA_LIST_MAX_FILES files win.
    Never runs ffprobe for a video; its probe fields appear only when already cached. A sound
    file's header is read within its own budget (media/sound_1.py ``_media_sound_fill``).
    """
    deadline = time.monotonic() + max(0.0, float(MEDIA_LIST_TIME_BUDGET_S))
    scan_budget = int(MEDIA_LIST_SCAN_BUDGET)
    errors: list[str] = []
    per_root: list[dict[str, Any]] = []
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    scanned = 0
    stop_reason: str | None = None

    for raw_root in approved_roots:
        try:
            root = resolve_approved_path(raw_root, approved_roots)
            if not root.is_dir():
                raise RecipeError("An approved media root is not a folder.")
        except (OSError, RecipeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        root_stats = {"root": str(root), "files_found": 0, "dirs_skipped": 0}
        per_root.append(root_stats)
        if stop_reason:
            continue

        queue: collections.deque[tuple[str, int]] = collections.deque([(str(root), 0)])
        while queue and not stop_reason:
            directory, depth = queue.popleft()
            if time.monotonic() >= deadline:
                stop_reason = "time_budget"
                break
            package = _media_dir_is_export_package(directory)
            try:
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        scanned += 1
                        if scanned > scan_budget:
                            stop_reason = "scan_budget"
                            break
                        if scanned % 256 == 0 and time.monotonic() >= deadline:
                            stop_reason = "time_budget"
                            break
                        try:
                            if entry.is_dir(follow_symlinks=False) or (
                                os.name == "nt" and _media_entry_is_link(entry) and entry.is_dir()
                            ):
                                if package or _media_dir_is_skipped(entry):
                                    root_stats["dirs_skipped"] += 1
                                elif depth < MEDIA_LIST_MAX_DEPTH:
                                    queue.append((entry.path, depth + 1))
                                continue
                            kind = MEDIA_EXTENSIONS.get(os.path.splitext(entry.name)[1].lower())
                            if not kind or not entry.is_file(follow_symlinks=False):
                                continue
                            if package and kind != "video":
                                continue
                            if os.name == "nt" and _media_entry_is_link(entry):
                                continue
                            info = entry.stat(follow_symlinks=False)
                        except OSError:
                            if len(errors) < 20:
                                errors.append(f"{entry.path}: Could not inspect this media asset.")
                            continue
                        key_path = entry.path
                        if key_path in seen:
                            continue
                        seen.add(key_path)
                        root_stats["files_found"] += 1
                        found.append(
                            {
                                "path": key_path,
                                "name": entry.name,
                                "kind": kind,
                                "bytes": info.st_size,
                                "modified": info.st_mtime,
                                "ref": _media_folder_ref(key_path, info.st_size, info.st_mtime_ns),
                                "_key": (key_path, info.st_mtime_ns, info.st_size),
                            }
                        )
            except OSError:
                if len(errors) < 20:
                    errors.append(
                        "Could not read the approved media root."
                        if depth == 0
                        else f"{directory}: Could not read this folder."
                    )

    files_found = len(found)
    found.sort(key=lambda item: (-item["modified"], item["path"].lower()))
    kept = found[: max(0, int(MEDIA_LIST_MAX_FILES))]
    truncated_reason = stop_reason or ("file_cap" if files_found > len(kept) else None)

    assets: list[dict[str, Any]] = []
    sounds: list[tuple[dict[str, Any], tuple[str, int, int]]] = []
    for item in kept:
        cache_key = item.pop("_key")
        if item["kind"] == "video":
            cached = MEDIA_PROBE_CACHE.get(cache_key)
            if cached is not None:
                item.update(cached)
            if (take_id := _media_folder_take_id(item["path"])) is not None:
                item["take_id"] = take_id
        elif item["kind"] == "audio":
            sounds.append((item, cache_key))
        elif item["kind"] == "image" and (digest := _media_picture_sha256(item["path"], cache_key)) is not None:
            item["sha256"] = digest
        assets.append(item)
    # Sound files: header facts + mark id within their own budget (media/sound_1.py).
    sounds_unread = _media_sound_fill(sounds) if sounds else 0
    assets.sort(key=lambda item: (item["kind"], item["name"].lower(), item["path"].lower()))
    return {
        "assets": assets,
        "errors": errors,
        "summary": {
            "files_found": files_found,
            "files_returned": len(assets),
            "truncated": truncated_reason is not None,
            "truncated_reason": truncated_reason,
            "max_depth": MEDIA_LIST_MAX_DEPTH,
            "per_root": per_root,
            "sounds_unread": sounds_unread,
        },
    }



# Lane w25-cutfix (Install 130: every Sample film shot twice in Media; Lightroom skips suspected duplicates by content):
# a folder picture names its content hash, so the column shows one tile for a file and the owned import made from the
# same bytes (/media/feed source_sha256). Pictures only, size-capped, cached by (path, mtime, size): read once.
_MEDIA_PICTURE_SHA: dict[tuple[str, int, int], str] = {}
_MEDIA_PICTURE_SHA_MAX_BYTES = 64 * 1024 * 1024


def _media_picture_sha256(path: str, key: tuple[str, int, int]) -> str | None:
    hit = _MEDIA_PICTURE_SHA.get(key)
    if hit is not None:
        return hit
    if key[2] > _MEDIA_PICTURE_SHA_MAX_BYTES:
        return None
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None
    if len(_MEDIA_PICTURE_SHA) >= 5_000:
        _MEDIA_PICTURE_SHA.clear()
    _MEDIA_PICTURE_SHA[key] = digest
    return digest

_MEDIA_TAKE_RECIPE_MAX = 1 << 20


def _media_folder_take_id(path: str) -> str | None:
    """Lane p13-med (VID-100, MED-017 neighbour): the take a folder clip IS, when its own recipe sidecar
    (``<stem>.flux3.json`` next to it, the file the take's publish writes) names a ``studio:`` job id; the same
    evidence the project take resolution reads (feed_1.py ``_recipe_take``). A copied or moved project's takes have
    no ledger row in this home, so the column lists them as files; this id lets Keep on that tile write the take's
    own mark (the Take tab's write). Size-capped, never follows the clip elsewhere; anything else answers None."""
    try:
        clip = Path(path)
        recipe = clip.with_name(clip.stem + ".flux3.json")
        if not recipe.is_file() or recipe.stat().st_size > _MEDIA_TAKE_RECIPE_MAX:
            return None
        named = json.loads(recipe.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    job_id = named.get("job_id") if isinstance(named, dict) else None
    if isinstance(job_id, str) and re.fullmatch(_take_ids_module.STUDIO_JOB_ID_BODY, job_id):
        return job_id
    return None


def _media_folder_ref(path: str, size: int, mtime_ns: int) -> str:
    """Lane p11-med item 1 (MED-053): the path-free id of a folder file the page publishes and Hermes selects by.
    No file read; stable while the file is unchanged (same path, size and modified time)."""
    digest = hashlib.sha256(f"{os.path.normcase(str(path))}|{size}|{mtime_ns}".encode("utf-8")).hexdigest()
    return "folder:" + digest[:32]


@router.post("/media/list")
async def media_list(request: MediaListRequest) -> dict[str, Any]:
    """Bounded listing: see ``_scan_media_roots``. The walk runs in a worker thread."""
    try:
        roots = _request_roots(request.approved_roots, request.project_ref, server_list=True)
    except _FolderNotApproved:
        return _folder_refusal()
    return await asyncio.to_thread(_scan_media_roots, roots)


def _load_pillow() -> Any:
    try:
        from PIL import Image
    except Exception:  # Pillow is optional
        return None
    return Image


def _render_thumbnail(path: Path, size: int) -> bytes:
    """Blocking: return a JPEG no larger than ``size`` px on its long edge."""
    image_module = _load_pillow()
    if image_module is None:
        raise RecipeError("thumbnail unavailable")
    with image_module.open(path) as source:
        source.draft("RGB", (size, size))
        picture = source.copy()
    picture.thumbnail((size, size))
    if picture.mode in ("RGBA", "LA", "P"):
        picture = picture.convert("RGBA")
        backdrop = image_module.new("RGB", picture.size, (255, 255, 255))
        backdrop.paste(picture, mask=picture.getchannel("A"))
        picture = backdrop
    elif picture.mode != "RGB":
        picture = picture.convert("RGB")
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


# Lane p5d-med item 3 (MED-045, REV-15): a clip's poster frame and a sound's waveform for the tiles.
# A clip: ONE frame at 10 % of its length (ffmpeg, fixed argv), scaled to the thumb size, cached ON DISK under the
# state root by (path, size, mtime_ns, thumb size): extracted once, every later read (also after a restart) is a
# file read. A sound: a small waveform drawn from peaks the backend already holds (MEDIA_PEAKS_CACHE, filled by
# /media/peaks); without stored peaks it is unavailable (404) and the tile shows its sound glyph.
MEDIA_POSTER_AT = 0.10


def _media_poster_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(state_root(Path(base) / "hermes")) / "media-posters"


def _media_poster_file(path: Path, stat: os.stat_result, size: int) -> Path:
    key = hashlib.sha256(f"{os.path.normcase(str(path))}|{stat.st_size}|{stat.st_mtime_ns}|{size}".encode("utf-8")).hexdigest()
    return _media_poster_dir() / f"{key[:2]}" / f"{key}.jpg"


def _media_poster_render(path: Path, size: int, target: Path) -> bytes:
    """Blocking: extract one frame at 10 % of the clip into ``target`` (atomic) and return its bytes."""
    media = probe_media(path)
    duration = float(media.get("duration") or 0.0)
    at = max(0.0, duration * MEDIA_POSTER_AT)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".poster-", suffix=".jpg", dir=str(target.parent))
    os.close(fd)
    temporary = Path(name)
    try:
        _run([str(FFMPEG_PATH), "-hide_banner", "-loglevel", "error", "-ss", f"{at:.3f}", "-i", str(path), "-frames:v", "1",
              "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease", "-q:v", "4", "-f", "image2", "-vcodec", "mjpeg",
              "-pix_fmt", "yuvj420p", "-y", str(temporary)], failure_message="thumbnail unavailable")
        body = temporary.read_bytes()
        if not body:
            raise RecipeError("thumbnail unavailable")
        os.replace(temporary, target)
        return body
    finally:
        temporary.unlink(missing_ok=True)


def _media_poster(path: Path, stat: os.stat_result, size: int) -> bytes:
    target = _media_poster_file(path, stat, size)
    try:
        return target.read_bytes()
    except OSError:
        return _media_poster_render(path, size, target)


def _media_wave_from_peaks(path: Path, stat: os.stat_result, size: int) -> bytes | None:
    """Blocking: a waveform JPEG from peaks already held for this file, or None (nothing is decoded here)."""
    image_module = _load_pillow()
    if image_module is None:
        return None
    facts = MEDIA_PROBE_CACHE.get((str(path), stat.st_mtime_ns, stat.st_size)) or {}
    keys = {facts.get("item_id")} - {None} | {f"{path}|{stat.st_mtime_ns}"}
    with _MEDIA_PEAKS_LOCK:
        held = [value for (key, _buckets), value in MEDIA_PEAKS_CACHE.items() if key in keys]
    if not held:
        return None
    peaks = max(held, key=lambda value: value.get("buckets", 0)).get("peaks") or []
    if not peaks:
        return None
    from PIL import ImageDraw  # Pillow is present: _load_pillow answered
    width, height = size, max(16, size * 10 // 16)
    picture = image_module.new("RGB", (width, height), (36, 38, 42))
    draw = ImageDraw.Draw(picture)
    middle = height / 2
    for x in range(width):
        low, high = peaks[min(len(peaks) - 1, x * len(peaks) // width)]
        draw.line([(x, middle - abs(high) * middle * 0.9), (x, middle + abs(low) * middle * 0.9)], fill=(190, 194, 200))
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


@router.post("/media/thumb")
async def media_thumb(request: MediaThumbRequest) -> Response:
    """Small JPEG for ONE approved picture, clip (poster frame, disk cache) or sound with stored peaks; 404 when unavailable."""

    def unavailable(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    if request.ref is not None:
        # Lane w22-cut (CUT-177): a folder still of the cut by ref, at viewer size (Resolve's viewer shows the linked
        # original). Only with its project; the footage index confines it to the project's folders.
        if request.project_ref is None:
            return JSONResponse(_folder_refusal(), status_code=403)
        try:
            located, _facts = await asyncio.to_thread(_timeline_footage_lookup(request.project_ref.project_id), request.ref)
            path = _confined_local_path(str(located), _request_roots([], request.project_ref, path=str(located), server_list=True))
        except RecipeError as exc:
            if isinstance(exc, _FolderNotApproved):
                return JSONResponse(_folder_refusal(), status_code=403)
            return unavailable(404, "thumbnail unavailable")
        except Exception:  # noqa: BLE001 - not this project's footage, moved or changed: offline
            return unavailable(404, "thumbnail unavailable")
        if MEDIA_EXTENSIONS.get(path.suffix.lower()) != "image":
            return unavailable(404, "thumbnail unavailable")
        limit = MEDIA_STILL_MAX_SIZE
    else:
        if not request.path:
            return unavailable(404, "thumbnail unavailable")
        try:
            path = _confined_local_path(request.path, _request_roots(request.approved_roots, request.project_ref, path=request.path, server_list=True))
        except RecipeError as exc:
            if isinstance(exc, _FolderNotApproved):
                return JSONResponse(_folder_refusal(), status_code=403)
            status = 403 if "outside every approved root" in str(exc) else 404
            return unavailable(status, f"Media thumbnail failed: {exc}")
        limit = MEDIA_THUMB_MAX_SIZE
    try:
        kind = MEDIA_EXTENSIONS.get(path.suffix.lower())
        size = max(MEDIA_THUMB_MIN_SIZE, min(2048 if request.reframe and kind == "image" and request.ref is None else limit, int(request.size)))  # w39-p4-fix: Crop / Pad bake
        if kind in ("video", "audio"):
            stat = path.stat()
            body = await asyncio.to_thread(_media_poster if kind == "video" else _media_wave_from_peaks, path, stat, size)
            if not body:
                return unavailable(404, "thumbnail unavailable")
            if request.format == "data_url":
                encoded = base64.b64encode(body).decode("ascii")
                return JSONResponse({"data_url": f"data:image/jpeg;base64,{encoded}"}, headers={"Cache-Control": "private, max-age=300"})
            return Response(content=body, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
        if kind != "image":
            return unavailable(404, "thumbnail unavailable")
        if _load_pillow() is None:
            return unavailable(404, "thumbnail unavailable")
        stat = path.stat()
        if stat.st_size > MEDIA_THUMB_SOURCE_MAX_BYTES:
            return unavailable(404, "thumbnail unavailable")
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size, size)
        with _MEDIA_THUMB_LOCK:
            if (body := MEDIA_THUMB_CACHE.get(cache_key)) is not None:
                MEDIA_THUMB_CACHE.move_to_end(cache_key)
        if body is None:
            body = await asyncio.to_thread(_render_thumbnail, path, size)
            if size <= MEDIA_THUMB_MAX_SIZE:  # a reframe still is not held in memory
                with _MEDIA_THUMB_LOCK:
                    MEDIA_THUMB_CACHE[cache_key] = body
                    MEDIA_THUMB_CACHE.move_to_end(cache_key)
                    while len(MEDIA_THUMB_CACHE) > MEDIA_THUMB_CACHE_MAX_ENTRIES:
                        MEDIA_THUMB_CACHE.popitem(last=False)
        if request.format == "data_url":
            encoded = base64.b64encode(body).decode("ascii")
            return JSONResponse({"data_url": f"data:image/jpeg;base64,{encoded}"}, headers={"Cache-Control": "private, max-age=300"})
        return Response(content=body, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
    except Exception:  # unreadable/corrupt/oversized picture: the UI falls back to the placeholder
        return unavailable(404, "thumbnail unavailable")
