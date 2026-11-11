from __future__ import annotations


def _model_catalog_client() -> Any:
    return _comfy_client_module.ComfyUIClient()


class ModelCatalogValidationRoute(APIRoute):
    """Translate model catalog request-model failures to a finite error."""

    def get_route_handler(self):
        original_route_handler = super().get_route_handler()

        async def finite_handler(request):
            try:
                return await original_route_handler(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={
                        "error": {
                            "code": "invalid_model_catalog_request",
                            "message": "Model catalog request is invalid",
                        }
                    },
                )

        return finite_handler


model_catalog_router = APIRouter(route_class=ModelCatalogValidationRoute)


class ModelCatalogRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    check_classes: bool = True
    # Lane fs-voice: voice families are listed only when asked ("voice"), so the
    # Image/Shoot pickers keep their answer; omitted = image and video.
    kinds: list[Literal["image", "video", "voice"]] | None = Field(default=None, min_length=1, max_length=3)


@model_catalog_router.post("/models/catalog")
async def models_catalog(request: ModelCatalogRequest) -> Any:
    """Families, variants, operations, inputs, presets, file names and health.

    Read-only: scans model folders off the event loop (bounded, names only)
    and, when asked, probes ``GET /object_info/{class}``; never queues work.
    """
    try:
        classes = None
        engine_files: frozenset[str] = frozenset()
        engine = None
        if request.check_classes:
            catalog = await asyncio.to_thread(_model_catalog_module.load_catalog)
            # w39-p2-tester item 4: the engine's own loader lists count as present (a ComfyUI on another port keeps
            # its models in its own folder), and the answer says whether an engine answered and where.
            classes, engine_files = await _model_catalog_module.probe_engine(
                _model_catalog_client(), _model_catalog_module.required_classes(catalog)
            )
            engine = await asyncio.to_thread(_comfy_endpoint_module.engine_state, answered=classes is not None)
        out = await asyncio.wait_for(
            asyncio.to_thread(
                _model_catalog_module.build_public_catalog,
                classes,
                comfy_root=_MODEL_CATALOG_COMFY_ROOT,
                engine_files=engine_files,
                **({"kinds": tuple(request.kinds)} if request.kinds else {}),
            ),
            _MODEL_CATALOG_TIMEOUT_SECONDS,
        )
        return {**out, "engine": engine} if engine is not None else out
    except Exception:  # noqa: BLE001 - finite, path-free plugin boundary
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "model_catalog_unavailable", "message": "Model catalog is unavailable"}},
        )
