from __future__ import annotations

# --- Previz stage 1 (lane w20-plan2): one set per scene (state/previz.json, studio.previz/1) ------------------------------
# The pure rules live in dashboard/planning/previz.py (set, figures, overrides, blocking, proposals) and
# dashboard/planning/previz_clip.py (the normal-map guide clip). Contract 2 (owner steer, architecture review): cameras
# live ONLY in the shot record (state/shot-plan.json); previz routes that move a camera write the shot record through
# the same planning rules and lock. Contract 3: a Hermes write is ONE proposal with its before/after delta (the scene set
# and every shot camera it touched); /projects/previz/review undo restores it only while unchanged since.
# Hand and Hermes use the same routes (continuo_previz_*). Every write announces media.changed for the project.


class PrevizReadRequest(_ProjectRequest):
    project_ref: ProjectReference
    scene: str | None = Field(default=None, pattern=r"^s-[0-9a-f]{8}$")


class PrevizWriteRequest(_ProjectRequest):
    project_ref: ProjectReference
    expected_revision: int = Field(ge=0)
    scene: str = Field(pattern=r"^s-[0-9a-f]{8}$")
    set: dict[str, Any]


class PrevizCameraRequest(_ProjectRequest):
    project_ref: ProjectReference
    shot: str = Field(pattern=r"^sh-[0-9a-f]{8}$")
    camera: dict[str, Any] | None
    # w22-pv SCR-080 (review codex3 F3): the shot plan's revision the camera was read at (previz read shot_plan_revision);
    # required, like /projects/shot-plan/write: a stale camera is refused instead of overwriting newer work.
    expected_revision: int | None = Field(default=None, ge=0)


class PrevizFigureRequest(_ProjectRequest):
    project_ref: ProjectReference
    scene: str = Field(pattern=r"^s-[0-9a-f]{8}$")
    figure: dict[str, Any]
    shot: str | None = Field(default=None, pattern=r"^sh-[0-9a-f]{8}$")


class PrevizBlockRequest(_ProjectRequest):
    project_ref: ProjectReference
    scene: str = Field(pattern=r"^s-[0-9a-f]{8}$")


class PrevizReviewRequest(_ProjectRequest):
    project_ref: ProjectReference
    action: Literal["undo", "dismiss"]
    proposal: str | None = Field(default=None, pattern=r"^pr-[0-9a-f]{8}$")


class PrevizClipRequest(_ProjectRequest):
    project_ref: ProjectReference
    shot: str = Field(pattern=r"^sh-[0-9a-f]{8}$")
    width: int = Field(ge=16, le=4096)
    height: int = Field(ge=16, le=4096)
    fps_num: int = Field(ge=1, le=240000)
    fps_den: int = Field(ge=1, le=1001)
    frames: list[str] = Field(min_length=1, max_length=1201)
    sidecar: dict[str, Any] = Field(default_factory=dict)


def _previz_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _previz_docs(tx: Any) -> tuple[Any, Any, dict[str, Any], dict[str, Any]]:
    pv, plan = _planning_module("previz"), _planning_module("shot_plan")
    doc = tx.load("previz.json")
    shots = tx.load("shot-plan.json")
    plan.migrate(shots)
    return pv, plan, doc, shots


def _previz_cameras(shots: dict[str, Any], ids: Any) -> dict[str, Any]:
    return {s: (shots.get("shots") or {}).get(s, {}).get("camera") for s in ids}


def _previz_save(tx: Any, doc: dict[str, Any], shots: dict[str, Any] | None) -> None:
    doc["revision"] = int(doc.get("revision") or 0) + 1
    if shots is None:
        tx.save("previz.json", doc)
        return
    shots["revision"] = int(shots.get("revision") or 0) + 1
    tx.commit({"shot-plan.json": shots, "previz.json": doc})  # w22-pv SCR-082: both files or neither (planning/store.py)


def _previz_answer(work: Any) -> dict[str, Any]:
    pv, plan, clip = _planning_module("previz"), _planning_module("shot_plan"), _planning_module("previz_clip")
    try:
        return work()
    except (pv.PrevizRefusal, plan.PlanRefusal, clip.ClipRefusal, _planning_module("store").PlanningStoreRefusal) as exc:
        return {"ok": False, "reason_code": exc.reason_code, "message": exc.message}
    except studio_core.ProjectError as exc:
        return {"ok": False, **_project_error_response(exc)}


def _previz_out(pv: Any, doc: dict[str, Any], shots: dict[str, Any], scene: str | None, **extra: Any) -> dict[str, Any]:
    plan = _planning_module("shot_plan")
    cams = {s: plan.camera_public(r.get("camera")) for s, r in (shots.get("shots") or {}).items() if r.get("camera")}
    return {"ok": True, "reason_code": None, "message": None, **pv.public(doc, scene), "cameras": cams, "shot_plan_revision": int(shots.get("revision") or 0), **extra}


def _previz_hermes(pv: Any, doc: dict[str, Any], scene: str, label: str, before_set: Any, after_set: Any, before_cams: dict[str, Any], after_cams: dict[str, Any]) -> dict[str, Any] | None:
    if _film_caller() != "hermes":
        return None
    # w22-pv SCR-081 (review codex3 F4): a change that leaves the set alone records NO set (not the set as it was): its
    # Undo then never touches the set, so geometry the artist made after it stays.
    if after_set == before_set:
        return pv.propose(doc, scene, "hermes", label, _previz_now(), {"cameras": before_cams}, {"cameras": after_cams})
    return pv.propose(doc, scene, "hermes", label, _previz_now(), {"set": before_set, "cameras": before_cams}, {"set": after_set, "cameras": after_cams})


def _previz_read(request: PrevizReadRequest) -> dict[str, Any]:
    directory = _planning_project(request.project_ref)
    with _planning_open(request.project_ref, directory) as tx:
        pv, _plan, doc, shots = _previz_docs(tx)
    return _previz_out(pv, doc, shots, request.scene)


def _previz_write(request: PrevizWriteRequest) -> dict[str, Any]:
    directory = _planning_project(request.project_ref)
    with _planning_open(request.project_ref, directory) as tx:
        pv, _plan, doc, shots = _previz_docs(tx)
        if request.expected_revision != int(doc.get("revision") or 0):
            return {"ok": False, "reason_code": "previz_conflict", "message": "The previz set changed meanwhile.", "revision": doc.get("revision")}
        before = doc["scenes"].get(request.scene)
        after = pv.apply_set(doc, request.scene, request.set)
        doc["scenes"][request.scene] = after
        proposal = _previz_hermes(pv, doc, request.scene, "changed the set", before, after, {}, {})
        _previz_save(tx, doc, None)
    return _previz_out(pv, doc, shots, request.scene, proposal=proposal and proposal["id"])


def _previz_camera(request: PrevizCameraRequest) -> dict[str, Any]:
    """Set one shot's camera: a shot-record write (the only camera authority), as one proposal when Hermes writes."""
    directory = _planning_project(request.project_ref)
    scene = _previz_scene_of_shot(request.project_ref, directory, request.shot)  # the script's own lock, taken before ours
    if request.expected_revision is None:
        raise _planning_module("previz").PrevizRefusal("revision_required", "Give expected_revision (continuo_previz_read shot_plan_revision).")
    with _planning_open(request.project_ref, directory) as tx:
        pv, plan, doc, shots = _previz_docs(tx)
        if request.expected_revision != int(shots.get("revision") or 0):
            return {"ok": False, "reason_code": "previz_conflict", "message": "The shot's camera changed meanwhile; nothing was saved.", "shot_plan_revision": int(shots.get("revision") or 0)}
        before = _previz_cameras(shots, [request.shot])
        plan.apply(shots, [{"shot": request.shot, "camera": request.camera}], None, _film_caller(), _previz_now())
        after = _previz_cameras(shots, [request.shot])
        set_now = doc["scenes"].get(scene) if scene else None
        proposal = _previz_hermes(pv, doc, scene, "set the camera of a shot", set_now, set_now, before, after) if scene else None
        _previz_save(tx, doc, shots)
    return _previz_out(pv, doc, shots, scene, proposal=proposal and proposal["id"])


def _previz_figure_label(request: PrevizFigureRequest, current: dict[str, Any], number: str | None) -> str:
    """Lane w24-rest: the Undo line names what changed ("FERRYMAN sits in shot 1.2"), never "placed a figure"."""
    fig = request.figure
    name = fig.get("name") or next((f.get("name") for f in current["figures"] if f["id"] == fig.get("id")), "") or "A figure"
    doing = {"sit": "sits", "walk": "walks", "stand": "stands"}.get(fig.get("pose")) if "pose" in fig else None
    what = doing or ("turns" if "yaw" in fig and "pos" not in fig else "moves" if "pos" in fig or fig.get("id") else "is placed")
    if request.shot is None:
        return f"{name} {what} in the scene"[:120]
    return f"{name} {what} in shot {number or request.shot}"[:120]


def _previz_shot_number(reference: ProjectReference, directory: str, shot: str | None) -> str | None:
    """The shot's number as the page shows it ("1.2"); read before the planning lock (the script's lock comes first)."""
    if shot is None:
        return None
    try:
        _state, derived = _previz_scene_rows(reference, directory)
    except Exception:  # noqa: BLE001 - the label never fails the write
        return None
    row = next((s for s in derived.get("shots") or [] if s["id"] == shot), None)
    number = row.get("number") if row else None
    return str(number) if number not in (None, "") else None


def _previz_figure(request: PrevizFigureRequest) -> dict[str, Any]:
    """Place one figure (by id; new without one): in the scene, or with ``shot`` as that shot's override only."""
    directory = _planning_project(request.project_ref)
    number = _previz_shot_number(request.project_ref, directory, request.shot)  # lane w24-rest: the Undo line's shot
    with _planning_open(request.project_ref, directory) as tx:
        pv, _plan, doc, shots = _previz_docs(tx)
        before = doc["scenes"].get(request.scene)
        current = pv.scene_of(doc, request.scene)
        fid = request.figure.get("id")
        if request.shot is not None:
            if not any(f["id"] == fid for f in current["figures"]):
                raise pv.PrevizRefusal("figure_unknown", "A per-shot placement names a figure of the scene (its id).")
            patch = {"overrides": {request.shot: {fid: {k: v for k, v in request.figure.items() if k != "id"}}}}
        else:
            figs = [dict(f) for f in current["figures"]]
            at = next((i for i, f in enumerate(figs) if f["id"] == fid), None)
            if fid is not None and at is None:
                raise pv.PrevizRefusal("figure_unknown", "No figure with that id in the scene (continuo_previz_read).")
            if at is None:
                figs.append(request.figure)
            else:
                figs[at] = {**figs[at], **request.figure}
            patch = {"figures": figs}
        after = pv.apply_set(doc, request.scene, patch)
        doc["scenes"][request.scene] = after
        proposal = _previz_hermes(pv, doc, request.scene, _previz_figure_label(request, current, number), before, after, {}, {})
        _previz_save(tx, doc, None)
    return _previz_out(pv, doc, shots, request.scene, proposal=proposal and proposal["id"])


def _previz_scene_rows(reference: ProjectReference, directory: str) -> tuple[dict[str, Any], dict[str, Any]]:
    result = _script_store().read(directory, project_id=reference.project_id)
    if not result["exists"]:
        raise _planning_module("previz").PrevizRefusal("script_missing", "The project has no script yet.")
    return result["state"], _script_view(result["state"], "derived")["derived"]


def _previz_scene_of_shot(reference: ProjectReference, directory: str, shot: str) -> str | None:
    try:
        _state, derived = _previz_scene_rows(reference, directory)
    except Exception:  # noqa: BLE001 - a camera can be set without a readable script; no proposal scene then
        return None
    return next((s["scene"] for s in derived.get("shots") or [] if s["id"] == shot), None)


def _previz_block(request: PrevizBlockRequest) -> dict[str, Any]:
    """Block a scene from the script as ONE proposal: figures for its people, a starting set, a camera per shot."""
    directory = _planning_project(request.project_ref)
    state, derived = _previz_scene_rows(request.project_ref, directory)
    if request.scene not in state["scenes"]:
        raise _planning_module("previz").PrevizRefusal("scene_unknown", "No such scene (continuo_script_read).")
    heading = next((line.strip() for line in state["scenes"][request.scene]["text"].split("\n") if line.strip()), "")
    scene_text = state["scenes"][request.scene]["text"]
    # w39-p2-previz: a character the scene's action names in its own words is in the scene too ("Ines smiles")
    people = [{"name": c["name"]} for c in derived.get("characters") or []
              if request.scene in (c.get("speaks") or []) or c.get("introduced") == request.scene
              or _planning_module("previz").names_in(c["name"], scene_text)]
    # w39-p2-previz (agent run on a new script: "MARA sits on a crate" gave no figure): people the action introduces in
    # capitals who never speak are people of the scene too
    known = {p["name"].casefold() for p in people} | {c["name"].casefold() for c in derived.get("characters") or []}
    people += [{"name": n} for n in _planning_module("previz").introduced_names(scene_text) if n.casefold() not in known]
    # The script's characters are elements once read (seeded, contract 4 tombstones respected): the same read as the page.
    catalogue = _elements_read(ElementsReadRequest(project_ref=request.project_ref))
    with _planning_open(request.project_ref, directory) as tx:
        pv, plan, doc, shots = _previz_docs(tx)
        for p in people:
            el = next((e for e in catalogue.get("elements") or [] if e.get("kind") == "character" and e["name"].casefold() == p["name"].casefold() and e.get("id")), None)
            if el:
                p["element"], p["version"] = el["id"], el["versions"][0]["id"]
        rows = [{"id": s["id"], "text": s["text"], "details": ((shots.get("shots") or {}).get(s["id"]) or {}).get("details") or {}}
                for s in derived.get("shots") or [] if s["scene"] == request.scene]
        cams_now = _previz_cameras(shots, [r["id"] for r in rows])
        before = doc["scenes"].get(request.scene)
        new_set, cams = pv.block(pv.scene_of(doc, request.scene), heading, people, rows, cams_now, scene_text)
        new_set = pv.apply_set({"scenes": {request.scene: new_set}}, request.scene, {})
        if cams:
            plan.apply(shots, [{"shot": s, "camera": c} for s, c in cams.items()], None, _film_caller(), _previz_now())
        doc["scenes"][request.scene] = new_set
        touched = list(cams)
        before_cams = {s: cams_now[s] for s in touched}
        after_cams = _previz_cameras(shots, touched)
        proposal = pv.propose(doc, request.scene, _film_caller(), f"blocked scene from the script · {len(touched)} cameras", _previz_now(),
                              {"set": before, "cameras": before_cams}, {"set": new_set, "cameras": after_cams})
        _previz_save(tx, doc, shots if cams else None)
    return _previz_out(pv, doc, shots, request.scene, proposal=proposal["id"], cameras_added=touched)


def _previz_review(request: PrevizReviewRequest) -> dict[str, Any]:
    directory = _planning_project(request.project_ref)
    with _planning_open(request.project_ref, directory) as tx:
        pv, plan, doc, shots = _previz_docs(tx)
        proposal = pv.find_proposal(doc, request.proposal)
        restored: list[str] = []
        if request.action == "undo":
            cams = proposal["after"].get("cameras") or {}
            pv.undo_check(proposal, doc["scenes"].get(proposal["scene"]), _previz_cameras(shots, cams))
            before = proposal["before"]
            # w22-pv SCR-081: only what the change touched is restored. A proposal without "set" (a camera-only change)
            # leaves the set as it is now; an old ambiguous one (set None on both sides) also leaves it.
            if "set" in proposal["after"] and not (before.get("set") is None and proposal["after"].get("set") is None):
                if before.get("set") is None:
                    doc["scenes"].pop(proposal["scene"], None)
                else:
                    kept = (doc["scenes"].get(proposal["scene"]) or {}).get("kept") or {}
                    doc["scenes"][proposal["scene"]] = {**before["set"], "kept": kept}
            for shot, cam in (before.get("cameras") or {}).items():
                record = shots.setdefault("shots", {}).setdefault(shot, {"details": {}, "camera": None, "pictures": [], "elements": [], "joined": []})
                record["camera"] = cam
                # one lens field (contract 2): the undone camera brings its lens back to details.lens as well
                if cam and cam.get("lens_mm") is not None:
                    record.setdefault("details", {})["lens"] = cam["lens_mm"]
                restored.append(shot)
        proposal["state"] = "undone" if request.action == "undo" else "dismissed"
        _previz_save(tx, doc, shots if restored else None)
    return _previz_out(pv, doc, shots, proposal["scene"], proposal=proposal["id"], state=proposal["state"], restored=restored)


def _previz_clip(request: PrevizClipRequest) -> dict[str, Any]:
    """Contract 5 seam (no UI): the page's rendered normal-pass frames at an EXPLICIT size, rational fps and frame count
    -> one frame-exact lossless clip (FFV1 .mkv) + sidecar in the project's exports/previz."""
    clip = _planning_module("previz_clip")
    directory = _planning_project(request.project_ref)
    with _planning_open(request.project_ref, directory) as tx:
        pv, _plan, doc, shots = _previz_docs(tx)
        rev, plan_rev = int(doc.get("revision") or 0), int(shots.get("revision") or 0)
    out = clip.encode(Path(directory) / "exports" / "previz", request.shot, request.width, request.height, request.fps_num, request.fps_den,
                      request.frames, {**request.sidecar, "previz_revision": rev, "shot_plan_revision": plan_rev}, str(FFMPEG_PATH))
    return {"ok": True, "reason_code": None, "message": None, **out}


def _previz_announce(project_ref: ProjectReference, answer: dict[str, Any]) -> dict[str, Any]:
    if answer.get("ok"):
        try:
            film_events.media_changed("film_owner:" + project_ref.project_id)
        except Exception:  # noqa: BLE001 - an event never fails a route
            pass
    return answer


@project_router.post("/projects/previz/read")
async def projects_previz_read(request: PrevizReadRequest) -> dict[str, Any]:
    """The previz set of one scene (or all), its open Hermes proposals and the shot cameras (from the shot record)."""
    return await asyncio.to_thread(_previz_answer, lambda: _previz_read(request))


@project_router.post("/projects/previz/write")
async def projects_previz_write(request: PrevizWriteRequest) -> dict[str, Any]:
    """Patch one scene's set (floor, boxes, figures, per-shot overrides, kept frames) against expected_revision."""
    return _previz_announce(request.project_ref, await asyncio.to_thread(_previz_answer, lambda: _previz_write(request)))


@project_router.post("/projects/previz/camera")
async def projects_previz_camera(request: PrevizCameraRequest) -> dict[str, Any]:
    """Set a shot's camera (the shot record is the only camera authority)."""
    return _previz_announce(request.project_ref, await asyncio.to_thread(_previz_answer, lambda: _previz_camera(request)))


@project_router.post("/projects/previz/figure")
async def projects_previz_figure(request: PrevizFigureRequest) -> dict[str, Any]:
    """Place or turn one figure in the scene, or for one shot only (override)."""
    return _previz_announce(request.project_ref, await asyncio.to_thread(_previz_answer, lambda: _previz_figure(request)))


@project_router.post("/projects/previz/block")
async def projects_previz_block(request: PrevizBlockRequest) -> dict[str, Any]:
    """Block a scene from the script as one undoable proposal."""
    return _previz_announce(request.project_ref, await asyncio.to_thread(_previz_answer, lambda: _previz_block(request)))


@project_router.post("/projects/previz/review")
async def projects_previz_review(request: PrevizReviewRequest) -> dict[str, Any]:
    """Undo (only while unchanged since) or dismiss an open previz proposal."""
    return _previz_announce(request.project_ref, await asyncio.to_thread(_previz_answer, lambda: _previz_review(request)))


@project_router.post("/projects/previz/clip")
async def projects_previz_clip(request: PrevizClipRequest) -> dict[str, Any]:
    """Encode rendered normal-pass frames to a frame-exact guide clip (no UI; the H3 guide seam)."""
    return await asyncio.to_thread(_previz_answer, lambda: _previz_clip(request))
