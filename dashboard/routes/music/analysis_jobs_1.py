from __future__ import annotations

# fs-inout: song analysis only on trigger (owner design E18). Opening a song reads only what is cheap and stored:
# its length, waveform and playback proxy, and an earlier analysis of the same file content (by SHA-256) when there
# is one. Beats / meter / bar 1 / sections run only on "Analyse" (start / status / stop, the same for the agent),
# with the engine the artist picks (Beat This! by default when installed, librosa as the basic fallback).
# The result is cached per file content hash by music_analysis.analyze_cached and reused on every later open.

_MUSIC_JOBS: dict[str, dict[str, Any]] = {}
_MUSIC_JOBS_LOCK = _thread.allocate_lock()
_MUSIC_JOBS_KEEP = 32


class MusicOpenRequest(BaseModel):
    path: str
    approved_roots: list[str] = Field(default_factory=list)
    project_ref: ProjectReference | None = None
    # Lane p13b-aud item 2: the engine named in the engine select; its stored result only (None = the default engine's,
    # else the other stored one).
    engine: str | None = Field(default=None, pattern=r"^(beat_this|librosa)$")


class MusicAnalyseStartRequest(BaseModel):
    # Lane w36-audio item 1: a song on a Cut music lane is named by its content id (file:<sha256>, no path); with
    # project_ref the file is the project's own file of that content (the footage index), analysed once per file.
    path: str | None = Field(default=None, min_length=1)
    track_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    approved_roots: list[str] = Field(default_factory=list)
    project_ref: ProjectReference | None = None
    engine: str | None = Field(default=None, pattern=r"^(beat_this|librosa)$")


class MusicAnalyseJobRequest(BaseModel):
    job_id: str = Field(pattern=r"^man_[0-9a-f]{32}$")


def _music_proxy_peaks(proxy: Path) -> tuple[list[list[float]], float]:
    """Waveform peaks (MUSIC_PEAKS_PER_SECOND buckets, mono min/max) and the exact length of the playback proxy.
    Lane p12-aud (AUD-059): the waveform never depends on the analysis engine's packages; without numpy the
    buckets are read with the standard library (min/max over the interleaved channels)."""
    import wave

    with wave.open(str(proxy), "rb") as handle:
        rate, channels, frames = handle.getframerate(), handle.getnchannels(), handle.getnframes()
        raw = handle.readframes(frames)
    duration = frames / float(rate) if rate else 0.0
    size = max(1, int(round(rate / MUSIC_PEAKS_PER_SECOND)))
    try:
        import numpy as np
    except ImportError:
        np = None
    peaks: list[list[float]] = []
    if np is None:
        import array
        import sys as _sys

        data = array.array("h")
        data.frombytes(raw[: (len(raw) // 2) * 2])
        if _sys.byteorder == "big":
            data.byteswap()
        step = size * max(1, channels)
        for start in range(0, len(data), step):
            chunk = data[start : start + step]
            if chunk:
                peaks.append([round(min(chunk) / 32768.0, 4), round(max(chunk) / 32768.0, 4)])
        return peaks, duration
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        data = data[: (data.size // channels) * channels].reshape(-1, channels).mean(axis=1)
    count = data.size // size
    if count:
        body = data[: count * size].reshape(count, size)
        peaks = [[round(float(lo), 4), round(float(hi), 4)] for lo, hi in zip(body.min(axis=1), body.max(axis=1))]
    rest_ = data[count * size :]
    if rest_.size:
        peaks.append([round(float(rest_.min()), 4), round(float(rest_.max()), 4)])
    return peaks, duration


def _music_open_paths(source: Path) -> tuple[str, Path, Path]:
    """(sha256, cache file, proxy file) of a song; without the analysis engine by hashlib and the same cache folder."""
    if music_analysis is not None:
        return _music_cache_paths(source)
    import hashlib

    digest = hashlib.sha256()
    with open(source, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    directory = Path(state_root(Path(_local_data.records_base()) / "hermes")) / "analysis"
    directory.mkdir(parents=True, exist_ok=True)
    hexdigest = digest.hexdigest()
    return hexdigest, directory / f"{hexdigest}.json", directory / f"{hexdigest}.wav"


def _music_open_sync(path: str, approved_roots: list[str], engine_error: str | None = None, engine: str | None = None, project_ref: Any = None) -> dict[str, Any]:
    source = _confined_music_path(path, approved_roots)
    digest, cache_file, proxy_file = _music_open_paths(source)
    proxy = _ensure_music_proxy(source, proxy_file)
    peaks, duration = _music_proxy_peaks(proxy)
    analysis = None
    shown = None
    stored_engines: list[str] = []
    if not engine_error and music_analysis is not None:
        # Lane p13b-aud item 2 (maintainer decision): the shown analysis belongs to the engine named in the select.
        # Without a named engine the default engine's stored result is shown, else the other stored one; a named
        # engine without a stored result opens time only (Analyse), never with the other engine's numbers.
        by_engine = {name: music_analysis.stored_engine_analysis(source, name, digest) for name in music_analysis.ENGINE_CHOICES}
        stored_engines = [name for name in music_analysis.ENGINE_CHOICES if by_engine[name] is not None]
        default = music_analysis.default_engine()
        order = [engine] if engine else [default] + [name for name in music_analysis.ENGINE_CHOICES if name != default]
        shown = next((name for name in order if by_engine.get(name) is not None), None)
        stored = by_engine[shown] if shown else None
        analysis = _music_metadata(stored, proxy) if stored is not None else None
    out = {
        "ok": True,
        "track_sha256": digest,
        "duration": round(duration, 6),
        "proxy_path": str(proxy),
        "peaks": peaks,
        "buckets_per_sec": MUSIC_PEAKS_PER_SECOND,
        "analysed": analysis is not None,
        "analysis": analysis,
        "engines": music_analysis.engine_choices() if not engine_error and music_analysis is not None else [],
        "default_engine": music_analysis.default_engine() if not engine_error and music_analysis is not None else None,
        "engine": shown or engine,
        "stored_engines": stored_engines,
    }
    if analysis is not None:
        # Lane w25-audio2 item 5 (HER-088): the ids the Audio page's map read gets (evidence import + map resolve), so
        # Hermes reaches continuo_music_map / continuo_use_in_cut by tools alone; null when the map cannot be read.
        out.update(_music_song_map_ids(digest, shown, project_ref))
    if engine_error:
        # Lane p12-aud (AUD-003/059, charter owner decision 10): the song opens; the missing engine is a fact for Analyse.
        out["analysis_error"] = engine_error
    return out


def _music_song_map_ids(track_sha256: str, engine: str | None = None, project_ref: Any = None) -> dict[str, Any]:
    """Lane w25-audio2 item 5 (HER-088): what the Audio page does before Use in Cut (use-shoot-music-map.js): import the
    stored analysis as evidence, resolve the map with the project's interpretation. {evidence_id, map_id} or Nones."""
    try:
        evidence, waveform = _music_import_v6_from_cache(track_sha256, engine)
        _root, evidence_store, _scratch, _boundary = _music_store_context()
        evidence_id, _waveform_id = evidence_store.publish_pair(evidence, waveform)
        interpretation_result = _music_read_interpretation(track_sha256, project_ref)
        interpretation = interpretation_result.get("interpretation") if interpretation_result.get("exists") else None
        resolved = _music_core_module("resolver").resolve_music_map(evidence_store.read_evidence(evidence_id), interpretation)
        map_id = resolved.get("map_id") if isinstance(resolved, dict) else None
        return {"evidence_id": evidence_id, "map_id": map_id if isinstance(map_id, str) else None}
    except Exception:  # noqa: BLE001 - no map is a fact (null ids), never a failed open
        return {"evidence_id": None, "map_id": None}


@router.post("/music/open")
async def music_open(request: MusicOpenRequest) -> dict[str, Any]:
    try:
        engine_error = None
        try:
            _load_music_analysis()
        except Exception as exc:  # noqa: BLE001 - a missing engine never blocks opening the song
            engine_error = _music_error(exc)
        roots = _request_roots(request.approved_roots, request.project_ref, path=request.path, server_list=True)
        return await asyncio.to_thread(_music_open_sync, request.path, roots, engine_error, request.engine, request.project_ref)
    except _FolderNotApproved as exc:
        return _folder_refusal(exc)
    except Exception as exc:  # noqa: BLE001 - route boundary translates every engine failure
        return _error(_music_error(exc))


def _music_job_public(job: dict[str, Any]) -> dict[str, Any]:
    out = {
        "ok": True,
        "job_id": job["job_id"],
        "track_sha256": job["track_sha256"],
        "engine": job["engine"],
        "status": job["status"],
        "progress": round(float(job["progress"]), 3),
        "stage": job["stage"],
    }
    if job["status"] == "done":
        out["analysis"] = job["result"]
    if job["status"] == "error":
        out["error"] = job["error"]
    return out


def _music_job_run(job: dict[str, Any], source: Path) -> None:
    def progress(fraction: float, stage: str) -> None:
        if job["stop"]:
            raise music_analysis.AnalysisStopped()
        job["progress"], job["stage"] = max(job["progress"], min(0.99, float(fraction))), stage

    digest, cache_file, proxy_file = _music_cache_paths(source)
    try:
        evict_analysis_cache(protected={digest})
        analysis = music_analysis.analyze_cached(source, job["engine"], progress)
        if job["stop"]:
            raise music_analysis.AnalysisStopped()
        proxy = _ensure_music_proxy(source, proxy_file)
        job["result"] = _music_metadata(analysis, proxy)
        job["status"], job["progress"], job["stage"] = "done", 1.0, "done"
    except music_analysis.AnalysisStopped:
        job["status"], job["stage"] = "stopped", "stopped"
    except Exception as exc:  # noqa: BLE001 - the job reports the translated failure
        job["status"], job["stage"], job["error"] = "error", "error", _music_error(exc)


# Lane w36-audio item 1: the songs a Cut started by content id (sha256 -> its job). One start per file in this process:
# a running, finished or failed analysis answers again instead of starting another (a failure is a fact, never a loop).
_MUSIC_SONG_STARTS: dict[str, dict[str, Any]] = {}


def _music_song_file(track_sha256: str, project_ref: Any) -> str:
    """The project's own file of this content (the Cut's footage index: confined to the project's folders, relinked by
    content after a move); refused by name when the project has no such file."""
    if project_ref is None:
        raise RecipeError("A song named by its content needs its project.")
    try:
        path, _facts = _timeline_footage_lookup(project_ref.project_id)("file:" + track_sha256)
    except Exception:  # noqa: BLE001 - no file of this content in the project's folders
        raise RecipeError("That track could not be found.") from None
    return str(path)


def _music_analyse_start_sync(request: MusicAnalyseStartRequest) -> dict[str, Any]:
    by_content = request.path is None
    if by_content:
        if request.track_sha256 is None:
            raise RecipeError("A path or a track_sha256 is required.")
        with _MUSIC_JOBS_LOCK:
            known = _MUSIC_SONG_STARTS.get(request.track_sha256)
            if known is not None and known["status"] in ("running", "done", "error"):
                return _music_job_public(known)
        request = request.model_copy(update={"path": _music_song_file(request.track_sha256, request.project_ref)})
    source = _confined_music_path(request.path, _request_roots(request.approved_roots, request.project_ref, path=request.path, server_list=True))
    digest = music_analysis._sha256_file(source)
    if by_content and digest != request.track_sha256:
        raise RecipeError("That track could not be found.")
    engine = request.engine or music_analysis.default_engine()
    with _MUSIC_JOBS_LOCK:
        known = _MUSIC_SONG_STARTS.get(digest) if by_content else None
        if known is not None and known["status"] in ("running", "done", "error"):
            return _music_job_public(known)
        for job in _MUSIC_JOBS.values():
            if job["track_sha256"] == digest and job["engine"] == engine and job["status"] == "running":
                if by_content:
                    _MUSIC_SONG_STARTS[digest] = job
                return _music_job_public(job)
        job = {"job_id": f"man_{uuid.uuid4().hex}", "track_sha256": digest, "engine": engine, "status": "running",
               "progress": 0.0, "stage": "starting", "stop": False, "result": None, "error": None, "started": time.time(), "by": _film_caller()}
        _MUSIC_JOBS[job["job_id"]] = job
        if by_content:
            _MUSIC_SONG_STARTS[digest] = job
        for old in sorted(_MUSIC_JOBS.values(), key=lambda row: row["started"])[:-_MUSIC_JOBS_KEEP]:
            if old["status"] != "running":
                _MUSIC_JOBS.pop(old["job_id"], None)
    _thread.start_new_thread(_music_job_run, (job, source))
    return _music_job_public(job)


@router.post("/music/analyse/start")
async def music_analyse_start(request: MusicAnalyseStartRequest) -> dict[str, Any]:
    try:
        _load_music_analysis()
        return await asyncio.to_thread(_music_analyse_start_sync, request)
    except _FolderNotApproved as exc:
        return _folder_refusal(exc)
    except Exception as exc:  # noqa: BLE001 - route boundary translates every engine failure
        return _error(_music_error(exc))


@router.post("/music/analyse/status")
async def music_analyse_status(request: MusicAnalyseJobRequest) -> dict[str, Any]:
    job = _MUSIC_JOBS.get(request.job_id)
    if job is None:
        return {"ok": False, "reason_code": "music_analysis_job_unknown", "message": "No such analysis (it ended long ago or the backend restarted)."}
    return _music_job_public(job)


@router.post("/music/analyse/stop")
async def music_analyse_stop(request: MusicAnalyseJobRequest) -> dict[str, Any]:
    job = _MUSIC_JOBS.get(request.job_id)
    if job is None:
        return {"ok": False, "reason_code": "music_analysis_job_unknown", "message": "No such analysis (it ended long ago or the backend restarted)."}
    if job["status"] == "running":
        job["stop"] = True
    return _music_job_public(job)
