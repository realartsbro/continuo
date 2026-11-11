from __future__ import annotations


# --- Audio page, Create: speak one line (lane fs-voice, Voice V1) -------------------------------
#
# Model-agnostic over the pinned voice targets of voice/targets.py (OmniVoice clip/description,
# Drama 3 clip; one graph + manifest each, keep_model_loaded pinned false).  The project's voices,
# characters, lines and the page's take choices live in voice/store.py (studio.voice-project/1).
# A generated take is one row of the Shoot ledger published as a FLAC in the project's output folder
# (Voice/<line>/...), with its recipe sidecar: Media lists it as a sound (out/fs-msnd-SPEC).
# Generation is local only and needs the artist's (or Hermes's on the artist's ask) explicit
# request; each take reads GET /queue before its submit and only ever appends.  Every route here
# is an agent tool (agent_tools.ROUTE_TOOLS) with the same answer the page reads.

import threading  # noqa: E402 - the composition namespace has no threading import


def _voice_load(name: str) -> Any:
    module_name = f"film_suite_voice_{name}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    # Executed in plugin_api's namespace: __file__ is dashboard/plugin_api.py, the modules are dashboard/voice/*.py.
    spec = importlib.util.spec_from_file_location(module_name, Path(__file__).with_name("voice") / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("The voice module is unavailable.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_voice_targets_module = _voice_load("targets")
_voice_store_module = _voice_load("store")
_voice_runner_module = _voice_load("runner")
_voice_asr_module = _voice_load("asr")
_VOICE_PROGRESS: dict[str, float] = {}
_VOICE_THREADS: list[Any] = []
_VOICE_TAKES_MAX = 8
_VOICE_HEALTH_TTL_S = 20.0
_VOICE_HEALTH_CACHE: dict[str, Any] = {}


def _voice_state_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(state_root(Path(base) / "hermes")) / "voice"


class VoiceProjectRequest(_ProjectRequest):
    project_ref: ProjectReference


class VoiceStateWriteRequest(VoiceProjectRequest):
    expected_revision: int = Field(ge=0)
    state: dict[str, Any]


class VoiceImportRequest(VoiceProjectRequest):
    """Copy sound files into the project (Voice/References of its media folder: legacy = first approved folder) and make each a voice."""
    paths: list[str] = Field(min_length=1, max_length=40)
    source_roots: list[str] = Field(min_length=1, max_length=8)
    transcribe: bool = False
    profiles: dict[str, dict[str, Any]] = Field(default_factory=dict)


class VoiceTranscribeRequest(VoiceProjectRequest):
    voice_id: str = Field(pattern=r"^voice_[0-9a-f]{12}$")


class VoiceGenerateRequest(VoiceProjectRequest):
    line_id: str = Field(pattern=r"^line_[0-9a-f]{12}$")
    target_id: str = Field(min_length=1, max_length=64)
    count: int = Field(default=1, ge=1, le=_VOICE_TAKES_MAX)
    values: dict[str, Any] = Field(default_factory=dict)
    seeds: list[int] | None = Field(default=None, min_length=1, max_length=_VOICE_TAKES_MAX)
    by: Literal["artist", "hermes"] = "artist"
    # Lane w21-rel (AUD-145; reference: /generation submit client_request_id): a caller-stable id; the same id
    # with the same request answers the batch it made, never a second one.
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")


class VoiceTakesRequest(VoiceProjectRequest):
    line_id: str | None = Field(default=None, pattern=r"^line_[0-9a-f]{12}$")


class VoiceTakeRequest(VoiceProjectRequest):
    studio_job_id: str = Field(pattern=_take_ids_module.STUDIO_JOB_ID_PATTERN, min_length=39, max_length=43)


class VoiceTakeSetRequest(VoiceTakeRequest):
    selected: bool | None = None
    gain_db: float | None = Field(default=None, ge=-24, le=24)


class VoiceMatchRequest(VoiceProjectRequest):
    line_id: str = Field(pattern=r"^line_[0-9a-f]{12}$")
    target_lufs: float | None = Field(default=None, ge=-40, le=-6)
    reset: bool = False


class VoiceTargetsRequest(_ProjectRequest):
    check: bool = True


def _voice_error(exc: BaseException) -> JSONResponse:
    code = getattr(exc, "code", None)
    if code == "voice_state_conflict":
        return JSONResponse(status_code=409, content={"error": {"code": code, "message": str(exc)}})
    if isinstance(exc, (_voice_store_module.VoiceStoreError, _voice_targets_module.VoiceTargetError, _voice_runner_module.VoiceRunError)):
        return JSONResponse(status_code=422, content={"error": {"code": code or "invalid_voice_request", "message": str(exc)}})
    if isinstance(exc, studio_core.ProjectError):
        return JSONResponse(status_code=404 if exc.code == "project_not_found" else 503,
                            content={"error": {"code": exc.code, "message": "The project could not be read"}})
    return JSONResponse(status_code=503, content={"error": {"code": "voice_unavailable", "message": "Voice is unavailable"}})


def _voice_project(reference: ProjectReference) -> tuple[str, str, list[str]]:
    directory = _project_reference_directory(reference)
    manifest = _project_store_factory().read_project(directory, expected_project_id=reference.project_id)
    return directory, reference.project_id, list(manifest["approved_roots"])


def _voice_project_name(directory: str, project_id: str) -> str:
    """The project's display name for the naming rule (lane p6-name)."""
    try:
        return str(_project_store_factory().read_project(directory, expected_project_id=project_id).get("name") or "Project")
    except Exception:  # noqa: BLE001 - the rule's fallback name
        return "Project"


def _voice_home(roots: list[str], output: Any = None) -> Path:
    """Where the project's voice files live (Project charter decisions 1, 2 + the 28.10. write-sites line).

    output given = a generated take: <project output folder>/Voice. Without it = imported material (voice
    references, recordings): <first approved folder>/Voice, where the project's imports go (refused without a folder).
    """
    if output is not None:
        return Path(output) / "Voice"
    if not roots:
        raise _voice_store_module.VoiceStoreError("no_folder", "No project folder")
    return Path(roots[0]) / "Voice"


def _voice_import_home(directory: str, roots: list[str]) -> Path:
    """Imported material (Project decision 1 + 28.10.): a legacy project's first approved folder, else <project>/media."""
    if roots:
        return _voice_home(roots)
    return _project_media_dir(directory, create=True) / "Voice"


def _voice_public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {"schema": _voice_store_module.SCHEMA, **state}


@router.post("/voice/state/read")
async def voice_state_read(request: VoiceProjectRequest):
    """The project's voices, characters, lines and take choices, with the revision to write against."""
    try:
        _directory, project_id, _roots = await asyncio.to_thread(_voice_project, request.project_ref)
        return _voice_public_state(await asyncio.to_thread(_voice_store_module.read, _voice_state_dir(), project_id))
    except Exception as exc:  # noqa: BLE001 - finite boundary
        return _voice_error(exc)


@router.post("/voice/state/write")
async def voice_state_write(request: VoiceStateWriteRequest):
    """Save voices, characters, lines and take choices whole, with expected_revision."""
    try:
        _directory, project_id, _roots = await asyncio.to_thread(_voice_project, request.project_ref)
        written = await asyncio.to_thread(_voice_store_module.write, _voice_state_dir(), project_id, request.expected_revision, request.state)
        return _voice_public_state(written)
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


def _voice_copy_in(source: Path, folder: Path) -> tuple[Path, str]:
    """Copy one file into folder under its own name (a sha256 suffix when the name is taken by other bytes)."""
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        target = folder / f"{source.stem}-{digest[:8]}{source.suffix}"
    if not target.exists():
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
    return target, digest


def _voice_import(request: VoiceImportRequest) -> dict[str, Any]:
    directory, project_id, roots = _voice_project(request.project_ref)
    folder = _voice_import_home(directory, roots) / "References"  # imported material stays where imports go (28.10.)
    footage = _timeline_module("footage")
    index = _timeline_audio_index(project_id)
    made: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for position, raw in enumerate(request.paths):
        try:
            source = resolve_approved_path(raw, list(request.source_roots))
            if not source.is_file() or source.suffix.lower() not in footage.AUDIO_SUFFIXES:
                raise ValueError("Only sound files can be a voice")
            copied, _digest = _voice_copy_in(source, folder)
            entry = index.add(copied)
            facts = entry["facts"]
            seconds = round(facts["samples"] / facts["sample_rate"], 3) if facts.get("samples") and facts.get("sample_rate") else None
            words = ""
            if request.transcribe:
                heard = _voice_asr_module.transcribe(copied)
                words = (heard or {}).get("text", "").strip()
            profile = request.profiles.get(source.name) or request.profiles.get(source.stem) or {}
            made.append({"voice": {"id": _voice_store_module.new_id("voice"), "name": source.stem.replace("_", " ")[:80], "source": "clip",
                                   # Over 20 s a clip is trimmed to its first 15 s (OmniVoice's own advice; IN/OUT stay editable).
                                   "clip": {"ref": entry["ref"], "name": copied.name, "in": 0.0, "out": 15.0 if seconds and seconds > 20 else None},
                                   "words": words, "description": "", "seed": None, "profile": dict(profile)},
                         "seconds": seconds, "loudness": facts.get("loudness") or {}, "transcribed": bool(words)})
        except (OSError, ValueError, footage.FootageError) as exc:
            errors.append({"index": position, "message": str(exc) if isinstance(exc, (ValueError, footage.FootageError)) else "The file could not be read"})
    if made:
        def add(body: dict[str, Any]) -> None:
            body["voices"].extend(item["voice"] for item in made)
        state = _voice_store_module.update(_voice_state_dir(), project_id, add)
    else:
        state = _voice_store_module.read(_voice_state_dir(), project_id)
    # A voice whose words were transcribed with a clip trim that does not match is re-transcribed by /voice/transcribe.
    return {"voices": [{**item["voice"], "seconds": item["seconds"], "loudness": item["loudness"]} for item in made], "errors": errors,
            "revision": state["revision"]}


@router.post("/voice/import")
async def voice_import(request: VoiceImportRequest):
    """Copy the artist's reference clips into the project and make each a voice (optionally transcribed locally)."""
    try:
        return await asyncio.to_thread(_voice_import, request)
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


def _voice_clip_file(project_id: str, roots: list[str], clip: Mapping[str, Any]) -> Path:
    path, _facts = _timeline_audio_index(project_id).locate(clip["ref"], roots)
    return Path(path)


def _voice_transcribe(request: VoiceTranscribeRequest) -> dict[str, Any]:
    _directory, project_id, roots = _voice_project(request.project_ref)
    state = _voice_store_module.read(_voice_state_dir(), project_id)
    voice = next((v for v in state["voices"] if v["id"] == request.voice_id), None)
    if voice is None or voice["clip"] is None:
        raise _voice_store_module.VoiceStoreError("voice_not_found", "That voice has no clip")
    clip = voice["clip"]
    wav = _voice_runner_module.cut_clip(_voice_clip_file(project_id, roots, clip), clip["in"], clip["out"], FFMPEG_PATH, _voice_state_dir() / "scratch")
    scratch = _voice_state_dir() / "scratch" / f"asr-{uuid.uuid4().hex}.wav"
    scratch.write_bytes(wav)
    try:
        heard = _voice_asr_module.transcribe(scratch)
    finally:
        scratch.unlink(missing_ok=True)
    if heard is None or not heard["text"].strip():
        return {"voice_id": voice["id"], "words": None, "message": "No local speech recognition could read this clip",
                "revision": state["revision"]}

    def put(body: dict[str, Any]) -> None:
        for item in body["voices"]:
            if item["id"] == voice["id"]:
                item["words"] = heard["text"].strip()[:4000]
    written = _voice_store_module.update(_voice_state_dir(), project_id, put)
    return {"voice_id": voice["id"], "words": heard["text"].strip(), "language": heard.get("language"), "revision": written["revision"]}


@router.post("/voice/transcribe")
async def voice_transcribe(request: VoiceTranscribeRequest):
    """Write the words of a voice clip (its IN-OUT) with the local recognizer; nothing is invented."""
    try:
        return await asyncio.to_thread(_voice_transcribe, request)
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


async def _voice_live_health() -> dict[str, Any]:
    cached = _VOICE_HEALTH_CACHE.get("value")
    if cached is not None and time.monotonic() - _VOICE_HEALTH_CACHE.get("at", 0) < _VOICE_HEALTH_TTL_S:
        return cached
    client = _comfy_client_module.ComfyUIClient()
    out: dict[str, Any] = {}
    targets = {tid: _voice_targets_module.load_target(tid)["manifest"] for tid in _voice_targets_module.TARGET_IDS}
    try:
        await client.system_stats()
        names = sorted({c for m in targets.values() for c in m["required_classes"]})
        info: dict[str, Any] = {}
        for name in names:
            try:
                answer = await client.object_info(name)
                if isinstance(answer, dict) and name in answer:
                    info[name] = answer[name]
            except Exception:  # noqa: BLE001 - a missing class is a reason, not a failure
                pass
        folders = sorted({m2["folder"] for m in targets.values() for m2 in m["models"]})
        models: dict[str, Any] = {}
        for folder in folders:
            try:
                models[folder] = await client.models(folder)
            except Exception:  # noqa: BLE001
                models[folder] = None
    except Exception:  # noqa: BLE001 - unreachable engine
        info, models = None, {}
    for target_id, manifest in targets.items():
        out[target_id] = _voice_targets_module.health(manifest, info, models)
    _VOICE_HEALTH_CACHE.update(value=out, at=time.monotonic())
    return out


@router.post("/voice/targets")
async def voice_targets(request: VoiceTargetsRequest):
    """The voice targets (engine, voice source, cues, the controls each really has) and their readiness."""
    try:
        listed = [_voice_targets_module.public_target(_voice_targets_module.load_target(tid)["manifest"]) for tid in _voice_targets_module.TARGET_IDS]
        health = await _voice_live_health() if request.check else {}
        for target in listed:
            target["health"] = health.get(target["id"]) or {"ready": None, "reasons": ["Not checked"]}
        return {"schema": "studio.voice-targets/1", "targets": listed}
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


def _voice_measure(path: Path) -> dict[str, Any]:
    measured = _timeline_module("footage").probe_audio(path, ffprobe=FFPROBE_PATH, ffmpeg=FFMPEG_PATH)
    return {"seconds": round(measured["samples"] / measured["sample_rate"], 3), "loudness": measured["loudness"],
            "sample_rate": measured["sample_rate"], "channels": measured["channels"]}


def _voice_write_recipe(path: Path, studio_job_id: str, entry: dict[str, Any], roots: list[str]) -> None:
    write_recipe_sidecar(path, _recipe_envelope(entry, job_id=studio_job_id, approved_roots=roots))


def _voice_progress(prompt_id: str) -> float | None:
    try:
        live = _image_preview_hub().live(prompt_id)
    except Exception:  # noqa: BLE001
        return None
    step, total = live.get("step"), live.get("total")
    return round(step / total, 3) if type(step) is int and type(total) is int and total > 0 else None


def _voice_plan(request: VoiceGenerateRequest) -> dict[str, Any]:
    """Everything a batch needs, checked before anything is queued (one clear refusal)."""
    _directory, project_id, roots = _voice_project(request.project_ref)
    output = _project_output_dir(_directory, project_id, create=True)
    if request.target_id not in _voice_targets_module.TARGET_IDS:
        raise _voice_targets_module.VoiceTargetError("That voice model is not in the catalog")
    target = _voice_targets_module.load_target(request.target_id)
    manifest = target["manifest"]
    state = _voice_store_module.read(_voice_state_dir(), project_id)
    line, voice = _voice_store_module.voice_of_line(state, request.line_id)
    if not line["text"].strip():  # w35-ui item A: an empty line is stored as a draft, never spoken
        raise _voice_store_module.VoiceStoreError("line_empty", "This line has no text yet")
    if voice is None:
        raise _voice_store_module.VoiceStoreError("no_voice", f"{line['character']} has no voice yet")
    if manifest["voice_from"] != voice["source"]:
        need = "a clip" if manifest["voice_from"] == "clip" else "a description"
        raise _voice_store_module.VoiceStoreError("voice_mismatch", f"{manifest['engine_label']} speaks from {need}; {voice['name']} has a {voice['source']}")
    text, warnings = _voice_targets_module.render_text(line["text"], line["direction"], manifest)
    clip_wav = None
    if voice["source"] == "clip":
        clip = voice["clip"]
        clip_wav = _voice_runner_module.cut_clip(_voice_clip_file(project_id, roots, clip), clip["in"], clip["out"], FFMPEG_PATH, _voice_state_dir() / "scratch")
    seed_control = next((c for c in manifest["controls"] if c["id"] == "seed"), None)
    base_seed = request.values.get("seed", voice["seed"] if voice["seed"] is not None else (seed_control or {}).get("default", 1))
    seeds = list(request.seeds) if request.seeds else [int(base_seed) + n for n in range(request.count)]
    # Compile once with the first seed: a bad value refuses the whole batch before the queue sees it.
    _voice_targets_module.compile_graph(target, text=text, values={**request.values, "seed": seeds[0]},
                                        reference="check.wav [temp]" if clip_wav is not None else None, words=voice["words"], description=voice["description"])
    return {"project_id": project_id, "roots": roots, "output": str(output), "target": target, "directory": _directory,
            "project_name": _voice_project_name(_directory, project_id), "line": line, "voice": voice, "text": text,
            "warnings": warnings, "clip_wav": clip_wav, "seeds": seeds}


# Waiting takes: one FIFO for the whole plugin, worked by one thread.  The ledger allows one active
# local row per connection, so a take's ledger row is made when it starts (connection "local-voice",
# beside Shoot's own); until then it is only this in-memory plan (shown as waiting, never submitted;
# a restart drops it, nothing half-done remains).
_VOICE_QUEUE: list[dict[str, Any]] = []
_VOICE_QUEUE_LOCK = threading.Lock()
_VOICE_CONNECTION = "local-voice"


def _voice_run_one(item: dict[str, Any]) -> None:
    plan, seed = item["plan"], item["seed"]
    manifest = plan["target"]["manifest"]
    line, voice = plan["line"], plan["voice"]
    store = _comfy_store()
    folder = _voice_home(plan["roots"], plan.get("output")) / _voice_runner_module.safe_name(f"{line['character']} {line['text']}", 48)
    # lane p6-name (Project decision 10): the take's file is named by the project rule when it starts.
    name = _project_result_name(plan["directory"], folder, plan.get("project_name"), "audio", _voice_runner_module.TAKE_SUFFIX)         if plan.get("directory") else f"{_voice_runner_module.safe_name(line['character'], 20)}-{manifest['engine']}-{seed}-{uuid.uuid4().hex[:6]}{_voice_runner_module.TAKE_SUFFIX}"
    args = {"prompt": f"{line['character']}: {line['text']}", "seed": seed, "voice": voice["name"],
            **{k: v for k, v in plan["values"].items() if k != "seed"}}
    generation_request = generation.GenerationRequest(
        client_request_id=item["client_request_id"], provider_id="comfyui-local", target_id=manifest["id"], args=args,
        save_to=str(folder / name), approved_roots=[str(folder)], project_id=plan["project_id"])
    facts: dict[str, Any] = {"target_id": manifest["id"], "seed": seed, "warnings": plan["warnings"]}
    try:
        record = store.create_or_get_job(generation_request, connection_id=_VOICE_CONNECTION, frozen_destination=str(folder), requested_basename=name)
    except Exception:  # noqa: BLE001 - no row: the take is reported failed in the plan
        item["error"] = "The take could not be started"
        return
    item["studio_job_id"] = record.studio_job_id

    def register(body: dict[str, Any]) -> None:
        body["takes"][record.studio_job_id] = {"line_id": line["id"], "selected": False, "gain_db": 0.0, "facts": {**facts, "state": "running"}}
    _voice_store_module.update(_voice_state_dir(), plan["project_id"], register)
    try:
        result = _voice_runner_module.run_take(
            store=store, studio_job_id=record.studio_job_id, target=plan["target"], compile_graph=_voice_targets_module.compile_graph,
            text=plan["text"], values={**plan["values"], "seed": seed}, clip_wav=plan["clip_wav"], words=voice["words"],
            description=voice["description"], save_path=folder / name, comfy=_VoiceWatch(_voice_comfy()), measure=_voice_measure,
            write_recipe=lambda path, job_id: _voice_write_recipe(path, job_id, {
                "providerId": "comfyui-local", "targetId": manifest["id"], "args": args, "saveTo": str(path)}, [str(folder)]),
            on_progress=lambda job_id, value: _VOICE_PROGRESS.__setitem__(job_id, value) if value is not None else _VOICE_PROGRESS.pop(job_id, None))
        facts.update(state="ready", seconds=result["seconds"], loudness=result["loudness"], took_seconds=result["took_seconds"])
    except Exception as exc:  # noqa: BLE001 - recorded on the take
        facts.update(state="failed", error=str(exc)[:200] or "The take failed")
        # Lane w27-rows item 6 (VOICE-1): the Jobs row names the same reason as the take (not "Unknown generation error").
        _job_cause_note(record.studio_job_id, facts["error"])

    def put(body: dict[str, Any]) -> None:
        if record.studio_job_id in body["takes"]:
            body["takes"][record.studio_job_id]["facts"] = facts
    try:
        _voice_store_module.update(_voice_state_dir(), plan["project_id"], put)
    except Exception:  # noqa: BLE001 - the ledger still has the take
        pass
    try:
        film_events.media_changed(f"film_owner:{plan['project_id']}")
    except Exception:  # noqa: BLE001
        pass


def _voice_worker() -> None:
    while True:
        with _VOICE_QUEUE_LOCK:
            item = next((entry for entry in _VOICE_QUEUE if entry["state"] == "waiting"), None)
            if item is None:
                return
            item["state"] = "started"
        try:
            _voice_run_one(item)
        finally:
            with _VOICE_QUEUE_LOCK:
                _VOICE_QUEUE.remove(item)


def _voice_wake_worker() -> None:
    with _VOICE_QUEUE_LOCK:
        if any(t.is_alive() for t in _VOICE_THREADS):
            return
        thread = threading.Thread(target=_voice_worker, name="fs-voice-takes", daemon=True)
        _VOICE_THREADS[:] = [thread]
        thread.start()


_VOICE_REQUESTS_LOCK = threading.Lock()
_VOICE_REQUESTS_KEEP = 64


def _voice_requests_path(project_id: str) -> Path:
    return _voice_state_dir() / "requests" / f"{project_id}.json"


def _voice_requests_read(project_id: str) -> dict[str, Any]:
    try:
        body = json.loads(_voice_requests_path(project_id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(body, dict) or body.get("schema") != "studio.voice-requests/1" or not isinstance(body.get("requests"), dict):
        raise _voice_store_module.VoiceStoreError("voice_state_unreadable", "The voice requests could not be read")
    return body["requests"]


def _voice_requests_write(project_id: str, requests: dict[str, Any]) -> None:
    folder = _voice_requests_path(project_id).parent
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / f".{project_id}.{uuid.uuid4().hex}.tmp"
    kept = dict(sorted(requests.items(), key=lambda kv: kv[1].get("at", 0))[-_VOICE_REQUESTS_KEEP:])
    temporary.write_text(json.dumps({"schema": "studio.voice-requests/1", "requests": kept}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(temporary, _voice_requests_path(project_id))


def _voice_request_digest(request: VoiceGenerateRequest) -> str:
    body = request.model_dump(mode="json", exclude={"request_id"})
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _voice_replay(requests: dict[str, Any], request_id: str, digest: str) -> dict[str, Any] | None:
    kept = requests.get(request_id)
    if not isinstance(kept, dict):
        return None
    if kept.get("digest") != digest:
        raise _voice_store_module.VoiceStoreError("request_id_reused", "This request_id already made another batch")
    return {**kept["answer"], "replayed": True}


def _voice_generate(request: VoiceGenerateRequest) -> dict[str, Any]:
    # Lane w21-rel (AUD-145): with a request_id the batch's receipt is durable before anything is queued, and the
    # whole check-plan-queue runs under one lock, so a lost-response retry answers the same batch.
    if request.request_id is None:
        return _voice_generate_batch(request)
    _directory, project_id, _roots = _voice_project(request.project_ref)
    digest = _voice_request_digest(request)
    with _VOICE_REQUESTS_LOCK:
        requests = _voice_requests_read(project_id)
        replay = _voice_replay(requests, request.request_id, digest)
        if replay is not None:
            return replay
        return _voice_generate_batch(request, receipt=lambda answer: _voice_requests_write(
            project_id, {**requests, request.request_id: {"digest": digest, "answer": answer, "at": time.time()}}))


def _voice_generate_batch(request: VoiceGenerateRequest, receipt: Any = None) -> dict[str, Any]:
    plan = _voice_plan(request)
    plan["values"] = dict(request.values)
    batch = uuid.uuid4().hex[:12]
    planned = []
    items = []
    for seed in plan["seeds"]:
        items.append({"plan": plan, "seed": seed, "state": "waiting", "batch": batch, "client_request_id": f"voice-{uuid.uuid4().hex}",
                      "line_id": plan["line"]["id"], "project_id": plan["project_id"], "target_id": plan["target"]["manifest"]["id"], "studio_job_id": None})
        planned.append({"seed": seed, "request_id": items[-1]["client_request_id"]})
    answer = {"line_id": plan["line"]["id"], "target_id": plan["target"]["manifest"]["id"], "batch": batch, "planned": planned,
              "warnings": plan["warnings"]}
    if receipt is not None:
        receipt(answer)
    with _VOICE_QUEUE_LOCK:
        _VOICE_QUEUE.extend(items)
    _voice_wake_worker()
    return {**answer, "replayed": False}


def _voice_comfy() -> Any:
    """The configured local engine (comfy_endpoint); the preview reader's client id, so its % reaches the take."""
    return _voice_runner_module.ComfyHTTP(client_id=_comfyui_preview_module.PREVIEW_CLIENT_ID, progress=_voice_progress)


class _VoiceWatch:
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


@router.post("/voice/generate")
async def voice_generate(request: VoiceGenerateRequest):
    """Speak one line N times (local engine only).  Answers at once with the take ids; takes run in order."""
    try:
        try:
            _image_preview_hub().ensure_started()
        except Exception:  # noqa: BLE001 - progress is optional
            pass
        return await asyncio.to_thread(_voice_generate, request)
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


def _voice_takes(request: VoiceTakesRequest) -> dict[str, Any]:
    _directory, project_id, _roots = _voice_project(request.project_ref)
    state = _voice_store_module.read(_voice_state_dir(), project_id)
    store = _comfy_store()
    marks = _media_marks_read(f"film_owner:{project_id}").get("items") or {}
    out = []
    for job_id, take in state["takes"].items():
        if request.line_id and take["line_id"] != request.line_id:
            continue
        try:
            record = store.get_job(job_id)
            raw, created = record.state, record.created_at
        except Exception:  # noqa: BLE001 - a missing row reads as failed
            raw, created = "failed", None
        mark = marks.get(job_id) or {}
        facts = take["facts"]
        shown = {"ready": "ready", "failed": "failed", "running": "running"}.get(raw, "waiting")
        # The ledger row turns ready a moment before the take's measured facts are saved: until they are,
        # the take still reads running at 100 % (never "ready" with no length or loudness).
        if shown == "ready" and facts.get("state") != "ready":
            shown = "failed" if facts.get("state") == "failed" else "running"
        out.append({"studio_job_id": job_id, "line_id": take["line_id"], "state": shown,
                    "progress": 1.0 if shown == "running" and raw == "ready" else _VOICE_PROGRESS.get(job_id), "selected": take["selected"], "gain_db": take["gain_db"],
                    "kept": bool(mark.get("kept")), "rating": int(mark.get("rating") or 0), "target_id": facts.get("target_id"),
                    "seed": facts.get("seed"), "seconds": facts.get("seconds"), "loudness": facts.get("loudness") or {},
                    "took_seconds": facts.get("took_seconds"), "warnings": facts.get("warnings") or [], "error": facts.get("error"),
                    "created_at": _finite_epoch(created), "words": facts.get("words") or None, "source": facts.get("source") or "engine",
                    "recut_of": facts.get("recut_of"), "pauses": facts.get("pauses")})
    out.sort(key=lambda item: item["created_at"] or 0)
    with _VOICE_QUEUE_LOCK:
        waiting = [entry for entry in _VOICE_QUEUE if entry["project_id"] == project_id and entry["studio_job_id"] is None
                   and (not request.line_id or entry["line_id"] == request.line_id)]
        for entry in waiting:
            out.append({"studio_job_id": None, "request_id": entry["client_request_id"], "line_id": entry["line_id"], "state": "waiting",
                        "progress": None, "selected": False, "gain_db": 0.0, "kept": False, "rating": 0, "target_id": entry["target_id"],
                        "seed": entry["seed"], "seconds": None, "loudness": {}, "took_seconds": None, "warnings": [], "error": None,
                        "created_at": None, "words": None, "source": "engine", "recut_of": None, "pauses": None})
    return {"takes": out, "revision": state["revision"]}


@router.post("/voice/takes")
async def voice_takes(request: VoiceTakesRequest):
    """The takes of one line (or all): state (waiting/running/ready/failed), %, seconds, loudness, kept, rating, selected, gain."""
    try:
        return await asyncio.to_thread(_voice_takes, request)
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


def _voice_take_set(request: VoiceTakeSetRequest) -> dict[str, Any]:
    _directory, project_id, _roots = _voice_project(request.project_ref)

    def put(body: dict[str, Any]) -> None:
        take = body["takes"].get(request.studio_job_id)
        if take is None:
            raise _voice_store_module.VoiceStoreError("take_not_found", "That take is not in this project")
        if request.selected is not None:
            if request.selected:
                for other in body["takes"].values():
                    if other["line_id"] == take["line_id"]:
                        other["selected"] = False
            take["selected"] = request.selected
        if request.gain_db is not None:
            take["gain_db"] = round(float(request.gain_db), 1)
    state = _voice_store_module.update(_voice_state_dir(), project_id, put)
    return {"studio_job_id": request.studio_job_id, **state["takes"][request.studio_job_id], "revision": state["revision"]}


@router.post("/voice/take/set")
async def voice_take_set(request: VoiceTakeSetRequest):
    """Select a take for its line (one per line) and/or set its gain (dB, non-destructive). Keep and stars are Media marks."""
    try:
        return await asyncio.to_thread(_voice_take_set, request)
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


def _voice_match(request: VoiceMatchRequest) -> dict[str, Any]:
    _directory, project_id, _roots = _voice_project(request.project_ref)
    gains: dict[str, float] = {}

    def put(body: dict[str, Any]) -> None:
        mine = {job_id: take for job_id, take in body["takes"].items() if take["line_id"] == request.line_id}
        if request.reset:
            gains.update({job_id: 0.0 for job_id in mine})
        else:
            loudness = {job_id: (take["facts"].get("loudness") or {}).get("integrated_lufs") for job_id, take in mine.items()
                        if take["facts"].get("state") == "ready"}
            gains.update(_voice_runner_module.match_gains(loudness, request.target_lufs))
        for job_id, gain in gains.items():
            mine[job_id]["gain_db"] = gain
    state = _voice_store_module.update(_voice_state_dir(), project_id, put)
    return {"line_id": request.line_id, "gains": gains, "revision": state["revision"]}


@router.post("/voice/match")
async def voice_match(request: VoiceMatchRequest):
    """Match level: a gain per take so the line's finished takes sit at one loudness (median, or target_lufs); reset=true clears it."""
    try:
        return await asyncio.to_thread(_voice_match, request)
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


def _voice_take_sound(request: VoiceTakeRequest) -> dict[str, Any]:
    _directory, project_id, _roots = _voice_project(request.project_ref)
    state = _voice_store_module.read(_voice_state_dir(), project_id)
    take = state["takes"].get(request.studio_job_id)
    if take is None:
        raise _voice_store_module.VoiceStoreError("take_not_found", "That take is not in this project")
    record = _comfy_store().get_job(request.studio_job_id)
    if record.state != "ready" or not record.published_path or not Path(record.published_path).is_file():
        raise _voice_store_module.VoiceStoreError("take_not_ready", "That take has no sound yet")
    data = _voice_runner_module.apply_gain(Path(record.published_path), take["gain_db"], FFMPEG_PATH, _voice_state_dir() / "scratch")
    return {"studio_job_id": request.studio_job_id, "mime": "audio/flac", "gain_db": take["gain_db"],
            "data_b64": base64.b64encode(data).decode("ascii")}


@router.post("/voice/take/sound")
async def voice_take_sound(request: VoiceTakeRequest):
    """One finished take as FLAC (base64) with its gain applied, for listening; never a path."""
    try:
        return await asyncio.to_thread(_voice_take_sound, request)
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


class VoiceToCutRequest(VoiceTakeRequest):
    expected_revision: int = Field(ge=0)
    role: Literal["dialogue", "music", "effects"] = "dialogue"
    at_frame: int = Field(default=0, ge=0)
    by: Literal["artist", "hermes"] = "artist"


def _voice_to_cut(request: VoiceToCutRequest) -> dict[str, Any]:
    """The take's file (it lives in the project's folder) registered like any sound file and placed on
    the role's sound track with the take's gain, in one timeline revision (one undo step)."""
    directory, project_id, _roots = _voice_project(request.project_ref)
    state = _voice_store_module.read(_voice_state_dir(), project_id)
    take = state["takes"].get(request.studio_job_id)
    if take is None:
        raise _voice_store_module.VoiceStoreError("take_not_found", "That take is not in this project")
    record = _comfy_store().get_job(request.studio_job_id)
    if record.state != "ready" or not record.published_path:
        raise _voice_store_module.VoiceStoreError("take_not_ready", "That take has no sound yet")
    registered = _timeline_audio_register(directory, project_id, [record.published_path])
    if not registered["audio"]:
        message = registered["errors"][0]["message"] if registered["errors"] else "The take could not be read"
        raise _voice_store_module.VoiceStoreError("take_not_ready", message)
    entry = registered["audio"][0]
    store = _project_store_factory()
    revision, before = _timeline_current(store, directory, project_id)
    if request.expected_revision != revision:
        raise studio_core.ProjectError("timeline_conflict", f"the timeline is at revision {revision}")
    ops_module = _timeline_module("ops")
    timeline = ops_module._timeline(before, {"op": "place_audio"})
    frames = _timeline_module("footage").audio_frames({"samples": entry["samples"], "sample_rate": entry["sample_rate"]}, timeline["rate"])
    line = next((item for item in state["lines"] if item["id"] == take["line_id"]), None)
    label = f"{line['character']}: {line['text']}"[:200] if line else entry["name"][:200]
    op = {"op": "place_audio", "at_frame": request.at_frame, "role": request.role, "clip": {
        "source": {"kind": "media", "ref": entry["ref"]}, "source_rate": dict(timeline["rate"]), "label": label, "gain_db": take["gain_db"],
        "media_info": {"frames": frames, "sample_rate": entry["sample_rate"], "channels": entry["channels"]}}}
    after, results = ops_module.apply_ops(before, [op])
    # Lane w27-rows item 6 (P1 CUT-043): the take is dialogue landing in the cut, so the music ducks under it in this
    # same revision (one Undo), exactly like /projects/timeline/audio/add and /projects/timeline/apply.
    ducking = _timeline_reduck(before, after, [op], project_id)
    track = results[0]["track"]
    written = store.write_timeline(directory, project_id=project_id, expected_revision=revision, state=after,
                                   label=f"Add {label[:60]} to {track}", by=request.by)
    film_events.timeline_changed(project_id, written["revision"], request.by)
    summary = _timeline_module("document").summarize(before, after)
    return {"track": track, "revision": written["revision"], "created": summary["created"], "ref": entry["ref"], "gain_db": take["gain_db"],
            **({"ducking": ducking} if ducking is not None else {})}


@router.post("/voice/take/to-cut")
async def voice_take_to_cut(request: VoiceToCutRequest):
    """Add to cut: the take on the Dialogue (or Music/Effects) sound track at at_frame, with its gain; one undo step."""
    try:
        return await asyncio.to_thread(_voice_to_cut, request)
    except studio_core.ProjectError as exc:
        if exc.code in ("timeline_conflict", "invalid_op", "overlap"):
            return JSONResponse(status_code=409, content={"error": {"code": exc.code, "message": str(exc)}})
        return _voice_error(exc)
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", None)
        if isinstance(exc, ValueError) and code:
            return JSONResponse(status_code=409, content={"error": {"code": code, "message": str(exc)}})
        return _voice_error(exc)
