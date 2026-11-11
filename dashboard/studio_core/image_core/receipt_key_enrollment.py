"""Profile-global receipt-key enrollment and crash-safe bootstrap.

This module is intentionally a narrow prerequisite for Image owner
construction.  It owns neither Image state nor a caller-provided authority.  A
profile gets one immutable enrollment record; after that record exists every
open is strict load-only.
"""

from __future__ import annotations


import datetime as _datetime
import ctypes
import errno
import hashlib
import hmac
import importlib
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

try:
    from hermes_constants import get_hermes_home
except ImportError:  # pragma: no cover - repository tests install Hermes' root
    def get_hermes_home() -> Path:
        return Path.home() / "AppData" / "Local" / "hermes"
# These owner modules can also be loaded as isolated top-level packages.
_migration_spec = importlib.util.spec_from_file_location(
    "film_suite_owner_migration", Path(__file__).resolve().parents[2] / "generation" / "legacy_plugin_migration.py"
)
_migration_module = importlib.util.module_from_spec(_migration_spec)
_migration_spec.loader.exec_module(_migration_module)
state_root = _migration_module.state_root
TOMBSTONE_PATTERN = _migration_module.TOMBSTONE_PATTERN


PROFILE_KEY_SIZE = 32
MAX_RECEIPT_DIRECTORY_ENTRIES = 2
MAX_PENDING_BYTES = 512
MAX_ENROLLMENT_BYTES = 2048
MAX_JSON_DEPTH = 8
STAGING_DIRECTORY_NAME = "receipt-key-staging"
MAX_STAGING_ACTIVE = 2
MAX_STAGING_RESIDUES = 8
MAX_STAGING_ENTRIES = MAX_STAGING_ACTIVE + MAX_STAGING_RESIDUES
MAX_STAGING_RESIDUE_BYTES = MAX_ENROLLMENT_BYTES
MAX_STAGING_TOTAL_BYTES = (
    (MAX_STAGING_ACTIVE + MAX_STAGING_RESIDUES) * MAX_STAGING_RESIDUE_BYTES
)

ENROLLMENT_SCHEMA = "studio.receipt-key-enrollment/1"
PENDING_SCHEMA = "studio.receipt-key-bootstrap/1"
ENROLLMENT_FILENAME = "enrollment-v1.json"
PENDING_FILENAME = ".bootstrap-pending"
KEY_FILENAME = "path-identity.key"

_ENROLLMENT_ID_RE = re.compile(r"^imgkey_[0-9a-f]{32}$")
_PENDING_ID_RE = re.compile(r"^boot_[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_STAGING_STAGE_RE = re.compile(
    r"^\.(?:bootstrap-pending|enrollment-v1\.json)\.[0-9a-f]{32}\.tmp$"
)
_STAGING_RESIDUE_RE = re.compile(TOMBSTONE_PATTERN)
_ENROLLMENT_ID_DOMAIN = b"hermes-studio/profile-receipt-enrollment-id/v1"


class ReceiptKeyEnrollmentError(ValueError):
    """Finite, public-safe error for every enrollment boundary failure."""

    CODES = frozenset({"image_key_unavailable", "image_state_unavailable", "image_state_not_private"})

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            code = "image_state_unavailable"
        self.code = code
        self.reason_code = code
        self.reason = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ReceiptKeyEnrollmentEvidence:
    """The closed, non-secret enrollment envelope."""

    schema: str
    owner_scope: str
    enrollment_id: str
    enrollment_revision: int
    receipt_key_fingerprint: str
    key_length: int
    enrolled_at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "owner_scope": self.owner_scope,
            "enrollment_id": self.enrollment_id,
            "enrollment_revision": self.enrollment_revision,
            "receipt_key_fingerprint": self.receipt_key_fingerprint,
            "key_length": self.key_length,
            "enrolled_at": self.enrolled_at,
        }

    to_dict = as_dict


@dataclass(frozen=True, slots=True)
class ReceiptKeyEnrollmentResult:
    """Validated public evidence plus the key for the in-process owner."""

    evidence: ReceiptKeyEnrollmentEvidence
    key: bytes

    def __post_init__(self) -> None:
        if type(self.key) is not bytes or len(self.key) != PROFILE_KEY_SIZE:
            raise TypeError("invalid internal enrollment key")

    def __repr__(self) -> str:
        return f"ReceiptKeyEnrollmentResult(evidence={self.evidence!r}, key=<internal>)"

    @property
    def enrollment_id(self) -> str:
        return self.evidence.enrollment_id

    @property
    def receipt_key_fingerprint(self) -> str:
        return self.evidence.receipt_key_fingerprint


# The module-level value is an implementation dependency, not caller authority.
# It is loaded from the accepted sibling so this file works both in the normal
# package and in the repository's synthetic test package topology.

def _load_path_boundary() -> ModuleType:
    for name, module in tuple(sys.modules.items()):
        if name.endswith(".path_boundary") and hasattr(module, "_profile_identity_lock"):
            return module
    package = __package__ or ""
    if "." in package:
        parent = package.rsplit(".", 1)[0]
        try:
            return importlib.import_module(f"{parent}.path_boundary")
        except ImportError:
            pass
    path = Path(__file__).parent.parent / "path_boundary.py"
    spec = importlib.util.spec_from_file_location(
        "_receipt_key_enrollment_path_boundary", path
    )
    if spec is None or spec.loader is None:
        raise ImportError("path boundary unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


path_boundary = _load_path_boundary()


@dataclass(frozen=True, slots=True)
class _Paths:
    home: Path
    data_dir: Path
    key_path: Path
    receipt_dir: Path
    enrollment_path: Path
    pending_path: Path
    staging_dir: Path


@dataclass(frozen=True, slots=True)
class _FileSnapshot:
    identity: Any
    raw: bytes
    digest: str
    size: int
    private: bool


@dataclass(frozen=True, slots=True)
class _StagingInventory:
    active: tuple[str, ...]
    active_count: int
    residue_count: int
    total_bytes: int


@dataclass(slots=True)
class _StageFile:
    path: Path
    handle: Any
    identity: Any
    creator: Any


class _MissingFile(Exception):
    pass


def _fail(code: str) -> Any:
    raise ReceiptKeyEnrollmentError(code) from None


def _utc_second() -> str:
    now = _datetime.datetime.now(_datetime.timezone.utc)
    if now.tzinfo is None:
        _fail("image_state_unavailable")
    now = now.astimezone(_datetime.timezone.utc).replace(microsecond=0)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(value: dict[str, Any]) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError, MemoryError):
        _fail("image_state_unavailable")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("image_state_unavailable")
        result[key] = value
    return result


def _check_json_depth(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _fail("image_state_unavailable")
    if isinstance(value, dict):
        for key, child in value.items():
            if type(key) is not str:
                _fail("image_state_unavailable")
            _check_json_depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _check_json_depth(child, depth + 1)


def _parse_canonical(raw: bytes, maximum: int) -> dict[str, Any]:
    if type(raw) is not bytes or len(raw) > maximum:
        _fail("image_state_unavailable")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: (_fail("image_state_unavailable")),
        )
    except ReceiptKeyEnrollmentError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError, MemoryError, OverflowError):
        _fail("image_state_unavailable")
    if type(value) is not dict:
        _fail("image_state_unavailable")
    _check_json_depth(value)
    if _canonical(value) != raw:
        _fail("image_state_unavailable")
    return value


def _valid_timestamp(value: Any) -> bool:
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        parsed = _datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError):
        return False
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ") == value


def _paths(hermes_home: os.PathLike[str] | str | None) -> _Paths:
    if hermes_home is None:
        hermes_home = get_hermes_home()
    try:
        home, data_dir, key_path = path_boundary._unprepared_profile_paths(hermes_home)
        home = Path(home)
        data_dir = Path(data_dir)
        key_path = Path(key_path)
        path_boundary._reject_reparse_components(home)
        profile_root = state_root(home)
        receipt_dir = profile_root / "receipt-key"
        staging_dir = profile_root / STAGING_DIRECTORY_NAME
        path_boundary._reject_reparse_components(profile_root)
        path_boundary._reject_reparse_components(receipt_dir)
        if os.name != "nt":
            path_boundary._reject_reparse_components(staging_dir)
        return _Paths(
            home=home,
            data_dir=data_dir,
            key_path=key_path,
            receipt_dir=receipt_dir,
            enrollment_path=receipt_dir / ENROLLMENT_FILENAME,
            pending_path=receipt_dir / PENDING_FILENAME,
            staging_dir=staging_dir,
        )
    except ReceiptKeyEnrollmentError:
        raise
    except Exception:
        _fail("image_state_unavailable")


def _ensure_private_directory(path: Path) -> None:
    try:
        path_boundary.ensure_private_node(path, directory=True)
    except Exception:
        _fail("image_state_unavailable")


def _prepare_profile_directories(paths: _Paths) -> None:
    # These are provisioning roots only.  The first protocol object mutation is
    # still the exclusive pending marker, after the profile lock is held.
    _ensure_private_directory(paths.home)
    _ensure_private_directory(paths.home / "plugin-data")
    _ensure_private_directory(paths.data_dir)
    _ensure_private_directory(state_root(paths.home))
    _ensure_private_directory(paths.receipt_dir)
    if os.name != "nt":
        _ensure_private_directory(paths.staging_dir)


def _retire_unenrolled_key(paths: _Paths) -> None:
    """Remove an inert pre-enrollment key so bootstrap can reserve its own.

    Only ever reached once ``_profile_state_exists`` has proven nothing is
    enrolled under it.  Failure to remove is a refusal, never a silent
    continue: bootstrapping around a key we could not retire would enroll
    bytes we did not choose.
    """
    try:
        paths.key_path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        _fail("image_key_unavailable")


def _profile_state_exists(paths: _Paths) -> bool:
    """Report whether anything is already enrolled under this profile's key.

    Only published image contexts count.  The check is a bounded existence
    probe - it opens nothing, parses nothing, and treats any uncertainty as
    "state exists" so an unreadable directory refuses rather than adopts.
    """
    contexts = paths.receipt_dir.parent / "image-contexts"
    try:
        if not contexts.is_dir():
            return False
        for entry in contexts.iterdir():
            if entry.is_dir():
                return True
        return False
    except OSError:
        return True


def _new_lock(paths: _Paths) -> Any:
    try:
        return path_boundary._profile_identity_lock(paths.data_dir)
    except ReceiptKeyEnrollmentError:
        raise
    except Exception:
        _fail("image_key_unavailable")


def _open_lock(paths: _Paths) -> Any:
    lock = _new_lock(paths)
    try:
        acquired = lock.acquire()
        if acquired is False:
            lock.close()
            _fail("image_key_unavailable")
    except ReceiptKeyEnrollmentError:
        try:
            lock.close()
        except Exception:
            pass
        raise
    except Exception:
        try:
            lock.close()
        except Exception:
            pass
        _fail("image_key_unavailable")
    return lock


def _close(handle: Any) -> None:
    try:
        handle.close()
    except Exception:
        pass


def _is_missing_error(exc: Exception) -> bool:
    if isinstance(exc, FileNotFoundError):
        return True
    return getattr(exc, "code", None) in {"project_not_found", "project_unavailable"} and not isinstance(
        exc, ReceiptKeyEnrollmentError
    )


def _identity_same(left: Any, right: Any) -> bool:
    try:
        same = getattr(left, "same_object", None)
        if not callable(same) or not bool(same(right)):
            return False
        reverse = getattr(right, "same_object", None)
        if callable(reverse) and not bool(reverse(left)):
            return False
        return bool(getattr(left, "is_directory")) == bool(getattr(right, "is_directory"))
    except Exception:
        return False


class _DescriptorView:
    """Borrowed native view of a creator descriptor; it never owns the fd."""

    __slots__ = ("raw", "closed")

    def __init__(self, fd: int) -> None:
        if os.name == "nt":
            import msvcrt

            raw = int(msvcrt.get_osfhandle(fd))
            if raw == -1:
                raise OSError("creator descriptor is unavailable")
            self.raw = raw
        else:
            self.raw = int(fd)
        self.closed = False


class _RawHandleView:
    """Borrowed native view of a raw creator HANDLE; it never closes it."""

    __slots__ = ("raw", "closed")

    def __init__(self, raw: int) -> None:
        self.raw = int(raw)
        self.closed = False


class _CreatedStageOwner:
    """Own the exact object created for a stage from the instant it exists.

    Windows starts with the raw CreateFile HANDLE and transfers ownership to the
    CRT descriptor only after ``open_osfhandle`` returns.  POSIX starts with the
    dirfd-relative creator descriptor.  Cleanup always consumes whichever native
    owner is still live, and ``close`` is idempotent so a failure path cannot
    double-close a HANDLE or descriptor.
    """

    __slots__ = ("_raw_handle", "_fd")

    def __init__(self, *, raw_handle: int | None = None, fd: int | None = None) -> None:
        if os.name == "nt":
            if raw_handle is None or fd is not None:
                raise ValueError("invalid Windows stage owner")
            self._raw_handle: int | None = int(raw_handle)
            self._fd: int | None = None
        else:
            if fd is None or raw_handle is not None:
                raise ValueError("invalid POSIX stage owner")
            self._raw_handle = None
            self._fd = int(fd)

    @property
    def descriptor_fd(self) -> int:
        if self._fd is None:
            raise RuntimeError("stage descriptor is not wrapped")
        return self._fd

    def native_view(self) -> Any:
        if os.name == "nt" and self._raw_handle is not None:
            return _RawHandleView(self._raw_handle)
        if self._fd is not None:
            return _DescriptorView(self._fd)
        raise RuntimeError("stage owner is closed")

    def adopt_wrapped_descriptor(self, fd: int) -> None:
        if os.name != "nt" or self._raw_handle is None or self._fd is not None:
            raise RuntimeError("stage owner transfer is not available")
        if type(fd) is not int or fd < 0:
            raise TypeError("invalid wrapped stage descriptor")
        # open_osfhandle has returned successfully, so the CRT descriptor now
        # owns the native HANDLE.  Do this exactly once and never CloseHandle
        # the transferred raw value separately.
        self._fd = fd
        self._raw_handle = None

    def close(self) -> None:
        if self._raw_handle is not None:
            raw = self._raw_handle
            self._raw_handle = None
            try:
                path_boundary._CloseHandle(raw)
            except BaseException:
                pass
            return
        if self._fd is not None:
            fd = self._fd
            self._fd = None
            try:
                os.close(fd)
            except BaseException:
                pass



def _stage_fallback(parent_identity: Any, name: str) -> Path:
    parent = str(getattr(parent_identity, "canonical_path", ""))
    separator = "\\" if os.name == "nt" else os.sep
    return Path(parent.rstrip("\\/") + separator + name)


def _same_filesystem(left: Any, right: Any) -> bool:
    try:
        return int(getattr(left, "volume_serial")) == int(getattr(right, "volume_serial"))
    except Exception:
        return False


def _open_stage_parent(
    paths: _Paths,
    receipt_handle: Any | None,
    receipt_identity: Any,
) -> tuple[Any, Any, Path, bool]:
    """Open the POSIX sibling staging parent, or reuse receipt on Windows."""

    if os.name == "nt":
        if receipt_handle is None:
            _fail("image_state_unavailable")
        return receipt_handle, receipt_identity, paths.receipt_dir, False
    stage_path = paths.staging_dir
    stage_handle = None
    try:
        stage_handle, stage_identity = path_boundary._open_existing(stage_path, directory=True)
        if (
            not bool(getattr(stage_identity, "is_directory", False))
            or not _same_filesystem(receipt_identity, stage_identity)
        ):
            _fail("image_state_unavailable")
        return stage_handle, stage_identity, stage_path, True
    except ReceiptKeyEnrollmentError:
        if stage_handle is not None:
            _close(stage_handle)
        raise
    except Exception:
        if stage_handle is not None:
            _close(stage_handle)
        _fail("image_state_unavailable")


def _posix_rename_between(
    source_parent: Any,
    source_name: str,
    target_parent: Any,
    target_name: str,
    flags: int,
) -> None:
    """Use Linux renameat2 across pinned same-filesystem directories only."""

    if os.name == "nt":
        raise RuntimeError("cross-directory POSIX rename requested on Windows")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        int(source_parent.raw),
        ctypes.c_char_p(os.fsencode(source_name)),
        int(target_parent.raw),
        ctypes.c_char_p(os.fsencode(target_name)),
        flags,
    )
    if result == 0:
        return
    error = ctypes.get_errno() or errno.EIO
    if error == errno.EEXIST and flags == 0x1:
        raise FileExistsError(error, "rename target already exists", target_name)
    if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        raise OSError(errno.ENOSYS, "requested cross-directory rename is unavailable")
    raise OSError(error, "cross-directory rename failed", source_name)


def _descriptor_identity(fd: int, fallback: Path) -> Any:
    return path_boundary._identity_from_handle(_DescriptorView(fd), fallback)


def _stage_parent_matches(identity: Any, parent_identity: Any, name: str) -> bool:
    actual = str(getattr(identity, "canonical_path", ""))
    expected = str(_stage_fallback(parent_identity, name))
    if os.name == "nt":
        return actual.casefold() == expected.casefold()
    return actual == expected


def _creator_matches(
    fd: int,
    expected_identity: Any,
    parent_identity: Any,
    name: str,
    *,
    expected_size: int | None,
) -> Any:
    observed = _descriptor_identity(fd, _stage_fallback(parent_identity, name))
    if (
        not _identity_same(expected_identity, observed)
        or bool(getattr(observed, "is_directory", True))
        or not _stage_parent_matches(observed, parent_identity, name)
        or (expected_size is not None and int(getattr(observed, "size", -1)) != expected_size)
    ):
        _fail("image_state_unavailable")
    return observed


def _posix_creator_identity(
    owner: _CreatedStageOwner,
    parent_identity: Any,
    name: str,
) -> Any | None:
    """Recover creator identity from the still-open fd without pathname I/O."""

    try:
        info = os.fstat(owner.descriptor_fd)
        return path_boundary.PathIdentity(
            str(_stage_fallback(parent_identity, name).resolve(strict=False)),
            int(info.st_dev),
            int(info.st_ino),
            stat.S_ISDIR(info.st_mode),
            int(info.st_size),
        )
    except BaseException:
        return None


def _cleanup_created_descriptor(
    owner: _CreatedStageOwner,
    expected_identity: Any | None,
    parent_handle: Any,
    parent_identity: Any,
    parent_path: Path,
    name: str,
    *,
    expected_sha256: str | None,
    max_bytes: int,
) -> bool:
    """Remove only the object owned by the live creator handle/descriptor.

    Windows disposition is submitted directly to the raw creator HANDLE or to
    the native handle still owned by its wrapped descriptor; it never needs a
    captured identity or a pathname.  POSIX recovers a descriptor identity only
    from the open creator fd when the normal capture is unavailable, then uses
    the pinned-parent tombstone protocol.  If that recovery is uncertain it
    preserves the pathname rather than risking replacement deletion.
    """

    try:
        if os.name == "nt":
            path_boundary._set_file_disposition(owner.native_view())
            return True
        cleanup_identity = expected_identity
        if cleanup_identity is None:
            cleanup_identity = _posix_creator_identity(owner, parent_identity, name)
        if cleanup_identity is None:
            return False
        return bool(
            path_boundary._posix_unlink_exact(
                parent_handle,
                parent_path,
                name,
                cleanup_identity,
                expected_sha256=expected_sha256,
                max_bytes=max_bytes,
            )
        )
    except BaseException:
        # Cleanup must not replace a SystemExit/KeyboardInterrupt raised at the
        # stage seam.  An uncertain cleanup is fail-closed and leaves the object
        # for a later owner-aware recovery path instead of pathname deletion.
        return False


def _close_creator_descriptor(owner: _CreatedStageOwner | None) -> None:
    if owner is not None:
        owner.close()


def _create_stage_descriptor(
    path: Path,
    parent_handle: Any,
    parent_identity: Any,
) -> _CreatedStageOwner:
    """Create an exclusive stage and own it before any wrapper can fail."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if os.name != "nt":
        fd = os.open(
            path.name,
            flags,
            0o600,
            dir_fd=int(parent_handle.raw),
        )
        # The dirfd-relative create is already a mutation.  Register its owner
        # before any identity/fstat work can raise.
        return _CreatedStageOwner(fd=fd)

    import ctypes
    import msvcrt

    desired = (
        0x80000000  # GENERIC_READ
        | 0x40000000  # GENERIC_WRITE
        | 0x00010000  # DELETE
        | 0x00000080  # FILE_READ_ATTRIBUTES
        | 0x00040000  # WRITE_DAC
        | 0x00080000  # WRITE_OWNER
    )
    raw = path_boundary._CreateFileW(
        str(path),
        desired,
        0x1 | 0x2 | 0x4,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        None,
        1,  # CREATE_NEW
        0x80,  # FILE_ATTRIBUTE_NORMAL
        None,
    )
    raw_value = raw if isinstance(raw, int) else getattr(raw, "value", None)
    if raw_value in (None, path_boundary._INVALID_HANDLE_VALUE):
        raise OSError(ctypes.get_last_error() or 1, "stage creation failed")

    # Ownership begins at the successful CreateFileW return, before CRT
    # wrapping.  If wrapping raises, dispose this raw HANDLE directly.
    owner = _CreatedStageOwner(raw_handle=int(raw_value))
    try:
        wrapped_fd = msvcrt.open_osfhandle(
            int(raw_value), os.O_RDWR | getattr(os, "O_BINARY", 0)
        )
        owner.adopt_wrapped_descriptor(wrapped_fd)
        return owner
    except BaseException:
        _cleanup_created_descriptor(
            owner,
            None,
            parent_handle,
            parent_identity,
            path.parent,
            path.name,
            expected_sha256=None,
            max_bytes=MAX_ENROLLMENT_BYTES,
        )
        _close_creator_descriptor(owner)
        raise


def _set_private_creator_acl(fd: int) -> None:
    """Apply the private file ACL to the exact creator handle on Windows."""

    if os.name != "nt":
        return
    import ctypes
    import ctypes.wintypes as wintypes
    import msvcrt

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    set_security_info = advapi32.SetSecurityInfo
    set_security_info.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    set_security_info.restype = wintypes.DWORD
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    user_sid = path_boundary._current_user_sid_string()
    sddl = f"O:{user_sid}D:P(A;;FA;;;{path_boundary._SYSTEM_SID})(A;;FA;;;{user_sid})"
    if not path_boundary._ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        _fail("image_state_unavailable")
    try:
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        owner_defaulted = wintypes.BOOL()
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        if not path_boundary._GetSecurityDescriptorOwner(
            descriptor,
            ctypes.byref(owner),
            ctypes.byref(owner_defaulted),
        ) or not path_boundary._GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present.value or not dacl.value:
            _fail("image_state_unavailable")
        status = set_security_info(
            wintypes.HANDLE(msvcrt.get_osfhandle(fd)),
            path_boundary._SE_FILE_OBJECT,
            path_boundary._OWNER_SECURITY_INFORMATION
            | path_boundary._DACL_SECURITY_INFORMATION
            | path_boundary._PROTECTED_DACL_SECURITY_INFORMATION,
            owner,
            None,
            dacl,
            None,
        )
        if status:
            _fail("image_state_unavailable")
    finally:
        if descriptor.value:
            path_boundary._LocalFree(descriptor)


def _revalidate_parent_binding(parent_handle: Any, parent_identity: Any, parent_path: Path) -> None:
    """Bind pathname operations to the directory captured by ``parent_handle``.

    The protected Windows child primitive currently resolves ``parent_path``
    rather than using ``parent_handle``.  Every such operation therefore gets a
    fresh pathname open immediately before and after it; a stable mismatch is
    an unavailable state, not permission to read the pathname's object.
    """

    current_handle = None
    try:
        pinned = path_boundary._identity_from_handle(parent_handle, parent_path)
        current_handle, current = path_boundary._open_existing(parent_path, directory=True)
        if (
            not _identity_same(parent_identity, pinned)
            or not _identity_same(parent_identity, current)
            or not bool(getattr(current, "is_directory", False))
        ):
            _fail("image_state_unavailable")
    except ReceiptKeyEnrollmentError:
        raise
    except Exception:
        _fail("image_state_unavailable")
    finally:
        if current_handle is not None:
            _close(current_handle)


def _posix_lstat_regular(path: Path) -> os.stat_result:
    """Require a non-reparse regular leaf without opening it."""

    if os.name == "nt":
        raise RuntimeError("POSIX regular-file check requested on Windows")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError:
        _fail("image_state_unavailable")
    if not stat.S_ISREG(info.st_mode):
        _fail("image_state_unavailable")
    return info


def _open_posix_regular_child(
    parent_handle: Any,
    parent_path: Path,
    name: str,
) -> tuple[Any, Any]:
    """Open an evidence leaf without allowing special-file open/read blocking."""

    if os.name == "nt":
        raise RuntimeError("POSIX evidence open requested on Windows")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not nonblocking or not cloexec:
        _fail("image_state_unavailable")
    flags = os.O_RDONLY | nofollow | nonblocking | cloexec
    fd: int | None = None
    handle = None
    try:
        fd = os.open(name, flags, dir_fd=int(parent_handle.raw))
        # This is deliberately the first operation after open.  A FIFO/socket/
        # device is rejected before identity reads or any byte read can occur.
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            _fail("image_state_unavailable")
        handle = path_boundary._NativeHandle(fd, "file")
        fd = None
        # O_NONBLOCK is a finite-open guard only.  Remove it before reading a
        # proven regular file so regular-file behavior remains unchanged.
        import fcntl

        current_flags = fcntl.fcntl(int(handle.raw), fcntl.F_GETFL)
        if current_flags & nonblocking:
            fcntl.fcntl(
                int(handle.raw),
                fcntl.F_SETFL,
                current_flags & ~nonblocking,
            )
        identity = path_boundary._identity_from_handle(
            handle, parent_path / name
        )
        if bool(getattr(identity, "is_directory", True)):
            _fail("image_state_unavailable")
        return handle, identity
    except BaseException:
        if handle is not None:
            _close(handle)
        elif fd is not None:
            try:
                os.close(fd)
            except BaseException:
                pass
        raise


def _open_child_bound(
    parent_handle: Any,
    parent_identity: Any,
    parent_path: Path,
    name: str,
    *,
    directory: bool,
) -> tuple[Any, Any]:
    _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
    child = None
    try:
        if os.name != "nt" and not directory:
            child_path = parent_path / name
            _posix_lstat_regular(child_path)
            _before_posix_child_open(child_path, name)
            child, identity = _open_posix_regular_child(
                parent_handle,
                parent_path,
                name,
            )
        else:
            child, identity = path_boundary._open_child_existing(
                parent_handle, parent_path, name, directory=directory
            )
        _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
        return child, identity
    except BaseException:
        if child is not None:
            _close(child)
        raise


def _private_file(path: Path, handle: Any) -> bool:
    if os.name == "nt":
        try:
            path_boundary._require_private_windows_acl(path)
            return True
        except Exception:
            return False
    try:
        mode = stat.S_IMODE(os.fstat(int(handle.raw)).st_mode)
    except Exception:
        return False
    return mode == 0o600


def _read_handle(boundary: Any, handle: Any, maximum: int) -> bytes:
    try:
        raw = boundary.read_handle_bytes(handle, max_bytes=maximum)
    except Exception:
        _fail("image_state_unavailable")
    if type(raw) is not bytes or len(raw) > maximum:
        _fail("image_state_unavailable")
    return raw


def _read_child(
    paths: _Paths,
    parent_handle: Any,
    parent_identity: Any,
    parent_path: Path,
    name: str,
    *,
    maximum: int,
    missing_is_ok: bool,
) -> _FileSnapshot | None:
    boundary = path_boundary.PathBoundary(paths.home)
    first = second = third = None
    try:
        _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
        try:
            first, first_identity = _open_child_bound(
                parent_handle, parent_identity, parent_path, name, directory=False
            )
        except Exception as exc:
            if missing_is_ok and _is_missing_error(exc):
                raise _MissingFile from None
            _fail("image_state_unavailable")
        if bool(getattr(first_identity, "is_directory", True)):
            _fail("image_state_unavailable")
        if int(getattr(first_identity, "size", maximum + 1)) > maximum:
            _fail("image_state_unavailable")
        raw = _read_handle(boundary, first, maximum)
        _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
        after_first = path_boundary._identity_from_handle(first, parent_path / name)
        if not _identity_same(first_identity, after_first) or int(getattr(after_first, "size", -1)) != len(raw):
            _fail("image_state_unavailable")
        private = _private_file(parent_path / name, first)
        _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
        _close(first)
        first = None
        _readback_point(parent_path / name, name)
        try:
            second, second_identity = _open_child_bound(
                parent_handle, parent_identity, parent_path, name, directory=False
            )
        except Exception:
            _fail("image_state_unavailable")
        if int(getattr(second_identity, "size", maximum + 1)) > maximum:
            _fail("image_state_unavailable")
        raw_again = _read_handle(boundary, second, maximum)
        _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
        after_second = path_boundary._identity_from_handle(second, parent_path / name)
        _close(second)
        second = None
        _after_final_read_point(parent_path / name, name)
        try:
            third, third_identity = _open_child_bound(
                parent_handle, parent_identity, parent_path, name, directory=False
            )
        except Exception:
            _fail("image_state_unavailable")
        if int(getattr(third_identity, "size", maximum + 1)) > maximum:
            _fail("image_state_unavailable")
        raw_final = _read_handle(boundary, third, maximum)
        _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
        after_third = path_boundary._identity_from_handle(third, parent_path / name)
        if (
            not _identity_same(first_identity, second_identity)
            or not _identity_same(second_identity, after_second)
            or not _identity_same(second_identity, third_identity)
            or not _identity_same(third_identity, after_third)
            or int(getattr(second_identity, "size", -1)) != len(raw_again)
            or int(getattr(third_identity, "size", -1)) != len(raw_final)
            or raw_again != raw
            or raw_final != raw_again
            or hashlib.sha256(raw_final).hexdigest() != hashlib.sha256(raw_again).hexdigest()
        ):
            _fail("image_state_unavailable")
        return _FileSnapshot(
            identity=third_identity,
            raw=raw_final,
            digest=hashlib.sha256(raw_final).hexdigest(),
            size=len(raw_final),
            private=private,
        )
    except _MissingFile:
        return None
    finally:
        if third is not None:
            _close(third)
        if second is not None:
            _close(second)
        if first is not None:
            _close(first)


def _staging_max_bytes(name: str) -> int:
    if name.startswith(".bootstrap-pending."):
        return MAX_PENDING_BYTES
    if name.startswith(".enrollment-v1.json."):
        return MAX_ENROLLMENT_BYTES
    return MAX_STAGING_RESIDUE_BYTES


def _scan_staging_directory(
    paths: _Paths,
    parent_handle: Any,
    parent_identity: Any,
) -> _StagingInventory:
    """Validate bounded POSIX stage/residue names without parsing residue bytes."""

    if os.name == "nt":
        return _StagingInventory((), 0, 0, 0)
    names: list[str] = []
    active: list[str] = []
    residue_count = 0
    try:
        _revalidate_parent_binding(parent_handle, parent_identity, paths.staging_dir)
        with os.scandir(paths.staging_dir) as entries:
            for entry in entries:
                name = entry.name
                names.append(name)
                if len(names) > MAX_STAGING_ENTRIES:
                    _fail("image_state_unavailable")
                if _STAGING_STAGE_RE.fullmatch(name) is not None:
                    active.append(name)
                    if len(active) > MAX_STAGING_ACTIVE:
                        _fail("image_state_unavailable")
                elif _STAGING_RESIDUE_RE.fullmatch(name) is not None:
                    residue_count += 1
                    if residue_count > MAX_STAGING_RESIDUES:
                        _fail("image_state_unavailable")
                else:
                    _fail("image_state_unavailable")
                if (
                    entry.is_symlink()
                    or entry.is_dir(follow_symlinks=False)
                    or not entry.is_file(follow_symlinks=False)
                ):
                    _fail("image_state_unavailable")
                path_boundary._reject_reparse_components(paths.staging_dir / name)
        total_bytes = 0
        for name in names:
            snapshot = _read_child(
                paths,
                parent_handle,
                parent_identity,
                paths.staging_dir,
                name,
                maximum=_staging_max_bytes(name),
                missing_is_ok=False,
            )
            if snapshot is None:
                _fail("image_state_unavailable")
            if not snapshot.private:
                _fail("image_state_not_private")
            total_bytes += snapshot.size
            if total_bytes > MAX_STAGING_TOTAL_BYTES:
                _fail("image_state_unavailable")
        _revalidate_parent_binding(parent_handle, parent_identity, paths.staging_dir)
        return _StagingInventory(tuple(active), len(active), residue_count, total_bytes)
    except ReceiptKeyEnrollmentError:
        raise
    except Exception:
        _fail("image_state_unavailable")


def _ensure_staging_capacity(
    paths: _Paths,
    parent_handle: Any,
    parent_identity: Any,
    *,
    required_active: int,
    required_residues: int,
    required_bytes: int,
) -> _StagingInventory:
    inventory = _scan_staging_directory(paths, parent_handle, parent_identity)
    if (
        inventory.active_count + required_active > MAX_STAGING_ACTIVE
        or inventory.residue_count + required_residues > MAX_STAGING_RESIDUES
        or inventory.total_bytes + required_bytes > MAX_STAGING_TOTAL_BYTES
    ):
        _fail("image_state_unavailable")
    return inventory


def _recover_staging(
    paths: _Paths,
    staging_handle: Any,
    staging_identity: Any,
) -> None:
    """Exact-clean stale active stages; leave only validated tombstone residue."""

    if os.name == "nt":
        return
    inventory = _scan_staging_directory(paths, staging_handle, staging_identity)
    for name in inventory.active:
        inventory = _ensure_staging_capacity(
            paths,
            staging_handle,
            staging_identity,
            required_active=0,
            required_residues=2,
            required_bytes=0,
        )
        snapshot = _read_child(
            paths,
            staging_handle,
            staging_identity,
            paths.staging_dir,
            name,
            maximum=_staging_max_bytes(name),
            missing_is_ok=False,
        )
        if snapshot is None:
            _fail("image_state_unavailable")
        try:
            removed = path_boundary.unlink_handle_bound(
                paths.staging_dir / name,
                snapshot.identity,
                parent_expected=staging_identity,
                expected_sha256=snapshot.digest,
                max_bytes=_staging_max_bytes(name),
            )
        except Exception:
            removed = False
        _revalidate_parent_binding(staging_handle, staging_identity, paths.staging_dir)
        if not removed:
            _fail("image_state_unavailable")
        _flush_pinned_parent(staging_handle, staging_identity, paths.staging_dir)
        inventory = _scan_staging_directory(paths, staging_handle, staging_identity)
    del inventory


def _open_receipt_parent(paths: _Paths) -> tuple[Any, Any]:
    try:
        handle, identity = path_boundary._open_existing(paths.receipt_dir, directory=True)
        if not bool(getattr(identity, "is_directory", False)):
            _fail("image_state_unavailable")
        return handle, identity
    except ReceiptKeyEnrollmentError:
        raise
    except Exception:
        _fail("image_state_unavailable")


def _scan_receipt_directory(
    paths: _Paths,
    parent_handle: Any | None = None,
    parent_identity: Any | None = None,
) -> None:
    allowed = {ENROLLMENT_FILENAME, PENDING_FILENAME}
    try:
        if parent_handle is not None and parent_identity is not None:
            _revalidate_parent_binding(parent_handle, parent_identity, paths.receipt_dir)
        with os.scandir(paths.receipt_dir) as entries:
            count = 0
            for entry in entries:
                count += 1
                if count > MAX_RECEIPT_DIRECTORY_ENTRIES:
                    _fail("image_state_unavailable")
                if entry.name not in allowed:
                    _fail("image_state_unavailable")
                if os.name == "nt":
                    if entry.is_symlink() or entry.is_dir(follow_symlinks=False):
                        _fail("image_state_unavailable")
                    path_boundary._reject_reparse_components(
                        paths.receipt_dir / entry.name
                    )
                else:
                    try:
                        leaf_info = entry.stat(follow_symlinks=False)
                    except OSError:
                        _fail("image_state_unavailable")
                    if not stat.S_ISREG(leaf_info.st_mode):
                        _fail("image_state_unavailable")
        if parent_handle is not None and parent_identity is not None:
            _revalidate_parent_binding(parent_handle, parent_identity, paths.receipt_dir)
    except ReceiptKeyEnrollmentError:
        raise
    except Exception:
        _fail("image_state_unavailable")


def _read_key(paths: _Paths) -> _FileSnapshot | None:
    try:
        data_handle, data_identity = path_boundary._open_existing(paths.data_dir, directory=True)
    except Exception as exc:
        if _is_missing_error(exc):
            return None
        _fail("image_key_unavailable")
    try:
        return _read_child(
            paths,
            data_handle,
            data_identity,
            paths.data_dir,
            KEY_FILENAME,
            maximum=PROFILE_KEY_SIZE,
            missing_is_ok=True,
        )
    except ReceiptKeyEnrollmentError:
        raise ReceiptKeyEnrollmentError("image_key_unavailable") from None
    finally:
        _close(data_handle)


def _parse_record(snapshot: _FileSnapshot) -> ReceiptKeyEnrollmentEvidence:
    value = _parse_canonical(snapshot.raw, MAX_ENROLLMENT_BYTES)
    required = {
        "schema",
        "owner_scope",
        "enrollment_id",
        "enrollment_revision",
        "receipt_key_fingerprint",
        "key_length",
        "enrolled_at",
    }
    if set(value) != required:
        _fail("image_state_unavailable")
    if (
        value.get("schema") != ENROLLMENT_SCHEMA
        or value.get("owner_scope") != "profile"
        or type(value.get("enrollment_id")) is not str
        or _ENROLLMENT_ID_RE.fullmatch(value["enrollment_id"]) is None
        or type(value.get("enrollment_revision")) is not int
        or type(value.get("key_length")) is not int
        or value["enrollment_revision"] != 1
        or value["key_length"] != PROFILE_KEY_SIZE
        or type(value.get("receipt_key_fingerprint")) is not str
        or _SHA256_RE.fullmatch(value["receipt_key_fingerprint"]) is None
        or not _valid_timestamp(value.get("enrolled_at"))
    ):
        _fail("image_state_unavailable")
    # IMG-104 (lane w19-g2rel): a home copied with cp -r / Explorer loses the explicit private ACL of this file; that
    # is named (image_state_not_private) instead of the blanket image_state_unavailable.
    if not snapshot.private:
        _fail("image_state_not_private")
    return ReceiptKeyEnrollmentEvidence(
        schema=value["schema"],
        owner_scope=value["owner_scope"],
        enrollment_id=value["enrollment_id"],
        enrollment_revision=value["enrollment_revision"],
        receipt_key_fingerprint=value["receipt_key_fingerprint"],
        key_length=value["key_length"],
        enrolled_at=value["enrolled_at"],
    )


def _parse_pending(snapshot: _FileSnapshot) -> str:
    value = _parse_canonical(snapshot.raw, MAX_PENDING_BYTES)
    if set(value) != {"schema", "creator_id"}:
        _fail("image_state_unavailable")
    creator_id = value.get("creator_id")
    if value.get("schema") != PENDING_SCHEMA or type(creator_id) is not str or _PENDING_ID_RE.fullmatch(creator_id) is None:
        _fail("image_state_unavailable")
    if not snapshot.private:
        _fail("image_state_not_private")
    return creator_id


def _inspect(paths: _Paths) -> tuple[ReceiptKeyEnrollmentEvidence | None, _FileSnapshot | None, _FileSnapshot | None, _FileSnapshot | None, Any, Any]:
    parent_handle, parent_identity = _open_receipt_parent(paths)
    try:
        _scan_receipt_directory(paths, parent_handle, parent_identity)
        _after_initial_scan_point()
        record = _read_child(
            paths,
            parent_handle,
            parent_identity,
            paths.receipt_dir,
            ENROLLMENT_FILENAME,
            maximum=MAX_ENROLLMENT_BYTES,
            missing_is_ok=True,
        )
        pending = _read_child(
            paths,
            parent_handle,
            parent_identity,
            paths.receipt_dir,
            PENDING_FILENAME,
            maximum=MAX_PENDING_BYTES,
            missing_is_ok=True,
        )
        evidence = _parse_record(record) if record is not None else None
        if pending is not None:
            _parse_pending(pending)
        key_snapshot = _read_key(paths)
        _scan_receipt_directory(paths, parent_handle, parent_identity)
        return evidence, record, pending, key_snapshot, parent_handle, parent_identity
    except BaseException:
        _close(parent_handle)
        raise


def _key_bytes(snapshot: _FileSnapshot | None) -> bytes | None:
    if snapshot is None:
        return None
    if snapshot.size != PROFILE_KEY_SIZE or len(snapshot.raw) != PROFILE_KEY_SIZE:
        _fail("image_key_unavailable")
    if not snapshot.private:
        _fail("image_state_not_private")
    return snapshot.raw


def _validate_key_for_evidence(evidence: ReceiptKeyEnrollmentEvidence, key: bytes | None) -> bytes:
    actual = _key_bytes(key)
    if actual is None:
        _fail("image_key_unavailable")
    expected_id = "imgkey_" + hmac.new(actual, _ENROLLMENT_ID_DOMAIN, hashlib.sha256).hexdigest()[:32]
    fingerprint_matches = hashlib.sha256(actual).hexdigest() == evidence.receipt_key_fingerprint
    id_matches = hmac.compare_digest(evidence.enrollment_id, expected_id)
    if not fingerprint_matches and id_matches:
        # The key is still the enrolled key, so a changed fingerprint is
        # malformed enrollment evidence rather than a replaced key.
        _fail("image_state_unavailable")
    if not fingerprint_matches:
        _fail("image_key_unavailable")
    if not id_matches:
        _fail("image_state_unavailable")
    return actual


def _temp_name(prefix: str) -> str:
    return f".{prefix}.{secrets.token_hex(16)}.tmp"


def _write_stage(
    paths: _Paths,
    parent_handle: Any,
    parent_identity: Any,
    parent_path: Path,
    name: str,
    raw: bytes,
) -> _StageFile:
    maximum = _staging_max_bytes(name)
    if (
        type(raw) is not bytes
        or len(raw) > maximum
        or _STAGING_STAGE_RE.fullmatch(name) is None
    ):
        _fail("image_state_unavailable")
    if os.name != "nt":
        _ensure_staging_capacity(
            paths,
            parent_handle,
            parent_identity,
            required_active=1,
            required_residues=2,
            required_bytes=len(raw),
        )
    path = parent_path / name
    owner: _CreatedStageOwner | None = None
    created_identity = None
    handle = None
    try:
        _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
        _before_stage_path_resolution(path)
        owner = _create_stage_descriptor(path, parent_handle, parent_identity)
        fd = owner.descriptor_fd
        created_identity = _descriptor_identity(
            fd, _stage_fallback(parent_identity, name)
        )
        # The descriptor is the creator's authority.  No payload write or ACL
        # operation is allowed until both the object and its parent are proven
        # to be the pinned ones.
        _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
        _creator_matches(
            fd,
            created_identity,
            parent_identity,
            name,
            expected_size=0,
        )
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        else:
            # ACL application is handle-relative.  The protected verifier is
            # pathname-shaped, so it is called only after creator-descriptor
            # proof and is followed by the same proof; any rebind preserves the
            # exact creator object for descriptor cleanup and fails closed
            # before payload bytes are written.
            _set_private_creator_acl(fd)
            _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
            _creator_matches(
                fd,
                created_identity,
                parent_identity,
                name,
                expected_size=0,
            )
            path_boundary._require_private_windows_acl(path)
            _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
            _creator_matches(
                fd,
                created_identity,
                parent_identity,
                name,
                expected_size=0,
            )
        remaining = memoryview(raw)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                _fail("image_state_unavailable")
            remaining = remaining[written:]
        path_boundary._flush_private_file(fd)
        _creator_matches(
            fd,
            created_identity,
            parent_identity,
            name,
            expected_size=len(raw),
        )
        handle, identity = _open_child_bound(
            parent_handle, parent_identity, parent_path, name, directory=False
        )
        if (
            not _identity_same(created_identity, identity)
            or int(getattr(identity, "size", -1)) != len(raw)
        ):
            _fail("image_state_unavailable")
        stage = _StageFile(path, handle, identity, owner)
        owner = None
        return stage
    except ReceiptKeyEnrollmentError:
        raise
    except Exception:
        _fail("image_state_unavailable")
    finally:
        if owner is not None:
            cleanup_identity = created_identity
            cleanup_digest: str | None = None
            if cleanup_identity is not None:
                try:
                    cleanup_identity = _descriptor_identity(
                        owner.descriptor_fd, _stage_fallback(parent_identity, name)
                    )
                    if int(getattr(cleanup_identity, "size", -1)) == len(raw):
                        cleanup_digest = hashlib.sha256(raw).hexdigest()
                except BaseException:
                    cleanup_identity = None
            _cleanup_created_descriptor(
                owner,
                cleanup_identity,
                parent_handle,
                parent_identity,
                parent_path,
                name,
                expected_sha256=cleanup_digest,
                max_bytes=maximum,
            )
            _close_creator_descriptor(owner)


def _publish_no_replace(
    paths: _Paths,
    parent_handle: Any,
    parent_identity: Any,
    target: str,
    raw: bytes,
) -> tuple[Any, Any]:
    stage_parent, stage_parent_identity, stage_path, close_stage_parent = _open_stage_parent(
        paths, parent_handle, parent_identity
    )
    stage_name = _temp_name(target)
    stage: _StageFile | None = None
    published = False
    primary_error = False
    try:
        stage = _write_stage(
            paths,
            stage_parent,
            stage_parent_identity,
            stage_path,
            stage_name,
            raw,
        )
        try:
            _revalidate_parent_binding(parent_handle, parent_identity, paths.receipt_dir)
            _revalidate_parent_binding(stage_parent, stage_parent_identity, stage_path)
            if not _same_filesystem(parent_identity, stage_parent_identity):
                _fail("image_state_unavailable")
            if os.name == "nt":
                path_boundary.win_link_handle_no_replace(stage.handle, parent_handle, target)
            else:
                path_boundary._posix_linkat_empty_path(
                    int(stage.handle.raw), int(parent_handle.raw), target
                )
            _revalidate_parent_binding(parent_handle, parent_identity, paths.receipt_dir)
            _revalidate_parent_binding(stage_parent, stage_parent_identity, stage_path)
            published = True
            snapshot = _read_child(
                paths,
                parent_handle,
                parent_identity,
                paths.receipt_dir,
                target,
                maximum=MAX_ENROLLMENT_BYTES,
                missing_is_ok=False,
            )
            if (
                snapshot is None
                or snapshot.raw != raw
                or snapshot.size != len(raw)
                or snapshot.digest != hashlib.sha256(raw).hexdigest()
                or not _identity_same(snapshot.identity, stage.identity)
            ):
                _fail("image_state_unavailable")
            _revalidate_parent_binding(parent_handle, parent_identity, paths.receipt_dir)
            _flush_pinned_parent(parent_handle, parent_identity, paths.receipt_dir)
            return snapshot.identity, stage.identity
        except FileExistsError:
            primary_error = True
            raise
        except ReceiptKeyEnrollmentError:
            primary_error = True
            raise
        except Exception:
            primary_error = True
            _fail("image_state_unavailable")
        except BaseException:
            primary_error = True
            raise
    finally:
        if stage is not None:
            if stage.handle is not None:
                _close(stage.handle)
            removed = _cleanup_created_descriptor(
                stage.creator,
                stage.identity,
                stage_parent,
                stage_parent_identity,
                stage_path,
                stage_name,
                expected_sha256=hashlib.sha256(raw).hexdigest(),
                max_bytes=_staging_max_bytes(stage_name),
            )
            _close_creator_descriptor(stage.creator)
            if not removed and not primary_error:
                _fail("image_state_unavailable")
            if removed:
                if os.name != "nt":
                    _flush_pinned_parent(stage_parent, stage_parent_identity, stage_path)
                    _scan_staging_directory(paths, stage_parent, stage_parent_identity)
                elif published:
                    _flush_pinned_parent(parent_handle, parent_identity, paths.receipt_dir)
                if published:
                    _scan_receipt_directory(paths, parent_handle, parent_identity)
        if close_stage_parent:
            _close(stage_parent)


def _cleanup_pending_posix(
    paths: _Paths,
    receipt_handle: Any,
    receipt_identity: Any,
    pending: _FileSnapshot,
    staging_handle: Any,
    staging_identity: Any,
) -> None:
    """Move a creator-owned marker out before conservative exact cleanup.

    The cross-directory ``RENAME_NOREPLACE`` is atomic: after a crash the
    receipt name is either still the verified marker or absent, while the
    verified marker may be an active stage in the private sibling directory.
    The protected exact unlink then quarantines any unavoidable POSIX residue
    in that sibling rather than poisoning the closed receipt directory.
    """

    stage_path = paths.staging_dir
    _ensure_staging_capacity(
        paths,
        staging_handle,
        staging_identity,
        required_active=1,
        required_residues=2,
        required_bytes=len(pending.raw),
    )
    if not _same_filesystem(receipt_identity, staging_identity):
        _fail("image_state_unavailable")
    current = _read_child(
        paths,
        receipt_handle,
        receipt_identity,
        paths.receipt_dir,
        PENDING_FILENAME,
        maximum=MAX_PENDING_BYTES,
        missing_is_ok=False,
    )
    if (
        current is None
        or not _identity_same(current.identity, pending.identity)
        or current.raw != pending.raw
        or current.digest != pending.digest
    ):
        _fail("image_state_unavailable")

    moved_name = _temp_name("bootstrap-pending")
    _revalidate_parent_binding(receipt_handle, receipt_identity, paths.receipt_dir)
    _revalidate_parent_binding(staging_handle, staging_identity, stage_path)
    _posix_rename_between(
        receipt_handle,
        PENDING_FILENAME,
        staging_handle,
        moved_name,
        0x1,  # RENAME_NOREPLACE
    )
    _revalidate_parent_binding(receipt_handle, receipt_identity, paths.receipt_dir)
    _revalidate_parent_binding(staging_handle, staging_identity, stage_path)

    moved = _read_child(
        paths,
        staging_handle,
        staging_identity,
        stage_path,
        moved_name,
        maximum=MAX_PENDING_BYTES,
        missing_is_ok=False,
    )
    if (
        moved is None
        or not _identity_same(moved.identity, pending.identity)
        or moved.raw != pending.raw
        or moved.digest != pending.digest
    ):
        _fail("image_state_unavailable")
    if (
        _read_child(
            paths,
            receipt_handle,
            receipt_identity,
            paths.receipt_dir,
            PENDING_FILENAME,
            maximum=MAX_PENDING_BYTES,
            missing_is_ok=True,
        )
        is not None
    ):
        _fail("image_state_unavailable")

    removed = path_boundary.unlink_handle_bound(
        stage_path / moved_name,
        moved.identity,
        parent_expected=staging_identity,
        expected_sha256=moved.digest,
        max_bytes=MAX_PENDING_BYTES,
    )
    if not removed:
        _fail("image_state_unavailable")
    _revalidate_parent_binding(receipt_handle, receipt_identity, paths.receipt_dir)
    _revalidate_parent_binding(staging_handle, staging_identity, stage_path)
    if (
        _read_child(
            paths,
            receipt_handle,
            receipt_identity,
            paths.receipt_dir,
            PENDING_FILENAME,
            maximum=MAX_PENDING_BYTES,
            missing_is_ok=True,
        )
        is not None
    ):
        _fail("image_state_unavailable")
    _flush_pinned_parent(receipt_handle, receipt_identity, paths.receipt_dir)
    _flush_pinned_parent(staging_handle, staging_identity, stage_path)
    _scan_staging_directory(paths, staging_handle, staging_identity)


def _cleanup_pending(paths: _Paths, parent_identity: Any, pending: _FileSnapshot) -> None:
    receipt_handle = _parent_for_publish(paths, parent_identity)
    staging_handle, staging_identity, staging_path, close_staging = _open_stage_parent(
        paths,
        receipt_handle,
        parent_identity,
    )
    try:
        if os.name == "nt":
            try:
                removed = path_boundary.unlink_handle_bound(
                    paths.pending_path,
                    pending.identity,
                    parent_expected=parent_identity,
                    expected_sha256=pending.digest,
                    max_bytes=MAX_PENDING_BYTES,
                )
            except Exception:
                removed = False
            if not removed:
                _fail("image_state_unavailable")
            _flush_pinned_parent(receipt_handle, parent_identity, paths.receipt_dir)
        else:
            _cleanup_pending_posix(
                paths,
                receipt_handle,
                parent_identity,
                pending,
                staging_handle,
                staging_identity,
            )
    finally:
        if close_staging:
            _close(staging_handle)
        _close(receipt_handle)


def _before_posix_child_open(_path: Path, _name: str) -> None:
    """Private no-op seam immediately before a safe POSIX evidence open."""


def _before_stage_path_resolution(_path: Path) -> None:
    """Private no-op seam immediately before exclusive stage creation."""


def _fault_point(_name: str) -> None:
    """Private no-op seam used only by real crash-schedule tests."""


def _readback_point(_path: Path, _name: str) -> None:
    """Private no-op seam used only by real identity-race tests."""


def _after_initial_scan_point() -> None:
    """Private no-op seam after the first bounded breadth scan."""


def _after_final_read_point(_path: Path, _name: str) -> None:
    """Private no-op seam after the final bytes and before pathname reopen."""


def _load_existing(paths: _Paths) -> ReceiptKeyEnrollmentResult:
    lock = _open_lock(paths)
    try:
        evidence, _record, _pending, key_snapshot, parent, parent_identity = _inspect(paths)
        try:
            if evidence is None:
                _fail("image_key_unavailable")
            key = _validate_key_for_evidence(evidence, key_snapshot)
            _scan_receipt_directory(paths, parent, parent_identity)
            return ReceiptKeyEnrollmentResult(evidence, key)
        finally:
            _close(parent)
    finally:
        lock.close()


def _open_or_bootstrap(hermes_home: os.PathLike[str] | str | None) -> ReceiptKeyEnrollmentResult:
    paths = _paths(hermes_home)
    _prepare_profile_directories(paths)

    lock = _open_lock(paths)
    created_pending: _FileSnapshot | None = None
    try:
        # Recover only bounded, exact stage names while the profile lock is
        # held.  Recovery never scans or mutates the closed receipt directory.
        recovery_receipt, recovery_receipt_identity = _open_receipt_parent(paths)
        try:
            recovery_staging, recovery_staging_identity, _stage_path, close_recovery_staging = _open_stage_parent(
                paths,
                recovery_receipt,
                recovery_receipt_identity,
            )
            try:
                _recover_staging(
                    paths,
                    recovery_staging,
                    recovery_staging_identity,
                )
            finally:
                if close_recovery_staging:
                    _close(recovery_staging)
        finally:
            _close(recovery_receipt)
        evidence, _record, pending, key_snapshot, parent, parent_identity = _inspect(paths)
        try:
            if evidence is not None:
                key = _validate_key_for_evidence(evidence, key_snapshot)
                return ReceiptKeyEnrollmentResult(evidence, key)
            if pending is not None:
                if key_snapshot is None:
                    # Pending-before-key is a deliberate no-mutation refusal.
                    return _fail("image_key_unavailable")
                _parse_pending(pending)
                key = _key_bytes(key_snapshot)
                if key is None:
                    _fail("image_key_unavailable")
                # A recovery owner never calls the high-level helper.
                return _finish_enrollment(
                    paths,
                    key,
                    pending,
                    parent_identity,
                    creator_pending=False,
                )
            if key_snapshot is not None:
                # A key without the fence is incomplete evidence: it was not
                # reserved by us, so it never becomes the enrolled key - a
                # planted key would otherwise inherit this profile's authority.
                #
                # What it guards decides whether we may move past it.  A
                # profile from before enrollment existed carries such a key
                # while guarding nothing; refusing it outright would brick the
                # whole Shoot surface, because opening a project builds the
                # image root.  So: nothing enrolled under this key yet means we
                # bootstrap a fresh one over it; anything already signed by it
                # is a refusal, unchanged.
                if _profile_state_exists(paths):
                    _fail("image_key_unavailable")
                # Nothing is enrolled under it, so the key is inert.  Retire it
                # under the held lock and fall through to a normal bootstrap:
                # the enrolled key stays one we reserved ourselves, which is
                # what keeps a planted key from inheriting authority.
                _retire_unenrolled_key(paths)
            pending_raw = _canonical(
                {"creator_id": "boot_" + secrets.token_hex(16), "schema": PENDING_SCHEMA}
            )
            stage_parent, stage_parent_identity, stage_path, close_stage_parent = _open_stage_parent(
                paths,
                parent,
                parent_identity,
            )
            stage: _StageFile | None = None
            primary_error = False
            try:
                stage_name = _temp_name("bootstrap-pending")
                stage = _write_stage(
                    paths,
                    stage_parent,
                    stage_parent_identity,
                    stage_path,
                    stage_name,
                    pending_raw,
                )
                try:
                    _revalidate_parent_binding(parent, parent_identity, paths.receipt_dir)
                    _revalidate_parent_binding(stage_parent, stage_parent_identity, stage_path)
                    if not _same_filesystem(parent_identity, stage_parent_identity):
                        _fail("image_state_unavailable")
                    if os.name == "nt":
                        path_boundary.win_link_handle_no_replace(
                            stage.handle, parent, PENDING_FILENAME
                        )
                    else:
                        path_boundary._posix_linkat_empty_path(
                            int(stage.handle.raw), int(parent.raw), PENDING_FILENAME
                        )
                    _revalidate_parent_binding(parent, parent_identity, paths.receipt_dir)
                    _revalidate_parent_binding(stage_parent, stage_parent_identity, stage_path)
                    created_pending = _read_child(
                        paths,
                        parent,
                        parent_identity,
                        paths.receipt_dir,
                        PENDING_FILENAME,
                        maximum=MAX_PENDING_BYTES,
                        missing_is_ok=False,
                    )
                    if (
                        created_pending is None
                        or created_pending.raw != pending_raw
                        or created_pending.digest != hashlib.sha256(pending_raw).hexdigest()
                        or not _identity_same(created_pending.identity, stage.identity)
                    ):
                        _fail("image_state_unavailable")
                    _revalidate_parent_binding(parent, parent_identity, paths.receipt_dir)
                    _flush_pinned_parent(parent, parent_identity, paths.receipt_dir)
                except FileExistsError:
                    primary_error = True
                    # A no-replace winner remains authoritative; this caller
                    # must not treat the collision as an enrollment success.
                    _fail("image_key_unavailable")
                except ReceiptKeyEnrollmentError:
                    primary_error = True
                    raise
                except Exception:
                    primary_error = True
                    _fail("image_state_unavailable")
                except BaseException:
                    primary_error = True
                    raise
            finally:
                if stage is not None:
                    if stage.handle is not None:
                        _close(stage.handle)
                    removed = _cleanup_created_descriptor(
                        stage.creator,
                        stage.identity,
                        stage_parent,
                        stage_parent_identity,
                        stage_path,
                        stage_name,
                        expected_sha256=hashlib.sha256(pending_raw).hexdigest(),
                        max_bytes=_staging_max_bytes(stage_name),
                    )
                    _close_creator_descriptor(stage.creator)
                    if not removed and not primary_error:
                        _fail("image_state_unavailable")
                    if removed:
                        if os.name != "nt":
                            _flush_pinned_parent(
                                stage_parent, stage_parent_identity, stage_path
                            )
                            _scan_staging_directory(
                                paths, stage_parent, stage_parent_identity
                            )
                        else:
                            _flush_pinned_parent(parent, parent_identity, paths.receipt_dir)
                if close_stage_parent:
                    _close(stage_parent)
            _fault_point("after-pending")
        finally:
            _close(parent)
    finally:
        lock.close()

    # The named lock is explicitly released before this helper.  The helper
    # obtains the same named profile lock internally and may create the key.
    try:
        key = path_boundary.profile_identity_key(hermes_home=paths.home)
    except ReceiptKeyEnrollmentError:
        raise
    except Exception:
        _fail("image_key_unavailable")
    if type(key) is not bytes or len(key) != PROFILE_KEY_SIZE:
        _fail("image_key_unavailable")
    # Keep the helper result only as ephemeral creator evidence.  The durable
    # authority is the strict key snapshot acquired after the lock is
    # reacquired below; the helper bytes are never passed to publication.
    bootstrap_key = key
    del key
    _fault_point("after-key")

    lock = _open_lock(paths)
    try:
        evidence, _record, pending, key_snapshot, parent, parent_identity = _inspect(paths)
        try:
            try:
                actual_key = _key_bytes(key_snapshot)
                if actual_key is None or not hmac.compare_digest(bootstrap_key, actual_key):
                    _fail("image_key_unavailable")
            finally:
                # Bytes are immutable in Python; dropping this reference limits
                # its lifetime without claiming memory erasure.
                del bootstrap_key
            if evidence is not None:
                _validate_key_for_evidence(evidence, key_snapshot)
                if created_pending is not None:
                    _cleanup_pending(paths, parent_identity, created_pending)
                _scan_receipt_directory(paths, parent, parent_identity)
                return ReceiptKeyEnrollmentResult(evidence, actual_key)
            if pending is None:
                _fail("image_state_unavailable")
            _parse_pending(pending)
            if actual_key is None:
                _fail("image_key_unavailable")
            return _finish_enrollment(
                paths,
                actual_key,
                created_pending or pending,
                parent_identity,
                creator_pending=created_pending is not None,
            )
        finally:
            _close(parent)
    finally:
        lock.close()


def _finish_enrollment(
    paths: _Paths,
    key: bytes,
    pending: _FileSnapshot,
    parent_identity: Any,
    *,
    creator_pending: bool,
) -> ReceiptKeyEnrollmentResult:
    if type(key) is not bytes or len(key) != PROFILE_KEY_SIZE:
        _fail("image_key_unavailable")
    fingerprint = hashlib.sha256(key).hexdigest()
    enrolled_at = _utc_second()
    record = {
        "enrollment_id": "imgkey_" + hmac.new(key, _ENROLLMENT_ID_DOMAIN, hashlib.sha256).hexdigest()[:32],
        "enrollment_revision": 1,
        "enrolled_at": enrolled_at,
        "key_length": PROFILE_KEY_SIZE,
        "owner_scope": "profile",
        "receipt_key_fingerprint": fingerprint,
        "schema": ENROLLMENT_SCHEMA,
    }
    if _ENROLLMENT_ID_RE.fullmatch(record["enrollment_id"]) is None or not _valid_timestamp(enrolled_at):
        _fail("image_state_unavailable")
    raw = _canonical(record)
    parent_handle = _parent_for_publish(paths, parent_identity)
    try:
        _publish_no_replace(paths, parent_handle, parent_identity, ENROLLMENT_FILENAME, raw)
    except FileExistsError:
        # The no-replace winner is authoritative.  Strictly reload it and
        # never publish a second record.
        record_snapshot = _read_record_after_publish(paths)
        record_evidence = _parse_record(record_snapshot)
        record_key = _validate_key_for_evidence(record_evidence, _read_key(paths))
        record_result = ReceiptKeyEnrollmentResult(record_evidence, record_key)
        if creator_pending:
            collision_parent, parent_identity_again = _open_receipt_parent(paths)
            try:
                current_pending = _read_child(
                    paths,
                    collision_parent,
                    parent_identity_again,
                    paths.receipt_dir,
                    PENDING_FILENAME,
                    maximum=MAX_PENDING_BYTES,
                    missing_is_ok=False,
                )
                if current_pending is None or not _identity_same(current_pending.identity, pending.identity):
                    _fail("image_state_unavailable")
                _cleanup_pending(paths, parent_identity_again, pending)
            finally:
                _close(collision_parent)
        _final_scan_receipt_directory(paths, parent_identity)
        return record_result
    finally:
        _close(parent_handle)
    if creator_pending:
        _cleanup_pending(paths, parent_identity, pending)
    evidence = _parse_record(
        _read_record_after_publish(paths)
    )
    if evidence.receipt_key_fingerprint != fingerprint:
        _fail("image_state_unavailable")
    _final_scan_receipt_directory(paths, parent_identity)
    return ReceiptKeyEnrollmentResult(evidence, key)


def _parent_for_publish(paths: _Paths, expected_identity: Any) -> Any:
    try:
        handle, identity = path_boundary._open_existing(paths.receipt_dir, directory=True)
        if not _identity_same(expected_identity, identity):
            _close(handle)
            _fail("image_state_unavailable")
        return handle
    except Exception:
        _fail("image_state_unavailable")


def _final_scan_receipt_directory(paths: _Paths, expected_identity: Any) -> None:
    handle = _parent_for_publish(paths, expected_identity)
    try:
        _scan_receipt_directory(paths, handle, expected_identity)
    finally:
        _close(handle)


def _flush_pinned_parent(parent_handle: Any, parent_identity: Any, parent_path: Path) -> None:
    try:
        _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
        if os.name == "nt":
            # The protected boundary does not expose a safe directory-handle
            # FlushFileBuffers primitive.  Its pathname flush is used only
            # inside this pinned/current identity envelope.
            path_boundary._fsync_parent(parent_path)
        else:
            os.fsync(int(parent_handle.raw))
        _revalidate_parent_binding(parent_handle, parent_identity, parent_path)
    except Exception:
        _fail("image_state_unavailable")


def _read_record_after_publish(paths: _Paths) -> _FileSnapshot:
    handle, identity = _open_receipt_parent(paths)
    try:
        snapshot = _read_child(
            paths,
            handle,
            identity,
            paths.receipt_dir,
            ENROLLMENT_FILENAME,
            maximum=MAX_ENROLLMENT_BYTES,
            missing_is_ok=False,
        )
        if snapshot is None:
            _fail("image_state_unavailable")
        _scan_receipt_directory(paths, handle, identity)
        return snapshot
    finally:
        _close(handle)


class ReceiptKeyEnrollment:
    """Stateless entry point for the profile enrollment prerequisite."""

    @classmethod
    def open(cls, hermes_home: os.PathLike[str] | str | None = None) -> ReceiptKeyEnrollmentResult:
        try:
            return _open_or_bootstrap(hermes_home)
        except ReceiptKeyEnrollmentError:
            raise
        except Exception:
            _fail("image_state_unavailable")

    bootstrap = open


def enroll_profile_receipt_key(hermes_home: os.PathLike[str] | str | None = None) -> ReceiptKeyEnrollmentResult:
    return ReceiptKeyEnrollment.open(hermes_home)


def bootstrap_profile_receipt_key(hermes_home: os.PathLike[str] | str | None = None) -> ReceiptKeyEnrollmentResult:
    return ReceiptKeyEnrollment.open(hermes_home)


load_or_enroll = enroll_profile_receipt_key
open_receipt_key_enrollment = enroll_profile_receipt_key


__all__ = [
    "ENROLLMENT_SCHEMA",
    "PENDING_SCHEMA",
    "PROFILE_KEY_SIZE",
    "ReceiptKeyEnrollment",
    "ReceiptKeyEnrollmentError",
    "ReceiptKeyEnrollmentEvidence",
    "ReceiptKeyEnrollmentResult",
    "bootstrap_profile_receipt_key",
    "enroll_profile_receipt_key",
    "load_or_enroll",
    "open_receipt_key_enrollment",
]
