"""Fetch third-party model code at a pinned commit for the engine installers (song, scene).

The installers never read code from a machine-local folder: each repository is fetched by the
installer itself, from its upstream URL or from ``--source-root`` (a folder of local clones, one
sub-folder per repository name, for offline installs), at ONE full commit sha.  The checkout is
verified (``HEAD`` must equal the pin) and the caller records URL + commit in the engine's own
``engine.json``.  Code without a licence is never copied into the Film Suite repository; it only
lands in the engine folder on the user's machine.

Only the paths a caller names are checked out (sparse; ``blob:none`` where the server supports
it), so weights bundled inside a repository are never written to the engine folder.  Line endings are the
repository's own bytes (``core.autocrlf=false``) on every machine, so file sha256 pins hold
everywhere.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_SHA = re.compile(r"[0-9a-f]{40}")
_GIT_CONFIG = ["-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "core.symlinks=false",
               "-c", "advice.detachedHead=false", "-c", "init.defaultBranch=main"]


class PinError(SystemExit):
    """Refusal with a message the user can act on (raised as SystemExit so the installer stops)."""


def remote_for(name: str, url: str, source_root: Path | None) -> str:
    """The pinned upstream URL, or ``<source_root>/<name>`` for an offline install."""
    if source_root is None:
        return url
    local = Path(source_root) / name
    if not local.exists():
        raise PinError(f"{name}: no local clone at {local} (--source-root holds one folder per repository)")
    return str(local)


def _git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *_GIT_CONFIG, *args], cwd=cwd, capture_output=True, text=True,
                          env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})


def fetch_pinned(name: str, url: str, commit: str, dest: Path, paths: list[str]) -> str:
    """Check out ``paths`` of ``url`` at ``commit`` into ``dest``; returns the verified commit.

    Refuses a short or malformed sha, a commit the remote does not have (wrong sha, or a branch
    that was force-moved and the pinned commit is gone), and a checkout whose HEAD is not the pin.
    """
    if not isinstance(commit, str) or not _SHA.fullmatch(commit):
        raise PinError(f"{name}: pin must be a full 40-character commit sha, got {commit!r}")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").exists():
        out = _git(["init", "-q", str(dest)])
        if out.returncode != 0:
            raise PinError(f"{name}: git init failed: {out.stderr.strip()[-300:]}")
    _git(["remote", "remove", "origin"], cwd=dest)
    _git(["remote", "add", "origin", url], cwd=dest)
    out = _git(["fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", commit], cwd=dest)
    if out.returncode != 0:
        raise PinError(f"{name}: pinned commit {commit} is not available from {url} "
                       f"(wrong sha, or the branch was moved and the commit is gone); refusing to install. "
                       f"git: {out.stderr.strip()[-300:]}")
    _git(["config", "remote.origin.promisor", "true"], cwd=dest)
    _git(["config", "remote.origin.partialclonefilter", "blob:none"], cwd=dest)
    out = _git(["sparse-checkout", "set", "--no-cone", *[f"/{p}" for p in paths]], cwd=dest)
    if out.returncode != 0:
        raise PinError(f"{name}: sparse checkout failed: {out.stderr.strip()[-300:]}")
    out = _git(["checkout", "-q", "--force", "--detach", commit], cwd=dest)
    if out.returncode != 0:
        raise PinError(f"{name}: checkout of {commit} failed: {out.stderr.strip()[-300:]}")
    head = _git(["rev-parse", "HEAD"], cwd=dest).stdout.strip()
    if head != commit:
        raise PinError(f"{name}: checkout is at {head or 'nothing'}, not the pinned {commit}; refusing to install")
    return head
