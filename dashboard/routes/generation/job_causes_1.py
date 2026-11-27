from __future__ import annotations

# --- Why a Video job failed (lane w11-backend item 3, M-35 backend half) -----------------------------
#
# The Shoot ledger keeps a finite reason_code per row; the sentence the artist was shown ("Choose an output
# folder for this project", "keyframe is outside approved roots") was lost, and a submit refused before a
# ledger row existed left no job at all, so the Jobs list showed only Image jobs. One small private SQLite
# table under the Hermes state folder keeps (a) the cause of a failed ledger row by its job id and (b) every
# refused submit as a job of its project (raw_state "refused"), beside the Shoot ledger. /jobs/list answers both. Causes are the
# finite route sentences; a text that looks like a path is never stored. Notes never fail a route.

_JOB_CAUSES_MAX = 2_000
_JOB_CAUSE_MAX_CHARS = 300
_JOB_CAUSES_LOCK = _thread.allocate_lock()
_JOB_CAUSE_PATHLIKE = re.compile(r"[A-Za-z]:[\\/]|\\\\|/|\\")
_JOB_CAUSE_PREFIXES = ("Generation submission failed: ",)


def _job_causes_path() -> Path:
    # Beside the Shoot ledger (like the H3 take facts): the causes share its profile and its lifetime.
    return Path(_comfy_store().path).parent / "job-causes.sqlite3"


def _job_causes_connect() -> sqlite3.Connection:
    path = _job_causes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=5)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS job_causes (job_id TEXT PRIMARY KEY, cause TEXT NOT NULL, at REAL NOT NULL,"
        " refused INTEGER NOT NULL DEFAULT 0, project_id TEXT, target_id TEXT, label TEXT)"
    )
    # Lane p12-shl (GEM-053): the refusal's finite code beside its sentence (older tables gain the column).
    if "reason_code" not in {row[1] for row in connection.execute("PRAGMA table_info(job_causes)")}:
        connection.execute("ALTER TABLE job_causes ADD COLUMN reason_code TEXT")
    return connection


def _job_reason_code(code: Any) -> str | None:
    """A finite code the page may name (ledger or refusal code), else None."""
    if type(code) is str and (code in generation.contract.REASON_CODES or code in generation.contract.REFUSAL_REASON_CODES
                              or code in V4_ROUTE_REASON_CODES):
        return code
    return None


def _job_cause_text(cause: Any) -> str | None:
    if type(cause) is not str:
        return None
    text = " ".join(cause.split())
    for prefix in _JOB_CAUSE_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
    if not text or len(text) > _JOB_CAUSE_MAX_CHARS or _JOB_CAUSE_PATHLIKE.search(text):
        return None
    return text


def _job_causes_write(row: tuple[Any, ...]) -> None:
    with _JOB_CAUSES_LOCK:
        connection = _job_causes_connect()
        try:
            with connection:
                connection.execute(
                    "INSERT OR REPLACE INTO job_causes (job_id, cause, at, refused, project_id, target_id, label, reason_code)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(row) + (None,) * (8 - len(row)),
                )
                (count,) = connection.execute("SELECT COUNT(*) FROM job_causes").fetchone()
                if count > _JOB_CAUSES_MAX:
                    connection.execute(
                        "DELETE FROM job_causes WHERE job_id IN (SELECT job_id FROM job_causes ORDER BY at ASC LIMIT ?)",
                        (count - _JOB_CAUSES_MAX,),
                    )
        finally:
            connection.close()


def _job_cause_note(job_id: Any, cause: Any) -> None:
    """The cause of one failed ledger row (first failure wins over nothing, a later note replaces it)."""
    try:
        text = _job_cause_text(cause)
        if type(job_id) is str and 0 < len(job_id) <= 160 and text:
            _job_causes_write((job_id, text, time.time(), 0, None, None, None))
    except Exception:  # noqa: BLE001 - a note never fails a route
        pass


def _job_refusal_note(request: Any, result: Any) -> None:
    """A /submit answered with an error and no job: it becomes a failed job of its project."""
    try:
        if not isinstance(result, dict) or "error" not in result or result.get("details") is not None:
            return
        # Lane w11-fixb item 3: the reason alone (reason_text) when /submit sent one.
        text = _job_cause_text(result.get("reason_text")) or _job_cause_text(result.get("error"))
        if not text:
            return
        project_id = getattr(request, "project_id", None)
        project_id = project_id if type(project_id) is str and project_id else None
        args = getattr(request, "args", None)
        prompt = args.get("prompt") if isinstance(args, dict) else None
        target = getattr(request, "target_id", None)
        job_id = "refused_" + uuid.uuid4().hex
        _job_causes_write((job_id, text, time.time(), 1, project_id,
                           target if type(target) is str and len(target) <= 120 else None,
                           _job_label(prompt) if type(prompt) is str else "",
                           _job_reason_code(result.get("reason_code"))))  # lane p12-shl: the refusal's own code
        _job_authors_note([job_id])  # lane p11-med (HER-049): the caller (_FILM_CALLER) is the refused job's author
        film_events.jobs_changed([job_id], "state", f"film_owner:{project_id}" if project_id else "film_owner:scratch")
    except Exception:  # noqa: BLE001
        pass


def _job_causes_read(job_ids: Any) -> dict[str, str]:
    ids = [job_id for job_id in (job_ids or ()) if type(job_id) is str and job_id]
    found: dict[str, str] = {}
    if not ids or not _job_causes_path().is_file():
        return found
    try:
        with _JOB_CAUSES_LOCK:
            connection = _job_causes_connect()
            try:
                for start in range(0, len(ids), 400):
                    chunk = ids[start:start + 400]
                    found.update(connection.execute(
                        f"SELECT job_id, cause FROM job_causes WHERE refused = 0 AND job_id IN ({','.join('?' * len(chunk))})",
                        chunk,
                    ).fetchall())
            finally:
                connection.close()
    except Exception:  # noqa: BLE001 - no cause reads as the reason code's sentence
        return {}
    return found


def _job_refusals_for_scope(project_id: str | None, limit: int) -> list[dict[str, Any]]:
    if not _job_causes_path().is_file():
        return []
    with _JOB_CAUSES_LOCK:
        connection = _job_causes_connect()
        try:
            rows = connection.execute(
                "SELECT job_id, cause, at, target_id, label, reason_code FROM job_causes WHERE refused = 1 AND project_id IS ?"
                " ORDER BY at DESC LIMIT ?", (project_id, limit),
            ).fetchall()
        finally:
            connection.close()
    return [{
        # w24-rest (maintainer review): refused before sending is not a failed run: state refused, never counted as failed.
        "module": "shoot", "job_id": job_id, "state": "refused", "raw_state": "refused", "label": label or "",
        "target_id": target_id, "created_at": float(at), "started_at": None, "finished_at": float(at), "error": cause,
        "reason_code": _job_reason_code(code),
    } for job_id, cause, at, target_id, label, code in rows]
