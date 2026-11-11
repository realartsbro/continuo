from __future__ import annotations


# --- Audio page, Create: local music generation (lane w16-mus) -----------------------------------
#
# Model-agnostic over the pinned music targets of music_gen/targets.py (Stable Audio 3 Small Music,
# YuE2 notes -> song, Foundation-1 loop / keybed; one manifest each, -6 dB headroom before the output,
# PreviewAudio = ComfyUI temp only).  The pattern is the voice routes (voice/voice_1.py): generate
# answers at once with take ids, takes run one after another on one worker thread, facts are measured
# on the stored file.  GPU rule: each submit waits until GET /queue is 0/0 (the artist's jobs run
# first; nothing is ever cancelled, interrupted or reordered).  A finished take is one row of the Shoot
# ledger published as a FLAC in the project's output folder (Music/<target>/...) with its recipe
# sidecar, so Media lists it as a sound.  Every route is an agent tool (agent_tools.ROUTE_TOOLS).

import threading  # noqa: E402 - the composition namespace has no threading import


def _music_gen_load(name: str) -> Any:
    module_name = f"film_suite_music_gen_{name}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    # Executed in plugin_api's namespace: __file__ is dashboard/plugin_api.py, the modules are dashboard/music_gen/*.py.
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name("music_gen") / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("The music module is unavailable.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_music_gen_targets = _music_gen_load("targets")
_music_gen_runner = _music_gen_load("runner")
_MUSIC_GEN_TAKES_MAX = 4
_MUSIC_GEN_CONNECTION = "local-music"
_MUSIC_GEN_QUEUE: list[dict[str, Any]] = []
_MUSIC_GEN_QUEUE_LOCK = threading.Lock()
_MUSIC_GEN_STATE_LOCK = threading.Lock()
_MUSIC_GEN_THREADS: list[Any] = []
_MUSIC_GEN_PROGRESS: dict[str, float] = {}
_MUSIC_GEN_TAKE_ID = r"^mtake_[0-9a-f]{12}$"


def _music_gen_sleep(seconds: float) -> None:
    time.sleep(seconds)


class MusicGenProjectRequest(_ProjectRequest):
    project_ref: ProjectReference


class MusicGenTargetsRequest(_ProjectRequest):
    check: bool = True


class MusicGenGenerateRequest(MusicGenProjectRequest):
    target_id: str = Field(min_length=1, max_length=64)
    values: dict[str, Any] = Field(default_factory=dict)
    count: int = Field(default=1, ge=1, le=_MUSIC_GEN_TAKES_MAX)
    seeds: list[int] | None = Field(default=None, min_length=1, max_length=_MUSIC_GEN_TAKES_MAX)
    notes_from: str | None = Field(default=None, pattern=_MUSIC_GEN_TAKE_ID)
    # Lane w21-rel (AUD-145; reference: /generation submit client_request_id, /projects/timeline/apply request_id):
    # a caller-stable id; the same id with the same request answers the batch it made, never a second one.
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")


class MusicGenTakesRequest(MusicGenProjectRequest):
    take_ids: list[str] | None = Field(default=None, min_length=1, max_length=64)


class _MusicGenError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _music_gen_error(exc: BaseException) -> JSONResponse:
    if isinstance(exc, (_music_gen_targets.MusicTargetError, _music_gen_runner.MusicRunError, _MusicGenError)):
        return JSONResponse(status_code=422, content={"error": {"code": exc.code, "message": str(exc)}})
    if isinstance(exc, studio_core.ProjectError):
        return JSONResponse(status_code=404 if exc.code == "project_not_found" else 503,
                            content={"error": {"code": exc.code, "message": "The project could not be read"}})
    return JSONResponse(status_code=503, content={"error": {"code": "music_unavailable", "message": "Music is unavailable"}})


def _music_gen_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(state_root(Path(base) / "hermes")) / "music-gen"


def _music_gen_read(project_id: str) -> dict[str, Any]:
    path = _music_gen_state_dir() / f"{project_id}.json"
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schema": "studio.music-takes/1", "takes": {}}
    if not isinstance(body, dict) or body.get("schema") != "studio.music-takes/1" or not isinstance(body.get("takes"), dict):
        raise _MusicGenError("music_state_unreadable", "The music takes could not be read")
    return body


def _music_gen_update(project_id: str, change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    with _MUSIC_GEN_STATE_LOCK:
        body = _music_gen_read(project_id)
        change(body)
        folder = _music_gen_state_dir()
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f".{project_id}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, folder / f"{project_id}.json")
        return body


def _music_gen_seconds_by_job(project_id: str | None) -> dict[str, float]:
    """studio job id -> measured seconds of each finished music take (lane w17-acre, MED-056; the Media feed's length).
    A take's request names its controls, never its length as made; the runner measured the stored file into the state."""
    try:
        takes = _music_gen_read(project_id)["takes"] if project_id else {}
    except Exception:  # noqa: BLE001 - no readable music state: no music lengths
        return {}
    out: dict[str, float] = {}
    for take in takes.values():
        seconds, job = ((take or {}).get("facts") or {}).get("seconds"), (take or {}).get("studio_job_id")
        if isinstance(job, str) and type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0:
            out[job] = round(float(seconds), 3)
    return out


def _music_gen_project(reference: ProjectReference) -> tuple[str, str, list[str], str]:
    directory = _project_reference_directory(reference)
    manifest = _project_store_factory().read_project(directory, expected_project_id=reference.project_id)
    return directory, reference.project_id, list(manifest["approved_roots"]), str(manifest.get("name") or "Project")


async def _music_gen_engine_facts(manifests: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Live /object_info for the required classes and /models/<folder> lists (None = engine unreachable)."""
    client = _comfy_client_module.ComfyUIClient()
    try:
        await client.system_stats()
    except Exception:  # noqa: BLE001 - unreachable engine
        return None, {}
    names = sorted({c for m in manifests.values() for entry in m["graphs"].values() for c in entry["required_classes"]})
    info: dict[str, Any] = {}
    for name in names:
        try:
            answer = await client.object_info(name)
            if isinstance(answer, dict) and name in answer:
                info[name] = answer[name]
        except Exception:  # noqa: BLE001 - a missing class is a reason, not a failure
            pass
    models: dict[str, Any] = {}
    for folder in sorted({model["folder"] for m in manifests.values() for model in m["models"]}):
        try:
            models[folder] = await client.models(folder)
        except Exception:  # noqa: BLE001
            models[folder] = None
    return info, models


@router.post("/music/gen/targets")
async def music_gen_targets(request: MusicGenTargetsRequest):
    """The local music targets (engine, stages, the controls each really takes) and their readiness."""
    try:
        manifests = {tid: _music_gen_targets.load_target(tid)["manifest"] for tid in _music_gen_targets.TARGET_IDS}
        listed = [_music_gen_targets.public_target(m) for m in manifests.values()]
        if request.check:
            info, models = await _music_gen_engine_facts(manifests)
            for target in listed:
                target["health"] = _music_gen_targets.health(manifests[target["id"]], info, models)
        else:
            for target in listed:
                target["health"] = {"ready": None, "reasons": ["Not checked"]}
        return {"schema": "studio.music-targets/1", "targets": listed}
    except Exception as exc:  # noqa: BLE001
        return _music_gen_error(exc)


def _music_gen_progress(prompt_id: str) -> float | None:
    try:
        live = _image_preview_hub().live(prompt_id)
    except Exception:  # noqa: BLE001
        return None
    step, total = live.get("step"), live.get("total")
    return round(step / total, 3) if type(step) is int and type(total) is int and total > 0 else None


def _music_gen_comfy() -> Any:
    """The configured local engine (comfy_endpoint); the preview reader's client id, so its % reaches the take."""
    return _music_gen_runner.ComfyHTTP(client_id=_comfyui_preview_module.PREVIEW_CLIENT_ID, progress=_music_gen_progress)


def _music_gen_loudness(path: Path) -> dict[str, Any]:
    measured = _timeline_module("footage").measure_loudness(path, ffmpeg=FFMPEG_PATH)
    return {k: measured.get(k) for k in ("integrated_lufs", "true_peak_dbtp", "lra_lu")}


def _music_gen_prompt_text(manifest: Mapping[str, Any], values: Mapping[str, Any]) -> str:
    text = values.get("prompt") or values.get("instrument") or manifest["label"]
    return str(text)[:2000] or manifest["label"]


_MUSIC_GEN_REQUESTS_KEEP = 64


def _music_gen_request_digest(request: MusicGenGenerateRequest) -> str:
    """The request without its id (lane w21-rel, AUD-145): what a retry must repeat exactly."""
    body = request.model_dump(mode="json", exclude={"request_id"})
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _music_gen_replay(body: dict[str, Any], request_id: str, digest: str) -> dict[str, Any] | None:
    """The kept answer of ``request_id`` (replayed), None when unknown; another request under it refuses."""
    kept = (body.get("requests") or {}).get(request_id) if isinstance(body.get("requests"), dict) else None
    if not isinstance(kept, dict):
        return None
    if kept.get("digest") != digest:
        raise _MusicGenError("request_id_reused", "This request_id already made another batch")
    return {**kept["answer"], "replayed": True}


def _music_gen_generate(request: MusicGenGenerateRequest) -> dict[str, Any]:
    """Everything is checked before a take is planned: one clear refusal, nothing queued."""
    directory, project_id, roots, project_name = _music_gen_project(request.project_ref)
    digest = _music_gen_request_digest(request) if request.request_id is not None else None
    if digest is not None:
        replay = _music_gen_replay(_music_gen_read(project_id), request.request_id, digest)
        if replay is not None:
            return replay
    if request.target_id not in _music_gen_targets.TARGET_IDS:
        raise _music_gen_targets.MusicTargetError("unknown_target", "That music model is not in the catalog")
    target = _music_gen_targets.load_target(request.target_id)
    manifest = target["manifest"]
    values = dict(request.values)
    if request.notes_from is not None:
        if manifest["engine"] != "yue2":
            raise _MusicGenError("notes_not_used", f"{manifest['engine_label']} does not read notes from a take")
        source = _music_gen_read(project_id)["takes"].get(request.notes_from)
        if not source or not (source.get("notes") or "").strip():
            raise _MusicGenError("notes_missing", "That take has no notes")
        values["notes"] = source["notes"]
    if "seed" in values and request.seeds:
        raise _MusicGenError("seed_twice", "The request names both seed and seeds")
    stages = _music_gen_targets.stage_plan(target, values)
    seed_default = next(c["default"] for c in manifest["controls"] if c["id"] == "seed")
    base = values.get("seed", seed_default)
    seeds = list(request.seeds) if request.seeds else [base + n if type(base) is int else base for n in range(request.count)]
    if request.seeds and len(seeds) != request.count and request.count != 1:
        raise _MusicGenError("seed_count", "One seed per take")
    # Compile every stage with the first seed: a bad value refuses the whole batch before anything waits.
    for stage in stages:
        check = {**values, "seed": seeds[0]}
        if stage == "song" and "notes" in stages[:stages.index(stage)] and not str(check.get("notes") or "").strip():
            check["notes"] = "X:1"
        _music_gen_targets.compile_graph(target, stage, check)
    output = _project_output_dir(directory, project_id, create=True)
    by = "hermes" if _film_caller() == "hermes" else "artist"
    now = time.time()
    planned = []
    rows: dict[str, Any] = {}
    for seed in seeds:
        take_id = f"mtake_{uuid.uuid4().hex[:12]}"
        take_values = {**values, "seed": seed}
        rows[take_id] = {"target_id": manifest["id"], "values": take_values, "stages": stages, "seed": seed, "by": by,
                         "state": "waiting", "stage": None, "notes": values.get("notes") or None, "facts": {}, "recipe": None,
                         "error": None, "code": None, "studio_job_id": None, "created_at": now,
                         "expected_seconds": _music_gen_targets.expected_seconds(target, stages[-1], take_values)
                         if manifest["engine"] != "yue2" else None}
        planned.append({"take_id": take_id, "seed": seed, "state": "waiting"})

    answer = {"target_id": manifest["id"], "stages": stages, "takes": planned, "by": by}
    raced: dict[str, Any] = {}

    def add(body: dict[str, Any]) -> None:
        # Lane w21-rel (AUD-145): the receipt is written with the takes, before anything is queued; a retry that
        # raced its first try finds it here under the state lock and queues nothing.
        if digest is not None:
            replay = _music_gen_replay(body, request.request_id, digest)
            if replay is not None:
                raced["answer"] = replay
                return
            requests = body.get("requests") if isinstance(body.get("requests"), dict) else {}
            requests = {**requests, request.request_id: {"digest": digest, "answer": answer, "at": now}}
            body["requests"] = dict(sorted(requests.items(), key=lambda kv: kv[1].get("at", 0))[-_MUSIC_GEN_REQUESTS_KEEP:])
        body["takes"].update(rows)
    _music_gen_update(project_id, add)
    if raced:
        return raced["answer"]
    with _MUSIC_GEN_QUEUE_LOCK:
        for take in planned:
            _MUSIC_GEN_QUEUE.append({"take_id": take["take_id"], "project_id": project_id, "directory": directory, "output": str(output),
                                     "project_name": project_name, "target": target, "values": rows[take["take_id"]]["values"],
                                     "stages": stages, "state": "waiting"})
    _music_gen_wake_worker()
    return {**answer, "replayed": False}


def _music_gen_set(project_id: str, take_id: str, **fields: Any) -> None:
    def put(body: dict[str, Any]) -> None:
        if take_id in body["takes"]:
            body["takes"][take_id].update(fields)
    try:
        _music_gen_update(project_id, put)
    except Exception:  # noqa: BLE001 - the take keeps its last stored state
        pass


def _music_gen_run_one(item: dict[str, Any]) -> None:
    target, values, project_id, take_id = item["target"], item["values"], item["project_id"], item["take_id"]
    manifest = target["manifest"]
    audio = any(manifest["graphs"][s]["output"]["kind"] == "audio" for s in item["stages"])
    folder = Path(item["output"]) / "Music" / _music_gen_runner.safe_name(manifest["engine_label"], 24)
    name = _project_result_name(item["directory"], folder, item.get("project_name"), "audio", _music_gen_runner.TAKE_SUFFIX)
    save_path = folder / name
    store = _comfy_store()
    record = None
    args = {"prompt": _music_gen_prompt_text(manifest, values), "seed": values.get("seed"),
            **{k: v for k, v in values.items() if k not in ("prompt", "seed") and isinstance(v, (str, int, float, bool))}}
    try:
        if audio:
            generation_request = generation.GenerationRequest(
                client_request_id=f"music-{take_id}", provider_id="comfyui-local", target_id=manifest["id"], args=args,
                save_to=str(save_path), approved_roots=[str(folder)], project_id=project_id)
            record = store.create_or_get_job(generation_request, connection_id=_MUSIC_GEN_CONNECTION, frozen_destination=str(folder),
                                             requested_basename=name)
            _music_gen_set(project_id, take_id, studio_job_id=record.studio_job_id)

        def on_stage(stage: str, facts: dict[str, Any]) -> None:
            if stage != "waiting_for_engine" and record is not None and stage == item["stages"][-1]:
                try:
                    store.transition(record.studio_job_id, "running")
                except Exception:  # noqa: BLE001 - the ledger row may already run (two stages)
                    pass
            _music_gen_set(project_id, take_id, state="waiting" if stage == "waiting_for_engine" else "running",
                           stage=None if stage == "waiting_for_engine" else stage, notes=facts.get("notes") or values.get("notes") or None)

        facts = _music_gen_runner.run_take(
            target=target, stages=item["stages"], values=values, compile_graph=_music_gen_targets.compile_graph,
            expected_seconds=_music_gen_targets.expected_seconds, save_path=save_path, comfy=_MusicGenWatch(_music_gen_comfy()),
            ffmpeg=FFMPEG_PATH, loudness=_music_gen_loudness, on_stage=on_stage, sleep=_music_gen_sleep,
            on_progress=lambda value: _MUSIC_GEN_PROGRESS.__setitem__(take_id, value) if value is not None else _MUSIC_GEN_PROGRESS.pop(take_id, None))
        stored_values = dict(values)
        if facts.get("notes"):
            stored_values["notes"] = facts["notes"]
        recipe = {**_music_gen_targets.recipe(target, item["stages"][-1], stored_values), "stages": item["stages"]}
        if record is not None:
            data = save_path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            store.set_output_identity(record.studio_job_id, {"filename": save_path.name, "prompt_id": facts["stages"][-1]["prompt_id"]})
            store.transition(record.studio_job_id, "ready")
            lease = store.acquire_publication_lease(record.studio_job_id, "fs-music", lease_seconds=120)
            store.release_publication_lease(record.studio_job_id, lease.owner_id, lease.token, published_path=str(save_path),
                                            source_media_sha256=digest, published_file_sha256=digest)
            write_recipe_sidecar(save_path, _recipe_envelope({"providerId": "comfyui-local", "targetId": manifest["id"],
                                                              "args": {**args, "music_recipe": recipe}, "saveTo": str(save_path)},
                                                             job_id=record.studio_job_id, approved_roots=[str(folder)]))
        public = {k: facts.get(k) for k in ("seconds", "samples", "sample_rate", "channels", "peak", "clipped_samples", "loudness",
                                            "expected_seconds", "length_exact", "took_seconds", "file")}
        _music_gen_set(project_id, take_id, state="ready", stage=None, facts=public, recipe=recipe, notes=facts.get("notes") or values.get("notes") or None)
    except Exception as exc:  # noqa: BLE001 - recorded on the take
        if record is not None:
            try:
                store.transition(record.studio_job_id, "failed", reason_code="unknown")
            except Exception:  # noqa: BLE001
                pass
        _music_gen_set(project_id, take_id, state="failed", stage=None, error=str(exc)[:200] or "The take failed",
                       code=getattr(exc, "code", None) or "take_failed")
    try:
        film_events.media_changed(f"film_owner:{project_id}")
    except Exception:  # noqa: BLE001
        pass


def _music_gen_worker() -> None:
    while True:
        with _MUSIC_GEN_QUEUE_LOCK:
            item = next((entry for entry in _MUSIC_GEN_QUEUE if entry["state"] == "waiting"), None)
            if item is None:
                return
            item["state"] = "started"
        try:
            _music_gen_run_one(item)
        finally:
            with _MUSIC_GEN_QUEUE_LOCK:
                _MUSIC_GEN_QUEUE.remove(item)


def _music_gen_wake_worker() -> None:
    with _MUSIC_GEN_QUEUE_LOCK:
        if any(t.is_alive() for t in _MUSIC_GEN_THREADS):
            return
        thread = threading.Thread(target=_music_gen_worker, name="fs-music-takes", daemon=True)
        _MUSIC_GEN_THREADS[:] = [thread]
        thread.start()


class _MusicGenWatch:
    """The ComfyUI side with the preview hub watching our own prompt (for the take's %)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def submit(self, graph: Mapping[str, Any]) -> str:
        prompt_id = self._inner.submit(graph)
        try:
            _image_preview_hub().watch(prompt_id)
        except Exception:  # noqa: BLE001 - progress is optional
            pass
        return prompt_id


@router.post("/music/gen/generate")
async def music_gen_generate(request: MusicGenGenerateRequest):
    """Make music on a local target (count 1-4).  Answers at once with take ids; takes run in order."""
    try:
        try:
            _image_preview_hub().ensure_started()
        except Exception:  # noqa: BLE001 - progress is optional
            pass
        return await asyncio.to_thread(_music_gen_generate, request)
    except Exception as exc:  # noqa: BLE001
        return _music_gen_error(exc)


def _music_gen_takes(request: MusicGenTakesRequest) -> dict[str, Any]:
    _directory, project_id, _roots, _name = _music_gen_project(request.project_ref)
    body = _music_gen_read(project_id)
    with _MUSIC_GEN_QUEUE_LOCK:
        live = {entry["take_id"] for entry in _MUSIC_GEN_QUEUE if entry["project_id"] == project_id}
    wanted = set(request.take_ids) if request.take_ids else None
    out = []
    for take_id, take in body["takes"].items():
        if wanted is not None and take_id not in wanted:
            continue
        state, error, code = take["state"], take.get("error"), take.get("code")
        if state in ("waiting", "running") and take_id not in live:
            state, error, code = "failed", "The take was stopped", "take_interrupted"
        facts = take.get("facts") or {}
        out.append({"take_id": take_id, "target_id": take["target_id"], "state": state, "stage": take.get("stage"),
                    "progress": _MUSIC_GEN_PROGRESS.get(take_id) if state == "running" else (1.0 if state == "ready" else None),
                    "seed": take.get("seed"), "by": take.get("by"), "stages": take.get("stages"), "values": take.get("values"),
                    "notes": take.get("notes"), "seconds": facts.get("seconds"), "expected_seconds": take.get("expected_seconds"),
                    "length_exact": facts.get("length_exact"), "peak": facts.get("peak"), "clipped_samples": facts.get("clipped_samples"),
                    "loudness": facts.get("loudness") or {}, "sample_rate": facts.get("sample_rate"), "channels": facts.get("channels"),
                    "file": facts.get("file"), "took_seconds": facts.get("took_seconds"), "recipe": take.get("recipe"),
                    "studio_job_id": take.get("studio_job_id"), "error": error, "code": code, "created_at": take.get("created_at")})
    if wanted is not None and len(out) != len(wanted):
        raise _MusicGenError("take_unknown", "That take is not in this project")
    out.sort(key=lambda item: item["created_at"] or 0)
    return {"schema": "studio.music-takes/1", "takes": out}


@router.post("/music/gen/takes")
async def music_gen_takes(request: MusicGenTakesRequest):
    """The project's music takes: state, stage, %, notes (YuE2), measured seconds, peak, loudness, recipe."""
    try:
        return await asyncio.to_thread(_music_gen_takes, request)
    except Exception as exc:  # noqa: BLE001
        return _music_gen_error(exc)
