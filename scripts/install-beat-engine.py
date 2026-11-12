"""Install the Film Suite Beat This! engine (lane fs-beat) into its OWN uv venv.

    python scripts/install-beat-engine.py [--engine-dir DIR] [--cpu-only] [--smoke AUDIO]

Creates ``%LOCALAPPDATA%/hermes/film-suite/engines/beat-this/``:
    venv/          uv venv, the lock's python, hash-locked stack (scripts/engine-locks) (never the Hermes venv,
                   never ComfyUI's python)
    runner.py      copy of backend/film-suite/dashboard/music_core/beat_runner.py
    final0.ckpt    Beat This! checkpoint, sha256-verified
    engine.json    record the backend reads (music_core/beat_engine.locate)

Idempotent: re-running keeps a verified checkpoint and re-installs from the hash lock
(scripts/engine-locks/beat-this.txt, --require-hashes): the exact stack the fs-beat measurement
ran on (out/fs-beat/DECODER.md); change it only with a new measurement.  Remove the directory to uninstall; the Music tab then falls back
to the librosa analyzer and says so.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER_SRC = (ROOT / "backend" / "film-suite" / "dashboard" if (ROOT / "backend" / "film-suite" / "dashboard").is_dir() else ROOT / "dashboard") / "music_core" / "beat_runner.py"
_env_spec = importlib.util.spec_from_file_location("film_suite_engine_env", Path(__file__).resolve().parent / "engine_env.py")
engine_env = importlib.util.module_from_spec(_env_spec)
_env_spec.loader.exec_module(engine_env)
# Packages: ONLY the hash lock scripts/engine-locks/beat-this.txt (CUDA) or .cpu.txt (--cpu-only), compiled from
# the live venv the measurements ran on (scripts/engine_env.py lock).  Change it only with a new measurement.
ENGINE = "beat-this"
PYTHON = engine_env.read_lock(engine_env.lock_path(ENGINE))[0]["python"]  # the exact interpreter of the lock
CHECKPOINT_URL = "https://cloud.cp.jku.at/public.php/dav/files/7ik4RrBKTS273gp/final0.ckpt"
CHECKPOINT_SHA256 = "8c328b45f59d8dd3dff219253ff6a8d6482be57d0133a29140e2febbf8eb8331"


def default_engine_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "hermes" / "film-suite" / "engines" / "beat-this"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env=engine_env.engine_process.child_env())


def fetch_checkpoint(target: Path, source: str | None) -> None:
    if target.is_file() and sha256(target) == CHECKPOINT_SHA256:
        print(f"checkpoint ok: {target}")
        return
    tmp = target.with_suffix(".part")
    if source and Path(source).is_file():
        shutil.copyfile(source, tmp)
    else:
        print(f"downloading {CHECKPOINT_URL}", flush=True)
        with urllib.request.urlopen(source or CHECKPOINT_URL, timeout=120) as response, tmp.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(response, out)
    digest = sha256(tmp)
    if digest != CHECKPOINT_SHA256:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"checkpoint sha256 mismatch: {digest}")
    tmp.replace(target)


def install_packages(uv: str, py: str, cpu_only: bool, runner=None) -> dict:
    """``uv pip install --require-hashes -r <lock> --exclude-newer <lock date>``; returns the env receipt."""
    lock = engine_env.lock_path(ENGINE, cpu_only)
    return engine_env.install(uv, py, lock, **({"runner": runner} if runner else {}))


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine-dir", type=Path, default=default_engine_dir())
    ap.add_argument("--cpu-only", action="store_true", help="CPU torch wheels (no CUDA)")
    ap.add_argument("--checkpoint-from", help="local file or URL to take final0.ckpt from (sha-checked)")
    ap.add_argument("--smoke", help="analyse this audio file once after installing (CPU)")
    args = ap.parse_args(argv)
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv not found on PATH")
    edir: Path = args.engine_dir
    edir.mkdir(parents=True, exist_ok=True)
    venv = edir / "venv"
    if not venv_python(venv).is_file():
        run([uv, "venv", "-p", PYTHON, str(venv)])
    py = str(venv_python(venv))
    env_receipt = install_packages(uv, py, args.cpu_only)
    shutil.copyfile(RUNNER_SRC, edir / "runner.py")
    ckpt = edir / "final0.ckpt"
    fetch_checkpoint(ckpt, args.checkpoint_from)
    probe = subprocess.run([py, "-c", "import beat_this, torch, sys; print(torch.__version__, torch.cuda.is_available(), sys.version.split()[0])"],
                           capture_output=True, text=True, check=True, env=engine_env.engine_process.engine_env()).stdout.split()
    record = {
        "schema": "studio.beat-engine/1",
        "python": py,
        "runner": str(edir / "runner.py"),
        "runner_sha256": sha256(edir / "runner.py"),
        "checkpoint": str(ckpt),
        "checkpoint_sha256": CHECKPOINT_SHA256,
        "versions": {"beat_this": "1.1.0", "torch": probe[0], "python": probe[2]},
        "cuda_available": probe[1] == "True",
        "pins": [f"{n}=={v}" for n, v in sorted(engine_env.lock_pins(engine_env.lock_path(ENGINE, args.cpu_only)).items())],
    }
    record["env"] = env_receipt  # lock sha256, python, uv, exclude-newer, sha256 of the post-install freeze
    (edir / "engine.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record, indent=2))
    if args.smoke:
        req = {"schema": "studio.beat-runner-request/1", "audio_path": args.smoke, "device": "cpu",
               "checkpoint": str(ckpt), "checkpoint_sha256": CHECKPOINT_SHA256}
        out = subprocess.run([py, str(edir / "runner.py")], input=json.dumps(req), capture_output=True, text=True,
                             env=engine_env.engine_process.engine_env())
        if out.returncode != 0:
            print(out.stderr[-2000:], file=sys.stderr)
            return out.returncode
        result = json.loads(out.stdout.strip().splitlines()[-1])
        print(f"smoke: {len(result['beats'])} beats, {len(result['downbeats'])} downbeats on {result['device']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
