"""Style boards: bundled, read-only style index, search, style text, thumbnails.

The index (``image_builder/style-boards.v1.json.gz``) is built by
``the development style board builder`` from the MIT, prompt-only upstream catalog
``Andro-Meta/ComfyUI-Krea-Moodboards`` (no images are bundled). It is loaded
once per file identity and kept in memory.

A style is transferable visual treatment only. ``style_text`` rewrites subject
phrasing and drops fragments that name subjects, so applying a board never adds
or changes what the user asked for (rules adapted from the upstream catalog).

Thumbnails are the board's public 256 px preview. They are read from a local
cache under the plugin state root; a missing one is fetched once from the single
allowlisted preview host (HTTPS, no redirects, bounded size, image magic
checked) and published atomically. Cache names derive only from validated ids.
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import re
import secrets
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

SCHEMA = "studio.style-boards/1"
INDEX_PATH = Path(__file__).resolve().parents[1] / "image_builder" / "style-boards.v1.json.gz"
INDEX_MAX_BYTES = 8 * 1024 * 1024
INDEX_MAX_RAW_BYTES = 32 * 1024 * 1024
BOARD_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
THUMB_HOST = "https://optim-images.krea.ai/"
THUMB_PATH_RE = re.compile(r"^[A-Za-z0-9._-]{8,400}-(?:\d{2,4})\.webp$")
THUMB_SIZE = 256
THUMB_MAX_BYTES = 256 * 1024
THUMB_TIMEOUT_S = 8.0
THUMB_BATCH_MAX = 60
THUMB_WORKERS = 6
# A preview that could not be fetched is not asked for again for this long
# (every open of the browser would otherwise wait on the same timeouts).
THUMB_MISS_TTL_S = 30 * 60
QUERY_MAX = 200


class StyleBoardError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------
_INDEX_LOCK = threading.Lock()
_INDEX: dict[str, Any] = {"key": None, "value": None}


def _index_key(path: Path) -> tuple[int, int, int]:
    info = path.stat()
    return (info.st_size, info.st_mtime_ns, getattr(info, "st_ino", 0))


def _validated(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA or not isinstance(payload.get("boards"), list):
        raise StyleBoardError("style_index_invalid", "The style index is invalid")
    boards: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for raw in payload["boards"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not BOARD_ID_RE.fullmatch(raw["id"]):
            raise StyleBoardError("style_index_invalid", "The style index is invalid")
        if raw["id"] in by_id:
            raise StyleBoardError("style_index_invalid", "The style index is invalid")
        board = {
            "id": raw["id"],
            "title": str(raw.get("title") or "")[:120],
            "keywords": [str(item)[:80] for item in raw.get("keywords") or [] if isinstance(item, str)][:16],
            "axes": [str(item)[:80] for item in raw.get("axes") or [] if isinstance(item, str)][:12],
            "prompt": str(raw.get("prompt") or "")[:1600],
            "negative": str(raw.get("negative") or "")[:600],
            "summary": str(raw.get("summary") or "")[:900],
            "thumb": str(raw.get("thumb") or ""),
        }
        board["_search"] = {
            "title": board["title"].lower(),
            "keywords": " ".join(board["keywords"]).lower(),
            "axes": " ".join(board["axes"]).lower(),
            "text": (board["prompt"] + " " + board["summary"]).lower(),
        }
        boards.append(board)
        by_id[board["id"]] = board
    return {"boards": boards, "by_id": by_id, "source": payload.get("source") if isinstance(payload.get("source"), dict) else {}}


def load_index(path: Path | None = None) -> dict[str, Any]:
    """Load the bundled index once per file identity (size, mtime, inode)."""
    target = Path(path or INDEX_PATH)
    try:
        key = (str(target), _index_key(target))
    except OSError as exc:
        raise StyleBoardError("style_index_unavailable", "The style index is unavailable") from exc
    with _INDEX_LOCK:
        if _INDEX["key"] == key and _INDEX["value"] is not None:
            return _INDEX["value"]
        try:
            with open(target, "rb") as handle:
                packed = handle.read(INDEX_MAX_BYTES + 1)
            if len(packed) > INDEX_MAX_BYTES:
                raise StyleBoardError("style_index_invalid", "The style index is too large")
            with gzip.GzipFile(fileobj=__import__("io").BytesIO(packed)) as stream:
                raw = stream.read(INDEX_MAX_RAW_BYTES + 1)
            if len(raw) > INDEX_MAX_RAW_BYTES:
                raise StyleBoardError("style_index_invalid", "The style index is too large")
            value = _validated(json.loads(raw.decode("utf-8")))
        except StyleBoardError:
            raise
        except (OSError, EOFError, ValueError, UnicodeDecodeError) as exc:
            raise StyleBoardError("style_index_unavailable", "The style index is unavailable") from exc
        _INDEX["key"], _INDEX["value"] = key, value
        return value


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
_FIELD_WEIGHT = (("title", 8), ("keywords", 4), ("axes", 3), ("text", 1))


def _tokens(query: str) -> list[str]:
    seen: list[str] = []
    for token in re.findall(r"[a-z0-9]+", query.lower()):
        if token not in seen:
            seen.append(token)
    return seen[:12]


def _variants(token: str) -> tuple[str, ...]:
    if len(token) > 3 and token.endswith("s"):
        return (token, token[:-1])
    return (token,)


def search(index: dict[str, Any], query: str, *, limit: int = 4000) -> dict[str, Any]:
    """Every query word must match somewhere; rank by field weight, then title."""
    text = str(query or "")[:QUERY_MAX]
    tokens = _tokens(text)
    boards = index["boards"]
    if not tokens:
        ranked = boards
    else:
        scored: list[tuple[int, str, dict[str, Any]]] = []
        for board in boards:
            fields = board["_search"]
            total = 0
            for token in tokens:
                best = 0
                for field, weight in _FIELD_WEIGHT:
                    haystack = fields[field]
                    if any(variant in haystack for variant in _variants(token)):
                        bonus = 2 if field == "title" and re.search(r"\b" + re.escape(token), haystack) else 0
                        best = max(best, weight + bonus)
                if not best:
                    total = 0
                    break
                total += best
            if total:
                scored.append((-total, fields["title"], board))
        scored.sort(key=lambda item: (item[0], item[1], item[2]["id"]))
        ranked = [item[2] for item in scored]
    items = [{"id": board["id"], "title": board["title"]} for board in ranked[: max(0, min(int(limit), 4000))]]
    return {"query": text, "total": len(ranked), "catalog_total": len(boards), "items": items}


# ---------------------------------------------------------------------------
# Style text (subject-safe)
# ---------------------------------------------------------------------------
_SUBJECT_TERMS = (
    "crowd", "crowds", "people", "person", "persons", "human", "humans", "figure", "figures",
    "man", "woman", "men", "women", "child", "children", "animal", "animals", "lettering",
    "building", "buildings", "architecture", "architectural", "vehicle", "vehicles", "face", "faces",
    "populated", "unpopulated", "empty scene", "empty scenes",
)
_SUBJECT_RE = re.compile(r"\b(?:" + "|".join(re.escape(term) for term in _SUBJECT_TERMS) + r")\b", re.I)
_REWRITES = (
    (re.compile(r"\b(?:a|an|the)\s+(?:black and white|high-contrast|glitchy|cinematic|close-up)?\s*portrait of\s+(?:a|an|the)?\s*[^.]+", re.I), "portrait-style framing for the requested subject"),
    (re.compile(r"\b(?:a|the)\s+lone figure\b", re.I), "a high-contrast silhouette treatment"),
    (re.compile(r"\b(?:lone|solitary|single|isolated)(?:,?\s+\w+){0,2}?\s+(?:silhouette|figure)s?\b", re.I), "high-contrast silhouette treatment"),
    (re.compile(r"\bcentered figure\b", re.I), "center-weighted contrast"),
    (re.compile(r"\bmany people\b", re.I), "multi-subject compositions"),
    (re.compile(r"\bno people\b", re.I), "subject-agnostic compositions"),
    (re.compile(r"\b(?:a|an|the)\s+(?:(?:single|lone|solitary|young|old|elderly)\s+){0,3}(?:woman|man|girl|boy|person|figure|child|face|subject|creature|animal)\b[^.]*", re.I), "the requested subject rendered with the board's palette, lighting, and texture"),
)


def _spaces(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _sentence(text: str) -> str:
    cleaned = _spaces(text)
    return cleaned if not cleaned or cleaned.endswith((".", "!", "?")) else cleaned + "."


def _abstract(text: str) -> str:
    result = str(text or "")
    for pattern, replacement in _REWRITES:
        result = pattern.sub(replacement, result)
    return result


def _prose(text: str) -> str:
    kept = [sentence for sentence in re.split(r"(?<=[.!?])\s+", _spaces(_abstract(text))) if sentence and not _SUBJECT_RE.search(sentence)]
    return " ".join(kept)


def _fragment(text: str) -> str:
    value = _spaces(_abstract(text))
    return "" if _SUBJECT_RE.search(value) else value


def style_text(board: dict[str, Any]) -> str:
    """The board's transferable style: treatment prose plus distinct style terms."""
    prose = _sentence(_prose(board.get("prompt") or ""))
    low = prose.lower()
    terms: list[str] = []
    for raw in [*board.get("keywords", []), *board.get("axes", [])]:
        term = _fragment(raw)
        key = term.lower()
        if term and key not in low and key not in (item.lower() for item in terms):
            terms.append(term)
    parts = [prose, f"Style keywords: {', '.join(terms[:12])}." if terms else ""]
    return " ".join(part for part in parts if part).strip()[:2400]


def detail(index: dict[str, Any], board_id: str) -> dict[str, Any]:
    board = index["by_id"].get(board_id)
    if board is None:
        raise StyleBoardError("style_unknown", "The style is unknown")
    return {
        "id": board["id"], "title": board["title"], "keywords": list(board["keywords"]),
        "axes": list(board["axes"]), "summary": board["summary"], "style": style_text(board),
    }


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401 - urllib hook
        return None


def thumb_url(board: dict[str, Any]) -> str | None:
    relative = board.get("thumb") or ""
    if not THUMB_PATH_RE.fullmatch(relative):
        return None
    return THUMB_HOST + re.sub(r"-(\d{2,4})\.webp$", f"-{THUMB_SIZE}.webp", relative)


def _image_mime(data: bytes) -> str | None:
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def fetch_thumb(url: str) -> bytes:
    """One bounded HTTPS GET from the allowlisted preview host; never follows redirects."""
    if not url.startswith(THUMB_HOST):
        raise StyleBoardError("style_thumb_refused", "The preview host is not allowed")
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "film-suite-style-browser/1", "Accept": "image/webp,image/*"})
    with opener.open(request, timeout=THUMB_TIMEOUT_S) as response:
        if getattr(response, "status", 200) != 200:
            raise StyleBoardError("style_thumb_unavailable", "The preview is unavailable")
        data = response.read(THUMB_MAX_BYTES + 1)
    if not data or len(data) > THUMB_MAX_BYTES:
        raise StyleBoardError("style_thumb_unavailable", "The preview is unavailable")
    return data


class ThumbCache:
    """Local preview cache confined to one directory; names come from ids only."""

    def __init__(self, root: Path | None, fetch: Callable[[str], bytes] = fetch_thumb) -> None:
        self.root = Path(root) if root is not None else None
        self.fetch = fetch
        self._memory: dict[str, bytes] = {}
        self._misses: dict[str, float] = {}
        self._lock = threading.Lock()

    def _path(self, board_id: str) -> Path | None:
        if self.root is None or not BOARD_ID_RE.fullmatch(board_id):
            return None
        path = self.root / f"{board_id}-{THUMB_SIZE}.img"
        if path.parent != self.root:
            return None
        return path

    def _read(self, board_id: str) -> bytes | None:
        with self._lock:
            if board_id in self._memory:
                return self._memory[board_id]
        path = self._path(board_id)
        if path is None:
            return None
        try:
            if path.is_symlink() or not path.is_file():
                return None
            with open(path, "rb") as handle:
                data = handle.read(THUMB_MAX_BYTES + 1)
        except OSError:
            return None
        return data if data and len(data) <= THUMB_MAX_BYTES and _image_mime(data) else None

    def _write(self, board_id: str, data: bytes) -> None:
        with self._lock:
            if len(self._memory) > 512:
                self._memory.clear()
            self._memory[board_id] = data
        path = self._path(board_id)
        if path is None:
            return
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink() or not self.root.is_dir():
                return
            temp = self.root / f".thumb-{secrets.token_hex(8)}.tmp"
            with open(temp, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError:
            try:
                temp.unlink()  # type: ignore[possibly-undefined]
            except Exception:  # noqa: BLE001 - best effort cleanup
                pass

    def _missed(self, board_id: str) -> bool:
        with self._lock:
            at = self._misses.get(board_id)
            if at is None:
                return False
            if time.monotonic() - at < THUMB_MISS_TTL_S:
                return True
            del self._misses[board_id]
            return False

    def _miss(self, board_id: str) -> None:
        with self._lock:
            if len(self._misses) > 8192:
                self._misses.clear()
            self._misses[board_id] = time.monotonic()

    @staticmethod
    def _encode(data: bytes) -> str | None:
        mime = _image_mime(data)
        return None if mime is None else f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    def cached_url(self, board: dict[str, Any]) -> str | None:
        """The preview from memory or disk only; never the network."""
        data = self._read(board["id"])
        return None if data is None else self._encode(data)

    def data_url(self, board: dict[str, Any]) -> str | None:
        board_id = board["id"]
        data = self._read(board_id)
        if data is None:
            url = thumb_url(board)
            if url is None or self._missed(board_id):
                return None
            try:
                data = self.fetch(url)
            except Exception:  # noqa: BLE001 - an unreachable preview is a missing preview
                self._miss(board_id)
                return None
            if not data or len(data) > THUMB_MAX_BYTES or _image_mime(data) is None:
                self._miss(board_id)
                return None
            self._write(board_id, data)
        return self._encode(data)

    def many(self, index: dict[str, Any], ids: list[str], *, cached_only: bool = False) -> dict[str, str | None]:
        """``cached_only`` answers at once from disk and leaves uncached ids out."""
        boards = [index["by_id"][board_id] for board_id in dict.fromkeys(ids) if board_id in index["by_id"]]
        if not boards:
            return {}
        if cached_only:
            found = {board["id"]: self.cached_url(board) for board in boards}
            return {board_id: url for board_id, url in found.items() if url is not None}
        # Disk hits first; only the rest wait on the network.
        out: dict[str, str | None] = {}
        rest = []
        for board in boards:
            url = self.cached_url(board)
            if url is None:
                rest.append(board)
            else:
                out[board["id"]] = url
        if rest:
            with ThreadPoolExecutor(max_workers=min(THUMB_WORKERS, len(rest))) as pool:
                out.update(zip([board["id"] for board in rest], pool.map(self.data_url, rest)))
        return {board["id"]: out.get(board["id"]) for board in boards}

    def warm(self, index: dict[str, Any], ids: list[str] | None = None, *, limit: int = 4000) -> dict[str, int]:
        """Fill the disk cache for boards without a cached preview, at most
        ``limit`` fetches. A one-off maintenance job; never run on a request."""
        boards = [index["by_id"][board_id] for board_id in dict.fromkeys(ids or list(index["by_id"])) if board_id in index["by_id"]]
        cached = [board for board in boards if self._read(board["id"]) is not None]
        todo = [board for board in boards if self._read(board["id"]) is None and thumb_url(board) is not None]
        batch = todo[: max(0, int(limit))]
        results: list[str | None] = []
        if batch:
            with ThreadPoolExecutor(max_workers=min(THUMB_WORKERS, len(batch))) as pool:
                results = list(pool.map(self.data_url, batch))
        fetched = sum(1 for url in results if url)
        return {"cached": len(cached), "fetched": fetched, "missing": len(batch) - fetched, "left": len(todo) - len(batch)}
