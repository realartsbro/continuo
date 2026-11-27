from __future__ import annotations


# --- Control layers (lane fs-ctrl) --------------------------------------------------------------
#
# Pose, depth, edges, line art and camera move derived from one source of the scope: a finished
# take (studio:...), a published picture (imgjob_...), or a file of the approved folders (path +
# approved_roots). The work lives in dashboard/control (extract.py, camera.py: the measured choice
# of extractors); this file is the HTTP face:
#
#   POST /media/control/extract  start one extraction (answers at once from the cache; else a job)
#   POST /media/control/status   read / long-poll a job (wait_ms)
#   POST /media/control/stop     stop after the layer that is running (ComfyUI is never cancelled)
#   POST /media/control/list     the derived layers of the scope (or of one source), grouped by source
#   POST /media/control/use      the hand-off of one layer: motion guide / camera sentence / picture control
#
# Layers of a project live in <project>/media/control/, of No project in the profile's plugin data.
# Each is a Media item (kind video | image | text) with derived_from + layer; its id is file:<sha256 of
# the layer file>, so the Media marks (Kept, rating, tags) take it like any file of the folders. The layer folder is the
# one folder Shoot needs to approve to send a motion guide (``use`` answers it as approve_root).
# Paths in the answers are files the Suite made in that folder, never a ledger's private path.

_CONTROL_PACKAGE_NAME = "film_suite_control"
_CONTROL_SCHEMA = "studio.control-layers/1"
_CONTROL_SOURCE_PATTERN = r"^(imgjob_[0-9a-f]{32}|" + _take_ids_module.STUDIO_JOB_ID_BODY + r")$"
_CONTROL_ITEM_PATTERN = r"^file:[0-9a-f]{64}$"
_CONTROL_SERVICE: list[Any] = []


def _control_package() -> Any:
    with _PACKAGE_LOAD_LOCK:
        package = sys.modules.get(_CONTROL_PACKAGE_NAME)
        if package is not None:
            return package
        directory = Path(__file__).with_name("control")
        spec = importlib.util.spec_from_file_location(_CONTROL_PACKAGE_NAME, directory / "__init__.py", submodule_search_locations=[str(directory)])
        if spec is None or spec.loader is None:
            raise RuntimeError("The control layer package is unavailable.")
        package = importlib.util.module_from_spec(spec)
        sys.modules[_CONTROL_PACKAGE_NAME] = package
        try:
            spec.loader.exec_module(package)
            importlib.import_module(f"{_CONTROL_PACKAGE_NAME}.camera")
            importlib.import_module(f"{_CONTROL_PACKAGE_NAME}.extract")
        except BaseException:
            for name in list(sys.modules):
                if name == _CONTROL_PACKAGE_NAME or name.startswith(_CONTROL_PACKAGE_NAME + "."):
                    sys.modules.pop(name, None)
            raise
        return package


def _control_extract() -> Any:
    _control_package()
    return sys.modules[f"{_CONTROL_PACKAGE_NAME}.extract"]


def _control_service() -> Any:
    if not _CONTROL_SERVICE:
        module = _control_extract()
        _CONTROL_SERVICE.append(module.ControlService(comfy_factory=lambda: _comfy_client_module.ComfyUIClient(), ffmpeg=FFMPEG_PATH, ffprobe=FFPROBE_PATH))
    return _CONTROL_SERVICE[0]


class ControlExtractRequest(_ProjectRequest):
    schema_: Literal["studio.control-layers/1"] = Field(alias="schema")
    owner_scope: str = Field(pattern=_OWNER_SCOPE_PATTERN, min_length=18, max_length=47)
    # A result of the scope (take or picture) by its id, or a file of the approved folders by path.
    source_id: str | None = Field(default=None, pattern=_CONTROL_SOURCE_PATTERN, min_length=38, max_length=43)
    path: str | None = Field(default=None, min_length=1, max_length=4096)
    approved_roots: list[str] = Field(default_factory=list)
    project_ref: ProjectReference | None = None
    layers: list[Literal["pose", "depth", "edges", "lineart", "camera"]] = Field(min_length=1, max_length=5)
    range_in: float | None = Field(default=None, ge=0, le=36000)
    range_out: float | None = Field(default=None, gt=0, le=36000)
    wait_ms: int = Field(default=0, ge=0, le=20_000)


class ControlJobRequest(_ProjectRequest):
    schema_: Literal["studio.control-layers/1"] = Field(alias="schema")
    owner_scope: str = Field(pattern=_OWNER_SCOPE_PATTERN, min_length=18, max_length=47)
    job_id: str = Field(pattern=r"^ctrljob_[0-9a-f]{32}$")
    wait_ms: int = Field(default=0, ge=0, le=20_000)


class ControlListRequest(_ProjectRequest):
    schema_: Literal["studio.control-layers/1"] = Field(alias="schema")
    owner_scope: str = Field(pattern=_OWNER_SCOPE_PATTERN, min_length=18, max_length=47)
    derived_from: str | None = Field(default=None, min_length=5, max_length=80)


class ControlUseRequest(_ProjectRequest):
    schema_: Literal["studio.control-layers/1"] = Field(alias="schema")
    owner_scope: str = Field(pattern=_OWNER_SCOPE_PATTERN, min_length=18, max_length=47)
    item_id: str = Field(pattern=_CONTROL_ITEM_PATTERN)
    use: Literal["motion_guide", "camera", "image_control"]


def _control_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _control_store(owner_scope: str) -> Any:
    project_id = _media_marks_project(owner_scope)
    if project_id is not None:
        directory = Path(_recent_directory(_recent_project_store_factory(), project_id))
        root = directory / "media" / "control"
    else:
        home = _active_hermes_home()
        if home is None:
            raise studio_core.ProjectError("project_unavailable", "no profile home")
        root = Path(state_root(Path(home) / "plugin-data", "control-layers"))
    return _control_extract().Store(root)


def _control_source(request: ControlExtractRequest) -> Any:
    """The source as a readable local file of this scope, or a ControlError/RecipeError."""
    extract = _control_extract()
    suffix = request.owner_scope[len("film_owner:"):]
    project_id = None if suffix == "scratch" else suffix
    if request.source_id and request.source_id.startswith("imgjob_"):
        with _IMAGE_FILM_ROOT_LOCK:
            root = _image_producer_root(request.owner_scope)
            handoff = _image_operations_handoff(root, request.owner_scope)
            jobs = handoff.producer_jobs()
            states = handoff.image_states()
            _collector, reader = handoff.result_services()
        try:
            job = jobs.get_job(request.source_id)
            states.require_owned(((job.get("request_snapshot") or {}).get("context_basis") or {}).get("context_id"))
        except Exception:  # noqa: BLE001 - unknown or another scope's: not found
            raise RecipeError("That picture is not in this project.") from None
        if job.get("state") != "published":
            raise RecipeError("The picture is not ready yet.")
        data, artifact = reader.read_published_job(request.source_id)
        staged = _stage_opening_frame_bytes(data, artifact["artifact_sha256"], artifact["mime_type"])
        label = _job_label(_image_human_prompt(request.source_id, ((job.get("request_snapshot") or {}).get("artist_input") or {}).get("prompt"))) or "Picture"
        return extract.Source(source_id=request.source_id, kind="image", path=Path(staged), label=label, sha256=artifact["artifact_sha256"])
    if request.source_id:
        try:
            record = _shoot_take(request.source_id, project_id)  # lane p8-cut: a copied project's takes (CUT-104)
        except Exception:  # noqa: BLE001
            raise RecipeError("That take is not in this project.") from None
        owner = getattr(record, "project_id", None)
        if owner is None:
            try:
                owner = (getattr(record, "request", None) or {}).get("project_id") or None
            except Exception:  # noqa: BLE001
                owner = None
        if owner != project_id:
            raise RecipeError("That take is not in this project.")
        if record.state != "ready" or not record.published_path or not Path(record.published_path).is_file():
            raise RecipeError("The take has no finished file yet.")
        path = Path(record.published_path)
        if MEDIA_EXTENSIONS.get(path.suffix.lower()) != "video":
            raise RecipeError("Only clips and pictures have control layers.")
        try:
            prompt = str(((record.request or {}).get("args") or {}).get("prompt") or "")
        except Exception:  # noqa: BLE001
            prompt = ""
        return extract.Source(source_id=request.source_id, kind="video", path=path, label=_job_label(prompt) or "Take")
    if not request.path:
        raise RecipeError("No take, picture or file of the approved folders named.")
    path = _confined_local_path(request.path, _request_roots(request.approved_roots, request.project_ref, path=request.path, server_list=True))
    kind = MEDIA_EXTENSIONS.get(path.suffix.lower())
    if kind not in ("video", "image"):
        raise RecipeError("Only clips and pictures have control layers.")
    sha = extract.sha256_file(path)
    return extract.Source(source_id=f"file:{sha}", kind=kind, path=path, label=path.name, sha256=sha)


def _control_public_item(item: Mapping[str, Any], store_root: Path) -> dict[str, Any]:
    extract = _control_extract()
    # The path is the layer's own file in the project's media/control folder (made by the Suite, not a
    # private ledger path): Media plays it from there and Shoot sends it as the motion guide.
    return extract.public_item(item)


def _control_failure(exc: BaseException) -> JSONResponse:
    extract = _control_extract()
    if isinstance(exc, extract.ControlError):
        status = 503 if exc.code == "comfy_down" else 422
        return _control_error(status, exc.code, str(exc))
    if isinstance(exc, _FolderNotApproved):
        return _control_error(403, _FolderNotApproved.reason_code, _folder_refusal(exc)["message"])
    if isinstance(exc, RecipeError):
        return _control_error(404 if "not in this project" in str(exc) else 422, "control_source", str(exc))
    code = getattr(exc, "code", None)
    if code == "project_not_found":
        return _control_error(404, "project_not_found", "Project not found")
    return _control_error(503, "control_unavailable", "Control layers are unavailable right now.")


def _control_job_answer(job: Any, store: Any) -> dict[str, Any]:
    answer = job.public()
    answer["items"] = [_control_public_item(item, store.root) for item in job.items]
    return answer


@router.post("/media/control/extract")
async def media_control_extract(request: ControlExtractRequest):
    """Make the asked control layers of one clip or picture of this scope (cached per content, layer,
    extractor and range). Answers the job at once; ``wait_ms`` waits up to 20 s for it."""
    try:
        store = await asyncio.to_thread(_control_store, request.owner_scope)
        source = await asyncio.to_thread(_control_source, request)
        service = _control_service()
        source = await asyncio.to_thread(service.describe_source, source)
        job = service.start(store, request.owner_scope, source, list(request.layers), request.range_in, request.range_out)
        if request.wait_ms:
            job = await service.wait(job.job_id, request.wait_ms)
        return _control_job_answer(job, store)
    except Exception as exc:  # noqa: BLE001 - finite plugin boundary
        return _control_failure(exc)


@router.post("/media/control/status")
async def media_control_status(request: ControlJobRequest):
    """One extraction job: state (waiting | running | done | failed | stopped), the layer running, the
    layers done with their Media items, and a plain sentence per layer that could not be made."""
    service = _control_service()
    job = await service.wait(request.job_id, request.wait_ms)
    if job is None or job.owner_scope != request.owner_scope:
        return _control_error(404, "control_job_unknown", "That extraction is not known (it may be from before a restart).")
    try:
        store = await asyncio.to_thread(_control_store, request.owner_scope)
    except Exception as exc:  # noqa: BLE001
        return _control_failure(exc)
    return _control_job_answer(job, store)


@router.post("/media/control/stop")
async def media_control_stop(request: ControlJobRequest):
    """Stop an extraction after the layer that is running now (a ComfyUI prompt is never cancelled)."""
    service = _control_service()
    job = service.jobs.get(request.job_id)
    if job is None or job.owner_scope != request.owner_scope:
        return _control_error(404, "control_job_unknown", "That extraction is not known (it may be from before a restart).")
    service.stop(request.job_id)
    store = await asyncio.to_thread(_control_store, request.owner_scope)
    return _control_job_answer(job, store)


@router.post("/media/control/list")
async def media_control_list(request: ControlListRequest):
    """The control layers of this scope as Media items (kind video | image | text, layer, derived_from),
    grouped under their source; ``derived_from`` narrows to one source."""
    try:
        store = await asyncio.to_thread(_control_store, request.owner_scope)
        items = await asyncio.to_thread(store.items, request.derived_from)
    except Exception as exc:  # noqa: BLE001
        return _control_failure(exc)
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(item["derived_from"], []).append(_control_public_item(item, store.root))
    return {"schema": _CONTROL_SCHEMA, "owner_scope": request.owner_scope,
            "groups": [{"derived_from": key, "source_label": value[0].get("source_label"), "source_kind": value[0].get("source_kind"), "items": value}
                       for key, value in groups.items()],
            "count": len(items)}


@router.post("/media/control/use")
async def media_control_use(request: ControlUseRequest):
    """The hand-off of one layer, the same for Media's buttons and the agent: ``motion_guide`` (a layer clip
    for Shoot's Motion guide slot: path + the one folder to approve), ``camera`` (the camera sentence for the
    prompt's camera part), ``image_control`` (a layer picture for Image's control input)."""
    try:
        store = await asyncio.to_thread(_control_store, request.owner_scope)
        item = await asyncio.to_thread(store.get, request.item_id)
        if item is None:
            return _control_error(404, "control_item_unknown", "That layer is not in this project.")
        answer = _control_extract().handoff(item, request.use)
        if request.use == "motion_guide" and _media_marks_project(request.owner_scope) is None:
            # No project keeps its layers in the profile's private data, which a take may not read
            # (approved folders exclude HERMES_HOME). A project keeps them in <project>/media/control.
            return _control_error(409, "control_needs_project", "Motion guides need a project: open or create one, then extract the layer there.")
    except Exception as exc:  # noqa: BLE001
        return _control_failure(exc)
    if "path" in answer:
        answer["approve_root"] = str(store.root)
    return {"schema": _CONTROL_SCHEMA, "owner_scope": request.owner_scope, **answer}
