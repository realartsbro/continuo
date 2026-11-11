"""Model-agnostic image edit contract (R4): ``studio.image-edit-request/1``.

The Suite owns the edit operations (fill, replace, remove, ... ), one common
request, the crop & stitch step and the result shape.  A model family only
declares what it can do (``image_edit_capabilities.v1.json``) and maps the
request onto its own pinned ComfyUI workflow (e.g. ``qwen_image_edit_adapter``).

This module is pure Python (no PIL, no network): request normalisation, the
crop math, the instruction templates, the capability declarations and the
ledger snapshot for one edit variant (``operation: "edit"``).  The ledger
validates edit snapshots through ``validate_edit_snapshot`` - a snapshot is
accepted only if rebuilding it from its own artist intent gives the same
canonical bytes, exactly like the Krea create snapshots.

No ``@dataclass`` and no bare relative imports at module level: the module is
also loaded unregistered by ``plugin_api``.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Mapping

REQUEST_SCHEMA = "studio.image-edit-request/1"
RESULT_SCHEMA = "studio.image-edit-result/1"
OPERATION_SCHEMA = "studio.image-edit-operation/1"
CAPABILITIES_SCHEMA = "studio.image-edit-capabilities/1"
SNAPSHOT_EDIT_SCHEMA = "studio.image-edit-snapshot/1"

# Every operation the Suite knows.  The first three are wired (Qwen family);
# the rest are declared so families can say what they can do, and so the
# desktop can show them disabled with a reason.
OPERATIONS_WIRED = ("fill", "replace", "remove", "restyle", "outpaint", "relight", "box")
OPERATIONS_DECLARED = ("control", "upscale", "move", "replace_character", "remove_background")
OPERATIONS = OPERATIONS_WIRED + OPERATIONS_DECLARED
# Area edits whose result is tone-matched to the picture and pasted through an inner ramp at a hard
# selection edge (image_edit_service.stitch seam). Not Outpaint (own band), Relight (re-tones on purpose), Box.
SEAM_OPERATIONS = ("fill", "replace", "remove")
# w24-edit (owner acceptance finding 07.11. 10:40, Photoshop Sky Replacement / Lightroom Select Sky): Restyle re-tones on
# purpose, like Relight. It is pasted through its matte (image_edit_service.stitch ``matte``): no colour match back to
# the old picture (that turned a violet sky amber) and no membrane (that pulled the subject's colours into the new sky
# as a halo); the output is registered to the picture and the edge band next to the kept subject is decontaminated.
# A Restyle recorded before (colour match on) keeps the old paste, so its composite rebuilds as it was.
MATTE_OPERATIONS = ("restyle",)
# w24-edit2 (owner decision 1, Photoshop Edit > Sky Replacement, image-lanes/w24-edit2-STUDY.md PS-SR/PTC-SR/SE-SR):
# Restyle inside a mask is composited with the dialog's settings, every one visible and settable by hand and Hermes and
# recorded in the recipe (``options.sky``). Defaults follow Photoshop's dialog (foreground harmonised to the new sky),
# with Shift Edge moving the matte 2 px INTO the subject over pixels that still read as the old sky (the warm rim).
# Outside the mask stays byte-identical only with shift_edge 0, lighting 0 and color 0; with the defaults the pixels
# changed outside the mask are exactly those three settings' (the proof's ``outside_by``).
# w25-sky (GEM-077, image-lanes/w25-sky-STUDY.md): two settings after Luminar Sky AI - ``haze`` (Horizon Blending: a
# soft haze band joins the new sky and the water) and ``reflect`` (Reflection Amount + Water Blur: open water takes
# the new sky, mirrored and blurred). A recipe that records them is composited by sky_replace.py (v2: plate edges and
# grain, unmixed rim, slots and thin structures, an open-horizon ramp); a recipe recorded before keeps v1.
SKY_DEFAULTS = {"shift_edge": 2, "fade_edge": 1, "brightness": 0, "temperature": 0, "lighting_mode": "multiply",
                "lighting": 50, "color": 50, "haze": 40, "reflect": 70}
SKY_RANGES = {"shift_edge": (-20, 20), "fade_edge": (0, 20), "brightness": (-100, 100), "temperature": (-100, 100),
              "lighting": (0, 100), "color": (0, 100), "haze": (0, 100), "reflect": (0, 100)}
SKY_LIGHTING_MODES = ("multiply", "screen")


def sky_settings(value: Any, operation: str) -> dict[str, Any] | None:
    """The Sky Replacement settings of one Restyle (``{}`` = the defaults), or None (not asked, or not a Restyle). The
    pane sends them when the Restyle's area is a Sky mask (Photoshop's Sky Replacement works on the sky)."""
    if operation not in MATTE_OPERATIONS:
        if value is not None:
            raise EditRequestError("invalid_edit_request", "Sky settings go with Restyle")
        return None
    if value is None:  # a Restyle without Sky settings: the w24-edit matte paste (no foreground change)
        return None
    if not isinstance(value, Mapping) or set(value) - set(SKY_DEFAULTS):
        raise EditRequestError("invalid_edit_request", "Sky settings are invalid: shift_edge, fade_edge, brightness, temperature, lighting_mode, lighting, color, haze, reflect")
    out = dict(SKY_DEFAULTS)
    for key, v in value.items():
        if key == "lighting_mode":
            if v not in SKY_LIGHTING_MODES:
                raise EditRequestError("invalid_edit_request", "Sky lighting_mode must be multiply or screen")
            out[key] = v
            continue
        lo, hi = SKY_RANGES[key]
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise EditRequestError("invalid_edit_request", f"Sky {key} must be a whole number from {lo} to {hi}")
        out[key] = v
    return out
# p13-img (IMG-129): the selection reaches the model as the sampler's latent noise mask (ComfyUI
# SetLatentNoiseMask + DifferentialDiffusion; none of the three families has a mask input of its own), grown and
# feathered at the WORK size, and the paste uses the same soft mask. Values = the owner's prototype approved by the
# artist on 01.11. (Replace: grow 20 px + Gaussian blur 14 px at 1024, denoise 1.0; whole boat in the water at seeds
# 111 and 7). Krita feathers by ~10 % of the selection diagonal; we keep the approved board's numbers.
LATENT_MASK_TARGETS = ("qwen-image-2.1-edit-pe-v3", "flux3-klein-sd-edit-v1", "flux2-klein-9b-kv-edit-v1")
# Remove (IMG-131/133, owner decision 01.11.): the crop is pre-filled by the owned LaMa engine (selection grown
# ``prefill_grow_px`` at crop size, inpaint_core/lama_engine), then the edit model refines the FILLED crop through the
# latent mask (grow 10 + blur 8 at work size) at denoise 0.5 with a background-only instruction. Owner prototype
# 31.10. (FLUX.2 Klein): box-edge jump 0.63-0.67 against 2.9 at install 112; 70 % invented objects, 50 % best.
# Krita pre-fills with LaMa too (workflow.py 913-915) but skips it for edit models (1546-1547); we deliberately
# keep it (that path painted the miniature harbour on Qwen/Klein and a ghost on FLUX.3).
# w22-img (IMG-129): Replace's soft mask scales with the selection (Krita AI Diffusion: feather = max(10 % of the
# selection diagonal, 32 px), grow = 4 + feather / 2, KR-model 1561-1610 / settings.py 218-245) and leaves room BELOW
# the selection (Photoshop: select "a little bigger" so there is room to blend; a reflection gets its own area below,
# ADOBE add-objects 00:28 / create-reflections 00:31): the hull below the line, its waterline and its reflection. The
# fixed 20 + 14 px (about 10 + 7 px in the picture) cut the new object at a tight oval or a rectangle (case C).
LATENT_MASK_SPECS = {
    # w24-edit (owner decision 06.11., Photoshop Generative Fill: the selection is the region): Replace edits exactly
    # the selection with the scaled soft edge and no room below; a reflection is its own selection (backlog IMG-211).
    # ``keep_edge_objects`` keeps small objects at the selection edge that lie mostly outside it.
    "replace": {"mode": "scaled", "feather_rel": 0.10, "feather_min_px": 32, "feather_max_short_rel": 0.25,
                "grow_offset_px": 4, "below_rel": 0.0, "keep_edge_objects": True, "denoise": 1.0},
    "remove": {"grow_px": 10, "blur_px": 8, "denoise": 0.5, "prefill": "lama", "prefill_grow_px": 10},
}
# Latent steps earlier Suite versions recorded: a recorded snapshot keeps its step and rebuilds byte for byte.
LEGACY_LATENT_MASK_SPECS = {
    "replace": ({"grow_px": 20, "blur_px": 14, "denoise": 1.0},  # p13-img .. 042e516f (install 127)
                {"mode": "scaled", "feather_rel": 0.10, "feather_min_px": 32, "feather_max_short_rel": 0.25,
                 "grow_offset_px": 4, "below_rel": 0.35, "denoise": 1.0},  # w22-img2 .. e7756461 (install 128)
                {"mode": "scaled", "feather_rel": 0.10, "feather_min_px": 32, "feather_max_short_rel": 0.25,
                 "grow_offset_px": 4, "below_rel": 0.35, "keep_edge_objects": True, "denoise": 1.0}),  # w23-img room over water .. 306caf0a (install 129)
}

SCENE_LIGHTS = ("backlit", "front")
# w22-img2 (line-art regression of the scaled step): on a flat-ink plate (manga, comic, line drawing; measured by
# image_edit_service.plate_kind) Replace keeps base 042e516f's crisp step. A wide soft band repainted and re-blended
# the ink around the new object (grey halo, softer edges; vision ranked base first on seeds 111 and 7), and line
# art has no scene light to match. The cap ``feather_max_short_rel`` keeps the soft band below a quarter of a small
# selection's shorter side (Krita's 32 px floor assumes selections of a few hundred px).
LINE_ART_LATENT_MASK = {"replace": {"grow_px": 20, "blur_px": 14, "denoise": 1.0}}
# The background-only instruction of a pre-filled Remove: never the removed object's name (naming it brings it back).
REMOVE_BACKGROUND_INSTRUCTION = ("The background continues seamlessly: the same ground, water, sky, walls and buildings, "
                                 "with the same light, colour, texture and perspective; nothing else is there.")


# p13-img (IMG-130): Restyle 70 % changed almost nothing on Qwen 2.1 (a watercolour stayed a photo). Strength maps
# through one curve before it reaches the sampler: denoise = strength ** STRENGTH_EXPONENT (InvokeAI rescales FLUX
# img2img strength with exponent 0.2, INV-gbu 228-260 / R1 section 2; Qwen 2.1 is a flow-matching model like FLUX):
# 70 % -> 0.931, 50 % -> 0.871, 20 % -> 0.725. New snapshots carry ``strength_curve``; a snapshot recorded before
# has none and keeps denoise = strength (it must rebuild byte for byte).
STRENGTH_EXPONENT = 0.2
STRENGTH_CURVE = "pow_0.2"
STRENGTH_CURVES = {"qwen-image-2.1-edit-pe-v3": STRENGTH_CURVE}
# w24-edit2 (IMG-130, owner decision 3: "70 % changes nothing on Qwen"): pow_0.2 sent denoise 0.931..1.0 for 70-100 %,
# and ComfyUI's KSampler runs int(steps / denoise) sigmas and keeps the last ``steps`` (samplers.py set_steps), so
# 70-100 % were the same 12 sigmas (byte-identical renders). Strength now names where on the model's OWN sigma schedule
# the sampler starts: ``start`` = the fraction of the schedule skipped (0 = the whole schedule, as at 100 %; 1 = none:
# the source comes back). The adapter makes the start exact with integers: ``steps`` sampled of ``total`` (KSampler:
# steps + denoise = steps / (total + 0.5)); a sigma-list adapter cuts its list at ``start``. Per model the start of each
# 10 % step comes from a measured sweep (image-lanes/w24-edit-MATRIX.md, w24-edit2 strength table): every 10 % step a
# different start, 0 % = the source, the default (Restyle 70 %) visibly the style at the same composition.
STRENGTH_CURVE_START = "start_v1"
STRENGTH_START = {  # start fraction at 0, 10, ..., 100 % (piecewise linear between)
    # Qwen 2.1 (sweeps sw1/sw4, kf0 watercolour seed 11): start 0.15 still read as the photo (mean change 18), 0.11 as
    # the style (39); the default 70 % sits at 0.09.
    "qwen-image-2.1-edit-pe-v3": (1.0, 0.4, 0.3, 0.24, 0.19, 0.15, 0.12, 0.09, 0.06, 0.03, 0.0),
    # FLUX.3 Klein and Krea 2 Turbo (sweep sw1, kf0 watercolour seed 11): starts 0.21-0.75 all stayed a photo (mean
    # change 3.5-6.5); the style comes in below 0.15, so their 10 % steps sit there.
    "flux3-klein-sd-edit-v1": (1.0, 0.3, 0.25, 0.21, 0.18, 0.15, 0.12, 0.09, 0.06, 0.03, 0.0),
    "flux2-klein-9b-kv-edit-v1": (1.0, 0.75, 0.65, 0.57, 0.5, 0.43, 0.36, 0.29, 0.21, 0.12, 0.0),
    "krea2-edit-turbo-v1": (1.0, 0.3, 0.25, 0.21, 0.18, 0.15, 0.12, 0.09, 0.06, 0.03, 0.0),
}
STRENGTH_STEPS_MAX = 4  # a KSampler schedule is solved with at most base x this many total steps


def strength_start(strength: float, target_id: str) -> float:
    """The start fraction of one Strength on ``target_id`` (its measured table, piecewise linear)."""
    table = STRENGTH_START[target_id]
    x = min(max(float(strength), 0.0), 1.0) * 10.0
    i = min(int(x), 9)
    return round(table[i] + (table[i + 1] - table[i]) * (x - i), 4)


def start_schedule(start: float, base_steps: int) -> dict[str, Any]:
    """Integers for a KSampler: ``steps`` sampled of ``total`` with 1 - steps/total closest to ``start``; ``denoise`` =
    steps / (total + 0.5) so ComfyUI's int(steps / denoise) is exactly ``total``. start 1 = no sampling (denoise 0)."""
    if start >= 0.999:
        return {"steps": 0, "total": base_steps, "denoise": 0.0, "start": 1.0}
    if start <= 0.0:
        return {"steps": base_steps, "total": base_steps, "denoise": 1.0, "start": 0.0}
    best = None
    for steps in range(base_steps, base_steps * STRENGTH_STEPS_MAX + 1):  # the fewest steps that hit the start within 0.005
        total = max(steps + 1, int(round(steps / (1.0 - start))))
        err = abs(1.0 - steps / total - start)
        if best is None or err < best[0] - 1e-9:
            best = (err, steps, total)
        if err <= 0.005:
            break
    if best is None:  # a start this late: the fewest steps that still reach it
        total = base_steps * STRENGTH_STEPS_MAX
        steps = max(1, int(round(total * (1.0 - start))))
        best = (0.0, steps, total)
    _err, steps, total = best
    return {"steps": steps, "total": total, "denoise": round(steps / (total + 0.5), 6), "start": round(1.0 - steps / total, 4)}


def paste_for(intent: Mapping[str, Any]) -> dict[str, Any]:
    """How the result route pastes one edit (image_edit_service.stitch): the area edits joined by the membrane; Restyle
    through its matte (w24-edit), except a Restyle recorded with colour match on (before w24-edit): its old paste.
    A Restyle with Sky settings (w24-edit2) is composited like Photoshop's Sky Replacement with exactly those."""
    op = intent.get("operation")
    if op in MATTE_OPERATIONS:
        if intent.get("sky") is not None and not intent.get("color_match"):
            return {"seam": False, "matte": True, "sky": dict(intent["sky"])}
        return {"seam": bool(intent.get("color_match")), "matte": not intent.get("color_match")}
    return {"seam": op in SEAM_OPERATIONS}


def effective_denoise(strength: float, curve: str | None, target_id: str | None = None) -> float:
    """The sampler denoise of one Strength (0..1) under ``curve`` (None = the recorded linear value)."""
    if curve is None:
        return float(strength)
    if curve == STRENGTH_CURVE_START and target_id in STRENGTH_START:
        return float(start_schedule(strength_start(strength, target_id), int(EDIT_TARGETS[target_id]["sampling"]["steps"]))["denoise"])
    if curve != STRENGTH_CURVE:
        raise EditRequestError("invalid_edit_snapshot", "edit-strength-curve")
    return round(float(strength) ** STRENGTH_EXPONENT, 3)


def strength_fields(intent: Mapping[str, Any]) -> dict[str, Any]:
    """What one edit snapshot carries for Strength: nothing at 100 %, else the value and its target's curve."""
    if intent.get("strength") is None:
        return {}
    curve = STRENGTH_CURVE_START if intent.get("target_id") in STRENGTH_START else STRENGTH_CURVES.get(intent.get("target_id"))
    return {"strength": intent["strength"], **({"strength_curve": curve} if curve else {})}


def latent_mask_for(intent: Mapping[str, Any]) -> dict[str, Any] | None:
    """The latent-mask step of one edit (a copy), or None when the operation/target paints without it."""
    spec = LATENT_MASK_SPECS.get(intent.get("operation"))
    if spec is None or intent.get("target_id") not in LATENT_MASK_TARGETS:
        return None
    return dict(spec)


def latent_mask_accepted(operation: Any, target_id: Any, step: Any) -> bool:
    """A snapshot's latent step is the current one or one an earlier version recorded (exactly)."""
    if target_id not in LATENT_MASK_TARGETS:
        return False
    return (step == LATENT_MASK_SPECS.get(operation) or step in LEGACY_LATENT_MASK_SPECS.get(operation, ())
            or (operation in LINE_ART_LATENT_MASK and step == LINE_ART_LATENT_MASK[operation]))
REFERENCE_ROLES = ("identity", "outfit", "object", "style", "location", "pose", "depth", "lineart", "subject", "reference")
MAX_VARIANTS = 4
MAX_PROMPT = 2000
MAX_REFERENCES = 9
CONTEXT_FACTOR_DEFAULT = 1.2
CONTEXT_FACTOR_MIN = 1.0
CONTEXT_FACTOR_MAX = 4.0
MIN_CROP_SIDE = 512
SEED_MAX = 18_446_744_073_709_551_615 - MAX_VARIANTS
# Outpaint: the picture on a larger canvas, the new margin is made like a Fill. Each margin is at
# most twice the picture side it extends; the canvas stays within the picture limits.
OUTPAINT_MARGIN_FACTOR = 2
OUTPAINT_CANVAS_MAX_SIDE = 16_384
OUTPAINT_CANVAS_MAX_PIXELS = 67_108_864
# The crop holds the new margin and as much of the picture beside it (the scene to continue).
OUTPAINT_CONTEXT_FACTOR = 2.0

# --- pinned edit targets ----------------------------------------------------
# One entry per wired family workflow.  The hashes are of the LF bytes of the
# checked-in files under workflows/<target_id>/ (see .gitattributes).
QWEN_TARGET_ID = "qwen-image-2.1-edit-pe-v3"
KLEIN_TARGET_ID = "flux3-klein-sd-edit-v1"
KLEIN2_TARGET_ID = "flux2-klein-9b-kv-edit-v1"
KLEINBOX_TARGET_ID = "flux3-klein-box-edit-v1"
PH3_TARGET_ID = "p-h3-image-lora-crop-edit-v1"  # w24-h3img: reads workflows/p-h3-image-lora-edit-v1
EDIT_TARGETS: dict[str, dict[str, Any]] = {
    QWEN_TARGET_ID: {
        "family": "qwen-image-2.1",
        "variant": "edit-pe-v3",
        "preset": "turbo",
        "capability_manifest_sha256": "632c313962fb25100064975a7595059d2ea0832dede0f18c4462186404c67dd0",
        "api_graph_sha256": "bdb7cf1acd5cef4c10c13ade7958bacbd42adcbf035354bfdd5633fdbaf982f1",
        "grid": 32,
        "work_resolution": 1024,
        "files": {
            "diffusion_model": "qwen_image_2.1_int8_convrot.safetensors",
            "text_encoder": "qwen3vl_8b_int8_convrot.safetensors",
            "vae": "qwen_image_2.1_vae_bf16.safetensors",
            "lora": "p_qwen_image_2.1_8step_v0.1.safetensors",
            "prompt_enhancer": "qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors",
        },
        "sampling": {"steps": 12, "cfg": 1.5, "sampler": "er_sde", "scheduler": "beta", "denoise": 1, "lora_strength": 1},
    },
    # FLUX.3 Klein distilled, the official ComfyUI edit template (ReferenceMultiLatent).
    # Measured 2026-10-18 on the Suite's bench: Qwen is better in every operation;
    # Klein is the artist's alternative.  Same Suite instructions (its own phrasing
    # measured no better), so no new legacy instruction texts.
    KLEIN_TARGET_ID: {
        "family": "flux3-klein",
        "variant": "edit-distilled",
        "preset": "distilled",
        "capability_manifest_sha256": "556918b29fd41c39446756691d2a8bca448a76b7baf4650fe9184d7532db74a8",
        "api_graph_sha256": "a85e210bf111c875898c82a52420255e5e09204544c540607e0f320485855084",
        "grid": 16,
        "work_resolution": 1024,
        "files": {
            "diffusion_model": "flux3_image_klein_sd_int8_convrot.safetensors",
            "text_encoder": "qwen3vl_4b_int8_convrot.safetensors",
            "vae": "flux2-vae.safetensors",
            "lora": None,
            "prompt_enhancer": "qwen3.5_9b_flux3_prompt_upsampler_int8_convrot.safetensors",
        },
        "sampling": {"steps": 16, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "denoise": 1, "lora_strength": 0},
    },
    # FLUX.2 Klein 9B KV (distilled, 4 steps), the official ComfyUI edit template with one
    # reference.  Measured 2026-10-18 on the same bench: about 2-3 s per edit (Qwen 6.2 s),
    # weaker in Fill and Remove; the fast choice.  No prompt upsampler in the template.
    KLEIN2_TARGET_ID: {
        "family": "flux2-klein",
        "variant": "edit-9b-kv",
        "preset": "distilled",
        "capability_manifest_sha256": "8a401f8d37d51f66ce00241040e9f6a7ea7e5778b80c67d72415587f2b9560f5",
        "api_graph_sha256": "7f2ab1757c7cc36378b01a65f266d78d8015f99d095b62f5cb96e2034231b245",
        "grid": 16,
        "work_resolution": 1024,
        "files": {
            "diffusion_model": "flux-2-klein-9b-kv-fp8.safetensors",
            "text_encoder": "qwen_3_8b_fp8mixed.safetensors",
            "vae": "flux2-vae.safetensors",
            "lora": None,
            "prompt_enhancer": None,
        },
        "sampling": {"steps": 4, "cfg": 1.0, "sampler": "euler", "scheduler": "flux2", "denoise": 1, "lora_strength": 0},
        # Fill and Remove see the ORIGINAL crop, located by words: the grey area came back unchanged
        # (img-models 2026-10-21, std < 1 inside it); measured 2026-10-22 with the original crop:
        # Remove emptied the lighthouse (seeds 7, 11), Fill put the boat into open sea (seeds 7, 11).
        "fill_remove_on": "original",
    },
    # FLUX.3 Klein with boxes (Image Box): NKD Klein Presampling (Klein 3) reads the Suite's caption +
    # BFL element list; same files and sampler as the distilled edit, no prompt upsampler.
    KLEINBOX_TARGET_ID: {
        "family": "flux3-klein",
        "variant": "edit-distilled",
        "preset": "distilled",
        "capability_manifest_sha256": "220ace0e90353862cb43297838a2cd7dd3116a9f6c48c78d5ebdb8f31111ad86",
        "api_graph_sha256": "66da2ca8109a145ef1c5996d6e927e51d929dd583a9cc20c34666b6bdb18aee6",
        "grid": 16,
        "work_resolution": 1024,
        "files": {
            "diffusion_model": "flux3_image_klein_sd_int8_convrot.safetensors",
            "text_encoder": "qwen3vl_4b_int8_convrot.safetensors",
            "vae": "flux2-vae.safetensors",
            "lora": None,
            "prompt_enhancer": None,
        },
        "sampling": {"steps": 16, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "denoise": 1, "lora_strength": 0},
    },
    # w24-h3img: P-H3-Image (PrunaAI LoRA on the MiniMax H3 FL2VA transformer), the catalog edit folder's own graph with
    # the crop at the card's 4 MP edit size (work_resolution 2048) straight into PH3ImageConditioning image_1; 8 fixed
    # sigmas, no CFG, no prompt upsampler (not installed). Append-only entry of lane w24-h3img.
    PH3_TARGET_ID: {
        "family": "p-h3-image",
        "variant": "lora-edit",
        "preset": "card",
        "capability_manifest_sha256": "e764e5fa7897842d09dc994f71a9db38960a15387c61bbbda392c6678f52bec9",  # w32-fix5 IMG-216b: Steps; earlier 9077fa79 in workflow_pin_history
        "api_graph_sha256": "6753056a8d63ac6b249e1001a883ab462b2c8f8aa842d62d1ecc93fc730446d8",
        "grid": 32,
        "work_resolution": 2048,
        "files": {
            "diffusion_model": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
            "text_encoder": "p-h3-image_text_encoder_int8_convrot.safetensors",
            "vae": "p-h3-image_vae_int8_convrot.safetensors",
            "lora": "p-h3-image_lora_bf16.safetensors",
            "prompt_enhancer": None,
        },
        "sampling": {"steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "manual_sigmas", "denoise": 1, "lora_strength": 1},
        # Matrix 07.11. (seed 7): with the grey crop Remove left a grey box on both pictures and Fill a hard rectangle on
        # shot-04, like FLUX.2 Klein; Fill and Remove see the ORIGINAL crop, located by words (FLUX.2 Klein's fix).
        "fill_remove_on": "original",
    },
}
# w24-krea: Krea 2 Edit (General Edit V2 LoRA, comfyui-krea2edit 2.0.0) on the Suite's crop, one picture. The render
# study (docs/studies/w24-krea-RESEARCH.md, 124 cells, Harbour kf0 + Sample shot-03, seeds 7/111) chose the base per
# operation: Kroma (the artist's Turbo model) for Replace through the latent-mask step, Krea 2 Turbo for Restyle and
# Relight (Kroma left the photo unchanged). Card settings: 8 steps, CFG 1, ref_boost 4, grounding 768, euler/simple.
KREA_REPLACE_TARGET_ID = "krea2-edit-kroma-v1"
KREA_LOOK_TARGET_ID = "krea2-edit-turbo-v1"
_KREA_FILES = {"text_encoder": "qwen3vl_4b_fp8_scaled.safetensors", "vae": "qwen_image_vae.safetensors",
               "lora": "krea2\\krea2_general_edit_v2.safetensors", "prompt_enhancer": None}
_KREA_SAMPLING = {"steps": 8, "cfg": 1.0, "sampler": "euler", "scheduler": "simple", "denoise": 1, "lora_strength": 1}
EDIT_TARGETS[KREA_REPLACE_TARGET_ID] = {
    "family": "krea2", "variant": "general-edit-v2", "preset": "turbo",
    "capability_manifest_sha256": "900c3691a1bef4ec4c48a3c013685ab5eb6ad57def2712c2b77220791f23a644",
    "api_graph_sha256": "b240bd96ff434341354f8b702a17b538d031fb7509a474657a2343a4b1f195c6",
    "grid": 16, "work_resolution": 1024,
    "files": {"diffusion_model": "kroma-v0.3.1-turbo-opd-int8-convrot.safetensors", **_KREA_FILES},
    "sampling": dict(_KREA_SAMPLING),
}
EDIT_TARGETS[KREA_LOOK_TARGET_ID] = {
    "family": "krea2", "variant": "general-edit-v2", "preset": "turbo",
    "capability_manifest_sha256": "360d4e412eecc74218e8269c9e550c0de5f0c9622db0a89eee6fd56d936f68bd",
    "api_graph_sha256": "68c9e62705b0b3018d7dfd9e91ebc09013df24cb8fa7044a99012bf8cbe6ffe3",
    "grid": 16, "work_resolution": 1024,
    "files": {"diffusion_model": "krea2_turbo_int8_convrot.safetensors", **_KREA_FILES},
    "sampling": dict(_KREA_SAMPLING),
}
LATENT_MASK_TARGETS = LATENT_MASK_TARGETS + (KREA_REPLACE_TARGET_ID,)  # Replace: the Suite's latent-mask step (study round 2)


# w32-fix5 IMG-216b: the edit contract's pin history. A pinned edit graph's manifest can change (P-H3-Image Edit gained
# Steps); a job stored under an earlier pin of the same target keeps decoding, polling and re-running under ITS pin, a new
# job carries the current one. Same file and mechanism as the create workflows (workflow_pin_history.v1.json, w29-fix2),
# keyed by the edit target id. Reference: Lightroom / Resolve open a catalog made by an older version and keep every picture.
_EDIT_PIN_HISTORY: dict[str, frozenset[tuple[str, str]]] = {}


def earlier_edit_pins(target_id: str) -> frozenset[tuple[str, str]]:
    """(capability_manifest_sha256, api_graph_sha256) of every earlier committed pin of this edit target."""
    if not _EDIT_PIN_HISTORY:
        doc = json.loads(Path(__file__).with_name("workflow_pin_history.v1.json").read_text(encoding="utf-8"))
        for target, pins in doc["earlier"].items():
            _EDIT_PIN_HISTORY[target] = frozenset((p["manifest_sha256"], p["api_sha256"]) for p in pins)
        _EDIT_PIN_HISTORY.setdefault("", frozenset())
    return _EDIT_PIN_HISTORY.get(target_id, frozenset()) if target_id in EDIT_TARGETS else frozenset()


def edit_pins(target_id: str) -> tuple[tuple[str, str], ...]:
    """The current pin first, then every earlier one this target accepts for a stored job."""
    target = EDIT_TARGETS[target_id]
    current = (target["capability_manifest_sha256"], target["api_graph_sha256"])
    return (current, *sorted(earlier_edit_pins(target_id) - {current}))

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_UPLOAD_NAME = re.compile(r"^filmsuite-edit-[0-9a-f]{32}\.png \[temp\]$")
_EDIT_ID = re.compile(r"^imgedit_[0-9a-f]{32}$")


class EditRequestError(ValueError):
    """A finite, user-facing refusal with a stable code (HTTP 422 at the route)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_box_cache: dict[str, Any] = {}


def box_module() -> Any:
    """generation/image_box.py, loaded by path (this module is also loaded unregistered)."""
    if "module" not in _box_cache:
        import importlib.util
        spec = importlib.util.spec_from_file_location("film_suite_image_box", Path(__file__).with_name("image_box.py"))
        if spec is None or spec.loader is None:
            raise RuntimeError("The image box module is unavailable.")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _box_cache["module"] = module
    return _box_cache["module"]


def _boxes(call: Any, *args: Any, **kwargs: Any) -> Any:
    """Run an image_box function; its BoxError answers as an EditRequestError with the same code."""
    module = box_module()
    try:
        return call(module)(*args, **kwargs)
    except module.BoxError as exc:
        raise EditRequestError(exc.code, exc.message) from exc


def neutral_for(intent: Mapping[str, Any]) -> Any:
    """What prepare paints grey: Fill/Remove/Outpaint their area, except a target that edits the original crop."""
    op = intent.get("operation")
    if op not in ("fill", "remove", "outpaint"):
        return False
    if op != "outpaint" and intent.get("strength") is not None:  # below 100 %: the model changes what is there
        return False
    if op != "outpaint" and EDIT_TARGETS.get(intent.get("target_id"), {}).get("fill_remove_on") == "original":
        return False
    return op


def box_cover(intent: Mapping[str, Any]) -> list[int] | None:
    """(x0, y0, x1, y1) the crop must show for a box edit: every box, Keep boxes and Move targets included."""
    boxes = intent.get("boxes")
    return _boxes(lambda m: m.cover, boxes) if boxes else None


# --- capabilities -------------------------------------------------------------
_CAPABILITIES_FILE = Path(__file__).with_name("image_edit_capabilities.v1.json")
# Real renders through the Suite (model_catalog.EVIDENCE_PATH, studio.model-evidence/1).
_EVIDENCE_FILE = Path(__file__).with_name("model_evidence.v1.json")
_capabilities_cache: dict[str, Any] = {}


def _edit_evidence() -> dict[str, Any]:
    """Per edit target: which operations rendered or failed for real (absent file: nothing proven)."""
    try:
        value = json.loads(_EVIDENCE_FILE.read_bytes().decode("utf-8"))
        targets = value["edit_targets"] if value.get("schema") == "studio.model-evidence/1" else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        targets = None
    return targets if isinstance(targets, dict) else {}


def _apply_evidence(value: dict[str, Any], evidence: Mapping[str, Any]) -> None:
    """Every wired operation is offered; a target without a render record carries that fact (``unrendered``,
    the tooltip and Hermes' warning), never a refusal (w39-p2-open). An operation that failed for real is refused."""
    for family in value["families"]:
        for op, declared in (family.get("operations") or {}).items():
            if not isinstance(declared, dict) or not declared.get("available"):
                continue
            entry = evidence.get(declared.get("target_id"))
            if not isinstance(entry, Mapping) or entry.get("state") != "rendered":
                declared["unrendered"] = "No render through the Suite on record"
            elif (entry.get("operations") or {}).get(op) == "failed" and declared.get("warning"):
                declared["verified"] = entry.get("date")  # w24-edit2: measured bad, offered with that measured warning
            elif (entry.get("operations") or {}).get(op) == "failed":
                declared.update(available=False, reason=f"Fails in the image engine ({entry.get('date')}): the grey area comes back unchanged")
            else:
                declared["verified"] = entry.get("date")


def load_capabilities() -> dict[str, Any]:
    """The per-family capability declarations (copy; file read once per process)."""
    if "value" not in _capabilities_cache:
        value = json.loads(_CAPABILITIES_FILE.read_bytes().decode("utf-8"))
        if value.get("schema") != CAPABILITIES_SCHEMA or not isinstance(value.get("families"), list):
            raise ValueError("image edit capabilities are malformed")
        for family in value["families"]:
            for op in family.get("operations") or {}:
                if op not in OPERATIONS:
                    raise ValueError(f"unknown operation {op} in capabilities")
        _apply_evidence(value, _edit_evidence())
        _capabilities_cache["value"] = value
    return copy.deepcopy(_capabilities_cache["value"])


def family_entry(family: str, variant: str | None = None) -> dict[str, Any] | None:
    for entry in load_capabilities()["families"]:
        if entry.get("family") == family and (variant is None or entry.get("variant") == variant):
            return entry
    return None


def operation_capability(family: str, variant: str | None, operation: str) -> dict[str, Any]:
    """What one family says about one operation; always a dict with ``available``."""
    entry = family_entry(family, variant)
    if entry is None:
        return {"available": False, "reason": "This model is not known to the editor."}
    declared = (entry.get("operations") or {}).get(operation)
    if not isinstance(declared, Mapping):
        return {"available": False, "reason": f"{entry.get('label', family)} cannot do this operation."}
    out = dict(declared)
    out.setdefault("available", False)
    if not out["available"]:
        out.setdefault("reason", f"{entry.get('label', family)}: not available yet.")
    return out


# --- request normalisation ----------------------------------------------------
def _text(value: Any, name: str, *, required: bool = False, limit: int = MAX_PROMPT) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise EditRequestError("invalid_edit_request", f"The {name} is invalid")
    value = " ".join(value.split())
    if required and not value:
        raise EditRequestError("invalid_edit_request", f"The {name} is empty")
    return value


def normalize_request(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the caller's edit intent (everything except source/mask bytes).

    Unknown keys are ignored (the desktop host adds its own).  Returns the
    canonical intent plus ``warnings`` for inputs the chosen family accepts
    but does not use (references, strength).
    """
    operation = raw.get("operation")
    if operation not in OPERATIONS:
        raise EditRequestError("invalid_edit_request", "The edit operation is unknown")
    model = raw.get("model") or {}
    if not isinstance(model, Mapping):
        raise EditRequestError("invalid_edit_request", "The model choice is invalid")
    family = model.get("family", "qwen-image-2.1")
    variant = model.get("variant")
    if not isinstance(family, str) or (variant is not None and not isinstance(variant, str)):
        raise EditRequestError("invalid_edit_request", "The model choice is invalid")
    capability = operation_capability(family, variant, operation)
    label = (family_entry(family, variant) or {}).get("label", family)
    if not capability.get("available"):
        raise EditRequestError("edit_not_available", f"{operation.replace('_', ' ').capitalize()} is not available for {label}: {capability.get('reason')}")
    # w24-edit2 (owner rule "keine Bevormundung", decision 4): a cell the matrix measured bad is offered and runs; its
    # measured reason travels with the edit as a warning for the hand (tooltip) and Hermes (the answer), never a refusal.
    measured = [f"model_warning: {label}: {capability['warning']}"] if capability.get("warning") else []
    if capability.get("unrendered"):  # w39-p2-open: offered and run; the fact travels with the edit
        measured.append(f"model_note: {label}: {capability['unrendered']}")
    boxes, box_mode, prompt_in, mode_in = None, None, raw.get("prompt"), None
    if raw.get("boxes") is not None and operation != "box":
        raise EditRequestError("invalid_edit_request", "Boxes go with the box operation")
    if operation == "box":
        boxes = _boxes(lambda m: m.normalize_boxes, raw.get("boxes"))
        box_mode = "native" if capability.get("method") == "native_boxes" else "mask"
        # Box intents per model (owner decision, wave 3): a family offers only the intents it was measured
        # good at, and Move where the model can carry (capability ``move``: native boxes; a mask cannot carry).
        offered = capability.get("intents")
        for box in boxes:
            if box["intent"] == "move" and not capability.get("move"):
                raise EditRequestError("box_move_unavailable", f"Move is not available for {label} yet")
            if isinstance(offered, list) and box["intent"] not in offered:
                names = ", ".join(i.capitalize() for i in offered if i != "keep")
                raise EditRequestError("box_intent_unavailable", f"{box['intent'].capitalize()} in a box is not available for {label}: it offers {names} (and Keep); FLUX.3 Klein offers every intent")
        if box_mode == "mask":  # Box -> mask: the changing boxes become one Fill / Replace / Remove
            plan = _boxes(lambda m: m.mask_plan, boxes, prompt_in if isinstance(prompt_in, str) else "", label)
            operation, prompt_in = plan["operation"], plan["prompt"]
            if plan["intent"] == "change":  # the description says what to do there
                mode_in = "raw"
                prompt_in = f"{prompt_in.rstrip('.')}. Keep everything else in the picture exactly the same."
            capability = operation_capability(family, variant, operation)
            if not capability.get("available"):
                raise EditRequestError("edit_not_available", f"{operation.capitalize()} (from the boxes) is not available for {label}: {capability.get('reason')}")
            if capability.get("warning"):
                measured.append(f"model_warning: {label}: {capability['warning']}")
    target_id = capability.get("target_id")
    if target_id not in EDIT_TARGETS:
        raise EditRequestError("edit_not_available", "This model has no wired edit workflow yet")
    target = EDIT_TARGETS[target_id]
    files = model.get("files")
    if files is not None:
        if not isinstance(files, Mapping) or any(not isinstance(v, str) for v in files.values()):
            raise EditRequestError("invalid_edit_request", "The model files are invalid")
        for role, name in files.items():
            if target["files"].get(role) != name:
                raise EditRequestError("edit_file_not_offered", f"{name} is not offered for {role} in this edit workflow")
    warnings: list[str] = list(measured)
    prompt = _text(prompt_in, "prompt", required=operation in ("fill", "replace", "restyle", "relight"))
    canvas = None
    if operation == "outpaint":
        opts = raw.get("options") or {}
        canvas = outpaint_margins(opts.get("canvas") if isinstance(opts, Mapping) else None)
    negative = _text(raw.get("negative"), "negative prompt")
    variants = raw.get("variants", 1)
    if isinstance(variants, bool) or not isinstance(variants, int) or not 1 <= variants <= MAX_VARIANTS:
        raise EditRequestError("invalid_edit_request", "Variants must be 1 to 4")
    seed = raw.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= SEED_MAX:
        raise EditRequestError("invalid_edit_request", "The seed is invalid")
    strength = raw.get("strength")
    if strength is not None:
        if isinstance(strength, bool) or not isinstance(strength, (int, float)) or not 0.0 <= float(strength) <= 1.0:
            raise EditRequestError("invalid_edit_request", "Strength must be between 0 and 1")
        # Owner decision 1 (charter image): 100 % = new content, below = the model changes what is there.
        # Only a family that declares strength takes it; 100 % is the request without strength (same graph).
        if float(strength) < 1.0:
            if capability.get("strength"):
                strength = round(float(strength), 3)
            else:
                warnings.append(f"strength_ignored: {label} always replaces the area")
                strength = None
        else:
            strength = None
    references_raw = raw.get("references") or []
    if not isinstance(references_raw, list) or len(references_raw) > MAX_REFERENCES:
        raise EditRequestError("invalid_edit_request", "References are invalid")
    references = []
    for item in references_raw:
        if not isinstance(item, Mapping) or not isinstance(item.get("ref"), str) or not 1 <= len(item["ref"]) <= 128:
            raise EditRequestError("invalid_edit_request", "A reference is invalid")
        role = item.get("role", "reference")
        ref_strength = item.get("strength", 1.0)
        if role not in REFERENCE_ROLES or isinstance(ref_strength, bool) or not isinstance(ref_strength, (int, float)) \
                or not 0.0 <= float(ref_strength) <= 2.0:
            raise EditRequestError("invalid_edit_request", "A reference is invalid")
        references.append({"ref": item["ref"], "role": role, "strength": round(float(ref_strength), 3)})
    if references and int(capability.get("refs_max") or 0) == 0:
        warnings.append("references_ignored: this model's edit workflow does not take references yet")
    options = raw.get("options") or {}
    if not isinstance(options, Mapping):
        raise EditRequestError("invalid_edit_request", "Options are invalid")
    # IMG-073 (artist's rule A-3192): a prompt enhancer/upsampler exists only as a manual checkbox, never
    # automatic. A missing value is off; only an explicit True from the caller turns it on.
    enhancer = options.get("prompt_enhancer", False)
    # Box edits on FLUX.3 Klein: the model brightens the whole crop (measured 2026-10-22, lighthouse seed 7: sky
    # +13.5/+11.3/+6.6 RGB), pasted back that is a visible rectangle; colour match is on unless asked off.
    # Area edits (p10-imgb): the edit models re-tone the whole crop too; matched by default (see SEAM_OPERATIONS).
    color_match = options.get("color_match", box_mode == "native" or operation in SEAM_OPERATIONS
                              or (operation == "outpaint" and target_id in OUTPAINT_COLOR_MATCH_TARGETS))
    mode = mode_in or options.get("instruction_mode", "template")
    factor = options.get("context_factor", OUTPAINT_CONTEXT_FACTOR if operation == "outpaint" else CONTEXT_FACTOR_DEFAULT)
    target_hint = _text(options.get("target"), "target", limit=200)
    if "reflection" in options:  # w24-edit: Replace edits exactly the selection (the reflection feature is IMG-211)
        raise EditRequestError("invalid_edit_request", "Replace edits exactly the selection; there is no reflection option")
    sky = sky_settings(options.get("sky"), operation)
    options = {k: v for k, v in options.items() if k != "canvas"}
    if operation == "relight" and enhancer is True:
        # Measured 2026-10-18 (4 lights x 3 wordings): Qwen's enhancer rewrote relight texts into blown
        # white "text pages" and other people in 6 of 8 cells; without it 0 of 8. Relight never uses it.
        enhancer = False
    if not isinstance(enhancer, bool) or not isinstance(color_match, bool) or mode not in ("template", "raw") \
            or isinstance(factor, bool) or not isinstance(factor, (int, float)) \
            or not CONTEXT_FACTOR_MIN <= float(factor) <= CONTEXT_FACTOR_MAX:
        raise EditRequestError("invalid_edit_request", "Options are invalid")
    if mode == "raw" and not prompt:
        raise EditRequestError("invalid_edit_request", "A raw instruction needs a prompt")
    intent = {
        "operation": operation, "prompt": prompt, "negative": negative, "variants": variants, "seed": seed,
        "target_id": target_id, "family": target["family"], "variant": target["variant"],
        "references": references, "prompt_enhancer": enhancer, "color_match": color_match,
        "instruction_mode": mode, "context_factor": round(float(factor), 3), "target": target_hint,
        "warnings": warnings,
    }
    if strength is not None:  # only a strength below 100 % on a family that takes it: every other intent is unchanged
        intent["strength"] = strength
    if canvas is not None:  # only Outpaint carries it: every other intent (and its fingerprint) is unchanged
        intent["canvas"] = canvas
    if sky is not None:  # w24-edit2: Restyle's Sky Replacement settings (the recipe names every value)
        intent["sky"] = sky
    if boxes is not None:  # only a box edit carries them (native: in the snapshot; mask: as the mask)
        intent.update(boxes=boxes, box_mode=box_mode)
    return intent


def outpaint_margins(value: Any) -> list[int]:
    """Outpaint's added canvas ``{left, top, right, bottom}`` in picture pixels -> [l, t, r, b]."""
    if not isinstance(value, Mapping) or set(value) != {"left", "top", "right", "bottom"}:
        raise EditRequestError("invalid_edit_request", "Outpaint needs left, top, right and bottom in pixels")
    out = []
    for key in ("left", "top", "right", "bottom"):
        v = value[key]
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= OUTPAINT_CANVAS_MAX_SIDE:
            raise EditRequestError("invalid_edit_request", "Outpaint margins must be whole pixels from 0")
        out.append(v)
    if not any(out):
        raise EditRequestError("invalid_edit_request", "Outpaint needs at least one side to extend")
    return out


def outpaint_canvas_size(size: tuple[int, int] | list[int], margins: list[int]) -> list[int]:
    """The canvas [w, h] for a picture of ``size`` with ``margins``; refuses an oversized canvas."""
    width, height = int(size[0]), int(size[1])
    left, top, right, bottom = margins
    if max(left, right) > OUTPAINT_MARGIN_FACTOR * width or max(top, bottom) > OUTPAINT_MARGIN_FACTOR * height:
        raise EditRequestError("outpaint_too_large", f"Outpaint adds at most {OUTPAINT_MARGIN_FACTOR} times the picture side")
    cw, ch = width + left + right, height + top + bottom
    if cw > OUTPAINT_CANVAS_MAX_SIDE or ch > OUTPAINT_CANVAS_MAX_SIDE or cw * ch > OUTPAINT_CANVAS_MAX_PIXELS:
        raise EditRequestError("outpaint_too_large", "The outpainted picture would be too large")
    return [cw, ch]


# --- crop math ----------------------------------------------------------------
def crop_box(bbox: tuple[int, int, int, int] | list[int], size: tuple[int, int], *, factor: float = CONTEXT_FACTOR_DEFAULT,
             min_side: int = MIN_CROP_SIDE, multiple: int = 32) -> list[int]:
    """The crop [x, y, w, h] around a mask bbox ``(x0, y0, x1, y1)`` (x1/y1 exclusive).

    Each side is the bbox side times ``factor``, at least ``min_side``, rounded
    up to ``multiple`` and never larger than the picture; the box is centred on
    the bbox, always covers it, and is shifted (not shrunk) at picture edges.
    """
    width, height = int(size[0]), int(size[1])
    x0, y0, x1, y1 = (int(v) for v in bbox)
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise EditRequestError("empty_mask", "The selection is empty")

    def side(lo: int, hi: int, limit: int) -> tuple[int, int]:
        length = hi - lo
        want = max(int(math.ceil(length * factor)), min_side, length)
        want = int(math.ceil(want / multiple) * multiple)
        want = min(want, limit)
        start = int(math.floor((lo + hi) / 2.0 - want / 2.0))
        start = min(start, lo)
        start = max(start, hi - want)
        start = max(0, min(start, limit - want))
        return start, want

    x, w = side(x0, x1, width)
    y, h = side(y0, y1, height)
    return [x, y, w, h]


def work_size(crop_w: int, crop_h: int, resolution: int = 1024, grid: int = 32) -> list[int]:
    """The model input size for a crop: TextEncodeQwenImage21's own rule.

    ``round(sqrt(R*R*ratio)/32)*32`` x ``round(sqrt(R*R/ratio)/32)*32``.  The
    Suite resizes the crop to exactly this size, so ComfyUI does no second
    resample and the output size is known before the run.
    """
    ratio = crop_w / crop_h
    w = round(math.sqrt(resolution * resolution * ratio) / grid) * grid
    h = round(math.sqrt(resolution * resolution / ratio) / grid) * grid
    w, h = max(grid, int(w)), max(grid, int(h))
    # The node applies the rule again to the picture it receives; iterate to
    # the fixed point so the size we send is the size it keeps (no resample).
    for _ in range(8):
        ratio = w / h
        nw = max(grid, int(round(math.sqrt(resolution * resolution * ratio) / grid) * grid))
        nh = max(grid, int(round(math.sqrt(resolution * resolution / ratio) / grid) * grid))
        if (nw, nh) == (w, h):
            break
        w, h = nw, nh
    return [w, h]


def where_phrase(mask_bbox: tuple[int, int, int, int] | list[int], crop: list[int]) -> str:
    """Plain-English location of the selection inside the crop (the model sees only the crop)."""
    x0, y0, x1, y1 = mask_bbox
    cx, cy, cw, ch = crop
    if (x1 - x0) * (y1 - y0) >= 0.6 * cw * ch:
        return ""
    fx = ((x0 + x1) / 2.0 - cx) / cw
    fy = ((y0 + y1) / 2.0 - cy) / ch
    col = "left" if fx < 1 / 3 else ("right" if fx > 2 / 3 else "")
    row = "top" if fy < 1 / 3 else ("bottom" if fy > 2 / 3 else "")
    if not col and not row:
        return "in the center of the picture"
    return "at the " + " ".join(p for p in (row, col) if p) + " of the picture"


LIGHT_MATCH = "lit like the rest of the picture: the same direction, colour and softness of light, with matching shadows and reflections"
# w22-img (IMG-129, live 116: the red boat came out front-lit in a backlit sunset): when the picture's light source
# lies behind the selection (image_edit_service.scene_light), Replace names that light instead of the generic match.
LIGHT_BACKLIT = ("with the light source directly behind it like everything else in the picture: it is a dark, almost black "
                 "silhouette against the bright background, its sides facing us in deep shadow as dark as the other "
                 "silhouettes in the picture, its own colours only faintly visible, a thin warm rim of light along its top "
                 "edges, and a long dark reflection of it on the water below, broken into the same horizontal ripples as the "
                 "other reflections on the water")
# w22-img2 (renders 06.11., kf0 oval + rect, seeds 111/7, harbour_measure.py): the wording above broke the reflection
# into ripples (row-to-row change 1.39-2.01x the water's, was 0.79-0.89x: a solid smear) and darkened the hull sides
# (mean 24.6-31.7, was 29.2-35.1). Asking for a smaller boat ("a third of the white ship's length") shrank it on the
# oval but repainted the rest of a rectangle selection as skyline, erasing part of the moored ship: not shipped.
LIGHT_BACKLIT_E4D85751 = ("with the light source directly behind it like everything else in the picture: it is a dark, almost "
                          "black silhouette against the bright background, its own colours only faintly visible in deep shadow, "
                          "a thin warm rim of light along its top edges, and a long dark reflection or shadow of it on the "
                          "surface below it")  # lane w22-img e4d85751 .. w22-img2: recorded snapshots keep it
# The first w22-img wording (render 06.11. 09:58, seeds 111/7): still front-lit to the eye; never shipped.

# w24-edit (Edit matrix 2026-11-07, image-lanes/w24-edit-MATRIX.md): FLUX.3 Klein filled a harbour rectangle with a boat
# cut off at the selection edge (seed 7); naming that the whole object fits inside the area gave a complete boat on
# seeds 7 and 111 on both matrix pictures (BFL's own advice: say in the instruction where the element goes).
FILL_INSIDE = "the whole of it small enough to fit inside the area, with the surrounding scene continuing all around it"
FILL_INSIDE_TARGETS = ("flux3-klein-sd-edit-v1",)
# w24-edit (matrix): FLUX.3 Klein's Outpaint margin came out as a lighter panel on a flat picture (lighthouse, seed 7);
# matched to the picture's tones (the area edits' colour match) it was seamless on seeds 7 and 111.
OUTPAINT_COLOR_MATCH_TARGETS = ("flux3-klein-sd-edit-v1",)

def instruction(intent: Mapping[str, Any], where: str, target_id: str | None = None) -> str:
    """The instruction sent to an instruction-edit family (before its prompt enhancer).

    A box edit (FLUX.3 Klein) is the caption + box sentences + BFL list (image_box.compile_prompt)
    with the boxes placed on the crop; a target whose Fill/Remove see the original crop gets them
    located by words (the area is not grey).
    """
    prompt = intent.get("prompt") or ""
    op = intent["operation"]
    if op == "box":
        return _boxes(lambda m: m.compile_prompt, prompt, intent["boxes"], intent["crop_box"])["prompt"]
    if intent.get("instruction_mode") == "raw":
        return prompt
    if op == "remove" and (intent.get("latent_mask") or {}).get("prefill"):
        return REMOVE_BACKGROUND_INSTRUCTION  # p13-img: LaMa filled the area; the model only continues the background
    at = (" " + where) if where else ""
    # A target that edits the original crop, or a Strength below 100 % (the area is not grey): located by words.
    if op in ("fill", "remove") and (EDIT_TARGETS.get(target_id, {}).get("fill_remove_on") == "original"
                                     or intent.get("strength") is not None):
        if op == "fill":
            return f"Add {prompt}{at}. Keep everything else in the picture exactly the same."
        what = prompt or intent.get("target") or "the object"
        return (f"Remove {what}{at} and fill that area with the surrounding background, matching its texture, "
                "colour and lighting. Keep everything else in the picture exactly the same.")
    # Fill and Remove send the crop with the masked area painted grey (see
    # image_edit_service.prepare): the grey says where, so no location phrase.
    # p11-img (owner's wording, 31.10.): the light of the new content matches the scene (live 111 / wave 10:
    # new boats lit frontally in a backlit harbour) and nothing of the old object stays (IMG-120). Fill keeps
    # its text: by the owner's rule (same seed, paste fixed) the new Fill text read better but made ghost leak
    # 2.33 -> 3.53 and the edge step 6.93 -> 8.81 worse on the real run (bench-kit/after-w11, case B).
    if op == "fill":
        if target_id in FILL_INSIDE_TARGETS:
            return f"Fill the grey area with {prompt}, {FILL_INSIDE}. Keep everything else in the picture exactly the same."
        return f"Fill the grey area with {prompt}. Keep everything else in the picture exactly the same."
    if op == "replace":
        target = intent.get("target") or ("the object" + at if at else "the main object")
        if intent.get("target") and at:
            target = f"{target}{at}"
        light = LIGHT_BACKLIT if intent.get("scene_light") == "backlit" else LIGHT_MATCH
        return (f"Replace {target} completely with {prompt}, so that nothing of {target} remains, {light}. "
                "Keep everything else in the picture exactly the same.")
    if op == "remove":
        # Real engine, 4 subjects x 4 seeds: "the surrounding background ... nothing is left there"
        # still put a glass/statue/cat there in 13 of 16; naming the background and forbidding any
        # object emptied 15 of 16 (one blank silhouette).
        return ("Fill the grey area with only the background that continues around it: the same ground, floor, "
                "table, wall, plants or sky, matching texture, colour, perspective and lighting. Do not add any "
                "object, person, figure or item there. Keep everything else in the picture exactly the same.")
    if op == "outpaint":
        # Real engine (person, right 40 % and all sides 20 %, seeds 7 and 11): naming the grey area a
        # continuation of the scene and forbidding a frame (E2) beat the Fill text (E1), which framed
        # the picture or repeated objects. The prompt is optional (what the new area shows).
        return ("Extend the picture into the grey area: continue the same scene, ground, background, light and "
                f"perspective seamlessly{(', with ' + prompt) if prompt else ''}. Do not add a frame or border. "
                "Keep everything else in the picture exactly the same.")
    if op == "restyle":
        # Real engine (person / glass, watercolour / ink / noir, seeds 7 and 11): "Restyle the whole picture"
        # restyles everything clearly; asking for "only the rendering style" without naming the background
        # dropped the hedge to white paper (98 % white). The mask alone confines a regional restyle.
        # p11-img (31.10.): Restyle 70 % put a second sun in the sky (IMG-121).
        return (f"Redraw the whole picture as {prompt}. Keep the same composition, every subject and the whole "
                f"background; change only the rendering style. Keep every light source where it is and add none.")
    if op == "relight" and target_id == KREA_LOOK_TARGET_ID:
        # w24-krea study round 4: the text below invented two people on Krea 2 Turbo (kf0 seed 111); Krea's own plain
        # instruction style (model card) kept the scene in 4 of 4 Turbo cells.
        return f"Change the lighting to {prompt}. Keep everything else in the picture exactly the same."
    if op == "relight":
        # Real engine (person, 4 lights x 3 wordings x 3 families): naming the light, then shadows,
        # highlights and colour, then everything to keep (pose, face, clothes, background) kept the scene.
        return (f"Relight the photo with {prompt}. Make the shadows, highlights and the colour of the light match "
                "it. Change only the lighting; keep the composition, every subject, their pose, face and clothes "
                "and the whole background exactly the same.")
    raise EditRequestError("edit_not_available", "This operation has no instruction template")


# Instructions earlier Suite versions recorded. A persisted job keeps the text it was sent with, so a
# rebuild accepts these as well; new jobs always get instruction(). Never remove an entry: a ledger
# holding one of these texts would refuse to open (every project's Image and Film state with it).
def legacy_instructions(intent: Mapping[str, Any], where: str, target_id: str | None = None) -> tuple[str, ...]:
    if intent.get("instruction_mode") == "raw" or intent.get("operation") == "box":
        return ()
    prompt = intent.get("prompt") or ""
    at = (" " + where) if where else ""
    op = intent["operation"]
    if op in ("fill", "remove") and EDIT_TARGETS.get(target_id, {}).get("fill_remove_on") == "original":
        # FLUX.2 Klein Fill/Remove recorded with the grey crop (until 2026-10-22) and every earlier text.
        return (instruction(intent, where),) + legacy_instructions(intent, where)
    if op == "fill":
        return (f"Add {prompt}{at}. Keep everything else in the picture exactly the same.",  # until 2026-10-17 (unpainted, by words)
                f"Fill the grey area with {prompt}. Keep everything else in the picture exactly the same.",  # FLUX.3 Klein until w24-edit
                f"Fill the grey area with {prompt}, {LIGHT_MATCH}, at the scene's scale and perspective. "
                "Keep everything else in the picture exactly the same.")  # 2026-10-31 only (p11-img bench, reverted)
    if op == "replace":  # until 2026-10-31 (p11-img: light match, nothing of the old object)
        target = intent.get("target") or ("the object" + at if at else "the main object")
        if intent.get("target") and at:
            target = f"{target}{at}"
        return (f"Replace {target} with {prompt}. Keep everything else in the picture exactly the same.",
                f"Replace {target} completely with {prompt}, so that nothing of {target} remains, {LIGHT_BACKLIT_E4D85751}. "
                "Keep everything else in the picture exactly the same.")  # w22-img e4d85751 backlit wording
    if op == "restyle":  # until 2026-10-31 (p11-img: no new light source)
        return (f"Redraw the whole picture as {prompt}. Keep the same composition, every subject and the whole "
                f"background; change only the rendering style.",)
    if op == "remove":
        what = prompt or intent.get("target") or "the object"
        return (
            f"Remove {what}{at} and fill that area with the surrounding background, matching its texture, "
            "colour and lighting. Keep everything else in the picture exactly the same.",  # until 2026-10-17
            "Fill the grey area with the surrounding background, matching its texture, colour and lighting, "
            "so that nothing is left there. Keep everything else in the picture exactly the same.",  # until 2026-10-18
        )
    return ()


def upload_name(crop_png_sha256: str) -> str:
    return f"filmsuite-edit-{crop_png_sha256[:32]}.png [temp]"


def edit_id_for(owner_scope: str, operation_id: str) -> str:
    return "imgedit_" + hashlib.sha256(f"{owner_scope}|{operation_id}".encode("utf-8")).hexdigest()[:32]


def variant_key(operation_id: str, index: int) -> str:
    """Ledger idempotency key of one variant (variant 0 keeps the caller's id)."""
    return operation_id if index == 0 else f"{operation_id}:var-{index}"


# --- ledger snapshot ----------------------------------------------------------
# Edit targets whose adapter wires Strength (KSampler denoise on the input image's latent).
STRENGTH_TARGETS = frozenset({QWEN_TARGET_ID}) | frozenset(STRENGTH_START)  # w24-edit2: every model with a measured start table

_EDIT_KEYS = {
    "schema", "edit_id", "operation", "prompt", "negative", "target", "instruction_mode", "prompt_enhancer",
    "color_match", "context_factor", "references", "variants", "variant_index", "source_sha256", "source_size",
    "mask_sha256", "mask_bbox", "crop_box", "work_size", "crop_sha256", "image",
}


def build_edit_snapshot(edit: Mapping[str, Any], target_id: str, basis: dict[str, Any], attach_policy: dict[str, Any],
                        seed: int, pin: tuple[str, str] | None = None) -> dict[str, Any]:
    """The ledger request snapshot of ONE edit variant (``operation: "edit"``).

    ``artist_input`` keeps the ledger's closed shape: the instruction (after
    the Suite's template), the variant seed and the exact output size (the
    work size - the model output of the crop is what the job publishes).
    Everything else that decides the render is in ``effective_input``.
    """
    if target_id not in EDIT_TARGETS:
        raise EditRequestError("edit_not_available", "This model has no wired edit workflow yet")
    target = EDIT_TARGETS[target_id]
    e = dict(edit)
    outpaint = e.get("operation") == "outpaint"
    boxed = e.get("operation") == "box"
    keys = set(e) - {"strength", "latent_mask", "strength_curve", "scene_light"}  # strength: only below 100 % and only on a target that declares it
    if keys != (_EDIT_KEYS | {"canvas"} if outpaint else _EDIT_KEYS | {"boxes"} if boxed else _EDIT_KEYS) or e["schema"] != SNAPSHOT_EDIT_SCHEMA:
        raise EditRequestError("invalid_edit_snapshot", "edit-shape")
    if "strength" in e:
        s = e["strength"]
        if target_id not in STRENGTH_TARGETS or isinstance(s, bool) or not isinstance(s, (int, float)) or not 0.0 <= float(s) < 1.0 \
                or not operation_capability(target["family"], target["variant"], e["operation"]).get("strength"):
            raise EditRequestError("invalid_edit_snapshot", "edit-strength")
    if "strength_curve" in e and ("strength" not in e or e["strength_curve"] not in (STRENGTH_CURVES.get(target_id), STRENGTH_CURVE_START if target_id in STRENGTH_START else None)):
        raise EditRequestError("invalid_edit_snapshot", "edit-strength-curve")
    if "latent_mask" in e and not latent_mask_accepted(e.get("operation"), target_id, e["latent_mask"]):
        raise EditRequestError("invalid_edit_snapshot", "edit-latent-mask")  # p13-img: only a declared step, exactly
    if "scene_light" in e and (e["scene_light"] not in SCENE_LIGHTS or e.get("operation") != "replace"
                               or (e.get("latent_mask") or {}).get("mode") != "scaled"):
        raise EditRequestError("invalid_edit_snapshot", "edit-scene-light")  # w22-img: measured by prepare, Replace only
    if boxed != (target_id == KLEINBOX_TARGET_ID):
        raise EditRequestError("invalid_edit_snapshot", "edit-box-target")
    if not _EDIT_ID.match(str(e["edit_id"])) or e["operation"] not in OPERATIONS_WIRED:
        raise EditRequestError("invalid_edit_snapshot", "edit-identity")
    for key in ("source_sha256", "mask_sha256", "crop_sha256"):
        if not isinstance(e[key], str) or not _HEX64.match(e[key]):
            raise EditRequestError("invalid_edit_snapshot", f"edit-{key}")
    if not isinstance(e["image"], str) or not _UPLOAD_NAME.match(e["image"]) or e["image"] != upload_name(e["crop_sha256"]):
        raise EditRequestError("invalid_edit_snapshot", "edit-image")
    for key, length in (("source_size", 2), ("mask_bbox", 4), ("crop_box", 4), ("work_size", 2)):
        value = e[key]
        if not isinstance(value, list) or len(value) != length or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in value):
            raise EditRequestError("invalid_edit_snapshot", f"edit-{key}")
    sw, sh = e["source_size"]
    if outpaint:  # the crop lies on the canvas: the picture with the added margins
        try:
            margins = outpaint_margins(dict(zip(("left", "top", "right", "bottom"), e["canvas"]))) \
                if isinstance(e["canvas"], list) and len(e["canvas"]) == 4 else None
            canvas_size = outpaint_canvas_size((sw, sh), margins) if margins and margins == e["canvas"] else None
        except EditRequestError:
            canvas_size = None
        if canvas_size is None:
            raise EditRequestError("invalid_edit_snapshot", "edit-canvas")
        sw, sh = canvas_size
    x, y, w, h = e["crop_box"]
    if not (1 <= sw <= 16384 and 1 <= sh <= 16384 and w >= 1 and h >= 1 and x + w <= sw and y + h <= sh):
        raise EditRequestError("invalid_edit_snapshot", "edit-crop-bounds")
    mx0, my0, mx1, my1 = e["mask_bbox"]
    if not (x <= mx0 < mx1 <= x + w and y <= my0 < my1 <= y + h):
        raise EditRequestError("invalid_edit_snapshot", "edit-mask-outside-crop")
    if boxed:  # the boxes are part of the request identity: canonical, on the picture, inside the crop
        try:
            canonical = box_module().normalize_boxes(copy.deepcopy(e["boxes"]), (sw, sh))
        except (box_module().BoxError, TypeError, KeyError):
            canonical = None
        cx0, cy0, cx1, cy1 = box_cover(e) if canonical == e["boxes"] else (-1, -1, -1, -1)
        if canonical != e["boxes"] or not (x <= cx0 and y <= cy0 and cx1 <= x + w and cy1 <= y + h):
            raise EditRequestError("invalid_edit_snapshot", "edit-boxes")
    if e["work_size"] != work_size(w, h, target["work_resolution"], target["grid"]):
        raise EditRequestError("invalid_edit_snapshot", "edit-work-size")
    if isinstance(e["variants"], bool) or not isinstance(e["variants"], int) or not 1 <= e["variants"] <= MAX_VARIANTS \
            or isinstance(e["variant_index"], bool) or not isinstance(e["variant_index"], int) \
            or not 0 <= e["variant_index"] < e["variants"]:
        raise EditRequestError("invalid_edit_snapshot", "edit-variants")
    for key in ("prompt_enhancer", "color_match"):
        if not isinstance(e[key], bool):
            raise EditRequestError("invalid_edit_snapshot", f"edit-{key}")
    if e["instruction_mode"] not in ("template", "raw") or not isinstance(e["context_factor"], (int, float)) \
            or isinstance(e["context_factor"], bool):
        raise EditRequestError("invalid_edit_snapshot", "edit-options")
    for key in ("prompt", "negative", "target"):
        if not isinstance(e[key], str) or len(e[key]) > MAX_PROMPT:
            raise EditRequestError("invalid_edit_snapshot", f"edit-{key}")
    if not isinstance(e["references"], list) or len(e["references"]) > MAX_REFERENCES:
        raise EditRequestError("invalid_edit_snapshot", "edit-references")
    text = instruction(e, where_phrase(e["mask_bbox"], e["crop_box"]), target_id)
    if not text or len(text) > 16_384:
        raise EditRequestError("invalid_edit_snapshot", "edit-instruction")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 18_446_744_073_709_551_615:
        raise EditRequestError("invalid_edit_snapshot", "edit-seed")
    files, sampling = target["files"], target["sampling"]
    if pin is not None and tuple(pin) not in edit_pins(target_id):  # w32-fix5: only a pin this target had
        raise EditRequestError("invalid_edit_snapshot", "edit-pin")
    capability, graph = tuple(pin) if pin is not None else (target["capability_manifest_sha256"], target["api_graph_sha256"])
    return {
        "operation": "edit",
        "target_id": target_id,
        "artist_input": {"prompt": text, "seed": seed, "width": e["work_size"][0], "height": e["work_size"][1]},
        "effective_input": {
            "edit": copy.deepcopy(e),
            "model": files["diffusion_model"], "encoder": files["text_encoder"], "vae": files["vae"],
            "lora": files["lora"], "lora_strength": sampling["lora_strength"], "prompt_enhancer_model": files["prompt_enhancer"],
            "steps": (start_schedule(strength_start(e["strength"], target_id), int(sampling["steps"]))["steps"]
                      if e.get("strength_curve") == STRENGTH_CURVE_START else sampling["steps"]),
            "cfg": sampling["cfg"], "sampler": sampling["sampler"],
            "scheduler": sampling["scheduler"],
            "denoise": (e["latent_mask"]["denoise"] if (e.get("latent_mask") or {}).get("prefill")
                        else effective_denoise(e["strength"], e.get("strength_curve"), target_id) if "strength" in e
                        else (e.get("latent_mask") or {}).get("denoise", sampling["denoise"])), "batch": 1,
        },
        "context_basis": copy.deepcopy(basis),
        "attach_policy": copy.deepcopy(attach_policy),
        "capability_manifest_sha256": capability,
        "api_graph_sha256": graph,
    }


def is_edit_snapshot(snapshot: Any) -> bool:
    return isinstance(snapshot, Mapping) and snapshot.get("operation") == "edit" and snapshot.get("target_id") in EDIT_TARGETS


def rebuild_edit_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild a snapshot from its own intent (the ledger compares canonical bytes)."""
    effective = snapshot.get("effective_input")
    artist = snapshot.get("artist_input")
    if not isinstance(effective, Mapping) or not isinstance(artist, Mapping) or not isinstance(effective.get("edit"), Mapping):
        raise EditRequestError("invalid_edit_snapshot", "edit-snapshot-shape")
    # w32-fix5: a job stored under an earlier pin of its target rebuilds under that pin (its own bytes), never today's.
    pin = (snapshot.get("capability_manifest_sha256"), snapshot.get("api_graph_sha256"))
    rebuilt = build_edit_snapshot(effective["edit"], snapshot["target_id"], snapshot["context_basis"], snapshot["attach_policy"], artist.get("seed"),
                                  pin if snapshot.get("target_id") in EDIT_TARGETS and pin in edit_pins(snapshot["target_id"]) else None)
    sent = artist.get("prompt")
    if sent != rebuilt["artist_input"]["prompt"]:
        edit = effective["edit"]
        if isinstance(sent, str) and sent in legacy_instructions(edit, where_phrase(edit["mask_bbox"], edit["crop_box"]), snapshot["target_id"]):
            rebuilt["artist_input"]["prompt"] = sent
    return rebuilt


__all__ = [
    "CAPABILITIES_SCHEMA", "EDIT_TARGETS", "EditRequestError", "MAX_VARIANTS", "OPERATIONS", "OPERATIONS_DECLARED",
    "OUTPAINT_MARGIN_FACTOR", "outpaint_canvas_size", "outpaint_margins",
    "KLEIN2_TARGET_ID", "KLEINBOX_TARGET_ID", "KREA_LOOK_TARGET_ID", "KREA_REPLACE_TARGET_ID", "MATTE_OPERATIONS", "SKY_DEFAULTS", "sky_settings", "STRENGTH_START", "start_schedule", "strength_start", "paste_for", "latent_mask_for", "latent_mask_accepted", "LATENT_MASK_SPECS", "LEGACY_LATENT_MASK_SPECS", "LINE_ART_LATENT_MASK", "SCENE_LIGHTS", "effective_denoise", "strength_fields", "KLEIN_TARGET_ID", "box_cover", "box_module", "neutral_for", "OPERATIONS_WIRED", "OPERATION_SCHEMA", "QWEN_TARGET_ID", "REQUEST_SCHEMA", "RESULT_SCHEMA", "SNAPSHOT_EDIT_SCHEMA",
    "build_edit_snapshot", "crop_box", "edit_id_for", "family_entry", "instruction", "is_edit_snapshot", "legacy_instructions",
    "load_capabilities", "normalize_request", "operation_capability", "rebuild_edit_snapshot", "upload_name",
    "variant_key", "where_phrase", "work_size",
    "PH3_TARGET_ID",  # w24-h3img
    "earlier_edit_pins", "edit_pins",  # w32-fix5
]
