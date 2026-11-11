from __future__ import annotations


# --- The Continuo frame (lane w1-frame) ---------------------------------------------------------
# GET /system/gpu: one read-only VRAM line for the bar's jobs popover (nvidia-smi, 3 s timeout,
# cached 2 s; no GPU or no nvidia-smi answers available=false, never an error).
# /suite/bridge/state|actions|ack: the frame's renderer bridge (which page is open, the project,
# the Script page's scene list); the agent reads it with continuo_suite_state and opens a page
# with continuo_suite_action open_page, applied by the handler the bar button uses.
# Beside this file in routes/shared/ (the routes package is installed whole); not a route area.
_SUITE_BRIDGE_PATH = Path(__file__).with_name("routes") / "shared" / "suite_bridge.py"
_SUITE_BRIDGE_SPEC = importlib.util.spec_from_file_location("film_suite_suite_bridge", _SUITE_BRIDGE_PATH)
if _SUITE_BRIDGE_SPEC is None or _SUITE_BRIDGE_SPEC.loader is None:
    raise RuntimeError("The suite frame bridge is unavailable.")
suite_bridge = importlib.util.module_from_spec(_SUITE_BRIDGE_SPEC)
_SUITE_BRIDGE_SPEC.loader.exec_module(suite_bridge)


def _gpu_run(argv: list[str], timeout: float) -> str | None:
    exe = shutil.which(argv[0])
    if not exe:
        return None
    try:
        done = subprocess.run([exe, *argv[1:]], capture_output=True, text=True, encoding="utf-8", timeout=timeout, check=False, env=_child_env())  # noqa: S603 - fixed program and arguments
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


@router.get("/system/gpu")
async def system_gpu() -> dict[str, Any]:
    try:
        return await asyncio.to_thread(suite_bridge.gpu_status, _gpu_run)
    except Exception:  # noqa: BLE001 - finite plugin boundary
        return {"schema": suite_bridge.GPU_SCHEMA, "available": False, "gpus": [], "source": "nvidia-smi", "cached_seconds": suite_bridge.GPU_CACHE_SECONDS}


def _suite_bridge_store() -> Any:
    hermes_home = _active_hermes_home()
    if hermes_home is None:
        raise suite_bridge.SuiteBridgeError("Suite bridge storage is unavailable")
    return suite_bridge.SuiteBridgeStore(state_root(hermes_home / "plugins", "suite-bridge"))


def _suite_bridge_error(exc: Exception | None) -> dict[str, Any]:
    if exc is None or isinstance(exc, suite_bridge.SuiteBridgeError):
        return {"ok": False, "reason_code": "invalid_suite_bridge_request", "message": "Suite bridge request is invalid."}
    return {"ok": False, "reason_code": "suite_bridge_unavailable", "message": "Suite bridge is unavailable."}


class SuiteBridgeValidationRoute(APIRoute):
    """Strict request models with a finite, neutral error envelope."""

    def get_route_handler(self):
        original_route_handler = super().get_route_handler()

        async def finite_handler(request):
            try:
                return await original_route_handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content=_suite_bridge_error(None))

        return finite_handler


suite_bridge_router = APIRouter(route_class=SuiteBridgeValidationRoute)


class SuiteBridgeStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    window_id: str = Field(pattern=r"^win_[0-9a-f]{32}$", min_length=36, max_length=36)
    state: dict[str, Any]


class SuiteBridgeActionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    window_id: str = Field(pattern=r"^win_[0-9a-f]{32}$", min_length=36, max_length=36)


class SuiteBridgeAckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    window_id: str = Field(pattern=r"^win_[0-9a-f]{32}$", min_length=36, max_length=36)
    action_id: str = Field(pattern=r"^act_[0-9a-f]{32}$", min_length=36, max_length=36)
    ack: dict[str, Any]


@suite_bridge_router.post("/suite/bridge/state")
async def suite_bridge_state(request: SuiteBridgeStateRequest) -> dict[str, Any]:
    try:
        record = await asyncio.to_thread(_suite_bridge_store().write_snapshot, request.window_id, request.state)
        return {"ok": True, "schema": suite_bridge.STATE_SCHEMA, "window_id": record["window_id"], "revision": record["revision"]}
    except Exception as exc:  # noqa: BLE001 - finite plugin boundary
        return _suite_bridge_error(exc)


@suite_bridge_router.post("/suite/bridge/actions")
async def suite_bridge_actions(request: SuiteBridgeActionsRequest) -> dict[str, Any]:
    try:
        return {"ok": True, "actions": await asyncio.to_thread(_suite_bridge_store().pending, request.window_id)}
    except Exception as exc:  # noqa: BLE001 - finite plugin boundary
        return _suite_bridge_error(exc)


@suite_bridge_router.post("/suite/bridge/ack")
async def suite_bridge_ack(request: SuiteBridgeAckRequest) -> dict[str, Any]:
    try:
        record = await asyncio.to_thread(_suite_bridge_store().ack, request.window_id, request.action_id, request.ack)
        return {"ok": True, "schema": suite_bridge.ACK_SCHEMA, "action_id": record["action_id"]}
    except Exception as exc:  # noqa: BLE001 - finite plugin boundary
        return _suite_bridge_error(exc)


router.include_router(suite_bridge_router)
