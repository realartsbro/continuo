"""studio.video-step/1: what a video target can do as the last step of a shot chain (lane w9-chain).

One manifest per chain-capable video target. Every number comes from the target's own module and its
catalog entry at call time (for local MiniMax H3: ``effective_frames``, ``LIMITS``, ``MEDIA_KEYS``,
``keyframe_slots`` and the registry controls), never from a copy here: a changed module changes the
manifest. ``support`` answers whether one concrete request (duration or frames, keyframes at frames or
seconds, a motion guide) fits a target, with plain reasons when it does not. It never compiles a graph,
never uploads and never talks to the engine; files are only probed.

Standalone module (no package-relative imports): the route unit passes the target module, the catalog,
the evidence file and the probe/confine helpers in.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA = "studio.video-step/1"
SUPPORT_SCHEMA_KEYS = ("supported", "reasons", "effective")
# w39-p2-tester item 4: the one configured ComfyUI address (generation/comfy_endpoint.py, loaded by path).
import importlib.util as _endpoint_util
from pathlib import Path as _EndpointPath
_endpoint_spec = _endpoint_util.spec_from_file_location("film_suite_comfy_endpoint", _EndpointPath(__file__).resolve().parents[1] / "generation" / "comfy_endpoint.py")
_endpoint = _endpoint_util.module_from_spec(_endpoint_spec)
_endpoint_spec.loader.exec_module(_endpoint)


class StepSource:
    """A chain-capable target family: its module and how to read the manifest values out of it."""

    def __init__(self, module: Any) -> None:
        self.module = module

    @property
    def target_ids(self) -> tuple[str, ...]:
        return tuple(self.module.TARGET_IDS)

    def frames_for(self, seconds: int, fps: int) -> int:
        return int(self.module.effective_frames(seconds, fps))

    def grid(self, fps: int, min_seconds: int, max_seconds: int) -> dict[str, Any]:
        values = [self.frames_for(s, fps) for s in range(min_seconds, max_seconds + 1)]
        modulus = 0
        for a, b in zip(values, values[1:]):
            modulus = math.gcd(modulus, b - a)
        modulus = modulus or 1
        remainders = {v % modulus for v in values}
        doc = (getattr(self.module.effective_frames, "__doc__", "") or "").lower()
        rule = "round_then_align" if "round" in doc and "align" in doc else "custom"
        return {"fps": fps, "rule": rule, "modulus": modulus, "remainder": remainders.pop() if len(remainders) == 1 else None,
                "min_seconds": min_seconds, "max_seconds": max_seconds}

    def keyframe_max(self) -> int:
        return int(self.module.LIMITS["keyframes"])

    def mapping(self, target_id: str) -> str:
        probe = {"target_id": target_id, "effective_frames": 3,
                 "keyframes": [{"index": 0, "source": "a", "frame": 0}, {"index": 1, "source": "b", "frame": 2}]}
        slots = {entry["slot"] for entry in self.module.keyframe_slots(probe)}
        return "first_last_guides" if slots & {"first_frame", "last_frame"} else "guides"

    def guide_max(self, target_id: str) -> int:
        return int(self.module.LIMITS["control_video"]) if "control_video" in self.module.MEDIA_KEYS[target_id] else 0

    def canvas(self, aspect_ratio: str, megapixels: float) -> tuple[int, int]:
        return self.module.canvas(aspect_ratio, float(megapixels))

    def image_suffixes(self) -> tuple[str, ...]:
        return tuple(self.module.IMAGE_SUFFIXES)

    def video_suffixes(self) -> tuple[str, ...]:
        return tuple(self.module.VIDEO_SUFFIXES)

    def min_clip_frames(self) -> int:
        return int(self.module.MIN_CLIP_FRAMES)


def _catalog_targets(catalog: Mapping[str, Any]) -> dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]]:
    found = {}
    for provider in catalog.get("providers") or ():
        for target in provider.get("targets") or ():
            found[target["id"]] = (provider, target)
    return found


def _control(target: Mapping[str, Any], control_id: str) -> Mapping[str, Any]:
    return next(c for c in target.get("controls") or () if c.get("id") == control_id)


def evidence_id(evidence: Mapping[str, Any] | None, target_id: str) -> str | None:
    """The model_evidence entry that proved this target with a real render, else None ("Not proven")."""
    for section in ("variants", "edit_targets"):
        for key, entry in ((evidence or {}).get(section) or {}).items():
            if isinstance(entry, Mapping) and entry.get("target_id") == target_id and entry.get("state") == "rendered":
                return key
    return None


def read_evidence(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _billing(provider: Mapping[str, Any]) -> str:
    return "local" if _endpoint.is_engine_label(provider.get("endpoint_label")) else "metered"


def _platforms(provider: Mapping[str, Any]) -> list[str]:
    # A local engine target is proven on the artist's Windows machine only.
    return ["windows"] if _endpoint.is_engine_label(provider.get("endpoint_label")) else ["windows", "macos", "linux"]


def _agent_note(mapping: str, guide: bool) -> str:
    if guide:
        return ("MiniMax H3 References: use it when the shot has a guide clip whose motion the take should follow and "
                "pictures at exact frames; do not use it without pictures or guide (plain prompt shots go to Text to Video).")
    if mapping == "first_last_guides":
        return ("MiniMax H3 Frames: use it for a shot held by pictures at exact frames (first and last frame are the "
                "opening and closing picture); it takes no guide clip, so do not use it when the motion must follow a guide.")
    return "Use it for pictures at exact frames; it takes no guide clip."


def manifest(source: StepSource, provider: Mapping[str, Any], target: Mapping[str, Any], evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    target_id = target["id"]
    duration = _control(target, "duration_seconds")
    fps = int((target.get("output") or {}).get("fps") or 24)
    mapping = source.mapping(target_id)
    guide_max = source.guide_max(target_id)
    motion_guide = None
    if guide_max:
        motion_guide = {"max": guide_max, "accepts": ["video"],
                        "length_rule": (f"The guide is read from its first frame at {fps} fps (another rate is resampled); "
                                        "a longer guide is cut to the shot length, a shorter one is refused.")}
    return {
        "schema": SCHEMA,
        "id": target_id,
        "label": target["label"],
        "provider_id": provider["id"],
        "billing": _billing(provider),
        "platforms": _platforms(provider),
        "evidence": evidence_id(evidence, target_id),
        "frame_grid": source.grid(fps, int(duration["min"]), int(duration["max"])),
        "keyframes": {"max": source.keyframe_max(), "placement": "any", "mapping": mapping},
        "motion_guide": motion_guide,
        "agent_note": _agent_note(mapping, bool(guide_max)),
    }


def manifests(source: StepSource, catalog: Mapping[str, Any], evidence: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    targets = _catalog_targets(catalog)
    return [manifest(source, *targets[target_id], evidence) for target_id in source.target_ids if target_id in targets]


# --------------------------------------------------------------------------- support


def _is_int(value: Any) -> bool:
    return type(value) is int


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def support(
    source: StepSource,
    catalog: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    confine: Callable[[str, list[str]], Path],
    probe: Callable[[Path], Mapping[str, Any]],
) -> dict[str, Any]:
    """The §1b answer. Never raises for a wrong request: ``supported: false`` with every reason found."""
    reasons: list[str] = []
    effective: dict[str, Any] = {"effective_frames": None, "fps": None, "width": None, "height": None,
                                 "keyframes": [], "motion_guide": None}
    targets = _catalog_targets(catalog)
    target_id = request.get("target_id")
    if target_id not in source.target_ids or target_id not in targets:
        return {"supported": False, "reasons": [f"The video step {target_id!r} is unknown or cannot run a shot chain."],
                "effective": effective}
    provider, target = targets[target_id]
    step = manifest(source, provider, target, None)
    grid = step["frame_grid"]
    fps = grid["fps"]
    effective["fps"] = fps
    aspect = _control(target, "aspect_ratio").get("default")
    megapixels = _control(target, "megapixels").get("default")
    try:
        effective["width"], effective["height"] = source.canvas(aspect, megapixels)
    except (TypeError, ValueError):
        pass
    roots = [str(r) for r in request.get("approved_roots") or () if isinstance(r, str)]

    # --- length: exactly one of duration_seconds / frames
    has_duration, has_frames = request.get("duration_seconds") is not None, request.get("frames") is not None
    frames: int | None = None
    if has_duration == has_frames:
        reasons.append("Length needs exactly one of duration_seconds or frames.")
    elif has_duration:
        seconds = request["duration_seconds"]
        if not _is_int(seconds) or not grid["min_seconds"] <= seconds <= grid["max_seconds"]:
            reasons.append(f"duration_seconds must be a whole number from {grid['min_seconds']} to {grid['max_seconds']}.")
        else:
            frames = source.frames_for(seconds, fps)
    else:
        wanted = request["frames"]
        allowed = {source.frames_for(s, fps): s for s in range(grid["min_seconds"], grid["max_seconds"] + 1)}
        if not _is_int(wanted) or wanted not in allowed:
            listed = ", ".join(str(v) for v in sorted(allowed))
            reasons.append(f"frames {wanted} is not on this step's frame grid; it renders one of {listed} frames.")
        else:
            frames = wanted
    effective["effective_frames"] = frames

    # --- keyframes: frame or seconds, on the grid, inside the shot, one per frame, a picture under the roots
    raw = request.get("keyframes") or []
    if not isinstance(raw, list):
        raw, _ = [], reasons.append("keyframes must be a list.")
    if len(raw) > step["keyframes"]["max"]:
        reasons.append(f"This step takes at most {step['keyframes']['max']} keyframes; {len(raw)} were given.")
    seen: dict[int, int] = {}
    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            reasons.append(f"Keyframe {index + 1} must be an object with frame or seconds and source.")
            continue
        has_f, has_s = entry.get("frame") is not None, entry.get("seconds") is not None
        requested: dict[str, Any] = {}
        resolved: int | None = None
        if has_f == has_s:
            reasons.append(f"Keyframe {index + 1}: needs exactly one of frame or seconds.")
            requested = {k: entry[k] for k in ("frame", "seconds") if entry.get(k) is not None}
        elif has_f:
            requested = {"frame": entry["frame"]}
            if not _is_int(entry["frame"]):
                reasons.append(f"Keyframe {index + 1}: frame must be a whole number.")
            else:
                resolved = entry["frame"]
        else:
            requested = {"seconds": entry["seconds"]}
            if not _is_number(entry["seconds"]) or entry["seconds"] < 0:
                reasons.append(f"Keyframe {index + 1}: seconds must be a number from 0.")
            else:
                exact = entry["seconds"] * fps
                if abs(exact - round(exact)) > 1e-6:
                    reasons.append(f"Keyframe {index + 1}: {entry['seconds']} s lies between two frames at {fps} fps "
                                   f"(frames {math.floor(exact)} and {math.ceil(exact)}).")
                else:
                    resolved = int(round(exact))
        if resolved is not None and frames is not None and not 0 <= resolved <= frames - 1:
            reasons.append(f"Keyframe {index + 1}: frame {resolved} is outside the shot; this shot has frames 0 to {frames - 1}.")
        if resolved is not None:
            if resolved in seen:
                reasons.append(f"Keyframes {seen[resolved] + 1} and {index + 1}: two keyframes on frame {resolved}; one frame takes one picture.")
            else:
                seen[resolved] = index
        reasons.extend(_file_reasons(f"Keyframe {index + 1}", entry.get("source"), source.image_suffixes(), roots, confine))
        effective["keyframes"].append({"requested": requested, "resolved_frame": resolved})

    # --- motion guide
    guide = request.get("motion_guide")
    if guide is not None:
        if not isinstance(guide, Mapping):
            reasons.append("motion_guide must be an object with source and offset_frame, or null.")
        elif step["motion_guide"] is None:
            reasons.append(f"{step['label']} takes no motion guide.")
        else:
            offset = guide.get("offset_frame", 0)
            if not _is_int(offset) or offset < 0:
                reasons.append("The guide's offset_frame must be a whole number from 0.")
            elif offset != 0:
                reasons.append(f"This step reads the guide from its first frame; offset_frame {offset} is not possible; "
                               "the shot starts at the guide's frame 0.")
            problems = _file_reasons("The guide", guide.get("source"), source.video_suffixes(), roots, confine)
            reasons.extend(problems)
            if not problems:
                try:
                    facts = probe(confine(guide["source"], roots))
                    frames_in, fps_in = int(facts["frame_count"]), float(facts["fps"])
                except Exception:  # noqa: BLE001 - an unreadable clip is a plain reason
                    reasons.append("The guide clip cannot be read as a video.")
                else:
                    shot_frames_available = math.floor(frames_in * fps / fps_in + 1e-9)
                    action = "resample" if abs(fps_in - fps) > 1e-3 else "exact"
                    if frames is not None:
                        if shot_frames_available < frames:
                            reasons.append(f"The guide has {frames_in} frames at {fps_in:g} fps ({shot_frames_available} shot frames); "
                                           f"the shot needs {frames}.")
                        elif action == "exact" and frames_in > frames:
                            action = "cut"
                        if frames_in < source.min_clip_frames():
                            reasons.append(f"The guide must have at least {source.min_clip_frames()} frames.")
                    effective["motion_guide"] = {"frames_in": frames_in, "fps_in": round(fps_in, 3),
                                                 "frames_used": frames, "action": action}
    return {"supported": not reasons, "reasons": reasons, "effective": effective}


def _file_reasons(what: str, value: Any, suffixes: tuple[str, ...], roots: list[str], confine: Callable[[str, list[str]], Path]) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"{what} needs a source file."]
    if Path(value).suffix.lower() not in suffixes:
        return [f"{what}: {Path(value).name} is not one of {', '.join(suffixes)}."]
    if not roots:
        return [f"{what}: no approved folders were given, so {Path(value).name} cannot be read."]
    try:
        confine(value, roots)
    except Exception as exc:  # noqa: BLE001 - confine answers outside/missing with its own text
        text = str(exc).lower()
        # Lane w34-sec (scan finding 7): the refused value itself is never probed (Path.exists on a UNC path opens an
        # SMB session; on a local path it answered whether any file on the disk exists). Confinement already said
        # "missing" for a file inside the folders; every other refusal is "outside the approved folders".
        if "not exist" in text or "missing" in text or "no such" in text:
            return [f"{what}: {Path(value).name} is missing."]
        return [f"{what}: {Path(value).name} is outside the approved folders."]
    return []
