from __future__ import annotations

import copy


# --- Timeline (studio.timeline/1): read, write, apply verbs, revisions, refs ----------
#
# One document per project, state/timeline.json, through ProjectStore's timeline
# methods. Every edit, the artist's and Hermes', goes through the same algebra
# (dashboard/timeline/ops.py, twin of src/timeline/ops) and lands in the same revision
# history, labelled by who made it. apply answers with ids, changed items and the refs
# it newly placed, never with the whole document; dry_run validates and answers without
# writing. Errors are {code, message, retryable, next}. No route returns a file path.
# A landed write or apply announces timeline.changed {project_id, revision, by} once
# (film_events); a dry run or a refused write announces nothing.

_TIMELINE_LABEL_MAX = 120
_TIMELINE_REASON_MAX = 200  # document.REASON_MAX (lane w8-agent: why, kept in the revision)


class TimelineReadRequest(_ProjectRequest):
    project_ref: ProjectReference
    resolve: bool = False
    # Lane w8-agent: "summary" answers ids, kinds, labels and frames only (document.summary_of);
    # "full" (default) is the answer of before, byte for byte.
    # Lane w23-bins (CUT-180): "cuts" answers Media's facts of every cut (timeline/nest.py cuts_list).
    detail: Literal["full", "summary", "cuts"] = "full"


class TimelineWriteRequest(_ProjectRequest):
    project_ref: ProjectReference
    expected_revision: int = Field(ge=0)
    state: dict[str, Any]
    label: str = Field(default="", max_length=_TIMELINE_LABEL_MAX)
    by: Literal["artist", "hermes"] = "artist"
    reason: str = Field(default="", max_length=_TIMELINE_REASON_MAX)


class TimelineApplyRequest(_ProjectRequest):
    project_ref: ProjectReference
    expected_revision: int = Field(ge=0)
    ops: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    label: str = Field(default="", max_length=_TIMELINE_LABEL_MAX)
    by: Literal["artist", "hermes"] = "hermes"
    dry_run: bool = False
    reason: str = Field(default="", max_length=_TIMELINE_REASON_MAX)
    # Lane w8-agent item 4: a retry with the same id and ops answers the kept answer (replayed).
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")


class TimelineRevisionsRequest(_ProjectRequest):
    project_ref: ProjectReference
    revision: int | None = Field(default=None, ge=1)


class TimelineRefsRequest(_ProjectRequest):
    project_ref: ProjectReference
    resolve: bool = False


def _timeline_module(name: str) -> Any:
    studio_core.ProjectStore._timeline_document()
    return importlib.import_module(f"{studio_core.ProjectStore._TIMELINE_PACKAGE}.{name}")


def _timeline_error(exc: BaseException) -> dict[str, Any]:
    document = _timeline_module("document")
    code = getattr(exc, "code", None)
    if type(code) is not str or code not in document.ERRORS:
        code = "timeline_unavailable"
    message = str(exc) if isinstance(exc, ValueError) and code not in {"timeline_unavailable"} else "Timeline is unavailable"
    return {"error": document.error_body(code, message, getattr(exc, "op_index", None))}


def _timeline_lookups(project_id: str) -> dict[str, Any]:
    """Read-only lookups for the resolver: one ledger row by id, never a listing."""

    def image_job(ref: str) -> Any:
        scope = f"film_owner:{project_id}"
        with _IMAGE_FILM_ROOT_LOCK:
            root = _image_producer_root(scope)
            handoff = _image_operations_handoff(root, scope)
            jobs = handoff.producer_jobs()
            states = handoff.image_states()
            _collector, reader = handoff.result_services()
        job = jobs.get_job(ref)
        states.require_owned(((job.get("request_snapshot") or {}).get("context_basis") or {}).get("context_id"))
        digest = (job.get("artifact") or {}).get("artifact_sha256")
        materials = getattr(reader, "materials_root", None)
        path = Path(materials) / f"{digest}.png" if materials is not None and type(digest) is str else None
        return job, path

    return {
        "project_id": project_id,
        "video_job": lambda ref: _shoot_take(ref, project_id),  # lane p8-cut: the project's own takes (CUT-104)
        "music_file": _timeline_music_file,
        "image_job": image_job,
        "probe": lambda path: _timeline_module("resolver").ffprobe_facts(path, ffprobe=FFPROBE_PATH),
        # The artist's footage (file:<sha256>): projects/timeline_footage_1.py, loaded after this unit.
        "footage": _timeline_song_or_footage(_timeline_footage_lookup(project_id)),
    }


_TIMELINE_SONG_FACTS: dict[str, dict[str, Any]] = {}


def _timeline_song_or_footage(footage: Any) -> Any:
    """Lane w11-cut item 1 (M-99): the song is a sound clip file:<sha256> on the music-role track. A file of the
    project's folders resolves as footage; a song Music analysed (placed with place_music) resolves to the analysis
    cache's playback WAV of that very recording (named by its sha256), probed once per process."""

    def locate(ref: str) -> tuple[Path, dict[str, Any]]:
        try:
            return footage(ref)
        except Exception:
            proxy = _timeline_music_file(ref[5:]) if ref.startswith("file:") else None
            if proxy is None:
                raise
            if ref not in _TIMELINE_SONG_FACTS:
                facts = _timeline_module("footage").probe_audio(proxy, ffprobe=FFPROBE_PATH)
                _TIMELINE_SONG_FACTS[ref] = {**facts, "name": "Song"}
            return proxy, _TIMELINE_SONG_FACTS[ref]

    return locate


def _timeline_music_file(track_sha256: str) -> Path | None:
    """The pinned song's playback file: the Music analysis cache's WAV proxy, named by the
    recording's sha256 (routes/music/cache_1.py). The engine itself is not loaded."""
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    proxy = state_root(Path(base) / "hermes") / "analysis" / f"{track_sha256}.wav"
    return proxy if proxy.is_file() and proxy.stat().st_size >= 44 else None


def _timeline_music_refs(state: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for timeline in state.get("timelines") or ():
        for track in timeline.get("tracks") or ():
            music = track.get("music") if track.get("kind") == "music" else None
            sha = music.get("track_sha256") if isinstance(music, dict) else None
            if isinstance(sha, str) and sha not in found:
                found.append(sha)
    return found


def _timeline_facts(state: dict[str, Any], project_id: str) -> dict[str, dict[str, Any]]:
    """Every source of every timeline (clips, then music lanes), resolved once, with private paths."""
    document, resolver = _timeline_module("document"), _timeline_module("resolver")
    lookups = _timeline_lookups(project_id)
    music_file = lookups.pop("music_file", None)
    facts = resolver.resolve_refs(document.refs_of(state), **lookups)
    for sha in _timeline_music_refs(state):
        facts.setdefault(sha, resolver.resolve_music(sha, music_file=music_file))
    return facts


def _timeline_sources(state: dict[str, Any], project_id: str) -> list[dict[str, Any]]:
    resolver = _timeline_module("resolver")
    return [resolver.public(fact) for fact in _timeline_facts(state, project_id).values()]


def _timeline_sound_info(fact: dict[str, Any], rate: dict[str, int], index: int) -> tuple[dict[str, Any], dict[str, int]]:
    """(media_info, source_rate) of a sound clip from a resolved source: a sound file is read at the
    timeline rate (whole frames of its samples); a take or a video with sound at its own rate."""
    audio = fact.get("audio")
    if audio:
        frames = _timeline_module("footage").audio_frames(audio, rate)
        return {"frames": frames, "sample_rate": audio.get("sample_rate"), "channels": audio.get("channels")}, dict(rate)
    info = fact.get("media_info") or {}
    if not info.get("frames") or not info.get("has_audio") or fact.get("source_rate") is None:
        error = studio_core.ProjectError("no_sound", f"ops[{index}]: {fact['ref']} has no sound")
        error.op_index = index
        raise error
    return {"frames": info["frames"], "sample_rate": None, "channels": None}, fact["source_rate"]


def _timeline_rate_of(state: dict[str, Any] | None, op: dict[str, Any]) -> dict[str, int]:
    wanted = op.get("timeline", (state or {}).get("active"))
    for timeline in (state or {}).get("timelines") or ():
        if timeline.get("id") == wanted:
            return timeline["rate"]
    return {"num": 24, "den": 1}


def _timeline_enrich(ops: list[dict[str, Any]], project_id: str, state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """A clip or swap without media_info gets it from the resolver; offline refuses."""
    resolver = _timeline_module("resolver")
    lookups = _timeline_lookups(project_id)
    lookups.pop("music_file", None)
    out = []
    for index, op in enumerate(ops):
        op = copy.deepcopy(op)
        if op.get("op") == "auto_duck" and "speech" not in op and state is not None:
            # The cut as it stands at this op (lane fs-duck, projects/timeline_duck_1.py).
            current = _timeline_module("ops").apply_ops(state, out)[0] if out else state
            out.append(_timeline_duck_speech(op, index, project_id, current))
            continue
        sound = op.get("clip") if op.get("op") == "place_audio" else None
        if isinstance(sound, dict) and "media_info" not in sound:
            ref = (sound.get("source") or {}).get("ref")
            fact = resolver.resolve_ref(ref, **lookups)
            if fact["status"] != "online":
                error = studio_core.ProjectError("source_offline", f"ops[{index}]: {ref} is offline ({fact['reason']})")
                error.op_index = index
                raise error
            info, source_rate = _timeline_sound_info(fact, _timeline_rate_of(state, op), index)
            sound["media_info"] = info
            sound.setdefault("source_rate", source_rate)
            if not sound.get("label") and isinstance(fact.get("footage"), dict):
                sound["label"] = fact["footage"].get("name", "")
            out.append(op)
            continue
        target = op.get("clip") if op.get("op") in {"append", "insert", "overwrite"} else op.get("take") if op.get("op") == "place_with_parent" else op if op.get("op") == "swap_take" else None
        if isinstance(target, dict) and "media_info" not in target and (target.get("source") or {}).get("kind") == "cut" and state is not None:
            # Lane w23-bins (CUT-180): a nested cut's facts come from the document as it stands at this op.
            current = _timeline_module("ops").apply_ops(state, out)[0] if out else state
            outer = next((t for t in current["timelines"] if t["id"] == op.get("timeline", current["active"])), None)
            try:
                info, source_rate = _timeline_module("nest").media_info(current, target["source"].get("ref"), outer)
            except ValueError as exc:
                exc.op_index = index
                raise
            target["media_info"] = info
            target.setdefault("source_rate", source_rate)
            out.append(op)
            continue
        if isinstance(target, dict) and "media_info" not in target:
            ref = target.get("ref") if op.get("op") == "swap_take" else (target.get("source") or {}).get("ref")
            fact = resolver.resolve_ref(ref, **lookups)
            if fact["status"] != "online":
                error = studio_core.ProjectError("source_offline", f"ops[{index}]: {ref} is offline ({fact['reason']})")
                error.op_index = index
                raise error
            target["media_info"] = fact["media_info"]
            if fact["source_rate"] is not None and "source_rate" not in target:
                target["source_rate"] = fact["source_rate"]
            if op.get("op") == "swap_take":
                target.setdefault("kind", fact["kind"])
        out.append(op)
    return out


def _timeline_current(store: Any, directory: str, project_id: str) -> tuple[int, dict[str, Any]]:
    current = store.read_timeline(directory, project_id=project_id)
    if not current["exists"]:
        return 0, _timeline_module("schema").default_state()
    return current["revision"], current["state"]


def _timeline_shot_link(state: Any, store: Any, directory: str, project_id: str) -> Any:
    """Lane p7-cut item 3 (VID-099): take clips of the project's Video shot carry its slot
    (timeline/ops_shot.py). The shot's jobs are read only when a take clip lacks the link."""
    shot = _timeline_module("ops_shot")
    if not shot.wants_link(state):
        return state
    try:
        jobs = store.read_shoot_state(directory, project_id=project_id)["state"].get("jobs")
    except (studio_core.ProjectError, ValueError, OSError, KeyError, TypeError):
        return state
    return shot.link_takes(state, shot.shot_takes(jobs))


@project_router.post("/projects/timeline/read")
async def projects_timeline_read(request: TimelineReadRequest) -> dict[str, Any]:
    try:
        directory = _project_reference_directory(request.project_ref)
        project_id = request.project_ref.project_id
        store = _project_store_factory()
        result = await asyncio.to_thread(store.read_timeline, directory, project_id=project_id)
        if result["exists"]:
            result["state"] = await asyncio.to_thread(_timeline_shot_link, result["state"], store, directory, project_id)
        if request.detail == "summary":
            return _timeline_module("document").summary_of(result)
        if request.detail == "cuts":
            state = result["state"]
            return {"detail": "cuts", "exists": result["exists"], "revision": result["revision"],
                    "active": state["active"] if state else None,
                    "cuts": _timeline_module("nest").cuts_list(state) if state else []}
        if request.resolve and result["exists"]:
            result["sources"] = await asyncio.to_thread(_timeline_sources, result["state"], project_id)
        ducking = _timeline_ducking_state(result.get("state"), project_id) if result["exists"] else None
        if ducking is not None:  # lane p12-cutb: each music track's ducking switch + the last edit's notice
            result["ducking"] = ducking
        return result
    except (studio_core.ProjectError, ValueError) as exc:
        return _timeline_error(exc)


@project_router.post("/projects/timeline/write")
async def projects_timeline_write(request: TimelineWriteRequest) -> dict[str, Any]:
    try:
        directory = _project_reference_directory(request.project_ref)
        project_id = request.project_ref.project_id
        store = _project_store_factory()
        _revision, before = await asyncio.to_thread(_timeline_current, store, directory, project_id)
        by = _caller_by(request.by)  # lane w34-sec (finding 18): a Hermes call is Hermes' revision
        written = await asyncio.to_thread(
            store.write_timeline, directory, project_id=project_id, expected_revision=request.expected_revision,
            state=request.state, label=request.label, by=by, reason=request.reason,
        )
        film_events.timeline_changed(project_id, written["revision"], by)
        summary = _timeline_module("document").summarize(before, written["state"])
        return {"revision": written["revision"], "updated_at": written["updated_at"], **summary}
    except (studio_core.ProjectError, ValueError) as exc:
        return _timeline_error(exc)


@project_router.post("/projects/timeline/apply")
async def projects_timeline_apply(request: TimelineApplyRequest) -> dict[str, Any]:
    try:
        directory = _project_reference_directory(request.project_ref)
        project_id = request.project_ref.project_id
        store = _project_store_factory()
        document = _timeline_module("document")
        digest = document.ops_sha256(request.ops) if request.request_id is not None else None
        if request.request_id is not None and not request.dry_run:
            current = await asyncio.to_thread(store.read_timeline, directory, project_id=project_id, request_id=request.request_id)
            kept = current["request"]
            if kept is not None:
                if kept["ops_sha256"] != digest:
                    raise studio_core.ProjectError("request_id_reused", "this request_id already applied other ops")
                return {**kept["answer"], "replayed": True}
        revision, before = await asyncio.to_thread(_timeline_current, store, directory, project_id)
        if request.expected_revision != revision:
            raise studio_core.ProjectError("timeline_conflict", f"the timeline is at revision {revision}")
        before = await asyncio.to_thread(_timeline_shot_link, before, store, directory, project_id)
        ops = await asyncio.to_thread(_timeline_enrich, request.ops, project_id, before)
        after, results = _timeline_module("ops").apply_ops(before, ops)
        after = await asyncio.to_thread(_timeline_shot_link, after, store, directory, project_id)
        # Lane p12-cutb (CUT-043): the music ducks under the edited dialogue in this same revision (one Undo).
        ducking = await asyncio.to_thread(_timeline_reduck, before, after, ops, project_id)
        summary = _timeline_module("document").summarize(before, after)
        by = _caller_by(request.by)  # lane w34-sec (finding 18): a Hermes call is Hermes' revision, labelled so
        # Lane w39-words: a label the caller passes is kept as written; without one, the editor's phrases of the ops
        # (timeline/words.py: "Hermes: Fit to fill, Freeze frame, Detach sound +3"), whole phrases only.
        label = request.label or _timeline_module("words").label_for(ops, by)
        extra = {"ducking": ducking} if ducking is not None else {}
        if request.dry_run:
            return {"dry_run": True, "revision": revision, "label": label, "results": results, **summary, **extra}
        answer = {"dry_run": False, "revision": None, "label": label, "results": results, **summary, **extra}
        remember = {"request_id": request.request_id, "ops_sha256": digest, "answer": answer} if request.request_id is not None else None
        written = await asyncio.to_thread(
            store.write_timeline, directory, project_id=project_id, expected_revision=revision,
            state=after, label=label, by=by, reason=request.reason, request=remember,
        )
        if remember is not None:
            if "revision" not in written:  # the first try landed while this retry was on its way
                return {**written["request"]["answer"], "replayed": True}
            film_events.timeline_changed(project_id, written["revision"], by)
            return {**written["request"]["answer"], "replayed": False}
        film_events.timeline_changed(project_id, written["revision"], by)
        return {**answer, "revision": written["revision"]}
    except (studio_core.ProjectError, ValueError) as exc:
        return _timeline_error(exc)


@project_router.post("/projects/timeline/revisions")
async def projects_timeline_revisions(request: TimelineRevisionsRequest) -> dict[str, Any]:
    try:
        directory = _project_reference_directory(request.project_ref)
        store = _project_store_factory()
        if request.revision is not None:
            return await asyncio.to_thread(
                store.read_timeline_revision, directory, project_id=request.project_ref.project_id, revision=request.revision,
            )
        return await asyncio.to_thread(store.timeline_revisions, directory, project_id=request.project_ref.project_id)
    except (studio_core.ProjectError, ValueError) as exc:
        return _timeline_error(exc)


@project_router.post("/projects/timeline/refs")
async def projects_timeline_refs(request: TimelineRefsRequest) -> dict[str, Any]:
    try:
        directory = _project_reference_directory(request.project_ref)
        project_id = request.project_ref.project_id
        revision, state = await asyncio.to_thread(_timeline_current, _project_store_factory(), directory, project_id)
        refs = list(_timeline_module("document").refs_of(state).values())
        result: dict[str, Any] = {"revision": revision, "refs": refs}
        if request.resolve:
            result["sources"] = await asyncio.to_thread(_timeline_sources, state, project_id)
        return result
    except (studio_core.ProjectError, ValueError) as exc:
        return _timeline_error(exc)
