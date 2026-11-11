"""The one ComfyUI address every Continuo client reads (lane w39-p2-tester item 4).

Order: ``FILM_SUITE_COMFY_URL`` when set (the artist's own environment: it may name another host); otherwise this
machine only - ``127.0.0.1:8188`` (ComfyUI's default), then ``127.0.0.1:8000`` (the Windows desktop ComfyUI). The
first that answers ``GET /system_stats`` wins and is kept for the process; when neither answers the default is used
and asked again after ``RETRY_SECONDS``.

Trust rule: an origin is scheme + host + port only - no credentials, no path, no query, no fragment; redirects are
never followed. Without the environment variable no other host is ever reached.

Standalone (stdlib only, no package-relative imports): modules that are loaded by path import it by path.
"""
from __future__ import annotations

import ipaddress
import json
import os
import sys
import threading
import time
import types
import urllib.request
from urllib.parse import urlencode, urlsplit, urlunsplit

URL_ENV = "FILM_SUITE_COMFY_URL"
ENV = URL_ENV
NO_PROBE_ENV = "FILM_SUITE_COMFY_NO_PROBE"  # set by the test session (inherited by its children): never ask 8188 / 8000
DEFAULT_ORIGIN = "http://127.0.0.1:8188"
DESKTOP_ORIGIN = "http://127.0.0.1:8000"
LOCAL_CANDIDATES = (DEFAULT_ORIGIN, DESKTOP_ORIGIN)
PROBE_TIMEOUT_SECONDS = 0.6
RETRY_SECONDS = 10.0

# Modules loaded by path get their own copy of this file; the probe result lives in ONE process-wide holder so every
# copy agrees on the address. ``candidates`` is the local probe order (tests point it at a closed port).
_STATE = sys.modules.setdefault("film_suite_comfy_endpoint_state", types.ModuleType("film_suite_comfy_endpoint_state"))
if not hasattr(_STATE, "lock"):
    _STATE.lock = threading.Lock()
    _STATE.found = None  # an answering local origin, kept for the process
    _STATE.missed_at = None  # monotonic time of the last probe in which nothing answered
    _STATE.candidates = LOCAL_CANDIDATES
    _STATE.blocked = frozenset()  # origins never asked (the test session blocks the artist's 8188 and 8000)


def normalize(value: str) -> str:
    """``scheme://host:port`` or ValueError: never credentials, a path, a query or a fragment."""
    if not isinstance(value, str) or value != value.strip() or any(ord(c) < 33 for c in value):
        raise ValueError("Invalid ComfyUI address")
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid ComfyUI port") from exc
    if (parts.scheme not in ("http", "https", "ws", "wss") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.path not in ("", "/") or parts.query or parts.fragment
            or port is None or port == 0 or "\\" in parts.netloc):
        raise ValueError("Invalid ComfyUI address")
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    return urlunsplit((scheme, parts.netloc.lower(), "", "", ""))


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def reachable(origin: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    """Whether a ComfyUI answers ``GET <origin>/system_stats`` (a read; never queues work)."""
    try:
        if normalize(origin) in _STATE.blocked or (os.environ.get(NO_PROBE_ENV, "") and normalize(origin) in LOCAL_CANDIDATES):
            return False
        with _opener.open(normalize(origin) + "/system_stats", timeout=timeout) as response:  # noqa: S310
            data = json.loads(response.read(1024 * 1024))
            return isinstance(data, dict) and isinstance(data.get("system"), dict)
    except (OSError, ValueError, TypeError):
        return False


def configured_origin() -> str | None:
    """The environment's address, normalized; None when unset. An invalid value raises ValueError."""
    value = os.environ.get(URL_ENV, "")
    return normalize(value) if value else None


def comfy_origin() -> str:
    """The address every client uses now."""
    configured = configured_origin()
    if configured:
        return configured
    candidates = tuple(_STATE.candidates)
    with _STATE.lock:
        if _STATE.found:
            return _STATE.found
        if _STATE.missed_at is not None and time.monotonic() - _STATE.missed_at < RETRY_SECONDS:
            return candidates[0]
        for candidate in candidates:
            if reachable(candidate):
                _STATE.found = candidate
                return candidate
        _STATE.missed_at = time.monotonic()
        return candidates[0]


def current_label() -> str:
    """``host:port`` in use WITHOUT asking any engine (registration at import time must not reach the network)."""
    configured = configured_origin()
    return label_of(configured or _STATE.found or tuple(_STATE.candidates)[0])


def label_of(origin: str) -> str:
    return urlsplit(normalize(origin)).netloc


def comfy_label() -> str:
    """``host:port`` of the address in use (public text: never a path or credentials)."""
    return label_of(comfy_origin())


def comfy_ws_url(client_id: str) -> str:
    parts = urlsplit(comfy_origin())
    return urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, "/ws", urlencode({"clientId": client_id}), ""))


def is_engine_label(text: str) -> bool:
    """A provider ``endpoint_label`` the registry accepts: the configured address, or this machine on any port."""
    if not isinstance(text, str):
        return False
    try:
        if text == current_label():
            return True
    except ValueError:
        pass
    try:
        return is_loopback_host(urlsplit(normalize("http://" + text)).hostname)
    except ValueError:
        return False


def tried_labels() -> list[str]:
    """The addresses Continuo asks, in order (the configured one alone, else the two local ports)."""
    configured = configured_origin()
    return [label_of(configured)] if configured else [label_of(origin) for origin in _STATE.candidates]


def unreachable_reason() -> str:
    """One fact: which address(es) did not answer."""
    return "No ComfyUI answers at " + " or ".join(tried_labels())


def engine_state(*, answered: bool | None = None) -> dict[str, object]:
    """``{reachable, address, tried, reason}``; ``answered`` = a probe the caller already made (skips asking again)."""
    try:
        origin = comfy_origin()
        ok = reachable(origin) if answered is None else bool(answered)
        address = label_of(origin)
        tried = tried_labels()
    except ValueError:
        return {"reachable": False, "address": None, "tried": [], "reason": f"{ENV} is not a valid address"}
    return {"reachable": ok, "address": address, "tried": tried, "reason": None if ok else unreachable_reason()}


def block(origins) -> None:
    """Never ask these origins (test sessions: the shared engine at 8188 / 8000 stays untouched)."""
    _STATE.blocked = frozenset(normalize(origin) for origin in origins)


def forget() -> None:
    """Drop the kept probe result (the next client asks the local ports again)."""
    with _STATE.lock:
        _STATE.found, _STATE.missed_at = None, None


__all__ = [
    "DEFAULT_ORIGIN", "DESKTOP_ORIGIN", "ENV", "LOCAL_CANDIDATES", "comfy_label", "comfy_origin", "comfy_ws_url",
    "block", "configured_origin", "current_label", "engine_state", "forget", "is_engine_label", "is_loopback_host", "label_of", "normalize", "reachable",
    "tried_labels", "unreachable_reason",
]
