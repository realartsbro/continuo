"""Install the Film Suite scene-analysis engine (lane w6-scene) into its OWN uv venv.

    python scripts/install-scene-engine.py [--engine-dir DIR] [--cpu-only] [--source-root DIR] [--smoke VIDEO]

Creates ``%LOCALAPPDATA%/hermes/film-suite/engines/scene-analysis/``:
    venv/          uv venv, the lock's python, hash-locked stack (scripts/engine-locks) (never the Hermes venv, never ComfyUI's python)
    runner.py      copy of backend/film-suite/dashboard/scene_core/scene_runner.py
    vendor/        sync_eval.py (the backend's evaluation layer, same bytes), the MIT model code of
                   OmniShotCut (40ecf12) and SyncNet (joonson/syncnet_python 907c0b5, SyncNetModel.py only)
    src/           the installer's own sparse checkouts of OmniShotCut and syncnet_python at the pins (SOURCES)
    engine.json    record the backend reads (scene_core/scene_engine.locate), schema studio.scene-engine/1,
                   with sources (URL + commit)

The model code is fetched by this installer at a pinned full commit (table SOURCES) from its upstream
URL, or from ``--source-root DIR`` (one local clone per repository name) for offline use.  A wrong sha
or a commit the remote no longer has is refused.

Weights are NEVER downloaded and never copied: they are read in place from
``film-suite/engines/candidates`` (each sha256 checked against candidates.json) and from ComfyUI's
MediaPipe face landmarker (models/detection, sha256 recorded). Idempotent: re-running re-installs from the lock (--require-hashes)
and the copies. Remove the directory to uninstall; the Take tab then says "Not installed".
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
CORE = (ROOT / "backend" / "film-suite" / "dashboard" if (ROOT / "backend" / "film-suite" / "dashboard").is_dir() else ROOT / "dashboard") / "scene_core"
# Packages: ONLY the hash lock scripts/engine-locks/scene-analysis.txt (CUDA) or .cpu.txt (--cpu-only), compiled from
# the live venv the measurements ran on (scripts/engine_env.py lock).  Change it only with a new measurement.
ENGINE = "scene-analysis"
PYTHON = engine_env.read_lock(engine_env.lock_path(ENGINE))[0]["python"]  # the exact interpreter of the lock
# Third-party code, fetched by the installer itself at ONE full commit (scripts/pinned_source.py).
# | repository     | URL                                                    | commit                                   |
# | OmniShotCut    | https://github.com/UVA-Computer-Vision-Lab/OmniShotCut | 40ecf12a0f6ace617680cf22b74da0bef4af3c21 |
# | syncnet_python | https://github.com/joonson/syncnet_python              | 907c0b579c2e2d83f0eae1b2ac9e720cde4e5623 |
SOURCES = {
    "OmniShotCut": {"url": "https://github.com/UVA-Computer-Vision-Lab/OmniShotCut",
                    "commit": "40ecf12a0f6ace617680cf22b74da0bef4af3c21",
                    "paths": ["omnishotcut/", "LICENSE"]},
    "syncnet_python": {"url": "https://github.com/joonson/syncnet_python",
                       "commit": "907c0b579c2e2d83f0eae1b2ac9e720cde4e5623",
                       "paths": ["SyncNetModel.py", "LICENSE.md"]},
}
OMNISHOTCUT_COMMIT = SOURCES["OmniShotCut"]["commit"]
SYNCNET_COMMIT = SOURCES["syncnet_python"]["commit"]
# key in the request -> (candidates.json entry, file inside it)
CANDIDATE_WEIGHTS = {
    "syncnet": ("syncnet-v2", "auxiliary/syncnet_v2.model"),
    "omnishotcut": ("omnishotcut", "OmniShotCut_ckpt.pth"),
    "nemotron_model": ("nemotron-3-diarization", "model.safetensors"),
}
_root_spec = importlib.util.spec_from_file_location("film_suite_comfy_root", CORE.parent / "generation" / "comfy_root.py")
_root_module = importlib.util.module_from_spec(_root_spec)
_root_spec.loader.exec_module(_root_module)
COMFY_ROOT = _root_module.comfy_root()  # FILM_SUITE_COMFY_ROOT: the Suite's one ComfyUI root
MEDIAPIPE = Path("models/detection/mediapipe_face_fp32.safetensors")


def hermes_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "hermes"


def default_engine_dir() -> Path:
    return hermes_dir() / "film-suite" / "engines" / "scene-analysis"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def install_packages(uv: str, py: str, cpu_only: bool, runner=None) -> dict:
    """``uv pip install --require-hashes -r <lock> --exclude-newer <lock date>``; returns the env receipt."""
    lock = engine_env.lock_path(ENGINE, cpu_only)
    return engine_env.install(uv, py, lock, **({"runner": runner} if runner else {}))


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def check_weights(cand_dir: Path) -> dict[str, dict[str, str]]:
    table = json.loads((cand_dir / "candidates.json").read_text(encoding="utf-8"))
    out = {}
    for key, (entry, name) in CANDIDATE_WEIGHTS.items():
        want = table[entry]["files"][name]["sha256"]
        path = cand_dir / entry / name
        if not path.is_file():
            raise SystemExit(f"weights missing: {path} (never downloaded by this installer)")
        got = sha256(path)
        if got != want:
            raise SystemExit(f"weights sha256 mismatch for {key}: {got} != candidates.json {want}")
        out[key] = {"path": str(path), "sha256": got}
        print(f"weights ok: {key} {got[:12]}")
    out["nemotron"] = {"path": str(cand_dir / "nemotron-3-diarization"), "sha256": out["nemotron_model"]["sha256"]}
    mp = COMFY_ROOT / MEDIAPIPE
    if not mp.is_file():
        raise SystemExit(f"MediaPipe face landmarker missing: {mp}")
    out["mediapipe"] = {"path": str(mp), "sha256": sha256(mp)}
    return out


def fetch_sources(edir: Path, source_root: Path | None = None, sources: dict | None = None) -> dict:
    """Sparse-checkout every SOURCES repository at its pin into ``edir/src/<name>``; {name: {url, commit, dir}}."""
    out = {}
    for name, pin in (SOURCES if sources is None else sources).items():
        remote = pinned_source.remote_for(name, pin["url"], source_root)
        dest = edir / "src" / name
        commit = pinned_source.fetch_pinned(name, remote, pin["commit"], dest, pin["paths"])
        print(f"code ok: {name} @ {commit} from {remote}", flush=True)
        out[name] = {"url": pin["url"], "fetched_from": remote, "commit": commit, "dir": str(dest)}
    return out


def copy_vendor(edir: Path, source: Path) -> None:
    vendor = edir / "vendor"
    osc_src = source / "OmniShotCut" / "omnishotcut"
    sn_src = source / "syncnet_python" / "SyncNetModel.py"
    if not osc_src.is_dir() or not sn_src.is_file():
        raise SystemExit(f"model code not found under {source} (OmniShotCut/omnishotcut, syncnet_python/SyncNetModel.py)")
    if (vendor / "omnishotcut").is_dir():
        shutil.rmtree(vendor / "omnishotcut")
    vendor.mkdir(parents=True, exist_ok=True)
    shutil.copytree(osc_src, vendor / "omnishotcut", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(sn_src, vendor / "SyncNetModel.py")
    # Two source edits, recorded in engine.json: the device comes from FILM_SUITE_SCENE_TORCH_DEVICE (upstream
    # hard-codes "cuda", so CPU jobs failed) and the backbone never asks torchvision for ImageNet weights.
    eng = vendor / "omnishotcut" / "engine.py"
    text = eng.read_text(encoding="utf-8")
    for old in ('model.to("cuda")', '.to("cuda")'):
        text = text.replace(old, old.replace('"cuda"', 'os.environ.get("FILM_SUITE_SCENE_TORCH_DEVICE", "cuda")'))
    eng.write_text(text, encoding="utf-8")
    bb = vendor / "omnishotcut" / "architecture" / "backbone.py"
    text = bb.read_text(encoding="utf-8")
    if "pretrained = is_main_process()" not in text:
        raise SystemExit("OmniShotCut backbone changed: the pretrained=False patch does not apply")
    bb.write_text(text.replace("pretrained = is_main_process()", "pretrained = False"), encoding="utf-8")
    shutil.copyfile(source / "OmniShotCut" / "LICENSE", vendor / "LICENSE-OmniShotCut")
    shutil.copyfile(source / "syncnet_python" / "LICENSE.md", vendor / "LICENSE-syncnet")
    shutil.copyfile(CORE / "sync_eval.py", vendor / "sync_eval.py")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine-dir", type=Path, default=default_engine_dir())
    ap.add_argument("--cpu-only", action="store_true", help="CPU torch wheels (no CUDA)")
    ap.add_argument("--candidates", type=Path, default=hermes_dir() / "film-suite" / "engines" / "candidates")
    ap.add_argument("--source-root", type=Path, default=None,
                    help="offline: folder of local clones (one per SOURCES name); default is each pinned URL")
    ap.add_argument("--smoke", help="analyse this video once after installing")
    args = ap.parse_args(argv)
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("uv not found on PATH")
    weights = check_weights(args.candidates)
    edir: Path = args.engine_dir
    edir.mkdir(parents=True, exist_ok=True)
    venv = edir / "venv"
    if not venv_python(venv).is_file():
        run([uv, "venv", "-p", PYTHON, str(venv)])
    py = str(venv_python(venv))
    env_receipt = install_packages(uv, py, args.cpu_only)
    sources = fetch_sources(edir, args.source_root)
    copy_vendor(edir, edir / "src")
    shutil.copyfile(CORE / "scene_runner.py", edir / "runner.py")
    probe = subprocess.run([py, "-c", "import torch, torchvision, transformers, sys; print(torch.__version__, torch.cuda.is_available(), sys.version.split()[0], transformers.__version__)"],
                           capture_output=True, text=True, check=True).stdout.split()
    record = {
        "schema": "studio.scene-engine/1",
        "python": py,
        "runner": str(edir / "runner.py"),
        "runner_sha256": sha256(edir / "runner.py"),
        "sync_eval_sha256": sha256(edir / "vendor" / "sync_eval.py"),
        "comfy_root": str(COMFY_ROOT),
        "weights": {k: v["path"] for k, v in weights.items()},
        "weights_sha256": {k: v["sha256"] for k, v in weights.items() if k != "nemotron"},
        "sources": {name: {"url": s["url"], "commit": s["commit"]} for name, s in sources.items()},
        "code": {"omnishotcut": sources["OmniShotCut"]["commit"], "syncnet_python": sources["syncnet_python"]["commit"],
                 "patches": ["omnishotcut/architecture/backbone.py: pretrained=False", "omnishotcut/engine.py: device from FILM_SUITE_SCENE_TORCH_DEVICE"]},
        "versions": {"torch": probe[0], "python": probe[2], "transformers": probe[3]},
        "cuda_available": probe[1] == "True",
        "pins": [f"{n}=={v}" for n, v in sorted(engine_env.lock_pins(engine_env.lock_path(ENGINE, args.cpu_only)).items())],
        "parts": ["cuts", "speakers", "sync", "motion"],
    }
    record["env"] = env_receipt  # lock sha256, python, uv, exclude-newer, sha256 of the post-install freeze
    (edir / "engine.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(json.dumps(record, indent=2))
    if args.smoke:
        req = {"schema": "studio.scene-runner-request/1", "video_path": args.smoke, "device": "cpu", "parts": ["motion", "cuts"],
               "weights": record["weights"], "weights_sha256": record["weights_sha256"], "comfy_root": record["comfy_root"]}
        out = subprocess.run([py, str(edir / "runner.py")], input=json.dumps(req), capture_output=True, text=True)
        if out.returncode != 0:
            print(out.stderr[-2000:], file=sys.stderr)
            return out.returncode
        result = json.loads(out.stdout.strip().splitlines()[-1])
        print("smoke:", json.dumps({k: (len(v.get("events", v.get("energy", []))) if isinstance(v, dict) else v) for k, v in result["parts"].items()}), result["errors"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
