from __future__ import annotations


def _v4_destination(request: SubmitRequest) -> tuple[Path, str, str | None]:
    """Validate and normalize the sole V4 destination form before admission."""
    save_to = "" if request.save_to is None else request.save_to
    basename = "" if request.requested_basename is None else request.requested_basename
    project_id = request.project_id
    project_ref = request.project_ref
    if project_id is not None:
        if project_ref is None or project_ref.project_id != project_id:
            raise ValueError("project scope is invalid")
        if save_to == "" and project_ref is not None:
            # lane p5c-frm (Project charter decision 2): no save_to = the project's output folder (made on first
            # write), named by the project rule unless a name was typed. The folder is an approved root of the project.
            project_directory = _project_reference_directory(project_ref)
            manifest = studio_core.read_project(project_directory, expected_project_id=project_id,
                                                boundary=studio_core.PathBoundary(hermes_home=_active_hermes_home()))
            folder = _project_output_dir(project_directory, project_id, create=True)
            if basename and (not basename.lower().endswith(".mp4") or "/" in basename or "\\" in basename):
                raise ValueError("requested basename is invalid")  # owner after p6: a typed name stays an MP4 name (no silent .mp4 append)
            name = _project_result_name(project_directory, folder, manifest.get("name"), "video", ".mp4", typed=basename or None)  # p6-name
            if "/" in name or "\\" in name or not name.lower().endswith(".mp4") or len(name) > 255:
                raise ValueError("requested basename is invalid")
            return Path(folder).absolute(), name, project_id
        if not isinstance(save_to, str) or not save_to or not isinstance(basename, str) or not basename:
            raise ValueError("project output destination is invalid")
    elif project_ref is not None:
        raise ValueError("project scope is invalid")

    if save_to == "" and basename == "":
        if project_id is not None:
            raise ValueError("project output destination is invalid")
        downloads = Path.home() / "Downloads"
        directory = downloads if downloads.is_dir() else Path.cwd()
        # No mkdir is permitted for the user-facing default.  PathBoundary
        # rejects HERMES_HOME, reparse points, and non-regular directories.
        studio_core.PathBoundary(hermes_home=_active_hermes_home()).validate_project_directory(
            directory, operation="open"
        )
        return directory, "video.mp4", None
    if not isinstance(save_to, str) or not isinstance(basename, str) or not save_to or not basename:
        raise ValueError("output directory and basename must be supplied together")
    if (
        basename in {".", ".."}
        or "/" in basename
        or "\\" in basename
        or "\x00" in basename
        or not basename.lower().endswith(".mp4")
        or len(basename) > 255
    ):
        raise ValueError("requested basename is invalid")

    boundary = studio_core.PathBoundary(hermes_home=_active_hermes_home())
    if project_ref is not None:
        # Lane p13-prjc finding 1: a save_to at the old place of a pending re-base is refused by name before any
        # list check, so the sent list (the state list, [] or any other) never decides it.
        try:
            _rebase_pending_refusal(project_ref, save_to)
        except _RebasePending as exc:
            raise studio_core.ProjectError("project_rebase_pending", str(exc)) from None
        project_directory = _project_reference_directory(project_ref)
        boundary.validate_project_directory(project_directory, operation="open")
        # Task 10 / §4.1: a project submit must be authorized against the
        # authoritative manifest, not just a syntactically valid directory.
        # Freshly read the manifest (this revalidates project identity and the
        # declared roots) and require the request's approved_roots to be exactly
        # the manifest's approved_roots before any output/media path is admitted.
        manifest = studio_core.read_project(
            project_directory,
            expected_project_id=project_id,
            boundary=boundary,
        )
        manifest_roots = manifest.get("approved_roots") or []
        # Owner after p9-vid: _request_roots adds the project's own output/ and media/ folders to the request; the
        # manifest side carries the same project folders (_manifest_roots_of), so only a foreign root diverges.
        manifest_roots = _with_output_root(list(manifest_roots), _project_output_existing(project_id, project_directory),
                                           _project_media_existing(project_id, project_directory))
        # Lane p13-prj item 1 (PRJ-018): since p12-med the request's list is narrowed inside the server's list, so a
        # Hermes request naming only the folder it writes to (or none) never EQUALS the manifest's list; equality
        # refused every explicit Hermes save_to as folders_changed. Charter decision 4: the server's list is the
        # authority and a sent list only narrows, so the narrowed roots must each be a manifest folder (subset), and
        # folders_changed names the one case left: a sent list of which the project holds nothing any more (stale).
        # An unreachable approved folder (unplugged drive) is not a destination and does not block the comparison.
        manifest_ids = _canonical_root_identities(boundary, [r for r in manifest_roots if isinstance(r, str) and os.path.isdir(r)])
        if not _canonical_root_identities(boundary, _request_roots(request.approved_roots, request.project_ref)) <= manifest_ids:
            raise ValueError("approved roots do not match the project manifest")
        # Lane p13-prjc finding 2: the stale-list check reads the sent list after this project's offline folders are
        # dropped; nothing left = an empty sent list. A foreign folder (existing or missing) still decides as before.
        sent = [r for r in _sent_without_offline(request.approved_roots, request.project_ref) if isinstance(r, str) and r]
        if sent and not any(_sent_root_identity(boundary, r) in manifest_ids for r in sent):
            raise ValueError("approved roots do not match the project manifest")
    if not _request_roots(request.approved_roots, request.project_ref):
        raise ValueError("an explicit output requires an approved root")
    roots = _request_roots(request.approved_roots, request.project_ref)
    # Lane p13-prjb finding 3 (cold review p13-prj Important 3): the folder boundary runs FIRST, so no filesystem call
    # ever sees a save_to outside the approved folders (a network path is refused by its spelling, never opened).
    # save_to_not_folder is then a check by name on a path the boundary has already placed inside the folders.
    # A network spelling in either slash form (//host/share as well as \\host\share) is refused by its text alone,
    # with the boundary's own refusal: the boundary's spelling rule names only the backslash form.
    if save_to.replace("/", "\\").startswith("\\\\"):
        raise studio_core.ProjectError("invalid_project", "path must be an absolute local path")
    try:
        boundary.validate_confined_path(save_to, roots, directory=True)
    except studio_core.ProjectError as exc:
        if str(exc) != "path type is not allowed":
            if Path(save_to).suffix.lower() in _SAVE_TO_FILE_SUFFIXES:
                try:  # a take's own (not yet written) path: its folder must be an approved one, then name it
                    boundary.validate_confined_path(str(Path(save_to).parent), roots, directory=True)
                except studio_core.ProjectError as parent_exc:
                    raise parent_exc from None
                raise ValueError("save_to is not a folder") from None  # never "Unknown generation error"
            raise
        try:  # an existing FILE inside the approved folders: name it; outside them the boundary refusal stands
            boundary.validate_confined_path(save_to, roots, directory=False)
        except studio_core.ProjectError:
            raise exc from None
        raise ValueError("save_to is not a folder") from None
    return Path(save_to).absolute(), basename, project_id


# Lane p13-prj item 1: a save_to whose leaf is a media file name is a file path, not a folder.
_SAVE_TO_FILE_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".png", ".jpg", ".jpeg", ".webp", ".wav", ".mp3", ".flac"})


def _sent_root_identity(boundary: Any, root: str) -> Any:
    """The canonical identity of one root a request sent, or None when it is no reachable folder (stale)."""
    try:
        return next(iter(_canonical_root_identities(boundary, [root])))
    except Exception:  # noqa: BLE001 - a stale or invalid sent root holds nothing
        return None


def _canonical_root_identities(boundary: Any, roots: Any) -> frozenset[tuple[int, int, str]]:
    """Canonical identity set for approved roots, order-independent.

    Each root is validated as an existing project-openable directory and pinned
    to its filesystem identity (volume serial + file id + canonical path) so the
    comparison is by real directory identity, matching how the store normalizes
    and dedupes roots. Any invalid or non-directory root is rejected.
    """
    if not isinstance(roots, (list, tuple)) or len(roots) > 64:
        raise ValueError("approved roots are invalid")
    identities: set[tuple[int, int, str]] = set()
    for root in roots:
        if not isinstance(root, str) or not root or len(root) > 32767:
            raise ValueError("approved roots are invalid")
        root_path = boundary.validate_project_directory(root, operation="open")
        identity = boundary.pin_directory(root_path)
        identities.add((identity.volume_serial, identity.file_id, identity.canonical_path))
    return frozenset(identities)


def _v4_path_boundary() -> Any:
    return studio_core.PathBoundary(hermes_home=_active_hermes_home())


def _v4_local_path_value(value: str) -> str:
    """Translate a file URI before the approved-root boundary sees it."""
    if not value.lower().startswith("file://"):
        return value
    parsed = urlparse(value)
    path = unquote(parsed.path)
    if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
        path = "//" + parsed.netloc + path
    if os.name == "nt" and path.startswith("/") and len(path) >= 3 and path[2] == ":":
        path = path[1:]
    return path


def _v4_validate_media(request: SubmitRequest) -> dict[str, Any]:
    """Validate V4 local inputs and return canonical values for identity only."""
    if (request.mode or (request.target_id or "").replace("-", "_")) == "text_to_video":
        return _request_identity_module.without_media(request.args)
    identity_args = dict(request.args)
    roots = tuple(_request_roots(request.approved_roots, request.project_ref))
    boundary = _v4_path_boundary()
    identity_key: bytes | None = None
    for field in ("input_image", "input_images", "input_video"):
        value = request.args.get(field)
        if field == "input_images" and isinstance(value, list):
            values = list(value)
        else:
            values = [value]
        projected = list(values)
        for index, item in enumerate(values):
            if not isinstance(item, str) or not item.strip():
                continue
            if not _request_identity_module.looks_like_local_path(item):
                continue
            if not roots:
                raise ValueError("local media requires an approved root")
            local_value = _v4_local_path_value(item)
            identity = boundary.validate_input(local_value, roots)
            if identity_key is None:
                identity_key = boundary.profile_identity_key()
            projected[index] = _request_identity_module.path_identity(
                identity.canonical_path, identity_key
            )
        if field == "input_images" and isinstance(value, list):
            identity_args[field] = projected
        elif values and projected[0] is not value:
            identity_args[field] = projected[0]
    return identity_args


def _v4_prepare_managed_media(
    request: SubmitRequest,
) -> tuple[dict[str, Any], dict[str, Any], Any | None, list[tuple[str, int, Any]]]:
    """Pin each new local Managed input and retain the opened leases until submit."""
    mode = request.mode or (request.target_id or "").replace("-", "_")
    if mode == "text_to_video":
        args = _request_identity_module.without_media(request.args)
        return dict(args), dict(args), None, []

    boundary = _v4_path_boundary()
    roots = tuple(_request_roots(request.approved_roots, request.project_ref))
    identity_args = _v4_validate_media(request)
    operational_args = dict(request.args)
    pinned: list[tuple[str, int, Any]] = []
    try:
        for field in ("input_image", "input_images", "input_video"):
            value = request.args.get(field)
            is_list = field == "input_images" and isinstance(value, list)
            values = list(value) if is_list else [value]
            for index, item in enumerate(values):
                if not isinstance(item, str) or not item.strip():
                    continue
                if not _request_identity_module.looks_like_local_path(item):
                    continue
                if not roots:
                    raise ValueError("local media requires an approved root")
                local_value = _v4_local_path_value(item)
                lease = boundary.pin_local_media(
                    local_value,
                    approved_roots=roots,
                    field=field,
                    index=index,
                )
                if isinstance(lease, tuple):
                    raise ValueError("managed media pin returned multiple leases")
                canonical_identity = getattr(lease, "canonical_identity", None)
                if (
                    not isinstance(canonical_identity, str)
                    or re.fullmatch(r"[0-9a-f]{64}", canonical_identity) is None
                ):
                    raise ValueError("managed media pin has no canonical identity")
                pinned.append((field, index, lease))
        return identity_args, operational_args, boundary, pinned
    except BaseException:
        for _field, _index, lease in pinned:
            try:
                boundary.abort_pinned_media(lease)
            except BaseException:
                pass
        raise


async def _v4_upload_pinned_managed_media(
    request: SubmitRequest,
    args: dict[str, Any],
    boundary: Any | None,
    pinned: list[tuple[str, int, Any]],
) -> dict[str, Any]:
    """Consume and upload one pinned medium at a time, replacing only its value."""
    prepared_args = dict(args)
    while pinned:
        field, index, lease = pinned.pop(0)
        max_bytes = 50 * 1024 * 1024 if field == "input_video" else 10 * 1024 * 1024
        try:
            data = boundary.consume_pinned_media(lease, max_bytes=max_bytes)
        except BaseException:
            try:
                boundary.abort_pinned_media(lease)
            except BaseException:
                pass
            raise
        try:
            reference = await flux3_provider.upload_managed_media(
                data=data,
                permitted=("video",) if field == "input_video" else ("image",),
                safe_suffix=getattr(lease, "safe_suffix", ""),
                client_request_id=request.client_request_id,
            )
        finally:
            del data
        if (
            not isinstance(reference, str)
            or not reference.startswith("nous-upload:")
            or len(reference) <= len("nous-upload:")
            or "\x00" in reference
            or "/" in reference
            or "\\" in reference
            or any(character.isspace() for character in reference)
        ):
            raise ValueError("managed media upload returned an invalid reference")
        if field == "input_images":
            values = list(prepared_args[field])
            values[index] = reference
            prepared_args[field] = values
        else:
            prepared_args[field] = reference
    return prepared_args


# --- Pinned H3 conditioning targets (minimax-h3-frames-v1 / -references-v1) ---
def _h3_is_conditioning(request: SubmitRequest) -> bool:
    return request.provider_id == "comfyui-local" and request.target_id in _h3c_module.TARGET_IDS


def _h3_prepare_request(request: SubmitRequest) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Validate args, confine every medium and fix its content hash; no side effect."""
    plan = _h3c_module.validate_args(request.target_id, request.args)
    resolved = _h3m_module.resolve_media(
        _h3c_module.media_items(plan),
        roots=tuple(_request_roots(request.approved_roots, request.project_ref)),
        boundary=_v4_path_boundary(),
        local_path=_v4_local_path_value,
        path_identity=_request_identity_module.path_identity,
        staged_root=prepared_cache_dir() / "opening-frames",
    )
    return plan, _h3_identity_with_camera(_h3m_module.identity_args(request.args, resolved), plan), resolved


def _h3_identity_with_camera(identity: dict[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
    """With ``camera`` the durable identity holds the normalized camera and the compiled prompt (lane w1-h3).

    The same camera (defaults filled in) compiles to the same prompt and so to the same
    request; a request without ``camera`` keeps exactly its earlier identity.
    """
    if not plan.get("camera"):
        return identity
    return {**identity, "camera": dict(plan["camera"]), "prompt_compiled": _h3c_module.camera_prompt(plan)}


def _v4_identity_args(request: SubmitRequest) -> dict[str, Any]:
    if _h3_is_conditioning(request):
        return _h3_prepare_request(request)[1]
    if _h3x_is_continuation(request):
        return _h3x_prepare_request(request)[1]
    if _h3f_is_finish(request):
        return _h3f_prepare_request(request)[1]
    if _vsr_is_upscale(request):
        return _vsr_prepare_request(request)["identity"]
    return _v4_validate_media(request)


async def _h3_prepare_fresh(request: SubmitRequest) -> dict[str, Any]:
    """Validate and pre-fit before the ledger: a bad medium never creates a row."""
    plan, identity, resolved = _h3_prepare_request(request)
    await _h3k_admit(plan)  # lane w2-h3ctl: controls the live engine cannot run never reach the ledger
    prefit = await asyncio.to_thread(
        _h3m_module.prefit_media,
        resolved,
        width=plan["width"],
        height=plan["height"],
        frames=plan["effective_frames"],
        ffmpeg=FFMPEG_PATH,
        scratch=Path(tempfile.gettempdir()),
    )
    return {"plan": plan, "identity": identity, "prefit": prefit}


async def _h3_upload_and_compile(manifest: Any, graph: Any, request: SubmitRequest, prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Upload the pre-fitted media to ComfyUI temp/ (right before submit), then compile."""
    upload = _h3_media_upload or _h3m_module.upload_temp
    media = await asyncio.to_thread(_h3m_module.upload_prefit, prepared["prefit"], upload)
    return _h3c_module.compile_h3_conditioning_request(graph=graph, manifest=manifest, args=request.args, media=media)


def _h3_facts_root() -> Path:
    return Path(_comfy_store().path).parent / "h3-take-facts"


def _h3_public_facts(studio_job_id: str) -> dict[str, Any] | None:
    try:
        return _h3m_module.public_take_facts(_h3m_module.read_take_facts(_h3_facts_root(), studio_job_id))
    except (OSError, AttributeError, TypeError, ValueError):
        return None


def _h3_is_take(row: Any) -> bool:
    return getattr(row, "provider_id", None) == "comfyui-local" and str(getattr(row, "target_id", "")).startswith("minimax-h3-")


def _h3_runtime_records() -> list[dict[str, Any]]:
    """This install's runtimes a published H3 take passed on (``runtime_policy``); none when unreadable."""
    try:
        return _comfy_provider_module.runtime_policy.read_tested(Path(_comfy_store().path).parent / "h3-runtime-tested.json")
    except Exception:  # noqa: BLE001 - an unreadable record only means "untested"
        return []


def _h3_note_runtime(row: Any, health: Any) -> None:
    """Write the runtime a fresh H3 take is submitted on into its take facts (write-once)."""
    if not _h3_is_take(row) or getattr(health, "runtime", None) is None:
        return
    try:
        _h3m_module.merge_take_facts(_h3_facts_root(), row.studio_job_id, {
            "runtime": dict(health.runtime), "runtime_tested": bool(health.runtime_tested)})
    except OSError:
        pass


async def _h3_verify_runtime(row: Any, operation: Any, root: Path) -> None:
    """First take on an untested runtime: check the published file, then record the runtime as tested.

    Checks frame count, fps, audio stream and a decodable poster frame against the
    manifest (the DownloadDigest was verified by the delivery).  Runs once per take.
    """
    facts = _h3m_module.read_take_facts(root, row.studio_job_id) or {}
    if facts.get("runtime_tested") is not False or "runtime_check" in facts:
        return
    policy = _comfy_provider_module.runtime_policy
    try:
        profile, _graph = _comfy_assets(row.target_id)
        manifest = profile.manifest if hasattr(profile, "manifest") else profile
        frames = facts.get("effective_frames")
        if type(frames) is not int:
            frames = policy.expected_frames(manifest, int(row.request["args"]["duration_seconds"]))
        if isinstance(facts.get("finish"), Mapping):  # lane w1-h3: a finished take plays at its own fps (48 with RIFE)
            duration = {**manifest["capabilities"]["duration"], "fps": facts["finish"]["fps"]}
            manifest = {**manifest, "capabilities": {**manifest["capabilities"], "duration": duration}}
        path = _private_ops().saved_path_for_receipt(operation)
        problem = await asyncio.to_thread(policy.verify_take_output, path, manifest=manifest, frames=frames, ffmpeg=FFMPEG_PATH)
        if problem is None:
            policy.record_tested(Path(_comfy_store().path).parent / "h3-runtime-tested.json", target_id=row.target_id,
                                 versions=facts["runtime"], studio_job_id=row.studio_job_id)
    except Exception:  # noqa: BLE001 - an unverifiable take leaves the runtime untested
        problem = "unverified"
    try:
        _h3m_module.merge_take_facts(root, row.studio_job_id, {"runtime_check": problem or "passed"})
    except OSError:
        pass


async def _h3_measure_take(row: Any, operation: Any) -> None:
    """motion_activity once per published H3 take (v1, v2, frames, references); first-take runtime check."""
    if not _h3_is_take(row) or getattr(row, "state", None) != "ready" or operation is None:
        return
    try:
        await _h3_verify_runtime(row, operation, _h3_facts_root())
    except (OSError, AttributeError, TypeError, ValueError):
        pass
    try:
        root = _h3_facts_root()
        known = _h3m_module.read_take_facts(root, row.studio_job_id) or {}
        if "motion_state" in known and "freeze_check" in known:
            return
    except (OSError, AttributeError, TypeError, ValueError):
        return
    try:
        path = _private_ops().saved_path_for_receipt(operation)
        value = known.get("motion_activity") if "motion_state" in known else None
        if type(value) not in (int, float):
            value = await asyncio.to_thread(_h3m_module.motion_activity, path, FFMPEG_PATH)
        update = {**_h3m_module.motion_facts(value), "motion_state": "measured"}
        update.update(_h3x_relative_update({**known, **update}))
        # Freeze check (lane w1-h3): act + hold_end, stored once with the take; a frozen take is marked, never hidden.
        local = await asyncio.to_thread(_h3m_module.local_motion_activity, path, FFMPEG_PATH)
        update["freeze_check"] = _h3m_module.freeze_check(value, await asyncio.to_thread(_h3m_module.hold_end, path, FFMPEG_PATH), local)
        update["frozen"] = update["freeze_check"]["frozen"]
    except Exception:  # noqa: BLE001 - an unreadable take is reported as unmeasured
        update = {"motion_state": "unmeasured", "freeze_check": {"schema": _h3m_module.FREEZE_CHECK_SCHEMA, "verdict": "unmeasured"}}
    try:
        _h3m_module.merge_take_facts(root, row.studio_job_id, update)
    except OSError:
        pass


# --- H3 take sidecars and continuation targets (lane fs-obvb) ---
def _h3x_is_continuation(request: SubmitRequest) -> bool:
    return request.provider_id == "comfyui-local" and request.target_id in _h3x_module.TARGET_IDS


def _h3x_prepare_request(request: SubmitRequest) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Validate args, confine each source take, fix its content hash; no side effect."""
    plan = _h3x_module.validate_args(request.target_id, request.args)
    resolved = _h3m_module.resolve_media(
        _h3x_module.media_items(plan),
        roots=tuple(_request_roots(request.approved_roots, request.project_ref)),
        boundary=_v4_path_boundary(),
        local_path=_v4_local_path_value,
        path_identity=_request_identity_module.path_identity,
        staged_root=prepared_cache_dir() / "opening-frames",
    )
    return plan, _h3x_module.identity_args(request.args, resolved), resolved


async def _h3x_prepare_fresh(request: SubmitRequest) -> dict[str, Any]:
    """Every source must be a verified take pair, before the ledger (never fake a continuation)."""
    plan, identity, resolved = _h3x_prepare_request(request)
    facts = await asyncio.to_thread(_h3x_module.check_pairs, plan, resolved)
    source_motion = None
    try:
        values = [await asyncio.to_thread(_h3m_module.motion_activity, item["path"], FFMPEG_PATH) for item in resolved]
        source_motion = round(sum(values) / len(values), 2)
    except Exception:  # noqa: BLE001 - relative motion is then simply not reported
        source_motion = None
    return {"plan": plan, "identity": identity, "resolved": resolved, "facts": facts, "source_motion": source_motion}


async def _h3x_stage_and_compile(manifest: Any, graph: Any, studio_job_id: str, prepared: Mapping[str, Any]) -> dict[str, Any]:
    """Stage the source pairs into ComfyUI/output/studio-h3/staged (right before submit), then compile."""
    upload = _h3x_upload or _h3x_module.upload_output
    pairs = [(fact, item["path"]) for fact, item in zip(prepared["facts"], prepared["resolved"])]
    names = _h3x_module.staged_names(studio_job_id, len(pairs))
    staged: list[str] = []

    def stage() -> list[str]:
        for pair, name in zip(pairs, names):
            staged.extend(_h3x_module.stage_pairs([pair], [name], upload))
        return staged

    try:
        await asyncio.to_thread(stage)
        compiled = _h3x_module.compile_request(graph=graph, manifest=manifest, plan=prepared["plan"],
                                               facts=prepared["facts"], staged=staged)
    except BaseException:
        await _h3x_release_paths(staged)
        raise
    compiled["staged"] = list(staged)
    return compiled


async def _h3x_release_paths(paths: list[str]) -> list[str]:
    if not paths:
        return []
    release = _h3x_release or _h3x_module.release_staged
    try:
        return await asyncio.to_thread(release, list(paths))
    except Exception:  # noqa: BLE001 - a later poll retries
        return []


async def _h3x_release_after(result: Any) -> None:
    """Remove a finished continuation job's staged copies once (ready, failed or cancelled)."""
    details = result.get("details") if isinstance(result, Mapping) else None
    if not isinstance(details, Mapping) or (details.get("target_id") not in _h3x_module.TARGET_IDS
                                             and details.get("target_id") != _h3f_module.TARGET_ID):
        return
    if details.get("status") not in ("ready", "failed", "cancelled", "moderated", "succeeded", "completed"):
        return
    studio_job_id = details.get("id")
    if not isinstance(studio_job_id, str):
        return
    try:
        root = _h3_facts_root()
        facts = _h3m_module.read_take_facts(root, studio_job_id) or {}
        staged = facts.get("staged")
        if not staged or "staged_released" in facts:
            return
        await _h3x_release_paths(list(staged))
        _h3m_module.merge_take_facts(root, studio_job_id, {"staged_released": True})
    except (OSError, AttributeError, TypeError, ValueError):
        return


async def _h3_collect_sidecar(row: Any, operation: Any, output_identity: Any) -> None:
    """Store the take's .mctx sidecar beside the published mp4 (byte-exact pair), then its lineage facts.

    The mp4 is never re-encoded: ``self_id`` in the sidecar header must equal
    the SHA-256 of the published file, or the sidecar is not kept.
    """
    if not _h3_is_take(row) or not isinstance(output_identity, Mapping):
        return
    filename = output_identity.get("filename")
    if not isinstance(filename, str) or not filename.lower().endswith(".mp4"):
        return
    root = _h3_facts_root()
    try:
        if "mctx_state" in (_h3m_module.read_take_facts(root, row.studio_job_id) or {}):
            return
    except (OSError, AttributeError, TypeError, ValueError):
        return
    update: dict[str, Any]
    try:
        published = Path(_private_ops().saved_path_for_receipt(operation))
        data = await _comfy_client_module.ComfyUIClient().view({
            "filename": filename[:-4] + _h3_mctx_module.SIDECAR_SUFFIX,
            "subfolder": str(output_identity.get("subfolder") or ""),
            "type": "output",
        })
        facts = _h3_mctx_module.take_facts(_h3_mctx_module.parse_header(data))
        if facts["self_id"] != getattr(row, "output_sha256", None) and facts["self_id"] != _h3_mctx_module.sha256_file(published):
            raise _h3_mctx_module.MctxError("the sidecar does not pair with the published take")
        target = _h3_mctx_module.sidecar_path(published)
        temporary = target.with_name(target.name + ".part")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        update = {"mctx_state": "paired", "mctx": _h3_mctx_module.public_mctx(facts)}
    except Exception:  # noqa: BLE001 - the take stays a plain take; continuation then offers nothing
        update = {"mctx_state": "missing"}
    try:
        _h3m_module.merge_take_facts(root, row.studio_job_id, update)
    except OSError:
        pass


def _h3x_relative_update(facts: Mapping[str, Any]) -> dict[str, Any]:
    """"Barely moves" relative to the source take(s) (fs-obvpm: the absolute flag alone misreads dark scenes)."""
    if "motion_relative" in facts or "continuation" not in facts:
        return {}
    relative = _h3_mctx_module.relative_motion(facts.get("motion_activity"), facts.get("source_motion_activity"))
    return relative or {}
