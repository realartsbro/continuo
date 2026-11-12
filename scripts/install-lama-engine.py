"""Install the Film Suite LaMa inpaint engine (lane p13-img): the pre-fill of Image Remove.

    python scripts/install-lama-engine.py --reuse-venv VENV --model BIG_LAMA_PT [--engine-dir DIR]

Writes ``%LOCALAPPDATA%/hermes/film-suite/engines/lama-inpaint/`` (or ``--engine-dir``):
    runner.py      copy of backend/film-suite/dashboard/inpaint_core/lama_runner.py
    engine.json    record the backend reads (inpaint_core/lama_engine.locate), schema studio.inpaint-engine/1

Reuse mode (the only mode): NOTHING is downloaded or installed.  ``--reuse-venv`` names an EXISTING venv that
already has CUDA torch, numpy and Pillow (the scene-analysis engine's venv, torch 2.11.0+cu128); the installer
imports them once with that python to prove it and records the versions.  ``--model`` is the big-lama
TorchScript read IN PLACE (ComfyUI ``models/inpaint/big-lama.pt``, installed with the artist's consent); its
sha256 must be 344c77bb... (refused otherwise).  The model is loaded with ``torch.jit.load`` (spandrel breaks its
colours).  Idempotent.  Remove the directory to uninstall: Remove then refuses with ``inpaint_engine_missing``.
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

REPO = Path(__file__).resolve().parents[1]
RUNNER_SOURCE = (REPO / "backend" / "film-suite" / "dashboard" if (REPO / "backend" / "film-suite" / "dashboard").is_dir() else REPO / "dashboard") / "inpaint_core" / "lama_runner.py"
ENGINE_SCHEMA = "studio.inpaint-engine/1"
MODEL_SHA256 = "344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9"


def _load_engine_process():
    """The plugin's own child-process helper (``dashboard/engine_process``), loaded by path: ``backend/film-suite/``
    in the repository, the tree root in the catalog copy.  Every child of this script gets its minimal environment."""
    here = Path(__file__).resolve().parent
    for dashboard in (here.parent / "backend" / "film-suite" / "dashboard", here.parent / "dashboard"):
        path = dashboard / "engine_process" / "__init__.py"
        if path.is_file():
            spec = importlib.util.spec_from_file_location("film_suite_engine_process", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise SystemExit("dashboard/engine_process is missing next to this script; reinstall the plugin")


engine_process = _load_engine_process()


def default_engine_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "hermes" / "film-suite" / "engines" / "lama-inpaint"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def venv_python(venv: Path) -> Path:
    for candidate in (venv / "Scripts" / "python.exe", venv / "bin" / "python"):
        if candidate.is_file():
            return candidate
    raise SystemExit(f"no python in the venv {venv}")


def probe(py: Path) -> dict:
    code = "import json, torch, numpy, PIL; print(json.dumps({'torch': torch.__version__, 'numpy': numpy.__version__, 'pillow': PIL.__version__, 'cuda': torch.cuda.is_available()}))"
    out = subprocess.run([str(py), "-c", code], capture_output=True, text=True, timeout=300, env=engine_process.engine_env())
    if out.returncode != 0:
        raise SystemExit(f"the venv python cannot import torch/numpy/PIL: {out.stderr.strip()[-300:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def install(engine_dir: Path, venv: Path, model: Path, *, check_venv: bool = True) -> dict:
    if not model.is_file():
        raise SystemExit(f"model not found: {model}")
    model_sha = sha256(model)
    if model_sha != MODEL_SHA256:
        raise SystemExit(f"model sha256 {model_sha} is not big-lama.pt ({MODEL_SHA256})")
    py = venv_python(venv)
    versions = probe(py) if check_venv else {}
    engine_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(RUNNER_SOURCE, engine_dir / "runner.py")
    record = {
        "schema": ENGINE_SCHEMA,
        "python": str(py),
        "runner": str(engine_dir / "runner.py"),
        "runner_sha256": sha256(engine_dir / "runner.py"),
        "model": str(model),
        "model_sha256": model_sha,
        "mode": "reuse_venv",
        "venv": str(venv),
        "versions": versions,
        "loader": "torch.jit.load",
    }
    temp = engine_dir / "engine.json.tmp"
    temp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    os.replace(temp, engine_dir / "engine.json")
    return record


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine-dir", type=Path, default=default_engine_dir())
    ap.add_argument("--reuse-venv", type=Path, required=True, help="an existing venv with CUDA torch, numpy, Pillow")
    ap.add_argument("--model", type=Path, required=True, help="big-lama.pt (TorchScript), read in place")
    args = ap.parse_args(argv)
    record = install(args.engine_dir, args.reuse_venv, args.model)
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
