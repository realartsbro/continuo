"""Loopback-only ComfyUI REST client.

Bounded, read-mostly client for the local ComfyUI service.  The v1 endpoint
is the one configured address (``comfy_endpoint.comfy_origin``: FILM_SUITE_COMFY_URL,
else this machine on port 8188, then 8000); an explicit origin that is not that
address, userinfo, a path or a redirect is rejected.

All blocking stdlib HTTP, hashing, and file work runs through
``asyncio.to_thread`` so the FastAPI event loop stays responsive.
``submit`` exists for the generation path but is never called by static
health methods.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from .bounded_download import DownloadError, ExclusiveSink

from .comfy_endpoint import DEFAULT_ORIGIN, comfy_origin  # noqa: F401 - DEFAULT_ORIGIN stays exported
CONNECT_TIMEOUT_SECONDS = 2.0
READ_TIMEOUT_SECONDS = 15.0
MAX_JSON_BYTES = 16 * 1024 * 1024
VIEW_MAX_BYTES = 2 * 1024 * 1024 * 1024
VIEW_MIN_BYTES = 65536
STREAM_MAX_BYTES = 1 << 30


class ComfyClientError(Exception):
    """Stable client-side error with a normalized reason code."""

    reason_code = "unknown"

    def __init__(self, message: str, *, reason_code: str = "unknown", http_status: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.reason_code = reason_code
        # The HTTP status ComfyUI answered with, when it answered at all. A
        # 404 from /view is proof the output is gone; "unreachable" is not.
        self.http_status = http_status


@dataclass(frozen=True)
class ViewStreamResult:
    sha256: str
    bytes_written: int


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise ComfyClientError(
            f"ComfyUI redirect to {newurl!r} is not allowed",
            reason_code="unreachable",
        )


class _BoundedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection with a separate 2s connect bound and 15s read bound."""

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self.host, self.port),
            CONNECT_TIMEOUT_SECONDS,
            self.source_address,
        )
        self.sock.settimeout(READ_TIMEOUT_SECONDS)


class _BoundedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> Any:
        return self.do_open(_BoundedHTTPConnection, req)


_OPENER = urllib.request.build_opener(_NoRedirectHandler, _BoundedHTTPHandler)


def urlopen(request: urllib.request.Request, *, timeout: float | None = None) -> Any:
    """Open a request through the no-redirect opener (mockable seam)."""
    return _OPENER.open(request, timeout=timeout)


class ComfyUIClient:
    """Bounded REST client for the fixed local ComfyUI origin."""

    def __init__(self, origin: str | None = None) -> None:
        resolved = comfy_origin()
        if origin is not None and origin != resolved:
            raise ValueError(f"ComfyUI origin must be the configured address {resolved}")
        self.origin = resolved

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: Mapping[str, str] | None = None,
        stream: bool = False,
    ) -> Any:
        return await asyncio.to_thread(
            self._blocking_request,
            method,
            path,
            body=body,
            query=query,
            stream=stream,
        )

    def _blocking_request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: Mapping[str, str] | None = None,
        stream: bool = False,
    ) -> Any:
        url = self.origin + path
        if query:
            encoded = urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
            url = f"{url}?{encoded}"
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=READ_TIMEOUT_SECONDS) as response:
                if stream:
                    return self._read_stream(response)
                return self._read_json(response)
        except ComfyClientError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise ComfyClientError(
                    f"ComfyUI redirect (HTTP {exc.code}) is not allowed",
                    reason_code="unreachable",
                ) from exc
            raise ComfyClientError(
                f"ComfyUI HTTP {exc.code}",
                reason_code="unreachable",
                http_status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise ComfyClientError(
                f"ComfyUI unreachable: {exc.reason}",
                reason_code="unreachable",
            ) from exc
        except http.client.HTTPException as exc:
            raise ComfyClientError(
                f"ComfyUI protocol error: {exc}",
                reason_code="unreachable",
            ) from exc
        except (TimeoutError, OSError) as exc:
            raise ComfyClientError(
                f"ComfyUI unreachable: {exc}",
                reason_code="unreachable",
            ) from exc
        except ValueError as exc:
            raise ComfyClientError(
                f"ComfyUI invalid response: {exc}",
                reason_code="unreachable",
            ) from exc

    @staticmethod
    def _content_length(response: Any) -> int | None:
        raw = response.getheader("Content-Length")
        if raw is None or not raw.strip():
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    @staticmethod
    def _check_truncated(response: Any, declared: int | None, total: int) -> None:
        if declared is not None and total < declared:
            response.close()
            raise ComfyClientError(
                f"ComfyUI response truncated: expected {declared} bytes, got {total}",
                reason_code="unreachable",
            )

    def _read_json(self, response: Any) -> Any:
        declared = self._content_length(response)
        if declared is not None and declared > MAX_JSON_BYTES:
            response.close()
            raise ComfyClientError(
                f"ComfyUI JSON response exceeds {MAX_JSON_BYTES} bytes",
                reason_code="unreachable",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_JSON_BYTES:
                response.close()
                raise ComfyClientError(
                    f"ComfyUI JSON response exceeds {MAX_JSON_BYTES} bytes",
                    reason_code="unreachable",
                )
            if declared is not None and total > declared:
                response.close()
                raise ComfyClientError(
                    "ComfyUI Content-Length is false (body exceeds declared length)",
                    reason_code="unreachable",
                )
            chunks.append(chunk)
        self._check_truncated(response, declared, total)
        raw = b"".join(chunks)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ComfyClientError(
                f"ComfyUI returned malformed JSON: {exc}",
                reason_code="unreachable",
            ) from exc

    def _read_stream(self, response: Any) -> bytes:
        declared = self._content_length(response)
        if declared is not None and declared > VIEW_MAX_BYTES:
            response.close()
            raise ComfyClientError(
                f"ComfyUI stream exceeds {VIEW_MAX_BYTES} bytes",
                reason_code="unreachable",
            )
        chunks: list[bytes] = []
        total = 0
        try:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > VIEW_MAX_BYTES:
                    raise ComfyClientError(
                        f"ComfyUI stream exceeds {VIEW_MAX_BYTES} bytes",
                        reason_code="unreachable",
                    )
                if declared is not None and total > declared:
                    raise ComfyClientError(
                        "ComfyUI Content-Length is false (body exceeds declared length)",
                        reason_code="unreachable",
                    )
                chunks.append(chunk)
            self._check_truncated(response, declared, total)
        finally:
            response.close()
        return b"".join(chunks)

    @staticmethod
    def _strict_content_length(response: Any) -> int | None:
        raw = response.getheader("Content-Length")
        if raw is None or raw == "":
            return None
        if not isinstance(raw, str) or not raw.strip() or not raw.strip().isdigit():
            raise ComfyClientError("ComfyUI Content-Length is invalid", reason_code="unreachable")
        value = int(raw.strip())
        if value < 0:
            raise ComfyClientError("ComfyUI Content-Length is invalid", reason_code="unreachable")
        return value

    def _blocking_stream_view_to_sink(
        self,
        output_ref: Mapping[str, str],
        *,
        sink: ExclusiveSink,
        min_bytes: int,
        max_bytes: int,
    ) -> ViewStreamResult:
        if (
            type(min_bytes) is not int
            or type(max_bytes) is not int
            or min_bytes < VIEW_MIN_BYTES
            or max_bytes > STREAM_MAX_BYTES
            or max_bytes < min_bytes
        ):
            raise ComfyClientError("ComfyUI stream bounds are invalid", reason_code="invalid_request")
        query = {str(key): str(value) for key, value in output_ref.items()}
        encoded = urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        request = urllib.request.Request(f"{self.origin}/view?{encoded}", method="GET")
        response: Any = None
        try:
            response = urlopen(request, timeout=READ_TIMEOUT_SECONDS)
            declared = self._strict_content_length(response)
            if declared is not None and (declared < min_bytes or declared > max_bytes):
                raise ComfyClientError("ComfyUI declared output size is outside bounds", reason_code="unreachable")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise ComfyClientError("ComfyUI returned a non-byte stream chunk", reason_code="unreachable")
                chunk_bytes = bytes(chunk)
                if total + len(chunk_bytes) > max_bytes or (declared is not None and total + len(chunk_bytes) > declared):
                    raise ComfyClientError("ComfyUI output exceeds its declared bound", reason_code="unreachable")
                sink.write(chunk_bytes)
                digest.update(chunk_bytes)
                total += len(chunk_bytes)
            if declared is not None and total != declared:
                raise ComfyClientError("ComfyUI response is truncated", reason_code="unreachable")
            if total < min_bytes:
                raise ComfyClientError("ComfyUI output is smaller than the minimum", reason_code="unreachable")
            try:
                sink.flush_and_close()
            except DownloadError as exc:
                raise ComfyClientError("ComfyUI output could not be flushed", reason_code="unreachable") from exc
            return ViewStreamResult(digest.hexdigest(), total)
        except ComfyClientError:
            raise
        except urllib.error.HTTPError as exc:
            raise ComfyClientError(f"ComfyUI HTTP {exc.code}", reason_code="unreachable") from exc
        except urllib.error.URLError as exc:
            raise ComfyClientError("ComfyUI output is unreachable", reason_code="unreachable") from exc
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            raise ComfyClientError("ComfyUI output is unreachable", reason_code="unreachable") from exc
        except Exception as exc:
            raise ComfyClientError("ComfyUI output is unreachable", reason_code="unreachable") from exc
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass

    async def stream_view_to_sink(
        self,
        output_ref: Mapping[str, str],
        *,
        sink: ExclusiveSink,
        min_bytes: int = 65536,
        max_bytes: int = 1 << 30,
    ) -> ViewStreamResult:
        """Stream one fixed-origin ``/view`` response into an exclusive sink."""
        if (
            type(min_bytes) is not int
            or type(max_bytes) is not int
            or min_bytes < VIEW_MIN_BYTES
            or max_bytes > STREAM_MAX_BYTES
            or max_bytes < min_bytes
        ):
            sink.request_abort()
            sink.worker_finished()
            sink.abort_owned()
            raise ComfyClientError("ComfyUI stream bounds are invalid", reason_code="invalid_request")
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._blocking_stream_view_to_sink,
                output_ref,
                sink=sink,
                min_bytes=min_bytes,
                max_bytes=max_bytes,
            )
        )
        try:
            result = await worker
        except asyncio.CancelledError:
            sink.request_abort()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                await worker
            except BaseException:
                pass
            sink.worker_finished()
            sink.abort_owned()
            raise
        except BaseException:
            sink.worker_finished()
            sink.abort_owned()
            raise
        sink.worker_finished()
        return result

    async def system_stats(self) -> dict[str, Any]:
        return await self._request("GET", "/system_stats")

    async def features(self) -> dict[str, Any]:
        return await self._request("GET", "/features")

    async def object_info(self, class_type: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(class_type, safe="")
        return await self._request("GET", f"/object_info/{encoded}")

    async def models(self, folder: str) -> dict[str, Any]:
        encoded = urllib.parse.quote(folder, safe="")
        return await self._request("GET", f"/models/{encoded}")

    async def queue(self) -> dict[str, Any]:
        return await self._request("GET", "/queue")

    @staticmethod
    def _owned_prompt_id(prompt_id: str) -> str:
        if not isinstance(prompt_id, str) or not prompt_id.strip():
            raise ValueError("A provider prompt id is required")
        return prompt_id.strip()

    async def cancel_prompt(self, prompt_id: str) -> dict[str, Any]:
        owned_id = self._owned_prompt_id(prompt_id)
        encoded_id = urllib.parse.quote(owned_id, safe="")
        return await self._request("POST", f"/api/jobs/{encoded_id}/cancel")

    async def interrupt_prompt(self, prompt_id: str) -> dict[str, Any]:
        """Stop one owned prompt on a ComfyUI without ``/api/jobs/{id}/cancel``.

        Removes it from the pending queue and interrupts it only if it is the
        one running (``/interrupt`` with a prompt id never touches another
        prompt).  Both answer an empty 200, so the body is read as bytes.
        """
        owned_id = self._owned_prompt_id(prompt_id)
        await self._request("POST", "/queue", body={"delete": [owned_id]}, stream=True)
        await self._request("POST", "/interrupt", body={"prompt_id": owned_id}, stream=True)
        return {"cancelled": True}

    async def submit(self, prompt: dict, *, client_id: str | None = None, extra_data: dict | None = None) -> dict:
        body: dict[str, Any] = {"prompt": prompt}
        if client_id is not None:
            body["client_id"] = client_id
        if extra_data is not None:
            body["extra_data"] = extra_data
        return await self._request("POST", "/prompt", body=body)

    async def recent_history(self, max_items: int = 100) -> dict[str, Any]:
        return await self._request(
            "GET", "/history", query={"max_items": str(int(max_items))}
        )

    async def history(self, prompt_id: str) -> dict[str, Any]:
        owned_id = self._owned_prompt_id(prompt_id)
        encoded = urllib.parse.quote(owned_id, safe="")
        return await self._request("GET", f"/history/{encoded}")

    async def view(self, output_ref: Mapping[str, str]) -> bytes:
        query = {str(key): str(value) for key, value in output_ref.items()}
        return await self._request("GET", "/view", query=query, stream=True)


__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "ComfyClientError",
    "ComfyUIClient",
    "DEFAULT_ORIGIN",
    "MAX_JSON_BYTES",
    "READ_TIMEOUT_SECONDS",
    "VIEW_MAX_BYTES",
    "ViewStreamResult",
]
