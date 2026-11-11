from __future__ import annotations


def _public_requested_args(value: Any, key: str = "") -> Any:
    """Project only creative request values; never copy path/private fields."""
    forbidden = {
        "approved_roots", "project_ref", "save_to", "input_video", "input_image",
        "source_path", "prepared_path", "directory", "frozen_destination",
    }
    if key in forbidden or key.endswith(("_path", "_directory", "_root")):
        return None
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for child_key, child_value in value.items():
            if not isinstance(child_key, str) or child_key in forbidden:
                continue
            projected = _public_requested_args(child_value, child_key)
            if projected is not None:
                result[child_key] = projected
        return result
    if isinstance(value, (list, tuple)):
        return [_public_requested_args(item, key) for item in value]
    return value


def _receipt_from_job(record: Any, *, operation: Any | None = None) -> dict[str, Any]:
    """Map a JobRecord through the finite, path-safe public receipt allowlist."""
    request = record.request if callable(getattr(record, "request", None)) else getattr(record, "request", {})
    if not isinstance(request, Mapping):
        request = {}
    requested = _public_requested_args(request.get("args", {}))
    row_contract = getattr(record, "row_contract", 3)
    saved_path = getattr(record, "published_path", None)
    if row_contract == 4:
        saved_path = None
        if operation is not None and getattr(record, "state", None) == "ready":
            try:
                saved_path = _private_ops().saved_path_for_receipt(operation)
            except (PrivateOperationError, OSError, ValueError):
                saved_path = None
    details: dict[str, Any] = {
        "id": record.studio_job_id,
        "provider_job_id": record.provider_job_id,
        "provider_id": record.provider_id,
        "target_id": record.target_id,
        "status": _normalized_state(record.state),
        "requested_basename": record.requested_basename,
        "queued_at": record.queued_at,
        "running_at": record.running_at,
        "completed_at": record.completed_at,
        "saved_path": saved_path,
        "saved_bytes": None,
        "requested": requested,
        "effective": None,
    }
    if row_contract == 4:
        details.update({"row_contract": 4, "project_id": getattr(record, "project_id", None)})
        if _h3_is_take(record) or getattr(record, "target_id", None) == _vsr_module.TARGET_ID:  # w23-vsr: upscale facts
            details.update(_h3_public_facts(record.studio_job_id) or {})
    receipt = {
        "result": None,
        "details": details,
        "reason_code": getattr(record, "reason_code", None),
        "retry_after": None,
    }
    untested = details.get("runtime") if row_contract == 4 and details.get("runtime_tested") is False else None
    if getattr(record, "state", None) == "failed" and isinstance(untested, Mapping):
        # A take that fails on a runtime nobody tested says so (the artist knows why).
        reason = generation.contract.public_reason(receipt["reason_code"])
        receipt["reason_text"] = f"{reason}. Engine version {untested.get('comfyui_version')} is not tested with this model yet."
    if record.provider_id == GMI_PROVIDER_ID:
        reason_detail = None
        reason_text = None
        if receipt["reason_code"] == "provider_refused":
            # For GMI a refusal is only ever the 402 credit gate.
            reason_detail = "credit_required"
            reason_text = _gmi_module.CREDIT_MESSAGE
        elif receipt["reason_code"] is not None:
            reason_text = _GMI_REASON_TEXT.get(record.studio_job_id)
        receipt["reason_detail"] = reason_detail
        receipt["reason_text"] = reason_text
    return receipt


def _normalized_state(state: str) -> str:
    mapping = {
        "submitting": "queued",
        "queued": "queued",
        "running": "running",
        "ready": "ready",
        "failed": "failed",
        "moderated": "moderated",
        "cancelled": "cancelled",
    }
    return mapping.get(state, "failed")


async def _comfy_submit_route(request: SubmitRequest) -> dict[str, Any]:
    """Versioned Comfy submit: transactional admission, then one /prompt."""
    if request.provider_id != "comfyui-local":
        return _error("Generation provider not supported.")
    if not request.client_request_id:
        return _error("A client_request_id is required for Comfy jobs.")
    if "prompt" not in request.args:
        return _error("A prompt is required.")
    try:
        store = _comfy_store()
        profile, graph = _comfy_assets()
        manifest_provider_id = profile.manifest.get("provider_id")
        manifest_target_id = profile.manifest.get("id")
        if (
            request.provider_id != manifest_provider_id
            or request.target_id != manifest_target_id
        ):
            return {
                "error": generation.contract.public_reason("invalid_request"),
                "reason_code": "invalid_request",
            }
        client = _comfy_client_module.ComfyUIClient()
        # Exact retries must still recover their ledger row even if the runtime
        # changed after the original submission. Fresh jobs, however, are
        # fail-closed at the authoritative backend boundary immediately before
        # the only path that can call ComfyUI /prompt.
        try:
            existing = store.get_by_client_request_id(request.client_request_id)
        except _comfy_job_store_module.JobNotFoundError:
            existing = None
        if existing is None:
            health = await _comfy_provider_module.static_health(
                client=client,
                manifest=profile.manifest,
                graph=graph,
            )
            if (
                health.provider_id != request.provider_id
                or health.target_id != request.target_id
            ):
                return {
                    "error": generation.contract.public_reason("invalid_request"),
                    "reason_code": "invalid_request",
                }
            if health.state != "ready_static":
                reason_code = health.reason_code or "unknown"
                return {
                    "error": generation.contract.public_reason(reason_code),
                    "reason_code": reason_code,
                }
        record = await _comfy_provider_module.comfy_submit(
            job_store=store,
            client=client,
            manifest=profile.manifest,
            graph=graph,
            args=dict(request.args),
            client_request_id=request.client_request_id,
            workflow_hash=profile.manifest["workflow"]["api_sha256"],
        )
        return _receipt_from_job(record)
    except Exception as exc:
        return _comfy_error(exc, "Comfy submission failed")


async def _comfy_poll_route(request: PollRequest) -> dict[str, Any]:
    """Versioned Comfy poll by Studio job id."""
    try:
        store = _comfy_store()
        client = _comfy_client_module.ComfyUIClient()
        profile, graph = _comfy_assets()
        record = await _comfy_provider_module.comfy_poll(
            job_store=store,
            client=client,
            studio_job_id=request.id.strip(),
            manifest=profile.manifest,
        )
        # A ready job with a captured output identity proceeds to safe
        # publication: fetch the declared output, validate, and publish under
        # the frozen destination.  Idempotent (a published row returns
        # unchanged); a lease guards concurrent publishers.
        if (
            getattr(record, "state", None) == "ready"
            and getattr(record, "provider_output_identity_json", None)
            and getattr(record, "publication_state", None) != "published"
        ):
            record = await _comfy_provider_module.publish_job_output(
                job_store=store,
                client=client,
                manifest=profile.manifest,
                studio_job_id=request.id.strip(),
            )
        return _receipt_from_job(record)
    except Exception as exc:
        return _comfy_error(exc, "Comfy poll failed")


def _comfy_error(exc: BaseException, prefix: str) -> dict[str, Any]:
    """Map known Comfy lifecycle failures to stable messages, never raw paths."""
    known_messages = {
        _comfy_job_store_module.ProviderBusyError: "Provider is busy",
        _comfy_job_store_module.JobNotFoundError: "Generation request is invalid",
        _comfy_provider_module.InvalidRequestError: "Generation request is invalid",
        _comfy_provider_module.ComfyClientError: "ComfyUI is unreachable",
    }
    message = known_messages.get(type(exc))
    if message is None:
        for exc_type, text in known_messages.items():
            if isinstance(exc, exc_type):
                message = text
                break
    if message is None:
        message = "Unknown generation error"
    return _error(f"{prefix}: {message}")


@router.get("/generation/catalog")
async def generation_catalog() -> dict[str, Any]:
    """Read-only public projection of registered providers and targets."""
    try:
        return _generation_registry.catalog()
    except Exception:
        return _error("Generation catalog is unavailable")


def _static_checks_all(value: bool) -> dict[str, bool]:
    return {name: value for name in generation.contract.STATIC_HEALTH_CHECKS}


def _unreachable_generation_health(request: GenerationHealthRequest) -> dict[str, Any]:
    out = generation.StaticHealth(
        provider_id=request.provider_id,
        target_id=request.target_id,
        state="unreachable",
        checks=_static_checks_all(False),
        inference_tested=False,
        reason_code="unreachable",
    ).to_dict()
    if request.provider_id == "comfyui-local":
        # w39-p2-tester item 2: the one true reason names the fact and the address(es) asked.
        engine = _comfy_endpoint_module.engine_state()
        out["engine"] = engine
        out["reason"] = engine.get("reason") or f"ComfyUI at {engine.get('address')} answers; this model's check failed"
    return out


@router.post("/generation/health")
async def generation_health(request: GenerationHealthRequest) -> dict[str, Any]:
    """Truthful static dependency health; never claims inference was tested."""
    if _generation_registry.is_paid_video_provider(request.provider_id):  # lane p5-vid: no key read, no probe
        return _generation_registry.paid_target_refusal()
    try:
        _generation_registry.resolve(request.provider_id, request.target_id)
    except _generation_registry.InvalidRequestError as exc:
        return _error(f"invalid_request: {exc}")
    except Exception:
        return _unreachable_generation_health(request)
    if request.provider_id == "flux3-managed":
        try:
            available = bool(flux3.check_bfl_requirements())
        except Exception:
            return _unreachable_generation_health(request)
        if available:
            return generation.StaticHealth(
                provider_id=request.provider_id,
                target_id=request.target_id,
                state="ready_static",
                checks=_static_checks_all(True),
                inference_tested=False,
            ).to_dict()
        return _unreachable_generation_health(request)
    if request.provider_id == "comfyui-local":
        # Truthful static dependency health against the pinned runtime.
        # Never queues work and never calls /prompt; inference_tested is
        # always False.
        try:
            profile, graph = _comfy_assets(request.target_id)
            client = _comfy_client_module.ComfyUIClient()
            # _comfy_assets returns a WorkflowProfile object; tests may mock
            # it with a plain dict.  Accept both.
            manifest = profile.manifest if hasattr(profile, "manifest") else profile
            health = await _comfy_static_health(
                graph=graph,
                manifest=manifest,
                client=client,
            )
            out = health.to_dict()
            if out.get("reason_code") == "unreachable":
                return _unreachable_generation_health(request)  # w39-p2-tester item 2: one reason, with the address
            return out
        except Exception:
            return _unreachable_generation_health(request)
    if request.provider_id == GMI_PROVIDER_ID:
        # Read-only GET /models/wan3.0-video; never generates.
        try:
            client = _gmi_client()
            if client is None:
                return generation.StaticHealth(
                    provider_id=request.provider_id,
                    target_id=request.target_id,
                    state="misconfigured",
                    checks=_static_checks_all(False),
                    inference_tested=False,
                    reason_code="auth_required",
                ).to_dict()
            state, reason = await client.model_health()
            return generation.StaticHealth(
                provider_id=request.provider_id,
                target_id=request.target_id,
                state=state,
                checks=_static_checks_all(state == "ready_static"),
                inference_tested=False,
                reason_code=reason,
            ).to_dict()
        except Exception:
            return _unreachable_generation_health(request)
    return _error(f"invalid_request: provider {request.provider_id} is not supported")
