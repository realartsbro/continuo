"""Exactly reproducible engine environments: hash locks, lock-only installs, receipts, read-only verify.

    python scripts/engine_env.py lock <engine-dir> <engine> [--out-dir scripts/engine-locks] [--exclude-newer ISO]
    python scripts/engine_env.py verify <engine-dir> <lock>

The measured quality of an analysis engine (song, scene, beat) holds only for the exact package set
that produced it.  So each engine has ONE lock per torch flavour in ``scripts/engine-locks``:

    <engine>.txt          the live CUDA set (torch family ``+cu128``), every ``name==version`` with all
                          of its ``--hash=sha256:`` lines (``uv pip compile --generate-hashes``)
    <engine>.cpu.txt      the same versions with the torch family's local tag ``+cpu``
    <engine>.freeze.txt   the ``uv pip freeze`` of the live venv the locks were compiled from

The lock header carries what the install depends on (``# key: value``): engine, python, uv,
index-url, extra-index-url, index-strategy, exclude-newer, platform, source, exceptions.  An installer
installs ONLY from a lock (``install_argv``: ``--require-hashes -r <lock> --exclude-newer <date>`` with
the lock's own indexes) and writes ``receipt`` into engine.json under ``env``.

``verify`` runs ``uv pip freeze`` for the engine's venv and prints ``OK`` or one line per difference
(``missing``, ``extra``, ``version a -> b``); exit 0 on OK, 1 on drift.  It never modifies anything.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = ROOT / "scripts" / "engine-locks"
ENGINES = ("song-analysis", "scene-analysis", "beat-this")
PYPI = "https://pypi.org/simple"
TORCH_INDEX = {"cuda": "https://download.pytorch.org/whl/cu128", "cpu": "https://download.pytorch.org/whl/cpu"}
INDEX_STRATEGY = "unsafe-best-match"  # as the installers have always resolved: torch from the PyTorch index
PLATFORM = "x86_64-pc-windows-msvc"
TORCH_FAMILY = ("torch", "torchaudio", "torchvision")
HEADER_KEYS = ("engine", "python", "uv", "index-url", "extra-index-url", "index-strategy", "exclude-newer",
               "platform", "source", "exceptions")
RECEIPT_SCHEMA = "studio.engine-env/1"
RECEIPT_KEYS = ("schema", "lock", "lock_sha256", "python_version", "uv_version", "exclude_newer", "freeze_sha256")

Runner = Callable[[list], subprocess.CompletedProcess]
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)")
_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")


class EnvError(SystemExit):
    """Refusal with a message the user can act on (SystemExit so an installer stops)."""


def _run(argv: list) -> subprocess.CompletedProcess:
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True)


def _checked(runner: Runner, argv: list, what: str) -> str:
    out = runner(argv)
    if out.returncode != 0:
        raise EnvError(f"{what} failed (exit {out.returncode}): {(out.stderr or '').strip()[-600:]}")
    return out.stdout or ""


def norm_name(name: str) -> str:
    """PEP 503 normalised project name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def venv_python(venv: Path) -> Path:
    return Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


# ---------------------------------------------------------------- freeze
def parse_freeze(text: str) -> tuple[dict, list]:
    """``uv pip freeze`` output -> ({normalised name: version}, [exception lines]).

    An exception is any installed distribution that is not a plain ``name==version`` from an index
    (``name @ file://...``, ``-e <path>``, VCS URLs): it cannot be hashed from an index and is reported,
    never dropped silently.  Its local path is stripped, only the name and the kind remain.
    """
    pins, exceptions = {}, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("Using Python"):
            continue
        m = _REQ.match(line)
        if m and " @ " not in line:
            pins[norm_name(m.group(1))] = m.group(2)
            continue
        if line.startswith("-e "):
            exceptions.append("editable install (local path)")
            continue
        name, _sep, url = line.partition(" @ ")
        kind = "vcs url" if url.strip().startswith(("git+", "hg+", "svn+", "bzr+")) else "local file or url"
        exceptions.append(f"{norm_name(name.strip())} ({kind})")
    return pins, exceptions


def freeze_lines(pins: dict) -> list[str]:
    """Sorted, normalised ``name==version`` lines (the form a receipt hashes and a freeze file stores)."""
    return [f"{name}=={pins[name]}" for name in sorted(pins)]


def freeze_sha256(pins: dict) -> str:
    return sha256_bytes(("\n".join(freeze_lines(pins)) + "\n").encode("utf-8"))


def live_freeze(python: Path | str, uv: str = "uv", runner: Runner = _run) -> tuple[dict, list]:
    """Read-only: the installed set of the venv that owns ``python``."""
    return parse_freeze(_checked(runner, [uv, "pip", "freeze", "--python", str(python)], "uv pip freeze"))


def python_version(python: Path | str, runner: Runner = _run) -> str:
    return _checked(runner, [str(python), "-c", "import platform; print(platform.python_version())"],
                    "python version probe").strip()


def uv_version(uv: str = "uv", runner: Runner = _run) -> str:
    text = _checked(runner, [uv, "--version"], "uv --version")
    m = re.search(r"(\d+\.\d+\.\d+)", text)
    if not m:
        raise EnvError(f"cannot read the uv version from {text!r}")
    return m.group(1)


# ---------------------------------------------------------------- lock files
def lock_path(engine: str, cpu_only: bool = False, lock_dir: Path = LOCK_DIR) -> Path:
    if engine not in ENGINES:
        raise EnvError(f"unknown engine {engine!r} (one of {', '.join(ENGINES)})")
    return Path(lock_dir) / (f"{engine}.cpu.txt" if cpu_only else f"{engine}.txt")


def read_lock(path: Path) -> tuple[dict, dict]:
    """(header {key: value}, {normalised name: (version, [--hash=sha256:...])}).

    Refuses a requirement without ``==`` or without a hash: such a lock cannot be installed with
    ``--require-hashes`` and is not a lock.
    """
    path = Path(path)
    header, reqs, current = {}, {}, None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = re.match(r"#\s*([a-z-]+):\s*(.*)$", line)
            if m and current is None and m.group(1) not in header:
                header[m.group(1)] = m.group(2).strip()
            continue
        if line.startswith("--hash="):
            h = line.rstrip(" \\")
            if current is None or not _HASH.fullmatch(h):
                raise EnvError(f"{path.name}: malformed hash line {line!r}")
            reqs[current][1].append(h)
            continue
        m = _REQ.match(line)
        if not m:
            raise EnvError(f"{path.name}: not a pinned requirement: {line!r}")
        current = norm_name(m.group(1))
        reqs[current] = (m.group(2), [])
    for name, (version, hashes) in reqs.items():
        if not hashes:
            raise EnvError(f"{path.name}: {name}=={version} has no --hash=sha256 line")
    missing = [k for k in HEADER_KEYS if k not in header]
    if missing:
        raise EnvError(f"{path.name}: header lacks {', '.join(missing)}")
    return header, reqs


def lock_pins(path: Path) -> dict:
    return {name: version for name, (version, _h) in read_lock(path)[1].items()}


def cpu_twin(pins: dict) -> dict:
    """The CPU flavour of a CUDA set: only the torch family's local version tag changes."""
    return {name: (re.sub(r"\+cu\d+$", "+cpu", v) if name in TORCH_FAMILY else v) for name, v in pins.items()}


def compile_lock(engine: str, pins: dict, exceptions: list, out: Path, *, flavour: str, python: str, uv_ver: str,
                 exclude_newer: str, uv: str = "uv", runner: Runner = _run) -> Path:
    """Write ``out``: header + ``uv pip compile --generate-hashes`` of exactly ``pins`` (no resolution freedom)."""
    out = Path(out)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.txt"
        dst = Path(tmp) / "out.txt"
        src.write_text("\n".join(freeze_lines(pins)) + "\n", encoding="utf-8")
        _checked(runner, [uv, "pip", "compile", str(src), "-o", str(dst), "--generate-hashes", "--no-header",
                          "--no-annotate", "--python-version", python, "--python-platform", PLATFORM,
                          "--index-url", PYPI, "--extra-index-url", TORCH_INDEX[flavour],
                          "--index-strategy", INDEX_STRATEGY, "--exclude-newer", exclude_newer],
                 f"uv pip compile ({engine}, {flavour})")
        body = dst.read_text(encoding="utf-8")
    header = {
        "engine": engine, "python": python, "uv": uv_ver, "index-url": PYPI, "extra-index-url": TORCH_INDEX[flavour],
        "index-strategy": INDEX_STRATEGY, "exclude-newer": exclude_newer, "platform": PLATFORM,
        "source": f"live venv freeze ({engine}.freeze.txt)" + ("; torch family +cu128 -> +cpu" if flavour == "cpu" else ""),
        "exceptions": "; ".join(exceptions) if exceptions else "none",
    }
    lines = ["# Film Suite engine lock: install ONLY with scripts/engine_env.py install_argv (--require-hashes).",
             "# Regenerate: python scripts/engine_env.py lock <engine-dir> <engine>  (never edit by hand)"]
    lines += [f"# {k}: {header[k]}" for k in HEADER_KEYS]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n" + body.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    got = lock_pins(out)
    if got != pins:
        diff = sorted(set(got.items()) ^ set(pins.items()))
        raise EnvError(f"{out.name}: the compiled lock is not the frozen set: {diff[:8]}")
    return out


def write_locks(engine: str, engine_dir: Path, *, exclude_newer: str | None = None, lock_dir: Path = LOCK_DIR,
                uv: str = "uv", runner: Runner = _run) -> list[Path]:
    """Freeze the live venv (read-only) and write the freeze, the CUDA lock and the CPU twin."""
    py = venv_python(Path(engine_dir) / "venv")
    pins, exceptions = live_freeze(py, uv, runner)
    pyver, uvver = python_version(py, runner), uv_version(uv, runner)
    stamp = exclude_newer or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lock_dir = Path(lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    freeze = lock_dir / f"{engine}.freeze.txt"
    freeze.write_text(f"# uv pip freeze of the live {engine} venv (python {pyver}, uv {uvver}, {stamp}); paths stripped\n"
                      + "\n".join(freeze_lines(pins) + [f"# exception: {e}" for e in exceptions]) + "\n",
                      encoding="utf-8", newline="\n")
    out = [freeze]
    for flavour, set_ in (("cuda", pins), ("cpu", cpu_twin(pins))):
        out.append(compile_lock(engine, set_, exceptions, lock_path(engine, flavour == "cpu", lock_dir), flavour=flavour,
                                python=pyver, uv_ver=uvver, exclude_newer=stamp, uv=uv, runner=runner))
    return out


# ---------------------------------------------------------------- install + receipt
def install_argv(uv: str, python: Path | str, lock: Path) -> list[str]:
    """The ONE install command an installer runs: every package from the lock, hash-checked."""
    header, _reqs = read_lock(lock)
    return [uv, "pip", "install", "--python", str(python), "--require-hashes", "-r", str(lock),
            "--index-url", header["index-url"], "--extra-index-url", header["extra-index-url"],
            "--index-strategy", header["index-strategy"], "--exclude-newer", header["exclude-newer"]]


def receipt(lock: Path, python: Path | str, uv: str = "uv", runner: Runner = _run) -> dict:
    """What engine.json records under ``env`` after an install (read from the venv, not assumed)."""
    header, _reqs = read_lock(lock)
    pins, exceptions = live_freeze(python, uv, runner)
    rec = {
        "schema": RECEIPT_SCHEMA,
        "lock": Path(lock).name,
        "lock_sha256": sha256_file(lock),
        "python_version": python_version(python, runner),
        "uv_version": uv_version(uv, runner),
        "exclude_newer": header["exclude-newer"],
        "freeze_sha256": freeze_sha256(pins),
    }
    if exceptions:
        rec["freeze_exceptions"] = exceptions
    return rec


def install(uv: str, python: Path | str, lock: Path, runner: Runner = _run, echo: bool = True) -> dict:
    """Install from ``lock`` only, refuse a venv whose Python is not the lock's, then return the receipt."""
    header, _reqs = read_lock(lock)
    have = python_version(python, runner)
    if have != header["python"]:
        raise EnvError(f"venv python {have} is not the lock's {header['python']} ({Path(lock).name}); "
                       f"remove the venv and re-run the installer")
    argv = install_argv(uv, python, lock)
    if echo:
        print("+", " ".join(argv), flush=True)
    _checked(runner, argv, "uv pip install --require-hashes")
    rec = receipt(lock, python, uv, runner)
    drift = diff(lock_pins(lock), live_freeze(python, uv, runner)[0])
    if drift:
        raise EnvError("installed set differs from the lock: " + "; ".join(drift))
    return rec


# ---------------------------------------------------------------- verify
def diff(want: dict, have: dict) -> list[str]:
    """One line per difference between a lock (want) and a freeze (have), sorted by name."""
    out = []
    for name in sorted(set(want) | set(have)):
        if name not in have:
            out.append(f"missing {name}=={want[name]}")
        elif name not in want:
            out.append(f"extra {name}=={have[name]}")
        elif want[name] != have[name]:
            out.append(f"version {name} {want[name]} -> {have[name]}")
    return out


def verify(engine_dir: Path, lock: Path, uv: str = "uv", runner: Runner = _run) -> tuple[int, list[str]]:
    """(exit code, report lines).  Read-only: runs ``uv pip freeze`` only."""
    py = venv_python(Path(engine_dir) / "venv")
    if not py.is_file():
        return 1, [f"missing venv python {py}"]
    pins, exceptions = live_freeze(py, uv, runner)
    lines = diff(lock_pins(lock), pins) + [f"unhashable {e}" for e in exceptions]
    return (1, lines) if lines else (0, ["OK"])


def main(argv: list[str], runner: Runner = _run) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify", help="compare an engine's venv with a lock (read-only); exit 1 on drift")
    v.add_argument("engine_dir", type=Path)
    v.add_argument("lock", type=Path)
    lk = sub.add_parser("lock", help="freeze a live engine venv (read-only) and write its hash locks")
    lk.add_argument("engine_dir", type=Path)
    lk.add_argument("engine", choices=ENGINES)
    lk.add_argument("--out-dir", type=Path, default=LOCK_DIR)
    lk.add_argument("--exclude-newer", default=None, help="ISO-8601 UTC cut-off (default: now)")
    args = ap.parse_args(argv)
    uv = shutil.which("uv") or "uv"
    if args.cmd == "verify":
        code, lines = verify(args.engine_dir, args.lock, uv, runner)
        print("\n".join(lines))
        return code
    for path in write_locks(args.engine, args.engine_dir, exclude_newer=args.exclude_newer, lock_dir=args.out_dir,
                            uv=uv, runner=runner):
        print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
