from __future__ import annotations


# --- Sample film (lane w21-pkg item 4, BCK-212): a judge's first success without any model ------------------------
#
# Reference: DaVinci Resolve and Final Cut Pro offer their sample projects from the start screen and open a COPY, so
# the shipped original never changes. POST /projects/sample/open copies dashboard/samples/sample_film (made by
# catalog/make-sample-film.py: an own three-scene Fountain script and synthesised music, CC0; six pictures AI-generated
# with Krea 2 Turbo and their 4 s clips) into
# a new folder <parent>/Sample film[ N] (parent: the request's, else <home>/Documents/Continuo), creates the project
# there with that folder approved, then builds it with the same route functions the page and Hermes use: script import,
# footage add + one timeline apply (the six clips on V1, one undo step; Cut's viewer streams folder clips, not stills), audio add with place (the pad on the music
# track). Answers the project like /projects/create (path-free) plus what was built.

# Route areas run in plugin_api's namespace: __file__ is dashboard/plugin_api.py.
_SAMPLE_DIR = Path(__file__).resolve().parent / "samples" / "sample_film"
_SAMPLE_COPY_MAX = 99


class ProjectSampleOpenRequest(_ProjectRequest):
    parent: str | None = Field(default=None, min_length=1, max_length=32767)


def _sample_error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "retryable": False, "next": None}}


def _sample_manifest() -> dict[str, Any]:
    return json.loads((_SAMPLE_DIR / "sample.json").read_text(encoding="utf-8"))


def _sample_target(parent: str | None) -> Path:
    """The next free <parent>/Sample film, Sample film 2, ...; never an existing folder."""
    base = Path(parent) if parent else Path.home() / "Documents" / "Continuo"
    name = _sample_manifest()["name"]
    for n in range(1, _SAMPLE_COPY_MAX + 1):
        candidate = base / (name if n == 1 else f"{name} {n}")
        if not candidate.exists():
            return candidate
    raise studio_core.ProjectError("project_not_empty", "every sample folder name is taken")


# w22-dlv (BCK-213): the sample needs ffmpeg and ffprobe (footage probe, Cut's streams, Deliver). Like Resolve and
# Premiere naming a missing component before they use it, both are checked before any folder or project exists; the
# answer names the tool and its fix (the FFMPEG_PATH setting, the official download page).
_FFMPEG_FIX = {"setting": "FFMPEG_PATH", "download": "https://ffmpeg.org/download.html"}


def _tool_runs(path: Path) -> bool:
    try:
        return subprocess.run([str(path), "-version"], capture_output=True, timeout=15, check=False, env=_child_env()).returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def _missing_media_tools() -> list[str]:
    return [name for name, path in (("ffmpeg", FFMPEG_PATH), ("ffprobe", FFPROBE_PATH)) if not _tool_runs(Path(path))]


def _sample_tools_error(missing: list[str]) -> dict[str, Any]:
    return {"error": {"code": "ffmpeg_missing", "message": " and ".join(missing) + " not found", "missing": missing,
                      "fix": dict(_FFMPEG_FIX), "retryable": True, "next": None}}


# w32-rel (BCK-224, review R2; an installer's rollback removes the files it wrote and leaves the rest): the folder is
# visible while the build runs, so the artist can put a file into it. Rollback removes only what the build made: a
# sample copy still byte-equal to the shipped file, and the project's own documents (project.json, script/, state/ by
# their store names) written since the build started. Anything else stays, and so does the folder that holds it.
_SAMPLE_OWN_NAMES = re.compile(r"^(\.continuo-building|project\.json|script/script\.(fountain|json)|state/[a-z0-9][a-z0-9-]*\.json)(\.r\d+)?$"
                               r"|(^|/)\.studio-[0-9a-f]{32}\.tmp$")


def _sample_owned(target: Path, path: Path, since_ns: int) -> bool:
    rel = path.relative_to(target).as_posix()
    try:
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_mtime_ns < since_ns:
            return False
        if _SAMPLE_OWN_NAMES.search(rel):  # search: the .studio-*.tmp alternative also matches nested (state/...)
            return True
        source = _SAMPLE_DIR / rel
        return (rel != "sample.json" and source.is_file() and source.stat().st_size == info.st_size
                and source.read_bytes() == path.read_bytes())
    except (OSError, ValueError):
        return False


def _sample_rollback(target: Path, project_id: str | None, since_ns: int = 0) -> bool:
    """Undo a sample build that failed after its folder was made: forget its Recent entry and remove what the build
    made. Only the folder THIS request created (it did not exist before: _sample_target never returns an existing one).
    Answers True when the folder was kept because it holds files the sample did not make (or that could not go)."""
    if project_id:
        try:
            store = _recent_project_store_factory()
            with store._writer_lock():
                entries, _snapshot = store._read_entries()
                if entries is not None and any(e["project_id"] == project_id for e in entries):
                    store._write_registry([e for e in entries if e["project_id"] != project_id])
        except (studio_core.ProjectError, OSError, ValueError, TypeError, UnicodeError):
            # A retained Recent entry must not point at a project we deleted.
            # Preserve the entire folder when its removal cannot be confirmed.
            return True
    try:
        entries = sorted(target.rglob("*"), key=lambda p: len(p.parts), reverse=True)
    except OSError:
        return target.exists()
    marker = target / ".continuo-building"
    for path in entries:
        if path == marker:
            continue  # removed last: a folder that keeps any leftover keeps its marker, so a later create repairs it
        try:
            if path.is_dir() and not path.is_symlink():
                path.rmdir()  # only an empty folder; a folder with a kept file stays
            elif _sample_owned(target, path, since_ns):
                path.unlink()
        except OSError:
            pass
    try:
        if [p.name for p in target.iterdir()] == [marker.name]:
            marker.unlink()
    except OSError:
        pass
    try:
        target.rmdir()
    except OSError:
        pass
    return target.exists()


def _sample_kept_answer(answer: dict[str, Any], target: Path) -> dict[str, Any]:
    """A failed build's answer, saying that the folder was kept and why (its name only, never the path)."""
    error = dict(answer.get("error") or {}) if isinstance(answer, dict) and isinstance(answer.get("error"), dict) else {
        "code": "sample_unavailable", "message": "The sample film could not be built.", "retryable": False, "next": None}
    error["message"] = (f"{error.get('message') or 'The sample film could not be built.'} The folder {target.name} was kept:"
                        " it holds files the sample did not make, or Recent could not be updated safely.")
    error["kept_folder"] = target.name
    return {**(answer if isinstance(answer, dict) else {}), "error": error}


def _sample_copy(target: Path, manifest: dict[str, Any], keys: tuple[str, ...] = ("clip",), music: bool = True) -> list[str]:
    """Copy the sample's media files into the empty project (never sample.json, the script or __init__.py).
    w23-first (BCK-216): the clips and the pad only; a shot picture becomes an owned picture of the project
    (_sample_pictures) and is copied only where that import is refused, so Media lists each picture once."""
    copied: list[str] = []
    for rel in [shot[key] for shot in manifest["shots"] for key in keys] + ([manifest["music"]] if music else []):
        source = _SAMPLE_DIR / rel
        destination = target / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        copied.append(str(destination))
    return copied


def _sample_repair_interrupted(parent: str | None) -> None:
    store = _recent_project_store_factory()
    entries, _snapshot = store._read_entries()
    registered = {str(Path(e['directory']).resolve()): e['project_id'] for e in entries or []
                  if isinstance(e.get('directory'), str) and isinstance(e.get('project_id'), str)}
    candidates = {Path(e['directory']) for e in entries or [] if isinstance(e.get('directory'), str)}
    base = Path(parent) if parent else Path.home() / 'Documents' / 'Continuo'
    try:
        candidates.update(p for p in base.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        pass
    for target in candidates:
        if not re.fullmatch(r"Sample film(?: \d+)?", target.name) or target.is_symlink():
            continue
        try:
            marker = json.loads((target / '.continuo-building').read_text(encoding='utf-8'))
            if not isinstance(marker, dict) or marker.get('sample') is not True:
                continue
            project_id = marker.get('project_id')
            recent_id = registered.get(str(target.resolve()))
            if recent_id and project_id is not None and project_id != recent_id:
                continue
            if project_id is not None and not isinstance(project_id, str):
                continue
            _sample_rollback(target, recent_id or project_id, since_ns=0)
        except (OSError, ValueError, TypeError, UnicodeError):
            continue


def _sample_write_marker(target: Path, project_id: str | None = None) -> None:
    data = {'sample': True, 'started_ns': time.time_ns()}
    if project_id is not None:
        data['project_id'] = project_id
    temp = target / ('.studio-' + uuid.uuid4().hex + '.tmp')
    temp.write_text(json.dumps(data), encoding='utf-8')
    temp.replace(target / '.continuo-building')


@project_router.post("/projects/sample/open")
async def projects_sample_open(request: ProjectSampleOpenRequest) -> dict[str, Any]:
    try:
        manifest = _sample_manifest()
    except (OSError, ValueError):
        return _sample_error("sample_unavailable", "The sample film is not part of this install.")
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError  # noqa: F401 - required by image import
    except (ImportError, OSError):
        return {"error": {"code": "dependencies_preparing", "message": "Continuo's picture import is still being set up", "retryable": True, "next": None}}
    missing = await asyncio.to_thread(_missing_media_tools)
    if missing:
        return _sample_tools_error(missing)
    since_ns = time.time_ns() - 2_000_000_000  # file times can lag the clock by up to the FAT/SMB granularity
    try:
        await asyncio.to_thread(_sample_repair_interrupted, request.parent)
        target = await asyncio.to_thread(_sample_target, request.parent)
        await asyncio.to_thread(target.mkdir, parents=True)
        await asyncio.to_thread(_sample_write_marker, target)
    except studio_core.ProjectError as exc:
        return _project_error_response(exc)
    except OSError:
        return _sample_error("sample_folder_unavailable", "The sample folder could not be made.")
    holder: dict[str, str] = {}
    # w23-first (BCK-216; Resolve / Final Cut open their sample as a finished project): the sample's own content is
    # the project's baseline, never a Hermes proposal. The route picks every path and value itself (nothing the caller
    # sends reaches a write but the parent folder), so its build writes as the project's own (by "artist"): no script
    # review, no Cut "Hermes did" line, whether the Start tile or continuo_project_sample asked.
    caller = _FILM_CALLER.set("artist")
    try:
        built = await _sample_build(target, manifest, holder)
    except Exception:
        await asyncio.to_thread(_sample_rollback, target, holder.get("project_id"), since_ns)
        raise
    finally:
        _FILM_CALLER.reset(caller)
    if not isinstance(built, dict) or built.get("error") or not built.get("project"):
        # w22-dlv (BCK-213): a failed build leaves no half sample in Recent or on disk (the Start tile stays).
        # w32-rel (BCK-224): only what the build made goes; a folder with the artist's files stays and the answer says so.
        if await asyncio.to_thread(_sample_rollback, target, holder.get("project_id"), since_ns):
            return _sample_kept_answer(built, target)
    return built


async def _sample_build(target: Path, manifest: dict[str, Any], holder: dict[str, str]) -> dict[str, Any]:
    # w25-cutfix (PRJ-063; Resolve / Final Cut name a duplicated project after its copy): a sample copy takes its
    # folder's name ("Sample film 2"), so Recent never shows two samples under one name.
    created = await asyncio.to_thread(_projects_create_sync, ProjectCreateRequest(directory=str(target), name=target.name),
                                      allow_building_marker=True)
    if not isinstance(created, dict) or created.get("error") or not created.get("project"):
        return created
    project_id = created["project"]["project_id"]
    holder["project_id"] = project_id
    marker = target / ".continuo-building"
    await asyncio.to_thread(_sample_write_marker, target, project_id)
    ref = ProjectReference(project_id=project_id)
    by = "hermes" if _film_caller() == "hermes" else "artist"
    try:
        # The project's own folder is its media folder (stored relative, so the copy moves with it).
        store = _project_store_factory()
        current = await asyncio.to_thread(store.read_project, str(target), expected_project_id=project_id)
        await asyncio.to_thread(store.write_metadata, str(target), project_id=project_id,
                                expected_revision=current["revision"], name=current["name"],
                                approved_roots=[str(target)])
        await asyncio.to_thread(_sample_copy, target, manifest)  # clips + pad (w23-first: pictures are imported)
    except (studio_core.ProjectError, OSError, ValueError) as exc:
        return _project_error_response(exc) if isinstance(exc, studio_core.ProjectError) else _sample_error(
            "sample_folder_unavailable", "The sample media could not be copied.")
    script = await projects_script_import(ScriptImportRequest(
        project_ref=ref, expected_revision=0, format="fountain", by=by, name=manifest["script"],
        text=(_SAMPLE_DIR / manifest["script"]).read_text(encoding="utf-8")))
    if not isinstance(script, dict) or script.get("error"):
        return script
    footage = await projects_timeline_footage_add(TimelineFootageAddRequest(
        project_ref=ref, paths=[str(target / shot["clip"]) for shot in manifest["shots"]]))
    if not isinstance(footage, dict) or footage.get("error") or footage.get("errors"):
        return footage if isinstance(footage, dict) and footage.get("error") else _sample_error(
            "sample_media_unavailable", "The sample clips could not be read.")
    rate = manifest["rate"]
    ops: list[dict[str, Any]] = [{"op": "set_format", "width": manifest["format"]["width"],
                                  "height": manifest["format"]["height"], "rate": rate}]
    for shot, item in zip(manifest["shots"], footage["footage"]):
        ops.append({"op": "append", "clip": {"source": {"kind": "media", "ref": item["ref"]}, "label": shot["label"],
                                             "duration_frames": shot["duration_frames"], "audio": "off"}})
    cut = await projects_timeline_apply(TimelineApplyRequest(
        project_ref=ref, expected_revision=0, ops=ops, label="Sample film opened", by=by))
    if not isinstance(cut, dict) or cut.get("error"):
        return cut
    music = await projects_timeline_audio_add(TimelineAudioAddRequest(
        project_ref=ref, paths=[str(target / manifest["music"])],
        place=TimelineAudioPlace(expected_revision=cut["revision"], role="music", at_frame=0, by=by)))
    if not isinstance(music, dict) or music.get("error") or not music.get("placed"):
        return music if isinstance(music, dict) and music.get("error") else _sample_error(
            "sample_media_unavailable", "The sample sound could not be placed.")
    planned = await _sample_plan(ref, target, manifest)
    if planned.get("error"):
        return planned
    marker.unlink()
    return {**created, "sample": {"scenes": len(script.get("order") or []), "clips": len(manifest["shots"]),
                                  "music": 1, "timeline_revision": music["placed"].get("revision"), **planned}}


# w22-dlv (BCK-214): the sample shows the shared plan, not only a cut. Its script has one shot line per clip
# (WIDE ON THE PIER, ...); each shot gets its record (sample.json details) with the shot's drawn picture as the
# storyboard picture (an owned picture of the project, the same import the Image page uses), every scene is blocked in
# Previz (figures, set, a camera per shot: the same route as the Previz button), and the pad is the song Audio opens
# (the project's musicTrack). Every step is the route the page and Hermes use; the answer says what was seeded.
async def _sample_plan(ref: ProjectReference, target: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    script = await projects_script_read(ScriptReadRequest(project_ref=ref))
    shots = (script.get("derived") or {}).get("shots") or [] if isinstance(script, dict) else []
    if len(shots) != len(manifest["shots"]):
        return _sample_error("sample_unavailable", "The sample script and its shots do not match.")
    pictures = await _sample_pictures(ref, target, manifest)
    if len(pictures) != len(manifest["shots"]) or not all(pictures):
        return _sample_error("sample_unavailable", "The sample storyboard pictures could not be imported.")
    changes = [{"shot": row["id"], "details": dict(shot.get("details") or {}),
                **({"pictures": [{"item": pictures[n], "role": "storyboard"}]} if pictures[n] else {})}
               for n, (row, shot) in enumerate(zip(shots, manifest["shots"]))]
    plan = await projects_shot_plan_write(ShotPlanWriteRequest(project_ref=ref, expected_revision=0, changes=changes))
    if not plan.get("ok"):
        return _sample_error("sample_unavailable", "The sample shots could not be written.")
    # w39-p2-previz: every scene is blocked (set from the heading's place, figures posed by the action, a camera per
    # shot), so Previz and its strip are complete when a stranger opens the sample; scenes in script order.
    cameras = 0
    for scene in dict.fromkeys(row["scene"] for row in shots):
        blocked = await projects_previz_block(PrevizBlockRequest(project_ref=ref, scene=scene))
        if not blocked.get("ok"):
            return _sample_error("sample_unavailable", "The sample's scenes could not be blocked.")
        cameras += len(blocked.get("cameras_added") or [])
    # w23-first (BCK-216): blocking records a proposal (the Previz line with Undo); the sample's blocking is its
    # baseline, so that proposal is closed as accepted (kept as history, the state a newer change gives it).
    await asyncio.to_thread(_sample_accept_proposals, ref, str(target))
    store = _project_store_factory()
    current = await asyncio.to_thread(store.read_shoot_state, str(target), project_id=ref.project_id)
    state = {**current["state"], "musicTrack": str(target / manifest["music"])}
    await asyncio.to_thread(store.write_shoot_state, str(target), project_id=ref.project_id,
                            expected_revision=current["revision"], state=state)
    try:  # the first page open lands on Cut (see _projects_open_recent_landing); a marker that cannot be written only skips that
        await asyncio.to_thread((target / "state" / _SAMPLE_LANDING_FILE).write_text, '{"page": "timeline"}', encoding="utf-8")
    except OSError:
        pass
    return {"shots": len(changes), "storyboard": sum(1 for p in pictures if p), "previz_cameras": cameras}


async def _sample_pictures(ref: ProjectReference, target: Path, manifest: dict[str, Any]) -> list[str | None]:
    """The sample's drawn pictures as owned pictures of the project (None where the import is refused, e.g. while the
    picture decoder is not installed yet: the shot keeps its record and the Board shows the empty frame).
    w23-first (BCK-216): imported from the shipped bytes (the same import as a paste), never from a copy in the
    project folder: that copy was a second Media item of the same picture (12 tiles for 6 pictures). A refused
    picture is copied into the project folder instead, so the file is still there for the artist."""
    out: list[str | None] = []
    for n, shot in enumerate(manifest["shots"]):
        try:
            source = _SAMPLE_DIR / shot["picture"]
            answer = await image_producer_import(ImageImportRequest.model_validate({
                "schema": "studio.image-import-request/1", "owner_scope": f"film_owner:{ref.project_id}",
                "operation_id": "imgop_" + hashlib.sha256(f"{ref.project_id}|sample|{n}".encode()).hexdigest()[:32],
                "data_b64": base64.b64encode(source.read_bytes()).decode("ascii"), "file_name": source.name,
                "project_ref": {"project_id": ref.project_id}}))
        except Exception:  # noqa: BLE001 - a picture never fails the sample
            answer = None
        job = answer.get("image_producer_job_id") if isinstance(answer, dict) else None
        if not isinstance(job, str):
            try:
                await asyncio.to_thread(_sample_copy, target, {"shots": [shot]}, ("picture",), False)
            except OSError:
                pass
        out.append(job if isinstance(job, str) else None)
    return out


def _sample_accept_proposals(ref: ProjectReference, directory: str) -> None:
    """Close the sample's own open Previz proposals as accepted (one planning transaction, previz.json only)."""
    with _planning_open(ref, directory) as tx:
        pv, _plan, doc, _shots = _previz_docs(tx)
        open_ = [p for p in doc.get("proposals") or [] if p.get("state") == "open"]
        if not open_:
            return
        for p in open_:
            p["state"] = "accepted"
        _previz_save(tx, doc, None)


# w23-first (BCK-216; Resolve opens a new sample on its Edit page): a sample built in this process lands on Cut on its
# FIRST open by the page, whichever way the page opens it (Start's tile, or Hermes' open_project through the same
# /projects/open-recent): that answer carries landing "timeline" once; every later open keeps the last page (w22-dlv).
# Hermes' own route call (continuo_project_open_recent, no page) neither gets nor uses it up. The marker is a file in
# the project's state folder, not process memory: Hermes builds the sample in its own process (bench finding).
_SAMPLE_LANDING_FILE = "sample-landing.json"
_projects_open_recent_before_landing = projects_open_recent


def _sample_landing_take(project_id: str) -> bool:
    """True once: the project's landing marker existed and is removed now."""
    try:
        marker = Path(_recent_directory(_recent_project_store_factory(), project_id)) / "state" / _SAMPLE_LANDING_FILE
        marker.unlink()
        return True
    except (studio_core.ProjectError, OSError, ValueError, TypeError):
        return False


async def _projects_open_recent_landing(request: ProjectOpenRecentRequest) -> Any:
    answer = await _projects_open_recent_before_landing(request)
    if (_film_caller() != "hermes" and isinstance(answer, dict) and isinstance(answer.get("project"), dict)
            and not answer.get("error") and await asyncio.to_thread(_sample_landing_take, request.project_id)):
        return {**answer, "landing": "timeline"}
    return answer


_projects_open_recent_landing.__name__ = _projects_open_recent_before_landing.__name__  # the route keeps its name
projects_open_recent = _projects_open_recent_landing  # noqa: F811 - the facade name answers the same as the route
for _route in project_router.routes:
    if getattr(_route, "path", None) == "/projects/open-recent" and "POST" in getattr(_route, "methods", ()):
        _route.endpoint = _projects_open_recent_landing
        _route.dependant.call = _projects_open_recent_landing
