"""Private, authenticated operation records for Contract-4 generation.

This module is intentionally independent of ``JobStore`` and provider clients.
It owns only profile-private metadata, reservations, and the caller's one
frozen destination.  All public operation objects are opaque handles; paths,
keys, MACs, and locks remain coordinator-private.
"""

from __future__ import annotations
from .legacy_plugin_migration import state_root


import contextlib
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from hermes_constants import get_hermes_home
except ImportError:  # pragma: no cover - repository tests install Hermes' root
    def get_hermes_home() -> Path:
        return Path.home() / "AppData" / "Local" / "hermes"

# ``bounded_download`` lives in the generation package, which the synthetic
# loader registers before ``studio_core``; import it through that documented
# synthetic name to keep the package boundary acyclic (studio_core never
# imports the generation *package object*, only this leaf pair of value types).
from film_suite_generation.bounded_download import DownloadDigest, DownloadError, ExclusiveSink
from . import path_boundary as _path_boundary
from .path_boundary import (
    NamedProjectLock,
    ProjectError,
    _fsync_parent,
    ensure_private_node,
    profile_identity_key,
)


PRIVATE_RECORD_SCHEMA = "studio.private-operation/1"
RESERVATION_SCHEMA = "studio.reservation/1"
DELIVERY_SCHEMA = "studio.delivery/1"
MAX_CANDIDATES = 50
MAX_RECORD_BYTES = 192 * 1024
MAX_MARKER_BYTES = 4096
MAX_SWEEP_CANDIDATES = 4096
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_PROJECT = re.compile(r"^prj_[0-9a-f]{32}$")

_RECORD_FIELDS = frozenset(
    {
        "schema",
        "created_at",
        "updated_at",
        "studio_job_id",
        "client_request_id",
        "provider_id",
        "target_id",
        "project_id",
        "request_hash",
        "destination",
        "destination_identity",
        "requested_basename",
        "phase",
        "published_identity",
        "output_sha256",
        "output_bytes",
        "completed_at",
        "terminal_status",
        "mac",
    }
)
_RESERVATION_FIELDS = frozenset(
    {
        "schema",
        "created_at",
        "updated_at",
        "client_request_id",
        "studio_job_id",
        "destination_identity",
        "record_name",
        "phase",
        "mac",
    }
)
_DELIVERY_FIELDS = frozenset(
    {
        "schema",
        "created_at",
        "updated_at",
        "phase",
        "provider_id",
        "destination_identity",
        "partial_name",
        "partial_identity",
        "output_sha256",
        "output_bytes",
        "published_identity",
        "mac",
    }
)


class PrivateOperationError(ValueError):
    """Finite private-state failure; no caller-controlled details are exposed."""

    def __init__(self, message: str, *, code: str = "private_state_unavailable") -> None:
        self.code = code
        self.reason_code = code
        super().__init__(message)


def _flush_parent(path: Path) -> None:
    """Flush one already-confined metadata/output parent."""
    try:
        _fsync_parent(path)
    except (OSError, ProjectError) as exc:
        raise PrivateOperationError("private state durability failed") from exc


class _Opaque:
    __slots__ = ()

    def __setattr__(self, name: str, value: Any) -> None:
        if hasattr(self, name):
            raise AttributeError(f"{type(self).__name__} is immutable")
        object.__setattr__(self, name, value)


class _OperationState:
    __slots__ = (
        "owner",
        "record_path",
        "reservation_path",
        "destination",
        "destination_identity",
        "studio_job_id",
        "client_request_id",
        "provider_id",
        "target_id",
        "project_id",
        "requested_basename",
        "request_hash",
        "phase",
        "lock",
        "delivery_lock",
        "consumed",
        "mutex",
        "marker_path",
        "partial_path",
        "partial_identity",
        "sink",
    )

    def __init__(self, **values: Any) -> None:
        self.mutex = threading.RLock()
        for key, value in values.items():
            setattr(self, key, value)


class PreparedOperation(_Opaque):
    __slots__ = ("_state", "destination_identity")

    def __init__(self, state: _OperationState) -> None:
        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "destination_identity", state.destination_identity)

    @property
    def phase(self) -> str:
        return self._state.phase

    @property
    def _record_path(self) -> Path:
        """Coordinator-private record path; used by tests, never public output."""
        return self._state.record_path


class OperationHandle(_Opaque):
    __slots__ = ("_state", "studio_job_id", "destination_identity")

    def __init__(self, state: _OperationState) -> None:
        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "studio_job_id", state.studio_job_id)
        object.__setattr__(self, "destination_identity", state.destination_identity)

    @property
    def phase(self) -> str:
        return self._state.phase


class DeliveryHandle(_Opaque):
    __slots__ = ("_state", "destination_identity")

    def __init__(self, state: _OperationState) -> None:
        object.__setattr__(self, "_state", state)
        object.__setattr__(self, "destination_identity", state.destination_identity)

    @property
    def phase(self) -> str:
        return self._state.phase


@dataclass(frozen=True)
class PublishedOutput:
    published_identity: str
    output_sha256: str
    output_bytes: int



def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _finite_text(value: Any, field: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    if type(value) is not str or not value or value != value.strip() or len(value) > 256:
        raise PrivateOperationError(f"{field} is invalid", code="invalid_request")
    if "\x00" in value:
        raise PrivateOperationError(f"{field} is invalid", code="invalid_request")
    return value


def _request_hash(value: Any) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):
        raise PrivateOperationError("request hash is invalid", code="invalid_request")
    return value


def _project(value: Any) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not _PROJECT.fullmatch(value):
        raise PrivateOperationError("project scope is invalid", code="invalid_request")
    return value


def _safe_basename(value: Any) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PrivateOperationError("requested basename is invalid", code="invalid_request")
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise PrivateOperationError("requested basename is invalid", code="invalid_request")
    if len(value) > 255 or not value.lower().endswith(".mp4"):
        raise PrivateOperationError("requested basename is invalid", code="invalid_request")
    return value


def _canonical(path: os.PathLike[str] | str, *, directory: bool = False) -> Path:
    if isinstance(path, bytes):
        raise PrivateOperationError("path is invalid", code="invalid_request")
    try:
        value = os.fspath(path)
    except TypeError as exc:
        raise PrivateOperationError("path is invalid", code="invalid_request") from exc
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PrivateOperationError("path is invalid", code="invalid_request")
    candidate = Path(value)
    if not candidate.is_absolute() or value.startswith(("\\\\", "//")):
        raise PrivateOperationError("path is invalid", code="invalid_request")
    if not candidate.exists() or (directory and not candidate.is_dir()) or (not directory and candidate.is_dir()):
        raise PrivateOperationError("path is unavailable", code="private_state_unavailable")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PrivateOperationError("path identity is unavailable") from exc
    if os.path.normcase(str(resolved)) != os.path.normcase(str(candidate.absolute())):
        raise PrivateOperationError("path reparse boundary is invalid", code="invalid_request")
    if candidate.is_symlink():
        raise PrivateOperationError("path reparse boundary is invalid", code="invalid_request")
    return candidate.absolute()


def _file_identity(path: Path) -> str:
    try:
        info = path.stat()
    except OSError as exc:
        raise PrivateOperationError("destination identity is unavailable") from exc
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise PrivateOperationError("destination identity is invalid", code="output_invalid")
    raw = f"{os.path.normcase(str(path))}\0{info.st_dev}\0{info.st_ino}\0{info.st_size}".encode("utf-8", "surrogatepass")
    return hashlib.sha256(raw).hexdigest()


def _object_token(identity: Any) -> str:
    """Bind a marker to volume/file identity, not a mutable pathname or size."""
    raw = f"{int(identity.volume_serial)}\0{int(identity.file_id)}\0{int(identity.is_directory)}".encode(
        "ascii"
    )
    return hashlib.sha256(raw).hexdigest()


def _partial_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PrivateOperationError("partial identity is unavailable", code="output_invalid") from exc
    return True


def _partial_token(path: Path) -> str:
    try:
        handle, identity = _path_boundary._open_existing(path, directory=False)
    except (OSError, ProjectError, ValueError) as exc:
        raise PrivateOperationError("partial identity is unavailable", code="output_invalid") from exc
    try:
        if identity.is_directory:
            raise PrivateOperationError("partial identity is invalid", code="output_invalid")
        return _object_token(identity)
    finally:
        handle.close()


class PrivateOperations:
    """Profile-private record/reservation and local delivery coordinator."""

    def __init__(
        self,
        hermes_home: os.PathLike[str] | str | None = None,
        *,
        identity_key: bytes | None = None,
        private_root: os.PathLike[str] | str | None = None,
    ) -> None:
        if identity_key is None:
            identity_key = profile_identity_key(hermes_home)
        if type(identity_key) is not bytes or len(identity_key) != 32:
            raise PrivateOperationError("profile identity key is unavailable")
        if private_root is None:
            if hermes_home is None:
                hermes_home = get_hermes_home()
            private_root = state_root(Path(hermes_home) / "plugin-data") / "v4-private"
        root = Path(private_root)
        if not root.is_absolute():
            raise PrivateOperationError("private state root is invalid")
        self._key = identity_key
        self._root = root
        self._records = root / "records"
        self._reservations = root / "reservations"
        self._markers = root / "markers"
        try:
            for path in (root, self._records, self._reservations, self._markers):
                ensure_private_node(path, directory=True)
            self._admission_lock_path = root / ".admission.lock"
        except (OSError, ProjectError, ValueError) as exc:
            raise PrivateOperationError("private state is unavailable") from exc

    @property
    def private_root(self) -> Path:
        return self._root

    def destination_identity(self, path: os.PathLike[str] | str) -> str:
        candidate = Path(path)
        # For a not-yet-created output, identity is bound to its existing safe
        # parent plus basename, never to a subsequently resolved object.
        if candidate.exists():
            candidate = _canonical(candidate)
            return self._mac_hex(b"destination\0" + str(candidate).encode())
        if not candidate.is_absolute() or not candidate.name or not candidate.parent.is_dir():
            raise PrivateOperationError("destination is unavailable", code="invalid_request")
        parent = _canonical(candidate.parent, directory=True)
        if candidate.is_symlink():
            raise PrivateOperationError("destination reparse boundary is invalid", code="invalid_request")
        return self._mac_hex(b"destination\0" + str(parent / candidate.name).encode())

    def _mac_hex(self, payload: bytes) -> str:
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def _record_name(self, client_request_id: str) -> str:
        return self._mac_hex(b"record\0" + client_request_id.encode("utf-8")) + ".json"

    def _reservation_name(self, destination_identity: str) -> str:
        return destination_identity + ".json"

    def _marker_name(self, destination_identity: str) -> str:
        return destination_identity + ".json"

    def _mac_payload(self, payload: dict[str, Any]) -> bytes:
        unsigned = {key: value for key, value in payload.items() if key != "mac"}
        return json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _signed(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = dict(payload)
        result["mac"] = self._mac_hex(self._mac_payload(result))
        return result

    def _encode_record(self, payload: dict[str, Any]) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"

    def _validate_payload(self, payload: dict[str, Any], *, schema: str) -> None:
        if not isinstance(payload, dict):
            raise PrivateOperationError("private record schema is invalid")
        fields = {
            PRIVATE_RECORD_SCHEMA: _RECORD_FIELDS,
            RESERVATION_SCHEMA: _RESERVATION_FIELDS,
            DELIVERY_SCHEMA: _DELIVERY_FIELDS,
        }.get(schema)
        if fields is None or payload.get("schema") != schema or set(payload) - fields:
            raise PrivateOperationError("private record schema is invalid")
        if type(payload.get("mac")) is not str or not _HEX.fullmatch(payload["mac"]):
            raise PrivateOperationError("private record authentication failed")
        for name in ("created_at", "updated_at"):
            if type(payload.get(name)) is not str or not payload[name] or len(payload[name]) > 64:
                raise PrivateOperationError("private record schema is invalid")
        phase = payload.get("phase")
        if type(phase) is not str or phase not in {"prepared", "ledgered", "submit_started", "provider_bound", "polling", "downloaded", "published", "ready", "aborted", "terminal"}:
            raise PrivateOperationError("private record schema is invalid")
        if schema == PRIVATE_RECORD_SCHEMA:
            for field in ("studio_job_id", "client_request_id", "provider_id", "target_id"):
                _finite_text(payload.get(field), field)
            _project(payload.get("project_id"))
            _request_hash(payload.get("request_hash"))
            _safe_basename(payload.get("requested_basename"))
            if type(payload.get("destination")) is not str or not Path(payload["destination"]).is_absolute():
                raise PrivateOperationError("private record schema is invalid")
            if type(payload.get("destination_identity")) is not str or not _HEX.fullmatch(payload["destination_identity"]):
                raise PrivateOperationError("private record schema is invalid")
            for field in ("published_identity", "output_sha256"):
                value = payload.get(field)
                if value is not None and (type(value) is not str or not _HEX.fullmatch(value)):
                    raise PrivateOperationError("private record schema is invalid")
            output_bytes = payload.get("output_bytes")
            if output_bytes is not None and (type(output_bytes) is not int or output_bytes < 0):
                raise PrivateOperationError("private record schema is invalid")
            completed_at = payload.get("completed_at")
            if completed_at is not None and (type(completed_at) is not str or len(completed_at) > 64):
                raise PrivateOperationError("private record schema is invalid")
        elif schema == RESERVATION_SCHEMA:
            _finite_text(payload.get("studio_job_id"), "studio_job_id")
            _finite_text(payload.get("client_request_id"), "client_request_id")
            if type(payload.get("record_name")) is not str or not _HEX.fullmatch(Path(payload["record_name"]).stem) or Path(payload["record_name"]).suffix != ".json":
                raise PrivateOperationError("private record schema is invalid")
            if type(payload.get("destination_identity")) is not str or not _HEX.fullmatch(payload["destination_identity"]):
                raise PrivateOperationError("private record schema is invalid")
        else:
            _finite_text(payload.get("provider_id"), "provider_id")
            if type(payload.get("destination_identity")) is not str or not _HEX.fullmatch(payload["destination_identity"]):
                raise PrivateOperationError("private record schema is invalid")
            partial_name = payload.get("partial_name")
            if type(partial_name) is not str or not partial_name or "/" in partial_name or "\\" in partial_name or len(partial_name) > 255:
                raise PrivateOperationError("private record schema is invalid")
            partial_identity = payload.get("partial_identity")
            if partial_identity is not None and (type(partial_identity) is not str or not _HEX.fullmatch(partial_identity)):
                raise PrivateOperationError("private record schema is invalid")
            output_bytes = payload.get("output_bytes")
            if output_bytes is not None and (type(output_bytes) is not int or output_bytes < 0):
                raise PrivateOperationError("private record schema is invalid")
            for field in ("output_sha256",):
                value = payload.get(field)
                if value is not None and (type(value) is not str or not _HEX.fullmatch(value)):
                    raise PrivateOperationError("private record schema is invalid")

    def _write_new(self, path: Path, payload: dict[str, Any], *, limit: int = MAX_RECORD_BYTES) -> None:
        payload = self._signed(payload)
        self._validate_payload(payload, schema=str(payload.get("schema")))
        encoded = self._encode_record(payload).encode("utf-8")
        if len(encoded) > limit:
            raise PrivateOperationError("private record exceeds its budget")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(path, 0o600)
            _flush_parent(path.parent)
        except FileExistsError as exc:
            raise PrivateOperationError("private record already exists") from exc
        except OSError as exc:
            raise PrivateOperationError("private record could not be written") from exc

    def _replace_signed(self, path: Path, payload: dict[str, Any], *, limit: int = MAX_RECORD_BYTES) -> None:
        if path.is_symlink() or not path.is_file():
            raise PrivateOperationError("private record is unavailable")
        payload = self._signed(payload)
        self._validate_payload(payload, schema=str(payload.get("schema")))
        encoded = self._encode_record(payload).encode("utf-8")
        if len(encoded) > limit:
            raise PrivateOperationError("private record exceeds its budget")
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            if os.name != "nt":
                os.chmod(path, 0o600)
            _flush_parent(path.parent)
        except OSError as exc:
            raise PrivateOperationError("private record could not be updated") from exc
        finally:
            temp.unlink(missing_ok=True)

    def _read_record(self, path: Path, *, schema: str = PRIVATE_RECORD_SCHEMA, limit: int = MAX_RECORD_BYTES) -> dict[str, Any]:
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > limit:
                raise OSError
            raw = path.read_bytes()
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PrivateOperationError("private record is unavailable") from exc
        self._validate_payload(payload, schema=schema)
        mac = payload.get("mac")
        if type(mac) is not str or not hmac.compare_digest(mac, self._mac_hex(self._mac_payload(payload))):
            raise PrivateOperationError("private record authentication failed")
        return payload

    def _candidate_path(self, directory: Path, basename: str, index: int) -> Path:
        if index == 0:
            return directory / basename
        stem, suffix = os.path.splitext(basename)
        return directory / f"{stem}-{index}{suffix}"

    def _record_fields_match(self, payload: dict[str, Any], **expected: Any) -> bool:
        return all(payload.get(key) == value for key, value in expected.items())

    def prepare(
        self,
        *,
        studio_job_id: str,
        client_request_id: str,
        provider_id: str,
        target_id: str,
        project_id: str | None,
        destination: os.PathLike[str] | str,
        requested_basename: str,
        request_hash: str,
    ) -> PreparedOperation:
        ids = {
            "studio_job_id": _finite_text(studio_job_id, "studio_job_id"),
            "client_request_id": _finite_text(client_request_id, "client_request_id"),
            "provider_id": _finite_text(provider_id, "provider_id"),
            "target_id": _finite_text(target_id, "target_id"),
        }
        project_id = _project(project_id)
        basename = _safe_basename(requested_basename)
        request_hash = _request_hash(request_hash)
        directory = _canonical(destination, directory=True)
        lock = NamedProjectLock(
            "Local\\HermesFilmSuite-V4Admission-" + self._mac_hex(str(directory).encode()),
            lock_file=self._admission_lock_path,
        )
        try:
            lock.acquire()
            record_path = self._records / self._record_name(str(client_request_id))
            if record_path.exists():
                existing = self._read_record(record_path)
                expected_parent = _canonical(Path(existing["destination"]).parent, directory=True)
                if not self._record_fields_match(
                    existing,
                    studio_job_id=studio_job_id,
                    client_request_id=client_request_id,
                    provider_id=provider_id,
                    target_id=target_id,
                    project_id=project_id,
                    requested_basename=basename,
                    request_hash=request_hash,
                ) or expected_parent != directory:
                    raise PrivateOperationError("client request identity conflicts", code="invalid_request")
                state = _OperationState(
                    owner=self,
                    record_path=record_path,
                    reservation_path=self._reservations / self._reservation_name(existing["destination_identity"]),
                    destination=Path(existing["destination"]),
                    destination_identity=existing["destination_identity"],
                    phase=existing["phase"],
                    lock=lock,
                    consumed=False,
                    **ids,
                    project_id=project_id,
                    requested_basename=basename,
                    request_hash=request_hash,
                )
                return PreparedOperation(state)
            chosen: Path | None = None
            chosen_identity: str | None = None
            for index in range(MAX_CANDIDATES):
                candidate = self._candidate_path(directory, basename, index)
                if candidate.exists() or candidate.is_symlink():
                    continue
                identity = self.destination_identity(candidate)
                reservation = self._reservations / self._reservation_name(identity)
                if reservation.exists():
                    continue
                chosen, chosen_identity = candidate, identity
                break
            if chosen is None or chosen_identity is None:
                raise PrivateOperationError("no collision-free destination is available", code="output_invalid")
            now = _now()
            reservation_path = self._reservations / self._reservation_name(chosen_identity)
            reservation = {
                "schema": RESERVATION_SCHEMA,
                "created_at": now,
                "updated_at": now,
                "client_request_id": client_request_id,
                "studio_job_id": studio_job_id,
                "destination_identity": chosen_identity,
                "record_name": record_path.name,
                "phase": "prepared",
            }
            self._write_new(reservation_path, reservation, limit=MAX_MARKER_BYTES)
            record = {
                "schema": PRIVATE_RECORD_SCHEMA,
                "created_at": now,
                "updated_at": now,
                "studio_job_id": studio_job_id,
                "client_request_id": client_request_id,
                "provider_id": provider_id,
                "target_id": target_id,
                "project_id": project_id,
                "request_hash": request_hash,
                "destination": str(chosen),
                "destination_identity": chosen_identity,
                "requested_basename": basename,
                "phase": "prepared",
            }
            try:
                self._write_new(record_path, record)
            except Exception:
                reservation_path.unlink(missing_ok=True)
                raise
            state = _OperationState(
                owner=self,
                record_path=record_path,
                reservation_path=reservation_path,
                destination=chosen,
                destination_identity=chosen_identity,
                phase="prepared",
                lock=lock,
                consumed=False,
                **ids,
                project_id=project_id,
                requested_basename=basename,
                request_hash=request_hash,
            )
            return PreparedOperation(state)
        except Exception:
            lock.close()
            raise

    def _assert_prepared(self, op: PreparedOperation) -> _OperationState:
        if not isinstance(op, PreparedOperation) or op._state.owner is not self:
            raise PrivateOperationError("private operation handle is invalid")
        state = op._state
        with state.mutex:
            if state.consumed or state.phase != "prepared":
                raise PrivateOperationError("private operation phase is invalid")
        return state

    def abort_prepared(self, op: PreparedOperation) -> None:
        if not isinstance(op, PreparedOperation) or op._state.owner is not self:
            return
        state = op._state
        with state.mutex:
            if state.consumed:
                return
            if state.phase == "prepared":
                deleted_parent: Path | None = None
                for path in (state.reservation_path, state.record_path, getattr(state, "marker_path", None), getattr(state, "partial_path", None)):
                    if path is None or path.is_symlink() or not path.exists():
                        continue
                    path.unlink(missing_ok=True)
                    deleted_parent = path.parent
                if deleted_parent is not None:
                    _flush_parent(deleted_parent)
                state.phase = "aborted"
            state.consumed = True
            state.lock.close()

    def mark_ledgered(self, op: PreparedOperation, row: Any) -> OperationHandle:
        state = self._assert_prepared(op)
        expected = {
            "studio_job_id": state.studio_job_id,
            "client_request_id": state.client_request_id,
            "provider_id": state.provider_id,
            "target_id": state.target_id,
            "project_id": state.project_id,
            "destination_identity": state.destination_identity,
            "requested_basename": state.requested_basename,
            "request_hash": state.request_hash,
        }
        with state.mutex:
            for key, value in expected.items():
                if getattr(row, key, None) != value:
                    raise PrivateOperationError("ledger ownership does not match reservation", code="invalid_request")
            payload = self._read_record(state.record_path)
            if payload.get("phase") != "prepared" or not self._record_fields_match(payload, **expected):
                raise PrivateOperationError("private record CAS failed")
            payload["phase"] = "ledgered"
            payload["updated_at"] = _now()
            self._replace_signed(state.record_path, payload)
            state.reservation_path.unlink(missing_ok=True)
            _flush_parent(state.reservation_path.parent)
            state.phase = "ledgered"
            state.consumed = True
            state.lock.close()
            return OperationHandle(state)

    def _state_for_handle(self, op: OperationHandle) -> _OperationState:
        if not isinstance(op, OperationHandle) or op._state.owner is not self:
            raise PrivateOperationError("private operation handle is invalid")
        state = op._state
        with state.mutex:
            if state.phase in {"aborted", "terminal"}:
                raise PrivateOperationError("private operation phase is invalid")
        return state

    def mark_submit_started(self, op: OperationHandle) -> None:
        state = self._state_for_handle(op)
        with state.mutex:
            payload = self._read_record(state.record_path)
            if payload.get("phase") not in {"ledgered", "submit_started"}:
                raise PrivateOperationError("private record phase is invalid")
            payload["phase"] = "submit_started"
            payload["updated_at"] = _now()
            self._replace_signed(state.record_path, payload)
            state.phase = "submit_started"

    def mark_submit_result(self, op: OperationHandle, row: Any) -> None:
        state = self._state_for_handle(op)
        with state.mutex:
            payload = self._read_record(state.record_path)
            for key in ("studio_job_id", "provider_id", "target_id", "project_id", "destination_identity"):
                if hasattr(row, key) and payload.get(key) != getattr(row, key):
                    raise PrivateOperationError("private record ownership does not match ledger")
            payload["phase"] = "provider_bound" if getattr(row, "provider_job_id", None) else payload.get("phase", "submit_started")
            payload["updated_at"] = _now()
            self._replace_signed(state.record_path, payload)
            state.phase = payload["phase"]

    def load_for_job(self, row: Any) -> OperationHandle:
        for name in ("studio_job_id", "client_request_id", "provider_id", "target_id", "destination_identity", "requested_basename", "request_hash"):
            if not hasattr(row, name):
                raise PrivateOperationError("private record ownership is incomplete")
        client_id = _finite_text(getattr(row, "client_request_id"), "client_request_id")
        record_path = self._records / self._record_name(str(client_id))
        payload = self._read_record(record_path)
        expected = {
            "studio_job_id": getattr(row, "studio_job_id"),
            "client_request_id": getattr(row, "client_request_id"),
            "provider_id": getattr(row, "provider_id"),
            "target_id": getattr(row, "target_id"),
            "project_id": getattr(row, "project_id", None),
            "destination_identity": getattr(row, "destination_identity"),
            "requested_basename": getattr(row, "requested_basename"),
            "request_hash": getattr(row, "request_hash"),
        }
        if not self._record_fields_match(payload, **expected):
            raise PrivateOperationError("private record ownership does not match ledger")
        destination = Path(payload.get("destination", ""))
        requested_basename = _safe_basename(payload["requested_basename"])
        valid_candidate_names = {
            self._candidate_path(destination.parent, requested_basename, index).name
            for index in range(MAX_CANDIDATES)
        }
        if not destination.is_absolute() or destination.name not in valid_candidate_names:
            raise PrivateOperationError("private destination is invalid")
        state = _OperationState(
            owner=self,
            record_path=record_path,
            reservation_path=self._reservations / self._reservation_name(payload["destination_identity"]),
            destination=destination,
            destination_identity=payload["destination_identity"],
            studio_job_id=payload["studio_job_id"],
            client_request_id=payload["client_request_id"],
            provider_id=payload["provider_id"],
            target_id=payload["target_id"],
            project_id=payload.get("project_id"),
            requested_basename=payload["requested_basename"],
            request_hash=payload["request_hash"],
            phase=payload["phase"],
            lock=None,
            delivery_lock=None,
            consumed=True,
            marker_path=None,
            partial_path=None,
            partial_identity=None,
            sink=None,
        )
        return OperationHandle(state)

    def validate_exact_retry(
        self,
        row: Any,
        *,
        destination: os.PathLike[str] | str,
        requested_basename: str,
    ) -> OperationHandle:
        """Authenticate a retry and bind it to its original output choice."""
        operation = self.load_for_job(row)
        state = operation._state
        directory = _canonical(destination, directory=True)
        original_parent = _canonical(state.destination.parent, directory=True)
        if (
            original_parent != directory
            or state.requested_basename != _safe_basename(requested_basename)
        ):
            raise PrivateOperationError(
                "client request output identity conflicts", code="invalid_request"
            )
        return operation

    def _marker_path(self, state: _OperationState) -> Path:
        return self._markers / self._marker_name(state.destination_identity)

    def _delivery_lock(self, state: _OperationState) -> NamedProjectLock:
        existing = getattr(state, "delivery_lock", None)
        if existing is not None:
            return existing
        lock = NamedProjectLock(
            "Local\\HermesFilmSuite-V4Delivery-" + state.destination_identity,
            lock_file=self._root / ("delivery-" + state.destination_identity + ".lock"),
        )
        try:
            lock.acquire()
        except (OSError, ProjectError, ValueError) as exc:
            lock.close()
            raise PrivateOperationError("delivery lock is unavailable") from exc
        state.delivery_lock = lock
        return lock

    def _release_delivery_lock(self, state: _OperationState) -> None:
        lock = getattr(state, "delivery_lock", None)
        if lock is not None:
            lock.close()
            state.delivery_lock = None

    def _load_marker(self, path: Path) -> dict[str, Any]:
        return self._read_record(path, schema=DELIVERY_SCHEMA, limit=MAX_MARKER_BYTES)

    def _validate_marker_identity(self, state: _OperationState, marker: dict[str, Any]) -> str:
        if not self._record_fields_match(
            marker,
            provider_id=state.provider_id,
            destination_identity=state.destination_identity,
        ):
            raise PrivateOperationError("delivery marker ownership failed", code="output_invalid")
        expected_partial = f".{state.destination.name}.{state.destination_identity}.part"
        if marker.get("partial_name") != expected_partial:
            raise PrivateOperationError("delivery marker ownership failed", code="output_invalid")
        return expected_partial

    def open_delivery(self, op: OperationHandle) -> DeliveryHandle:
        state = self._state_for_handle(op)
        with state.mutex:
            marker_path = self._marker_path(state)
            state.marker_path = marker_path
            try:
                self._delivery_lock(state)
                if _partial_present(marker_path):
                    marker = self._load_marker(marker_path)
                    expected_partial = self._validate_marker_identity(state, marker)
                    state.phase = marker.get("phase", "polling")
                    state.partial_path = state.destination.parent / expected_partial
                    state.partial_identity = marker.get("partial_identity")
                else:
                    state.phase = "polling"
                    state.partial_path = state.destination.parent / f".{state.destination.name}.{state.destination_identity}.part"
                    state.partial_identity = None
                    marker = {
                        "schema": DELIVERY_SCHEMA,
                        "created_at": _now(),
                        "updated_at": _now(),
                        "phase": "polling",
                        "provider_id": state.provider_id,
                        "destination_identity": state.destination_identity,
                        "partial_name": state.partial_path.name,
                        "partial_identity": None,
                        "output_sha256": None,
                        "output_bytes": None,
                    }
                    self._write_new(marker_path, marker, limit=MAX_MARKER_BYTES)
            except BaseException:
                # Release the delivery lock on ANY failure between acquiring it
                # and returning the handle — including BaseException such as
                # asyncio.CancelledError / KeyboardInterrupt / SystemExit.
                # No DeliveryHandle is returned on this path, so the caller's
                # own cleanup cannot reach abort_delivery(); if we only caught
                # Exception the cross-process lock would leak on cancellation.
                self._release_delivery_lock(state)
                raise
            return DeliveryHandle(state)

    def _assert_delivery(self, delivery: DeliveryHandle) -> _OperationState:
        if not isinstance(delivery, DeliveryHandle) or delivery._state.owner is not self:
            raise PrivateOperationError("delivery handle is invalid")
        return delivery._state

    def _owned_partial_token(self, state: _OperationState, marker: dict[str, Any]) -> str:
        expected = marker.get("partial_identity")
        if type(expected) is not str or not _HEX.fullmatch(expected):
            raise PrivateOperationError("partial ownership is unavailable", code="output_invalid")
        if state.partial_path is None or not _partial_present(state.partial_path):
            raise PrivateOperationError("downloaded partial is unavailable", code="output_invalid")
        actual = _partial_token(state.partial_path)
        if actual != expected:
            raise PrivateOperationError("partial ownership failed", code="output_invalid")
        state.partial_identity = expected
        return expected

    def _retire_owned_partial(self, state: _OperationState, expected: str) -> None:
        path = state.partial_path
        if path is None or not _partial_present(path):
            return
        handle: Any = None
        try:
            if os.name == "nt":
                handle = _path_boundary._win_open_handle(
                    path,
                    directory=False,
                    media=False,
                    allow_reparse=False,
                    delete_source=True,
                )
                identity = _path_boundary._identity_from_handle(handle, path)
                if _object_token(identity) != expected:
                    raise PrivateOperationError("partial ownership failed", code="output_invalid")
                _path_boundary._set_file_disposition(handle)
            else:
                # POSIX has no portable unlink-by-open-FD primitive.  Bind the
                # retirement to the observed object and delegate the final
                # mutation to the pinned-parent tombstone protocol.  The
                # helper reopens the parent/source under descriptor-relative
                # handles, exchanges with an exclusive same-parent tombstone,
                # and preserves the retired bytes under that private name.
                handle, identity = _path_boundary._open_existing(path, directory=False)
                if identity.is_directory or _object_token(identity) != expected:
                    raise PrivateOperationError("partial ownership failed", code="output_invalid")
                handle.close()
                handle = None
                if not _path_boundary.unlink_handle_bound(
                    path,
                    identity,
                    expected_sha256=None,
                    max_bytes=identity.size,
                ):
                    raise PrivateOperationError("partial ownership could not be retired", code="output_invalid")
        except PrivateOperationError:
            raise
        except (OSError, ProjectError, ValueError) as exc:
            raise PrivateOperationError("owned partial could not be retired", code="output_invalid") from exc
        finally:
            if handle is not None:
                handle.close()

    def open_sink(self, delivery: DeliveryHandle) -> ExclusiveSink:
        state = self._assert_delivery(delivery)
        with state.mutex:
            if state.phase != "polling" or state.partial_path is None:
                raise PrivateOperationError("delivery phase is invalid")
            if getattr(state, "sink", None) is not None:
                raise PrivateOperationError("delivery sink is already open", code="output_invalid")
            sink: ExclusiveSink | None = None
            try:
                # ExclusiveSink uses O_EXCL, so a regular file, symlink, or
                # reparse object at this exact name is preserved and rejected.
                sink = ExclusiveSink(state.partial_path)
                state.sink = sink
                state.partial_identity = _partial_token(state.partial_path)
                marker = self._load_marker(state.marker_path)
                self._validate_marker_identity(state, marker)
                marker.update({"partial_identity": state.partial_identity, "updated_at": _now()})
                self._replace_signed(state.marker_path, marker, limit=MAX_MARKER_BYTES)
                return sink
            except BaseException as exc:
                cleanup_exc: BaseException | None = None
                fallback_retired = False
                try:
                    if sink is not None:
                        sink.request_abort()
                        sink.worker_finished()
                        sink.abort_owned()
                except BaseException as caught:
                    cleanup_exc = caught
                    if state.partial_identity is not None:
                        try:
                            self._retire_owned_partial(state, state.partial_identity)
                            fallback_retired = True
                        except BaseException as retirement_exc:
                            cleanup_exc = retirement_exc
                            try:
                                marker = self._load_marker(state.marker_path)
                                self._validate_marker_identity(state, marker)
                                marker.update(
                                    {
                                        "partial_identity": state.partial_identity,
                                        "updated_at": _now(),
                                    }
                                )
                                self._replace_signed(
                                    state.marker_path, marker, limit=MAX_MARKER_BYTES
                                )
                            except BaseException as persistence_exc:
                                cleanup_exc = persistence_exc
                finally:
                    if cleanup_exc is None or fallback_retired:
                        state.partial_identity = None
                    self._release_delivery_lock(state)
                if cleanup_exc is not None and not fallback_retired:
                    raise PrivateOperationError(
                        "partial cleanup failed", code="output_invalid"
                    ) from cleanup_exc
                if isinstance(exc, DownloadError):
                    raise PrivateOperationError(
                        "partial could not be created exclusively", code="output_invalid"
                    ) from exc
                raise

    def mark_downloaded(self, delivery: DeliveryHandle, digest: DownloadDigest) -> None:
        state = self._assert_delivery(delivery)
        if not isinstance(digest, DownloadDigest) or not _SHA256.fullmatch(digest.sha256) or type(digest.bytes_written) is not int or digest.bytes_written < 0:
            raise PrivateOperationError("download digest is invalid", code="output_invalid")
        with state.mutex:
            try:
                sink = getattr(state, "sink", None)
                if state.phase != "polling" or state.partial_path is None or sink is None or sink.state != "closed":
                    raise PrivateOperationError("downloaded partial is unavailable", code="output_invalid")
                marker = self._load_marker(state.marker_path)
                self._validate_marker_identity(state, marker)
                self._owned_partial_token(state, marker)
                actual = _sha256_file(state.partial_path)
                if actual != digest.sha256 or state.partial_path.stat().st_size != digest.bytes_written:
                    raise PrivateOperationError("download digest does not match partial", code="output_invalid")
                marker.update({"phase": "downloaded", "updated_at": _now(), "output_sha256": digest.sha256, "output_bytes": digest.bytes_written})
                self._replace_signed(state.marker_path, marker, limit=MAX_MARKER_BYTES)
                state.phase = "downloaded"
            except Exception:
                self._release_delivery_lock(state)
                raise

    def abort_delivery(self, delivery: DeliveryHandle) -> None:
        """Release the cross-process delivery lock after a failed delivery."""
        state = self._assert_delivery(delivery)
        with state.mutex:
            self._release_delivery_lock(state)

    def _publish_owned_partial(self, state: _OperationState, marker: dict[str, Any], digest: DownloadDigest) -> None:
        if state.partial_path is None:
            raise PrivateOperationError("download partial is unavailable", code="output_invalid")
        expected = self._owned_partial_token(state, marker)
        source: Any = None
        parent: Any = None
        try:
            if os.name == "nt":
                source = _path_boundary._win_open_handle(
                    state.partial_path,
                    directory=False,
                    media=False,
                    allow_reparse=False,
                    delete_source=True,
                )
                identity = _path_boundary._identity_from_handle(source, state.partial_path)
            else:
                source, identity = _path_boundary._open_existing(state.partial_path, directory=False)
            if _object_token(identity) != expected:
                raise PrivateOperationError("partial ownership failed", code="output_invalid")
            actual_sha, actual_bytes = _path_boundary._hash_handle(
                source,
                max_bytes=digest.bytes_written,
            )
            if actual_sha != digest.sha256 or actual_bytes != digest.bytes_written:
                raise PrivateOperationError("download digest does not match partial", code="output_invalid")
            parent, parent_identity = _path_boundary._open_existing(state.destination.parent, directory=True)
            if not parent_identity.is_directory:
                raise PrivateOperationError("output parent is unavailable", code="output_invalid")
            try:
                if os.name == "nt":
                    _path_boundary.win_link_handle_no_replace(source, parent, state.destination.name)
                else:
                    _path_boundary._posix_linkat_empty_path(int(source.raw), int(parent.raw), state.destination.name)
            except FileExistsError:
                _verify_output(state.destination, digest)
            if os.name == "nt":
                _path_boundary._set_file_disposition(source)
            _flush_parent(state.destination.parent)
        except PrivateOperationError:
            raise
        except (OSError, ProjectError, ValueError) as exc:
            raise PrivateOperationError("output publication failed", code="output_invalid") from exc
        finally:
            if parent is not None:
                parent.close()
            if source is not None:
                source.close()

    def publish_or_adopt(self, delivery: DeliveryHandle, digest: DownloadDigest) -> PublishedOutput:
        state = self._assert_delivery(delivery)
        with state.mutex:
            try:
                self._delivery_lock(state)
                marker = self._load_marker(state.marker_path)
                self._validate_marker_identity(state, marker)
                if marker.get("phase") == "published":
                    if marker.get("output_sha256") != digest.sha256 or marker.get("output_bytes") != digest.bytes_written:
                        raise PrivateOperationError("published marker does not match output", code="output_invalid")
                    _verify_output(state.destination, digest)
                    if state.partial_path is not None and _partial_present(state.partial_path):
                        expected = self._owned_partial_token(state, marker)
                        self._retire_owned_partial(state, expected)
                elif marker.get("phase") == "downloaded":
                    self._publish_owned_partial(state, marker, digest)
                    marker.update({"phase": "published", "updated_at": _now(), "output_sha256": digest.sha256, "output_bytes": digest.bytes_written, "published_identity": digest.sha256})
                    self._replace_signed(state.marker_path, marker, limit=MAX_MARKER_BYTES)
                    state.phase = "published"
                else:
                    raise PrivateOperationError("delivery marker phase is invalid", code="output_invalid")
                return PublishedOutput(digest.sha256, digest.sha256, digest.bytes_written)
            except Exception:
                self._release_delivery_lock(state)
                raise

    def recover_delivery_before_provider(self, op: OperationHandle, row: Any) -> PublishedOutput | None:
        state = self._state_for_handle(op)
        marker_path = self._marker_path(state)
        state.marker_path = marker_path
        with state.mutex:
            if not _partial_present(marker_path):
                return None
            try:
                self._delivery_lock(state)
                if not _partial_present(marker_path):
                    self._release_delivery_lock(state)
                    return None
                marker = self._load_marker(marker_path)
                expected_partial = self._validate_marker_identity(state, marker)
                state.partial_path = state.destination.parent / expected_partial
                state.partial_identity = marker.get("partial_identity")
                phase = marker.get("phase")
                if phase == "polling":
                    if _partial_present(state.partial_path):
                        expected = self._owned_partial_token(state, marker)
                        self._retire_owned_partial(state, expected)
                    marker_path.unlink(missing_ok=True)
                    self._release_delivery_lock(state)
                    return None
                if phase not in {"downloaded", "published"}:
                    raise PrivateOperationError("delivery marker phase is invalid", code="output_invalid")
                state.phase = phase
                digest = DownloadDigest(str(marker.get("output_sha256")), int(marker.get("output_bytes")))
                return self.publish_or_adopt(DeliveryHandle(state), digest)
            except Exception:
                self._release_delivery_lock(state)
                raise

    def finalize_ready_receipt(self, op: OperationHandle, published: PublishedOutput, row: Any) -> OperationHandle:
        state = self._state_for_handle(op)
        with state.mutex:
            try:
                self._delivery_lock(state)
                if getattr(row, "destination_identity", state.destination_identity) != state.destination_identity:
                    raise PrivateOperationError("published destination does not match ledger", code="output_invalid")
                payload = self._read_record(state.record_path)
                marker_path = getattr(state, "marker_path", None)
                marker = self._load_marker(marker_path) if isinstance(marker_path, Path) and _partial_present(marker_path) else None
                partial_path = getattr(state, "partial_path", None)
                if isinstance(partial_path, Path) and _partial_present(partial_path):
                    if marker is None:
                        raise PrivateOperationError("partial ownership is unavailable", code="output_invalid")
                    expected = self._owned_partial_token(state, marker)
                    self._retire_owned_partial(state, expected)
                payload.update({"phase": "ready", "updated_at": _now(), "output_sha256": published.output_sha256, "output_bytes": published.output_bytes, "published_identity": published.published_identity})
                self._replace_signed(state.record_path, payload)
                for path in (marker_path, state.reservation_path):
                    if isinstance(path, Path):
                        path.unlink(missing_ok=True)
                if state.lock is not None:
                    state.lock.close()
                    state.lock = None
                state.phase = "ready"
                return op
            finally:
                self._release_delivery_lock(state)

    def reconcile_ready_receipt(self, row: Any) -> OperationHandle:
        op = self.load_for_job(row)
        state = op._state
        with state.mutex:
            payload = self._read_record(state.record_path)
            if payload.get("phase") == "ready":
                published_identity = payload.get("published_identity")
                output_sha256 = payload.get("output_sha256")
                output_bytes = payload.get("output_bytes")
                ledger_published = getattr(row, "published_identity", None)
                encoded_identity = getattr(row, "provider_output_identity_json", None)
                try:
                    ledger_identity = json.loads(encoded_identity)
                except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
                    raise PrivateOperationError(
                        "ready ledger identity is invalid", code="output_invalid"
                    ) from exc
                if (
                    type(published_identity) is not str
                    or _HEX.fullmatch(published_identity) is None
                    or ledger_published != published_identity
                    or type(ledger_identity) is not dict
                    or set(ledger_identity) != {"bytes", "published_identity", "sha256"}
                    or ledger_identity.get("published_identity") != published_identity
                    or ledger_identity.get("sha256") != output_sha256
                    or ledger_identity.get("bytes") != output_bytes
                ):
                    raise PrivateOperationError(
                        "ready ledger identity does not match private record",
                        code="output_invalid",
                    )
                _verify_output(
                    state.destination,
                    DownloadDigest(output_sha256, int(output_bytes)),
                )
                return op
            marker_path = self._marker_path(state)
            marker = self._load_marker(marker_path)
            expected_partial = self._validate_marker_identity(state, marker)
            state.marker_path = marker_path
            state.partial_path = state.destination.parent / expected_partial
            state.partial_identity = marker.get("partial_identity")
            if marker.get("phase") != "published":
                raise PrivateOperationError("ready receipt cannot be reconciled", code="output_invalid")
            digest = DownloadDigest(marker.get("output_sha256"), int(marker.get("output_bytes")))
            published = self.publish_or_adopt(DeliveryHandle(state), digest)
            expected = getattr(row, "published_identity", None)
            if expected is not None and expected != published.published_identity:
                raise PrivateOperationError("published identity does not match ledger", code="output_invalid")
            return self.finalize_ready_receipt(op, published, row)

    def release_terminal_without_output(self, op: OperationHandle, terminal_status: str) -> None:
        state = self._state_for_handle(op)
        if terminal_status not in {"failed", "cancelled", "moderated"}:
            raise PrivateOperationError("terminal status is invalid", code="invalid_request")
        with state.mutex:
            try:
                marker_path = getattr(state, "marker_path", None)
                if not isinstance(marker_path, Path):
                    marker_path = self._marker_path(state)
                    state.marker_path = marker_path
                marker = None
                if _partial_present(marker_path):
                    self._delivery_lock(state)
                    marker = self._load_marker(marker_path)
                    expected_partial = self._validate_marker_identity(state, marker)
                    state.partial_path = state.destination.parent / expected_partial
                    state.partial_identity = marker.get("partial_identity")
                    if _partial_present(state.partial_path):
                        expected = self._owned_partial_token(state, marker)
                        self._retire_owned_partial(state, expected)
                payload = self._read_record(state.record_path)
                payload.update({"phase": "terminal", "terminal_status": terminal_status, "updated_at": _now()})
                self._replace_signed(state.record_path, payload)
                for path in (marker_path, state.reservation_path):
                    if isinstance(path, Path):
                        path.unlink(missing_ok=True)
                state.phase = "terminal"
            finally:
                self._release_delivery_lock(state)

    def saved_path_for_receipt(self, op: OperationHandle) -> str:
        state = self._state_for_handle(op)
        with state.mutex:
            payload = self._read_record(state.record_path)
            if payload.get("phase") != "ready":
                raise PrivateOperationError("ready private record is unavailable")
            digest = DownloadDigest(payload.get("output_sha256"), int(payload.get("output_bytes")))
            _verify_output(state.destination, digest)
            return str(state.destination)

    def sweep(self, *, now: float | None = None, row_exists: Callable[[dict[str, Any]], bool] | None = None) -> int:
        return self.sweep_orphan_reservations(now=now, row_exists=row_exists)

    def sweep_orphan_reservations(
        self,
        *,
        now: float | None = None,
        row_exists: Callable[[str], bool] | None = None,
    ) -> int:
        """Sweep only after the caller supplies an explicit durable-row check."""
        if not callable(row_exists):
            raise PrivateOperationError("private reservation sweep requires a ledger callback")
        now = time.time() if now is None else now
        lock = NamedProjectLock(
            "Local\\HermesFilmSuite-V4Admission-Sweep-" + self._mac_hex(str(self._root).encode()),
            lock_file=self._admission_lock_path,
        )
        try:
            lock.acquire()
            try:
                candidates = list(self._reservations.iterdir())
            except OSError as exc:
                raise PrivateOperationError("private reservation sweep is unavailable") from exc
            if len(candidates) > MAX_SWEEP_CANDIDATES:
                raise PrivateOperationError("private reservation sweep is unavailable")
            deleted = 0
            for path in candidates:
                if not _HEX.fullmatch(path.stem) or path.suffix != ".json":
                    continue
                try:
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MARKER_BYTES or now - info.st_mtime < 86400:
                        continue
                    reservation = self._read_record(path, schema=RESERVATION_SCHEMA, limit=MAX_MARKER_BYTES)
                    if reservation.get("destination_identity") != path.stem:
                        continue
                    try:
                        row_present = bool(row_exists(str(reservation["client_request_id"])))
                    except Exception as exc:
                        raise PrivateOperationError("private reservation sweep is unavailable") from exc
                    if row_present:
                        continue
                    record_name = reservation.get("record_name")
                    if not isinstance(record_name, str) or not _HEX.fullmatch(Path(record_name).stem) or Path(record_name).suffix != ".json":
                        continue
                    record_path = self._records / record_name
                    if record_path.is_file() or record_path.is_symlink():
                        continue
                    path.unlink()
                    _flush_parent(path.parent)
                    deleted += 1
                except PrivateOperationError:
                    raise
                except OSError:
                    continue
            return deleted
        finally:
            lock.close()



def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PrivateOperationError("output could not be hashed", code="output_invalid") from exc
    return digest.hexdigest()


def _verify_output(path: Path, digest: DownloadDigest) -> None:
    if not path.is_file() or path.is_symlink():
        raise PrivateOperationError("output identity is unavailable", code="output_invalid")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PrivateOperationError("output identity is unavailable", code="output_invalid") from exc
    if size != digest.bytes_written or _sha256_file(path) != digest.sha256:
        raise PrivateOperationError("output identity does not match", code="output_invalid")


__all__ = [
    "DELIVERY_SCHEMA",
    "MAX_CANDIDATES",
    "MAX_MARKER_BYTES",
    "OperationHandle",
    "PrivateOperationError",
    "PrivateOperations",
    "PreparedOperation",
    "PublishedOutput",
    "DeliveryHandle",
]
