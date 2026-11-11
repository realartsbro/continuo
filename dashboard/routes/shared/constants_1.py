from __future__ import annotations
# w20-cat (CATALOG.md #23): env override, else PATH, else <SystemDrive>\\ffmpeg\\bin (generation/ffmpeg_locate.py).
_ffmpeg_locate_spec = importlib.util.spec_from_file_location("film_suite_ffmpeg_locate", Path(__file__).with_name("generation") / "ffmpeg_locate.py")
_ffmpeg_locate = importlib.util.module_from_spec(_ffmpeg_locate_spec)
_ffmpeg_locate_spec.loader.exec_module(_ffmpeg_locate)
FFMPEG_PATH = Path(_ffmpeg_locate.locate("ffmpeg"))
FFPROBE_PATH = Path(_ffmpeg_locate.locate("ffprobe"))
# w39-env: one minimal environment for every child process (dashboard/engine_process): ffmpeg, ffprobe, nvidia-smi and
# the file manager never inherit the backend's environment (provider keys, HERMES_*).
_engine_process_spec = importlib.util.spec_from_file_location("film_suite_engine_process", Path(__file__).with_name("engine_process") / "__init__.py")
_engine_process = importlib.util.module_from_spec(_engine_process_spec)
_engine_process_spec.loader.exec_module(_engine_process)
_child_env = _engine_process.child_env
MEDIA_PROBE_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
MEDIA_EXTENSIONS = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".mp4": "video"}
# Sound files (lane fs-msnd): kind "audio" for exactly the suffixes /projects/timeline/audio/add takes
# (dashboard/timeline/footage.py AUDIO_SUFFIXES: wav mp3 flac opus ogg oga m4a aac), one list for both.
studio_core.ProjectStore._timeline_document()
MEDIA_EXTENSIONS.update({suffix: "audio" for suffix in sorted(importlib.import_module(f"{studio_core.ProjectStore._TIMELINE_PACKAGE}.footage").AUDIO_SUFFIXES)})
# POST /media/list bounds: an approved folder must never be able to stall or crash the gateway.
MEDIA_LIST_MAX_FILES = 500
MEDIA_LIST_MAX_DEPTH = 2  # approved root = depth 0
MEDIA_LIST_TIME_BUDGET_S = 3.0
MEDIA_LIST_SCAN_BUDGET = 20_000  # directory entries looked at across all roots
MEDIA_LIST_SKIP_DIR_NAMES = frozenset(
    {"node_modules", ".git", "__pycache__", "$recycle.bin", "system volume information", "appdata"}
)
MEDIA_PROBE_CACHE_MAX_ENTRIES = 5_000
MEDIA_THUMB_MAX_SIZE = 384
MEDIA_STILL_MAX_SIZE = 1920  # lane w22-cut (CUT-177): a folder still read by ref for Cut's viewer
MEDIA_THUMB_MIN_SIZE = 16
MEDIA_THUMB_SOURCE_MAX_BYTES = 64 * 1024 * 1024
MEDIA_THUMB_CACHE_MAX_ENTRIES = 256
MEDIA_THUMB_CACHE: "collections.OrderedDict[tuple[str, int, int, int], bytes]" = collections.OrderedDict()
_MEDIA_THUMB_LOCK = _thread.allocate_lock()
_WIN_ATTRIBUTE_HIDDEN = 0x2
_WIN_ATTRIBUTE_SYSTEM = 0x4
_WIN_ATTRIBUTE_REPARSE_POINT = 0x400
_WIN_REPARSE_TAGS_NOT_FOLLOWED = frozenset({0xA0000003, 0xA000000C})  # junction (mount point), symlink
PREPARED_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
PREPARED_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
PREPARED_CACHE_MIN_RESIDENCY_SECONDS = 30.0
MAX_DERIVATION_CHAIN_LENGTH = 64
MAX_DERIVATION_TIME_SECONDS = 1_000_000_000.0
MAX_DERIVATION_FRAME = 9_007_199_254_740_991
MAX_DERIVATION_FPS = 1_000.0
MEDIA_STAGE_MAX_AGE_SECONDS = 60 * 60
MUSIC_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
MUSIC_CACHE_MAX_BYTES = 8 * 1024 * 1024 * 1024
MUSIC_PEAKS_PER_SECOND = 200
