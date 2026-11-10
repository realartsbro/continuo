"""Durable image producer orchestration for the offline U4b slice.

The coordinator is deliberately provider-neutral.  It owns the ledger
transitions and calls only the injected adapter; it never publishes bytes,
changes image authoring state, or performs film adoption.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import threading
import time
from collections.abc import Mapping
from typing import Any, Protocol

from .image_contract import ContractError
from .image_job_store import ImageProducerJobStore


class ImageProviderAdapter(Protocol):
    """Provider-neutral transport seam consumed by the coordinator."""

    async def submit(self, **kwargs: Any) -> Any: ...

    async def recover_submit(self, client_request_id: str, **kwargs: Any) -> Any: ...

    async def poll(self, provider_prompt_id: str, **kwargs: Any) -> Any: ...

    async def cancel(self, provider_prompt_id: str) -> Any: ...


class ImageCoordinatorError(ValueError):
    """Raised for invalid coordinator arguments or impossible local state."""


class ImageProducerCoordinator:
    """Orchestrate submit, recovery, status and cancel around the image ledger."""

    def __init__(self, store: ImageProducerJobStore, adapter: ImageProviderAdapter) -> None:
        required_methods = ("submit", "recover_submit", "poll", "cancel")
        if not hasattr(store, "accept") or any(
            not callable(getattr(adapter, name, None)) for name in required_methods
        ):
            raise TypeError("store and provider adapter are required")
        self.store = store
        self.adapter = adapter
        # Provider output identity for terminal jobs, keyed by local job id.
        # This is provider-returned retrieval metadata, not a publication path,
        # and it is never accepted from a caller.
        self._descriptors: dict[str, dict[str, Any]] = {}
        # Lane w25-imgfix P0 (artist's window 22:34, Install 130): the page's routes and Hermes' tool dispatch run on
        # different event loops. One asyncio.Lock per job bound itself to the loop that first waited on it; the other
        # loop then raised "bound to a different event loop" on every status poll, and a waiter woken from the
        # wrong thread never woke (continuo_image_cancel hung 420 s, the job stayed Running). The per-job lock is a
        # threading.Lock, awaited without blocking any loop; any thread may release it.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, job_id: str) -> "_JobLock":
        with self._locks_guard:
            lock = self._locks.get(job_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[job_id] = lock
        return _JobLock(lock)

    def _public(self, job: Mapping[str, Any]) -> dict[str, Any]:
        """Return a copy without publication paths or adapter internals."""
        result = copy.deepcopy(dict(job))
        # This slice never creates publication metadata.  Do not let a caller
        # accidentally receive a path if an older envelope contains one.
        result.pop("publication_path", None)
        # Retrieval metadata is only meaningful for a job the ledger considers
        # ready; every other state must not carry one.
        result.pop("output_descriptor", None)
        if result.get("state") == "ready":
            known = self._descriptors.get(result.get("image_producer_job_id"))
            if known is not None:
                result["output_descriptor"] = copy.deepcopy(known)
        return result

    def _with_descriptor(self, job: dict[str, Any], descriptor: dict[str, Any] | None) -> dict[str, Any]:
        """Remember the provider descriptor that belongs to this local job."""
        if descriptor is not None:
            self._descriptors[job["image_producer_job_id"]] = descriptor
        return job

    async def submit(
        self,
        idempotency_key: str,
        request_snapshot: Mapping[str, Any],
        *,
        client_request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create/replay one local job and perform at most one fresh submit."""
        job, _ = self.store.accept(idempotency_key, dict(request_snapshot), client_request_id)
        lock = self._lock_for(job["image_producer_job_id"])
        async with lock:
            current = self.store.get_job(job["image_producer_job_id"])
            result = await self._submit_locked(current)
            return self._public(result)

    create = submit
    submit_request = submit

    async def submit_job(self, image_producer_job_id: str) -> dict[str, Any]:
        """Resume one existing job, recovering ambiguity instead of resubmitting."""
        job = self.store.get_job(image_producer_job_id)
        lock = self._lock_for(job["image_producer_job_id"])
        async with lock:
            result = await self._submit_locked(self.store.get_job(image_producer_job_id))
            return self._public(result)

    async def _submit_locked(self, job: dict[str, Any]) -> dict[str, Any]:
        state = job["state"]
        if state == "accepted":
            job = self.store.mark_submitting(job["image_producer_job_id"])
            job = self.store.record_submission_attempt(job["image_producer_job_id"])
            try:
                outcome = await self.adapter.submit(
                    image_producer_job_id=job["image_producer_job_id"],
                    request_snapshot=job["request_snapshot"],
                    client_request_id=job["submit_intent"]["client_request_id"],
                    request_snapshot_sha256=job["request_snapshot_sha256"],
                )
            except Exception:
                outcome = None
            if _outcome_kind(outcome) == "accepted" and _outcome_prompt(outcome):
                try:
                    return self.store.bind_provider_prompt(
                        job["image_producer_job_id"], _outcome_prompt(outcome)
                    )
                except Exception:
                    # The provider acknowledged but local ownership could not
                    # be recorded.  Reconciliation is required; never retry.
                    return self.store.mark_submit_unresolved(job["image_producer_job_id"])
            return self.store.mark_submit_unresolved(job["image_producer_job_id"])

        if state == "submitting":
            return await self._recover_locked(job)

        if state == "quarantined" and job.get("quarantine_reason") == "submit-reconciliation-required":
            return await self._recover_locked(job)

        # A bound or terminal row is an idempotent replay.  In particular, do
        # not call the adapter for queued/running/ready/failed/cancelled rows.
        return job

    async def recover_submit(self, image_producer_job_id: str) -> dict[str, Any]:
        """Recover an unresolved submit by exact correlation, never by submit."""
        job = self.store.get_job(image_producer_job_id)
        lock = self._lock_for(job["image_producer_job_id"])
        async with lock:
            result = await self._recover_locked(self.store.get_job(image_producer_job_id))
            return self._public(result)

    recover = recover_submit

    async def _recover_locked(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = job["image_producer_job_id"]
        if job["state"] not in {"submitting", "quarantined"}:
            return job
        if job["state"] == "quarantined" and job.get("quarantine_reason") != "submit-reconciliation-required":
            return job
        if job["state"] == "submitting" and job["submission_count"] < job["attempt"]:
            # A crash before the attempt marker is still made safe without a
            # provider operation.  It remains retryable, not submit-ambiguous.
            return self.store.quarantine(job_id, "submit-attempt-not-recorded")
        try:
            outcome = await self.adapter.recover_submit(
                job["submit_intent"]["client_request_id"],
                image_producer_job_id=job_id,
                request_snapshot_sha256=job["request_snapshot_sha256"],
            )
        except Exception:
            outcome = None
        if _outcome_kind(outcome) == "accepted" and _outcome_prompt(outcome):
            try:
                if job["state"] == "quarantined":
                    return self.store.reconcile_submit(job_id, _outcome_prompt(outcome))
                return self.store.bind_provider_prompt(job_id, _outcome_prompt(outcome))
            except Exception:
                return self.store.get_job(job_id)
        if job["state"] == "submitting":
            return self.store.mark_submit_unresolved(job_id)
        return self.store.get_job(job_id)

    async def status(self, image_producer_job_id: str) -> dict[str, Any]:
        """Poll only the provider prompt owned by the local ledger."""
        job = self.store.get_job(image_producer_job_id)
        lock = self._lock_for(job["image_producer_job_id"])
        async with lock:
            job = self.store.get_job(image_producer_job_id)
            # w25-imgfix P0 recovery: a submit that stayed "submitting" past the grace (its process died after the
            # attempt marker) is reconciled by its exact client request id against /queue and /history, never
            # resubmitted; not found there, it settles (submit-reconciliation-required) instead of running forever.
            if job["state"] == "submitting" and self._older_than(job, _SUBMIT_STUCK_SECONDS):
                return self._public(await self._recover_locked(job))
            if job["state"] in {"accepted", "submitting", "failed", "cancelled", "quarantined", "published"}:
                return self._public(job)
            # A ready job is terminal for the provider: answer from the ledger
            # instead of asking again.  After a restart the descriptor cache is
            # empty, so that one case still polls once to recover it.
            if job["state"] == "ready" and job["image_producer_job_id"] in self._descriptors:
                return self._public(job)
            prompt_id = job["provider"]["prompt_id"]
            if prompt_id is None:
                return self._public(job)
            # This operation is derived from the store, so a caller cannot
            # substitute a prompt belonging to another local job.
            self.store.status_operation(job["image_producer_job_id"], prompt_id)
            expected_identity = {
                "provider_id": job["provider"]["provider_id"],
                "image_producer_job_id": job["image_producer_job_id"],
                "client_request_id": job["provider"]["client_request_id"],
                "target_id": job["provider"]["target_id"],
                "request_snapshot_sha256": job["request_snapshot_sha256"],
                "capability_manifest_sha256": job["capability_manifest_sha256"],
                "api_graph_sha256": job["api_graph_sha256"],
            }
            try:
                outcome = await self.adapter.poll(
                    prompt_id,
                    expected_identity=expected_identity,
                )
            except Exception:
                outcome = None
            kind = _outcome_kind(outcome)
            if kind == "missing":
                # Every adapter reads history first, then the queue: a prompt that finishes between
                # the two reads is in neither answer. Ask once more before calling it lost (demo
                # finding: one picture of a batch ended job_lost although ComfyUI had finished it).
                try:
                    outcome = await self.adapter.poll(
                        prompt_id,
                        expected_identity=expected_identity,
                    )
                except Exception:
                    outcome = None
                kind = _outcome_kind(outcome)
            if kind == "transport_error" or outcome is None:
                return self._public(self.store.get_job(job["image_producer_job_id"]))
            if kind == "queued":
                return self._public(self._transition_if_needed(job, "queued"))
            if kind == "running":
                return self._public(self._transition_if_needed(job, "running"))
            if kind == "ready":
                return self._public(self._mark_ready(job, outcome))
            if kind == "failed":
                return self._public(self._transition_if_needed(job, "failed", _safe_reason(outcome, "execution_failed")))
            if kind == "cancelled":
                return self._public(self._transition_if_needed(job, "cancelled"))
            if kind == "missing":
                return self._public(self.store.quarantine(job["image_producer_job_id"], "job_lost"))
            # Every malformed or non-authoritative provider status maps to the
            # single public product reason. Provider diagnostics are not ledger
            # authority and must not create a second reason vocabulary.
            return self._public(
                self.store.quarantine(job["image_producer_job_id"], "output_invalid")
            )

    poll = status

    def _older_than(self, job: Mapping[str, Any], seconds: float) -> bool:
        try:
            stamp = (self.store.job_times([job["image_producer_job_id"]]) or {}).get(job["image_producer_job_id"]) or {}
        except Exception:  # noqa: BLE001 - a ledger without times never settles a job by age
            return False
        at = stamp.get("updated_at") or stamp.get("created_at")
        return isinstance(at, (int, float)) and time.time() - float(at) >= seconds

    def _transition_if_needed(self, job: dict[str, Any], state: str, reason: str | None = None) -> dict[str, Any]:
        if job["state"] == state:
            return job
        return self.store.transition(job["image_producer_job_id"], state, reason=reason)

    def _mark_ready(self, job: dict[str, Any], outcome: Any = None) -> dict[str, Any]:
        current = job
        descriptor = _outcome_descriptor(outcome)
        sent_prompt = outcome.get("sent_prompt") if isinstance(outcome, Mapping) else getattr(outcome, "sent_prompt", None)
        if sent_prompt is not None:
            current = self.store.record_sent_prompt(current["image_producer_job_id"], sent_prompt)
        if current["state"] == "queued":
            current = self.store.transition(current["image_producer_job_id"], "running")
        if current["state"] == "running":
            ready = self.store.transition(current["image_producer_job_id"], "ready")
            return self._with_descriptor(ready, descriptor)
        if current["state"] == "ready":
            return self._with_descriptor(current, descriptor)
        # A provider can report success while the local ledger is still in a
        # pre-running state only if the local state is corrupt or was changed
        # concurrently.  Do not weaken the lifecycle contract.
        raise ImageCoordinatorError("ready-state-not-running")

    async def cancel(
        self,
        image_producer_job_id: str,
        provider_prompt_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel locally before ownership or cancel the store-owned prompt."""
        job = self.store.get_job(image_producer_job_id)
        lock = self._lock_for(job["image_producer_job_id"])
        # w25-imgfix P0: a cancel answers within seconds. While a status poll still holds the job, the cancel goes to
        # the provider prompt the ledger owns without the lock (cancel is idempotent there); the next poll settles it.
        async with lock.within(_CANCEL_LOCK_WAIT_SECONDS):
            job = self.store.get_job(image_producer_job_id)
            if job["state"] in {"failed", "cancelled", "quarantined", "published"}:
                return self._public(job)
            if job["state"] == "accepted":
                operation = self.store.cancel_operation(job["image_producer_job_id"], provider_prompt_id)
                if operation["cancel_scope"] != "local":
                    raise ImageCoordinatorError("local-cancel-ownership-conflict")
                return self._public(self.store.cancel(job["image_producer_job_id"], None))
            owned_prompt = job["provider"]["prompt_id"]
            if owned_prompt is None:
                # Submitting means a provider boundary may already have been
                # crossed.  Do not turn unknown ownership into a local cancel.
                return self._public(job)
            operation = self.store.cancel_operation(job["image_producer_job_id"], provider_prompt_id or owned_prompt)
            exact_prompt = operation["provider_prompt_id"]
            try:
                outcome = await self.adapter.cancel(exact_prompt)
            except Exception:
                outcome = None
            if _outcome_kind(outcome) == "accepted":
                return self._public(self.store.cancel(job["image_producer_job_id"], exact_prompt))
            if _outcome_kind(outcome) == "transport_error" or outcome is None:
                return self._public(self.store.get_job(job["image_producer_job_id"]))
            return self._public(self.store.quarantine(job["image_producer_job_id"], "cancel_unknown"))

    cancel_job = cancel

    def retry(self, image_producer_job_id: str, retry_key: str) -> dict[str, Any]:
        """Delegate retry identity and lifecycle reset to the durable store."""
        return self._public(self.store.retry(image_producer_job_id, retry_key))

    def get_job(self, image_producer_job_id: str) -> dict[str, Any]:
        return self._public(self.store.get_job(image_producer_job_id))

    get = get_job


def _outcome_descriptor(outcome: Any) -> dict[str, Any] | None:
    """Return the adapter-validated output identity, or None.

    The adapter has already checked the descriptor against the pinned
    manifest; the coordinator only refuses obviously unusable shapes so a
    malformed value can never reach a caller as if it were authoritative.
    """
    identity = outcome.get("output_identity") if isinstance(outcome, Mapping) else getattr(outcome, "output_identity", None)
    if not isinstance(identity, Mapping):
        return None
    required = ("filename", "subfolder", "type")
    if any(not isinstance(identity.get(key), str) for key in required):
        return None
    return {str(key): value for key, value in identity.items()}


def _outcome_kind(outcome: Any) -> str | None:
    if isinstance(outcome, Mapping):
        value = outcome.get("kind")
    else:
        value = getattr(outcome, "kind", None)
    return value if isinstance(value, str) else None


def _outcome_prompt(outcome: Any) -> str | None:
    if isinstance(outcome, Mapping):
        value = outcome.get("provider_prompt_id", outcome.get("prompt_id"))
    else:
        value = getattr(outcome, "provider_prompt_id", None)
        if value is None:
            value = getattr(outcome, "prompt_id", None)
    return value if isinstance(value, str) and value else None


def _safe_reason(outcome: Any, default: str) -> str:
    if isinstance(outcome, Mapping):
        reason = outcome.get("reason")
    else:
        reason = getattr(outcome, "reason", None)
    allowed = {
        "execution_failed", "output_invalid", "invalid_history", "invalid_queue",
        "job_lost", "cancel_unknown", "unreachable", "provider-failed",
        # w6-gptimage: the subscription provider's two finite reasons (shown "Not connected" / "Limit reached").
        "not_connected", "limit_reached",
        # lane w24-rest (GEM-056): ComfyUI ran out of memory (comfy_failure.py), named instead of "execution_failed".
        "out_of_memory",
    }
    return reason if isinstance(reason, str) and reason in allowed else default


__all__ = [
    "ImageCoordinatorError",
    "ImageProviderAdapter",
    "ImageProducerCoordinator",
]


_LOCK_POLL_SECONDS = 0.01
_CANCEL_LOCK_WAIT_SECONDS = 5.0
_SUBMIT_STUCK_SECONDS = 120.0


class _JobLock:
    """An async context over a threading.Lock: usable from any event loop and any thread (w25-imgfix P0)."""

    def __init__(self, lock: threading.Lock) -> None:
        self._lock = lock
        self._limit: float | None = None
        self._held = False

    def within(self, seconds: float) -> "_JobLock":
        bounded = _JobLock(self._lock)
        bounded._limit = seconds
        return bounded

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> "_JobLock":
        deadline = None if self._limit is None else time.monotonic() + self._limit
        while not self._lock.acquire(blocking=False):
            if deadline is not None and time.monotonic() >= deadline:
                self._held = False
                return self
            await asyncio.sleep(_LOCK_POLL_SECONDS)
        self._held = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._held:
            self._held = False
            with contextlib.suppress(RuntimeError):
                self._lock.release()
