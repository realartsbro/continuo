"""One way to start a child process (lanes p14-sam AUD-062 and w39-env).

Hermes loads the user's ``.env`` into its own process, so the backend's ``os.environ`` can hold every provider key.
No child of this plugin (ffmpeg, ffprobe, nvidia-smi, the file manager, an engine's Python) gets that environment.
``child_env()`` builds the child's environment from an ALLOWLIST: the variables an operating system needs to start a
program, find its libraries and its temp folder, plus what the call site names itself (``extra``).  Everything else
stays in the backend: no ``*_API_KEY`` / ``*_TOKEN`` / ``*_SECRET``, no ``HERMES_*``, no proxy settings, no
``PYTHON*``.  A refused name is refused in ``extra`` too (``ValueError``), so no call site can hand a secret on.

``engine_env()`` is the same environment for a foreign engine Python: every owned engine (song map, scene analysis,
camera flow, LaMa, Beat This!, speech recognition) runs in its own venv.  The backend itself runs with PYTHONPATH /
PYTHONHOME pointing at the plugin's dependency env (another NumPy): inherited, the engine imports the backend's
packages and dies (live 01.11.: "No module named numpy._core._multiarray_umath" on every song-map track).  Same
isolation as ``uv run`` or an embedded venv: the child sees no ``PYTHON*`` variable, no ``VIRTUAL_ENV`` /
``CONDA_PREFIX``, and runs with ``-I`` (no environment paths, no user site, no script folder on ``sys.path``; a
venv's own ``.pth`` files still apply, which is how the song engine reaches its ``vendor/`` folder; runners that
need their own folder insert it themselves).  On top of ``child_env`` an engine gets the GPU selection and the model
cache locations (``ENGINE_ALLOWED``).

Standalone (stdlib only): loaded by path from every module that starts a process.
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Mapping

# name -> why a child needs it.  Matched without regard to case on Windows (``Path`` is ``PATH`` there).
ALLOWED: dict[str, str] = {
    "SystemRoot": "Windows: the system folder; without it a child cannot load system DLLs or open a socket",
    "windir": "Windows: the same folder under its older name, read by older libraries",
    "SystemDrive": "Windows: the drive of the system folder, used to resolve drive-relative paths",
    "PATH": "where the child finds its own DLLs / shared libraries and helper programs",
    "PATHEXT": "Windows: which suffixes count as programs when a child starts a helper by name",
    "TEMP": "the temp folder (Windows; also honoured by ffmpeg and Python)",
    "TMP": "the temp folder under its second name",
    "TMPDIR": "the temp folder on POSIX",
    "USERPROFILE": "Windows: the user's home folder (an engine's model cache defaults to a folder inside it)",
    "HOME": "POSIX: the user's home folder (same reason)",
    "HOMEDRIVE": "Windows: first half of the home folder for libraries that do not read USERPROFILE",
    "HOMEPATH": "Windows: second half of that home folder",
    # The login name: Python's getpass.getuser() reads LOGNAME, USER, LNAME, USERNAME and otherwise imports the POSIX-only
    # `pwd` (on Windows: ModuleNotFoundError). torch names its compile cache with it at import time, so without these the
    # song engine's words task failed for every user (w39-rcfix item 1).
    "USERNAME": "Windows: the login name; getpass.getuser() (torch's cache folder) fails without it",
    "USER": "POSIX: the login name, read by getpass.getuser() before the password database",
    "LOGNAME": "POSIX: the login name under its older name, read first by getpass.getuser()",
    "LOCALAPPDATA": "Windows: per-user program data, where the engines and their caches are installed",
    "APPDATA": "Windows: roaming per-user program data, read by libraries for their settings folder",
    "ProgramData": "Windows: machine-wide program data (GPU driver files are looked up there)",
    "ProgramFiles": "Windows: known-folder lookup of installed programs by a child",
    "ProgramFiles(x86)": "Windows: the same for 32-bit programs",
    "ProgramW6432": "Windows: the same as seen from a 32-bit process",
    "CommonProgramFiles": "Windows: shared components of installed programs",
    "CommonProgramFiles(x86)": "Windows: the same for 32-bit programs",
    "CommonProgramW6432": "Windows: the same as seen from a 32-bit process",
    "COMSPEC": "Windows: the command interpreter, needed when a child starts a .cmd / .bat helper",
    "NUMBER_OF_PROCESSORS": "Windows: thread pool size of ffmpeg and of numeric libraries",
    "PROCESSOR_ARCHITECTURE": "Windows: which build of a library a child loads",
    "OS": "Windows: read by libraries that tell Windows from POSIX through the environment",
    "LANG": "locale: how a child decodes file names and prints messages",
    "LANGUAGE": "locale: message language order",
    "TZ": "time zone of the time stamps a child writes",
    "LD_LIBRARY_PATH": "Linux: where a child finds shared libraries outside the system folders",
    "DYLD_LIBRARY_PATH": "macOS: the same",
    "DYLD_FALLBACK_LIBRARY_PATH": "macOS: the same, as fallback",
}
# Whole families, by prefix.
ALLOWED_PREFIXES: dict[str, str] = {"LC_": "locale categories (LC_ALL, LC_CTYPE, ...)"}

# Only for a foreign engine Python (``engine_env``): GPU selection and where its models are cached.
ENGINE_ALLOWED: dict[str, str] = {
    "CUDA_VISIBLE_DEVICES": "which GPUs torch may use (the user's own selection must reach the engine)",
    "CUDA_DEVICE_ORDER": "how CUDA_VISIBLE_DEVICES numbers the GPUs",
    "HF_HOME": "a moved Hugging Face cache: the engines load their weights offline from it",
    "HF_HUB_CACHE": "the same, hub cache only",
    "HUGGINGFACE_HUB_CACHE": "the same under its older name",
    "TRANSFORMERS_CACHE": "the same for older transformers releases",
    "XDG_CACHE_HOME": "POSIX: the base of the default model cache",
    "TORCH_HOME": "a moved torch hub cache",
}
# Only for the file manager (``desktop=True``, "Show in folder"): the POSIX desktop session it must join.
DESKTOP_SESSION: dict[str, str] = {
    "DISPLAY": "X11 display",
    "WAYLAND_DISPLAY": "Wayland display",
    "XAUTHORITY": "the NAME of the X11 authority file (not its content)",
    "XDG_RUNTIME_DIR": "the session's socket folder",
    "XDG_CURRENT_DESKTOP": "which file manager xdg-open starts",
    "XDG_SESSION_TYPE": "x11 or wayland",
    "XDG_DATA_DIRS": "where xdg-open finds the file manager's entry",
    "XDG_CONFIG_DIRS": "the same for its settings",
    "DBUS_SESSION_BUS_ADDRESS": "the session bus the file manager is reached on",
    "DESKTOP_SESSION": "the desktop's name",
}

_DROPPED = frozenset({"VIRTUAL_ENV", "CONDA_PREFIX", "__PYVENV_LAUNCHER__"})
# A name with one of these parts (between underscores) never reaches a child, whoever asks.
_SECRET_PART = re.compile(r"(?:^|_)(?:API_?KEY|APIKEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIALS?|AUTH|COOKIE)(?:$|_)")
_PROXY = re.compile(r"(?:^|_)PROXY$")


def refused(name: str) -> str | None:
    """Why ``name`` never reaches a child, or None.  Case does not matter."""
    upper = str(name).upper()
    if upper.startswith("HERMES_"):
        return "Hermes's own state"
    if upper.startswith("PYTHON") or upper in _DROPPED:
        return "steers a Python interpreter"
    if _SECRET_PART.search(upper):
        return "names a credential"
    if _PROXY.search(upper):
        return "proxy setting (may carry a password)"
    return None


def child_env(extra: Mapping[str, str] | None = None, *, desktop: bool = False, passthrough: Iterable[str] = (),
              environ: Mapping[str, str] | None = None, windows: bool | None = None) -> dict[str, str]:
    """The minimal environment of a child process: the allowlist, ``passthrough`` names, then ``extra``.

    ``passthrough`` names further variables to copy from the backend when they are set; ``extra`` sets values.  A
    refused name (``refused``) in either raises ``ValueError``.  ``environ`` / ``windows`` exist for tests.
    """
    source = os.environ if environ is None else environ
    fold = str.upper if (os.name == "nt" if windows is None else windows) else str
    names = [*ALLOWED, *(DESKTOP_SESSION if desktop else ())]
    for name in passthrough:
        reason = refused(name)
        if reason:
            raise ValueError(f"child_env refuses {name}: {reason}")
        names.append(name)
    wanted = {fold(name) for name in names}
    prefixes = tuple(fold(prefix) for prefix in ALLOWED_PREFIXES)
    env: dict[str, str] = {}
    spelled: dict[str, str] = {}  # folded name -> the spelling that is in env
    for key, value in source.items():
        folded = fold(key)
        if folded in spelled or refused(key) or not (folded in wanted or folded.startswith(prefixes)):
            continue
        env[key] = value
        spelled[folded] = key
    for key, value in (extra or {}).items():
        reason = refused(key)
        if reason:
            raise ValueError(f"child_env refuses {key}: {reason}")
        folded = fold(key)
        if spelled.get(folded, key) != key:  # Windows: ``Path`` given, ``PATH`` inherited: one variable
            del env[spelled[folded]]
        env[key] = str(value)
        spelled[folded] = key
    return env


def engine_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """``child_env`` for a foreign engine Python: plus GPU selection and model cache locations, plus ``extra``."""
    return child_env(extra, passthrough=ENGINE_ALLOWED)


def engine_command(python: str | os.PathLike[str], *args: str | os.PathLike[str], flags: tuple[str, ...] = ()) -> list[str]:
    """``[python, -I, *flags, *args]``: the engine's interpreter, isolated."""
    return [str(python), "-I", *flags, *(str(a) for a in args)]


__all__ = ["ALLOWED", "ALLOWED_PREFIXES", "DESKTOP_SESSION", "ENGINE_ALLOWED", "child_env", "engine_command", "engine_env", "refused"]
