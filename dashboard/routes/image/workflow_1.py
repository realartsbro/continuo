from __future__ import annotations


@project_router.post("/projects/image-producer/session")
async def image_producer_session(http_request: Request) -> dict[str, Any]:
    """Establish Desktop authority only through the host's verified HTTP gate."""
    try:
        _image_film_caller_scope_module._read_request_auth_state(http_request)
        _image_film_caller_scope_module._desktop_loopback_request_is_authenticated(http_request)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 - same finite Image boundary
        return _image_film_error_response(exc)


class ImageWorkflowReference(_ProjectRequest):
    image_producer_job_id: str = Field(pattern=r"^imgjob_[0-9a-f]{32}$")
    role: str = Field(min_length=1, max_length=64)


class ImageWorkflowSubmitRequest(_ProjectRequest):
    schema_: Literal["studio.image-workflow-request/1"] = Field(alias="schema")
    owner_scope: str = Field(pattern=_OWNER_SCOPE_PATTERN, min_length=18, max_length=47)
    operation_id: str = Field(pattern=r"^imgop_[0-9a-f]{32}$")
    target_id: str = Field(pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$", max_length=64)
    prompt: str = Field(min_length=1, max_length=16_384)
    controls: dict[str, Any]
    references: list[ImageWorkflowReference] = Field(max_length=16)
    # w1-imgout: N pictures (seed + i each) and a LoRA chain [{file, strength, role?}] for every catalog target.
    batch: int | None = Field(default=None, ge=1, le=8)
    loras: list[dict[str, Any]] | None = Field(default=None, max_length=32)
    # Lane w6-link: the Script shot this run makes a frame for ("+ Frame"); recorded in the job's recipe.
    shot_id: str | None = Field(default=None, pattern=r"^sh-[0-9a-f]{8}$", min_length=11, max_length=11)
    # Lane w35-ui-3 (critic run 3 #11): the artist's own words when ``prompt`` is a caption built from them (Ideogram 4's
    # JSON caption); the job's human title in Jobs, Media and the Image stage. Never part of the snapshot.
    title: str | None = Field(default=None, min_length=1, max_length=2_000)


class ImageWorkflowHealthRequest(_ProjectRequest):
    schema_: Literal["studio.image-workflow-request/1"] = Field(alias="schema")
    target_id: str = Field(pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$", max_length=64)
    prompt: str = Field(min_length=1, max_length=16_384)
    controls: dict[str, Any]
    references: list[ImageWorkflowReference] = Field(max_length=16)
    loras: list[dict[str, Any]] | None = Field(default=None, max_length=32)


def _image_workflow_run_module():
    return importlib.import_module(_image_contract_module().__package__ + ".image_workflow_run")


async def _image_workflow_loras(target_id: str, raw: Any) -> tuple[list[dict[str, Any]] | None, JSONResponse | None]:
    """The canonical LoRA list for a catalog target, or the finite refusal (unsupported, unlisted, other family)."""
    run = _image_workflow_run_module()
    if raw is None:
        return None, None
    try:
        loras = run.normalize_loras(raw)
        if not loras:
            return None, None
        family_id = _image_workflow_module().entries()[target_id][0]["id"]
        support = run.lora_support(target_id, family_id)
        if not support["available"]:
            raise run.RunError("loras_unavailable", f"LoRAs are not available for this model: {support['reason']}")
        try:
            live = await _image_live_loras()
        except Exception:  # noqa: BLE001 - engine down
            return None, _edit_engine_unreachable()
        library, root, catalog = _image_lora_library(), _image_lora_root(), _image_lora_catalog()

        def base_of(name: str) -> str:
            row = _image_lora_row(library, root, name, catalog)
            return (row or {}).get("base_model") or ""
        await asyncio.to_thread(run.admit_loras, target_id, family_id, loras, live, base_of)
    except run.RunError as exc:
        return None, _edit_error(422, exc.code, exc.message)
    return loras, None


def _image_workflow_module():
    return importlib.import_module(_image_contract_module().__package__ + ".image_workflow_contract")


@project_router.post("/projects/image-producer/workflow-health")
async def image_workflow_health(request: ImageWorkflowHealthRequest) -> Any:
    workflow = _image_workflow_module()
    if request.target_id not in workflow.entries():
        return _edit_error(422, "target_unavailable", "This Image target is not registered")
    desc = workflow.entries()[request.target_id][1]["descriptor"]
    refs = [{"image": "filmsuite-ref-" + "0" * 32 + ".png [temp]", "role": r.role} for r in request.references]
    if not refs and desc["capabilities"]["edit"]:
        refs = [{"image": "filmsuite-ref-" + "0" * 32 + ".png [temp]", "role": "target"}]
    # w20-img (MOD-006): health checks graph, nodes and files, never the artist's words; a target whose prompt is a
    # JSON caption (Ideogram 4) is probed with a neutral caption, or the prose probe refused the whole family.
    try:
        manifest = workflow.compiler(request.target_id).load_assets(request.target_id)[0]
    except Exception:  # noqa: BLE001 - an adapter without manifest assets keeps the sent prompt
        manifest = {}
    probe = '{"caption": "Static check"}' if (manifest.get("prompt") or {}).get("format") == "json_object" else request.prompt
    compiled = {"schema": request.schema_, "target_id": request.target_id, "prompt": probe, "controls": request.controls, "references": refs}
    loras, refused = await _image_workflow_loras(request.target_id, request.loras)
    if refused is not None:
        return refused
    if loras:
        compiled["loras"] = loras
    runtime_module = importlib.import_module(_image_contract_module().__package__ + ".image_workflow_runtime")
    client_module = importlib.import_module(_image_contract_module().__package__ + ".comfyui_client")
    try:
        health = await runtime_module.ImageWorkflowRuntime(client_module.ComfyUIClient(), None, None).health(compiled)
        return {**health, "availability": "runnable", "target_id": request.target_id}
    except ValueError:
        return _edit_error(422, "invalid_image_request", "The Image controls do not match the descriptor")
    except Exception:
        engine = _comfy_endpoint_module.engine_state()  # w39-p2-tester item 2: say why, with the address
        return {"state": "unreachable", "inference_tested": False, "target_id": request.target_id, "engine": engine,
                **({"reason": engine["reason"]} if engine.get("reason") else {})}



def _workflow_refusal(exc: BaseException) -> JSONResponse:
    """w31-fix4 item 3 (IMG-223): 422 naming the adapter's own reason and the request field it is about."""
    reason = str(exc) if type(exc).__name__ in ("WorkflowRequestError", "ContractError", "RunError") else ""
    if not reason or len(reason) > 200 or "\n" in reason:
        return _edit_error(422, "invalid_image_request", "The Image request does not match its descriptor")
    words = (("control", "controls"), ("reference", "references"), ("lora", "loras"), ("size", "controls"), ("prompt", "prompt"))
    field = next((name for word, name in words if word in reason.lower()), None)
    return JSONResponse(status_code=422, content={"error": {"code": "invalid_image_request", "message": f"Refused: {reason}",
                                                            "reason": reason, **({"problem_field": field} if field else {})}})

@project_router.post("/projects/image-producer/workflow-submit")
async def image_workflow_submit(request: ImageWorkflowSubmitRequest) -> Any:
    """Owned descriptor intent -> immutable snapshot -> existing durable coordinator.

    References are published jobs of this owner, never upload strings, paths or
    URLs. Uploads occur only inside the adapter AFTER durable job admission.
    """
    workflow = _image_workflow_module()
    if request.target_id not in workflow.entries():
        return _edit_error(422, "target_unavailable", "This Image target is not registered")
    async with _IMAGE_FILM_ROOT_LOCK_ASYNC:
        root = _image_producer_root(request.owner_scope)
        handoff = _image_operations_handoff(root, request.owner_scope)
        jobs, states = handoff.producer_jobs(), handoff.image_states()
        coordinator = handoff.producer_coordinator()
    refs = []
    for ref in request.references:
        source = _edit_owned_job(jobs, states, ref.image_producer_job_id)
        if source is None:
            return _edit_error(404, "picture_unknown", "The reference picture is unknown")
        if source.get("state") != "published":
            return _edit_error(409, "picture_not_ready", "The reference picture is not ready")
        artifact = source["artifact"]
        refs.append({"image_producer_job_id": ref.image_producer_job_id, "role": ref.role,
                     "sha256": artifact["artifact_sha256"], "width": artifact["width"], "height": artifact["height"]})
    key = "imgop_" + hashlib.sha256((request.owner_scope + ":" + request.operation_id).encode()).hexdigest()[:32]
    loras, refused = await _image_workflow_loras(request.target_id, request.loras)
    if refused is not None:
        return refused
    batch = request.batch or 1
    try:
        try:
            previous = jobs.get_by_idempotency_key(key)
        except Exception as exc:
            if type(exc).__name__ != "ImageJobNotFoundError":
                raise
            previous = None
        if previous:
            basis, policy = previous["context_basis"], previous["attach_policy"]
            states.require_owned(basis["context_id"])
        else:
            working = states.ensure_working_context(key)
            claim = states.load_current_owner(working["context_id"], working["work_id"], "draft", None)
            basis = {k: claim[k] for k in ("context_id", "work_id", "basis_kind", "revision_id", "draft_generation", "basis_content_sha256")}
            policy = {"mode": "current", "owner_token": claim["owner_token"], "expected_revision": claim["expected_revision"]}
        intent = {"schema": request.schema_, "target_id": request.target_id, "prompt": request.prompt, "controls": request.controls,
                  **({"loras": loras} if loras else {})}
        snapshot = workflow.build_snapshot(intent, refs, basis, policy)
        if previous and snapshot != previous["request_snapshot"]:
            return _edit_error(409, "idempotency_conflict", "This operation already has another request")
        if not previous:
            # w24-h3img: a target whose adapter knows its memory need refuses by name before the ledger
            # (P-H3-Image, like the video upscaler's memory guard); unreadable engine stats never refuse.
            memory_check = getattr(workflow.compiler(request.target_id), "memory_check", None)
            if memory_check is not None:
                try:
                    stats = await importlib.import_module(_image_contract_module().__package__ + ".comfyui_client").ComfyUIClient().system_stats()
                except Exception:  # noqa: BLE001 - the engine check below answers an unreachable engine
                    stats = None
                try:
                    memory_check(snapshot["effective_input"]["workflow"], stats)
                except ValueError as exc:
                    if getattr(exc, "reason_code", None):
                        return _edit_error(409, exc.reason_code, str(exc))
                    raise
                # w28-fix item 3 (IMG-220): another model still in graphics memory -> /free first, through the one helper
                # that exists (Deliver's RIFE pass: only while ComfyUI's queue is empty, never the artist's queued work).
                free_first = getattr(importlib.import_module(type(workflow.compiler(request.target_id)).__module__), "free_first", None)
                if callable(free_first) and free_first(stats):
                    try:
                        _timeline_export_package()
                        rife = importlib.import_module(_TIMELINE_EXPORT_PACKAGE_NAME + ".rife_comfy")
                        await asyncio.to_thread(rife.free_when_idle)
                    except Exception:  # noqa: BLE001 - freeing is a help, never a refusal
                        pass
            runtime = coordinator.adapter.routes[request.target_id]
            try:
                health = await runtime.health(snapshot["effective_input"]["workflow"])
            except Exception:
                return _edit_engine_unreachable()
            if health["state"] != "ready_static":
                return _edit_error(503, "image_target_not_ready", "Required files, nodes or controls are unavailable")
        # Batch (w1-imgout): picture i is its own ledger job with seed + i; picture 0 keeps the
        # single-picture key, so batch 1 is exactly the request before; a replay replays the whole run.
        members = []
        seed_default = snapshot["effective_input"]["workflow"]["controls"].get("seed", 0)
        for index in range(batch):
            member_key = key if index == 0 else f"{key}:run-{index}"
            member = snapshot if index == 0 else workflow.build_snapshot(
                {**intent, "controls": _image_workflow_run_module().member_controls(snapshot["effective_input"]["workflow"]["controls"], seed_default, index)},
                refs, basis, policy)
            member_job = await coordinator.submit(member_key, member, client_request_id=member_key)
            members.append((index, member["artist_input"]["seed"], member_job))
        job = members[0][2]
        # w28-fix item 2a (IMG-218): named like the hand path (producer_1.py, lane p6-name): each member's rule name is
        # reserved in <project>/naming.json now, so Jobs shows it while it runs and Keep writes <output>/<that name>.
        result_names = _image_result_names_reserve(request.owner_scope, [m[2]["image_producer_job_id"] for m in members])
        _image_job_recipe_write([m[2]["image_producer_job_id"] for m in members], target_id=request.target_id, prompt=request.prompt, shot_id=request.shot_id)
        _image_job_title_write([m[2]["image_producer_job_id"] for m in members], request.title)  # w35-ui-3 #11
        shot_id = _image_job_shot(job["image_producer_job_id"])
        _announce_image_jobs([(m[2]["image_producer_job_id"], None, m[2].get("state")) for m in members], request.owner_scope)
        return {"schema": "studio.image-producer-operation/1", "kind": "image_producer_submit_result",
                **({"run": {"batch_size": batch, "jobs": [{"image_producer_job_id": m[2]["image_producer_job_id"], "batch_index": m[0],
                                                           "seed": m[1], "state": m[2]["state"]} for m in members]}} if batch > 1 else {}),
                **({"loras": loras} if loras else {}),
                "owner_scope": request.owner_scope, "operation_id": request.operation_id,
                "image_producer_job_id": job["image_producer_job_id"], "state": job["state"],
                "result_name": result_names.get(job["image_producer_job_id"]),
                "target_id": job["provider"]["target_id"], "provider_id": job["provider"]["provider_id"],
                "request_contract": request.schema_, "sent_prompt": job.get("sent_prompt"), "render_spec": _job_render_spec(job),
                "title": _image_human_title(job),
                **({"shot_id": shot_id} if shot_id else {}),
                **({"quarantine_reason": job["quarantine_reason"]} if job.get("quarantine_reason") else {})}
    except (ValueError, KeyError, TypeError) as exc:
        # w31-fix4 item 3 (IMG-223): a refusal names its field and why (Ideogram 4: "prompt must be a JSON object"),
        # never only the generic descriptor sentence (the page and Hermes show the reason as it is).
        return _workflow_refusal(exc)
