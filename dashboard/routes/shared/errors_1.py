from __future__ import annotations


_PROJECT_ERROR_MESSAGES = {
    "invalid_project": "Project request is invalid",
    "project_not_found": "Project not found",
    "project_manifest_missing": "The project folder no longer has its project file",
    "project_incomplete": "This project was not finished being created.",
    "project_exists": "Project already exists",
    "project_not_empty": "Project directory must be empty",
    "project_conflict": "Project changed since this revision",
    "project_scope_mismatch": "Project belongs to a different context",
    "project_unavailable": "Project is unavailable",
    "project_io_failed": "Project could not be saved",
    # Lane w33-rel2 item 1 (BCK-227): another process held the project lock past the bounded wait.
    "project_busy": "Project is busy: another window or Hermes is saving it. Nothing was changed.",
}
_PROJECT_ERROR_CODES = frozenset(_PROJECT_ERROR_MESSAGES)


_IMAGE_FILM_ERROR_MESSAGES = MappingProxyType(
    {
        "image_film_unavailable": "Image and Film state is unavailable",
        "image_key_unavailable": "Image state key is unavailable",
        "image_state_unavailable": "Image state is unavailable",
        "image_state_not_private": "Image state files lost their private access rights (copied home folder)",
        "image_context_not_owned": "This image belongs to another project",
        "binding-target-required": "A binding or Film reference is required",
        "binding-id-invalid": "The Image binding identifier is invalid",
        "film-reference-id-invalid": "The Film reference identifier is invalid",
        "binding-missing": "The Image binding was not found",
        "caller_identity_unavailable": "Caller identity is unavailable",
        "caller_identity_invalid": "Caller identity is invalid",
        "image_catalog_unavailable": "Image source catalog is unavailable",
        "image_catalog_state_unavailable": "Image source catalog state is unavailable",
        "image_catalog_identity_changed": "Image source catalog changed during the read",
        "image_catalog_limit_exceeded": "Image source catalog exceeds its bounded limits",
        "image_catalog_response_too_large": "Image source catalog response is too large",
    }
)
_IMAGE_FILM_ERROR_CODES = frozenset(_IMAGE_FILM_ERROR_MESSAGES)
def _image_catalog_error_response(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, _ImageFilmCallerScopeError):
        code = exc.code
    elif isinstance(exc, _ImageFilmCompositionError):
        code = exc.code
        if code in {"image_film_unavailable", "image_key_unavailable", "root_closed"}:
            code = "image_catalog_unavailable"
        elif code not in {
            "image_catalog_unavailable",
            "image_catalog_state_unavailable",
            "image_catalog_identity_changed",
            "image_catalog_limit_exceeded",
            "image_catalog_response_too_large",
        }:
            code = "image_catalog_state_unavailable"
    else:
        code = "image_catalog_state_unavailable"
    return {"error": {"code": code, "message": _IMAGE_FILM_ERROR_MESSAGES[code]}}


_IMAGE_CALLER_SCOPE_RE = re.compile(r"^imgcaller_[0-9a-f]{64}$")


_MODULE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _missing_module(exc: BaseException) -> str | None:
    """The top-level module a ModuleNotFoundError / ImportError in the cause chain names, else None."""
    try:
        cause, seen = exc, 0
        while cause is not None and seen < 8:
            if isinstance(cause, ImportError):
                name = (getattr(cause, "name", None) or "").split(".")[0]
                if _MODULE_NAME_RE.fullmatch(name):
                    return name
            cause, seen = cause.__cause__ or cause.__context__, seen + 1
    except Exception:  # noqa: BLE001 - the answer stays finite
        return None
    return None


def _image_film_error_response(exc: BaseException) -> dict[str, Any]:
    try:
        code = getattr(exc, "code", None)
    except BaseException:
        code = None
    if type(code) is not str:
        code = None
    # The answer stays finite; the cause goes to the Hermes log so a live
    # failure can be diagnosed (type + code + the cause chain's types only).
    try:
        chain, cause = [], exc
        while cause is not None and len(chain) < 6:
            chain.append(f"{type(cause).__name__}:{getattr(cause, 'code', '')}")
            cause = cause.__cause__ or cause.__context__
        import logging as _logging
        _logging.getLogger("film_suite.image").warning("image route failed: %s", " <- ".join(chain), exc_info=exc)
    except Exception:  # noqa: BLE001 - logging must never break the answer
        pass
    if code in _PROJECT_ERROR_CODES:
        return _project_error_response(exc)
    # P3S-12 (IMG-100/104): a missing module or an import failure in the cause chain is passed through as the fact
    # ("numpy not installed") instead of the blanket "Image state is unavailable". Only the module name leaves.
    missing = _missing_module(exc)
    if missing is not None and code in (None, "image_state_unavailable", "image_film_unavailable"):
        return {"error": {"code": "image_dependency_missing", "message": f"{missing} not installed"}}
    if code not in _IMAGE_FILM_ERROR_CODES:
        code = "image_state_unavailable"
    return {"error": {"code": code, "message": _IMAGE_FILM_ERROR_MESSAGES[code]}}
