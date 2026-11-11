from __future__ import annotations


# --- Model sources (lane w5-backend item 5; artist decision 23.10.: weights never in the repo) ---------
#
# workflows/model-sources.json names, per workflow, every model file it loads and, per file, where it
# comes from: url (Hugging Face resolve/<commit sha>/..., pinned, never main), bytes, sha256 (of the local
# file, matched against the remote LFS sha256 before the url was accepted), license of the WEIGHTS
# (code_license when it differs), gated, and per workflow the custom node packages {repo, commit}.
# Unknown values stay null with a reason. GET /models/missing (agent tool continuo_models_missing)
# answers, per workflow, which of its files are absent on THIS machine or carry another hash: read-only.
# The models folder is FILM_SUITE_COMFY_MODELS, else <ComfyUI root>/models with the root from the Suite's one helper
# (generation/comfy_root.py, with its default; the model catalog reads the same),
# plus every folder its ComfyUI root's extra_model_paths.yaml lists (lane w6-link item 5). A file's sha256 is hashed once
# and kept by (path, size, mtime) in a private cache, so a repeated call does not re-read the weights.

# This unit executes in plugin_api's namespace (routes/__init__.py load_area), so __file__ is
# dashboard/plugin_api.py: the repository root is parents[3], the installed dashboard parents[0].
_HERE_MS = Path(__file__).resolve()
_MODEL_SOURCES_PATH = next((p / "workflows" / "model-sources.json" for p in (_HERE_MS.parents[3], _HERE_MS.parent)
                            if (p / "workflows" / "model-sources.json").is_file()), _HERE_MS.parents[3] / "workflows" / "model-sources.json")
_MODEL_HASH_LOCK = _thread.allocate_lock()


def _model_sources() -> dict[str, Any]:
    return json.loads(_MODEL_SOURCES_PATH.read_text(encoding="utf-8"))


def _models_root() -> Path:
    configured = os.environ.get("FILM_SUITE_COMFY_MODELS")
    if configured:
        return Path(configured)
    return _model_catalog_module.comfy_root() / "models"


# --- extra_model_paths.yaml (lane w6-link item 5) ---------------------------------------------------
# ComfyUI loads model files from its models/ folder AND from every section of the root's
# extra_model_paths.yaml: base_path plus, per folder kind, one path or a multi-line "|" list. The
# missing check reads the same folders in ComfyUI's order: main models/<kind> (with ComfyUI's default
# alias folders: unet for diffusion_models, clip for text_encoders) first, then each section in file
# order. The file is parsed by a small reader for exactly this shape (sections, key: value, key: | block)
# so the route needs no YAML package; anything else in it is ignored, a broken file adds no folders.
_MODEL_FOLDER_ALIASES = {"diffusion_models": ("diffusion_models", "unet"), "text_encoders": ("text_encoders", "clip")}
_EXTRA_PATHS_MAX_BYTES = 64 * 1024


def _extra_model_paths(comfy_root: Path) -> list[tuple[Path, dict[str, list[str]]]]:
    config = comfy_root / "extra_model_paths.yaml"
    try:
        if not config.is_file() or config.stat().st_size > _EXTRA_PATHS_MAX_BYTES:
            return []
        lines = config.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    sections: list[dict[str, list[str]]] = []
    current: dict[str, list[str]] | None = None
    block: list[str] | None = None
    block_indent = 0
    for raw in lines:
        text = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not text.strip():
            continue
        indent = len(text) - len(text.lstrip(" \t"))
        if block is not None and indent > block_indent:
            block.append(text.strip())
            continue
        block = None
        if indent == 0:
            if not text.endswith(":"):
                current = None
                continue
            current = {}
            sections.append(current)
            continue
        if current is None or ":" not in text:
            continue
        key, _, value = text.strip().partition(":")
        key, value = key.strip(), value.strip().strip("'\"")
        if value in ("|", "|-", "|+", ">", ">-"):
            block, block_indent = [], indent
            current[key] = block
        elif value:
            current[key] = [value]
    out = []
    for section in sections[:16]:
        base_value = (section.get("base_path") or [""])[0]
        if not base_value:
            continue
        base = Path(os.path.expandvars(os.path.expanduser(base_value)))
        if not base.is_absolute():
            base = comfy_root / base
        out.append((base, section))
    return out


def _model_folder_roots(models_root: Path, folder: str) -> list[Path]:
    """Where ComfyUI looks for a file of this folder kind: main models/ first, then extra_model_paths."""
    roots = [models_root / sub for sub in _MODEL_FOLDER_ALIASES.get(folder, (folder,))]
    for base, section in _extra_model_paths(models_root.parent):
        for line in (section.get(folder) or [])[:16]:
            candidate = base / line
            if candidate not in roots:
                roots.append(candidate)
    return roots


def _model_file_path(models_root: Path, folder: str, name: str) -> Path:
    """The first folder that has the file (ComfyUI's order), else the main folder's path."""
    roots = _model_folder_roots(models_root, folder)
    for candidate in roots:
        if (candidate / name).is_file():
            return candidate / name
    return roots[0] / name


def _model_hash_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(state_root(Path(base) / "hermes")) / "model-hashes.json"


def _model_file_sha256(path: Path) -> str:
    stat_ = path.stat()
    key = f"{path}|{stat_.st_size}|{stat_.st_mtime_ns}"
    with _MODEL_HASH_LOCK:
        cache_path = _model_hash_cache_path()
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - no cache yet
            cache = {}
        if isinstance(cache.get(key), str):
            return cache[key]
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(16 << 20), b""):
            digest.update(block)
    value = digest.hexdigest()
    with _MODEL_HASH_LOCK:
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cache = {}
        cache[key] = value
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        os.replace(tmp, cache_path)
    return value


# --- Never block a read on hashing (lane p5b-bck, BCK-027/034) ----------------------------------------
# Measured on a fresh bench: a cold read hashed every present weight file inside the request (tens of GB,
# 41.8 s for P3, > 240 s once for the combined capability read). Now a read answers from the hash cache;
# a file not hashed yet for its (path, size, mtime) is answered "unverified" and hashed ONCE in the
# background on a private one-worker pool (never the default executor every other to_thread route uses);
# a burst of reads schedules it once. A small file (<= _MODEL_HASH_INLINE_BYTES) is hashed inline: ms.
_MODEL_HASH_INLINE_BYTES = 64 << 20
_MODEL_HASH_PENDING: set[str] = set()
_MODEL_HASH_POOL: list[Any] = []


def _model_hash_pool() -> Any:
    with _MODEL_HASH_LOCK:
        if not _MODEL_HASH_POOL:
            import concurrent.futures as _ms_futures
            _MODEL_HASH_POOL.append(_ms_futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="film-suite-model-hash"))
        return _MODEL_HASH_POOL[0]


def _model_hash_background(path: Path, key: str) -> None:
    try:
        _model_file_sha256(path)
    except Exception:  # noqa: BLE001 - the next read schedules it again
        pass
    finally:
        with _MODEL_HASH_LOCK:
            _MODEL_HASH_PENDING.discard(key)


def _model_file_sha256_ready(path: Path, cache: dict[str, Any]) -> str | None:
    """The file's sha256 if known now (cache, or a small file hashed inline); else None and a background hash."""
    stat_ = path.stat()
    key = f"{path}|{stat_.st_size}|{stat_.st_mtime_ns}"
    if isinstance(cache.get(key), str):
        return cache[key]
    if stat_.st_size <= _MODEL_HASH_INLINE_BYTES:
        return _model_file_sha256(path)
    with _MODEL_HASH_LOCK:
        if key in _MODEL_HASH_PENDING:
            return None
        _MODEL_HASH_PENDING.add(key)
    _model_hash_pool().submit(_model_hash_background, path, key)
    return None


def _models_missing(workflow: str | None) -> dict[str, Any]:
    sources = _model_sources()
    models, workflows = sources["models"], sources["workflows"]
    if workflow is not None and workflow not in workflows:
        return {"error": {"code": "unknown_workflow", "message": "That workflow is not in the catalog"}}
    root = _models_root()
    with _MODEL_HASH_LOCK:
        try:
            hash_cache = json.loads(_model_hash_cache_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - no cache yet
            hash_cache = {}
    if not isinstance(hash_cache, dict):
        hash_cache = {}
    answer: dict[str, Any] = {}
    for workflow_id in ([workflow] if workflow else sorted(workflows)):
        missing = []
        for key in workflows[workflow_id]["models"]:
            entry = models[key]
            path = _model_file_path(root, entry["folder"], entry["name"])
            if not path.is_file():
                state = "absent"
            elif entry.get("bytes") is not None and path.stat().st_size != entry["bytes"]:
                state = "wrong_hash"
            elif entry.get("sha256") and (digest := _model_file_sha256_ready(path, hash_cache)) is None:
                state = "unverified"
            elif entry.get("sha256") and digest != entry["sha256"]:
                state = "wrong_hash"
            else:
                continue
            missing.append({
                "name": entry["name"], "folder": entry["folder"], "state": state, "url": entry.get("url"),
                "gb": round(entry["bytes"] / 1e9, 2) if entry.get("bytes") else None, "sha256": entry.get("sha256"),
                "license": entry.get("license"), "gated": entry.get("gated"), "reason": entry.get("reason"),
            })
        answer[workflow_id] = {"missing": missing, "node_packages": workflows[workflow_id].get("node_packages", [])}
    return {"schema": "studio.models-missing/1", "workflows": answer}


@router.get("/models/missing")
async def models_missing(request: Request) -> Any:
    """Per workflow, which of its model files are absent here or carry another hash, with url, GB, licence and gated."""
    workflow = request.query_params.get("workflow") or None
    if workflow is not None and not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{0,79}", workflow):
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "message": "workflow is an id like minimax-h3-frames-v1"}})
    try:
        out = await asyncio.to_thread(_models_missing, workflow)
    except Exception:  # noqa: BLE001 - finite error, never a path
        return JSONResponse(status_code=503, content={"error": {"code": "model_sources_unavailable", "message": "The model source list is unavailable"}})
    if "error" in out:
        return JSONResponse(status_code=404, content=out)
    # w39-p2-tester item 2: files are read from this machine's folders; whether an engine answers (and where) is the
    # other half of the truth, so the agent never reads "absent" as the only reason.
    engine = await asyncio.to_thread(_comfy_endpoint_module.engine_state)
    return {**out, "engine": engine}
