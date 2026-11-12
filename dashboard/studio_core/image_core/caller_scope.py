"""Resolve the only caller principals trusted by Image/Film authority.

Hermes authenticates every ``/api/plugins/film-suite/*`` request before the
plugin router runs, so this module uses no Hermes internal: it decides only
WHICH authenticated caller a request is.  A hosted sign-in carries a verified
``Session`` (public ``hermes_cli.dashboard_auth``) on ``request.state.session``.
The Desktop person is an HTTP request on the plugin mount in a Hermes Desktop
backend, from a loopback peer to a loopback ``Host``.  Hermes' own tool calls run
in-process (marked by ``trusted_agent_call``) and can only reuse a scope the
person established; they never establish one.

A request-state flag, a token-shaped value, a request body or header, or a
service ``TokenPrincipal`` is never enough.  The returned identifier is an
opaque principal scope for the Image/Film session authority; it is not a
browser, conversation, or user-session identifier.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
import hashlib
import json
import os
import secrets
import threading
from dataclasses import dataclass
from typing import Any


_CALLER_SCOPE_SCHEMA = "studio.image-film-caller-scope/1"
_HOSTED_SCOPE_KIND = "host_session_principal"
_DESKTOP_SCOPE_KIND = "hermes_desktop_loopback_principal"
_DESKTOP_SCOPE_PROVIDER = "hermes_desktop"
_DESKTOP_SCOPE_PRINCIPAL = "desktop"
_CALLER_SCOPE_PREFIX = "imgcaller_"
_MAX_ID_TEXT = 256
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_PLUGIN_MOUNT = "/api/plugins/film-suite/"  # where Hermes mounts plugin_api.router (plugin id film-suite)
_DESKTOP_SCOPE_NONCE = None
_DESKTOP_SCOPE_NONCE_LOCK = threading.Lock()
_AGENT_SENTINEL = object()
_AGENT_CALL = contextvars.ContextVar("studio_trusted_agent_call", default=None)


@contextmanager
def trusted_agent_call():
    # Child asyncio tasks inherit context, including after the parent resets it.
    # External server tasks and threads started outside this call do not.
    token = _AGENT_CALL.set(_AGENT_SENTINEL)
    try:
        yield
    finally:
        _AGENT_CALL.reset(token)


class ImageFilmCallerScopeError(ValueError):
    """Finite fail-closed error for the host caller identity boundary."""

    CODES = frozenset({"caller_identity_unavailable", "caller_identity_invalid"})

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in self.CODES:
            code = "caller_identity_unavailable"
        self.code = code
        self.reason_code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TrustedImageFilmCallerScope:
    """Opaque Image/Film authority scope derived from one verified principal.

    ``user_id`` is populated only for a hosted user principal.  Desktop is an
    explicitly authenticated local application principal, not a hosted user,
    so it carries ``principal_id="desktop"`` and an empty user/org pair.
    """

    scope_id: str
    provider: str
    org_id: str
    user_id: str

    @property
    def principal_kind(self) -> str:
        if self.provider == _DESKTOP_SCOPE_PROVIDER and not self.org_id and not self.user_id:
            return _DESKTOP_SCOPE_KIND
        return _HOSTED_SCOPE_KIND

    @property
    def principal_id(self) -> str:
        return _DESKTOP_SCOPE_PRINCIPAL if self.principal_kind == _DESKTOP_SCOPE_KIND else self.user_id

    @property
    def kind(self) -> str:
        """Compatibility alias for the canonical principal-kind label."""
        return self.principal_kind


def _fail(code: str) -> Any:
    raise ImageFilmCallerScopeError(code) from None


def _read_request_auth_state(request: Any) -> Any:
    """Read only Hermes-owned authentication state from ``request``.

    The loopback gate does not populate ``state.session``.  ``None`` therefore
    means "no hosted session", not "Desktop"; the Desktop person is established
    separately (``_desktop_loopback_request_is_authenticated``).
    """
    if request is None:
        _fail("caller_identity_unavailable")
    try:
        state = getattr(request, "state")
    except Exception:
        _fail("caller_identity_unavailable")
    try:
        token_principal = getattr(state, "token_principal", None)
        token_authenticated = getattr(state, "token_authenticated", False)
    except Exception:
        _fail("caller_identity_unavailable")
    # Image/Film accepts the interactive Hosted session and the explicitly
    # authenticated Desktop loopback principal only.  A service principal is a
    # different trust contract, even if Hermes has already verified it.
    if token_principal is not None or token_authenticated is not False:
        _fail("caller_identity_unavailable")
    try:
        session = getattr(state, "session", None)
    except Exception:
        _fail("caller_identity_unavailable")
    return state, session


def _verified_session_type() -> type[Any]:
    try:
        from hermes_cli.dashboard_auth import Session  # the public package (exported since before 0.22.7)
    except Exception:
        _fail("caller_identity_unavailable")
    return Session


_BAD_HOST_CHARS = frozenset('"\'<> \n\r\t/?#@\\')


def _host_name(host_header: Any) -> str:
    """The host name of an HTTP ``Host`` authority (``127.0.0.1:9119``, ``[::1]:9119``, ``localhost``), else ``""``."""
    if type(host_header) is not str:
        return ""
    value = host_header.strip().lower()
    if not value or any(char in _BAD_HOST_CHARS for char in value):
        return ""
    if value.startswith("["):
        close = value.find("]")
        if close == -1:
            return ""
        rest = value[close + 1:]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return ""
        return value[1:close]
    name, colon, port = value.partition(":")
    if colon and not port.isdigit():
        return ""
    return name


def _loopback_peer_and_host(request: Any) -> bool:
    """True when the socket peer AND the ``Host`` the caller addressed are both this machine's loopback."""
    try:
        client = getattr(request, "client", None)
        peer = getattr(client, "host", None) if client is not None else None
        host = _host_name(getattr(request, "headers").get("host", ""))
    except Exception:
        return False
    return type(peer) is str and peer in _LOOPBACK_HOSTS and host in _LOOPBACK_HOSTS


def _routed_through_the_plugin_mount(request: Any) -> bool:
    """True when the request reached the plugin through Hermes' own ``/api/plugins/film-suite/`` mount.

    Hermes mounts the plugin router under that prefix and runs its authentication middleware for every request to it
    (``auth_middleware`` on a loopback backend, the dashboard sign-in gate otherwise) before the route runs.  The
    plugin's in-process ASGI apps (Hermes' tool calls) serve the routes WITHOUT the prefix: a request there never
    passed Hermes' gate and cannot be the person.
    """
    try:
        scope = getattr(request, "scope")
        path = scope.get("path")
        root = scope.get("root_path") or ""
    except Exception:
        return False
    if type(path) is not str or type(root) is not str:
        return False
    if root and path.startswith(root):
        path = path[len(root):]
    return path.startswith(_PLUGIN_MOUNT)


def _desktop_loopback_request_is_authenticated(request: Any) -> str:
    """Establish the Desktop person's scope from a request Hermes already authenticated.

    Hermes refuses an unauthenticated ``/api/plugins/film-suite/*`` request before this router runs, so this module
    asks no Hermes internal (catalog review: "trust the request that reaches you").  It decides only WHICH
    authenticated caller this is: the Desktop person is an HTTP request on the plugin mount, in a Hermes Desktop
    backend (``HERMES_DESKTOP=1``), from a loopback peer to a loopback ``Host``.  A Hermes tool call (in-process,
    marked by ``trusted_agent_call``) never establishes it, whatever the request looks like.
    """
    if _AGENT_CALL.get() is _AGENT_SENTINEL:
        _fail("caller_identity_unavailable")
    if os.environ.get("HERMES_DESKTOP") != "1":
        _fail("caller_identity_unavailable")
    if not _routed_through_the_plugin_mount(request) or not _loopback_peer_and_host(request):
        _fail("caller_identity_unavailable")
    # Only the person's HTTP request may establish the process scope.
    global _DESKTOP_SCOPE_NONCE
    try:
        with _DESKTOP_SCOPE_NONCE_LOCK:
            if _DESKTOP_SCOPE_NONCE is None:
                _DESKTOP_SCOPE_NONCE = secrets.token_hex(32)
            nonce = _DESKTOP_SCOPE_NONCE
    except Exception:
        _fail("caller_identity_unavailable")
    if type(nonce) is not str or len(nonce) != 64:
        _fail("caller_identity_unavailable")
    return nonce


def _scope_from_projection(
    projection: dict[str, str],
    *,
    provider: str,
    org_id: str,
    user_id: str,
) -> TrustedImageFilmCallerScope:
    try:
        canonical = json.dumps(
            projection,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError):
        _fail("caller_identity_invalid")
    scope_id = _CALLER_SCOPE_PREFIX + hashlib.sha256(canonical).hexdigest()
    return TrustedImageFilmCallerScope(
        scope_id=scope_id,
        provider=provider,
        org_id=org_id,
        user_id=user_id,
    )


def _identity_text(session: Any, field: str, *, allow_empty: bool = False) -> str:
    try:
        value = object.__getattribute__(session, field)
    except Exception:
        _fail("caller_identity_invalid")
    if type(value) is not str:
        _fail("caller_identity_invalid")
    if not allow_empty and not value:
        _fail("caller_identity_invalid")
    if len(value) > _MAX_ID_TEXT or "\x00" in value:
        _fail("caller_identity_invalid")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail("caller_identity_invalid")
    try:
        value.encode("utf-8")
    except UnicodeError:
        _fail("caller_identity_invalid")
    return value


def resolve_trusted_image_film_caller_scope(request: Any) -> TrustedImageFilmCallerScope:
    """Resolve a verified Hosted or authenticated Desktop caller on ``request``.

    Hosted requests use ``request.state.session``, which Hermes populates only
    after provider verification.  The Desktop person is a request Hermes routed
    to the plugin mount in a Desktop loopback backend.  No request JSON,
    caller-provided session ID, arbitrary state marker, or service
    ``TokenPrincipal`` can create a scope.
    """
    _state, session = _read_request_auth_state(request)
    if session is not None:
        session_type = _verified_session_type()
        if type(session) is not session_type:
            _fail("caller_identity_invalid")

        provider = _identity_text(session, "provider")
        org_id = _identity_text(session, "org_id", allow_empty=True)
        user_id = _identity_text(session, "user_id")
        return _scope_from_projection(
            {
                "schema": _CALLER_SCOPE_SCHEMA,
                "kind": _HOSTED_SCOPE_KIND,
                "provider": provider,
                "org_id": org_id,
                "user_id": user_id,
            },
            provider=provider,
            org_id=org_id,
            user_id=user_id,
        )

    if _AGENT_CALL.get() is _AGENT_SENTINEL:
        # A marker is not an identity: it can only reuse a scope the person established, in-process on loopback.
        if os.environ.get("HERMES_DESKTOP") != "1" or not _loopback_peer_and_host(request):
            _fail("caller_identity_unavailable")
        with _DESKTOP_SCOPE_NONCE_LOCK:
            desktop_server_instance = _DESKTOP_SCOPE_NONCE
        if type(desktop_server_instance) is not str or len(desktop_server_instance) != 64:
            _fail("caller_identity_unavailable")
    else:
        desktop_server_instance = _desktop_loopback_request_is_authenticated(request)
    return _scope_from_projection(
        {
            "schema": _CALLER_SCOPE_SCHEMA,
            "kind": _DESKTOP_SCOPE_KIND,
            "provider": _DESKTOP_SCOPE_PROVIDER,
            "principal_id": _DESKTOP_SCOPE_PRINCIPAL,
            "server_instance": desktop_server_instance,
        },
        provider=_DESKTOP_SCOPE_PROVIDER,
        org_id="",
        user_id="",
    )


__all__ = [
    "ImageFilmCallerScopeError",
    "TrustedImageFilmCallerScope",
    "resolve_trusted_image_film_caller_scope",
]
