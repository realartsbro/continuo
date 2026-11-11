from __future__ import annotations

# Lane w19-g1fresh (BCK-007, AUD-050, IMG-100): is the plugin's dependency environment ready in THIS process?
#
# On a fresh machine Hermes' package manager publishes the environment with the plugin's declared
# python_dependencies (plugin.yaml) about 100 s after the first boot, and a running process never switches
# environments (Hermes docs, reference/package-management.md, Troubleshooting: "New environment requires restart
# ... Do not add a second site-packages tree to its live imports"). So the plugin never edits sys.path; it only
# Read-only: importability can establish readiness, but not whether the package manager is still
# preparing the next environment or whether this process needs a restart.

_DEPENDENCY_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _declared_plugin_requirements() -> list[str]:
    manifest = Path(__file__).resolve().parents[1] / "plugin.yaml"
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out, inside = [], False
    for line in lines:
        if re.match(r"^python_dependencies\s*:", line):
            inside = True
            continue
        if inside:
            item = re.match(r"^\s+-\s*[\"']?([^\"'#]+)[\"']?", line)
            if item:
                out.append(item.group(1).strip())
            elif line.strip() and not line.startswith((" ", "\t", "#")):
                break
    return out


# A distribution whose import name differs from its name (pillow installs the module PIL).
_IMPORT_NAMES = {"pillow": "PIL"}


def _requirement_module(requirement: str) -> str:
    match = _DEPENDENCY_NAME_RE.match(requirement)
    name = (match.group(1) if match else requirement).replace("-", "_").lower()
    return _IMPORT_NAMES.get(name, name)


def _module_importable(name: str) -> bool:
    if name in sys.modules and sys.modules[name] is not None:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _film_suite_dependencies() -> dict[str, Any]:
    """{ready, state, missing}: the fact the page and Hermes read while the dependency environment is prepared."""
    requirements = _declared_plugin_requirements()
    missing_reqs = [req for req in requirements if not _module_importable(_requirement_module(req))]
    missing = [_requirement_module(req) for req in missing_reqs]
    if not missing:
        return {"ready": True, "state": "ready", "missing": []}
    return {"ready": False, "state": "preparing", "missing": missing}


def _dependency_fact(missing_module: str | None) -> dict[str, Any] | None:
    """The readiness fact when ``missing_module`` is one of the plugin's declared dependencies, else None."""
    if not missing_module:
        return None
    facts = _film_suite_dependencies()
    if facts["ready"] or missing_module.split(".")[0].lower() not in facts["missing"]:
        return None
    return facts


_DEPENDENCY_TEXTS = {
    # Facts, no instruction (DESIGN "UI texts"): what is happening, and what the state waits for.
    ("audio", "preparing"): "Preparing the Audio engine, or restart Hermes",
    ("image", "preparing"): "Preparing picture import, or restart Hermes",
}


@router.get("/dependencies")
async def film_suite_dependencies() -> dict[str, Any]:
    """Readiness of the plugin's declared Python dependencies in this backend process (see above), plus the external
    tools (w22-dlv BCK-213): ffmpeg and ffprobe are not Python modules, so ``ready`` keeps its meaning and
    ``tools`` / ``missing_tools`` / ``tools_fix`` name a missing one with its fix."""
    facts = await asyncio.to_thread(_film_suite_dependencies)
    missing_tools = await asyncio.to_thread(_missing_media_tools)
    engine = await asyncio.to_thread(_comfy_endpoint_module.engine_state)  # w39-p2-tester item 2: the image engine
    return {**facts, "tools": {name: name not in missing_tools for name in ("ffmpeg", "ffprobe")},
            "missing_tools": missing_tools, "tools_fix": dict(_FFMPEG_FIX), "engine": engine}


# The Audio and Image answers name the readiness state instead of "<module> not installed" when the missing module is
# one of the plugin's declared dependencies (appended overrides; every caller resolves these names at call time).
_music_error_before_w19_g1fresh = _music_error


def _music_error(exc: BaseException) -> str:  # noqa: F811 - append-only extension
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        fact = _dependency_fact(exc.name)
        if fact is not None:
            return _DEPENDENCY_TEXTS[("audio", fact["state"])]
    return _music_error_before_w19_g1fresh(exc)


_image_film_error_response_before_w19_g1fresh = _image_film_error_response


def _image_film_error_response(exc: BaseException) -> Any:  # noqa: F811 - append-only extension
    answer = _image_film_error_response_before_w19_g1fresh(exc)
    try:
        error = answer.get("error") if isinstance(answer, dict) else None
        if isinstance(error, dict) and error.get("code") == "image_dependency_missing":
            fact = _dependency_fact(_missing_module(exc))
            if fact is not None:
                return {"error": {"code": "image_dependency_missing", "message": _DEPENDENCY_TEXTS[("image", fact["state"])]}}
    except Exception:  # noqa: BLE001 - the original answer stays
        return answer
    return answer
