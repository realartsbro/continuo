"""Install the Film Suite song-analysis engine (lane w6-song) into its OWN uv venv.

    python scripts/install-song-engine.py [--engine-dir DIR] [--models-dir DIR] [--no-download] [--cpu-only]
                                          [--source-root DIR] [--smoke AUDIO]

Creates ``%LOCALAPPDATA%/hermes/film-suite/engines/song-analysis/``:
    venv/          uv venv, the lock's python, hash-locked stack (scripts/engine-locks) (never the Hermes venv,
                   never ComfyUI's python)
    runner.py      copy of backend/film-suite/dashboard/music_core/song_map_runner.py
    src/ADTOF-pytorch/      the installer's own sparse checkout of the pinned commit (table SOURCES)
    vendor/adtof_pytorch/   the four source files of that checkout (sha256-checked; the repository's
                   bundled weights are never checked out or copied)
    engine.json    record the backend reads (music_core/song_map.locate), schema
                   studio.song-engine/1: python, runner + sha256, every model file's path + sha256,
                   versions, pins, sources (URL + commit)
and fills ``engines/models/`` (``--models-dir``) with the model files the runner reads: ADTOF, RMVPE,
Qwen3-ForcedAligner-0.6B and SongFormer, 5.0 GB, each from its publisher at one pinned revision
(``scripts/engine_models.py``).  A file that is already there is checked and kept; ``--no-download`` fetches nothing
and lists what is missing.

Third-party code is fetched by this installer at a pinned full commit (table SOURCES) from its
upstream URL, or from ``--source-root DIR`` (one local clone per repository name) for offline use.
A wrong sha or a commit the remote no longer has is refused.  ADTOF-pytorch has no licence: its
code never enters the Film Suite repository, only this engine folder.

Every model file must have its pinned sha256; a file with another one is refused, never replaced.  Idempotent:
re-running re-installs from the lock (--require-hashes), re-checks every hash and resumes an interrupted download.
Remove the directory to uninstall; the Music page then names each map track "Not installed".
Decisions and numbers: eval/bench/RESULTS.md (B1 SongFormer, B2 ADTOF, B3 RMVPE, B4 Qwen3-FA).
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("film_suite_pinned_source", Path(__file__).resolve().parent / "pinned_source.py")
pinned_source = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pinned_source)
_env_spec = importlib.util.spec_from_file_location("film_suite_engine_env", Path(__file__).resolve().parent / "engine_env.py")
engine_env = importlib.util.module_from_spec(_env_spec)
_env_spec.loader.exec_module(engine_env)
_models_spec = importlib.util.spec_from_file_location("film_suite_engine_models", Path(__file__).resolve().parent / "engine_models.py")
engine_models = importlib.util.module_from_spec(_models_spec)
_models_spec.loader.exec_module(engine_models)
RUNNER_SRC = (ROOT / "backend" / "film-suite" / "dashboard" if (ROOT / "backend" / "film-suite" / "dashboard").is_dir() else ROOT / "dashboard") / "music_core" / "song_map_runner.py"
SCHEMA = "studio.song-engine/1"
# Packages: ONLY the hash lock engine_env.lock_path picks: scripts/engine-locks/song-analysis.txt (CUDA), .cpu.txt
# (--cpu-only), .macos.txt (macOS) or .linux.txt (CUDA on Linux, plus torch's CUDA runtime wheels), compiled from
# the live venv the measurements ran on (scripts/engine_env.py lock).  Change it only with a new measurement.
ENGINE = "song-analysis"
PYTHON = engine_env.read_lock(engine_env.lock_path(ENGINE))[0]["python"]  # the exact interpreter of the lock
# Third-party code, fetched by the installer itself at ONE full commit (scripts/pinned_source.py).
# | repository    | URL                                       | commit                                   |
# | ADTOF-pytorch | https://github.com/xavriley/ADTOF-pytorch | 85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9 |
SOURCES = {
    "ADTOF-pytorch": {"url": "https://github.com/xavriley/ADTOF-pytorch",
                      "commit": "85c192e78f716ea0b111cc8a5ee4a8f6a3a4f8a9"},
}
ADTOF_DIR = "src/adtof_pytorch"  # inside the ADTOF-pytorch checkout
# sha256 of the four files as committed upstream (LF bytes, checked out with core.autocrlf=false on every
# machine; the wave-6 pins hashed one machine's CRLF working tree and failed elsewhere)
ADTOF_SOURCES = {
    "__init__.py": "9c0a061a9a3335def7b89a4a6000f7c0b141a789d06a96d8f3bf94a4e7d83194",
    "audio.py": "9aac619b430a3bd7551918bd1f187f53f0ad4e17cd615fbeb868cdf4c088cb0e",
    "model.py": "7537ddd224996fee8eeeade3c2965ba2d502465a724b27a38a41b97c85e62865",
    "post_processing.py": "b6b4a41fe5f13c61b35eb2dc4cad5e2fbc6c4ac47a9c9046cf9d0aced697db66",
}
# key in engine.json -> folder of the model table (scripts/engine_models.py: sources, bytes and sha256 per file)
WEIGHTS = {"adtof": "adtof-pytorch", "rmvpe": "rmvpe", "qwen3_aligner": "qwen3-forcedaligner-0.6b",
           "songformer": "songformer"}
ENGINE_IDS = {"drums": "adtof-frame-rnn", "melody": "rmvpe-onnx", "words": "qwen3-forcedaligner-0.6b",
              "sections": "songformer-hx8", "novelty": "foote-beats-librosa", "loudness": "rms-librosa",
              "events": "rms-librosa"}


def _machine_local() -> str:
    """LOCALAPPDATA, else ~/Library/Application Support on macOS, else ~/AppData/Local (dashboard/local_data
    engines_base, inline: this script stands alone; PR #2 by @ShawnCRMer)."""
    if os.environ.get("LOCALAPPDATA"):
        return os.environ["LOCALAPPDATA"]
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    return str(Path.home() / "AppData" / "Local")


def default_engine_dir() -> Path:
    base = _machine_local()
    return Path(base) / "hermes" / "film-suite" / "engines" / "song-analysis"


def default_models_dir() -> Path:
    """``engines/models``; an install from before this folder had a name keeps its ``engines/candidates``."""
    engines = Path(_machine_local()) / "hermes" / "film-suite" / "engines"
    return engines / "candidates" if (engines / "candidates").is_dir() else engines / "models"


def fetch_sources(edir: Path, source_root: Path | None = None, sources: dict | None = None) -> dict:
    """Sparse-checkout every SOURCES repository at its pin into ``edir/src/<name>``; {name: {url, commit, dir}}."""
    out = {}
    for name, pin in (SOURCES if sources is None else sources).items():
        remote = pinned_source.remote_for(name, pin["url"], source_root)
        dest = edir / "src" / name
        commit = pinned_source.fetch_pinned(name, remote, pin["commit"], dest, [f"{ADTOF_DIR}/{f}" for f in ADTOF_SOURCES])
        print(f"code ok: {name} @ {commit} from {remote}", flush=True)
        out[name] = {"url": pin["url"], "fetched_from": remote, "commit": commit, "dir": str(dest)}
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env=engine_env.engine_process.child_env())


def install_packages(uv: str, py: str, cpu_only: bool, runner=None) -> dict:
    """``uv pip install --require-hashes -r <lock> --exclude-newer <lock date>``; returns the env receipt."""
    lock = engine_env.lock_path(ENGINE, cpu_only)
    return engine_env.install(uv, py, lock, **({"runner": runner} if runner else {}))


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def check_weights(models_dir: Path, *, download: bool = True, opener=None, models: dict | None = None) -> dict:
    """Every model file the runner reads, in ``models_dir`` with its pinned sha256; a missing one is downloaded
    (``engine_models.ensure``) unless ``download`` is off.  ``models`` replaces the pinned table (tests)."""
    table = engine_models.MODELS if models is None else models
    found = engine_models.ensure(models_dir, tuple(WEIGHTS.values()), download=download, opener=opener,
                                 log=lambda line: print(line, flush=True), models=table)
    return {key: {"candidate": folder, "revision": table[folder]["revision"], "licence": table[folder]["licence"],
                  "dir": str(Path(models_dir) / folder), "files": found[folder]}
            for key, folder in WEIGHTS.items()}


def vendor_adtof(src: Path, edir: Path) -> Path:
    dst = edir / "vendor" / "adtof_pytorch"
    dst.mkdir(parents=True, exist_ok=True)
    for name, want in ADTOF_SOURCES.items():
        target = dst / name
        if target.is_file() and sha256(target) == want:
            continue
        got = sha256(src / name)
        if got != want:
            raise SystemExit(f"ADTOF source {name} sha256 {got} != {want}")
        shutil.copyfile(src / name, target)
    return dst.parent


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine-dir", type=Path, default=default_engine_dir())
    ap.add_argument("--models-dir", "--candidates-dir", dest="models_dir", type=Path, default=default_models_dir(),
                    help=f"where the model files live ({engine_models.gigabytes(engine_models.total_bytes('song'))}; "
                         "downloaded there when missing)")
    ap.add_argument("--no-download", action="store_true", help="fetch no model file: list the missing ones and stop")
    ap.add_argument("--source-root", type=Path, default=None,
                    help="offline: folder of local clones (one per SOURCES name); default is each pinned URL")
    ap.add_argument("--cpu-only", action="store_true", help="CPU torch wheels (no CUDA); macOS has one lock (MPS or CPU at run time) and ignores it")
    ap.add_argument("--smoke", help="run every task once on this audio file after installing (CPU)")
    args = ap.parse_args(argv)
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv not found on PATH")
    edir: Path = args.engine_dir
    edir.mkdir(parents=True, exist_ok=True)
    weights = check_weights(args.models_dir, download=not args.no_download)
    venv = edir / "venv"
    if not venv_python(venv).is_file():
        run([uv, "venv", "-p", PYTHON, str(venv)])
    py = str(venv_python(venv))
    env_receipt = install_packages(uv, py, args.cpu_only)
    sources = fetch_sources(edir, args.source_root)
    vendor = vendor_adtof(Path(sources["ADTOF-pytorch"]["dir"]) / ADTOF_DIR, edir)
    site = venv / "Lib" / "site-packages" if os.name == "nt" else next((venv / "lib").glob("python3*/site-packages"))
    (site / "film_suite_song_vendor.pth").write_text(str(vendor) + "\n", encoding="utf-8")
    shutil.copyfile(RUNNER_SRC, edir / "runner.py")
    probe = subprocess.run([py, "-c", "import importlib.metadata as m, sys, torch, adtof_pytorch, muq; "
                                      "print(torch.__version__, torch.cuda.is_available(), sys.version.split()[0], "
                                      "m.version('transformers'), m.version('librosa'), m.version('onnxruntime'), m.version('muq'), bool(getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available()))"],
                           capture_output=True, text=True, check=True,
                           env=engine_env.engine_process.engine_env({"HF_HUB_OFFLINE": "1"})).stdout.split()
    record = {
        "schema": SCHEMA,
        "python": py,
        "runner": str(edir / "runner.py"),
        "runner_sha256": sha256(edir / "runner.py"),
        "vendor": {"adtof_pytorch": {"dir": str(vendor / "adtof_pytorch"), "url": sources["ADTOF-pytorch"]["url"],
                                     "commit": sources["ADTOF-pytorch"]["commit"], "files": ADTOF_SOURCES}},
        "sources": {name: {"url": s["url"], "commit": s["commit"]} for name, s in sources.items()},
        "weights": weights,
        "engines": ENGINE_IDS,
        "versions": {"torch": probe[0], "python": probe[2], "transformers": probe[3], "librosa": probe[4],
                     "onnxruntime": probe[5], "muq": probe[6],
                     "adtof_pytorch": sources["ADTOF-pytorch"]["commit"][:7]},
        "cuda_available": probe[1] == "True",
        "mps_available": probe[7] == "True",  # Apple Silicon GPU (Metal)
        "pins": [f"{n}=={v}" for n, v in sorted(engine_env.lock_pins(engine_env.lock_path(ENGINE, args.cpu_only)).items())],
    }
    record["env"] = env_receipt  # lock sha256, python, uv, exclude-newer, sha256 of the post-install freeze
    (edir / "engine.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps({k: record[k] for k in ("schema", "python", "runner_sha256", "versions", "cuda_available")}, indent=2))
    if args.smoke:
        req = {"schema": "studio.song-runner-request/1", "audio_path": args.smoke, "device": "cpu",
               "tasks": ["drums", "loudness", "events"],
               "weights": {"adtof": weights["adtof"]["files"]["adtof_frame_rnn_pytorch_weights.pth"]["path"]}}
        out = subprocess.run([py, str(edir / "runner.py")], input=json.dumps(req), capture_output=True, text=True,
                             env=engine_env.engine_process.engine_env())
        if out.returncode != 0:
            print(out.stderr[-2000:], file=sys.stderr)
            return out.returncode
        result = json.loads(out.stdout.strip().splitlines()[-1])
        print("smoke:", {t: (r.get("ok"), r.get("wall_s")) for t, r in result["tasks"].items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
