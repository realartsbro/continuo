from __future__ import annotations


# --- Audio page, Create: takes that are not spoken by an engine (lane w4-audio, board E15 ⑩/⑭) ------------------
#
# Loaded after voice/voice_1.py, in plugin_api's namespace (it uses the _voice_* helpers there).
# * /voice/take/import: a recording (a sound file of an artist-chosen folder) becomes a take of a line: copied into
#   the line's Voice folder, one ledger row (provider local-voice, target voice-recording-v1) published with its sha256
#   and measured length/loudness, its recipe sidecar written; Media lists it like a spoken take.
# * /voice/take/words: the take's words with their times (local recognizer, word_timestamps), kept on the take;
#   /voice/takes carries them (words) so the words lane places each word on the time axis.
# * /voice/take/recut: Re-cut pauses (E15 ⑭): the pauses between the take's words set without re-speaking (cut in
#   the middle of each pause, voice/takecut.py), published as a NEW take of the line whose recipe names the source.
# * /voice/history/read|add: the Audio page's History per project (you and Hermes), append-only, newest first.
# Every route is an agent tool (agent_tools.ROUTE_TOOLS) with the answer the page reads.

_voice_takecut_module = _voice_load("takecut")
_VOICE_RECORDING_TARGET = "voice-recording-v1"
_VOICE_LOCAL_PROVIDER = "local-voice"
_VOICE_RECUT_TARGET = "voice-recut-v1"
_VOICE_HISTORY_MAX = 500
_VOICE_HISTORY_LOCK = _thread.allocate_lock()


class VoiceTakeImportRequest(VoiceProjectRequest):
    line_id: str = Field(pattern=r"^line_[0-9a-f]{12}$")
    path: str = Field(min_length=1, max_length=4096)
    source_roots: list[str] = Field(min_length=1, max_length=8)
    by: Literal["artist", "hermes"] = "artist"


class VoiceTakeWordsRequest(VoiceTakeRequest):
    language: str | None = Field(default=None, pattern=r"^[a-z]{2}$")


class VoiceTakeRecutRequest(VoiceTakeRequest):
    gaps: dict[str, float] = Field(default_factory=dict, max_length=400)
    max_pause: float | None = Field(default=None, ge=0, le=10)
    end_pause: float | None = Field(default=None, ge=0, le=10)
    by: Literal["artist", "hermes"] = "artist"


class VoiceHistoryAddRequest(VoiceProjectRequest):
    text: str = Field(min_length=1, max_length=200)
    by: Literal["artist", "hermes"] = "artist"


def _voice_history_path(project_id: str) -> Path:
    if not re.fullmatch(r"prj_[0-9a-f]{32}", project_id):
        raise _voice_store_module.VoiceStoreError("invalid_voice_state", "project id is invalid")
    return _voice_state_dir() / f"{project_id}.history.json"


def _voice_history_read(project_id: str) -> list[dict[str, Any]]:
    path = _voice_history_path(project_id)
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        value = {}
    rows = value.get("entries") if isinstance(value, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _voice_history_add(project_id: str, by: str, text: str) -> dict[str, Any]:
    entry = {"by": "hermes" if by == "hermes" else "artist", "text": " ".join(str(text).split())[:200], "at": round(time.time(), 3)}
    with _VOICE_HISTORY_LOCK:
        rows = [entry, *_voice_history_read(project_id)][:_VOICE_HISTORY_MAX]
        path = _voice_history_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps({"schema": "studio.voice-history/1", "project_id": project_id, "entries": rows}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    return entry


def _voice_publish_take(*, project_id: str, roots: list[str], line: Mapping[str, Any], source: Path, name: str, target_id: str,
                        args: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    """A finished sound file already in the line's folder -> one published ledger row + recipe + the take in the voice state."""
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    measured = _voice_measure(source)
    store = _comfy_store()
    request = generation.GenerationRequest(
        client_request_id=f"voice-{uuid.uuid4().hex}", provider_id=_VOICE_LOCAL_PROVIDER, target_id=target_id, args=args,
        save_to=str(source), approved_roots=[str(source.parent)], project_id=project_id)
    record = store.create_or_get_job(request, connection_id=_VOICE_CONNECTION, frozen_destination=str(source.parent), requested_basename=name)
    store.transition(record.studio_job_id, "running")
    store.set_output_identity(record.studio_job_id, {"filename": name, "sha256": digest})
    store.transition(record.studio_job_id, "ready")
    lease = store.acquire_publication_lease(record.studio_job_id, "fs-voice", lease_seconds=120)
    store.release_publication_lease(record.studio_job_id, lease.owner_id, lease.token, published_path=str(source),
                                    source_media_sha256=digest, published_file_sha256=digest)
    _voice_write_recipe(source, record.studio_job_id, {"providerId": _VOICE_LOCAL_PROVIDER, "targetId": target_id, "args": args,
                                                       "saveTo": str(source)}, [str(source.parent)])
    facts = {**facts, "target_id": target_id, "state": "ready", "seconds": measured["seconds"], "loudness": measured["loudness"],
             "sha256": digest, "took_seconds": facts.get("took_seconds", 0.0)}

    def register(body: dict[str, Any]) -> None:
        body["takes"][record.studio_job_id] = {"line_id": line["id"], "selected": False, "gain_db": 0.0, "facts": facts}
    state = _voice_store_module.update(_voice_state_dir(), project_id, register)
    try:
        film_events.media_changed(f"film_owner:{project_id}")
    except Exception:  # noqa: BLE001
        pass
    return {"studio_job_id": record.studio_job_id, "line_id": line["id"], "seconds": measured["seconds"], "loudness": measured["loudness"],
            "sha256": digest, "name": name, "revision": state["revision"]}


def _voice_take_import(request: VoiceTakeImportRequest) -> dict[str, Any]:
    _directory, project_id, roots = _voice_project(request.project_ref)
    state = _voice_store_module.read(_voice_state_dir(), project_id)
    line = next((item for item in state["lines"] if item["id"] == request.line_id), None)
    if line is None:
        raise _voice_store_module.VoiceStoreError("line_not_found", "That line is not in this project")
    footage = _timeline_module("footage")
    try:
        source = resolve_approved_path(request.path, list(request.source_roots))
    except (OSError, ValueError):
        raise _voice_store_module.VoiceStoreError("outside_roots", "The recording is outside the chosen folder") from None
    if not source.is_file() or source.suffix.lower() not in footage.AUDIO_SUFFIXES:
        raise _voice_store_module.VoiceStoreError("not_sound", "Only a sound file can be a take (wav, mp3, flac, ogg, m4a)")
    folder = _voice_home(roots) / _voice_runner_module.safe_name(f"{line['character']} {line['text']}", 48)
    copied, _digest = _voice_copy_in(source, folder)
    try:
        _voice_measure(copied)
    except Exception:  # noqa: BLE001 - not a readable sound: nothing is registered
        raise _voice_store_module.VoiceStoreError("not_sound", "The recording holds no readable sound") from None
    args = {"prompt": f"{line['character']}: {line['text']}", "recording": source.name}
    out = _voice_publish_take(project_id=project_id, roots=roots, line=line, source=copied, name=copied.name, target_id=_VOICE_RECORDING_TARGET,
                              args=args, facts={"seed": None, "warnings": [], "source": "recording", "recording": source.name})
    _voice_history_add(project_id, request.by, f"Recording {source.name[:60]} as a take")
    return out


@router.post("/voice/take/import")
async def voice_take_import(request: VoiceTakeImportRequest):
    """A recording becomes a take of a line (copied into the project, sha256 + length + loudness recorded, Media lists it)."""
    try:
        return await asyncio.to_thread(_voice_take_import, request)
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


def _voice_take_file(project_id: str, studio_job_id: str) -> tuple[dict[str, Any], Path]:
    state = _voice_store_module.read(_voice_state_dir(), project_id)
    take = state["takes"].get(studio_job_id)
    if take is None:
        raise _voice_store_module.VoiceStoreError("take_not_found", "That take is not in this project")
    record = _comfy_store().get_job(studio_job_id)
    if record.state != "ready" or not record.published_path or not Path(record.published_path).is_file():
        raise _voice_store_module.VoiceStoreError("take_not_ready", "That take has no sound yet")
    return take, Path(record.published_path)


def _voice_take_words(request: VoiceTakeWordsRequest) -> dict[str, Any]:
    _directory, project_id, _roots = _voice_project(request.project_ref)
    _take, path = _voice_take_file(project_id, request.studio_job_id)
    heard = _voice_asr_module.transcribe(path, words=True)
    if heard is None or not heard.get("words"):
        return {"studio_job_id": request.studio_job_id, "words": None, "message": "No local speech recognition could read this take"}
    words = _voice_takecut_module.clean_words(heard["words"])[:400]

    def put(body: dict[str, Any]) -> None:
        if request.studio_job_id in body["takes"]:
            body["takes"][request.studio_job_id]["facts"]["words"] = words
    state = _voice_store_module.update(_voice_state_dir(), project_id, put)
    return {"studio_job_id": request.studio_job_id, "words": words, "language": heard.get("language"), "revision": state["revision"]}


@router.post("/voice/take/words")
async def voice_take_words(request: VoiceTakeWordsRequest):
    """The take's words with their start/end seconds (local recognizer, CPU), kept on the take; /voice/takes carries them."""
    try:
        return await asyncio.to_thread(_voice_take_words, request)
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


def _voice_take_recut(request: VoiceTakeRecutRequest) -> dict[str, Any]:
    _directory, project_id, roots = _voice_project(request.project_ref)
    take, path = _voice_take_file(project_id, request.studio_job_id)
    facts = take["facts"]
    measured = _voice_measure(path)
    try:
        plan = _voice_takecut_module.plan(facts.get("words") or [], measured["seconds"], gaps=request.gaps,
                                          max_pause=request.max_pause, end_pause=request.end_pause)
    except _voice_takecut_module.TakeCutError as exc:
        raise _voice_store_module.VoiceStoreError(exc.code, str(exc)) from None
    rate, channels = int(measured["sample_rate"]), int(measured["channels"])
    started = time.monotonic()
    done = subprocess.run([str(FFMPEG_PATH), "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-acodec", "pcm_s16le",
                           "-ar", str(rate), "-ac", str(channels), "-"], capture_output=True, timeout=120, check=False, shell=False, env=_child_env())
    if done.returncode != 0 or not done.stdout:
        raise _voice_runner_module.VoiceRunError("The take could not be read")
    pcm = _voice_takecut_module.splice(done.stdout, rate, channels, plan["pieces"])
    state = _voice_store_module.read(_voice_state_dir(), project_id)
    line = next(item for item in state["lines"] if item["id"] == take["line_id"])
    name = f"{path.stem.split('-recut-')[0]}-recut-{uuid.uuid4().hex[:6]}{_voice_runner_module.TAKE_SUFFIX}"
    target = path.parent / name
    temporary = target.with_name(f".{name}.{uuid.uuid4().hex}.tmp.flac")
    encoded = subprocess.run([str(FFMPEG_PATH), "-hide_banner", "-loglevel", "error", "-y", "-f", "s16le", "-ar", str(rate), "-ac", str(channels),
                              "-i", "-", str(temporary)], input=pcm, capture_output=True, timeout=120, check=False, shell=False, env=_child_env())
    if encoded.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise _voice_runner_module.VoiceRunError("The re-cut take could not be written")
    os.replace(temporary, target)
    pauses = {"before": plan["pauses_before"], "after": plan["pauses_after"], "end_before": plan["end_before"], "end_after": plan["end_after"]}
    args = {"prompt": f"{line['character']}: {line['text']}", "recut_of": request.studio_job_id, "seed": facts.get("seed"),
            "source_target_id": facts.get("target_id"), "pauses_after": plan["pauses_after"], "end_pause": plan["end_after"]}
    out = _voice_publish_take(project_id=project_id, roots=roots, line=line, source=target, name=name, target_id=_VOICE_RECUT_TARGET, args=args,
                              facts={"seed": facts.get("seed"), "warnings": [], "source": "recut", "recut_of": request.studio_job_id,
                                     "source_target_id": facts.get("target_id"), "pauses": pauses, "words": plan["words"],
                                     "took_seconds": round(time.monotonic() - started, 2)})
    shorter = round(measured["seconds"] - out["seconds"], 2)
    _voice_history_add(project_id, request.by, f"Re-cut pauses · {abs(shorter):.2f} s {'shorter' if shorter >= 0 else 'longer'}")
    return {**out, "recut_of": request.studio_job_id, "pauses": pauses, "words": plan["words"], "seconds_before": measured["seconds"]}


@router.post("/voice/take/recut")
async def voice_take_recut(request: VoiceTakeRecutRequest):
    """Re-cut pauses: set the pauses between the take's words (gaps {word index: s}, max_pause, end_pause) without re-speaking; a new take."""
    try:
        return await asyncio.to_thread(_voice_take_recut, request)
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


@router.post("/voice/history/read")
async def voice_history_read(request: VoiceProjectRequest):
    """The Audio page's History of this project, newest first: {by artist|hermes, text, at}."""
    try:
        _directory, project_id, _roots = await asyncio.to_thread(_voice_project, request.project_ref)
        return {"entries": await asyncio.to_thread(_voice_history_read, project_id)}
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)


@router.post("/voice/history/add")
async def voice_history_add(request: VoiceHistoryAddRequest):
    """Add one line to the Audio page's History (by artist or hermes); kept with the project."""
    try:
        _directory, project_id, _roots = await asyncio.to_thread(_voice_project, request.project_ref)
        return {"entry": await asyncio.to_thread(_voice_history_add, project_id, request.by, request.text)}
    except Exception as exc:  # noqa: BLE001
        return _voice_error(exc)
