"""Fail-closed local path and handle boundaries for Film Suite projects.

This module is deliberately stdlib-only.  It never turns a user supplied path
into an authorization token: existing objects are opened, checked for
reparse/symlink components, and identified by the opened object before they
are used.  The public media object is opaque; its native handle/descriptor is
kept here until a bounded consume or abort closes it.
"""

from __future__ import annotations


import contextlib
import ctypes
import ctypes.wintypes as wintypes
import errno
import os
if os.name != "nt":
    import fcntl
else:  # pragma: no cover - Windows has no fcntl module
    fcntl = None  # type: ignore[assignment]
import hashlib
import hmac
import json
import ntpath
import os
import re
import secrets
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

try:
    from hermes_constants import get_hermes_home
except ImportError:  # pragma: no cover - repository tests install Hermes' root
    def get_hermes_home() -> Path:
        return Path.home() / "AppData" / "Local" / "hermes"
# Native stores also load this stdlib module directly, outside a package.
import importlib.util as _migration_import
_migration_spec = _migration_import.spec_from_file_location(
    "film_suite_path_migration", Path(__file__).resolve().parents[1] / "generation" / "legacy_plugin_migration.py"
)
_migration_module = _migration_import.module_from_spec(_migration_spec)
_migration_spec.loader.exec_module(_migration_module)
state_root = _migration_module.state_root
TOMBSTONE_PATTERN = _migration_module.TOMBSTONE_PATTERN
# Lane w34-sec (scan findings 5, 6): the one lexical guard against UNC / device paths, loaded by file like the
# migration module above (this file is also loaded outside its package).
_local_path_spec = _migration_import.spec_from_file_location(
    "film_suite_path_boundary_local_path", Path(__file__).resolve().with_name("local_path.py")
)
_local_path = _migration_import.module_from_spec(_local_path_spec)
_local_path_spec.loader.exec_module(_local_path)
# One minimal environment for every child process (lane w39-env): dashboard/engine_process, loaded by path.
import importlib.util as _child_env_import
_child_env_spec = _child_env_import.spec_from_file_location(
    "film_suite_engine_process", Path(__file__).resolve().parents[1] / "engine_process" / "__init__.py")
_child_env_module = _child_env_import.module_from_spec(_child_env_spec)
_child_env_spec.loader.exec_module(_child_env_module)
child_env = _child_env_module.child_env


WINDOWS_JUNCTION_PRIVILEGE_UNAVAILABLE = "WINDOWS_JUNCTION_PRIVILEGE_UNAVAILABLE"
PROFILE_KEY_SIZE = 32
IMAGE_LIMIT = 10 * 1024 * 1024
VIDEO_LIMIT = 50 * 1024 * 1024
MAX_INPUTS = 10

_PROJECT_ID_RE = re.compile(r"^prj_[0-9a-f]{32}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


class ProjectError(ValueError):
    """Finite, public-safe failure for every project boundary violation."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.reason_code = code
        self.reason = code
        super().__init__(message or code)


@dataclass(frozen=True)
class PathIdentity:
    """Identity captured from an open object, not merely from its spelling."""

    canonical_path: str
    volume_serial: int
    file_id: int
    is_directory: bool
    size: int
    missing_leaf: bool = False

    @property
    def token(self) -> str:
        raw = (
            f"{self.canonical_path}\0{self.volume_serial}\0{self.file_id}\0"
            f"{int(self.is_directory)}\0{self.size}"
        ).encode("utf-8", "surrogatepass")
        return hashlib.sha256(raw).hexdigest()

    def same_object(self, other: "PathIdentity") -> bool:
        if self.missing_leaf or other.missing_leaf:
            return (
                self.missing_leaf
                and other.missing_leaf
                and self.volume_serial == other.volume_serial
                and self.file_id == other.file_id
                and self.canonical_path == other.canonical_path
            )
        return (
            self.volume_serial == other.volume_serial
            and self.file_id == other.file_id
            and self.is_directory == other.is_directory
        )


class _NativeHandle:
    __slots__ = ("raw", "kind", "_closed", "_directory_scan_started")

    def __init__(self, raw: Any, kind: str) -> None:
        self.raw = raw
        self.kind = kind
        self._closed = False
        self._directory_scan_started = False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        if os.name == "nt":
            _CloseHandle(self.raw)
        else:
            os.close(int(self.raw))
        self._closed = True


def close_native_handle_safely(handle: _NativeHandle) -> None:
    """Close a lease handle from either explicit cleanup or GC finalization."""

    with contextlib.suppress(Exception):
        handle.close()


@dataclass
class _LeaseRecord:
    handle: _NativeHandle
    identity: PathIdentity
    size: int
    limit: int
    finalizer: weakref.finalize


_PINNED_MEDIA_GUARD = object()


class PinnedMedia:
    """Opaque, one-shot media lease containing only safe public metadata."""

    __slots__ = (
        "field",
        "index",
        "canonical_identity",
        "size",
        "safe_suffix",
        "__weakref__",
    )

    def __init__(
        self,
        *,
        field: str,
        index: int,
        identity: PathIdentity,
        size: int,
        safe_suffix: str,
        _guard: object,
    ) -> None:
        if _guard is not _PINNED_MEDIA_GUARD:
            raise TypeError("PinnedMedia leases are created by PathBoundary")
        object.__setattr__(self, "field", field)
        object.__setattr__(self, "index", index)
        object.__setattr__(self, "canonical_identity", identity.token)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "safe_suffix", safe_suffix)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("PinnedMedia is immutable")


# Win32 constants and declarations are kept out of import-time execution on
# POSIX, where ctypes.windll does not exist.
if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CreateFileW.restype = wintypes.HANDLE
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _CloseHandle.restype = wintypes.BOOL
    _FlushFileBuffers = _kernel32.FlushFileBuffers
    _FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _FlushFileBuffers.restype = wintypes.BOOL
    _GetCurrentProcess = _kernel32.GetCurrentProcess
    _GetCurrentProcess.restype = wintypes.HANDLE
    _GetFileAttributesW = _kernel32.GetFileAttributesW
    _GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
    _GetFileAttributesW.restype = wintypes.DWORD
    _GetFinalPathNameByHandleW = _kernel32.GetFinalPathNameByHandleW
    _GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _GetFileInformationByHandle = _kernel32.GetFileInformationByHandle
    _GetFileInformationByHandleEx = _kernel32.GetFileInformationByHandleEx
    _GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _GetFileInformationByHandleEx.restype = wintypes.BOOL
    _GetFileSizeEx = _kernel32.GetFileSizeEx
    _GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
    _GetFileSizeEx.restype = wintypes.BOOL
    _ReadFile = _kernel32.ReadFile
    _ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _ReadFile.restype = wintypes.BOOL
    _CreateMutexW = _kernel32.CreateMutexW
    _CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    _CreateMutexW.restype = wintypes.HANDLE
    _MoveFileExW = _kernel32.MoveFileExW
    _MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _MoveFileExW.restype = wintypes.BOOL
    _ntdll = ctypes.WinDLL("ntdll", use_last_error=False)
    _NtOpenFile = _ntdll.NtOpenFile
    _NtOpenFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _NtOpenFile.restype = ctypes.c_long
    _NtCreateFile = _ntdll.NtCreateFile
    _NtCreateFile.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    _NtCreateFile.restype = ctypes.c_long
    _NtSetInformationFile = _ntdll.NtSetInformationFile
    _NtSetInformationFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _NtSetInformationFile.restype = ctypes.c_long
    _WaitForSingleObject = _kernel32.WaitForSingleObject
    _WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _WaitForSingleObject.restype = wintypes.DWORD
    _ReleaseMutex = _kernel32.ReleaseMutex
    _ReleaseMutex.argtypes = [wintypes.HANDLE]
    _ReleaseMutex.restype = wintypes.BOOL
    _ERROR_ALREADY_EXISTS = 183
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_FILE_EXISTS = 80
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _FILE_READ_ATTRIBUTES = 0x80
    _SYNCHRONIZE = 0x00100000
    _FILE_READ_DATA = 0x0001
    _FILE_ADD_FILE = 0x0002
    _FILE_SHARE_READ = 0x1
    _FILE_SHARE_WRITE = 0x2
    _FILE_SHARE_DELETE = 0x4
    _OPEN_EXISTING = 3
    _FILE_CREATE = 2
    _FILE_ATTRIBUTE_NORMAL = 0x80
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_NAME_NORMALIZED = 0x0
    _VOLUME_NAME_GUID = 0x1
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 258
    _INFINITE = 0xFFFFFFFF
    _MOVEFILE_WRITE_THROUGH = 0x8

    class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _IO_STATUS_BLOCK(ctypes.Structure):
        _fields_ = [
            ("Status", ctypes.c_long),
            ("Information", ctypes.c_size_t),
        ]

    class _FILE_LINK_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", ctypes.c_wchar * 1),
        ]

    class _FILE_DISPOSITION_INFORMATION(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    class _UNICODE_STRING(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _OBJECT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", ctypes.c_void_p),
            ("SecurityQualityOfService", ctypes.c_void_p),
        ]

    class _FILE_ID_BOTH_DIR_INFO(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", wintypes.DWORD),
            ("FileIndex", wintypes.DWORD),
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("AllocationSize", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
            ("FileNameLength", wintypes.DWORD),
            ("EaSize", wintypes.DWORD),
            ("ShortNameLength", ctypes.c_ubyte),
            ("ShortName", wintypes.WCHAR * 12),
            ("FileId", ctypes.c_longlong),
            ("FileName", wintypes.WCHAR * 1),
        ]

    _NtQueryDirectoryFile = _ntdll.NtQueryDirectoryFile
    _NtQueryDirectoryFile.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(_IO_STATUS_BLOCK),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
        wintypes.BOOLEAN,
        ctypes.c_void_p,
        wintypes.BOOLEAN,
    ]
    _NtQueryDirectoryFile.restype = ctypes.c_long

    _FILE_LINK_INFORMATION_CLASS = 11
    _FILE_DISPOSITION_INFORMATION_CLASS = 13
    _FILE_ID_BOTH_DIRECTORY_INFO_CLASS = 10
    _FILE_ID_BOTH_DIRECTORY_INFORMATION_CLASS = 37
    _OBJ_CASE_INSENSITIVE = 0x40
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_DIRECTORY_FILE = 0x00000001
    _FILE_NON_DIRECTORY_FILE = 0x00000040
    _FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
    _FILE_OPEN_REPARSE_POINT = 0x00200000
    _STATUS_OBJECT_NAME_COLLISION = 0xC0000035
    _STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
    _STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
    _STATUS_SHARING_VIOLATION = 0xC0000043
    _STATUS_ACCESS_DENIED = 0xC0000022
    _STATUS_CANNOT_DELETE = 0xC0000121
    _STATUS_REPARSE_POINT_ENCOUNTERED = 0xC000050B
    _STATUS_IO_REPARSE_TAG_NOT_HANDLED = 0xC0000279
    _STATUS_BUFFER_OVERFLOW = 0x80000005
    _STATUS_NO_MORE_FILES = 0x80000006
    _STATUS_NO_SUCH_FILE = 0xC000000F
    _ERROR_NO_MORE_FILES = 18
    _ERROR_HANDLE_EOF = 38

    _GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
    ]
    _GetFileInformationByHandle.restype = wintypes.BOOL

    _GetCurrentProcessToken = _advapi32.OpenProcessToken
    _GetCurrentProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _GetCurrentProcessToken.restype = wintypes.BOOL
    _GetTokenInformation = _advapi32.GetTokenInformation
    _GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _GetTokenInformation.restype = wintypes.BOOL
    _ConvertSidToStringSidW = _advapi32.ConvertSidToStringSidW
    _ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    _ConvertSidToStringSidW.restype = wintypes.BOOL
    _ConvertStringSecurityDescriptorToSecurityDescriptorW = (
        _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    )
    _ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _GetSecurityDescriptorOwner = _advapi32.GetSecurityDescriptorOwner
    _GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _GetSecurityDescriptorOwner.restype = wintypes.BOOL
    _GetSecurityDescriptorDacl = _advapi32.GetSecurityDescriptorDacl
    _GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _GetSecurityDescriptorControl = _advapi32.GetSecurityDescriptorControl
    _GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _GetSecurityDescriptorControl.restype = wintypes.BOOL
    _GetNamedSecurityInfoW = _advapi32.GetNamedSecurityInfoW
    _GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _GetNamedSecurityInfoW.restype = wintypes.DWORD
    _SetNamedSecurityInfoW = _advapi32.SetNamedSecurityInfoW
    _SetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _SetNamedSecurityInfoW.restype = wintypes.DWORD
    _GetAclInformation = _advapi32.GetAclInformation
    _GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    _GetAclInformation.restype = wintypes.BOOL
    _GetAce = _advapi32.GetAce
    _GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    _GetAce.restype = wintypes.BOOL
    _LocalFree = _kernel32.LocalFree
    _LocalFree.argtypes = [ctypes.c_void_p]
    _LocalFree.restype = ctypes.c_void_p

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", _SID_AND_ATTRIBUTES)]

    class _ACE_HEADER(ctypes.Structure):
        _fields_ = [("AceType", wintypes.BYTE), ("AceFlags", wintypes.BYTE), ("AceSize", wintypes.WORD)]

    class _ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_DACL_PRESENT = 0x00000004
    _SE_DACL_PROTECTED = 0x00001000
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER_CLASS = 1
    _ACL_SIZE_INFORMATION_CLASS = 2
    _ACCESS_ALLOWED_ACE_TYPE = 0
    _OBJECT_INHERIT_ACE = 0x01
    _CONTAINER_INHERIT_ACE = 0x02
    _INHERITED_ACE = 0x10
    _FILE_ALL_ACCESS = 0x001F01FF
    _SYSTEM_SID = "S-1-5-18"



def _raise(code: str, message: str | None = None) -> "NoReturn":
    raise ProjectError(code, message)


def _path_text(value: os.PathLike[str] | str) -> str:
    if isinstance(value, bytes):
        _raise("invalid_project", "path must be text")
    try:
        text = os.fspath(value)
    except TypeError:
        _raise("invalid_project", "path must be text")
    if not isinstance(text, str):
        _raise("invalid_project", "path must be text")
    return text


def _identity_text(text: str) -> str:
    normalized = unicodedata.normalize("NFC", text)
    return normalized.casefold() if os.name == "nt" else normalized


def _reject_path_spelling(text: str) -> None:
    if "\x00" in text or any(ord(char) < 32 or ord(char) == 127 for char in text):
        _raise("invalid_project", "path contains forbidden characters")
    drive, tail = ntpath.splitdrive(text)
    if text.startswith(("\\\\?\\", "\\\\.\\", "\\\\?/") ):
        _raise("invalid_project", "extended/device paths are not local paths")
    # Lane w34-sec (finding 5): //host/share, /\\host and \\/host are UNC paths too; refused on the text, before abspath.
    if _local_path.remote_path_reason(text) is not None:
        _raise("invalid_project", "path must be an absolute local path")
    if text.startswith("\\\\") or (drive and not tail.startswith(("\\", "/"))):
        _raise("invalid_project", "path must be an absolute local path")
    if not drive and not os.path.isabs(text):
        _raise("invalid_project", "path must be absolute")
    if ":" in tail:
        _raise("invalid_project", "path contains a reserved component")
    components = [part for part in re.split(r"[\\/]", tail) if part not in ("", ".")]
    for component in components:
        if component == "..":
            _raise("invalid_project", "parent path components are not allowed")
        if component.endswith((".", " ")):
            _raise("invalid_project", "path has a trailing dot or space")
        if component.casefold().split(".", 1)[0] in _WINDOWS_RESERVED:
            _raise("invalid_project", "path contains a reserved component")


def _strip_extended_prefix(text: str) -> str:
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def _normalise_text_path(text: str) -> Path:
    _reject_path_spelling(text)
    if os.name == "nt":
        text = _strip_extended_prefix(text)
        return Path(os.path.abspath(text))
    return Path(os.path.abspath(text))


def _posix_open_walk(
    path: Path,
    *,
    final_directory: bool | None,
    require_final: bool,
) -> list[_NativeHandle]:
    """Walk an absolute POSIX path through descriptor-relative no-follow opens."""
    if os.name == "nt":
        raise RuntimeError("POSIX descriptor walk requested on Windows")
    base_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    try:
        current_fd = os.open(path.anchor or os.sep, base_flags | directory_flag | nofollow)
    except OSError:
        _raise("project_unavailable", "path could not be opened")
    handles = [_NativeHandle(current_fd, "directory")]
    components = [part for part in path.parts if part not in (path.anchor, "", ".")]
    try:
        for index, component in enumerate(components):
            is_final = index == len(components) - 1
            flags = base_flags | nofollow
            if not is_final or final_directory is True:
                flags |= directory_flag
            try:
                child_fd = os.open(component, flags, dir_fd=current_fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    _raise("invalid_project", "symlink components are not allowed")
                if not require_final:
                    break
                _raise("project_unavailable", "path could not be opened")
            handles.append(_NativeHandle(child_fd, "directory" if not is_final or final_directory else "file"))
            current_fd = child_fd
        if require_final and len(handles) != len(components) + 1:
            _raise("project_unavailable", "path could not be opened")
        return handles
    except Exception:
        for handle in handles:
            handle.close()
        raise


def _posix_component_reparse(path: Path) -> bool:
    handles = _posix_open_walk(path, final_directory=None, require_final=False)
    for handle in handles:
        handle.close()
    return False


def _win_component_reparse(path: str) -> bool:
    if os.name != "nt":
        return False
    try:
        handles = _win_walk_components(Path(path), final_directory=None, media=False, require_final=False)
    except ProjectError as exc:
        if exc.code == "invalid_project":
            raise
        return False
    finally:
        for handle in locals().get("handles", ()):
            handle.close()
    return False


def _win_walk_components(
    path: Path,
    *,
    final_directory: bool | None,
    media: bool,
    require_final: bool,
    delete_source: bool = False,
) -> list[_NativeHandle]:
    """Open every existing Windows component as a non-following handle."""
    if os.name != "nt":
        raise RuntimeError("Windows component walk requested on POSIX")
    native_path = _strip_extended_prefix(str(path))
    if len(native_path) >= 240 and not native_path.startswith("\\\\?\\"):
        native_path = "\\\\?\\" + native_path
    drive, tail = ntpath.splitdrive(native_path)
    current = drive + "\\\\" if drive else "\\\\"
    components = [part for part in re.split(r"[\\\\/]", tail) if part]
    handles: list[_NativeHandle] = []
    try:
        for index, component in enumerate(components):
            is_final = index == len(components) - 1
            current = ntpath.join(current, component)
            directory = bool(final_directory) if is_final and final_directory is not None else not is_final
            share = (
                _FILE_SHARE_READ | _FILE_SHARE_DELETE
                if delete_source and is_final
                else _FILE_SHARE_READ
                if media and is_final
                else _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE
            )
            desired = (
                _GENERIC_READ | _DELETE
                if delete_source and is_final
                else (
                    _FILE_LIST_DIRECTORY | _FILE_ADD_FILE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
                    if is_final and directory
                    else _FILE_READ_ATTRIBUTES | (_GENERIC_READ if is_final else 0)
                )
            )
            raw = _CreateFileW(
                current,
                desired,
                share,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if raw in (None, _INVALID_HANDLE_VALUE):
                error = ctypes.get_last_error()
                if not require_final or error in (_ERROR_FILE_NOT_FOUND, 3):
                    break
                _raise("project_unavailable", "path could not be opened")
            handle = _NativeHandle(raw, "directory" if directory else "file")
            info = _BY_HANDLE_FILE_INFORMATION()
            if not _GetFileInformationByHandle(raw, ctypes.byref(info)):
                handle.close()
                _raise("project_unavailable", "path identity unavailable")
            if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                handle.close()
                _raise("invalid_project", "reparse components are not allowed")
            handles.append(handle)
        if require_final and (not components or len(handles) != len(components)):
            _raise("project_unavailable", "path could not be opened")
        return handles
    except Exception:
        for handle in handles:
            handle.close()
        raise


def _win_open_handle(
    path: Path,
    *,
    directory: bool,
    media: bool,
    allow_reparse: bool,
    delete_source: bool = False,
) -> _NativeHandle:
    if os.name != "nt":
        raise RuntimeError("Windows handle requested on POSIX")
    if allow_reparse:
        raise ValueError("reparse handles are not supported")
    handles = _win_walk_components(
        path,
        final_directory=directory,
        media=media,
        require_final=True,
        delete_source=delete_source,
    )
    final = handles.pop()
    for ancestor in handles:
        ancestor.close()
    return final


def _reject_reparse_components(path: Path) -> None:
    if os.name == "nt":
        if _win_component_reparse(str(path)):
            _raise("invalid_project", "reparse components are not allowed")
    elif _posix_component_reparse(path):
        _raise("invalid_project", "symlink components are not allowed")


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        handle: _NativeHandle | None = None
        try:
            _reject_reparse_components(path)
            expected_handle, expected_identity = _open_existing(path, directory=True)
            expected_handle.close()

            native_path = str(path)
            if len(native_path) >= 240 and not native_path.startswith("\\\\?\\"):
                native_path = "\\\\?\\" + native_path
            raw = _CreateFileW(
                native_path,
                _GENERIC_WRITE | _FILE_READ_ATTRIBUTES,
                _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
                None,
                _OPEN_EXISTING,
                _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
            if raw in (None, _INVALID_HANDLE_VALUE):
                raise OSError(ctypes.get_last_error() or errno.EIO, "parent directory could not be opened")
            handle = _NativeHandle(raw, "directory")
            actual_identity = _identity_from_handle(handle, path)
            if not actual_identity.is_directory or not expected_identity.same_object(actual_identity):
                raise OSError(errno.EIO, "parent directory identity changed")
            if not _FlushFileBuffers(handle.raw):
                raise OSError(ctypes.get_last_error() or errno.EIO, "parent flush failed")
        except ProjectError:
            _raise("project_io_failed", "parent flush failed")
        except Exception:
            _raise("project_io_failed", "parent flush failed")
        finally:
            if handle is not None:
                handle.close()
        return
    expected_handle: _NativeHandle | None = None
    actual_handle: _NativeHandle | None = None
    try:
        expected_handle, expected_identity = _open_existing(path, directory=True)
        actual_handle, actual_identity = _open_existing(path, directory=True)
        if not actual_identity.is_directory or not expected_identity.same_object(actual_identity):
            raise OSError(errno.EIO, "parent directory identity changed")
        os.fsync(int(actual_handle.raw))
    except ProjectError:
        _raise("project_io_failed", "parent flush failed")
    except OSError:
        _raise("project_io_failed", "parent flush failed")
    finally:
        if actual_handle is not None:
            actual_handle.close()
        if expected_handle is not None:
            expected_handle.close()


def _flush_private_file(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        native = msvcrt.get_osfhandle(fd)
        if native == -1 or not _FlushFileBuffers(wintypes.HANDLE(native)):
            raise OSError(ctypes.get_last_error(), "file flush failed")
        return
    os.fsync(fd)


def _fsync_directory_handle(handle: _NativeHandle) -> None:
    """Flush one already-pinned directory handle without resolving a path."""

    _require_directory_handle(handle)
    try:
        if os.name == "nt":
            if not _FlushFileBuffers(handle.raw):
                raise OSError(ctypes.get_last_error() or errno.EIO, "directory flush failed")
        else:
            os.fsync(int(handle.raw))
    except OSError:
        raise
    except Exception as exc:
        raise OSError(errno.EIO, "directory flush failed") from exc


def _identity_from_handle(handle: _NativeHandle, fallback: Path) -> PathIdentity:
    if os.name == "nt":
        info = _BY_HANDLE_FILE_INFORMATION()
        if not _GetFileInformationByHandle(handle.raw, ctypes.byref(info)):
            _raise("project_unavailable", "path identity unavailable")
        attributes = int(info.dwFileAttributes)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            _raise("invalid_project", "reparse object is not allowed")
        buffer = ctypes.create_unicode_buffer(32768)
        length = _GetFinalPathNameByHandleW(
            handle.raw, buffer, len(buffer), _FILE_NAME_NORMALIZED | _VOLUME_NAME_GUID
        )
        if not length or length >= len(buffer):
            _raise("project_unavailable", "path canonical identity unavailable")
        final_path = _strip_extended_prefix(buffer.value)
        final_path = _identity_text(final_path).replace("/", "\\")
        size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
        file_id = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        return PathIdentity(final_path, int(info.dwVolumeSerialNumber), file_id, bool(attributes & _FILE_ATTRIBUTE_DIRECTORY), size)
    info = os.fstat(int(handle.raw))
    return PathIdentity(
        _identity_text(str(fallback.resolve(strict=False))),
        int(info.st_dev),
        int(info.st_ino),
        stat.S_ISDIR(info.st_mode),
        int(info.st_size),
    )


def _open_existing(path: Path, *, directory: bool, media: bool = False) -> tuple[_NativeHandle, PathIdentity]:
    if os.name == "nt":
        handle = _win_open_handle(path, directory=directory, media=media, allow_reparse=False)
        try:
            identity = _identity_from_handle(handle, path)
            if identity.is_directory != directory:
                _raise("invalid_project", "path type is not allowed")
            return handle, identity
        except Exception:
            handle.close()
            raise
    handles = _posix_open_walk(path, final_directory=directory, require_final=True)
    final = handles.pop()
    for ancestor in handles:
        ancestor.close()
    try:
        identity = _identity_from_handle(final, path)
        if identity.is_directory != directory:
            _raise("invalid_project", "path type is not allowed")
    except Exception:
        final.close()
        raise
    return final, identity


def win_link_handle_no_replace(source: _NativeHandle, parent: _NativeHandle, name: str) -> None:
    """Create a hard link from an open source into an open parent directory.

    The NT native FileLinkInformation primitive accepts a directory handle as
    RootDirectory and resolves the supplied name relative to that handle.  No
    pathname is used for publication, and ReplaceIfExists is permanently false.
    """

    if os.name != "nt":
        raise RuntimeError("Windows handle link requested on POSIX")
    if source.closed or parent.closed:
        raise ValueError("cannot link with a closed handle")
    if not isinstance(name, str) or not name or "\x00" in name or any(
        separator in name for separator in ("\\", "/")
    ):
        raise ValueError("relative link name is invalid")
    encoded_name = name.encode("utf-16-le", "strict")
    filename_offset = _FILE_LINK_INFORMATION.FileName.offset
    size = filename_offset + len(encoded_name)
    buffer = ctypes.create_string_buffer(size)
    information = ctypes.cast(buffer, ctypes.POINTER(_FILE_LINK_INFORMATION)).contents
    information.ReplaceIfExists = 0
    information.RootDirectory = parent.raw
    information.FileNameLength = len(encoded_name)
    ctypes.memmove(
        ctypes.addressof(buffer) + filename_offset,
        encoded_name,
        len(encoded_name),
    )
    io_status = _IO_STATUS_BLOCK()
    status = int(
        _NtSetInformationFile(
            source.raw,
            ctypes.byref(io_status),
            ctypes.cast(buffer, ctypes.c_void_p),
            size,
            _FILE_LINK_INFORMATION_CLASS,
        )
    ) & 0xFFFFFFFF
    if status == 0:
        return
    if status == _STATUS_OBJECT_NAME_COLLISION:
        raise FileExistsError(errno.EEXIST, "link target already exists", name)
    if status in {_STATUS_OBJECT_PATH_NOT_FOUND, _STATUS_OBJECT_NAME_NOT_FOUND}:
        raise FileNotFoundError(errno.ENOENT, "link parent or source is unavailable", name)
    raise OSError(errno.EIO, "native hard-link publication failed", name)


def _hash_handle(handle: _NativeHandle, *, max_bytes: int) -> tuple[str, int]:
    """Hash the bytes observed through one already-pinned handle/descriptor."""

    digest = hashlib.sha256()
    total = 0
    while total <= max_bytes:
        size = min(1024 * 1024, max_bytes - total + 1)
        if os.name == "nt":
            buffer = ctypes.create_string_buffer(size)
            read = wintypes.DWORD()
            if not _ReadFile(handle.raw, buffer, size, ctypes.byref(read), None):
                raise OSError(ctypes.get_last_error() or errno.EIO, "file hash failed")
            chunk = buffer.raw[: read.value]
        else:
            chunk = os.read(int(handle.raw), size)
        if not chunk:
            return digest.hexdigest(), total
        total += len(chunk)
        if total > max_bytes:
            raise OSError(errno.EFBIG, "file hash exceeds bound")
        digest.update(chunk)
    raise OSError(errno.EFBIG, "file hash exceeds bound")


def _set_file_disposition(handle: _NativeHandle) -> None:
    """Mark exactly ``handle`` for deletion without resolving a pathname."""

    if os.name != "nt":
        raise RuntimeError("Windows file disposition requested on POSIX")
    if handle.closed:
        raise ValueError("cannot dispose a closed handle")
    information = _FILE_DISPOSITION_INFORMATION()
    information.DeleteFile = 1
    io_status = _IO_STATUS_BLOCK()
    raw_status = _NtSetInformationFile(
        handle.raw,
        ctypes.byref(io_status),
        ctypes.byref(information),
        ctypes.sizeof(information),
        _FILE_DISPOSITION_INFORMATION_CLASS,
    )
    status = ctypes.c_ulong(raw_status).value
    if status == 0:
        return
    if status in {_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND}:
        raise FileNotFoundError(errno.ENOENT, "file is unavailable")
    if status in {_STATUS_SHARING_VIOLATION, _STATUS_ACCESS_DENIED, _STATUS_CANNOT_DELETE}:
        raise PermissionError(errno.EACCES, "file cannot be deleted")
    raise OSError(errno.EIO, "native file disposition failed")


def _same_posix_object(info: os.stat_result, identity: PathIdentity) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and int(info.st_dev) == identity.volume_serial
        and int(info.st_ino) == identity.file_id
        and int(info.st_size) == identity.size
    )


_AT_FDCWD = -100
_AT_SYMLINK_FOLLOW = 0x400
_AT_EMPTY_PATH = 0x1000
_RENAME_NOREPLACE = 0x1
_RENAME_EXCHANGE = 0x2


def _posix_proc_fd_matches_source(source_fd: int, proc_path: bytes) -> None:
    """Prove that ``/proc/self/fd/<fd>`` still names the opened regular object."""

    try:
        source_info = os.fstat(int(source_fd))
        proc_info = os.stat(proc_path)
    except OSError as exc:
        raise OSError(errno.EIO, "proc fd source is unavailable") from exc
    if (
        not stat.S_ISREG(source_info.st_mode)
        or not stat.S_ISREG(proc_info.st_mode)
        or int(source_info.st_dev) != int(proc_info.st_dev)
        or int(source_info.st_ino) != int(proc_info.st_ino)
        or int(source_info.st_size) != int(proc_info.st_size)
    ):
        raise OSError(errno.EIO, "proc fd source identity changed")


def _posix_linkat_empty_path(source_fd: int, parent_fd: int, name: str) -> None:
    """Link an opened source into a pinned parent without resolving its pathname.

    Linux may deny ``AT_EMPTY_PATH`` to an otherwise unprivileged process even
    when the source descriptor is valid.  Only those capability-style errors
    use the descriptor-bound ``/proc/self/fd`` spelling with
    ``AT_SYMLINK_FOLLOW``; all other failures remain fail-closed.
    """

    if os.name == "nt":
        raise RuntimeError("POSIX linkat requested on Windows")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        linkat = libc.linkat
    except (AttributeError, OSError) as exc:
        raise OSError(errno.ENOSYS, "AT_EMPTY_PATH linkat is unavailable") from exc
    linkat.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    linkat.restype = ctypes.c_int
    encoded_name = ctypes.c_char_p(os.fsencode(name))
    result = linkat(
        int(source_fd),
        ctypes.c_char_p(b""),
        int(parent_fd),
        encoded_name,
        _AT_EMPTY_PATH,
    )
    if result == 0:
        return
    error = ctypes.get_errno() or errno.EIO
    if error == errno.EEXIST:
        raise FileExistsError(error, "link target already exists", name)
    if error not in {errno.ENOENT, errno.EPERM, errno.EACCES}:
        raise OSError(error, "AT_EMPTY_PATH linkat failed", name)

    proc_path = os.fsencode(f"/proc/self/fd/{int(source_fd)}")
    _posix_proc_fd_matches_source(int(source_fd), proc_path)
    result = linkat(
        _AT_FDCWD,
        ctypes.c_char_p(proc_path),
        int(parent_fd),
        encoded_name,
        _AT_SYMLINK_FOLLOW,
    )
    if result == 0:
        return
    error = ctypes.get_errno() or errno.EIO
    if error == errno.EEXIST:
        raise FileExistsError(error, "link target already exists", name)
    raise OSError(error, "proc fd linkat fallback failed", name)


def _posix_renameat2(parent_fd: int, source_name: str, target_name: str, flags: int) -> None:
    """Apply one renameat2 operation to names below a pinned parent fd."""

    if os.name == "nt":
        raise RuntimeError("POSIX renameat2 requested on Windows")
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = libc.renameat2
    except (AttributeError, OSError) as exc:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        int(parent_fd),
        ctypes.c_char_p(os.fsencode(source_name)),
        int(parent_fd),
        ctypes.c_char_p(os.fsencode(target_name)),
        int(flags),
    )
    if result != 0:
        error = ctypes.get_errno() or errno.EIO
        if error == errno.EEXIST and flags == _RENAME_NOREPLACE:
            raise FileExistsError(error, "rename target already exists", target_name)
        if error in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
            raise OSError(errno.ENOSYS, "requested renameat2 operation is unavailable")
        raise OSError(error, "renameat2 failed", source_name)


def _posix_rename_noreplace(parent_fd: int, source_name: str, target_name: str) -> None:
    """Move one checked private name without replacing a destination."""

    _posix_renameat2(parent_fd, source_name, target_name, _RENAME_NOREPLACE)


def _posix_rename_exchange(parent_fd: int, source_name: str, placeholder_name: str) -> None:
    """Atomically exchange two names in one already-open parent directory."""

    _posix_renameat2(parent_fd, source_name, placeholder_name, _RENAME_EXCHANGE)


_POSIX_TOMBSTONE_RE = re.compile(TOMBSTONE_PATTERN)


def _posix_create_tombstone(
    parent_handle: _NativeHandle,
    parent_path: Path,
    source_name: str,
) -> tuple[str, _NativeHandle, PathIdentity]:
    """Create a private, same-parent empty tombstone without pathname cleanup."""

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    for _attempt in range(16):
        name = f".film-suite-tombstone-{secrets.token_hex(16)}.bin"
        if name == source_name:
            continue
        try:
            fd = os.open(name, flags, 0o600, dir_fd=int(parent_handle.raw))
        except FileExistsError:
            continue
        handle = _NativeHandle(fd, "file")
        try:
            identity = _identity_from_handle(handle, parent_path / name)
            if identity.is_directory or identity.size != 0:
                raise OSError(errno.EIO, "tombstone is not an empty regular file")
            return name, handle, identity
        except Exception:
            # The name is private and the object is still descriptor-pinned.
            # Preserve it rather than pathname-unlinking an unchecked name.
            handle.close()
            raise
    raise FileExistsError(errno.EEXIST, "owned tombstone name collision")


# Kept as a compatibility alias for injected callers from the previous
# implementation; the generated name is now always a tombstone name.
_posix_create_placeholder = _posix_create_tombstone


def _posix_verify_owned_name(
    parent_handle: _NativeHandle,
    parent_path: Path,
    name: str,
    expected: PathIdentity,
    *,
    expected_sha256: str | None,
    max_bytes: int,
) -> bool:
    """Verify one name by reopening it below the pinned parent."""

    handle: _NativeHandle | None = None
    try:
        handle, observed = _open_child_existing(parent_handle, parent_path, name, directory=False)
        if (
            not expected.same_object(observed)
            or expected.size != observed.size
            or observed.is_directory
            or observed.size > max_bytes
        ):
            return False
        actual_hash, actual_size = _hash_handle(handle, max_bytes=max_bytes)
        if actual_size != expected.size or (
            expected_sha256 is not None and actual_hash != expected_sha256
        ):
            return False
        final = _identity_from_handle(handle, parent_path / name)
        return expected.same_object(final) and expected.size == final.size
    except (FileNotFoundError, OSError, ProjectError):
        return False
    finally:
        if handle is not None:
            handle.close()


def _posix_move_owned_placeholder(
    parent_handle: _NativeHandle,
    parent_path: Path,
    source_name: str,
    expected: PathIdentity,
    *,
    expected_sha256: str,
    max_bytes: int,
) -> bool:
    """Move an owned source-name placeholder to a private name, no-replace."""

    if not _posix_verify_owned_name(
        parent_handle,
        parent_path,
        source_name,
        expected,
        expected_sha256=expected_sha256,
        max_bytes=max_bytes,
    ):
        return False
    for _attempt in range(16):
        destination = f".film-suite-tombstone-{secrets.token_hex(16)}.bin"
        if destination == source_name or not _POSIX_TOMBSTONE_RE.fullmatch(destination):
            continue
        try:
            _posix_rename_noreplace(int(parent_handle.raw), source_name, destination)
        except FileExistsError:
            continue
        except (OSError, ProjectError, ValueError):
            return False
        if _posix_verify_owned_name(
            parent_handle,
            parent_path,
            destination,
            expected,
            expected_sha256=expected_sha256,
            max_bytes=max_bytes,
        ):
            try:
                source_handle, _source_identity = _open_child_existing(
                    parent_handle, parent_path, source_name, directory=False
                )
            except ProjectError as exc:
                if exc.code == "project_unavailable":
                    return True
                return False
            else:
                source_handle.close()
                # A rebind won the final move race.  Preserve both names and
                # report failure; in particular, never replace or unlink the
                # foreign source object.
                return False
        # The destination was not proven to contain our placeholder.  Roll it
        # back only if the destination still carries our identity and source is
        # absent; otherwise leave both names untouched and fail closed.
        if not _posix_verify_owned_name(
            parent_handle,
            parent_path,
            destination,
            expected,
            expected_sha256=expected_sha256,
            max_bytes=max_bytes,
        ):
            return False
        try:
            source_handle, _source_identity = _open_child_existing(
                parent_handle, parent_path, source_name, directory=False
            )
        except ProjectError as exc:
            if exc.code != "project_unavailable":
                return False
        else:
            source_handle.close()
            return False
        try:
            _posix_rename_noreplace(int(parent_handle.raw), destination, source_name)
        except (OSError, ProjectError, ValueError):
            return False
        return _posix_verify_owned_name(
            parent_handle,
            parent_path,
            source_name,
            expected,
            expected_sha256=expected_sha256,
            max_bytes=max_bytes,
        )
    return False


def _posix_rollback_exchange(
    parent_handle: _NativeHandle,
    parent_path: Path,
    source_name: str,
    placeholder_name: str,
    placeholder_identity: PathIdentity,
    expected: PathIdentity,
    *,
    max_bytes: int,
) -> bool:
    """Exchange back without deleting either displaced object."""

    source_handle: _NativeHandle | None = None
    displaced_handle: _NativeHandle | None = None
    displaced_identity: PathIdentity | None = None
    try:
        source_handle, source_identity = _open_child_existing(
            parent_handle, parent_path, source_name, directory=False
        )
        if not placeholder_identity.same_object(source_identity) or source_identity.size != placeholder_identity.size:
            return False
        displaced_handle, displaced_identity = _open_child_existing(
            parent_handle, parent_path, placeholder_name, directory=False
        )
        # The displaced object may be the expected object, a mutation, or a
        # foreign regular file.  Exchange-back is safe for all of those while
        # both names are still present; it performs no irreversible cleanup.
        if displaced_identity.is_directory:
            return False
        _posix_rename_exchange(
            int(parent_handle.raw), source_name, placeholder_name
        )
    except (FileNotFoundError, OSError, ProjectError):
        return False
    finally:
        if displaced_handle is not None:
            displaced_handle.close()
        if source_handle is not None:
            source_handle.close()
    if displaced_identity is None:
        return False
    try:
        restored_handle, restored_identity = _open_child_existing(
            parent_handle, parent_path, source_name, directory=False
        )
        restored_handle.close()
        return displaced_identity.same_object(restored_identity)
    except (FileNotFoundError, OSError, ProjectError):
        return False


def _posix_unlink_exact(
    parent_handle: _NativeHandle,
    parent_path: Path,
    source_name: str,
    expected: PathIdentity,
    *,
    expected_sha256: str | None,
    max_bytes: int,
) -> bool:
    """Logically remove one exact source through conservative tombstones.

    POSIX has no unlink-by-open-descriptor primitive.  The checked source is
    exchanged with an exclusive same-parent tombstone, then the displaced
    object is verified by descriptor identity and hash.  A successful exchange
    leaves the expected object quarantined under that private tombstone and
    moves only our empty placeholder to another private no-replace name.  No
    checked object is pathname-unlinked.
    """

    source_handle: _NativeHandle | None = None
    placeholder_handle: _NativeHandle | None = None
    placeholder_name: str | None = None
    placeholder_identity: PathIdentity | None = None
    exchanged = False
    displaced_verified = False
    moved_placeholder = False
    initial_hash: str | None = None
    try:
        source_handle, observed = _open_child_existing(
            parent_handle, parent_path, source_name, directory=False
        )
        if (
            not expected.same_object(observed)
            or expected.size != observed.size
            or observed.is_directory
            or observed.size > max_bytes
        ):
            return False
        initial_hash, initial_size = _hash_handle(source_handle, max_bytes=max_bytes)
        if initial_size != expected.size or (
            expected_sha256 is not None and initial_hash != expected_sha256
        ):
            return False
        final_source = _identity_from_handle(source_handle, parent_path / source_name)
        if not expected.same_object(final_source) or expected.size != final_source.size:
            return False
    except (FileNotFoundError, OSError, ProjectError):
        return False
    finally:
        if source_handle is not None:
            source_handle.close()

    try:
        placeholder_name, placeholder_handle, placeholder_identity = _posix_create_tombstone(
            parent_handle, parent_path, source_name
        )
        _posix_rename_exchange(
            int(parent_handle.raw), source_name, placeholder_name
        )
        exchanged = True

        displaced_handle: _NativeHandle | None = None
        try:
            displaced_handle, displaced_identity = _open_child_existing(
                parent_handle, parent_path, placeholder_name, directory=False
            )
            if (
                not expected.same_object(displaced_identity)
                or expected.size != displaced_identity.size
            ):
                return False
            displaced_hash, displaced_size = _hash_handle(
                displaced_handle, max_bytes=max_bytes
            )
            if displaced_size != expected.size or displaced_hash != initial_hash:
                return False
            if expected_sha256 is not None and displaced_hash != expected_sha256:
                return False
            final_displaced = _identity_from_handle(
                displaced_handle, parent_path / placeholder_name
            )
            if not expected.same_object(final_displaced) or expected.size != final_displaced.size:
                return False
            displaced_verified = True
        finally:
            if displaced_handle is not None:
                displaced_handle.close()

        if not displaced_verified or placeholder_identity is None or initial_hash is None:
            return False
        moved_placeholder = _posix_move_owned_placeholder(
            parent_handle,
            parent_path,
            source_name,
            placeholder_identity,
            expected_sha256=hashlib.sha256(b"").hexdigest(),
            max_bytes=0,
        )
        return moved_placeholder
    except (FileNotFoundError, OSError, ProjectError, ValueError):
        return False
    finally:
        if exchanged and not moved_placeholder and placeholder_name is not None and placeholder_identity is not None:
            _posix_rollback_exchange(
                parent_handle,
                parent_path,
                source_name,
                placeholder_name,
                placeholder_identity,
                expected,
                max_bytes=max_bytes,
            )
        if placeholder_handle is not None:
            placeholder_handle.close()


def unlink_handle_bound(
    path: Path,
    expected: PathIdentity,
    *,
    parent_expected: PathIdentity | None = None,
    expected_sha256: str | None = None,
    max_bytes: int,
    allow_size_change: bool = False,
) -> bool:
    """Delete only the object pinned by ``expected``.

    Windows keeps DELETE access on the source handle and submits file
    disposition to NT on that exact handle.  POSIX uses ``renameat2`` exchange
    with a same-parent owned placeholder; it never uses a stat-plus-unlink
    protocol against the source name.
    """

    if expected.missing_leaf:
        return False
    parent_handle: _NativeHandle | None = None
    source_handle: _NativeHandle | None = None
    try:
        parent_handle, observed_parent = _open_existing(path.parent, directory=True)
        if parent_expected is not None and not parent_expected.same_object(observed_parent):
            return False
        if os.name == "nt":
            source_handle = _win_open_handle(
                path,
                directory=False,
                media=False,
                allow_reparse=False,
                delete_source=True,
            )
        else:
            source_handle, observed_source = _open_child_existing(
                parent_handle,
                path.parent,
                path.name,
                directory=False,
            )
        observed_source = _identity_from_handle(source_handle, path)
        if not expected.same_object(observed_source) or observed_source.is_directory:
            return False
        effective_expected = expected
        if allow_size_change:
            effective_expected = PathIdentity(
                expected.canonical_path,
                expected.volume_serial,
                expected.file_id,
                expected.is_directory,
                observed_source.size,
            )
        elif expected.size != observed_source.size:
            return False
        if observed_source.size > max_bytes:
            return False
        if expected_sha256 is not None:
            actual_hash, actual_size = _hash_handle(source_handle, max_bytes=max_bytes)
            if actual_size != effective_expected.size or actual_hash != expected_sha256:
                return False
        final_source = _identity_from_handle(source_handle, path)
        if (
            not effective_expected.same_object(final_source)
            or effective_expected.size != final_source.size
            or final_source.is_directory
        ):
            return False
        if os.name == "nt":
            # The directory handle is retained through disposition.  The
            # disposition itself never consults ``path``; a parent rebind can
            # therefore affect neither the owned source nor a foreign parent.
            if parent_expected is not None:
                current_parent = _identity_from_handle(parent_handle, path.parent)
                if not parent_expected.same_object(current_parent):
                    return False
            _set_file_disposition(source_handle)
            return True
        current_parent = _identity_from_handle(parent_handle, path.parent)
        if parent_expected is not None and not parent_expected.same_object(current_parent):
            return False
        return _posix_unlink_exact(
            parent_handle,
            path.parent,
            path.name,
            effective_expected,
            expected_sha256=expected_sha256,
            max_bytes=max_bytes,
        )
    except (FileNotFoundError, OSError, ProjectError):
        return False
    finally:
        if source_handle is not None:
            source_handle.close()
        if parent_handle is not None:
            parent_handle.close()


def _directory_name_error(message: str) -> None:
    _raise("project_unavailable", message)


def _validate_relative_child_name(name: Any) -> str:
    if type(name) is not str or not name or name in {".", ".."}:
        _directory_name_error("child name is invalid")
    if "\x00" in name or "\\" in name or "/" in name:
        _directory_name_error("child name is invalid")
    if os.name == "nt":
        if any(char in '<>"|?*:' for char in name):
            _directory_name_error("child name is invalid")
        if name.endswith((".", " ")) or name.casefold().split(".", 1)[0] in _WINDOWS_RESERVED:
            _directory_name_error("child name is invalid")
    try:
        if len(name.encode("utf-16-le" if os.name == "nt" else "utf-8", "strict")) == 0:
            _directory_name_error("child name is invalid")
    except UnicodeError:
        _directory_name_error("child name is invalid")
    return name


def _require_directory_handle(parent_handle: Any) -> _NativeHandle:
    try:
        closed = bool(parent_handle.closed)
        kind = parent_handle.kind
    except Exception as exc:
        raise ProjectError("project_unavailable", "parent directory handle is unavailable") from exc
    if closed or kind != "directory":
        _directory_name_error("parent directory handle is unavailable")
    return parent_handle


def _win_open_child_existing(
    parent_handle: _NativeHandle,
    parent_path: Path,
    name: str,
    *,
    directory: bool,
) -> tuple[_NativeHandle, PathIdentity]:
    """Open one child with an NT RootDirectory handle, never a child path."""
    if os.name != "nt":
        raise RuntimeError("Windows child open requested on POSIX")
    parent_handle = _require_directory_handle(parent_handle)
    name = _validate_relative_child_name(name)
    encoded = name.encode("utf-16-le", "strict")
    if len(encoded) > 0xFFFE:
        _directory_name_error("child name is too long")
    name_buffer = ctypes.create_unicode_buffer(name)
    unicode_name = _UNICODE_STRING(
        len(encoded),
        len(encoded) + ctypes.sizeof(wintypes.WCHAR),
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = _OBJECT_ATTRIBUTES(
        ctypes.sizeof(_OBJECT_ATTRIBUTES),
        parent_handle.raw,
        ctypes.pointer(unicode_name),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    io_status = _IO_STATUS_BLOCK()
    desired = _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
    options = _FILE_OPEN_REPARSE_POINT | _FILE_SYNCHRONOUS_IO_NONALERT
    if directory:
        desired = _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
        options |= _FILE_DIRECTORY_FILE
    else:
        options |= _FILE_NON_DIRECTORY_FILE
    raw = wintypes.HANDLE()
    status = int(
        _NtOpenFile(
            ctypes.byref(raw),
            desired,
            ctypes.cast(ctypes.byref(attributes), ctypes.c_void_p),
            ctypes.cast(ctypes.byref(io_status), ctypes.c_void_p),
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            options,
        )
    ) & 0xFFFFFFFF
    if status != 0:
        if status in {_STATUS_REPARSE_POINT_ENCOUNTERED, _STATUS_IO_REPARSE_TAG_NOT_HANDLED}:
            _raise("invalid_project", "reparse object is not allowed")
        if status in {_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND}:
            _raise("project_unavailable", "child is unavailable")
        if status in {_STATUS_ACCESS_DENIED, _STATUS_SHARING_VIOLATION}:
            raise PermissionError(errno.EACCES, "child cannot be opened", name)
        raise OSError(errno.EIO, f"relative child open failed (NTSTATUS 0x{status:08x})", name)
    handle = _NativeHandle(raw, "directory" if directory else "file")
    try:
        identity = _identity_from_handle(handle, parent_path / name)
        if identity.is_directory != directory:
            _raise("invalid_project", "path type is not allowed")
        return handle, identity
    except Exception:
        handle.close()
        raise


def _win_create_exclusive_child(
    parent_handle: _NativeHandle,
    parent_path: Path,
    name: str,
) -> tuple[_NativeHandle, PathIdentity]:
    """Create one regular child with ``NtCreateFile`` below a pinned parent."""

    if os.name != "nt":
        raise RuntimeError("Windows child creation requested on POSIX")
    parent_handle = _require_directory_handle(parent_handle)
    name = _validate_relative_child_name(name)
    encoded = name.encode("utf-16-le", "strict")
    if len(encoded) > 0xFFFE:
        _directory_name_error("child name is too long")
    name_buffer = ctypes.create_unicode_buffer(name)
    unicode_name = _UNICODE_STRING(
        len(encoded),
        len(encoded) + ctypes.sizeof(wintypes.WCHAR),
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = _OBJECT_ATTRIBUTES(
        ctypes.sizeof(_OBJECT_ATTRIBUTES),
        parent_handle.raw,
        ctypes.pointer(unicode_name),
        _OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    io_status = _IO_STATUS_BLOCK()
    raw = wintypes.HANDLE()
    status = int(
        _NtCreateFile(
            ctypes.byref(raw),
            _GENERIC_WRITE | _DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            ctypes.cast(ctypes.byref(attributes), ctypes.c_void_p),
            ctypes.cast(ctypes.byref(io_status), ctypes.c_void_p),
            None,
            _FILE_ATTRIBUTE_NORMAL,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            _FILE_CREATE,
            _FILE_NON_DIRECTORY_FILE | _FILE_SYNCHRONOUS_IO_NONALERT,
            None,
            0,
        )
    ) & 0xFFFFFFFF
    if status != 0:
        if status == _STATUS_OBJECT_NAME_COLLISION:
            raise FileExistsError(errno.EEXIST, "child already exists", name)
        if status in {_STATUS_OBJECT_NAME_NOT_FOUND, _STATUS_OBJECT_PATH_NOT_FOUND}:
            _raise("project_unavailable", "child parent is unavailable")
        if status in {_STATUS_REPARSE_POINT_ENCOUNTERED, _STATUS_IO_REPARSE_TAG_NOT_HANDLED}:
            _raise("invalid_project", "child parent is a reparse object")
        if status in {_STATUS_ACCESS_DENIED, _STATUS_SHARING_VIOLATION}:
            raise PermissionError(errno.EACCES, "child cannot be created", name)
        raise OSError(errno.EIO, f"relative child create failed (NTSTATUS 0x{status:08x})", name)
    handle = _NativeHandle(raw, "file")
    try:
        identity = _identity_from_handle(handle, parent_path / name)
        if identity.is_directory:
            _raise("invalid_project", "created child is not a regular file")
        return handle, identity
    except Exception:
        handle.close()
        raise


def _posix_create_exclusive_child(
    parent_handle: _NativeHandle,
    parent_path: Path,
    name: str,
) -> tuple[_NativeHandle, PathIdentity]:
    """Create one regular child with ``openat(O_EXCL|O_NOFOLLOW)``."""

    if os.name == "nt":
        raise RuntimeError("POSIX child creation requested on Windows")
    parent_handle = _require_directory_handle(parent_handle)
    name = _validate_relative_child_name(name)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise OSError(errno.ENOTSUP, "exclusive child creation requires O_NOFOLLOW")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(name, flags, 0o600, dir_fd=int(parent_handle.raw))
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            _raise("invalid_project", "child is a symlink object")
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            _raise("project_unavailable", "child parent is unavailable")
        raise
    handle = _NativeHandle(fd, "file")
    try:
        identity = _identity_from_handle(handle, parent_path / name)
        if identity.is_directory:
            _raise("invalid_project", "created child is not a regular file")
        return handle, identity
    except Exception:
        handle.close()
        raise


def _create_exclusive_child(
    parent_handle: _NativeHandle,
    parent_path: Path,
    name: str,
) -> tuple[_NativeHandle, PathIdentity]:
    """Create a direct child below one pinned directory, never by pathname."""

    if os.name == "nt":
        return _win_create_exclusive_child(parent_handle, parent_path, name)
    return _posix_create_exclusive_child(parent_handle, parent_path, name)


def _win_enumerate_child_entries_with_restart(
    parent_handle: _NativeHandle,
    parent_path: Path,
    *,
    max_entries: int,
) -> list[tuple[str, PathIdentity]]:
    """Restart a directory scan on the same native handle before reading it."""
    parent_identity = _identity_from_handle(parent_handle, parent_path)
    names: list[tuple[str, PathIdentity]] = []
    raw_entries = 0
    page_count = 0
    buffer_size = 64 * 1024
    header_size = _FILE_ID_BOTH_DIR_INFO.FileName.offset
    restart = True
    while True:
        page_count += 1
        if page_count > max_entries + 3:
            _directory_name_error("directory enumeration exceeded its page bound")
        buffer = ctypes.create_string_buffer(buffer_size)
        io_status = _IO_STATUS_BLOCK()
        status = int(
            _NtQueryDirectoryFile(
                parent_handle.raw,
                None,
                None,
                None,
                ctypes.byref(io_status),
                ctypes.cast(buffer, ctypes.c_void_p),
                wintypes.DWORD(buffer_size),
                _FILE_ID_BOTH_DIRECTORY_INFORMATION_CLASS,
                wintypes.BOOLEAN(0),
                None,
                wintypes.BOOLEAN(1 if restart else 0),
            )
        ) & 0xFFFFFFFF
        restart = False
        if status in {_STATUS_NO_MORE_FILES, _STATUS_NO_SUCH_FILE}:
            break
        if status not in {0, _STATUS_BUFFER_OVERFLOW}:
            raise OSError(errno.EIO, f"directory enumeration failed (NTSTATUS 0x{status:08x})")
        available = int(io_status.Information)
        if available < header_size or available > buffer_size:
            _directory_name_error("directory enumeration record is truncated")
        offset = 0
        page_entries = 0
        while True:
            remaining = available - offset
            if remaining < header_size:
                _directory_name_error("directory enumeration record is truncated")
            record = _FILE_ID_BOTH_DIR_INFO.from_buffer(buffer, offset)
            name_length = int(record.FileNameLength)
            if name_length <= 0 or name_length % 2:
                _directory_name_error("directory enumeration name is malformed")
            end = offset + header_size + name_length
            if end > available:
                _directory_name_error("directory enumeration name exceeds its page")
            try:
                name = ctypes.string_at(
                    ctypes.addressof(buffer) + offset + header_size,
                    name_length,
                ).decode("utf-16-le", "strict")
            except (UnicodeError, ValueError):
                _directory_name_error("directory enumeration name is malformed")
            raw_entries += 1
            page_entries += 1
            if raw_entries > max_entries + 2:
                _directory_name_error("directory entry count exceeded its bound")
            if name not in {".", ".."}:
                _validate_relative_child_name(name)
                if int(record.FileAttributes) & _FILE_ATTRIBUTE_REPARSE_POINT:
                    _directory_name_error("directory entry is a reparse object")
                entry_size = int(record.EndOfFile)
                if entry_size < 0:
                    _directory_name_error("directory entry size is malformed")
                entry_identity = PathIdentity(
                    _identity_text(str(parent_path / name)),
                    parent_identity.volume_serial,
                    int(record.FileId) & ((1 << 64) - 1),
                    bool(int(record.FileAttributes) & _FILE_ATTRIBUTE_DIRECTORY),
                    entry_size,
                )
                names.append((name, entry_identity))
                if len(names) > max_entries:
                    _directory_name_error("directory entry count exceeded its bound")
            next_offset = int(record.NextEntryOffset)
            if next_offset == 0:
                break
            if next_offset < header_size + name_length or next_offset > remaining or next_offset % 2:
                _directory_name_error("directory enumeration offset is malformed")
            offset += next_offset
        if page_entries == 0:
            _directory_name_error("directory enumeration returned no records")
    return names


def _win_enumerate_child_entries(
    parent_handle: _NativeHandle,
    parent_path: Path,
    *,
    max_entries: int,
) -> list[tuple[str, PathIdentity]]:
    """Enumerate direct names and opened-object metadata through one handle."""
    if os.name != "nt":
        raise RuntimeError("Windows directory enumeration requested on POSIX")
    parent_handle = _require_directory_handle(parent_handle)
    if parent_handle._directory_scan_started:
        return _win_enumerate_child_entries_with_restart(
            parent_handle,
            parent_path,
            max_entries=max_entries,
        )
    parent_identity = _identity_from_handle(parent_handle, parent_path)
    names: list[tuple[str, PathIdentity]] = []
    raw_entries = 0
    page_count = 0
    buffer_size = 64 * 1024
    header_size = _FILE_ID_BOTH_DIR_INFO.FileName.offset
    while True:
        page_count += 1
        if page_count > max_entries + 3:
            _directory_name_error("directory enumeration exceeded its page bound")
        buffer = ctypes.create_string_buffer(buffer_size)
        if not _GetFileInformationByHandleEx(
            parent_handle.raw,
            _FILE_ID_BOTH_DIRECTORY_INFO_CLASS,
            ctypes.cast(buffer, ctypes.c_void_p),
            wintypes.DWORD(buffer_size),
        ):
            error = ctypes.get_last_error()
            if error in {_ERROR_NO_MORE_FILES, _ERROR_HANDLE_EOF}:
                break
            raise OSError(error or errno.EIO, "directory enumeration failed")
        offset = 0
        page_entries = 0
        while True:
            available = buffer_size - offset
            if available < header_size:
                _directory_name_error("directory enumeration record is truncated")
            record = _FILE_ID_BOTH_DIR_INFO.from_buffer(buffer, offset)
            name_length = int(record.FileNameLength)
            if name_length <= 0 or name_length % 2:
                _directory_name_error("directory enumeration name is malformed")
            end = offset + header_size + name_length
            if end > buffer_size:
                _directory_name_error("directory enumeration name exceeds its page")
            try:
                name = ctypes.string_at(
                    ctypes.addressof(buffer) + offset + header_size,
                    name_length,
                ).decode("utf-16-le", "strict")
            except (UnicodeError, ValueError):
                _directory_name_error("directory enumeration name is malformed")
            raw_entries += 1
            page_entries += 1
            if raw_entries > max_entries + 2:
                _directory_name_error("directory entry count exceeded its bound")
            if name not in {".", ".."}:
                _validate_relative_child_name(name)
                if int(record.FileAttributes) & _FILE_ATTRIBUTE_REPARSE_POINT:
                    _directory_name_error("directory entry is a reparse object")
                entry_size = int(record.EndOfFile)
                if entry_size < 0:
                    _directory_name_error("directory entry size is malformed")
                entry_path = parent_path / name
                entry_identity = PathIdentity(
                    _identity_text(str(entry_path)),
                    parent_identity.volume_serial,
                    int(record.FileId) & ((1 << 64) - 1),
                    bool(int(record.FileAttributes) & _FILE_ATTRIBUTE_DIRECTORY),
                    entry_size,
                )
                names.append((name, entry_identity))
                if len(names) > max_entries:
                    _directory_name_error("directory entry count exceeded its bound")
            next_offset = int(record.NextEntryOffset)
            if next_offset == 0:
                break
            if next_offset < header_size + name_length or next_offset > available or next_offset % 2:
                _directory_name_error("directory enumeration offset is malformed")
            offset += next_offset
        if page_entries == 0:
            _directory_name_error("directory enumeration returned no records")
    parent_handle._directory_scan_started = True
    return names


def _win_enumerate_child_names(
    parent_handle: _NativeHandle,
    parent_path: Path,
    *,
    max_entries: int,
) -> list[str]:
    return [
        name
        for name, _identity in _win_enumerate_child_entries(
            parent_handle,
            parent_path,
            max_entries=max_entries,
        )
    ]


def _posix_enumerate_child_entries(
    parent_handle: _NativeHandle,
    parent_path: Path,
    *,
    max_entries: int,
) -> list[tuple[str, PathIdentity]]:
    """Enumerate direct names and no-follow metadata through a pinned descriptor."""
    if os.name == "nt":
        raise RuntimeError("POSIX directory enumeration requested on Windows")
    parent_handle = _require_directory_handle(parent_handle)
    parent_identity = _identity_from_handle(parent_handle, parent_path)
    names: list[tuple[str, PathIdentity]] = []
    try:
        with os.scandir(int(parent_handle.raw)) as iterator:
            for entry in iterator:
                name = entry.name
                if name in {".", ".."}:
                    continue
                _validate_relative_child_name(name)
                info = entry.stat(follow_symlinks=False)
                names.append(
                    (
                        name,
                        PathIdentity(
                            _identity_text(str(parent_path / name)),
                            parent_identity.volume_serial,
                            int(info.st_ino),
                            stat.S_ISDIR(info.st_mode),
                            int(info.st_size),
                        ),
                    )
                )
                if len(names) > max_entries:
                    _directory_name_error("directory entry count exceeded its bound")
    except ProjectError:
        raise
    except Exception as exc:
        raise OSError(errno.EIO, "directory enumeration failed") from exc
    return names


def _posix_enumerate_child_names(
    parent_handle: _NativeHandle,
    parent_path: Path,
    *,
    max_entries: int,
) -> list[str]:
    return [
        name
        for name, _identity in _posix_enumerate_child_entries(
            parent_handle,
            parent_path,
            max_entries=max_entries,
        )
    ]


def _validate_enumeration_bound(max_entries: int) -> None:
    if type(max_entries) is not int or isinstance(max_entries, bool) or max_entries < 1:
        _directory_name_error("directory enumeration bound is invalid")
    if max_entries > 1_000_000:
        _directory_name_error("directory enumeration bound is too large")


def enumerate_child_entries(
    parent_handle: _NativeHandle,
    parent_path: Path,
    *,
    max_entries: int,
) -> list[tuple[str, PathIdentity]]:
    """Internal handle-relative names plus identity metadata with a hard bound."""
    _validate_enumeration_bound(max_entries)
    if os.name == "nt":
        return _win_enumerate_child_entries(parent_handle, parent_path, max_entries=max_entries)
    return _posix_enumerate_child_entries(parent_handle, parent_path, max_entries=max_entries)


def enumerate_child_names(
    parent_handle: _NativeHandle,
    parent_path: Path,
    *,
    max_entries: int,
) -> list[str]:
    """Internal handle-relative direct-child names with a hard bound."""
    return [
        name
        for name, _identity in enumerate_child_entries(
            parent_handle,
            parent_path,
            max_entries=max_entries,
        )
    ]


def _open_child_existing(
    parent_handle: _NativeHandle,
    parent_path: Path,
    name: str,
    *,
    directory: bool,
) -> tuple[_NativeHandle, PathIdentity]:
    if os.name == "nt":
        return _win_open_child_existing(parent_handle, parent_path, name, directory=directory)
    name = _validate_relative_child_name(name)
    parent_handle = _require_directory_handle(parent_handle)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(name, flags, dir_fd=int(parent_handle.raw))
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            _raise("invalid_project", "symlink object is not allowed")
        _raise("project_unavailable", "path could not be opened")
    handle = _NativeHandle(fd, "directory" if directory else "file")
    try:
        identity = _identity_from_handle(handle, parent_path / name)
        if identity.is_directory != directory:
            _raise("invalid_project", "path type is not allowed")
        return handle, identity
    except Exception:
        handle.close()
        raise


def _native_view_for_file_handle(handle: Any) -> tuple[_NativeHandle, bool]:
    """View a file-like exact handle as a native handle without taking ownership."""

    if isinstance(handle, _NativeHandle):
        return handle, False
    if os.name == "nt":
        import msvcrt

        try:
            native = msvcrt.get_osfhandle(handle.fileno())
        except (AttributeError, OSError, ValueError) as exc:
            raise OSError(errno.EBADF, "exact file handle is unavailable") from exc
        if native == -1:
            raise OSError(errno.EBADF, "exact file handle is unavailable")
        return _NativeHandle(wintypes.HANDLE(native), "file"), True
    try:
        raw = int(handle.fileno())
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise OSError(errno.EBADF, "exact file handle is unavailable") from exc
    return _NativeHandle(raw, "file"), True


def dispose_exclusive_child(
    parent_handle: _NativeHandle,
    parent_path: Path,
    name: str,
    child_handle: Any,
    expected: PathIdentity,
    *,
    expected_sha256: str | None = None,
    max_bytes: int,
) -> bool:
    """Dispose one exact child below a pinned parent, never by parent pathname."""

    parent_handle = _require_directory_handle(parent_handle)
    name = _validate_relative_child_name(name)
    native_child, borrowed_view = _native_view_for_file_handle(child_handle)
    try:
        observed = _identity_from_handle(native_child, parent_path / name)
        if observed.is_directory or not expected.same_object(observed):
            return False
        if os.name == "nt":
            _set_file_disposition(native_child)
            return True
        # The creation identity has size zero; use the exact handle's current
        # size for the POSIX exchange while retaining its object identity.
        effective_expected = PathIdentity(
            expected.canonical_path,
            expected.volume_serial,
            expected.file_id,
            expected.is_directory,
            observed.size,
        )
        return _posix_unlink_exact(
            parent_handle,
            parent_path,
            name,
            effective_expected,
            expected_sha256=expected_sha256,
            max_bytes=max_bytes,
        )
    finally:
        if borrowed_view:
            native_child._closed = True


def _missing_leaf_identity(path: Path) -> PathIdentity:
    """Identify a not-yet-created leaf by its pinned parent and basename."""

    parent = path.parent
    if path.name in ("", ".", ".."):
        _raise("invalid_project", "leaf name is invalid")
    parent_handle, parent_identity = _open_existing(parent, directory=True)
    parent_handle.close()
    basename = _identity_text(path.name)
    canonical = parent_identity.canonical_path.rstrip("\\/") + (
        "\\" if "\\" in parent_identity.canonical_path else os.sep
    ) + basename
    leaf_id = int.from_bytes(
        hashlib.sha256(
            f"{parent_identity.volume_serial}:{parent_identity.file_id}:{basename}".encode(
                "utf-8", "surrogatepass"
            )
        ).digest()[:8],
        "big",
    )
    return PathIdentity(
        canonical,
        parent_identity.volume_serial,
        leaf_id,
        False,
        0,
        True,
    )


def _is_within(child: PathIdentity, parent: PathIdentity) -> bool:
    child_path = child.canonical_path.rstrip("\\/")
    parent_path = parent.canonical_path.rstrip("\\/")
    if child_path == parent_path:
        return True
    return child_path.startswith(parent_path + ("\\" if "\\" in parent_path else os.sep))


def _safe_json_loads(raw: bytes, *, object_pairs_hook: Any = None) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=object_pairs_hook)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
        MemoryError,
        OverflowError,
    ):
        _raise("invalid_project", "manifest is not valid UTF-8 JSON")


def ensure_private_node(path: os.PathLike[str] | str, *, directory: bool | None = None) -> None:
    """Create/check a profile-private node with user-only permissions."""

    target = _normalise_text_path(_path_text(path))
    _reject_reparse_components(target)
    if directory is None:
        directory = target.is_dir()
    if target.exists() and target.is_dir() is not bool(directory):
        _raise("private_state_unavailable", "private node has the wrong type")
    if directory:
        target.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(target, 0o700)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.touch()
        if os.name != "nt":
            os.chmod(target, 0o600)
    if os.name == "nt":
        _set_private_windows_acl(target, directory=bool(directory))


def _current_user_sid_string() -> str:
    if os.name != "nt":
        raise RuntimeError("Windows SID requested on POSIX")
    token = wintypes.HANDLE()
    if not _GetCurrentProcessToken(_GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), "current process token unavailable")
    try:
        size = wintypes.DWORD()
        _GetTokenInformation(token, _TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
        if not size.value:
            raise OSError(ctypes.get_last_error(), "current user SID size unavailable")
        buffer = ctypes.create_string_buffer(size.value)
        if not _GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            buffer,
            size,
            ctypes.byref(size),
        ):
            raise OSError(ctypes.get_last_error(), "current user SID unavailable")
        user = ctypes.cast(buffer, ctypes.POINTER(_TOKEN_USER)).contents.User.Sid
        return _sid_to_string(user)
    finally:
        _CloseHandle(token)


def _sid_to_string(sid: Any) -> str:
    result = ctypes.c_wchar_p()
    sid_pointer = sid if isinstance(sid, ctypes.c_void_p) else ctypes.c_void_p(sid)
    if not _ConvertSidToStringSidW(sid_pointer, ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(), "SID conversion failed")
    try:
        if not result.value:
            raise OSError("empty SID")
        return result.value
    finally:
        _LocalFree(ctypes.cast(result, ctypes.c_void_p))


def _inspect_private_windows_acl(path: Path) -> dict[str, Any]:
    """Read the effective owner, DACL and protection bit without shell tools."""

    if os.name != "nt":
        raise RuntimeError("Windows ACL requested on POSIX")
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = _GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status:
        raise OSError(int(status), "private ACL query failed")
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not _GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise OSError(ctypes.get_last_error(), "private ACL control query failed")
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        effective_dacl = ctypes.c_void_p()
        if not _GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(effective_dacl),
            ctypes.byref(defaulted),
        ):
            raise OSError(ctypes.get_last_error(), "private DACL query failed")
        if not present.value or not effective_dacl.value:
            return {
                "owner_sid": _sid_to_string(owner),
                "dacl_protected": bool(control.value & _SE_DACL_PROTECTED),
                "aces": [],
            }
        info = _ACL_SIZE_INFORMATION()
        if not _GetAclInformation(
            effective_dacl,
            ctypes.byref(info),
            ctypes.sizeof(info),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            raise OSError(ctypes.get_last_error(), "private DACL size query failed")
        aces: list[dict[str, Any]] = []
        for index in range(int(info.AceCount)):
            ace = ctypes.c_void_p()
            if not _GetAce(effective_dacl, index, ctypes.byref(ace)) or not ace.value:
                raise OSError(ctypes.get_last_error(), "private ACE query failed")
            header = ctypes.cast(ace, ctypes.POINTER(_ACE_HEADER)).contents
            entry: dict[str, Any] = {
                "type": int(header.AceType),
                "flags": int(header.AceFlags),
                "sid": None,
                "mask": None,
            }
            if header.AceType == _ACCESS_ALLOWED_ACE_TYPE:
                mask = ctypes.cast(ace.value + 4, ctypes.POINTER(wintypes.DWORD)).contents.value
                sid = ctypes.c_void_p(ace.value + 8)
                entry["sid"] = _sid_to_string(sid)
                entry["mask"] = int(mask)
            aces.append(entry)
        return {
            "owner_sid": _sid_to_string(owner),
            "dacl_protected": bool(control.value & _SE_DACL_PROTECTED),
            "aces": aces,
        }
    finally:
        if descriptor.value:
            _LocalFree(descriptor)


def _verify_private_windows_acl(path: Path) -> bool:
    snapshot = _inspect_private_windows_acl(path)
    expected_flags = (
        _OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE
        if path.is_dir()
        else 0
    )
    expected = {
        (_SYSTEM_SID, _FILE_ALL_ACCESS, expected_flags),
        (_current_user_sid_string(), _FILE_ALL_ACCESS, expected_flags),
    }
    actual = {
        (ace["sid"], ace["mask"], ace["flags"])
        for ace in snapshot["aces"]
        if ace["type"] == _ACCESS_ALLOWED_ACE_TYPE
    }
    return (
        snapshot["owner_sid"] == _current_user_sid_string()
        and snapshot["dacl_protected"]
        and len(snapshot["aces"]) == len(expected)
        and actual == expected
    )


def _require_private_windows_acl(path: Path) -> None:
    try:
        valid = _verify_private_windows_acl(path)
    except Exception:
        valid = False
    if not valid:
        _raise("private_state_unavailable", "private ACL verification failed")


def _set_private_windows_acl(path: Path, *, directory: bool | None = None) -> None:
    """Set and verify a protected DACL using current-user and SYSTEM SIDs.

    Applying an already-identical protected DACL via SetNamedSecurityInfoW is
    extremely expensive on some Windows systems (several seconds per call).
    Verification is cheap and is the same exact security predicate used after
    application, so short-circuit only when the existing ACL already satisfies
    it. A false/failed verification still takes the original repair+verify path.
    """

    if os.name != "nt":
        return
    requested_directory = path.is_dir() if directory is None else bool(directory)
    try:
        # Preserve the caller's explicit object-type intent.  An ACL that is
        # exact for a file must not satisfy a directory request (or vice versa).
        if path.is_dir() is not requested_directory:
            _raise("private_state_unavailable", "private node has the wrong type")
        if _verify_private_windows_acl(path):
            # Match the prior apply path's final fail-closed verification and
            # repeat the type check adjacent to return.  This restores the old
            # post-operation boundary while still avoiding the costly ACL write.
            _require_private_windows_acl(path)
            if path.is_dir() is not requested_directory:
                _raise("private_state_unavailable", "private node has the wrong type")
            return
    except ProjectError:
        raise
    except Exception:
        # Fail closed into the repair path; _require_private_windows_acl below
        # will still reject the node if applying the protected DACL fails.
        pass
    directory = requested_directory
    user_sid = _current_user_sid_string()
    inheritance = "OICI" if directory else ""
    sddl = (
        f"O:{user_sid}D:P"
        f"(A;{inheritance};FA;;;{_SYSTEM_SID})"
        f"(A;{inheritance};FA;;;{user_sid})"
    )
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.DWORD()
    if not _ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        _raise("private_state_unavailable", "private ACL could not be built")
    try:
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        owner_defaulted = wintypes.BOOL()
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        if not _GetSecurityDescriptorOwner(
            descriptor,
            ctypes.byref(owner),
            ctypes.byref(owner_defaulted),
        ) or not _GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present.value or not dacl.value:
            _raise("private_state_unavailable", "private ACL could not be read")
        # Lane w21-pkg item 5 (BCK-211): writing OWNER needs WRITE_OWNER even when the owner does not change, and a
        # home whose inherited ACE grants Modify (not Full control) lacks it, so the whole write failed with status 5
        # and project creation stopped. Skip only that redundant write when the inspected owner already IS the
        # current user; the protected user+SYSTEM DACL and the verification below stay exactly as before.
        try:
            owner_is_current = _inspect_private_windows_acl(path)["owner_sid"] == user_sid
        except Exception:
            owner_is_current = False  # unknown owner: request it as before
        information = _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
        if not owner_is_current:
            information |= _OWNER_SECURITY_INFORMATION
        status = _SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            information,
            None if owner_is_current else owner,
            None,
            dacl,
            None,
        )
        if status:
            _raise("private_state_unavailable", "private ACL could not be applied")
    finally:
        if descriptor.value:
            _LocalFree(descriptor)
    _require_private_windows_acl(path)


def _private_node_ok(path: Path, *, directory: bool) -> bool:
    if not path.exists() or path.is_symlink() or _posix_component_reparse(path) if os.name != "nt" else False:
        return False
    if directory:
        return stat.S_ISDIR(path.stat().st_mode) and (path.stat().st_mode & 0o777) == 0o700
    return stat.S_ISREG(path.stat().st_mode) and (path.stat().st_mode & 0o777) == 0o600


def _write_fully(handle: Any, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = handle.write(remaining)
        if written is None:
            written = len(remaining)
        if written <= 0:
            raise OSError("short private write")
        remaining = remaining[written:]
    handle.flush()
    _flush_private_file(handle.fileno())


# Lane w33-rel2 item 1 (BCK-227): the longest a project read/write waits for another process's project lock.
PROJECT_LOCK_WAIT_SECONDS = 15.0
PROJECT_BUSY = "project_busy"


class NamedProjectLock:
    """Cross-process lock named from the HMAC of canonical handle identity."""

    def __init__(self, name: str, *, lock_file: Path | None = None) -> None:
        self.name = name
        self._owned = False
        self._preexisting = False
        self._handle: Any = None
        self._file: Any = None
        if os.name == "nt":
            ctypes.set_last_error(0)
            self._handle = _CreateMutexW(None, False, name)
            if not self._handle:
                _raise("project_unavailable", "project lock unavailable")
            self._preexisting = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
        else:
            if lock_file is None:
                _raise("project_unavailable", "project lock unavailable")
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(lock_file, "a+b")

    def acquire(self, blocking: bool = True) -> bool:
        # Lane w33-rel2 item 1 (BCK-227, review R5): a blocking acquire waits at most PROJECT_LOCK_WAIT_SECONDS (read
        # now, so a test or a caller can shorten it), then refuses PROJECT_BUSY; it never waits forever for another
        # process. A non-blocking acquire answers as before.
        if self._owned:
            return True
        wait = max(0.0, float(PROJECT_LOCK_WAIT_SECONDS)) if blocking else 0.0
        if os.name == "nt":
            result = _WaitForSingleObject(self._handle, int(wait * 1000))
            if result == _WAIT_OBJECT_0:
                self._owned = True
                return True
            if result == _WAIT_TIMEOUT:
                if blocking:
                    _raise(PROJECT_BUSY, "project lock is busy")
                _raise("project_unavailable", "project lock is busy")
            _raise("project_unavailable", "project lock unavailable")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    _raise("project_unavailable", "project lock unavailable")
                if not blocking:
                    _raise("project_unavailable", "project lock is busy")
                if time.monotonic() >= deadline:
                    _raise(PROJECT_BUSY, "project lock is busy")
                time.sleep(0.02)
        self._owned = True
        return True

    def release(self) -> None:
        if not self._owned:
            return
        if os.name == "nt":
            _ReleaseMutex(self._handle)
        else:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._owned = False

    def close(self) -> None:
        self.release()
        if os.name == "nt":
            if self._handle:
                _CloseHandle(self._handle)
                self._handle = None
        elif self._file:
            self._file.close()
            self._file = None

    def __enter__(self) -> "NamedProjectLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup
        with contextlib.suppress(Exception):
            self.close()


def _profile_paths(hermes_home: os.PathLike[str] | str) -> tuple[Path, Path]:
    home = _normalise_text_path(_path_text(hermes_home))
    _reject_reparse_components(home)
    data = state_root(home / "plugin-data")
    data.mkdir(parents=True, exist_ok=True)
    for parent in (home, home / "plugin-data", data):
        ensure_private_node(parent, directory=True)
    return data, data / "path-identity.key"


def _unprepared_profile_paths(hermes_home: os.PathLike[str] | str) -> tuple[Path, Path, Path]:
    """Derive Windows profile paths without racing private ACL publication."""

    home = _normalise_text_path(_path_text(hermes_home))
    _reject_reparse_components(home)
    data = state_root(home / "plugin-data")
    return home, data, data / "path-identity.key"


def _profile_identity_lock(data_dir: Path) -> NamedProjectLock:
    """Serialize key discovery/publication without requiring the key itself."""

    canonical = _identity_text(str(data_dir.resolve(strict=False)))
    digest = hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
    name = "Local\\HermesFilmSuite-ProfileIdentity-" + digest
    if os.name == "nt":
        return NamedProjectLock(name)
    return NamedProjectLock(name, lock_file=data_dir / ".path-identity.lock")


def _read_profile_identity_key(key_path: Path) -> bytes:
    """Read only a regular, complete, private identity key."""

    _reject_reparse_components(key_path)
    if key_path.is_symlink() or not key_path.is_file():
        _raise("private_state_unavailable", "identity key is not a regular file")
    try:
        raw = key_path.read_bytes()
    except OSError:
        _raise("private_state_unavailable", "identity key is unavailable")
    if len(raw) != PROFILE_KEY_SIZE:
        _raise("private_state_unavailable", "identity key is corrupt")
    if os.name != "nt" and (key_path.stat().st_mode & 0o777) != 0o600:
        _raise("private_state_unavailable", "identity key permissions are unsafe")
    if os.name == "nt":
        _require_private_windows_acl(key_path)
    return raw


def _publish_profile_identity_key(temp: Path, key_path: Path) -> None:
    """Publish a complete temp file without ever opening the final path."""

    if os.name == "nt":
        native_temp = str(temp)
        native_key = str(key_path)
        if len(native_temp) >= 240 and not native_temp.startswith("\\\\?\\"):
            native_temp = "\\\\?\\" + native_temp
        if len(native_key) >= 240 and not native_key.startswith("\\\\?\\"):
            native_key = "\\\\?\\" + native_key
        if not _MoveFileExW(native_temp, native_key, _MOVEFILE_WRITE_THROUGH):
            error = ctypes.get_last_error()
            if error in (_ERROR_FILE_EXISTS, _ERROR_ALREADY_EXISTS):
                raise FileExistsError(error, os.strerror(error), str(key_path))
            raise OSError(error, os.strerror(error), str(key_path))
        return
    # A same-directory hard link is the portable POSIX no-replace primitive.
    # It exposes the already-flushed temp bytes atomically; do not fall back to
    # opening the final path and writing into it.
    os.link(temp, key_path)


def _rollback_profile_identity_key(
    key_path: Path,
    parent: Path,
    *,
    expected: PathIdentity | None = None,
    parent_expected: PathIdentity | None = None,
    expected_sha256: str | None = None,
) -> bool:
    """Remove only this call's published key through the exact-delete primitive."""

    if expected is None or expected.missing_leaf:
        return False
    try:
        if parent_expected is None:
            parent_handle, parent_expected = _open_existing(parent, directory=True)
            parent_handle.close()
        removed = unlink_handle_bound(
            key_path,
            expected,
            parent_expected=parent_expected,
            expected_sha256=expected_sha256,
            max_bytes=PROFILE_KEY_SIZE,
        )
        if not removed:
            return False
    except (FileNotFoundError, OSError, ProjectError):
        return False
    with contextlib.suppress(Exception):
        _fsync_parent(parent)
    return True


def profile_identity_key(
    hermes_home: os.PathLike[str] | str | None = None,
) -> bytes:
    """Load or exclusively publish the profile's 32-byte HMAC key."""

    if hermes_home is None:
        hermes_home = get_hermes_home()
    if os.name == "nt":
        profile_home, data_dir, key_path = _unprepared_profile_paths(hermes_home)
    else:
        data_dir, key_path = _profile_paths(hermes_home)
    _reject_reparse_components(data_dir)
    try:
        with _profile_identity_lock(data_dir):
            if os.name == "nt":
                for parent in (profile_home, profile_home / "plugin-data", data_dir):
                    ensure_private_node(parent, directory=True)
            _reject_reparse_components(data_dir)
            if key_path.exists() or key_path.is_symlink():
                return _read_profile_identity_key(key_path)

            payload = secrets.token_bytes(PROFILE_KEY_SIZE)
            temp = data_dir / f".path-identity.{secrets.token_hex(16)}.tmp"
            published = False
            published_identity: PathIdentity | None = None
            temp_identity: PathIdentity | None = None
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
                fd = os.open(temp, flags, 0o600)
                try:
                    remaining = memoryview(payload)
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise OSError("short identity-key write")
                        remaining = remaining[written:]
                    _flush_private_file(fd)
                finally:
                    os.close(fd)
                if os.name != "nt":
                    os.chmod(temp, 0o600)
                if os.name == "nt":
                    _set_private_windows_acl(temp)
                temp_handle, temp_identity = _open_existing(temp, directory=False)
                temp_handle.close()
                try:
                    _publish_profile_identity_key(temp, key_path)
                    published = True
                    published_handle, published_identity = _open_existing(
                        key_path, directory=False
                    )
                    published_handle.close()
                    _fsync_parent(data_dir)
                except FileExistsError:
                    # Another implementation/version may have published while
                    # the lock was being acquired.  Never read until the final
                    # path is revalidated as a complete regular file.
                    pass
                except (OSError, ProjectError) as exc:
                    if published:
                        _rollback_profile_identity_key(
                            key_path,
                            data_dir,
                            expected=published_identity,
                            expected_sha256=hashlib.sha256(payload).hexdigest(),
                        )
                    if isinstance(exc, ProjectError):
                        _raise("private_state_unavailable", "identity key could not be published")
                    raise
            except ProjectError:
                raise
            except OSError:
                _raise("private_state_unavailable", "identity key could not be published")
            finally:
                if temp_identity is not None:
                    _rollback_profile_identity_key(
                        temp,
                        data_dir,
                        expected=temp_identity,
                        expected_sha256=hashlib.sha256(payload).hexdigest(),
                    )
            return _read_profile_identity_key(key_path)
    except ProjectError:
        raise
    except OSError:
        _raise("private_state_unavailable", "identity key publication lock unavailable")


class PathBoundary:
    """Reparse-aware, handle-pinned project boundary."""

    def __init__(self, hermes_home: os.PathLike[str] | str | None = None) -> None:
        if hermes_home is None:
            hermes_home = get_hermes_home()
        self.hermes_home = _normalise_text_path(_path_text(hermes_home))
        _reject_reparse_components(self.hermes_home)
        self._key: bytes | None = None
        self._leases: weakref.WeakKeyDictionary[PinnedMedia, _LeaseRecord] = weakref.WeakKeyDictionary()
        self._pinned_lock = threading.RLock()

    @property
    def open_pinned_count(self) -> int:
        with self._pinned_lock:
            return len(self._leases)

    def _identity_key(self) -> bytes:
        if self._key is None:
            self._key = profile_identity_key(hermes_home=self.hermes_home)
        return self._key

    def open_existing(
        self,
        value: os.PathLike[str] | str,
        *,
        directory: bool,
    ) -> tuple[_NativeHandle, PathIdentity]:
        """Open one already-authorized path and return its pinned handle."""

        path = self._prepare_path(value)
        self._check_exists(path, directory=directory, operation="open")
        return _open_existing(path, directory=directory)

    def _lease_record(self, pinned: PinnedMedia) -> _LeaseRecord:
        if not isinstance(pinned, PinnedMedia):
            _raise("invalid_request", "media lease is invalid")
        with self._pinned_lock:
            record = self._leases.get(pinned)
        if record is None:
            _raise("project_unavailable", "media lease is closed")
        return record

    def _prepare_path(self, value: os.PathLike[str] | str) -> Path:
        # Key creation is the first operation of every path-bound call.  It is
        # deliberately before spelling/reparse checks and remains fail-closed.
        self._identity_key()
        path = _normalise_text_path(_path_text(value))
        _reject_reparse_components(path)
        home = self.hermes_home
        inside_home = False
        if path.exists():
            home_handle, home_identity = _open_existing(home, directory=True)
            candidate_handle: _NativeHandle | None = None
            try:
                candidate_handle, candidate_identity = _open_existing(
                    path, directory=path.is_dir()
                )
                inside_home = _is_within(candidate_identity, home_identity)
            finally:
                if candidate_handle is not None:
                    candidate_handle.close()
                home_handle.close()
        else:
            child_text = _identity_text(str(path)).rstrip("\\/")
            home_text = _identity_text(str(home)).rstrip("\\/")
            separator = "\\" if "\\" in home_text else os.sep
            inside_home = child_text == home_text or child_text.startswith(
                home_text + separator
            )
        if inside_home:
            _raise("invalid_project", "path is inside HERMES_HOME")
        return path

    def _check_exists(self, path: Path, *, directory: bool, operation: str) -> None:
        if not path.exists():
            _raise("project_not_found" if operation == "open" else "project_unavailable", "path does not exist")
        if path.is_symlink():
            _raise("invalid_project", "symlink object is not allowed")
        if directory and not path.is_dir():
            _raise("invalid_project", "path must be a directory")
        if not directory and not path.is_file():
            _raise("invalid_project", "path must be a regular file")

    def validate_project_directory(
        self,
        directory: os.PathLike[str] | str,
        *,
        operation: str = "open",
        create: bool | None = None,
    ) -> Path:
        if create is not None:
            operation = "create" if create else "open"
        path = self._prepare_path(directory)
        self._check_exists(path, directory=True, operation=operation)
        handle, identity = _open_existing(path, directory=True)
        try:
            if not identity.is_directory:
                _raise("invalid_project", "path must be a directory")
            if operation == "create" and next(path.iterdir(), None) is not None:
                _raise("project_not_empty", "project directory is not empty")
        finally:
            handle.close()
        return path

    def validate_local_directory(self, directory: os.PathLike[str] | str, **kwargs: Any) -> Path:
        return self.validate_project_directory(directory, **kwargs)

    def path_identity(
        self,
        value: os.PathLike[str] | str,
        *,
        allow_missing_leaf: bool = False,
    ) -> PathIdentity:
        path = self._prepare_path(value)
        if allow_missing_leaf and not path.exists():
            identity = _missing_leaf_identity(path)
            home_handle, home_identity = _open_existing(self.hermes_home, directory=True)
            try:
                if _is_within(identity, home_identity):
                    _raise("invalid_project", "path is inside HERMES_HOME")
            finally:
                home_handle.close()
            return identity
        self._check_exists(path, directory=path.is_dir(), operation="open")
        handle, identity = _open_existing(path, directory=path.is_dir())
        handle.close()
        return identity

    canonical_identity = path_identity

    def canonical_handle_identity(
        self,
        value: os.PathLike[str] | str,
        *,
        allow_missing_leaf: bool = False,
    ) -> str:
        return self.path_identity(value, allow_missing_leaf=allow_missing_leaf).canonical_path

    canonical_path = canonical_handle_identity
    identity_for_path = path_identity

    def pin_directory(self, value: os.PathLike[str] | str) -> PathIdentity:
        path = self._prepare_path(value)
        self._check_exists(path, directory=True, operation="open")
        handle, identity = _open_existing(path, directory=True)
        handle.close()
        return identity

    def pin_file(self, value: os.PathLike[str] | str) -> PathIdentity:
        path = self._prepare_path(value)
        self._check_exists(path, directory=False, operation="open")
        handle, identity = _open_existing(path, directory=False)
        handle.close()
        return identity

    def revalidate_directory(self, value: os.PathLike[str] | str, expected: PathIdentity) -> None:
        path = self._prepare_path(value)
        self._check_exists(path, directory=True, operation="open")
        handle, identity = _open_existing(path, directory=True)
        handle.close()
        if not expected.same_object(identity):
            _raise("project_unavailable", "directory identity changed")

    def enumerate_child_entries(
        self,
        parent_handle: _NativeHandle,
        parent_path: os.PathLike[str] | str,
        *,
        max_entries: int,
    ) -> list[tuple[str, PathIdentity]]:
        """Enumerate direct child names and identity metadata from one handle."""
        try:
            diagnostic_path = Path(os.fspath(parent_path))
        except (TypeError, ValueError):
            _raise("invalid_request", "directory diagnostic path is invalid")
        return enumerate_child_entries(parent_handle, diagnostic_path, max_entries=max_entries)

    def enumerate_child_names(
        self,
        parent_handle: _NativeHandle,
        parent_path: os.PathLike[str] | str,
        *,
        max_entries: int,
    ) -> list[str]:
        """Enumerate direct children from an already-open directory handle.

        ``parent_path`` is diagnostic context only.  It is never consulted to
        select the directory or any of its children.
        """
        try:
            diagnostic_path = Path(os.fspath(parent_path))
        except (TypeError, ValueError):
            _raise("invalid_request", "directory diagnostic path is invalid")
        return enumerate_child_names(parent_handle, diagnostic_path, max_entries=max_entries)

    def open_child_existing(
        self,
        parent_handle: _NativeHandle,
        parent_path: os.PathLike[str] | str,
        name: str,
        *,
        directory: bool,
    ) -> tuple[_NativeHandle, PathIdentity]:
        """Open one direct child relative to an already-open directory handle."""
        try:
            diagnostic_path = Path(os.fspath(parent_path))
        except (TypeError, ValueError):
            _raise("invalid_request", "directory diagnostic path is invalid")
        return _open_child_existing(parent_handle, diagnostic_path, name, directory=directory)

    def create_exclusive_child(
        self,
        parent_handle: _NativeHandle,
        parent_path: os.PathLike[str] | str,
        name: str,
    ) -> tuple[_NativeHandle, PathIdentity]:
        """Create one direct regular child relative to a pinned parent handle."""

        try:
            diagnostic_path = Path(os.fspath(parent_path))
        except (TypeError, ValueError):
            _raise("invalid_request", "directory diagnostic path is invalid")
        return _create_exclusive_child(parent_handle, diagnostic_path, name)

    def fsync_directory_handle(
        self,
        parent_handle: _NativeHandle,
        *,
        expected: PathIdentity | None = None,
    ) -> None:
        """Flush one pinned directory handle after optional identity checks."""

        fallback = Path(getattr(expected, "canonical_path", "."))
        if expected is not None:
            before = _identity_from_handle(parent_handle, fallback)
            if not expected.same_object(before) or not before.same_object(expected):
                _raise("project_unavailable", "directory identity changed before flush")
        _fsync_directory_handle(parent_handle)
        if expected is not None:
            after = _identity_from_handle(parent_handle, fallback)
            if not expected.same_object(after) or not after.same_object(expected):
                _raise("project_unavailable", "directory identity changed during flush")

    def flush_private_file(self, fd: int) -> None:
        """Flush one already-open private file descriptor."""

        _flush_private_file(fd)

    def fsync_parent(self, value: os.PathLike[str] | str) -> None:
        """Flush a validated parent path for compatibility callers."""

        _fsync_parent(self._prepare_path(value))

    def win_link_handle_no_replace(self, source: Any, parent: Any, name: str) -> None:
        """Publish one open source into one open parent without replacement."""

        win_link_handle_no_replace(source, parent, name)

    def posix_link_no_replace(self, source_fd: int, parent_fd: int, name: str) -> None:
        """Publish one POSIX source descriptor without replacement."""

        _posix_linkat_empty_path(source_fd, parent_fd, name)

    def unlink_handle_bound(
        self,
        value: os.PathLike[str] | str,
        expected: PathIdentity,
        **kwargs: Any,
    ) -> bool:
        """Delete only the exact object observed at a validated path."""

        return unlink_handle_bound(self._prepare_path(value), expected, **kwargs)

    def dispose_exclusive_child(
        self,
        parent_handle: _NativeHandle,
        parent_path: os.PathLike[str] | str,
        name: str,
        child_handle: Any,
        expected: PathIdentity,
        *,
        expected_sha256: str | None = None,
        max_bytes: int,
    ) -> bool:
        """Dispose one child created under a pinned parent and exact handle."""

        try:
            diagnostic_path = Path(os.fspath(parent_path))
        except (TypeError, ValueError):
            _raise("invalid_request", "directory diagnostic path is invalid")
        return dispose_exclusive_child(
            parent_handle,
            diagnostic_path,
            name,
            child_handle,
            expected,
            expected_sha256=expected_sha256,
            max_bytes=max_bytes,
        )

    def revalidate_file(self, value: os.PathLike[str] | str, expected: PathIdentity) -> None:
        path = self._prepare_path(value)
        self._check_exists(path, directory=False, operation="open")
        handle, identity = _open_existing(path, directory=False)
        handle.close()
        if not expected.same_object(identity) or expected.size != identity.size:
            _raise("project_unavailable", "file identity changed")

    def validate_confined_path(
        self,
        value: os.PathLike[str] | str,
        approved_roots: Iterable[os.PathLike[str] | str],
        *,
        directory: bool = False,
        allow_missing_leaf: bool = False,
    ) -> PathIdentity:
        path = self._prepare_path(value)
        if allow_missing_leaf and not path.exists():
            identity = _missing_leaf_identity(path)
        else:
            handle, identity = _open_existing(path, directory=directory)
            handle.close()
        if identity.is_directory != directory:
            _raise("invalid_project", "path type is not allowed")
        roots = []
        for root in approved_roots:
            root_path = self._prepare_path(root)
            self._check_exists(root_path, directory=True, operation="open")
            root_handle, root_identity = _open_existing(root_path, directory=True)
            root_handle.close()
            roots.append(root_identity)
        if not any(_is_within(identity, root) for root in roots):
            _raise("invalid_project", "path is outside approved roots")
        return identity

    validate_confined = validate_confined_path

    def validate_approved_root(self, value: os.PathLike[str] | str) -> PathIdentity:
        return self.validate_confined_path(value, (value,), directory=True)

    def validate_input(
        self,
        value: os.PathLike[str] | str,
        approved_roots: Iterable[os.PathLike[str] | str],
    ) -> PathIdentity:
        return self.validate_confined_path(value, approved_roots, directory=False)

    def revalidate_root(self, value: os.PathLike[str] | str, expected: PathIdentity) -> None:
        self.revalidate_directory(value, expected)

    def revalidate_input(self, value: os.PathLike[str] | str, expected: PathIdentity) -> None:
        self.revalidate_file(value, expected)

    def profile_identity_key(self) -> bytes:
        return self._identity_key()

    def project_lock(self, directory: os.PathLike[str] | str) -> NamedProjectLock:
        identity = self.pin_directory(directory)
        digest = hmac.new(self._identity_key(), b"lock\0" + identity.canonical_path.encode("utf-8"), hashlib.sha256).hexdigest()
        name = "Local\\HermesFilmSuite-" + digest
        if os.name == "nt":
            return NamedProjectLock(name)
        lock_file = state_root(self.hermes_home / "plugin-data") / (".lock-" + digest)
        ensure_private_node(lock_file.parent, directory=True)
        return NamedProjectLock(name, lock_file=lock_file)

    def pin_local_media(
        self,
        value: os.PathLike[str] | str | Sequence[os.PathLike[str] | str],
        approved_roots: Iterable[os.PathLike[str] | str] = (),
        field: str = "input_image",
        index: int = 0,
    ) -> PinnedMedia | tuple[PinnedMedia, ...]:
        approved_roots = tuple(approved_roots)
        if not approved_roots:
            _raise("invalid_project", "media requires at least one approved root")
        if isinstance(value, (list, tuple)):
            if len(value) < 1 or len(value) > MAX_INPUTS:
                _raise("invalid_request", "too many media inputs")
            created: list[PinnedMedia] = []
            try:
                for offset, path in enumerate(value):
                    created.append(
                        self._pin_one(
                            path,
                            approved_roots=approved_roots,
                            field=field,
                            index=offset,
                        )
                    )
            except Exception:
                for pinned in created:
                    self._close_pinned(pinned)
                raise
            return tuple(created)
        return self._pin_one(value, approved_roots=approved_roots, field=field, index=index)

    def _pin_one(
        self,
        value: os.PathLike[str] | str,
        *,
        approved_roots: Iterable[os.PathLike[str] | str],
        field: str,
        index: int,
    ) -> PinnedMedia:
        path = self._prepare_path(value)
        self._check_exists(path, directory=False, operation="open")
        handle, identity = _open_existing(path, directory=False, media=True)
        try:
            roots = tuple(approved_roots)
            if roots:
                root_identities = []
                for root in roots:
                    root_path = self._prepare_path(root)
                    self._check_exists(root_path, directory=True, operation="open")
                    root_handle, root_identity = _open_existing(root_path, directory=True)
                    root_handle.close()
                    root_identities.append(root_identity)
                if not any(_is_within(identity, root_identity) for root_identity in root_identities):
                    _raise("invalid_project", "media is outside approved roots")
            return self._register_pinned(handle, identity, path, field=field, index=index)
        except Exception:
            handle.close()
            raise

    def _register_pinned(
        self,
        handle: _NativeHandle,
        identity: PathIdentity,
        path: Path,
        *,
        field: str,
        index: int,
    ) -> PinnedMedia:
        """Bound-check one opened media handle and own it as a lease."""
        suffix = path.suffix.casefold()
        limit = VIDEO_LIMIT if suffix in _VIDEO_SUFFIXES or field in {"input_video", "video"} else IMAGE_LIMIT
        if identity.size > limit:
            _raise("invalid_request", "media exceeds the bounded input size")
        pinned = PinnedMedia(
            field=field,
            index=index,
            identity=identity,
            size=identity.size,
            safe_suffix=suffix if re.fullmatch(r"\.[a-z0-9]{1,12}", suffix) else "",
            _guard=_PINNED_MEDIA_GUARD,
        )
        finalizer = weakref.finalize(pinned, close_native_handle_safely, handle)
        with self._pinned_lock:
            self._leases[pinned] = _LeaseRecord(
                handle, identity, identity.size, limit, finalizer
            )
        return pinned

    def _open_staged(
        self,
        value: os.PathLike[str] | str,
        staged_root: os.PathLike[str] | str,
    ) -> tuple[_NativeHandle, PathIdentity, Path]:
        """Open one file the backend itself staged directly in ``staged_root``.

        The staged folder is backend-owned and may sit inside HERMES_HOME, so
        the HERMES_HOME refusal of ``_prepare_path`` is replaced by a stricter
        rule: the file must be a direct child of exactly this one folder.  All
        other checks (spelling, reparse components, regular file, handle-bound
        identity) are the ones approved-root media gets.  Callers pass a
        backend-chosen ``staged_root``, never a caller-supplied one.
        """
        self._identity_key()
        path = _normalise_text_path(_path_text(value))
        root = _normalise_text_path(_path_text(staged_root))
        # Refuse an unrelated private file before opening it at all. The
        # handle-derived direct-child check below independently validates the
        # actual object after the no-reparse walk (including short path names).
        if _identity_text(str(path.parent)) != _identity_text(str(root)):
            _raise("invalid_project", "media is outside the staged folder")
        _reject_reparse_components(root)
        _reject_reparse_components(path)
        self._check_exists(root, directory=True, operation="open")
        self._check_exists(path, directory=False, operation="open")
        root_handle, root_identity = _open_existing(root, directory=True)
        try:
            handle, identity = _open_existing(path, directory=False, media=True)
        finally:
            root_handle.close()
        try:
            parent = root_identity.canonical_path.rstrip("\\/")
            child = identity.canonical_path
            separator = "\\" if "\\" in parent else os.sep
            rest = child[len(parent) + 1 :] if child.startswith(parent + separator) else ""
            if not rest or "\\" in rest or "/" in rest:
                _raise("invalid_project", "media is outside the staged folder")
        except Exception:
            handle.close()
            raise
        return handle, identity, path

    def validate_staged_input(
        self,
        value: os.PathLike[str] | str,
        staged_root: os.PathLike[str] | str,
    ) -> PathIdentity:
        """Identity of one backend-staged file (see ``_open_staged``)."""
        handle, identity, _path = self._open_staged(value, staged_root)
        handle.close()
        return identity

    def pin_staged_media(
        self,
        value: os.PathLike[str] | str,
        *,
        staged_root: os.PathLike[str] | str,
        field: str = "input_image",
        index: int = 0,
    ) -> PinnedMedia:
        """Pin one backend-staged file with the same lease as approved media."""
        handle, identity, path = self._open_staged(value, staged_root)
        try:
            return self._register_pinned(handle, identity, path, field=field, index=index)
        except Exception:
            handle.close()
            raise

    def media_limit(self, pinned: PinnedMedia) -> int:
        return self._lease_record(pinned).limit

    def _assert_owned(self, pinned: PinnedMedia) -> _LeaseRecord:
        return self._lease_record(pinned)

    def _read_pinned(self, pinned: PinnedMedia, max_bytes: int) -> bytes:
        record = self._assert_owned(pinned)
        if max_bytes < 0 or max_bytes > record.limit:
            _raise("invalid_request", "bounded media read is invalid")
        if record.size > max_bytes:
            _raise("invalid_request", "media exceeds the bounded read")
        chunks: list[bytes] = []
        total = 0
        while total < max_bytes:
            remaining = min(1024 * 1024, max_bytes - total)
            if os.name == "nt":
                buffer = ctypes.create_string_buffer(remaining)
                read = wintypes.DWORD()
                if not _ReadFile(record.handle.raw, buffer, remaining, ctypes.byref(read), None):
                    _raise("project_unavailable", "media could not be read")
                data = buffer.raw[: read.value]
            else:
                data = os.read(int(record.handle.raw), remaining)
            if not data:
                break
            total += len(data)
            if total > max_bytes:
                _raise("invalid_request", "media exceeds the bounded read")
            chunks.append(data)
        if total != record.size:
            _raise("project_unavailable", "media size changed during read")
        return b"".join(chunks)

    def consume_pinned_media(self, pinned: PinnedMedia, max_bytes: int | None = None) -> bytes:
        record = self._assert_owned(pinned)
        try:
            before = _identity_from_handle(record.handle, Path("."))
            if not record.identity.same_object(before) or record.identity.size != before.size:
                _raise("project_unavailable", "media identity changed before read")
            result = self._read_pinned(pinned, record.limit if max_bytes is None else max_bytes)
            after = _identity_from_handle(record.handle, Path("."))
            if not record.identity.same_object(after) or record.identity.size != after.size:
                _raise("project_unavailable", "media identity changed after read")
            return result
        except Exception:
            self.abort_all_pinned_media()
            raise
        finally:
            self._close_pinned(pinned)

    consume = consume_pinned_media

    def _close_pinned(self, pinned: PinnedMedia) -> None:
        with self._pinned_lock:
            if not isinstance(pinned, PinnedMedia):
                return
            record = self._leases.pop(pinned, None)
        if record is not None:
            record.finalizer.detach()
            close_native_handle_safely(record.handle)

    def abort_pinned_media(self, pinned: PinnedMedia) -> None:
        if not isinstance(pinned, PinnedMedia):
            _raise("invalid_request", "media lease is invalid")
        self._close_pinned(pinned)

    abort = abort_pinned_media

    def abort_all_pinned_media(self) -> None:
        with self._pinned_lock:
            items = tuple(self._leases.keys())
        for pinned in items:
            self._close_pinned(pinned)

    def read_handle_bytes(self, handle: _NativeHandle, *, max_bytes: int) -> bytes:
        if max_bytes < 0:
            _raise("invalid_request", "invalid read bound")
        chunks: list[bytes] = []
        total = 0
        while total <= max_bytes:
            size = min(1024 * 1024, max_bytes - total + 1)
            if os.name == "nt":
                buffer = ctypes.create_string_buffer(size)
                read = wintypes.DWORD()
                if not _ReadFile(handle.raw, buffer, size, ctypes.byref(read), None):
                    _raise("project_unavailable", "file could not be read")
                chunk = buffer.raw[: read.value]
            else:
                chunk = os.read(int(handle.raw), size)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > max_bytes:
                _raise("invalid_request", "read bound exceeded")
            chunks.append(chunk)
        return b"".join(chunks)

    def read_manifest(
        self,
        directory: os.PathLike[str] | str,
        *,
        max_bytes: int = 256 * 1024,
        object_pairs_hook: Any = None,
    ) -> dict[str, Any]:
        project_dir = self._prepare_path(directory)
        self._check_exists(project_dir, directory=True, operation="open")
        project_handle, project_identity = _open_existing(project_dir, directory=True)
        manifest_handle: _NativeHandle | None = None
        try:
            try:
                manifest_handle, manifest_identity = _open_child_existing(
                    project_handle, project_dir, "project.json", directory=False
                )
            except ProjectError as exc:
                if exc.code == "project_unavailable":
                    _raise("invalid_project", "project manifest is missing")
                raise
            try:
                if manifest_identity.size > max_bytes:
                    _raise("invalid_project", "manifest is too large")
                data = self.read_handle_bytes(manifest_handle, max_bytes=max_bytes)
                current_manifest_identity = _identity_from_handle(
                    manifest_handle, project_dir / "project.json"
                )
                if current_manifest_identity != manifest_identity:
                    _raise("project_unavailable", "project manifest identity changed")
            finally:
                manifest_handle.close()
                manifest_handle = None
            # The directory handle binds the read, while this second identity
            # check proves that its pathname was not rebound during the read.
            self.revalidate_directory(project_dir, project_identity)
        finally:
            if manifest_handle is not None:
                manifest_handle.close()
            project_handle.close()
        value = _safe_json_loads(data, object_pairs_hook=object_pairs_hook)
        if not isinstance(value, dict):
            _raise("invalid_project", "manifest must be an object")
        return value


def _junction_privilege_denied(result: subprocess.CompletedProcess[str]) -> bool:
    output = "\n".join(
        part.strip()
        for part in (getattr(result, "stdout", ""), getattr(result, "stderr", ""))
        if part
    )
    return (
        "ERROR_PRIVILEGE_NOT_HELD (1314)" in output
        or "ERROR_PRIVILEGE_NOT_HELD" in output
        or "You do not have sufficient privilege to perform this operation." in output
        or getattr(result, "returncode", None) == 1314
    )


def create_junction(link: os.PathLike[str] | str, target: os.PathLike[str] | str) -> None:
    """Create a test-only junction with one exact privilege-denial mapping."""

    if os.name != "nt":
        _raise(WINDOWS_JUNCTION_PRIVILEGE_UNAVAILABLE, WINDOWS_JUNCTION_PRIVILEGE_UNAVAILABLE)
    link_text = _path_text(link)
    target_text = _path_text(target)
    try:
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", link_text, target_text],
            capture_output=True,
            text=True,
            check=False, env=child_env(),
        )
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314 or getattr(exc, "errno", None) == 1314:
            _raise(WINDOWS_JUNCTION_PRIVILEGE_UNAVAILABLE, WINDOWS_JUNCTION_PRIVILEGE_UNAVAILABLE)
        raise
    if result.returncode == 0:
        return
    if _junction_privilege_denied(result):
        _raise(WINDOWS_JUNCTION_PRIVILEGE_UNAVAILABLE, WINDOWS_JUNCTION_PRIVILEGE_UNAVAILABLE)
    _raise("project_unavailable", "junction creation failed")


def pin_local_media(
    path: os.PathLike[str] | str,
    approved_roots: Iterable[os.PathLike[str] | str] = (),
    boundary: PathBoundary | None = None,
    field: str = "input_image",
    index: int = 0,
) -> PinnedMedia:
    return (boundary or PathBoundary()).pin_local_media(
        path, approved_roots=approved_roots, field=field, index=index
    )


def consume_pinned_media(
    boundary: PathBoundary, pinned: PinnedMedia, max_bytes: int | None = None
) -> bytes:
    return boundary.consume_pinned_media(pinned, max_bytes=max_bytes)


def abort_pinned_media(boundary: PathBoundary, pinned: PinnedMedia) -> None:
    boundary.abort_pinned_media(pinned)


__all__ = [
    "IMAGE_LIMIT",
    "MAX_INPUTS",
    "PathBoundary",
    "PathIdentity",
    "PinnedMedia",
    "ProjectError",
    "VIDEO_LIMIT",
    "WINDOWS_JUNCTION_PRIVILEGE_UNAVAILABLE",
    "NamedProjectLock",
    "abort_pinned_media",
    "consume_pinned_media",
    "create_junction",
    "ensure_private_node",
    "pin_local_media",
    "profile_identity_key",
]
