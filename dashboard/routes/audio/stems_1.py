from __future__ import annotations


# --- Audio page, Edit: stems (lane w3-audio, board E15 Edit ⑤/⑥/⑧) --------------------------------------------
#
# Separate one sound file of the approved folders on the local ComfyUI, with node packs already installed there
# (generation/audio_stems.py; graphs pinned in workflows/audio-stems-v1): "Vocals + rest" (Mel-Band RoFormer) or
# "4 stems" (Hybrid Demucs). A job like the song analysis (start / status / stop): start answers at once, the work
# reads GET /queue before its submit and only appends; Stop stops waiting, it never cancels the engine. The stems are
# kept per (content sha256, split) under the Suite's state folder and reused; "Add stems to Media" copies them into
# the project's output folder (Stems/<song>/) where Media lists them as sound files. Every route is an agent
# tool (agent_tools.ROUTE_TOOLS) with the answer the page reads.

def _audio_stems_load() -> Any:
    existing = sys.modules.get("film_suite_audio_stems")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("film_suite_audio_stems", Path(__file__).with_name("generation") / "audio_stems.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("The stems module is unavailable.")
    module = importlib.util.module_from_spec(spec)
    sys.modules["film_suite_audio_stems"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # w19-g1fresh: a failed import must not leave a half module behind for the next caller.
        sys.modules.pop("film_suite_audio_stems", None)
        raise
    return module


_audio_stems_module = _audio_stems_load()
_AUDIO_STEMS_JOBS: dict[str, dict[str, Any]] = {}
_AUDIO_STEMS_LOCK = _thread.allocate_lock()
_AUDIO_STEMS_KEEP = 16


class AudioStemsRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    approved_roots: list[str] = Field(default_factory=list, max_length=32)
    project_ref: ProjectReference | None = None
    split: Literal["vocals_rest", "four"] = "four"


class AudioStemsJobRequest(BaseModel):
    job_id: str = Field(pattern=r"^stm_[0-9a-f]{32}$")


class AudioStemsSoundRequest(AudioStemsRequest):
    audible: list[str] = Field(min_length=1, max_length=4)


class AudioStemsToMediaRequest(AudioStemsRequest):
    project_ref: ProjectReference
    stems: list[str] | None = Field(default=None, max_length=4)


def _audio_stems_dir(digest: str, split: str) -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(state_root(Path(base) / "hermes")) / "stems" / digest / split


def _audio_stems_error(exc: BaseException) -> JSONResponse:
    code = getattr(exc, "code", None)
    if isinstance(exc, _audio_stems_module.StemsError):
        return JSONResponse(status_code=422, content={"error": {"code": code, "message": str(exc)}})
    if isinstance(exc, _FolderNotApproved):
        return JSONResponse(status_code=403, content=_folder_refusal())
    if isinstance(exc, RecipeError):
        return JSONResponse(status_code=422, content={"error": {"code": "stems_source_refused", "message": str(exc)}})
    if isinstance(exc, studio_core.ProjectError):
        return JSONResponse(status_code=404 if exc.code == "project_not_found" else 503, content={"error": {"code": exc.code, "message": "The project could not be read"}})
    return JSONResponse(status_code=503, content={"error": {"code": "stems_unavailable", "message": "Stems are unavailable"}})


def _audio_stems_source(request: AudioStemsRequest) -> tuple[Path, str]:
    source = _confined_local_path(request.path, _request_roots(request.approved_roots, request.project_ref, path=request.path, server_list=True))
    if MEDIA_EXTENSIONS.get(source.suffix.lower()) != "audio":
        raise _audio_stems_module.StemsError("stems_not_sound", "Only a sound file has stems")
    return source, _sha256_file_hex(source)


def _sha256_file_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _audio_stems_public(job: dict[str, Any]) -> dict[str, Any]:
    out = {"ok": True, "job_id": job["job_id"], "split": job["split"], "status": job["status"], "progress": round(float(job["progress"]), 3), "stage": job["stage"]}
    if job["status"] == "done":
        out["result"] = job["result"]
    if job["status"] == "error":
        out["error"] = job["error"]
    return out


def _audio_stems_comfy() -> Any:
    """The configured local engine (comfy_endpoint), through the voice runner's loopback client (upload, queue, submit, history, view)."""
    return _voice_runner_module.ComfyHTTP(client_id=_comfyui_preview_module.PREVIEW_CLIENT_ID, progress=_voice_progress)


async def _audio_stems_health() -> dict[str, Any]:
    client = _comfy_client_module.ComfyUIClient()
    out: dict[str, Any] = {}
    try:
        await client.system_stats()
        info: dict[str, Any] = {}
        models: dict[str, Any] = {}
        for split in _audio_stems_module.SPLITS:
            entry, _graph = _audio_stems_module.load_split(split)
            for name in entry["required_classes"]:
                if name not in info:
                    try:
                        answer = await client.object_info(name)
                        if isinstance(answer, dict) and name in answer:
                            info[name] = answer[name]
                    except Exception:  # noqa: BLE001 - a missing class is a reason
                        pass
            for model in entry["models"]:
                if model["folder"] not in models:
                    try:
                        models[model["folder"]] = await client.models(model["folder"])
                    except Exception:  # noqa: BLE001
                        models[model["folder"]] = None
    except Exception:  # noqa: BLE001 - unreachable engine
        info, models = None, {}
    for split in _audio_stems_module.SPLITS:
        out[split] = _audio_stems_module.health(split, info, models)
    return out


def _audio_stems_run(job: dict[str, Any], source: Path, digest: str) -> None:
    def progress(fraction: float, stage: str) -> None:
        job["progress"], job["stage"] = max(job["progress"], min(0.99, float(fraction))), stage
    try:
        result = _audio_stems_module.run(source=source, split=job["split"], comfy=_audio_stems_comfy(), out_dir=_audio_stems_dir(digest, job["split"]),
                                         ffmpeg=FFMPEG_PATH, on_progress=progress, should_stop=lambda: job["stop"])
        job["result"] = result
        job["status"], job["progress"], job["stage"] = "done", 1.0, "done"
    except _audio_stems_module.StemsError as exc:
        if exc.code == "stems_stopped":
            job["status"], job["stage"] = "stopped", "stopped"
        else:
            job["status"], job["stage"], job["error"] = "error", "error", {"code": exc.code, "message": str(exc)}
    except Exception:  # noqa: BLE001 - the job reports a plain failure
        job["status"], job["stage"], job["error"] = "error", "error", {"code": "stems_failed", "message": "The separation failed"}


def _audio_stems_start_sync(request: AudioStemsRequest) -> dict[str, Any]:
    source, digest = _audio_stems_source(request)
    kept = _audio_stems_module.stored(_audio_stems_dir(digest, request.split))
    if kept is not None:
        return {"ok": True, "job_id": None, "split": request.split, "status": "done", "progress": 1.0, "stage": "stored", "result": kept}
    with _AUDIO_STEMS_LOCK:
        for job in _AUDIO_STEMS_JOBS.values():
            if job["digest"] == digest and job["split"] == request.split and job["status"] == "running":
                return _audio_stems_public(job)
        job = {"job_id": f"stm_{uuid.uuid4().hex}", "digest": digest, "split": request.split, "status": "running", "progress": 0.0,
               "stage": "starting", "stop": False, "result": None, "error": None, "started": time.time()}
        _AUDIO_STEMS_JOBS[job["job_id"]] = job
        for old in sorted(_AUDIO_STEMS_JOBS.values(), key=lambda row: row["started"])[:-_AUDIO_STEMS_KEEP]:
            if old["status"] != "running":
                _AUDIO_STEMS_JOBS.pop(old["job_id"], None)
    _thread.start_new_thread(_audio_stems_run, (job, source, digest))
    return _audio_stems_public(job)


@router.post("/audio/stems/start")
async def audio_stems_start(request: AudioStemsRequest):
    """Separate a sound file into stems on the local engine (or answer the stored stems at once)."""
    try:
        health = (await _audio_stems_health()).get(request.split) or {}
        source, digest = await asyncio.to_thread(_audio_stems_source, request)
        if _audio_stems_module.stored(_audio_stems_dir(digest, request.split)) is None and not health.get("ready"):
            raise _audio_stems_module.StemsError("stems_not_ready", "; ".join(health.get("reasons") or ["The local engine is not ready"]))
        return await asyncio.to_thread(_audio_stems_start_sync, request)
    except Exception as exc:  # noqa: BLE001 - finite boundary
        return _audio_stems_error(exc)


@router.post("/audio/stems/status")
async def audio_stems_status(request: AudioStemsJobRequest):
    job = _AUDIO_STEMS_JOBS.get(request.job_id)
    if job is None:
        return {"ok": False, "reason_code": "stems_job_unknown", "message": "No such separation (it ended long ago or the backend restarted)."}
    return _audio_stems_public(job)


@router.post("/audio/stems/stop")
async def audio_stems_stop(request: AudioStemsJobRequest):
    """Stop waiting for a separation; the engine is never cancelled and nothing is kept."""
    job = _AUDIO_STEMS_JOBS.get(request.job_id)
    if job is None:
        return {"ok": False, "reason_code": "stems_job_unknown", "message": "No such separation."}
    job["stop"] = True
    return _audio_stems_public(job)


@router.post("/audio/stems/read")
async def audio_stems_read(request: AudioStemsRequest):
    """The stored stems of a file and split (null when not separated yet), and whether each split can run here."""
    try:
        source, digest = await asyncio.to_thread(_audio_stems_source, request)
        kept = await asyncio.to_thread(_audio_stems_module.stored, _audio_stems_dir(digest, request.split))
        return {"ok": True, "split": request.split, "result": kept, "splits": await _audio_stems_health(),
                "labels": {sid: entry["label"] for sid, entry in _audio_stems_module.manifest()["splits"].items()},
                "models": {sid: entry["model_label"] for sid, entry in _audio_stems_module.manifest()["splits"].items()}}
    except Exception as exc:  # noqa: BLE001
        return _audio_stems_error(exc)


def _audio_stems_sound_sync(request: AudioStemsSoundRequest) -> dict[str, Any]:
    _source, digest = _audio_stems_source(request)
    folder = _audio_stems_dir(digest, request.split)
    kept = _audio_stems_module.stored(folder)
    if kept is None:
        raise _audio_stems_module.StemsError("stems_missing", "Stems not separated")
    scratch = folder.parent / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    data = _audio_stems_module.mix_audible(folder, kept, list(request.audible), FFMPEG_PATH, scratch)
    return {"ok": True, "mime": "audio/flac", "audible": [s["id"] for s in kept["stems"] if s["id"] in request.audible], "data_b64": base64.b64encode(data).decode("ascii")}


@router.post("/audio/stems/sound")
async def audio_stems_sound(request: AudioStemsSoundRequest):
    """The audible stems (Solo / Mute) summed, as FLAC (base64) for listening; never a path."""
    try:
        return await asyncio.to_thread(_audio_stems_sound_sync, request)
    except Exception as exc:  # noqa: BLE001
        return _audio_stems_error(exc)


def _audio_stems_to_media_sync(request: AudioStemsToMediaRequest) -> dict[str, Any]:
    source, digest = _audio_stems_source(request)
    folder = _audio_stems_dir(digest, request.split)
    kept = _audio_stems_module.stored(folder)
    if kept is None:
        raise _audio_stems_module.StemsError("stems_missing", "Stems not separated")
    directory = _project_reference_directory(request.project_ref)
    # lane p5c-frm: under the project's output folder (Project charter decision 2), not its first approved folder.
    target = _project_output_dir(directory, request.project_ref.project_id, create=True) / "Stems" / (_voice_runner_module.safe_name(source.stem, 48) or "song")
    target.mkdir(parents=True, exist_ok=True)
    wanted = [s for s in kept["stems"] if request.stems is None or s["id"] in request.stems]
    added = []
    # lane p6-name (Project decision 10): one take of the project rule per separation added, "<rule> · <stem>.flac";
    # a stem already added (same bytes) keeps its file, so adding again writes nothing new.
    rule_stem: str | None = None
    for stem in wanted:
        data = (folder / f"{stem['id']}.flac").read_bytes()
        out = next((p for p in sorted(target.glob(f"* · {stem['label']}.flac")) if p.is_file() and p.read_bytes() == data), None)
        if out is None:
            if rule_stem is None:
                rule_stem = _project_result_name(directory, target, _voice_project_name(directory, request.project_ref.project_id), "audio", "")
            out = target / f"{rule_stem} · {stem['label']}.flac"
        if not out.exists() or out.read_bytes() != data:
            temporary = out.with_name(f".{out.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(data)
            os.replace(temporary, out)
        added.append({"id": stem["id"], "label": stem["label"], "name": out.name, "item_id": "file:" + hashlib.sha256(data).hexdigest()})
    try:
        film_events.media_changed(f"film_owner:{request.project_ref.project_id}")
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "folder": target.name, "added": added}


@router.post("/audio/stems/to-media")
async def audio_stems_to_media(request: AudioStemsToMediaRequest):
    """Add the stems to Media: copied into the project's output folder (Stems/<song>/)."""
    try:
        return await asyncio.to_thread(_audio_stems_to_media_sync, request)
    except Exception as exc:  # noqa: BLE001
        return _audio_stems_error(exc)
