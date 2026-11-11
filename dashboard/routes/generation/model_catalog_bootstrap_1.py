from __future__ import annotations


# --- Model & capability layer (masterplan package 1) -------------------------
# Loaded under its own module name and never registered in sys.modules, so the
# synthetic generation package tree stays exactly as the route loader pins it.
def _load_model_catalog_module() -> Any:
    module_path = Path(__file__).with_name("generation") / "model_catalog.py"
    spec = importlib.util.spec_from_file_location("film_suite_model_catalog", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("The model catalog module is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_model_catalog_module = _load_model_catalog_module()
_MODEL_CATALOG_COMFY_ROOT = None  # None: FILM_SUITE_COMFY_ROOT read per request (generation/comfy_root.py, w7-rest)
_MODEL_CATALOG_TIMEOUT_SECONDS = 60.0


def _load_comfy_endpoint_module() -> Any:
    """The one configured ComfyUI address (w39-p2-tester item 4); the same process-wide probe as every client."""
    spec = importlib.util.spec_from_file_location("film_suite_comfy_endpoint", Path(__file__).with_name("generation") / "comfy_endpoint.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("The ComfyUI address module is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_comfy_endpoint_module = _load_comfy_endpoint_module()
