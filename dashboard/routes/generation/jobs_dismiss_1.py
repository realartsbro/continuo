from __future__ import annotations

# --- Dismissed failures (lane p5c-frm item 1; frame charter decision 3, added 28.10.) ------------------------
# A failed job stays in the Jobs list until the artist dismisses it (Dismiss per row, "Clear failed"; the
# Premiere/Resolve render queue "Clear"). Hermes has the same verb (continuo_jobs_dismiss) on the same route
# with the same refusals. The ids live in one small JSON per owner scope under the Hermes state root
# (append-only, atomic replace, bounded to the newest _JOBS_DISMISSED_MAX). /jobs/list marks each row
# ``dismissed``, leaves them out unless include_dismissed, and its ``counts`` never count them. Only failed rows of the scope's own list
# can be dismissed: a running or waiting one is refused job_running, an id the scope does not list job_not_found.

_JOBS_DISMISSED_MAX = 500
# lane p7-jobs (VID-102): failed and cancelled rows are cleared alike; only failed ones are counted.
_JOBS_DISMISSABLE = ("failed", "cancelled", "refused")  # w24-rest: a refused request is cleared like a failure, never counted
_JOBS_DISMISSED_SCHEMA = "studio.jobs-dismissed/1"
_JOBS_DISMISSED_LOCK = _thread.allocate_lock()
_JOB_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,96}$"


class JobsDismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    owner_scope: str = Field(pattern=_OWNER_SCOPE_PATTERN, min_length=18, max_length=47)
    job_ids: list[str] | None = Field(default=None, max_length=200)
    all_failed: bool = False


def _jobs_dismissed_path(owner_scope: str) -> Path:
    base = _local_data.records_base()
    leaf = owner_scope[len("film_owner:"):]
    return Path(state_root(Path(base) / "hermes")) / "jobs-dismissed" / f"jobs-dismissed-{leaf}.json"


def _jobs_dismissed_list(owner_scope: str) -> list[str]:
    path = _jobs_dismissed_path(owner_scope)
    try:
        document = json.loads(_win_io.read_text(path, encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ids = document.get("job_ids") if isinstance(document, dict) else None
    return [value for value in ids if isinstance(value, str)] if isinstance(ids, list) else []


def _jobs_dismissed_read(owner_scope: str) -> set[str]:
    return set(_jobs_dismissed_list(owner_scope))


def _jobs_dismissed_add(owner_scope: str, job_ids: list[str]) -> list[str]:
    with _JOBS_DISMISSED_LOCK:
        kept = _jobs_dismissed_list(owner_scope)
        known = set(kept)
        for job_id in job_ids:
            if job_id not in known:
                kept.append(job_id)
                known.add(job_id)
        kept = kept[-_JOBS_DISMISSED_MAX:]
        path = _jobs_dismissed_path(owner_scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".dismissed-", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"schema": _JOBS_DISMISSED_SCHEMA, "owner_scope": owner_scope, "job_ids": kept}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            _win_io.replace(temp, path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise
        return kept


# Lane w25-imgfix item 2 (IMG-212, seen live: "1 failed" in red for a day, a Krea run that died with the
# previous Hermes process pinned on top across restarts). Reference: Resolve's render queue and Media Encoder's queue
# mark a job of a session that ended as interrupted once, in its place by time; a new session starts with no red
# count. A failed row that settled before this backend process started is ``interrupted``: listed (still Dismiss /
# Clear failed, which persists through /jobs/dismiss), never counted as failed.
_JOBS_PROCESS_STARTED = time.time()


def _job_interrupted(job: dict[str, Any]) -> bool:
    # w25-imgfix P0: a job whose ComfyUI prompt vanished from /queue and /history (engine restarted, the coordinator's
    # job_lost) was interrupted too, not a failure of the picture.
    if job.get("state") == "failed" and job.get("reason_code") == "job_lost":
        return True
    finished = job.get("finished_at")
    return job.get("state") == "failed" and isinstance(finished, (int, float)) and not isinstance(finished, bool) \
        and 0 < finished < _JOBS_PROCESS_STARTED


def _jobs_mark_dismissed(owner_scope: str, jobs: list[dict[str, Any]]) -> dict[str, int]:
    """Marks every row ``dismissed`` (only a failed row can be) and answers the bar's counts."""
    try:
        dismissed = _jobs_dismissed_read(owner_scope)
    except Exception:  # noqa: BLE001 - an unreadable store dismisses nothing
        dismissed = set()
    counts = {"running": 0, "waiting": 0, "failed": 0}
    for job in jobs:
        # lane p7-jobs: a cancelled row is cleared like a failure (Dismiss / Clear) and is never a failure.
        gone = job.get("state") in _JOBS_DISMISSABLE and job.get("job_id") in dismissed
        job["dismissed"] = gone
        interrupted = _job_interrupted(job)  # w25-imgfix: died with the previous process, never counted
        if interrupted:
            job["interrupted"] = True  # only on such a row: every other row keeps its exact key set
        if job.get("state") == "running":
            counts["running"] += 1
        elif job.get("state") == "queued":
            counts["waiting"] += 1
        elif job.get("state") == "failed" and not gone and not interrupted:
            counts["failed"] += 1
    return counts


def _jobs_dismiss_refusal(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "reason_code": code, "message": message, "dismissed": []}


@project_router.post("/jobs/dismiss")
async def jobs_dismiss(request: JobsDismissRequest) -> dict[str, Any]:
    """Dismiss failed jobs of one owner scope (the hand and Hermes alike)."""
    if not request.all_failed and not request.job_ids:
        return _jobs_dismiss_refusal("invalid_request", "Name the jobs to dismiss, or all failed.")
    if any(not re.fullmatch(_JOB_ID_PATTERN, job_id) for job_id in request.job_ids or []):
        return _jobs_dismiss_refusal("invalid_request", "A job id is not valid.")
    scope = request.owner_scope
    project_id = None if scope == "film_owner:scratch" else scope[len("film_owner:"):]
    listed: list[dict[str, Any]] = []
    try:
        listed.extend(_image_jobs_for_scope(scope, 200))
    except Exception:  # noqa: BLE001 - a missing ledger lists nothing
        pass
    try:
        listed.extend(_video_jobs_for_scope(project_id, 200))
    except Exception:  # noqa: BLE001
        pass
    by_id = {job.get("job_id"): job for job in listed}
    if request.all_failed:
        wanted = [job["job_id"] for job in listed if job.get("state") in _JOBS_DISMISSABLE]
    else:
        wanted = list(dict.fromkeys(request.job_ids or []))
        for job_id in wanted:
            job = by_id.get(job_id)
            if job is None:
                return _jobs_dismiss_refusal("job_not_found", "That job is not in this list.")
            if job.get("state") in ("running", "queued"):
                return _jobs_dismiss_refusal("job_running", "A running job cannot be dismissed. Stop it first.")
            if job.get("state") not in _JOBS_DISMISSABLE:
                return _jobs_dismiss_refusal("job_not_failed", "Only a failed or cancelled job can be dismissed.")
    try:
        _jobs_dismissed_add(scope, wanted)
    except (OSError, ValueError):
        return _jobs_dismiss_refusal("jobs_io_failed", "The dismissed jobs could not be saved.")
    if wanted:
        try:
            film_events.jobs_changed(wanted[:50], "state", scope)
        except Exception:  # noqa: BLE001 - an event never fails a route
            pass
    return {"ok": True, "reason_code": None, "message": None, "owner_scope": scope, "dismissed": wanted}
