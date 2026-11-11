from __future__ import annotations

import contextlib
import secrets


# --- Shot chain studio.shot-chain/1 (lane w9-chain): guide -> marks -> pictures -> shot -----------------------------
# One document per shot in the project's private state (state/chains/<chain_id>.json through ProjectStore's chain
# methods; revisions kept like the timeline; expected_revision on every write; request_id replay like
# /projects/timeline/apply). The verbs live in dashboard/chain/ops.py (pure); this unit gives them their effects:
# set_guide probes and hashes the clip, add_mark / move_mark pull the FULL-size guide frame and make it an owned
# Image picture through the same import + Use in Shoot routes the page's "Frame -> Keyframe" uses, bind_picture
# takes a kept Image result. status (dashboard/chain/status.py) is computed on every answer, never stored.
# Errors are the timeline's body {code, message, retryable, next}; no answer of list carries a path.
_CHAIN_LABEL_MAX = 120
_CHAIN_DEFAULT_FPS = 24


class ChainListRequest(_ProjectRequest):
    project_ref: ProjectReference


class ChainReadRequest(_ProjectRequest):
    project_ref: ProjectReference
    chain_id: str = Field(pattern=r"^ch-[0-9a-f]{8}$")


class ChainApplyRequest(_ProjectRequest):
    project_ref: ProjectReference
    chain_id: str | None = Field(default=None, pattern=r"^ch-[0-9a-f]{8}$")
    expected_revision: int = Field(ge=0)
    ops: list[dict[str, Any]] = Field(min_length=1, max_length=200)
    by: Literal["artist", "hermes"] = "artist"
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    reason: str = Field(default="", max_length=200)


def _chain_doc() -> Any:
    return studio_core.ProjectStore._chain_module("document")


def _chain_error(exc: BaseException) -> dict[str, Any]:
    document = _chain_doc()
    code = getattr(exc, "code", None)
    if type(code) is not str or code not in document.ERRORS:
        code = "chain_unavailable"
    message = str(exc) if code != "chain_unavailable" else "The chain is unavailable."
    return {"error": document.error_body(code, message, getattr(exc, "op_index", None))}


def _chain_project(project_ref: ProjectReference) -> tuple[str, str, dict[str, Any]]:
    directory = _project_reference_directory(project_ref)
    manifest = _project_store_factory().read_project(directory, expected_project_id=project_ref.project_id)
    return directory, project_ref.project_id, manifest


def _chain_roots(directory: str, manifest: dict[str, Any]) -> list[str]:
    return [*manifest["approved_roots"], str(Path(os.path.realpath(directory)))]


def _chain_stored_guide(guide: dict[str, Any], manifest: dict[str, Any]) -> Path:
    """Lane w34-sec (scan finding 1): a guide read back from state/chains/<id>.json is project data, never trusted.
    It is confined again to the project's approved folders (as set_guide's probe_guide did) before any open: a network
    or device path is refused on its text, anything outside the folders is unreadable."""
    path = guide.get("path") if isinstance(guide, dict) else None
    try:
        if not isinstance(path, str) or _local_path_module.remote_path_reason(path) is not None:
            raise ValueError("not a local path")
        return resolve_approved_path(path, list(manifest["approved_roots"]))
    except (OSError, ValueError):
        _chain_doc().fail("guide_unreadable", "the guide is missing or outside the project's approved folders")


def _chain_step_manifest(target_id: str | None) -> dict[str, Any] | None:
    if target_id is None:
        return None
    steps = _video_steps_module()
    for step in steps.manifests(_video_steps_source(), _generation_registry.catalog(), None):
        if step["id"] == target_id:
            return step
    return None


def _chain_fps(chain: dict[str, Any]) -> int:
    step = _chain_step_manifest((chain.get("step") or {}).get("target_id"))
    return int(step["frame_grid"]["fps"]) if step else _CHAIN_DEFAULT_FPS


def _chain_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _chain_json(response: Any) -> dict[str, Any]:
    """A route answer as a dict (route functions answer a dict or a JSONResponse on refusal)."""
    if isinstance(response, JSONResponse):
        return json.loads(bytes(response.body).decode("utf-8"))
    return response


def _chain_env(project_id: str, directory: str, manifest: dict[str, Any]) -> dict[str, Any]:
    document = _chain_doc()
    scope = f"film_owner:{project_id}"
    allowed = list(manifest["approved_roots"])  # a guide must be usable by the submit, whose roots are exactly these

    def probe_guide(path: str, approved_roots: list[str]) -> dict[str, Any]:
        try:
            file = _confined_local_path(path, approved_roots)
            resolve_approved_path(str(file), allowed)
        except (RecipeError, OSError, ValueError):
            document.fail("guide_unreadable", "the guide is missing or outside the project's approved folders")
        try:
            facts = _timeline_module("resolver").ffprobe_facts(file, ffprobe=FFPROBE_PATH)
        except (OSError, ValueError, subprocess.SubprocessError):
            document.fail("guide_unreadable", f"{file.name} cannot be read as a video")
        sha = _chain_sha256(file)
        rate = facts["rate"]
        return {"path": str(file), "media_id": "file:" + sha, "sha256": sha, "fps": round(rate["num"] / rate["den"], 6),
                "frames": int(facts["frames"]), "width": facts["width"], "height": facts["height"], "label": file.stem[:_CHAIN_LABEL_MAX] or "Guide"}

    def stage(job_id: str) -> dict[str, Any]:
        staged = _chain_json(asyncio.run(image_producer_stage_opening_frame(ImageProducerStageOpeningFrameRequest.model_validate({
            "schema": "studio.image-producer-operation/1", "kind": "image_producer_stage_opening_frame",
            "owner_scope": scope, "image_producer_job_id": job_id}))))
        if not isinstance(staged.get("path"), str):
            raise document.ChainError("still_failed", "the picture could not be prepared for the shot")
        # The staged picture itself (prepared/opening-frames): the H3 submit accepts exactly that backend folder, the
        # same path the keyframe track uses; nothing is copied into the artist's folders.
        return {"image_job_id": job_id, "path": staged["path"], "sha256": staged["sha256"]}

    def pull_still(guide: dict[str, Any], frame: int) -> dict[str, Any]:
        """The guide frame at full size (an exact frame by index, not a time seek) as an owned Image picture."""
        stored = _chain_stored_guide(guide, manifest)  # lane w34-sec (finding 1): confined again before the open
        if _chain_sha256(stored) != guide["sha256"]:
            document.fail("still_failed", "the guide file changed since it was set")
        fd, name = tempfile.mkstemp(prefix="chain-still-", suffix=".png")
        os.close(fd)
        try:
            run = subprocess.run([str(FFMPEG_PATH), "-hide_banner", "-loglevel", "error", "-y", "-i", str(stored),
                                  "-vf", f"select=eq(n\\,{int(frame)})", "-vsync", "0", "-frames:v", "1", name],
                                 capture_output=True, timeout=300, check=False, env=_child_env())
            data = Path(name).read_bytes() if run.returncode == 0 else b""
        finally:
            with contextlib.suppress(OSError):
                os.unlink(name)
        if not data:
            document.fail("still_failed", f"frame {frame} of the guide could not be read")
        imported = _chain_json(asyncio.run(image_producer_import(ImageImportRequest.model_validate({
            "schema": "studio.image-import-request/1", "owner_scope": scope, "operation_id": "imgop_" + secrets.token_hex(16),
            "data_b64": base64.b64encode(data).decode("ascii"), "file_name": f"{guide['label']} frame {frame}.png"}))))
        job = imported.get("image_producer_job_id")
        if not isinstance(job, str):
            document.fail("still_failed", ((imported.get("error") or {}).get("message")) or "the still could not be imported")
        return stage(job)

    def kept_picture(job_id: str) -> dict[str, Any]:
        with _IMAGE_FILM_ROOT_LOCK:
            root = _image_producer_root(scope)
            handoff = _image_operations_handoff(root, scope)
            states = handoff.image_states()
        if job_id not in _kept_image_index(states):
            document.fail("picture_not_kept", f"{job_id} is not a kept picture of this project")
        return stage(job_id)

    def check_step(target_id: str, args: dict[str, Any]) -> dict[str, Any]:
        if _chain_step_manifest(target_id) is None:
            document.fail("step_unknown", f"{target_id!r} is not a video step a chain can use")
        wanted = set(_h3c_module.SCALAR_KEYS)
        if set(args) != wanted:
            document.fail("invalid_step_args", "step args are exactly " + ", ".join(sorted(wanted)))
        try:
            _h3c_module.validate_args(target_id, args)
        except ValueError as exc:
            document.fail("invalid_step_args", str(exc))
        return dict(args)

    return {"probe_guide": probe_guide, "pull_still": pull_still, "kept_picture": kept_picture, "check_step": check_step}


def _chain_support_body(chain: dict[str, Any], manifest: dict[str, Any], directory: str) -> dict[str, Any] | None:
    """The §1b request the chain stands for, or None when it lacks a guide, marks or a step."""
    step, guide = chain.get("step"), chain.get("guide")
    if step is None or guide is None or not chain.get("marks"):
        return None
    fps = _chain_fps(chain)
    document = _chain_doc()
    step_manifest = _chain_step_manifest(step["target_id"])
    return {
        "target_id": step["target_id"],
        "duration_seconds": step["args"].get("duration_seconds"),
        "keyframes": [{"frame": document.shot_frame(guide, m["frame"], fps), "source": (m.get("picture") or {}).get("path")}
                      for m in sorted(chain["marks"], key=lambda m: m["frame"])],
        "motion_guide": {"source": guide["path"], "offset_frame": 0} if step_manifest and step_manifest["motion_guide"] else None,
        "approved_roots": list(manifest["approved_roots"]),
    }


def _chain_status(envelope: dict[str, Any], manifest: dict[str, Any], directory: str, *, check_support: bool = True) -> dict[str, Any]:
    status = studio_core.ProjectStore._chain_module("status")
    chain = envelope["chain"]
    reasons = status.base_reasons(chain)
    if check_support and not reasons:
        body = _chain_support_body(chain, manifest, directory)
        if body is not None:
            reasons.extend(_video_step_support(body)["reasons"])
    return status.compute(chain, envelope["bindings"], envelope["take_inputs"], _chain_fps(chain), reasons)


def _chain_envelope(store: Any, directory: str, project_id: str, chain_id: str) -> dict[str, Any]:
    envelope = store.read_chain(directory, project_id=project_id, chain_id=chain_id)
    if envelope is None:
        raise studio_core.ProjectError("chain_unknown", f"no chain {chain_id} in this project")
    return envelope


def _chain_list_sync(request: ChainListRequest) -> dict[str, Any]:
    directory, project_id, manifest = _chain_project(request.project_ref)
    rows = []
    for envelope in _project_store_factory().list_chains(directory, project_id=project_id):
        chain = envelope["chain"]
        status = _chain_status(envelope, manifest, directory, check_support=False)
        rows.append({"chain_id": chain["chain_id"], "shot_id": chain["shot_id"], "label": chain["label"],
                     "revision": chain["revision"], "marks": len(chain["marks"]), "takes": len(chain["takes"]),
                     "stale": bool(status["stale"]) or any(m["picture_stale"] for m in status["marks"])})
    return {"chains": rows}


def _chain_read_sync(request: ChainReadRequest) -> dict[str, Any]:
    directory, project_id, manifest = _chain_project(request.project_ref)
    envelope = _chain_envelope(_project_store_factory(), directory, project_id, request.chain_id)
    return {**envelope["chain"], "status": _chain_status(envelope, manifest, directory)}


def _chain_apply_sync(request: ChainApplyRequest) -> dict[str, Any]:
    document, ops_module = _chain_doc(), studio_core.ProjectStore._chain_module("ops")
    directory, project_id, manifest = _chain_project(request.project_ref)
    store = _project_store_factory()
    creating = request.chain_id is None
    digest = document.ops_sha256({"chain_id": request.chain_id, "ops": request.ops}) if request.request_id else None
    chain_id = request.chain_id
    if request.request_id is not None and chain_id is None:
        # A create retried with the same request_id names the same chain: its id is derived from the request id.
        chain_id = "ch-" + hashlib.sha256(f"{project_id}:{request.request_id}".encode()).hexdigest()[:8]
    if request.request_id is not None:
        kept = store.read_chain(directory, project_id=project_id, chain_id=chain_id, request_id=request.request_id)["request"]
        if kept is not None:
            if kept["ops_sha256"] != digest:
                raise studio_core.ProjectError("request_id_reused", "this request_id already applied other ops")
            return _chain_replay(store, directory, project_id, manifest, chain_id, kept)
    try:
        ops = ops_module.check_ops(request.ops, creating=creating)
        if creating:
            chain_id = chain_id or document.new_chain_id()
            if store.read_chain(directory, project_id=project_id, chain_id=chain_id) is not None:
                raise studio_core.ProjectError("chain_conflict", f"the chain {chain_id} exists already")
            envelope = {"chain": document.new_chain(chain_id, project_id, "Shot", None, "", _caller_by(request.by)), "bindings": {}, "take_inputs": {}}
        else:
            envelope = _chain_envelope(store, directory, project_id, chain_id)
        revision = envelope["chain"]["revision"]
        if request.expected_revision != revision:
            raise studio_core.ProjectError("chain_conflict", f"the chain is at revision {revision}")
        chain, bindings, _results = ops_module.apply(envelope["chain"], envelope["bindings"], ops, _chain_env(project_id, directory, manifest))
    except document.ChainError as exc:
        error = studio_core.ProjectError(exc.code, str(exc))
        error.op_index = exc.op_index
        raise error from exc
    # A kept request remembers the revision it made; its answer is rebuilt from that kept revision (same chain).
    remember = {"request_id": request.request_id, "ops_sha256": digest, "answer": {}} if request.request_id is not None else None
    written = store.write_chain(directory, project_id=project_id, chain_id=chain_id, expected_revision=revision, chain=chain,
                                bindings=bindings, take_inputs=envelope["take_inputs"], by=_caller_by(request.by), reason=request.reason,
                                request=remember)
    if remember is not None and "chain" not in written:  # the first try landed while this retry was on its way
        return _chain_replay(store, directory, project_id, manifest, chain_id, written["request"])
    answer = {"revision": written["chain"]["revision"], "chain": written["chain"], "status": _chain_status(written, manifest, directory)}
    return {**answer, "replayed": False} if remember is not None else answer


def _chain_replay(store: Any, directory: str, project_id: str, manifest: dict[str, Any], chain_id: str, kept: dict[str, Any]) -> dict[str, Any]:
    envelope = store.read_chain_revision(directory, project_id=project_id, chain_id=chain_id, revision=kept["revision"])
    return {"revision": kept["revision"], "chain": envelope["chain"], "status": _chain_status(envelope, manifest, directory), "replayed": True}


def _chain_record_take(store: Any, directory: str, project_id: str, chain_id: str, studio_job_id: str, *,
                       expected_revision: int | None = None, by: str, planned: dict[str, Any] | None = None) -> dict[str, Any]:
    """Append one take and remember what it was shot from (for status.why). ``planned``: the chain as it was
    planned and submitted (its inputs_sha256 is the take's); the take lands on the newest revision
    (``expected_revision`` None) so a write between submit and record never loses the take."""
    document = _chain_doc()
    envelope = _chain_envelope(store, directory, project_id, chain_id)
    chain = copy.deepcopy(envelope["chain"])
    if any(take["studio_job_id"] == studio_job_id for take in chain["takes"]):
        return envelope
    source = planned if planned is not None else chain
    fps = _chain_fps(source)
    chain["takes"].append({"studio_job_id": studio_job_id, "inputs_sha256": document.inputs_sha256(source, fps),
                           "made_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "camera": None})
    take_inputs = {**envelope["take_inputs"], studio_job_id: document.take_record(source, fps)}
    revision = chain["revision"] if expected_revision is None else expected_revision
    return store.write_chain(directory, project_id=project_id, chain_id=chain_id, expected_revision=revision, chain=chain,
                             bindings=envelope["bindings"], take_inputs=take_inputs, by=by, reason="take recorded")


@project_router.post("/projects/chain/list")
async def projects_chain_list(request: ChainListRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_chain_list_sync, request)
    except (studio_core.ProjectError, ValueError) as exc:
        return _chain_error(exc)


@project_router.post("/projects/chain/read")
async def projects_chain_read(request: ChainReadRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_chain_read_sync, request)
    except (studio_core.ProjectError, ValueError) as exc:
        return _chain_error(exc)


@project_router.post("/projects/chain/apply")
async def projects_chain_apply(request: ChainApplyRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_chain_apply_sync, request)
    except (studio_core.ProjectError, ValueError) as exc:
        return _chain_error(exc)


# --- plan + shoot (lane w9-chain item 3) -------------------------------------------------------------------------
# plan answers the exact body continuo_video_submit would send for the chain (keyframes in SHOT frames: the mark's
# guide frame through the guide fps -> shot fps; equal fps and offset 0 keep the number), the §1b support answer and
# the chain's inputs_sha256. shoot = plan + support (refusing with its reasons) + the same /submit path the page and
# continuo_video_submit use (explicitly authorized, local GPU, queued behind whatever runs; never cancels) + the take
# recorded on the chain, in one action. The take goes into the project's output folder as <chain_id>.mp4
# (the ledger picks a free name). /submit's identity rules decide repeats: the same client_request_id is never
# submitted twice, and a take already recorded for it is answered again.
class ChainPlanRequest(_ProjectRequest):
    project_ref: ProjectReference
    chain_id: str = Field(pattern=r"^ch-[0-9a-f]{8}$")


class ChainShootRequest(_ProjectRequest):
    project_ref: ProjectReference
    chain_id: str = Field(pattern=r"^ch-[0-9a-f]{8}$")
    expected_revision: int = Field(ge=1)
    client_request_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,128}$")


def _chain_take_name(project_ref: ProjectReference, manifest: dict[str, Any], client_request_id: str, reserve: bool) -> str:
    """lane p6-name (Project decision 10): the chain take's file by the project rule. Plan shows the next name and
    reserves nothing; Shoot reserves it; a replayed Shoot keeps the name its row already has."""
    existing = _read_client_row(client_request_id) if reserve else None
    if existing is not None and isinstance(getattr(existing, "requested_basename", None), str) and existing.requested_basename:
        return existing.requested_basename
    directory = _project_reference_directory(project_ref)
    folder = _project_output_dir(directory, project_ref.project_id, create=reserve)
    if reserve:
        return _project_result_name(directory, folder, manifest.get("name"), "video", ".mp4")
    return _naming.preview(directory, folder, manifest.get("name") or "Project", "video", suffix=".mp4")


def _chain_submit_args(chain: dict[str, Any], manifest: dict[str, Any], project_ref: ProjectReference, client_request_id: str,
                       reserve: bool = False) -> dict[str, Any]:
    step, guide = chain["step"], chain["guide"]
    fps = _chain_fps(chain)
    document = _chain_doc()
    args = dict(step["args"])
    args["keyframes"] = [{"source": m["picture"]["path"], "frame": document.shot_frame(guide, m["frame"], fps)}
                         for m in sorted(chain["marks"], key=lambda m: m["frame"])]
    step_manifest = _chain_step_manifest(step["target_id"])
    if step_manifest and step_manifest["motion_guide"]:
        args["control_video"] = guide["path"]
    roots = list(manifest["approved_roots"])
    return {
        "schema": "studio.generation-request/1",
        "provider_id": step_manifest["provider_id"] if step_manifest else None,
        "target_id": step["target_id"],
        "client_request_id": client_request_id,
        "args": args,
        # lane p5c-frm: the take goes into the project's output folder (Project charter decision 2).
        "save_to": str(_project_output_dir(_project_reference_directory(project_ref), project_ref.project_id, create=True)),
        "requested_basename": _chain_take_name(project_ref, manifest, client_request_id, reserve),
        "approved_roots": roots,
        "project_id": project_ref.project_id,
        "project_ref": {"project_id": project_ref.project_id},
    }


def _chain_plan_sync(project_ref: ProjectReference, chain_id: str, client_request_id: str | None = None) -> dict[str, Any]:
    directory, project_id, manifest = _chain_project(project_ref)
    envelope = _chain_envelope(_project_store_factory(), directory, project_id, chain_id)
    chain = envelope["chain"]
    status = studio_core.ProjectStore._chain_module("status")
    reasons = status.base_reasons(chain)
    body = _chain_support_body(chain, manifest, directory)
    support = _video_step_support(body) if body is not None and not reasons else {"supported": False, "reasons": reasons, "effective": None}
    if reasons and body is not None:
        support = {**support, "supported": False, "reasons": [*reasons, *support["reasons"]]}
    plan_id = client_request_id or f"chain-{chain_id}-r{chain['revision']}"
    submit_args = _chain_submit_args(chain, manifest, project_ref, plan_id, reserve=client_request_id is not None) if not reasons else None
    return {"submit_args": submit_args, "support": support, "inputs_sha256": _chain_doc().inputs_sha256(chain, _chain_fps(chain)),
            "_chain": chain, "_directory": directory, "_project_id": project_id}


def _chain_public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: plan[key] for key in ("submit_args", "support", "inputs_sha256")}


async def _chain_submit(body: dict[str, Any]) -> dict[str, Any]:
    """The /submit route itself (the page's and continuo_video_submit's path), in-process."""
    return await submit(SubmitRequest.model_validate(body))


@project_router.post("/projects/chain/plan")
async def projects_chain_plan(request: ChainPlanRequest) -> dict[str, Any]:
    try:
        return _chain_public_plan(await asyncio.to_thread(_chain_plan_sync, request.project_ref, request.chain_id))
    except (studio_core.ProjectError, ValueError) as exc:
        return _chain_error(exc)


@project_router.post("/projects/chain/shoot")
async def projects_chain_shoot(request: ChainShootRequest) -> dict[str, Any]:
    try:
        plan = await asyncio.to_thread(_chain_plan_sync, request.project_ref, request.chain_id, request.client_request_id)
        chain = plan["_chain"]
        store = _project_store_factory()
        # A repeat of a shoot that landed: the take is on the chain already; answer it, submit nothing.
        existing = _read_client_row(request.client_request_id) if plan["submit_args"] is not None else None
        if existing is not None:
            recorded = next((t for t in chain["takes"] if t["studio_job_id"] == existing.studio_job_id), None)
            if recorded is not None:
                return {"revision": chain["revision"], "studio_job_id": recorded["studio_job_id"],
                        "inputs_sha256": recorded["inputs_sha256"], "replayed": True}
        if request.expected_revision != chain["revision"]:
            raise studio_core.ProjectError("chain_conflict", f"the chain is at revision {chain['revision']}")
        if not plan["support"]["supported"]:
            error = {"error": _chain_doc().error_body("not_supported", " ".join(plan["support"]["reasons"]) or "The shot cannot be shot now.")}
            error["error"]["reasons"] = list(plan["support"]["reasons"])
            return error
        answer = await _chain_submit(plan["submit_args"])
        details = answer.get("details") if isinstance(answer, dict) else None
        job_id = details.get("id") if isinstance(details, dict) else None
        if not isinstance(job_id, str) or answer.get("error"):
            message = answer.get("error") if isinstance(answer, dict) and isinstance(answer.get("error"), str) else "the submit was refused"
            body = _chain_doc().error_body("submit_failed", str(message))
            body["reason_code"] = answer.get("reason_code") if isinstance(answer, dict) else None
            return {"error": body}
        written = await asyncio.to_thread(_chain_record_take, store, plan["_directory"], plan["_project_id"], request.chain_id, job_id,
                                          by=_film_caller(), planned=chain)
        take = next(t for t in written["chain"]["takes"] if t["studio_job_id"] == job_id)
        return {"revision": written["chain"]["revision"], "studio_job_id": job_id, "inputs_sha256": take["inputs_sha256"]}
    except (studio_core.ProjectError, ValueError) as exc:
        return _chain_error(exc)


# --- camera transfer (lane w9-chain item 4): measure guide and take, keep it on the take ---------------------------
class ChainMeasureRequest(_ProjectRequest):
    project_ref: ProjectReference
    chain_id: str = Field(pattern=r"^ch-[0-9a-f]{8}$")
    studio_job_id: str = Field(min_length=1, max_length=120)


def _chain_measure_sync(request: ChainMeasureRequest) -> dict[str, Any]:
    directory, project_id, manifest = _chain_project(request.project_ref)
    store = _project_store_factory()
    envelope = _chain_envelope(store, directory, project_id, request.chain_id)
    chain = copy.deepcopy(envelope["chain"])
    take = next((t for t in chain["takes"] if t["studio_job_id"] == request.studio_job_id), None)
    if take is None:
        raise studio_core.ProjectError("take_unknown", f"{request.studio_job_id} is not a take of this chain")
    if chain["guide"] is None:
        raise studio_core.ProjectError("no_guide", "the chain has no guide to compare with")
    flow = _camera_flow()
    stored = _chain_stored_guide(chain["guide"], manifest)  # lane w34-sec (finding 1): guide_unreadable outside
    try:
        guide = _camera_measure(stored)
        shot = _camera_measure(_camera_take_file(request.studio_job_id, project_id))
    except _SceneError as exc:
        code = "take_unknown" if exc.code == "take_unknown" else "measure_unavailable"
        raise studio_core.ProjectError(code, exc.message) from None
    camera = flow.result(guide, shot)
    take["camera"] = camera
    store.write_chain(directory, project_id=project_id, chain_id=request.chain_id, expected_revision=chain["revision"], chain=chain,
                      bindings=envelope["bindings"], take_inputs=envelope["take_inputs"], by=_film_caller(), reason="camera measured")
    return {"camera": camera}


@project_router.post("/projects/chain/measure")
async def projects_chain_measure(request: ChainMeasureRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(_chain_measure_sync, request)
    except (studio_core.ProjectError, ValueError) as exc:
        return _chain_error(exc)
