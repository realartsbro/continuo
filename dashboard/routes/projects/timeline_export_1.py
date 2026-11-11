from __future__ import annotations


# --- Timeline export (studio.timeline-export/1) -------------------------------
# Film (exact reference movie), FCPXML 1.9 (Resolve), FCP7 XML (Premiere), EDL or OTIO, written
# under the project's exports/timeline/ folder with a manifest.  Long work runs
# as a job like /image/mask-job: start answers ``waiting``/``running``,
# ``wait_ms`` long-polls, /status reads, /cancel stops.  The package lives in
# dashboard/timeline_export and loads on first use, so a missing package can
# never break the import of this facade.
_TIMELINE_EXPORT_PACKAGE_NAME = "film_suite_timeline_export"
_TIMELINE_EXPORT_WAIT_MS_MAX = 20_000
_TIMELINE_EXPORT_STATE: dict[str, Any] = {}


def _timeline_export_package() -> Any:
    with _PACKAGE_LOAD_LOCK:
        package = sys.modules.get(_TIMELINE_EXPORT_PACKAGE_NAME)
        if package is not None:
            return package
        directory = Path(__file__).with_name("timeline_export")
        spec = importlib.util.spec_from_file_location(
            _TIMELINE_EXPORT_PACKAGE_NAME, directory / "__init__.py", submodule_search_locations=[str(directory)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("The timeline export package is unavailable.")
        package = importlib.util.module_from_spec(spec)
        sys.modules[_TIMELINE_EXPORT_PACKAGE_NAME] = package
        try:
            spec.loader.exec_module(package)
            for name in ("export_jobs", "export_package", "export_range", "resolver", "export_check"):
                importlib.import_module(f"{_TIMELINE_EXPORT_PACKAGE_NAME}.{name}")
        except BaseException:
            for name in list(sys.modules):
                if name == _TIMELINE_EXPORT_PACKAGE_NAME or name.startswith(_TIMELINE_EXPORT_PACKAGE_NAME + "."):
                    sys.modules.pop(name, None)
            raise
        return package


def _timeline_export_jobs() -> Any:
    jobs = _TIMELINE_EXPORT_STATE.get("jobs")
    if jobs is None:
        jobs = _TIMELINE_EXPORT_STATE["jobs"] = sys.modules[f"{_TIMELINE_EXPORT_PACKAGE_NAME}.export_jobs"].ExportJobs()
        # Lane w20-dlv: this plugin instance's queue in this process (a replaced one = a closed app: its jobs app_closed).
        claim = getattr(jobs, "claim_slot", None)
        if claim is not None:
            claim(f"route-{id(_TIMELINE_EXPORT_STATE)}")
    return jobs


def _timeline_export_store_ok(project_id: str, directory: Any) -> bool:
    """Lane w22-cut (CUT-176): a project's queue store is bound only inside its existing folder whose manifest names it
    (a moved-away folder is never recreated by a poll, Deliver or a job lookup; Resolve marks it offline)."""
    try:
        manifest = json.loads((Path(directory) / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(manifest, dict) and manifest.get("project_id") == project_id


def _timeline_export_jobs_for(project_id: str, directory: Path) -> Any:
    """Lane p12-cut (CUT-006): the queue with this project's stored jobs read back (state/export-queue.json)."""
    _timeline_export_package()
    jobs = _timeline_export_jobs()
    if _timeline_export_store_ok(project_id, directory):
        jobs.restore(project_id, Path(directory) / "state" / "export-queue.json")
    return jobs


def _timeline_export_jobs_knowing(job_id: str) -> Any:
    """Lane w20-dlv (HER-031): the queue that knows ``job_id``. A job id alone (status, Stop) may name a job another
    process renders (the artist's app, while this is Hermes' tool process): bind the stored queues of the recent
    projects, then look again. None when no queue holds it."""
    try:
        _timeline_export_package()
        jobs = _timeline_export_jobs()
    except Exception:  # noqa: BLE001 - the export package is missing
        return None
    if jobs.get(job_id) is not None:
        return jobs
    try:
        entries, _snapshot = _recent_project_store_factory()._read_entries()
    except Exception:  # noqa: BLE001 - no recent registry: nothing more to bind
        entries = None
    for entry in entries or ():
        directory = entry.get("directory") if isinstance(entry, dict) else None
        project_id = entry.get("project_id") if isinstance(entry, dict) else None
        store = Path(directory) / "state" / "export-queue.json" if isinstance(directory, str) and directory else None
        if store is not None and isinstance(project_id, str) and store.is_file() and hasattr(jobs, "restore"):
            jobs.restore(project_id, store)
    return jobs if jobs.get(job_id) is not None else None


def _timeline_export_stored(jobs: Any, job_id: str) -> tuple[str | None, dict[str, Any]] | None:
    """(project_id, stored row) of a job; a queue without ``stored`` (an older package) answers its live snapshot."""
    if hasattr(jobs, "stored"):
        return jobs.stored(job_id)
    state = jobs.get(job_id)
    return None if state is None else (None, state)


def _timeline_export_done_folder(job_id: str) -> tuple[dict[str, Any], Path] | None:
    """Lane w20-dlv (CUT-006, CUT-005): (stored row, package folder) of a job of the render queue, whichever process
    rendered it and across a restart. The folder is this process's own record when it rendered it, else the stored
    answer's folder (relative to the project, or the artist's chosen folder), resolved and confined to the project and
    its approved folders; None when unknown or outside them."""
    jobs = _timeline_export_jobs_knowing(job_id)
    found = None if jobs is None else _timeline_export_stored(jobs, job_id)
    if found is None:
        return None
    project_id, row = found
    folder = (row.get("result") or {}).get("folder") if isinstance(row.get("result"), dict) else None
    # Lane w24-rest (Deliver QC a/f): the job's OWN answer names its package folder; the process record is keyed by
    # settings, so a later job with the same settings overwrote it and QC of the earlier job found no file. The record
    # is only the fallback for a job whose answer has no folder.
    if not isinstance(project_id, str) or not isinstance(folder, str) or not folder:
        key = (_TIMELINE_EXPORT_STATE.get("keys") or {}).get(job_id)
        own = (_TIMELINE_EXPORT_STATE.get("folders") or {}).get(key) if key is not None else None
        return (row, Path(own)) if own is not None else None
    try:
        directory = _project_reference_directory(ProjectReference(project_id=project_id))
        manifest = _project_store_factory().read_project(directory, expected_project_id=project_id)
        project_dir = Path(os.path.realpath(directory))
        candidate = Path(folder) if Path(folder).is_absolute() else project_dir / folder
        return row, resolve_approved_path(str(candidate), [*manifest["approved_roots"], str(project_dir)])
    except Exception:  # noqa: BLE001 - unknown project, outside the approved folders: nothing to show
        return None


def _timeline_export_settings(request: Any) -> dict[str, Any]:
    """What a job renders, path-free (the queue row shows it; Resolve keeps settings + range per job)."""
    return {"kind": request.kind, "timeline_id": request.timeline_id, "quality": request.quality if request.kind == "film" else None,
            "loudness": request.loudness.model_dump() if request.loudness is not None else None,
            "range": None if request.range is None else {"in_frame": request.range.in_frame, "out_frame": request.range.out_frame},
            "name": request.name or None, "folder": "chosen" if request.folder is not None else "project output", "music_dry": request.music_dry}


def _timeline_export_bound_resolver(document: dict[str, Any], project_id: str, ffprobe: Path) -> tuple[Any, dict[str, str]]:
    """A ResolvedResolver over the project's own resolver; offline refs stay unbound (the
    plan then refuses with source_offline and names the items). Returns the binding too,
    so the job key changes when a ref resolves to another file."""
    package = sys.modules[f"{_TIMELINE_EXPORT_PACKAGE_NAME}.resolver"]
    state = document.get("state") if "state" in document else document
    if not isinstance(state, dict) or not isinstance(state.get("timelines"), list):
        return package.ResolvedResolver({}, {}, ffprobe=ffprobe), {}
    try:
        facts = _timeline_facts(state, project_id)
    except (KeyError, TypeError, AttributeError):
        return package.ResolvedResolver({}, {}, ffprobe=ffprobe), {}
    online = {ref: fact["path"] for ref, fact in facts.items() if fact["status"] == "online" and fact["path"]}
    music = {ref: path for ref, path in online.items() if facts[ref]["kind"] == "music"}
    clips = {ref: path for ref, path in online.items() if facts[ref]["kind"] != "music"}
    links, changed = _timeline_export_song_links(online)
    # Lane w20-song (CUT-144; Resolve / Premiere render from the linked original): a song whose original still has the
    # pinned sha256 is rendered from that file too, never from the analysis cache's 44.1 kHz playback copy; a song
    # changed since the analysis keeps the analysed copy for the picture's timing (the hand-over links the current file).
    for ref, source in links.items():
        if ref in clips and Path(clips[ref]).name == ref[5:] + ".wav" and source.name not in changed:
            clips[ref] = str(source)
            online[ref] = str(source)
    resolver = package.ResolvedResolver(clips, music, ffprobe=ffprobe, links=links)
    resolver.songs_changed = changed
    return resolver, {**online, **{"link:" + k: str(v) for k, v in links.items()}}


def _timeline_export_song_links(online: dict[str, str]) -> tuple[dict[str, Path], list[str]]:
    """Lane p12-cut (CUT-021, charter cut decision 8): a song bound to the Music analysis cache's playback WAV
    (``<analysis>/<sha256>.wav``) links, in a hand-over, the recording the artist chose: the cache entry's
    ``source_path`` (FCPXML 1.9 ``original-media`` = the source file). Lane p12-cutb (owner answer 4, charter cut
    decision 8: never a cache proxy): a source that changed since the analysis is still the artist's CURRENT file; it is
    linked as it is now and the plan names the change (beats may be off)."""
    out: dict[str, Path] = {}
    changed: list[str] = []
    for ref, raw in online.items():
        sha = ref[5:] if ref.startswith("file:") else ref
        proxy = Path(raw)
        if proxy.suffix != ".wav" or proxy.stem != sha or proxy.parent.name != "analysis" or not re.fullmatch(r"[0-9a-f]{64}", sha):
            continue
        try:
            entry = json.loads(proxy.with_suffix(".json").read_text(encoding="utf-8"))
            source = Path(entry["source_path"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not source.is_file():
            continue
        out["file:" + sha] = source
        if hashlib.sha256(source.read_bytes()).hexdigest() != sha:
            changed.append(source.name)
    return out, changed


class TimelineExportRange(BaseModel):
    """Record frames ``[in_frame, out_frame)`` of the timeline (the artist's IN-OUT, OUT inclusive + 1)."""

    in_frame: int = Field(ge=0)
    out_frame: int = Field(ge=1)


class TimelineExportLoudness(_ProjectRequest):
    """Delivery loudness (lane w2-cutdel): integrated target (None: no target, only the ceiling) and
    the true-peak ceiling; the written film is measured and fails closed above the ceiling."""

    target_lufs: float | None = Field(default=None, ge=-40, le=-5)
    ceiling_dbtp: float = Field(default=-1.0, ge=-12, le=0)
    # w14-dlv (CUT-069, EBU R128 / Tech 3342): the loudness range the film is brought inside; null = Off
    # (the mix keeps its own range). 11 LU for -14 LUFS web delivery.
    lra_lu: float | None = Field(default=11.0, ge=1, le=20)


class TimelineExportRequest(_ProjectRequest):
    """Export one timeline.  ``document`` is the studio.timeline/1 document (or its
    ``state``).  Without ``sources`` and ``music`` the project's own resolver binds every
    take, picture and pinned song (the default for a real project).  With them, ``sources``
    maps each clip ``source.ref`` and ``music`` each pinned ``track_sha256`` to a file
    inside the project's approved roots."""

    schema_: Literal["studio.timeline-export-request/1"] = Field(alias="schema")
    project_ref: ProjectReference
    kind: Literal["film", "fcpxml", "fcp7", "edl", "otio"]
    timeline_id: str | None = Field(default=None, min_length=1, max_length=120)
    document: dict[str, Any]
    sources: dict[str, str] = Field(default_factory=dict, max_length=2000)
    music: dict[str, str] = Field(default_factory=dict, max_length=8)
    # Film masters (lane w2-cutdel): .mov with PCM 24-bit sound, verified with ffprobe; w14-dlv: the six
    # Apple ProRes profiles in Apple's order (BCK-036).
    quality: Literal["review", "lossless", "prores_proxy", "prores_lt", "prores_422", "prores_422hq", "prores_4444",
                     "prores_4444xq", "dnxhr_hqx"] = "review"
    loudness: TimelineExportLoudness | None = None
    # Stems (lane fs-duck): the music stem carries the volume automation (the stems sum to the mix);
    # true adds audio/music-dry.wav, the music without automation, for a re-mix from scratch.
    music_dry: bool = False
    # The whole cut (None) or a frame range; the package is the cut of that range (export_range.py).
    range: TimelineExportRange | None = None
    # Where the package folder goes: None = the project output folder (p5d-frm); else a folder inside the
    # project's approved folders (the artist's chooser), refused with folder_outside_roots otherwise.
    folder: str | None = Field(default=None, min_length=1, max_length=2000)
    # The package's name (Deliver's Name field, lane w3-deliver): the folder and files are called
    # <name>-<kind>-<date-time>/<name>.<ext>; None = the timeline's name.
    name: str | None = Field(default=None, min_length=1, max_length=60)
    wait_ms: int = Field(default=0, ge=0, le=_TIMELINE_EXPORT_WAIT_MS_MAX)
    # Lane w23-subs (CUT-011, Resolve Deliver > Subtitle Settings): the cut's subtitle track ST1 as files next to the
    # package (<name>.srt + <name>.vtt; FCPXML also as captions), burned into the film too, or left out.
    subtitles: Literal["files", "burn", "off"] = "files"


class TimelineExportStatusRequest(_ProjectRequest):
    job_id: str = Field(pattern=r"^tlexp_[0-9a-f]{32}$", min_length=38, max_length=38)
    wait_ms: int = Field(default=0, ge=0, le=_TIMELINE_EXPORT_WAIT_MS_MAX)


class TimelineExportCancelRequest(_ProjectRequest):
    job_id: str = Field(pattern=r"^tlexp_[0-9a-f]{32}$", min_length=38, max_length=38)


class TimelineExportRevealRequest(_ProjectRequest):
    job_id: str = Field(pattern=r"^tlexp_[0-9a-f]{32}$", min_length=38, max_length=38)


def _timeline_export_open_folder(folder: Path) -> None:
    """Show the package folder in the system file manager (the artist's "Show in folder")."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", str(folder)], env=_child_env(desktop=True))  # noqa: S603,S607 - fixed program, path from our own job
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)], env=_child_env(desktop=True))  # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", str(folder)], env=_child_env(desktop=True))  # noqa: S603,S607


def _timeline_export_answer(state: dict[str, Any], project_dir: Path | None = None) -> dict[str, Any]:
    """Schema-tagged job state; the project directory never appears in an answer."""
    text = json.dumps({"schema": "studio.timeline-export-job/1", **state}, ensure_ascii=False)
    if project_dir is not None:
        for form in {str(project_dir), str(project_dir).replace("\\", "\\\\"), project_dir.as_posix()}:
            text = text.replace(form, "<project>")
    return json.loads(text)


@project_router.post("/projects/timeline/export")
async def projects_timeline_export(request: TimelineExportRequest) -> Any:
    try:
        package = _timeline_export_package()
    except Exception:  # noqa: BLE001 - finite plugin boundary
        return {"error": {"code": "export_unavailable", "message": "Timeline export is not installed."}}
    try:
        directory = _project_reference_directory(request.project_ref)
        manifest = _project_store_factory().read_project(directory, expected_project_id=request.project_ref.project_id)
        project_dir = Path(os.path.realpath(directory))
        roots = [*manifest["approved_roots"], str(project_dir)]

        def confine(raw: str) -> Path:
            try:
                return resolve_approved_path(raw, roots)
            except (OSError, ValueError):
                raise package.ExportError("source_outside_roots", "A source is outside the project's approved folders.") from None

        resolver_module = sys.modules[f"{_TIMELINE_EXPORT_PACKAGE_NAME}.resolver"]
        ffmpeg, ffprobe = Path(FFMPEG_PATH), Path(FFPROBE_PATH)
        document = request.document
        if document.get("project_id") not in (None, request.project_ref.project_id):
            return {"error": {"code": "project_scope_mismatch", "message": "The timeline belongs to another project."}}
        if request.range is not None:
            cropper = sys.modules[f"{_TIMELINE_EXPORT_PACKAGE_NAME}.export_range"]
            document = cropper.crop_document(document, request.range.in_frame, request.range.out_frame, timeline_id=request.timeline_id)
        # Lane p5d-frm (Project decision 2): Deliver's default Location is the project output folder (made on first
        # write); a chosen folder (Browse…) is used as before.
        export_root = Path(os.path.realpath(_project_output_dir(directory, request.project_ref.project_id, create=True)))
        if request.folder is not None:
            try:
                export_root = resolve_approved_path(request.folder, roots)
            except (OSError, ValueError):
                return {"error": {"code": "folder_outside_roots", "message": "The folder is outside the project's approved folders.",
                                  "retryable": False, "next": "Folder outside the project's media folders."}}
            if not export_root.is_dir():
                return {"error": {"code": "folder_missing", "message": "The folder does not exist.", "retryable": False, "next": "No folder at this path."}}
        bound: dict[str, str] = {}
        if request.sources or request.music:
            # The caller names the files (fixtures, relinked media): confined to approved roots.
            # Lane w11-cut M-99: the song is a sound clip file:<track_sha256> on the music track; a named music file
            # binds it too (an explicit source for that ref wins).
            named = {**{"file:" + sha: path for sha, path in request.music.items()}, **request.sources}
            resolver = resolver_module.ProbeResolver(named, request.music, ffprobe=ffprobe, confine=confine)
        else:
            # Default: the project's own resolver (routes/projects/timeline_1.py) binds every
            # take, picture and pinned song of this document to its published file.
            resolver, bound = await asyncio.to_thread(_timeline_export_bound_resolver, document, request.project_ref.project_id, ffprobe)
        plan = await asyncio.to_thread(package.build_plan, document, resolver, timeline_id=request.timeline_id)
        if not plan.project_id:
            plan.project_id = request.project_ref.project_id
        for song in getattr(resolver, "songs_changed", None) or ():
            if request.kind != "film":
                plan.warnings.append("The song changed since it was analysed; beats may be off")
        exporter = sys.modules[f"{_TIMELINE_EXPORT_PACKAGE_NAME}.export_package"]
        # Lane w23-subs: the stored subtitles of the exported cut, cropped and rebased like the range.
        subtitles = None if request.subtitles == "off" else await asyncio.to_thread(
            _subtitles_for_export, request.project_ref.project_id, directory, plan.timeline_id, request.document)
        if subtitles is not None and request.range is not None:
            subtitles = sys.modules[f"{_TIMELINE_EXPORT_PACKAGE_NAME}.subtitles"].crop(subtitles, request.range.in_frame, request.range.out_frame)
        if subtitles is not None and not subtitles["cues"]:
            subtitles = None
        key = hashlib.sha256(json.dumps(
            [request.project_ref.project_id, request.kind, request.quality, request.timeline_id, document, request.sources, request.music, bound,
             str(export_root), *(["music_dry"] if request.music_dry else []), *([["name", request.name]] if request.name else []),
             *([request.loudness.model_dump()] if request.loudness is not None else []),
             *([["subtitles", request.subtitles, subtitles]] if subtitles is not None else [])],
            sort_keys=True, ensure_ascii=False, default=str,
        ).encode("utf-8")).hexdigest()

        def work(progress: Any, should_cancel: Any) -> dict[str, Any]:
            # Lane p7-cut item 4 (PRJ-015, Project decision 10): no typed name = the project rule's name for page
            # deliver, the package folder and the film inside (<rule>.mp4); the rule's counter moves once per export.
            rule = None if request.name else _project_result_name(directory, export_root, manifest.get("name"), "deliver", "")
            result = exporter.export_package(
                plan, export_root, request.kind, ffmpeg=ffmpeg, ffprobe=ffprobe, quality=request.quality,
                progress=progress, should_cancel=should_cancel, **({"music_dry": True} if request.music_dry else {}),
                **({"name": request.name} if request.name else {"name": rule, "folder_name": rule}),
                **({"loudness": request.loudness.model_dump()} if request.loudness is not None else {}),
                **({"subtitles": subtitles, "burn_subtitles": request.subtitles == "burn"} if subtitles is not None else {}),
            )
            # Kept backend-side for /export/reveal; the answer stays path-free.
            _TIMELINE_EXPORT_STATE.setdefault("folders", {})[key] = result.folder
            files = {key: entry["name"] for key, entry in result.manifest["files"].items() if not key.startswith("media:")}
            inside = export_root == project_dir / "exports" / "timeline"  # (kept for old answers; see below)
            return {
                # Relative to the project for the default place, else the chosen folder's full path.
                # Lane p5d-frm: a folder inside the project (the default output/) is answered relative to it, path-free.
                "folder": Path(os.path.relpath(result.folder, project_dir)).as_posix() if Path(os.path.realpath(result.folder)).is_relative_to(project_dir) else str(result.folder),
                "range": None if request.range is None else {"in_frame": request.range.in_frame, "out_frame": request.range.out_frame},
                "files": files,
                "media_files": sum(1 for key in result.manifest["files"] if key.startswith("media:")),
                # Lane w24-rest (CUT-006 05.11.): the package's own files on disk (film, sound, timeline files; media copies
                # excluded), the size each Render queue card shows (Resolve / Media Encoder list the output size per job).
                "bytes": sum(int(entry.get("bytes") or 0) for key, entry in result.manifest["files"].items() if not key.startswith("media:")),
                "total_frames": result.manifest["total_frames"],
                # One shape for every kind (fcpxml, fcp7, otio), built by the exporter; edl has none.
                "verification": (result.manifest["verification"] or {}).get("summary"),
                "warnings": result.manifest["warnings"],
                # Sound tracks A1-A3 (film): measured LUFS / true peak of the mix before and of the film
                # after loudnorm, and the stems' format; None for a cut without sound tracks.
                "audio": result.manifest.get("audio"),
                # A film master's ffprobe read-back (codec, profile, frames, duration, PCM); None otherwise.
                "master": result.manifest.get("master"),
                # Lane w23-subs: {cues, burned_in, fcpxml_captions, preset} when the package carries subtitles.
                **({"subtitles": result.manifest["subtitles"]} if "subtitles" in result.manifest else {}),
            }

        jobs = _timeline_export_jobs_for(request.project_ref.project_id, directory)
        # The render queue (lane w2-cutdel) lists each job with what it renders and its project.
        label = f"{request.name or plan.name} · {request.kind}" + (f" · {request.quality}" if request.kind == "film" else "")
        stored = request.model_dump(mode="json", by_alias=True, exclude={"wait_ms"})
        state = jobs.start(key, request.kind, work, label=label, project_id=request.project_ref.project_id,
                           request=stored, settings=_timeline_export_settings(request))
        _TIMELINE_EXPORT_STATE.setdefault("keys", {})[state["job_id"]] = key
        # Lane w27-rows (IMG-213 scope, owner 08.11. 09:22: tlexp jobs Hermes started read by "artist"): the new export
        # job's author is the caller, noted like a Shoot / Image job (job_authors_1.py; agent tool = "hermes").
        _job_authors_note([state["job_id"]])
        if request.wait_ms and state["state"] in {"waiting", "running"}:
            state = await jobs.wait(state["job_id"], request.wait_ms) or state
        return _timeline_export_answer(state, project_dir)
    except studio_core.ProjectError as exc:
        return _project_error_response(exc)
    except package.ExportError as exc:
        body = _timeline_export_answer({"error": exc.as_dict()}, project_dir if "project_dir" in locals() else None)
        body.pop("schema", None)
        return body
    except Exception:  # noqa: BLE001 - finite plugin boundary
        return {"error": {"code": "export_failed", "message": "The export failed."}}


@project_router.post("/projects/timeline/export/status")
async def projects_timeline_export_status(request: TimelineExportStatusRequest) -> Any:
    jobs = await asyncio.to_thread(_timeline_export_jobs_knowing, request.job_id)  # BCK-227: queue files off the loop
    state = None if jobs is None else await jobs.wait(request.job_id, request.wait_ms)
    if state is None:
        return JSONResponse(status_code=404, content={"error": {"code": "job_unknown", "message": "Export job is unknown"}})
    return _timeline_export_answer(state)


@project_router.post("/projects/timeline/export/cancel")
async def projects_timeline_export_cancel(request: TimelineExportCancelRequest) -> Any:
    jobs = await asyncio.to_thread(_timeline_export_jobs_knowing, request.job_id)  # BCK-227: Stop never waits on the loop
    state = None if jobs is None else await asyncio.to_thread(jobs.cancel, request.job_id)
    if state is None:
        return JSONResponse(status_code=404, content={"error": {"code": "job_unknown", "message": "Export job is unknown"}})
    return _timeline_export_answer(state)


@project_router.post("/projects/timeline/export/reveal")
async def projects_timeline_export_reveal(request: TimelineExportRevealRequest) -> Any:
    """Open a finished export's folder in the file manager (one done job of this session)."""
    # Lane w20-dlv: a done job of the queue keeps Show in folder after a restart and from Hermes' process.
    jobs = _timeline_export_jobs_knowing(request.job_id)
    if jobs is None:
        return JSONResponse(status_code=404, content={"error": {"code": "job_unknown", "message": "Export job is unknown"}})
    found = _timeline_export_done_folder(request.job_id)
    state = _timeline_export_stored(jobs, request.job_id)[1]
    folder = None if found is None else found[1]
    if state["state"] != "done" or folder is None or not Path(folder).is_dir():
        return {"error": {"code": "not_done", "message": "The export has no folder to show.", "retryable": False, "next": "The export is not done."}}
    try:
        _timeline_export_open_folder(Path(folder))
    except OSError:
        return {"error": {"code": "reveal_failed", "message": "The folder could not be shown.", "retryable": True, "next": ""}}
    return {"ok": True, "job_id": request.job_id, "shown": True}
