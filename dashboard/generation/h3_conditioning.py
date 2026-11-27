"""Pinned MiniMax H3 conditioning targets: profiles, validation and compile.

Two versioned targets (SPEC fs-h3a §2-§5, §12):

* ``minimax-h3-frames-v1`` (fl2va): keyframes 0-10 at any frame; frame 0 ->
  ``first_frame``, the last frame -> ``last_frame``, every other frame ->
  ``MiniMaxH3AddGuide``.
* ``minimax-h3-references-v1`` (ref2va): reference images (<=9), reference
  videos (<=3), keyframes (<=10, all through AddGuide) and an optional
  ``control_video`` through the Fun ControlNet union patch at strength 1.0.

Sound (lane fs-h3aud, measured in fs-h3audio MEASURE.md §2/§5):

* ``reference_audios`` (References only): up to 3 files, each 2-15 s, together
  at most 15 s, each with a role: ``voice_of`` (the timbre of ``<Subject k>``,
  never the words) or ``music_bed`` (the recording plays from the first frame
  under the dialogue).  They need at least one reference picture or clip.
* ``reference_video_sound`` (References only): indices of reference clips whose
  own soundtrack is used (default: none); the soundtrack replaces the shot's
  sound almost verbatim.
* ``sound_track`` (Frames and References): one file anchored at frame 0 through
  ``MiniMaxH3AddGuide.audio``, cut to the shot length.  A mid-shot anchor is
  not offered (frame 36 measured: not copied).

``<Audio N>`` labels follow the node (``comfy_extras/nodes_minimax_h3.py``):
a clip's soundtrack is numbered right before its ``<Video k>``, standalone
reference audio after every video.  The compile adds one subject line and one
retention line per audio reference to the prompt; a request without sound
compiles to exactly the graph and prompt it compiled to before.

The pinned graphs carry every optional branch.  The compiler writes only the
declared pointers and removes the branches of absent roles structurally (no
placeholder ever reaches ComfyUI: AddGuide raises without an image).  This
module is pure: no network, no files except the pinned profile, no ffmpeg.
It is loaded standalone (no package-relative imports).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping


def _load_h3_megapixels():
    """The one H3 megapixel table (``h3_megapixels.py`` next to this file; lane p13-vid VID-005).

    Loaded by path so this module works both as a package member and when loaded alone by path."""
    import hashlib as _hashlib
    import importlib.util as _importlib_util
    import sys as _sys
    from pathlib import Path as _Path

    path = _Path(__file__).with_name("h3_megapixels.py")
    name = "film_suite_h3_megapixels_" + _hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    module = _sys.modules.get(name)
    if module is None:
        spec = _importlib_util.spec_from_file_location(name, path)
        module = _importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _sys.modules[name] = module
    return module


H3_MP = _load_h3_megapixels()

FRAMES_TARGET_ID = "minimax-h3-frames-v1"
REFERENCES_TARGET_ID = "minimax-h3-references-v1"
TARGET_IDS = (FRAMES_TARGET_ID, REFERENCES_TARGET_ID)
PROVIDER_ID = "comfyui-local"
PROMPT_SENTINEL = "__STUDIO_PROMPT__"

# Byte pins: a changed manifest or graph is a new target version, never an edit.
_PINS = {
    FRAMES_TARGET_ID: {
        "manifest_sha256": "78559d7f44539c72131c34631896136a4ff637233227538047dc640d9a069089",
        "api_sha256": "61e89c5862f3f40b9ea1a587ce2a51eec9a35ac8c6168a8a2c1aa74da329c33d",
        "node_count": 41,
    },
    REFERENCES_TARGET_ID: {
        "manifest_sha256": "fed9f6658fc75713eb9ad72762346b82ad5680d2506815e768388bacf7772893",
        "api_sha256": "fec5023410ca37bc8c2df39baabafd7f76c1b3afaeee3ff51b27b24900ce7947",
        "node_count": 61,
    },
}

ASPECT_RATIOS = {
    "21:9 (Ultrawide)": (21, 9),
    "16:9 (Widescreen)": (16, 9),
    "4:3 (Standard)": (4, 3),
    "1:1 (Square)": (1, 1),
    "3:4 (Portrait Standard)": (3, 4),
    "9:16 (Portrait Widescreen)": (9, 16),
}
# Lane p12-vid (VID-005): the official H3 sizes [PL docs.comfy.org minimax-h3]: Draft ~0.4 MP, Native 0.98 MP
# (1344x768 at 16:9), sizes in steps of 32. Native is the official node's adapt_canvas
# (ComfyUI/comfy_extras/nodes_minimax_h3.py:32-34,54-67, copied below as _adapt_canvas); Draft is the same rule with
# the short edge scaled to the Draft area. Saved shots, chains and recipes still carry the old values 0.5 / 1.0:
# they are read as Draft / Native (maintainer review: a mapping on read, never a refusal).
# One list (lane p13-vid VID-005): the numbers come from h3_megapixels (official aliases + stored values).
MEGAPIXELS = tuple(H3_MP.OFFICIAL_ALIASES)
SIZE_TIERS = {value: H3_MP.any_tier(value) for value in MEGAPIXELS}
LEGACY_MEGAPIXELS = {value: H3_MP.any_tier(value) for value in H3_MP.options("minimax-h3-frames-v1")}
MEGAPIXELS_ACCEPTED = (*MEGAPIXELS, *LEGACY_MEGAPIXELS)
# Official node constants (nodes_minimax_h3.py:32-34).
CANVAS_MULTIPLE = 32
BASE_SHORT_EDGE = 768
MAX_PIXELS = 768 * 1344
SAMPLERS = ("er_sde", "res_multistep", "euler")
SCHEDULERS = ("beta", "simple")
SCALAR_KEYS = frozenset(
    {"prompt", "duration_seconds", "seed", "aspect_ratio", "megapixels", "steps", "sampler_name", "scheduler"}
)
MEDIA_KEYS = {
    FRAMES_TARGET_ID: frozenset({"keyframes", "sound_track", "camera"}),
    REFERENCES_TARGET_ID: frozenset({"keyframes", "reference_images", "reference_videos", "control_video",
                                     "reference_audios", "reference_video_sound", "sound_track", "camera"}),
}
LIMITS = {"keyframes": 10, "reference_images": 9, "reference_videos": 3, "control_video": 1, "reference_audios": 3}
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_SUFFIXES = (".mp4",)
AUDIO_SUFFIXES = (".wav", ".mp3", ".flac", ".m4a", ".ogg", ".opus")
MIN_CLIP_FRAMES = 5
# Official reference-audio guidance (the node does not enforce it): each 2-15 s, together <= 15 s.
AUDIO_MIN_SECONDS, AUDIO_MAX_SECONDS, AUDIO_TOTAL_SECONDS = 2.0, 15.0, 15.0
SOUND_TRACK_MIN_SECONDS = 0.5
AUDIO_ROLES = ("voice_of", "music_bed")
MAX_SUBJECT = 9

# Pinned node ids (identical in both graphs where shared).
_GENERATOR = "8"
_SEED = "9"
_SAMPLER = "11"
_SCHEDULER = "12"
_GUIDER = "13"
_FIRST, _LAST = "30", "31"
_KEYFRAME_LOADER_BASE, _GUIDE_BASE, _GUIDE_SLOTS = 60, 70, 10
_REF_IMAGE_BASE, _REF_VIDEO_BASE, _REF_VIDEO_COMPONENTS_BASE = 40, 50, 53
_CONTROL_NODES = ("80", "81", "82", "83")
_REF_AUDIO_BASE = 56
_TRACK_LOADER, _TRACK_GUIDE = "90", "91"
_BASE_MODEL = ["6", 0]


class H3ConditioningError(ValueError):
    """Finite validation error raised before any side effect."""

    reason_code = "invalid_request"


class H3SizeError(H3ConditioningError):
    """A megapixel value that is neither an official size nor a saved legacy value."""

    reason_code = "megapixels_not_offered"


def _fail(message: str) -> H3ConditioningError:
    return H3ConditioningError(message)


# --------------------------------------------------------------------------- frames / canvas


def effective_frames(duration_seconds: int, fps: int = 24) -> int:
    """``round_fps_then_align_modulo``: 17k+5 frames at 24 fps (5 s -> 124)."""
    base = max(5, round(duration_seconds * fps))
    return base + ((5 - base) % 17)


def _adapt_canvas(width: float, height: float, short_edge: float = BASE_SHORT_EDGE) -> tuple[int, int]:
    """Copy of the official ``adapt_canvas`` (ComfyUI/comfy_extras/nodes_minimax_h3.py:54-65): 768-short-edge canvas
    with a 768*1344 area cap, per-axis round to 32. ``short_edge`` other than 768 is the Draft size (same rule)."""
    ratio = width / height
    if ratio >= 1.0:
        nom_w, nom_h = short_edge * ratio, short_edge
    else:
        nom_w, nom_h = short_edge, short_edge / ratio
    cap = MAX_PIXELS * (short_edge / BASE_SHORT_EDGE) ** 2
    if nom_w * nom_h > cap:
        s = math.sqrt(cap / (nom_w * nom_h))
        nom_w, nom_h = nom_w * s, nom_h * s
    return (max(CANVAS_MULTIPLE, round(nom_w / CANVAS_MULTIPLE) * CANVAS_MULTIPLE),
            max(CANVAS_MULTIPLE, round(nom_h / CANVAS_MULTIPLE) * CANVAS_MULTIPLE))


def size_tier(megapixels: Any) -> str:
    """'draft' | 'native' for an official size or a saved legacy value (0.5 -> draft, 1.0 -> native); else refused."""
    if type(megapixels) in (int, float):
        value = float(megapixels)
        if value in SIZE_TIERS:
            return SIZE_TIERS[value]
        if value in LEGACY_MEGAPIXELS:
            return LEGACY_MEGAPIXELS[value]
    raise H3SizeError(f"{H3_MP.CODE}: megapixels must be Draft 0.4 or Native 0.98 (saved 0.5 / 1.0 read as Draft / Native)")


def canvas(aspect_ratio: str, megapixels: float, multiple: int = 32) -> tuple[int, int]:
    """The official H3 canvas: Native = adapt_canvas (1344x768 at 16:9), Draft = the same rule at ~0.4 MP."""
    if aspect_ratio not in ASPECT_RATIOS:
        raise _fail("aspect ratio is not an approved preset")
    w_ratio, h_ratio = ASPECT_RATIOS[aspect_ratio]
    tier = size_tier(megapixels)
    short = BASE_SHORT_EDGE if tier == "native" else BASE_SHORT_EDGE * math.sqrt(MEGAPIXELS[0] / MEGAPIXELS[1])
    return _adapt_canvas(w_ratio, h_ratio, short)


# --------------------------------------------------------------------------- profiles


class H3Profile:
    """Validated pinned profile; every read returns a fresh copy.

    A plain slotted class (not a dataclass) so the module loads standalone
    without being registered in ``sys.modules``.
    """

    __slots__ = ("target_id", "manifest_path", "api_path", "_manifest_json", "_graph_json")

    def __init__(self, *, target_id: str, manifest_path: Path, api_path: Path, _manifest_json: str, _graph_json: str) -> None:
        self.target_id = target_id
        self.manifest_path = manifest_path
        self.api_path = api_path
        self._manifest_json = _manifest_json
        self._graph_json = _graph_json

    @property
    def id(self) -> str:
        return self.target_id

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(self._manifest_json)

    @property
    def graph(self) -> dict[str, Any]:
        return json.loads(self._graph_json)


def _resolve(graph: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise _fail(f"pointer {pointer!r} must be absolute")
    node = graph
    for part in pointer[1:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise _fail(f"graph is missing pointer {pointer}")
        node = node[part]
    return node


def _is_link(value: Any) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int


def validate_pinned_graph(graph: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    """Fixed assertions, model pointers, declared pointers and classes."""
    for pointer, expected in manifest["fixed_assertions"].items():
        actual = _resolve(graph, pointer)
        if actual != expected or type(actual) is not type(expected):
            raise _fail(f"fixed assertion differs at {pointer}")
    for name, model in manifest["models"].items():
        if _resolve(graph, model["assertion_pointer"]) != model["filename"]:
            raise _fail(f"model {name} differs from its pinned file")
    for pointer in manifest["mutable_json_pointers"]:
        if _is_link(_resolve(graph, pointer)):
            raise _fail(f"mutable pointer {pointer} names a link")
    classes = sorted({node["class_type"] for node in graph.values()})
    if classes != sorted(manifest["required_classes"]):
        raise _fail("graph classes differ from required_classes")
    _assert_links_resolve(graph)


def load_h3_profile(target_id: str, workflow_root: Path | str) -> H3Profile:
    if target_id not in _PINS:
        raise _fail(f"unknown H3 conditioning target {target_id!r}")
    pins = _PINS[target_id]
    directory = Path(workflow_root) / target_id
    manifest_path = directory / "manifest.json"
    api_path = directory / "workflow-api.json"
    manifest_raw = manifest_path.read_bytes()
    api_raw = api_path.read_bytes()
    if hashlib.sha256(manifest_raw).hexdigest() != pins["manifest_sha256"]:
        raise _fail("workflow manifest bytes do not match the pinned SHA-256")
    if hashlib.sha256(api_raw).hexdigest() != pins["api_sha256"]:
        raise _fail("workflow API bytes do not match the pinned SHA-256")
    manifest = json.loads(manifest_raw.decode("utf-8"))
    graph = json.loads(api_raw.decode("utf-8"))
    if manifest.get("id") != target_id or manifest.get("provider_id") != PROVIDER_ID:
        raise _fail("workflow manifest names another target")
    if manifest["workflow"]["api_sha256"] != pins["api_sha256"]:
        raise _fail("workflow manifest declares another graph")
    if len(graph) != pins["node_count"] or manifest["workflow"]["node_count"] != pins["node_count"]:
        raise _fail("workflow node count differs from the pin")
    validate_pinned_graph(graph, manifest)
    return H3Profile(
        target_id=target_id,
        manifest_path=manifest_path,
        api_path=api_path,
        _manifest_json=json.dumps(manifest, ensure_ascii=False),
        _graph_json=json.dumps(graph, ensure_ascii=False),
    )


# --------------------------------------------------------------------------- request validation


def _suffix(value: str) -> str:
    dot = value.rfind(".")
    return value[dot:].lower() if dot >= 0 else ""


def _media_text(value: Any, field_name: str, suffixes: tuple[str, ...]) -> str:
    if type(value) is not str or not value.strip():
        raise _fail(f"{field_name} must name a file")
    if _suffix(value) not in suffixes:
        raise _fail(f"{field_name} must be one of {', '.join(suffixes)}")
    return value


def validate_args(target_id: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the exact target allowlist (operational args); return the plan."""
    if target_id not in TARGET_IDS:
        raise _fail("unknown H3 conditioning target")
    if not isinstance(args, Mapping):
        raise _fail("args must be an object")
    controls = None
    if _controls_requested(args):  # lane w2-h3ctl: a request without control keys takes the path before, unchanged
        h3k = _h3_controls()
        steps_value = args.get("steps")
        try:
            if h3k.has_controls(args):  # count / take_index alone change only the seed, never the graph
                controls = h3k.validate_controls(target_id, args, steps_value if type(steps_value) is int else 0)
            h3k.batch_args(args)
        except ValueError as exc:
            error = _fail(str(exc))
            if isinstance(getattr(exc, "diagnostic", None), dict):
                error.diagnostic = exc.diagnostic
            raise error from exc
        args = h3k.split_args(args)[0]
    allowed = SCALAR_KEYS | MEDIA_KEYS[target_id]
    names = set(args)
    if names - allowed:
        raise _fail("args contain an unknown field: " + ", ".join(sorted(map(str, names - allowed))))
    if SCALAR_KEYS - names:
        raise _fail("args are missing: " + ", ".join(sorted(SCALAR_KEYS - names)))
    prompt = args["prompt"]
    if type(prompt) is not str or not prompt.strip() or PROMPT_SENTINEL in prompt:
        raise _fail("prompt must contain text")
    duration = args["duration_seconds"]
    if type(duration) is not int or not 4 <= duration <= 15:
        raise _fail("duration_seconds must be a whole number from 4 to 15")
    seed = args["seed"]
    if type(seed) is not int or not 0 <= seed <= 4294967295:
        raise _fail("seed must be a whole number from 0 to 4294967295")
    aspect = args["aspect_ratio"]
    if type(aspect) is not str or aspect not in ASPECT_RATIOS:
        raise _fail("aspect_ratio is not an approved preset")
    megapixels = args["megapixels"]
    size_tier(megapixels)  # official 0.4 / 0.98, or a saved 0.5 / 1.0 read as Draft / Native; else megapixels_not_offered
    steps = args["steps"]
    if type(steps) is not int or not 1 <= steps <= 100:
        raise _fail("steps must be a whole number from 1 to 100")
    if type(args["sampler_name"]) is not str or args["sampler_name"] not in SAMPLERS:
        raise _fail("sampler_name is not an approved preset")
    if type(args["scheduler"]) is not str or args["scheduler"] not in SCHEDULERS:
        raise _fail("scheduler is not an approved preset")

    frames = effective_frames(duration)
    width, height = canvas(aspect, float(megapixels))
    image, video = IMAGE_SUFFIXES, VIDEO_SUFFIXES

    def source(value: Any, field_name: str, suffixes: tuple[str, ...]) -> str:
        return _media_text(value, field_name, suffixes)

    keyframes_raw = args.get("keyframes", [])
    if type(keyframes_raw) is not list or len(keyframes_raw) > LIMITS["keyframes"]:
        raise _fail("keyframes must be a list of at most 10 entries")
    keyframes: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, entry in enumerate(keyframes_raw):
        if not isinstance(entry, Mapping):
            raise _fail(f"keyframes[{index}] must be an object")
        if set(entry) != {"source", "frame"}:
            raise _fail(f"keyframes[{index}] must contain exactly frame and source")
        frame = entry["frame"]
        if type(frame) is not int or not 0 <= frame <= frames - 1:
            raise _fail(f"keyframes[{index}].frame must be a whole number from 0 to {frames - 1}")
        if frame in seen:
            raise _fail("keyframe frames must be unique")
        seen.add(frame)
        keyframes.append({"index": index, "source": source(entry["source"], f"keyframes[{index}].source", image), "frame": frame})

    def media_list(name: str, suffixes: tuple[str, ...]) -> list[str]:
        raw = args.get(name, [])
        if type(raw) is not list or len(raw) > LIMITS[name]:
            raise _fail(f"{name} must be a list of at most {LIMITS[name]} entries")
        return [source(item, f"{name}[{i}]", suffixes) for i, item in enumerate(raw)]

    sound_track = None
    if "sound_track" in args:
        sound_track = _audio_entry(args["sound_track"], "sound_track", track=True)
    camera = camera_args(args["camera"]) if "camera" in args else None
    if camera is not None and camera_terms_in(prompt):
        raise _fail("the prompt already names a camera move; use one camera term per shot")

    plan: dict[str, Any] = {
        "target_id": target_id,
        "prompt": prompt,
        "duration_seconds": duration,
        "seed": seed,
        "aspect_ratio": aspect,
        "megapixels": float(megapixels),
        "steps": steps,
        "sampler_name": args["sampler_name"],
        "scheduler": args["scheduler"],
        "effective_frames": frames,
        "width": width,
        "height": height,
        "keyframes": keyframes,
        "reference_images": [],
        "reference_videos": [],
        "control_video": None,
        "reference_audios": [],
        "reference_video_sound": [],
        "sound_track": sound_track,
    }
    if camera is not None:
        plan["camera"] = camera
    if controls is not None:
        plan["controls"] = controls
    if target_id == REFERENCES_TARGET_ID:
        plan["reference_images"] = media_list("reference_images", image)
        plan["reference_videos"] = media_list("reference_videos", video)
        if "control_video" in args:
            plan["control_video"] = source(args["control_video"], "control_video", video)
        audios_raw = args.get("reference_audios", [])
        if type(audios_raw) is not list or len(audios_raw) > LIMITS["reference_audios"]:
            raise _fail("reference_audios must be a list of at most 3 entries")
        plan["reference_audios"] = [_audio_entry(entry, f"reference_audios[{i}]") for i, entry in enumerate(audios_raw)]
        if plan["reference_audios"] and not (plan["reference_images"] or plan["reference_videos"]):
            raise _fail("reference_audios need at least one reference picture or clip")
        known = sum(entry["end"] - entry["start"] for entry in plan["reference_audios"] if entry["end"] is not None)
        if known > AUDIO_TOTAL_SECONDS + 1e-6:
            raise _fail("reference_audios may last at most 15 seconds together")
        sound = args.get("reference_video_sound", [])
        if type(sound) is not list or any(type(i) is not int for i in sound):
            raise _fail("reference_video_sound must be a list of reference clip indices")
        if sound != sorted(set(sound)) or any(not 0 <= i < len(plan["reference_videos"]) for i in sound):
            raise _fail("reference_video_sound must name each reference clip at most once, in order")
        plan["reference_video_sound"] = list(sound)
    return plan


def _seconds(value: Any, field_name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or value > 3600:
        raise _fail(f"{field_name} must be a number of seconds from 0 to 3600")
    return float(value)


def _audio_entry(entry: Any, field_name: str, *, track: bool = False) -> dict[str, Any]:
    """One audio medium: ``{source, start?, end?}`` (+ ``role``/``subject`` for a reference)."""
    if not isinstance(entry, Mapping):
        raise _fail(f"{field_name} must be an object")
    allowed = {"source", "start", "end"} | (set() if track else {"role", "subject"})
    if set(entry) - allowed or "source" not in entry:
        raise _fail(f"{field_name} must contain source" + ("" if track else ", role") + " and optional start/end")
    result: dict[str, Any] = {"source": _media_text(entry["source"], f"{field_name}.source", AUDIO_SUFFIXES),
                              "start": _seconds(entry.get("start", 0), f"{field_name}.start"), "end": None}
    if "end" in entry:
        result["end"] = _seconds(entry["end"], f"{field_name}.end")
        if result["end"] <= result["start"]:
            raise _fail(f"{field_name}.end must be after start")
    if track:
        if result["end"] is not None and result["end"] - result["start"] < SOUND_TRACK_MIN_SECONDS:
            raise _fail(f"{field_name} must last at least {SOUND_TRACK_MIN_SECONDS} seconds")
        return result
    role = entry.get("role")
    if role not in AUDIO_ROLES:
        raise _fail(f"{field_name}.role must be voice_of or music_bed")
    result["role"] = role
    if role == "voice_of":
        subject = entry.get("subject")
        if type(subject) is not int or not 1 <= subject <= MAX_SUBJECT:
            raise _fail(f"{field_name}.subject must be a whole number from 1 to {MAX_SUBJECT}")
        result["subject"] = subject
    elif "subject" in entry:
        raise _fail(f"{field_name}.subject belongs to a voice only")
    if result["end"] is not None and not AUDIO_MIN_SECONDS - 1e-6 <= result["end"] - result["start"] <= AUDIO_MAX_SECONDS + 1e-6:
        raise _fail(f"{field_name} must last 2 to 15 seconds")
    return result


def shot_seconds(plan: Mapping[str, Any]) -> float:
    """Length of the shot in seconds (effective frames at 24 fps)."""
    return plan["effective_frames"] / 24.0


# --------------------------------------------------------------------------- camera (lane w1-h3)
#
# The closed vocabulary of fs-cam2 RESULT §4: one H3 camera term per shot, written
# as a sentence (never a bracketed tag), "with <amplitude> amplitude at <speed>
# speed", then one sentence of visible consequence (§3.4).  Measured from a
# keyframe: Arc Shot and Push In move with "large ... slow"; Shake only renders as
# "large ... fast" ("small amplitude" did nothing for Shake, so it is refused).
CAMERA_KEYS = frozenset({"move", "direction", "amplitude", "speed"})
CAMERA_MOVES = {
    "arc_shot": {"directions": ("left", "right"), "amplitudes": ("large", "small"), "speeds": ("slow", "fast"),
                 "default_speed": "slow"},
    "push_in": {"directions": (), "amplitudes": ("large", "small"), "speeds": ("slow", "fast"), "default_speed": "slow"},
    "shake": {"directions": (), "amplitudes": ("large",), "speeds": ("fast",), "default_speed": "fast"},
}
_CAMERA_CONSEQUENCE = {
    "arc_shot": "the subject stays centred and turns to show its {side} side; the foreground slides out of one edge "
                "while the background slides the other way.",
    "push_in": "the subject grows much larger; edge objects slide out of frame.",
    "shake": "the picture bobs up and down like a running operator's handheld camera while it walks forward.",
}
_CAMERA_TAG = re.compile(r"\[(?:arc|push|pull|shake|pan|tilt|truck|pedestal|dolly|zoom|orbit|handheld)[^\]]*\]", re.IGNORECASE)
_SHOT_LABEL = "[Shot 1]"
# The six-field References layout (video-prompts skill §5): the shots sit under detailed_description.
_DETAILED_HEADING = re.compile(r"^[ \t]*detailed_description:", re.IGNORECASE | re.MULTILINE)
_NEXT_HEADING = re.compile(r"^[ \t]*(?:subject_definitions|summary|retention_analysis|overall_soundscape|non_diegetic_music):",
                           re.IGNORECASE | re.MULTILINE)


def camera_args(value: Any) -> dict[str, str]:
    """Validate ``camera = {move, direction?, amplitude?, speed?}`` against the closed vocabulary."""
    if not isinstance(value, Mapping) or set(value) - CAMERA_KEYS or "move" not in value:
        raise _fail("camera must be {move, direction, amplitude, speed}")
    move = value["move"]
    if type(move) is not str or move not in CAMERA_MOVES:
        raise _fail("camera.move must be one of " + ", ".join(CAMERA_MOVES))
    rule = CAMERA_MOVES[move]
    result = {"move": move}
    direction = value.get("direction")
    if rule["directions"]:
        if type(direction) is not str or direction not in rule["directions"]:
            raise _fail(f"camera.direction must be one of {', '.join(rule['directions'])} for {move}")
        result["direction"] = direction
    elif direction is not None:
        raise _fail(f"{move} takes no camera.direction")
    amplitude = value.get("amplitude", "large")
    if type(amplitude) is not str or amplitude not in rule["amplitudes"]:
        raise _fail(f"camera.amplitude must be {' or '.join(rule['amplitudes'])} for {move}")
    speed = value.get("speed", rule["default_speed"])
    if type(speed) is not str or speed not in rule["speeds"]:
        raise _fail(f"camera.speed must be {' or '.join(rule['speeds'])} for {move}")
    result.update(amplitude=amplitude, speed=speed)
    return result


def camera_terms_in(prompt: str) -> bool:
    """The prompt already carries a camera instruction, a bracketed camera tag or a second shot."""
    return ("the camera performs" in prompt.lower() or _CAMERA_TAG.search(prompt) is not None
            or re.search(r"\[Shot [2-9]\d*\]", prompt) is not None)


def camera_sentence(camera: Mapping[str, str]) -> str:
    move = camera["move"]
    term = {"arc_shot": f"an Arc Shot to the {camera.get('direction')}", "push_in": "a Push In", "shake": "a Shake"}[move]
    side = "right" if camera.get("direction") == "left" else "left"
    consequence = _CAMERA_CONSEQUENCE[move].format(side=side)
    return f"The camera performs {term} with {camera['amplitude']} amplitude at {camera['speed']} speed: {consequence}"


def _detailed_description_span(prompt: str) -> tuple[int, int] | None:
    """``(body start, section end)`` of a six-field ``detailed_description:`` section, or None."""
    heading = _DETAILED_HEADING.search(prompt)
    if heading is None:
        return None
    following = _NEXT_HEADING.search(prompt, heading.end())
    return heading.end(), following.start() if following else len(prompt)


def camera_prompt(plan: Mapping[str, Any]) -> str:
    """The artist's prompt with the compiled camera (RESULT §4); unchanged without ``camera``.

    The keyframe alignment line opens the prompt only on Frames when a keyframe
    sits at frame 0 (``<Picture 1>`` is that still; on References ``<Picture 1>``
    is the first reference picture and a keyframe is an unlabelled guide).  The
    camera sentence follows the first ``[Shot 1]`` label, or opens the shot text
    when the prompt has none.  In a six-field prompt the shots live in
    ``detailed_description``: the sentence follows the ``[Shot 1]`` there (an
    earlier ``[Shot 1]`` in ``retention_analysis`` is a reference, not the shot),
    or opens that section's text when it has no label.
    """
    camera = plan.get("camera")
    prompt = plan["prompt"]
    if not camera:
        return prompt
    newline = "\r\n" if "\r\n" in prompt else "\n"
    sentence = camera_sentence(camera)
    span = _detailed_description_span(prompt)
    if span is not None:
        start, end = span
        at = prompt.find(_SHOT_LABEL, start, end)
        if at < 0:
            lead = len(prompt[start:end]) - len(prompt[start:end].lstrip())
            at = start + lead
            body = prompt[:at] + sentence + " " + prompt[at:]
        else:
            rest = prompt[at + len(_SHOT_LABEL):].lstrip(" ")
            body = prompt[:at] + _SHOT_LABEL + " " + sentence + (" " + rest if rest and not rest.startswith(("\n", "\r")) else rest)
    elif (at := prompt.find(_SHOT_LABEL)) >= 0:
        rest = prompt[at + len(_SHOT_LABEL):].lstrip(" ")
        body = prompt[:at] + _SHOT_LABEL + " " + sentence + (" " + rest if rest and not rest.startswith(("\n", "\r")) else rest)
    else:
        body = sentence + " " + prompt.lstrip(" ")
    if plan.get("target_id", FRAMES_TARGET_ID) == FRAMES_TARGET_ID and any(entry["frame"] == 0 for entry in plan.get("keyframes", ())):
        body = ("For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."
                + newline + newline + body)
    return body


def audio_labels(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``<Audio N>`` in the order the node emits them: each clip soundtrack right
    before its ``<Video k>``, then the standalone references (nodes_minimax_h3.py)."""
    labels: list[dict[str, Any]] = []
    for k in plan["reference_video_sound"]:
        labels.append({"key": f"reference_videos/{k}", "label": f"<Audio {len(labels) + 1}>", "role": "clip_sound", "video": f"<Video {k + 1}>"})
    for i, entry in enumerate(plan["reference_audios"]):
        labels.append({"key": f"reference_audios/{i}", "label": f"<Audio {len(labels) + 1}>", "role": entry["role"], "subject": entry.get("subject")})
    return labels


def audio_prompt_lines(plan: Mapping[str, Any]) -> dict[str, list[str]]:
    """Role text per MEASURE §5: one subject line and one retention line per audio reference."""
    subjects, retention = [], []
    for item in audio_labels(plan):
        tag = item["label"]
        if item["role"] == "voice_of":
            who = f"<Subject {item['subject']}>"
            subjects.append(f"{tag} is the voice-timbre reference for {who} (S{item['subject']}).")
            retention.append(f"{tag}: reference - {who} speaks with {tag}'s voice timbre without copying the original signal or words.")
        elif item["role"] == "music_bed":
            subjects.append(f"{tag} is the background music of this shot.")
            retention.append(f"{tag}: partially_copy - the source audio is reused purely as the background music layer, "
                             "leaving room for the model to generate and mix new foreground diegetic dialogue and sound effects on top.")
        else:
            subjects.append(f"{tag} is the soundtrack of {item['video']}.")
            retention.append(f"{tag}: fully_copy - the soundtrack of {item['video']} is reused unchanged as the sound of this shot.")
    return {"subject_definitions": subjects, "retention_analysis": retention}


def _section_end(lines: list[str], heading: str) -> int | None:
    """Index after the last non-empty line of a ``heading:`` section, or None."""
    start = next((i for i, line in enumerate(lines) if line.strip().lower() == heading + ":"), None)
    if start is None:
        return None
    end = start + 1
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.endswith(":") and " " not in stripped:
            break
        if stripped:
            end = i + 1
    return end


def prompt_with_audio(prompt: str, plan: Mapping[str, Any]) -> str:
    """The prompt the node receives: the artist's words plus the audio role lines.

    A line whose ``<Audio N>`` the section already names is not added (the artist
    wrote it).  Without a section heading the lines open the prompt.  Without
    audio references the prompt is returned unchanged.  The sound track adds no
    line: its spoken words stay in the artist's ``<d>`` line.
    """
    wanted = audio_prompt_lines(plan)
    if not any(wanted.values()):
        return prompt
    newline = "\r\n" if "\r\n" in prompt else "\n"
    lines = prompt.split(newline)
    head: list[str] = []
    for heading in ("subject_definitions", "retention_analysis"):
        end = _section_end(lines, heading)
        if end is None:
            head += [heading + ":", *wanted[heading], ""]
            continue
        start = next(i for i, line in enumerate(lines) if line.strip().lower() == heading + ":")
        section = newline.join(lines[start:end])
        missing = [line for line in wanted[heading] if line[: line.index(">") + 1] not in section]
        lines[end:end] = missing
    return newline.join(head + lines)


def keyframe_slots(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Map each keyframe to ``first`` / ``last`` / ``guide`` (frames-v1) or ``guide``."""
    last = plan["effective_frames"] - 1
    slots = []
    for entry in sorted(plan["keyframes"], key=lambda item: item["frame"]):
        if plan["target_id"] == FRAMES_TARGET_ID and entry["frame"] == 0:
            role = "first_frame"
        elif plan["target_id"] == FRAMES_TARGET_ID and entry["frame"] == last:
            role = "last_frame"
        else:
            role = "guide"
        slots.append({**entry, "slot": role})
    return slots


def media_items(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every medium the compile needs, with a stable key and its fit rule."""
    items: list[dict[str, Any]] = []
    for entry in keyframe_slots(plan):
        items.append({"key": f"keyframes/{entry['index']}", "role": "keyframes", "kind": "image", "fit": "cover",
                      "source": entry["source"], "frame": entry["frame"], "slot": entry["slot"]})
    for i, value in enumerate(plan["reference_images"]):
        items.append({"key": f"reference_images/{i}", "role": "reference_images", "kind": "image", "fit": "keep", "source": value})
    for i, value in enumerate(plan["reference_videos"]):
        item = {"key": f"reference_videos/{i}", "role": "reference_videos", "kind": "video", "fit": "keep", "source": value}
        if i in plan.get("reference_video_sound", ()):
            item["sound"] = True
        items.append(item)
    if plan["control_video"] is not None:
        items.append({"key": "control_video/0", "role": "control_video", "kind": "video", "fit": "cover", "source": plan["control_video"]})
    for i, entry in enumerate(plan.get("reference_audios", ())):
        items.append({"key": f"reference_audios/{i}", "role": "reference_audios", "kind": "audio", "fit": "reference",
                      "source": entry["source"], "start": entry["start"], "end": entry["end"], "audio_role": entry["role"]})
    if plan.get("sound_track") is not None:
        track = plan["sound_track"]
        items.append({"key": "sound_track/0", "role": "sound_track", "kind": "audio", "fit": "track", "source": track["source"],
                      "start": track["start"], "end": track["end"], "frame": 0, "limit_seconds": shot_seconds(plan)})
    return items


# --------------------------------------------------------------------------- compile


def _assert_links_resolve(graph: Mapping[str, Any]) -> None:
    for node_id, node in graph.items():
        for name, value in node["inputs"].items():
            if _is_link(value) and value[0] not in graph:
                raise _fail(f"node {node_id} input {name} points at a removed node")


def _remove(graph: dict[str, Any], *node_ids: str) -> None:
    for node_id in node_ids:
        graph.pop(node_id, None)


def compile_h3_conditioning_request(
    *,
    graph: Mapping[str, Any],
    manifest: Mapping[str, Any],
    args: Mapping[str, Any],
    media: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    """Clone the pinned graph, write declared pointers, remove absent branches.

    ``media`` maps each :func:`media_items` key to ``{"name": "<file> [temp]",
    "sha256": ...}`` (the uploaded, pre-fitted file).  Returns ``{graph,
    requested, effective_frames, canvas, media}``.
    """
    target_id = manifest.get("id")
    validate_pinned_graph(graph, manifest)
    plan = validate_args(target_id, args)
    items = media_items(plan)
    if set(media) != {item["key"] for item in items}:
        raise _fail("uploaded media do not match the request")
    for key, value in media.items():
        name = value.get("name") if isinstance(value, Mapping) else None
        if type(name) is not str or not name.endswith(" [temp]") or "/" in name or "\\" in name or name.startswith("__"):
            raise _fail(f"uploaded medium {key} has no temp name")

    g = copy.deepcopy(dict(graph))
    gen = g[_GENERATOR]["inputs"]
    gen["prompt"] = prompt_with_audio(camera_prompt(plan), plan)
    gen["width"], gen["height"], gen["length"] = plan["width"], plan["height"], plan["effective_frames"]
    g[_SEED]["inputs"]["value"] = plan["seed"]
    g[_SCHEDULER]["inputs"]["steps"] = plan["steps"]
    g[_SCHEDULER]["inputs"]["scheduler"] = plan["scheduler"]
    g[_SAMPLER]["inputs"]["sampler_name"] = plan["sampler_name"]

    guides = []
    for item in items:
        if item["role"] != "keyframes":
            continue
        name = media[item["key"]]["name"]
        if item["slot"] == "first_frame":
            g[_FIRST]["inputs"]["image"] = name
        elif item["slot"] == "last_frame":
            g[_LAST]["inputs"]["image"] = name
        else:
            guides.append((item["frame"], name))
    slots = [item["slot"] for item in items if item["role"] == "keyframes"]
    if target_id == FRAMES_TARGET_ID:
        if "first_frame" not in slots:
            _remove(g, _FIRST)
            del gen["first_frame"]
        if "last_frame" not in slots:
            _remove(g, _LAST)
            del gen["last_frame"]
    tail = [_GENERATOR, 0]
    for k in range(_GUIDE_SLOTS):
        loader, guide = str(_KEYFRAME_LOADER_BASE + k), str(_GUIDE_BASE + k)
        if k < len(guides):
            g[loader]["inputs"]["image"] = guides[k][1]
            g[guide]["inputs"]["frame_idx"] = guides[k][0]
            g[guide]["inputs"]["positive"] = list(tail)
            tail = [guide, 0]
        else:
            _remove(g, loader, guide)
    if plan["sound_track"] is not None:
        g[_TRACK_LOADER]["inputs"]["audio"] = media["sound_track/0"]["name"]
        g[_TRACK_GUIDE]["inputs"]["frame_idx"] = 0
        g[_TRACK_GUIDE]["inputs"]["positive"] = list(tail)
        tail = [_TRACK_GUIDE, 0]
    else:
        _remove(g, _TRACK_LOADER, _TRACK_GUIDE)
    g[_GUIDER]["inputs"]["conditioning"] = tail

    if target_id == REFERENCES_TARGET_ID:
        refs = [media[i["key"]]["name"] for i in items if i["role"] == "reference_images"]
        clips = [media[i["key"]]["name"] for i in items if i["role"] == "reference_videos"]
        for i in range(LIMITS["reference_images"]):
            if i < len(refs):
                g[str(_REF_IMAGE_BASE + i)]["inputs"]["image"] = refs[i]
            else:
                _remove(g, str(_REF_IMAGE_BASE + i))
                del gen[f"ref_images.ref_image_{i}"]
        for i in range(LIMITS["reference_videos"]):
            if i < len(clips):
                g[str(_REF_VIDEO_BASE + i)]["inputs"]["file"] = clips[i]
            else:
                _remove(g, str(_REF_VIDEO_BASE + i), str(_REF_VIDEO_COMPONENTS_BASE + i))
                del gen[f"ref_videos.ref_video_{i}"]
            if i not in plan["reference_video_sound"]:
                del gen[f"ref_video_audios.ref_video_audio_{i}"]
        sounds = [media[i["key"]]["name"] for i in items if i["role"] == "reference_audios"]
        for i in range(LIMITS["reference_audios"]):
            if i < len(sounds):
                g[str(_REF_AUDIO_BASE + i)]["inputs"]["audio"] = sounds[i]
            else:
                _remove(g, str(_REF_AUDIO_BASE + i))
                del gen[f"ref_audios.ref_audio_{i}"]
        if plan["control_video"] is not None:
            g["81"]["inputs"]["file"] = media["control_video/0"]["name"]
        else:
            _remove(g, *_CONTROL_NODES)
            g[_SCHEDULER]["inputs"]["model"] = list(_BASE_MODEL)
            g[_GUIDER]["inputs"]["model"] = list(_BASE_MODEL)

    _assert_links_resolve(g)
    required = set(manifest["required_classes"])
    for node_id, node in g.items():
        if node["class_type"] not in required:
            raise _fail(f"node {node_id} class is not pinned")
        for value in node["inputs"].values():
            if isinstance(value, str) and (value.startswith("__") or PROMPT_SENTINEL in value):
                raise _fail(f"node {node_id} still carries a placeholder")
    canvas_out = {"width": plan["width"], "height": plan["height"]}
    if plan.get("controls") is not None:
        # lane w2-h3ctl: the controls change the compiled graph structurally (never a second pinned graph).
        h3k = _h3_controls()
        g = h3k.apply_controls(g, plan["controls"], steps=plan["steps"])
        canvas_out = h3k.canvas_after(plan["controls"], canvas_out)

    labels = {item["key"]: item["label"] for item in audio_labels(plan)}
    requested = {key: plan[key] for key in ("duration_seconds", "seed", "aspect_ratio", "megapixels", "steps", "sampler_name", "scheduler")}
    if plan.get("camera"):
        requested["camera"] = dict(plan["camera"])
    if plan.get("controls") is not None:
        requested["controls"] = _h3_controls().requested(plan["controls"])
    return {
        "graph": g,
        "requested": requested,
        "effective_frames": plan["effective_frames"],
        "canvas": canvas_out,
        "media": [
            {"role": item["role"], "key": item["key"], "frame": item.get("frame"), "slot": item.get("slot"),
             "sha256": media[item["key"]].get("sha256"),
             **({"audio": labels[item["key"]]} if item["key"] in labels else {}),
             **({"audio_role": item["audio_role"]} if "audio_role" in item else {})}
            for item in items
        ],
        "prompt_sent": gen["prompt"],
    }


# --------------------------------------------------------------------------- live schema health


def _section(info: Mapping[str, Any], class_type: str) -> dict[str, Any]:
    node = info.get(class_type) if isinstance(info, Mapping) else None
    inputs = node.get("input") if isinstance(node, Mapping) else None
    if not isinstance(inputs, Mapping):
        return {}
    merged: dict[str, Any] = {}
    for part in ("required", "optional"):
        section = inputs.get(part)
        if isinstance(section, Mapping):
            merged.update(section)
    return merged


def _options(spec: Any) -> set[Any]:
    if not isinstance(spec, list) or not spec:
        return set()
    if isinstance(spec[0], list):  # legacy combo: [[options], {...}]
        values = spec[0]
    elif len(spec) > 1 and isinstance(spec[1], Mapping) and isinstance(spec[1].get("options"), list):
        values = spec[1]["options"]
    else:
        return set()
    try:
        return set(values)
    except TypeError:
        return set()


def _bounds(spec: Any) -> Mapping[str, Any]:
    if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], Mapping):
        return spec[1]
    return {}


def live_schema_problem(rule: Mapping[str, Any], info: Mapping[str, Any]) -> str | None:
    """Check one manifest ``live_schema`` rule against a live ``/object_info`` answer.

    Autogrow groups are read in the shape ComfyUI 0.41.0 serves them (read
    live once, 19.10.): ``["COMFY_AUTOGROW_V3", {"template": {"input":
    {"required": {<slot>: [...]}}, "prefix": "ref_image_", "min": 0, "max": 9}}]``:
    prefix and bounds sit inside ``template``.
    """
    spec = _section(info, rule["class_type"]).get(rule["input"])
    if spec is None:
        return "missing_input"
    kind = rule["kind"]
    if kind == "present":
        return None
    if kind == "combo":
        return None if set(rule["includes"]) <= _options(spec) else "combo_options"
    if kind == "range":
        bounds = _bounds(spec)
        low, high = bounds.get("min"), bounds.get("max")
        if type(low) not in (int, float) or type(high) not in (int, float):
            return "range_bounds"
        return None if low <= rule["covers"][0] and high >= rule["covers"][1] else "range_bounds"
    if kind == "autogrow":
        if not isinstance(spec, list) or len(spec) < 2 or spec[0] != "COMFY_AUTOGROW_V3" or not isinstance(spec[1], Mapping):
            return "autogrow_shape"
        template = spec[1].get("template")
        if not isinstance(template, Mapping) or not isinstance(template.get("input"), Mapping):
            return "autogrow_shape"
        slots = template["input"].get("required")
        if template.get("prefix") != rule["prefix"] or not isinstance(slots, Mapping) or rule["slot"] not in slots:
            return "autogrow_shape"
        maximum = template.get("max")
        return None if type(maximum) is int and maximum >= rule["max_at_least"] else "autogrow_max"
    return "unknown_rule"


_EMPTY_CHECKS = ("reachable", "runtime_version", "required_classes", "input_schema", "required_models", "workflow_integrity")


async def conditioning_static_health(
    *,
    graph: Mapping[str, Any],
    manifest: Mapping[str, Any],
    client: Any,
    health_type: Any,
    client_error: type[BaseException],
    tested: Any = (),
    policy: Any = None,
) -> Any:
    """Static health for a pinned conditioning target; never queues work.

    The six v1 checks, plus: every model (incl. the turbo LoRA in ``loras``
    and the Fun ControlNet patch in ``model_patches``) is listed live, and
    every ``live_schema`` rule holds against the live ``/object_info``.  The
    runtime stage follows ``runtime_policy`` (``policy``, loaded by path when
    omitted): an untested runtime runs when the live node schema matches the
    manifest's ``schema_fingerprint``.
    """
    policy = policy or _runtime_policy()
    checks = {name: False for name in _EMPTY_CHECKS}
    provider_id, target_id = manifest["provider_id"], manifest["id"]
    runtime: dict[str, Any] = {}

    def answer(state: str, reason: str | None, detail: str | None = None) -> Any:
        extra = dict(runtime)
        if detail is not None:
            extra["reason_detail"] = detail
        return health_type(provider_id=provider_id, target_id=target_id, state=state, checks=dict(checks),
                           inference_tested=False, reason_code=reason, **extra)

    try:
        validate_pinned_graph(graph, manifest)
    except (H3ConditioningError, KeyError, TypeError):
        return answer("misconfigured", "workflow_drift")
    fingerprint = policy.effective_fingerprint(manifest, graph)
    if manifest["runtime_compatibility"].get("policy") == policy.SCHEMA_VERIFIED and fingerprint is None:
        return answer("misconfigured", "workflow_drift")
    checks["workflow_integrity"] = True
    try:
        stats = await client.system_stats()
        await client.features()
        checks["reachable"] = True
        identity = policy.runtime_identity(stats)
        verdict = policy.runtime_verdict(manifest, identity, tested)
        if identity is not None:
            runtime.update(runtime=identity, runtime_tested=verdict == "tested")
        if verdict == "blocked":
            return answer("unverified_runtime", "unverified_runtime")
        checks["runtime_version"] = True
        infos: dict[str, Any] = {}
        for class_type in manifest["required_classes"]:
            info = await client.object_info(class_type)
            if not isinstance(info, Mapping) or class_type not in info:
                return answer("incompatible", "missing_node", policy.detail(class_type, None))
            infos[class_type] = info
        checks["required_classes"] = True
        if fingerprint is not None:
            problem = policy.schema_problem(fingerprint, {name: info[name] for name, info in infos.items()})
            if problem is not None:
                class_type, name, kind = problem
                return answer("incompatible", "missing_node" if kind == "missing_node" else "incompatible_node_schema",
                              policy.detail(class_type, name))
        for rule in manifest["live_schema"]:
            info = infos.get(rule["class_type"])
            if info is None:
                info = await client.object_info(rule["class_type"])
            if live_schema_problem(rule, info) is not None:
                return answer("incompatible", "incompatible_node_schema", policy.detail(rule["class_type"], rule["input"]))
        checks["input_schema"] = True
        for model in manifest["models"].values():
            files = await client.models(model["folder"])
            if not isinstance(files, list) or model["filename"] not in files:
                return answer("misconfigured", "missing_model")
        checks["required_models"] = True
    except client_error:
        runtime.clear()
        return answer("unreachable", "unreachable")
    return answer("ready_static", None)


_H3_CONTROLS: list[Any] = []


def _controls_requested(args: Any) -> bool:
    """Any control or batch key: the keys are h3_controls.CONTROL_KEYS | BATCH_KEYS (one place, w46-b)."""
    if not isinstance(args, Mapping):
        return False
    h3k = _h3_controls()
    return any(key in args for key in h3k.CONTROL_KEYS | h3k.BATCH_KEYS)


def _h3_controls() -> Any:
    """generation/h3_controls.py, loaded by path once (this module loads standalone)."""
    if not _H3_CONTROLS:
        import importlib.util

        spec = importlib.util.spec_from_file_location("film_suite_h3_controls", Path(__file__).with_name("h3_controls.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _H3_CONTROLS.append(module)
    return _H3_CONTROLS[0]


def _runtime_policy() -> Any:
    """generation/runtime_policy.py, loaded by path (this module loads standalone)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("film_suite_h3_runtime_policy", Path(__file__).with_name("runtime_policy.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


__all__ = [
    "FRAMES_TARGET_ID",
    "REFERENCES_TARGET_ID",
    "TARGET_IDS",
    "AUDIO_SUFFIXES",
    "H3ConditioningError",
    "H3Profile",
    "audio_labels",
    "audio_prompt_lines",
    "canvas",
    "compile_h3_conditioning_request",
    "effective_frames",
    "conditioning_static_health",
    "keyframe_slots",
    "live_schema_problem",
    "load_h3_profile",
    "media_items",
    "prompt_with_audio",
    "shot_seconds",
    "validate_args",
    "validate_pinned_graph",
]
