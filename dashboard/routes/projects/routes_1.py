from __future__ import annotations


@project_router.get("/projects/recent")
async def projects_recent() -> dict[str, Any]:
    """Read-only recent-project projection; no repair is attempted here."""
    try:
        return await asyncio.to_thread(_projects_recent_sync)  # BCK-227
    except studio_core.ProjectError as exc:
        return _project_error_response(exc)


def _projects_recent_sync() -> dict[str, Any]:
    store = _recent_project_store_factory()
    result = store.list_recent()
    try:  # read-only projection: a missing or corrupt registry answers exactly as list_recent does
        entries, _snapshot = store._read_entries()
    except (studio_core.ProjectError, OSError, ValueError, TypeError, UnicodeError):
        entries = None
    directories = {e.get("project_id"): e.get("directory") for e in entries or []}
    for project in result.get("projects", []):
        directory = directories.get(project.get("project_id"))
        if isinstance(directory, str) and (Path(directory) / ".continuo-building").is_file():
            project.update(available=False, state="incomplete")
    return result


def _refuse_incomplete(directory: str) -> None:
    if (Path(directory) / ".continuo-building").is_file():
        raise studio_core.ProjectError("project_incomplete", "This project was not finished being created.")


# Lane w24-models item 3: opening a project never depends on a module's state.  Resolve and Premiere open a
# project whose media or plug-in is offline and mark only that item; ComfyUI loads a workflow whose model is
# missing.  So the project store decides whether a project opens; the Image/Film root is a module: when it
# cannot come up (its ledger, key or a dependency), the project still opens, the answer names the module's
# state under ``modules`` (only then), and the Image page shows its own named state.  A project identity
# refusal from the root (another context's project) stays a refusal.
_MODULE_IDENTITY_CODES = frozenset({"project_scope_mismatch"})


def _module_root_or_state() -> tuple[Any, dict[str, Any] | None]:
    try:
        return _image_film_root(), None
    except _ImageFilmCompositionError as exc:
        return None, _module_state(exc)


def _module_state(exc: BaseException) -> dict[str, Any]:
    answer = _image_film_error_response(exc)  # logs the cause chain; path-free code + message
    error = answer.get("error") if isinstance(answer, dict) else None
    error = error if isinstance(error, dict) else {}
    return {"image": {"state": "unavailable", "code": str(error.get("code") or "image_film_unavailable"),
                      "message": str(error.get("message") or "Image state is unavailable")}}


def _module_open(root: Any, directory: str, project_id: str) -> dict[str, Any] | None:
    """Hand the opened project to the Image/Film root; its failure is the module's state, never the project's."""
    if root is None:
        return None
    try:
        root.open_project(directory, expected_project_id=project_id)
        return None
    except _ImageFilmCompositionError as exc:
        if getattr(exc, "code", None) in _MODULE_IDENTITY_CODES:
            raise
        return _module_state(exc)


def _with_modules(payload: dict[str, Any], modules: dict[str, Any] | None) -> dict[str, Any]:
    if modules:
        payload["modules"] = modules
    return payload


@project_router.post("/projects/create")
async def projects_create(request: ProjectCreateRequest) -> dict[str, Any]:
    return await asyncio.to_thread(_projects_create_sync, request)  # BCK-227: the whole create off the event loop


def _projects_create_sync(request: ProjectCreateRequest, *, allow_building_marker: bool = False) -> dict[str, Any]:
    try:
        root, modules = _module_root_or_state()
        create_kwargs: dict[str, Any] = {}
        if allow_building_marker:
            create_kwargs["allow_building_marker"] = True
        if request.copy_state is not None:
            if _film_caller() == "hermes" and request.copy_state.get("approvedRoots"):
                return _folder_refusal()  # lane p5-prj (HER-012): a Hermes-made project approves no folder
            create_kwargs["approved_roots"] = request.copy_state.get("approvedRoots", [])
            create_kwargs["copy_state"] = request.copy_state
        opened = _recent_project_store_factory().create_project(
            request.directory,
            request.name,
            **create_kwargs,
        )
        # A public project is usable by Image/Film only after the trusted root
        # has accepted the same project identity and cached its opaque bundle.
        # Initialize the root first: ProjectStore's PathBoundary may create the
        # profile key, so trusted enrollment must own that first bootstrap.
        # Keep the bundle handoff after the store's successful create.
        modules = _module_open(root, request.directory, opened["project"]["project_id"]) or modules
        return _with_modules(_public_project_payload(opened), modules)
    except studio_core.ProjectError as exc:
        return _project_error_response(exc)
    except _ImageFilmCompositionError as exc:
        return _image_film_error_response(exc)


@project_router.post("/projects/open")
async def projects_open(request: ProjectOpenRequest) -> dict[str, Any]:
    return await asyncio.to_thread(_projects_open_sync, request)  # BCK-227: the whole open off the event loop


def _projects_open_sync(request: ProjectOpenRequest) -> dict[str, Any]:
    try:
        _refuse_incomplete(request.directory)
        root, modules = _module_root_or_state()
        opened = _recent_project_store_factory().open_project(request.directory)
        if isinstance(opened.get("moved_from"), str):
            _rebase_project_footage(opened["project"]["project_id"], opened["moved_from"], request.directory)
        modules = _module_open(root, request.directory, opened["project"]["project_id"]) or modules
        return _with_modules(_public_project_payload(opened), modules)
    except studio_core.ProjectError as exc:
        return _project_error_response(exc)
    except _ImageFilmCompositionError as exc:
        return _image_film_error_response(exc)


def _recent_directory(store: Any, project_id: str) -> str:
    """Resolve a recent ID only inside the backend; never return this path."""
    try:
        entries, _snapshot = store._read_entries()
    except (OSError, TypeError, ValueError, UnicodeError):
        raise studio_core.ProjectError("project_unavailable", "recent project is unavailable")
    if not entries:
        raise studio_core.ProjectError("project_not_found", "recent project is unavailable")
    for entry in entries:
        if entry.get("project_id") == project_id:
            directory = entry.get("directory")
            if isinstance(directory, str) and directory:
                return directory
            break
    raise studio_core.ProjectError("project_not_found", "recent project is unavailable")


@project_router.post("/projects/open-recent")
async def projects_open_recent(request: ProjectOpenRecentRequest) -> dict[str, Any]:
    return await asyncio.to_thread(_projects_open_recent_sync, request)  # BCK-227: off the event loop


def _projects_open_recent_sync(request: ProjectOpenRecentRequest) -> dict[str, Any]:
    try:
        store = _recent_project_store_factory()
        directory = _recent_directory(store, request.project_id)
        _refuse_incomplete(directory)
        root, modules = _module_root_or_state()
        opened = store.open_recent(request.project_id)
        modules = _module_open(root, directory, opened["project_id"]) or modules
        shoot = store.project_store.read_shoot_state(directory, project_id=request.project_id)
        result: dict[str, Any] = {
            "project": {
                "project_id": opened["project_id"],
                "name": opened["name"],
                "revision": opened["revision"],
            },
            "shoot": shoot,
        }
        if "warning" in opened:
            result["warning"] = opened["warning"]
        return _with_modules(_public_project_payload(result), modules)
    except studio_core.ProjectError as exc:
        return _project_error_response(exc)
    except _ImageFilmCompositionError as exc:
        return _image_film_error_response(exc)


def _rebase_project_footage(project_id: str, old: str, new: str) -> None:
    """lane w11-backend M-3: the private footage/sound index of a moved or copied project follows it.

    Only entries under the old project folder move; a file outside it keeps its path (offline there)."""
    try:
        new_dir = str(_project_store_factory().boundary.validate_project_directory(new, operation="open"))
        index = _timeline_footage_index(project_id)
        with index._lock:
            files = index._read()
            changed = False
            for entry in files.values():
                if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                    moved = studio_core.project_store._rebase_path(entry["path"], old, new_dir)
                    if moved != entry["path"]:
                        entry["path"] = moved
                        changed = True
            if changed:
                index._write(files)
    except Exception:  # noqa: BLE001 - an unmoved index only leaves sources offline, as before
        pass
