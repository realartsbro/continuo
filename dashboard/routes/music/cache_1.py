from __future__ import annotations


def _confined_music_path(path: str, approved_roots: list[str]) -> Path:
    try:
        return _confined_local_path(path, approved_roots)
    except RecipeError as exc:
        raise (music_analysis.MusicAnalysisError if music_analysis is not None else ValueError)(str(exc)) from exc


def _music_cache_dir() -> Path:
    return music_analysis._cache_dir()


def _music_cache_paths(source: Path) -> tuple[str, Path, Path]:
    digest = music_analysis._sha256_file(source)
    directory = _music_cache_dir()
    return digest, directory / f"{digest}.json", directory / f"{digest}.wav"


def evict_analysis_cache(
    directory: Path | None = None,
    protected: set[str] | None = None,
    now: float | None = None,
) -> None:
    """Evict analysis/proxy pairs together, never touching the current digest."""
    cache_dir = directory or _music_cache_dir()
    protected_stems = set(protected or set())
    stamp = time.time() if now is None else now
    grouped: dict[str, list[Path]] = {}
    for suffix in (".json", ".wav"):
        for candidate in cache_dir.glob(f"*{suffix}"):
            # Lane p13b-aud item 2: <sha>.<engine>.json belongs to the same entry as <sha>.json / <sha>.wav.
            grouped.setdefault(candidate.name.split(".", 1)[0], []).append(candidate)

    def entry_stats(paths: list[Path]) -> tuple[int, float, float] | None:
        stats = []
        for path in paths:
            try:
                stats.append(path.stat())
            except OSError:
                continue
        if not stats:
            return None
        return (
            sum(stat.st_size for stat in stats),
            max(stat.st_mtime for stat in stats),
            min(stat.st_atime for stat in stats),
        )

    entries: list[tuple[str, list[Path], int, float, float]] = []
    for stem, paths in grouped.items():
        stats = entry_stats(paths)
        if stats is not None:
            entries.append((stem, paths, *stats))

    for stem, paths, _size, modified, _accessed in entries:
        if stem in protected_stems or stamp - modified <= MUSIC_CACHE_MAX_AGE_SECONDS:
            continue
        for path in paths:
            path.unlink(missing_ok=True)

    remaining: list[tuple[str, list[Path], int, float, float]] = []
    for stem, paths in grouped.items():
        stats = entry_stats(paths)
        if stats is not None:
            remaining.append((stem, paths, *stats))
    total = sum(size for _stem, _paths, size, _modified, _accessed in remaining)
    for stem, paths, size, _modified, _accessed in sorted(remaining, key=lambda item: (item[4], item[3])):
        if total <= MUSIC_CACHE_MAX_BYTES or stem in protected_stems:
            continue
        for path in paths:
            path.unlink(missing_ok=True)
        total -= size


def _read_music_cache_only(source: Path) -> dict[str, Any] | None:
    digest, cache_file, proxy_file = _music_cache_paths(source)
    try:
        with cache_file.open("r", encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not music_analysis._is_valid_cache_entry(cached, digest):
        return None
    if not proxy_file.is_file() or proxy_file.stat().st_size <= 0:
        return None
    os.utime(cache_file, None)
    os.utime(proxy_file, None)
    return cached


def _ensure_music_proxy(source: Path, proxy_file: Path) -> Path:
    if proxy_file.is_file() and proxy_file.stat().st_size >= 44:
        try:
            with proxy_file.open("rb") as handle:
                header = handle.read(12)
            if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
                os.utime(proxy_file, None)
                return proxy_file
        except OSError:
            pass
        proxy_file.unlink(missing_ok=True)

    temporary = proxy_file.with_name(f".{proxy_file.name}.tmp-{uuid.uuid4().hex}")
    command = [
        str(music_analysis.FFMPEG if music_analysis is not None else FFMPEG_PATH),  # w20-cat: one ffmpeg rule (ffmpeg_locate)
        "-v",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(temporary),
    ]
    try:
        subprocess.run(command, capture_output=True, text=True, encoding="utf-8", check=True, timeout=600, env=_child_env())
        if not temporary.is_file() or temporary.stat().st_size < 44:
            raise RecipeError("The playback proxy was not created.")
        os.replace(temporary, proxy_file)
        return proxy_file
    except FileNotFoundError as exc:
        raise RecipeError("The local audio service is unavailable.") from exc
    except (OSError, subprocess.CalledProcessError, subprocess.SubprocessError) as exc:
        raise RecipeError("The playback proxy could not be created.") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _music_error(exc: BaseException) -> str:
    if isinstance(exc, ModuleNotFoundError) and exc.name:
        # P3S-12 (AUD-050/054): a missing engine package is a fact, named by its top-level module.
        return f"{exc.name.split('.')[0]} not installed"
    message = str(exc)
    lowered = message.lower()
    if "outside every approved root" in lowered or "approved root" in lowered or "unc paths" in lowered:
        return message
    if any(token in lowered for token in ("no audio", "no streams", "audio stream", "matches no stream")):
        return "This file has no audio."
    if any(token in lowered for token in ("too short", "at least 3", "minimum duration")):
        return "That file is too short to analyse."
    if any(token in lowered for token in ("does not exist", "could not read", "not found", "file is empty")):
        return "That track could not be found."
    return "That track could not be analysed."


def _music_metadata(analysis: dict[str, Any], proxy: Path) -> dict[str, Any]:
    metadata = {key: value for key, value in analysis.items() if key != "peaks"}
    metadata.update(
        {
            "ok": True,
            "summary": music_analysis.summarize(analysis),
            "proxy_path": str(proxy),
            "proxy_bytes": proxy.stat().st_size,
            "schema": music_analysis.SCHEMA,
        }
    )
    return metadata
