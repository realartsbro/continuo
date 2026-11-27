"""R4 edit service: Suite-owned crop & stitch and the edit bookkeeping around ledger jobs.

Loaded unregistered by ``plugin_api`` (like ``image_mask_jobs``): no bare
relative imports, no ``@dataclass``.  Pixel work (PIL + numpy, present in the
Hermes venv) always runs in a worker thread.

Crop & stitch
-------------
* ``prepare``: decode the owned source picture and the 8-bit mask (same size,
  white = change, soft edges allowed), find the mask bbox (> 0), compute the
  crop (contract ``crop_box``: context factor, min 512, multiple of 32,
  shifted at the picture edges), resize the crop to the family work size and
  encode it as PNG (RGB).  Deterministic: same bytes in, same crop bytes out.
* ``stitch``: resize the model output back to the crop size, optionally
  colour-match it to the source crop (mean/std per channel, measured only
  outside the mask), and build the PATCH: an RGBA PNG at source size whose
  alpha is the soft mask (0 outside the mask, so the composite outside the
  mask is the original byte for byte).  Also returns the composited picture
  and the proof numbers (sha256 of the unmasked pixels before/after).

Edit records
------------
An edit is one ``studio.image-edit-request/1`` accepted by ``POST /image/edit``:
``edit_id`` (derived from owner scope + operation id), the variant job ids,
the request, crop facts, and the uploaded crop name.  Records live in memory
(bounded) and are mirrored to a small JSON file per edit under
``plugin-data/film-suite/image-edits/`` so a result can be rebuilt after a
backend restart (mask bytes are stored once by hash, write-once).
"""
from __future__ import annotations

import base64
import hashlib
import math
import io
import json
import os
import re
import tempfile
import time
import urllib.request
import uuid
from collections import OrderedDict
from pathlib import Path
# Lane w47-q3: the one Windows file-sharing retry rule for replaced state files (studio_core/win_io.py, stdlib, by path).
import importlib.util as _win_io_import
_win_io_spec = _win_io_import.spec_from_file_location("film_suite_image_edit_service_win_io", Path(__file__).resolve().parents[1] / "studio_core" / "win_io.py")
_win_io = _win_io_import.module_from_spec(_win_io_spec)
_win_io_spec.loader.exec_module(_win_io)
from typing import Any, Mapping

MAX_SOURCE_PIXELS = 67_108_864
MASK_MAX_BYTES = 32 * 1024 * 1024
UPLOAD_MAX_BYTES = 64 * 1024 * 1024
RECORDS_MAX = 64
_EDIT_ID = re.compile(r"^imgedit_[0-9a-f]{32}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class EditServiceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _imports():
    import numpy as np  # type: ignore[import-not-found]
    from PIL import Image  # type: ignore[import-not-found]
    return np, Image


def _png(image: Any) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=False, compress_level=6)
    return out.getvalue()


def decode_mask(data: bytes, size: tuple[int, int]) -> Any:
    """8-bit mask at source size; white (255) = change.  RGBA/LA masks use luminance, not alpha."""
    np, Image = _imports()
    if not isinstance(data, (bytes, bytearray)) or not data or len(data) > MASK_MAX_BYTES:
        raise EditServiceError("invalid_mask", "The selection mask is invalid")
    try:
        with Image.open(io.BytesIO(bytes(data))) as image:
            image.load()
            if image.format != "PNG":
                raise EditServiceError("invalid_mask", "The selection mask must be a PNG")
            if image.size != tuple(size):
                raise EditServiceError("mask_size_mismatch", f"The selection mask must be {size[0]} x {size[1]} (the picture size)")
            mask = image.convert("L")
    except EditServiceError:
        raise
    except Exception as exc:  # noqa: BLE001 - finite answer
        raise EditServiceError("invalid_mask", "The selection mask is invalid") from exc
    return np.asarray(mask, dtype=np.uint8).copy()


def mask_bbox(mask: Any) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) of all pixels > 0 (x1/y1 exclusive), or None when empty."""
    np, _ = _imports()
    ys, xs = np.nonzero(mask > 0)
    if xs.size == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


# p13-img: the owned LaMa engine (inpaint_core/lama_engine.py) pre-fills Remove; absent or failing = a named
# refusal with its engine status (never the old grey path).
REFUSAL_STATUS = {"inpaint_engine_missing": 503, "inpaint_engine_failed": 502}
_INPAINT_ENGINE: list[Any] = []


def inpaint_engine() -> Any:
    """inpaint_core/lama_engine.py, loaded once unregistered (no torch: one runner subprocess per job)."""
    if not _INPAINT_ENGINE:
        import importlib.util
        path = Path(__file__).resolve().parents[1] / "inpaint_core" / "lama_engine.py"
        spec = importlib.util.spec_from_file_location("film_suite_inpaint_lama_engine", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        _INPAINT_ENGINE.append(module)
    return _INPAINT_ENGINE[0]


def lama_prefill(crop_png: bytes, hole_png: bytes) -> dict[str, Any]:
    """The default pre-fill: one LaMa engine run; its refusals become EditServiceError with the same code."""
    engine = inpaint_engine()
    try:
        return engine.inpaint(crop_png, hole_png)
    except engine.InpaintEngineError as exc:
        raise EditServiceError(exc.code, exc.message) from None


NEUTRAL_GREY = 128
# How far the grey reaches past the mask, as a fraction of the crop's longer side.  Measured on the
# real engine (own test pictures: backlit hair, bare tree, white cat, wine glass, the user's spoon):
# - Remove needs a generous margin: at 1.1 % the grey silhouette told the model the shape and it
#   painted another cat / a leafy tree / a filled glass; at 4 % all four were gone, spoon ghost-free.
# - Fill must respect the selection exactly: any grow eats into a background mask's subject (hair
#   strands lost, the whole bare tree erased at 1.1 %).  Grey where the mask is mostly in (>= 128),
#   pasted back with the soft mask itself: strands, twigs, fur and glass survive.
REMOVE_GROW_FRACTION = 0.04
FILL_GREY_AT = 128
LEGACY_GROW_FRACTION = 0.011  # edits recorded as ``neutral: true`` before the split


def neutral_grow_px(crop: list[int], mode: Any = "remove") -> int:
    """Grow of the grey area for a neutral mode ("fill" 0, "remove" 4 %, legacy True 1.1 %)."""
    if mode in ("fill", "outpaint") or not mode:
        return 0
    fraction = REMOVE_GROW_FRACTION if mode == "remove" else LEGACY_GROW_FRACTION
    return max(2, int(round(max(int(crop[2]), int(crop[3])) * fraction)))


def _neutral_mode(neutral: Any) -> Any:
    if neutral in (None, False):
        return None
    if neutral in ("fill", "remove", "outpaint"):
        return neutral
    return True  # legacy


def edit_region(mask: Any, grow: int) -> Any:
    """The area an edit replaces: the mask grown by ``grow`` px with a soft edge (uint8, same size).

    Fill and Remove paint this area grey for the model AND paste the result
    back over this same area. A tight mask leaves the object's rim highlights
    just outside it; pasting only inside the mask keeps them as an outline
    (measured on the spoon: ghost at 0 px, faint at 3 px, clean at 6 px).
    """
    np, Image = _imports()
    if grow <= 0:
        return mask
    from PIL import ImageFilter  # type: ignore[import-not-found]
    region = Image.fromarray(mask).filter(ImageFilter.MaxFilter(2 * grow + 1)).filter(ImageFilter.GaussianBlur(grow / 2.0))
    return np.maximum(np.asarray(region, dtype=np.uint8), mask)


def _neutralise(piece: Any, region: Any, work: tuple[int, int], grey_at: int = 1) -> Any:
    """Paint the edit region of the crop neutral grey where ``region >= grey_at`` (hard edge).

    Instruction-edit models see only the crop. With the old content visible
    they keep it - Fill inside an object redraws the object, Remove leaves a
    ghost of it. A grey hole has to be painted.
    """
    np, Image = _imports()
    hard = Image.fromarray(((region >= max(1, grey_at)) * 255).astype(np.uint8)).resize(work, Image.Resampling.BILINEAR)
    alpha = np.asarray(hard, dtype=np.float32)[..., None] / 255.0
    rgb = np.asarray(piece, dtype=np.float32)
    out = rgb * (1.0 - alpha) + float(NEUTRAL_GREY) * alpha
    return Image.fromarray(np.clip(np.rint(out), 0, 255).astype(np.uint8), mode="RGB")


def soft_mask(mask: Any, crop: list[int], work: list[int] | tuple[int, int], spec: Mapping[str, Any],
              source: Any = None) -> tuple[Any, Any]:
    """The latent-mask step's soft mask (p13-img, IMG-129): ``(work_mask, full_mask)``, both uint8, 255 = paint.

    Made at the WORK size (the size the model samples): the selection resized into the crop's work size,
    grown by ``grow_px`` (max filter) and blurred by ``blur_px`` (Gaussian), never below the selection itself
    (inside the selection the model always paints fully). ``full_mask`` is that work mask resized back to
    the crop (bilinear), again never below the selection, at source size with 0 outside the crop: the
    composite mask of ``stitch`` is exactly the mask the sampler denoised through.
    """
    np, Image = _imports()
    from PIL import ImageFilter  # type: ignore[import-not-found]
    x, y, w, h = (int(v) for v in crop)
    work = (int(work[0]), int(work[1]))
    piece = np.ascontiguousarray(mask[y:y + h, x:x + w])
    sel = Image.fromarray(piece, mode="L").resize(work, Image.Resampling.BILINEAR)
    sel_np = np.asarray(sel, dtype=np.uint8)
    if spec.get("mode") == "scaled":
        region = Image.fromarray(_scaled_region(sel_np, spec), mode="L")
    else:  # the fixed step p13-img recorded (grow_px / blur_px at the work size)
        grow, blur = int(spec["grow_px"]), float(spec["blur_px"])
        region = sel
        if grow > 0:
            region = region.filter(ImageFilter.MaxFilter(2 * grow + 1))
        if blur > 0:
            region = region.filter(ImageFilter.GaussianBlur(blur))
    work_mask = np.maximum(np.asarray(region, dtype=np.uint8), np.where(sel_np >= 128, 255, sel_np).astype(np.uint8))
    back = np.asarray(Image.fromarray(work_mask, mode="L").resize((w, h), Image.Resampling.BILINEAR), dtype=np.uint8)
    full = np.zeros(mask.shape, dtype=np.uint8)
    full[y:y + h, x:x + w] = np.maximum(back, piece)
    if spec.get("keep_edge_objects") and source is not None:  # w23-img: small objects at the selection edge stay
        keep = edge_objects(source, mask, spec)
        if keep is not None:
            full[y:y + h, x:x + w] = np.rint(full[y:y + h, x:x + w] * (1.0 - keep[y:y + h, x:x + w])).astype(np.uint8)
            kw = np.asarray(Image.fromarray(np.rint(keep[y:y + h, x:x + w] * 255).astype(np.uint8), mode="L")
                            .resize(work, Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
            work_mask = np.rint(work_mask * (1.0 - kw)).astype(np.uint8)
    return work_mask, full


# w23-img (IMG-129, maintainer decision): small objects at the selection edge stay in the picture (a small object at
# the harbour oval's bottom edge was repainted by every w22 render, ~50 of 255 mean change). An object is a compact
# blob that stands out from its row (|luminance - the row's running median over 30 % of the selection width| > 24),
# at most a quarter of the selection's width and height, in the band from 10 % above the selection's bottom to the end
# of the soft edge below, and mostly (more than half) outside the selection. An object mostly inside the selection is
# the artist's to replace: the small object at the kf0 oval's edge has 35 of its 38 dark px inside, and keeping it
# pasted its old outline over the new hull (render 06.11. 17:41, vision: "a pasted artifact"). Kept on kf0: the small
# objects over the water below the oval (x 447-603, y 326-375); the moored ship's hull line is never kept (a long run
# is scenery).
EDGE_OBJECT_STEP = 24
EDGE_OBJECT_MAX_REL = 0.25
EDGE_OBJECT_MIN_PX = 10


def edge_objects(source: Any, mask: Any, spec: Mapping[str, Any]) -> Any:
    """Float keep mask (0..1, source size) of the small things the selection's bottom edge cuts, or None."""
    np, _Image = _imports()
    from scipy import ndimage  # type: ignore[import-not-found]
    rgb = np.asarray(source, dtype=np.float32)
    lum = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    height, width = lum.shape
    sel = mask >= 128
    ys, xs = np.nonzero(sel)
    if ys.size == 0:
        return None
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    sw, sh = x1 - x0 + 1, y1 - y0 + 1
    feather = max(float(spec.get("feather_rel", 0.10)) * math.hypot(sw, sh), float(spec.get("feather_min_px", 32)))
    reach = int(round(float(spec.get("grow_offset_px", 4)) + feather / 2.0 + feather))
    edge = max(2, int(round(0.10 * sh)))
    r0, r1 = max(0, y1 - edge), min(height, y1 + int(round(float(spec.get("below_rel", 0.0)) * sh)) + reach + 1)
    c0, c1 = max(0, x0 - reach), min(width, x1 + reach + 1)
    band = lum[r0:r1, c0:c1]
    if band.size == 0:
        return None
    k = max(9, int(0.3 * sw)) | 1
    odd = np.abs(band - ndimage.median_filter(band, size=(1, k), mode="nearest")) > EDGE_OBJECT_STEP
    # a long horizontal run (a hull line, a quay, a horizon) is scenery, not a thing: cut it out so a small thing
    # touching it (a small object's tip on the moored ship's waterline) is its own blob
    run_max = max(3, int(EDGE_OBJECT_MAX_REL * sw))
    runs, _rn = ndimage.label(odd, structure=np.array([[0, 0, 0], [1, 1, 1], [0, 0, 0]]))
    sizes = np.bincount(runs.ravel())
    sizes[0] = 0
    odd &= sizes[runs] <= run_max
    labels, _n = ndimage.label(odd, structure=np.ones((3, 3)))
    inside = sel[r0:r1, c0:c1]
    keep = np.zeros(band.shape, dtype=bool)
    for index, box in enumerate(ndimage.find_objects(labels), 1):
        if box is None:
            continue
        blob = labels[box] == index
        area = int(blob.sum())
        bh, bw = box[0].stop - box[0].start, box[1].stop - box[1].start
        if area < EDGE_OBJECT_MIN_PX or bw > EDGE_OBJECT_MAX_REL * sw or bh > EDGE_OBJECT_MAX_REL * sh or box[0].start == 0:
            continue
        if int((blob & inside[box]).sum()) * 2 > area:
            continue  # mostly inside the selection: the artist selected it (maintainer rule)
        keep[box] |= blob
    if not keep.any():
        return None
    keep = ndimage.binary_dilation(keep, iterations=2)
    soft = np.zeros(lum.shape, dtype=np.float32)
    soft[r0:r1, c0:c1] = ndimage.gaussian_filter(keep.astype(np.float32), 1.0)
    soft[r0:r1, c0:c1] = np.maximum(soft[r0:r1, c0:c1], keep.astype(np.float32))
    return np.clip(soft, 0.0, 1.0)


def scaled_extent(sel: Any, spec: Mapping[str, Any]) -> dict[str, int]:
    """The scaled step's numbers on one work-size selection: feather (blur), grow and the room below (px)."""
    np, _Image = _imports()
    ys, xs = np.nonzero(sel >= 128)
    if ys.size == 0:
        ys, xs = np.nonzero(sel > 0)
    sw, sh = (int(xs.max()) - int(xs.min()) + 1, int(ys.max()) - int(ys.min()) + 1) if ys.size else (1, 1)
    feather = max(float(spec["feather_rel"]) * math.hypot(sw, sh), float(spec["feather_min_px"]))
    if spec.get("feather_max_short_rel") is not None:  # w22-img2: never wider than a quarter of a small selection
        feather = max(1.0, min(feather, float(spec["feather_max_short_rel"]) * min(sw, sh)))
    return {"feather": int(round(feather)), "grow": int(round(float(spec["grow_offset_px"]) + feather / 2.0)),
            "below": int(round(float(spec["below_rel"]) * sh))}


def _shift_max(a: Any, steps: int, axis: int, both: bool = True) -> Any:
    """Grey dilation along one axis by ``steps`` px (both directions, or only towards +axis)."""
    np, _Image = _imports()
    out = a.copy()
    for _ in range(max(0, int(steps))):
        nxt = out.copy()
        if axis == 0:
            nxt[1:] = np.maximum(nxt[1:], out[:-1])
            if both:
                nxt[:-1] = np.maximum(nxt[:-1], out[1:])
        else:
            nxt[:, 1:] = np.maximum(nxt[:, 1:], out[:, :-1])
            if both:
                nxt[:, :-1] = np.maximum(nxt[:, :-1], out[:, 1:])
        out = nxt
    return out


def _scaled_region(sel: Any, spec: Mapping[str, Any]) -> Any:
    """w22-img (IMG-129): the selection extended down by ``below``, grown by ``grow`` and blurred by feather / 2 (sigma)."""
    np, Image = _imports()
    from PIL import ImageFilter  # type: ignore[import-not-found]
    ext = scaled_extent(sel, spec)
    region = _shift_max(sel, ext["below"], 0, both=False)
    region = _shift_max(_shift_max(region, ext["grow"], 0), ext["grow"], 1)
    blurred = Image.fromarray(region.astype(np.uint8), mode="L").filter(ImageFilter.GaussianBlur(ext["feather"] / 2.0))
    return np.asarray(blurred, dtype=np.uint8)


# w22-img (IMG-129, live 116): where the picture's light source sits relative to the selection. The brightest 0.2 %
# of the picture outside the selection is the light core; it counts as a light source when it is compact (a sun, a
# lamp, a window; not a whole bright sky), clearly brighter than the picture's median, ABOVE the selection's centre
# and within half a selection width of it horizontally: then whatever stands in the selection is seen against it.
LIGHT_CORE_QUANTILE = 0.998
LIGHT_CORE_MIN_RATIO = 1.4
LIGHT_CORE_MAX_SPREAD = 0.08  # std of the core's x and y, as a fraction of the picture's longer side


# w22-img2: a flat-ink plate (manga, comic, line drawing) is almost only neutral black and white around the selection.
INK_LEVELS = (40, 215)       # luminance at or below / at or above: ink or paper
INK_MAX_CHROMA = 24          # max - min of R, G, B: neutral
INK_MIN_SHARE = 0.90         # share of the crop outside the selection that is ink or paper


def plate_kind(source: Any, mask: Any, crop: list[int]) -> str:
    """``"ink"`` when the crop around the selection is flat black/white line art, else ``"painted"``."""
    np, _Image = _imports()
    x, y, w, h = (int(v) for v in crop)
    rgb = np.asarray(source, dtype=np.int16)[y:y + h, x:x + w]
    outside = mask[y:y + h, x:x + w] == 0
    if int(outside.sum()) < 100:
        return "painted"
    lum = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    neutral = (rgb.max(axis=-1) - rgb.min(axis=-1)) < INK_MAX_CHROMA
    ink = neutral & ((lum <= INK_LEVELS[0]) | (lum >= INK_LEVELS[1]))
    return "ink" if float(ink[outside].mean()) >= INK_MIN_SHARE else "painted"


def scene_light(source: Any, mask: Any) -> str:
    """``"backlit"`` when a compact light source stands behind/above the selection, else ``"front"``."""
    np, _Image = _imports()
    rgb = np.asarray(source, dtype=np.float32)
    lum = rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114
    sel = mask > 0
    ys, xs = np.nonzero(sel)
    if ys.size == 0 or (~sel).sum() < 100:
        return "front"
    outside = lum[~sel]
    core_at = float(np.quantile(outside, LIGHT_CORE_QUANTILE))
    if core_at < LIGHT_CORE_MIN_RATIO * max(1.0, float(np.median(lum))):
        return "front"
    cy, cx = np.nonzero((lum >= core_at) & ~sel)
    longer = float(max(lum.shape))
    if float(cx.std()) > LIGHT_CORE_MAX_SPREAD * longer or float(cy.std()) > LIGHT_CORE_MAX_SPREAD * longer:
        return "front"
    x0, x1, y0, y1 = int(xs.min()), int(xs.max()), int(ys.min()), int(ys.max())
    half = (x1 - x0 + 1) / 2.0
    if x0 - half <= float(cx.mean()) <= x1 + half and float(cy.mean()) < (y0 + y1) / 2.0:
        return "backlit"
    return "front"


def prepare(source_png: bytes, mask_png: bytes, *, contract: Any, factor: float, target: Mapping[str, Any],
            neutral: Any = False, cover: list[int] | None = None, latent: Mapping[str, Any] | None = None,
            prefill: Any = None) -> dict[str, Any]:
    """Crop the source around the mask for the family; returns crop PNG + facts.

    ``neutral`` "fill" / "remove": the masked area of the crop is painted grey
    first so the model paints something new there instead of keeping it
    (Fill: exactly the selection; Remove: grown, see REMOVE_GROW_FRACTION).
    ``cover`` (x0, y0, x1, y1): the crop also shows this area (a box edit: Keep boxes, Move targets).
    ``latent`` (the contract's latent-mask step): the crop PNG is RGBA whose alpha is 255 - the soft mask
    (ComfyUI ``LoadImage`` output 1 = 1 - alpha = the mask the sampler denoises through).
    ``prefill`` (Remove, latent step with ``prefill``): a callable ``(crop_png, mask_png) -> {"image_png", ...}``
    (inpaint_core.lama_engine.inpaint) run on the crop at its own size with the selection grown
    ``prefill_grow_px``; the filled pixels replace the crop inside that hard mask before it is resized and sent.
    """
    np, Image = _imports()
    with Image.open(io.BytesIO(source_png)) as image:
        image.load()
        source = image.convert("RGB")
    width, height = source.size
    if width * height > MAX_SOURCE_PIXELS:
        raise EditServiceError("picture_too_large", "The picture is too large to edit")
    mask = decode_mask(mask_png, (width, height))
    bbox = mask_bbox(mask)
    if bbox is None:
        raise EditServiceError("empty_mask", "The selection is empty")
    around = bbox if cover is None else (min(bbox[0], cover[0]), min(bbox[1], cover[1]), max(bbox[2], cover[2]), max(bbox[3], cover[3]))
    crop = contract.crop_box(around, (width, height), factor=factor, min_side=contract.MIN_CROP_SIDE, multiple=int(target["grid"]))
    work = contract.work_size(crop[2], crop[3], int(target["work_resolution"]), int(target["grid"]))
    x, y, w, h = crop
    piece = source.crop((x, y, x + w, y + h))
    prefill_facts = None
    if latent is not None and latent.get("prefill"):
        if prefill is None:
            if inpaint_engine().locate() is None:
                raise EditServiceError("inpaint_engine_missing", "Remove needs the LaMa inpaint engine, which is not installed")
            prefill = lama_prefill
        from PIL import ImageFilter  # type: ignore[import-not-found]
        grow = int(latent.get("prefill_grow_px") or 0)
        hole = Image.fromarray(np.where(mask[y:y + h, x:x + w] > 0, 255, 0).astype(np.uint8), mode="L")
        if grow > 0:
            hole = hole.filter(ImageFilter.MaxFilter(2 * grow + 1))
        done = prefill(_png(piece), _png(hole))
        with Image.open(io.BytesIO(done["image_png"])) as filled_image:
            filled_image.load()
            filled = filled_image.convert("RGB")
        if filled.size != piece.size:
            raise EditServiceError("inpaint_engine_failed", "The LaMa inpaint engine returned a picture of the wrong size")
        piece = Image.composite(filled, piece, hole)
        prefill_facts = {"engine": done.get("engine"), "device": done.get("device"), "wall_ms": done.get("wall_ms"),
                         "grow_px": grow, "filled_pixels": int((np.asarray(hole) > 0).sum())}
    if (w, h) != tuple(work):
        piece = piece.resize(tuple(work), Image.Resampling.LANCZOS)
    mode = None if latent is not None else _neutral_mode(neutral)  # p13-img: the latent mask replaces the grey hole
    if mode == "outpaint":  # the canvas margin is already grey; the seam band keeps the picture
        pass
    elif mode:
        region = edit_region(mask, neutral_grow_px(crop, mode))
        piece = _neutralise(piece, region[y:y + h, x:x + w], tuple(work), FILL_GREY_AT if mode == "fill" else 1)
    latent_facts = None
    if latent is not None and latent.get("mode") == "scaled" and plate_kind(np.asarray(source), mask, crop) == "ink":
        latent = dict(contract.LINE_ART_LATENT_MASK["replace"])  # w22-img2: line art keeps the crisp step
    light = scene_light(np.asarray(source), mask) if latent is not None and latent.get("mode") == "scaled" else None
    if latent is not None:
        work_mask, full = soft_mask(mask, crop, work, latent, source=np.asarray(source))
        piece = piece.convert("RGB")
        piece.putalpha(Image.fromarray((255 - work_mask).astype(np.uint8), mode="L"))
        latent_facts = {**dict(latent), "soft_pixels": int((full > 0).sum())}
    crop_png = _png(piece)
    coverage = float((mask > 0).mean() * 100.0)
    return {
        "source_size": [width, height],
        "mask_bbox": list(bbox),
        "crop_box": crop,
        "work_size": list(work),
        "crop_png": crop_png,
        "crop_sha256": hashlib.sha256(crop_png).hexdigest(),
        "mask_sha256": hashlib.sha256(mask_png).hexdigest(),
        "mask_coverage_pct": round(coverage, 3),
        "neutral": mode or False,
        **({"latent_mask": latent_facts} if latent_facts is not None else {}),
        **({"prefill": prefill_facts} if prefill_facts is not None else {}),
        # what the edit route stores: the snapshot's latent step (part of the request identity) and the record's facts
        **({"snapshot_edit": {"latent_mask": dict(latent), **({"scene_light": light} if light else {})},
            "record_crop": {"latent_mask": {**dict(latent), "work_size": list(work)}, **({"scene_light": light} if light else {}),
                            **({"prefill": prefill_facts} if prefill_facts is not None else {})}} if latent is not None else {}),
    }


# Outpaint: how far the paste-back reaches into the picture past each extended edge, as a fraction of
# the picture's longer side (a ramp from the edge inwards). Real engine (person, all sides 20 %, seeds
# 7 and 11): a hard edge left a visible seam (22.7 mean step across the old edge, 6.2 inside); a 1-2 %
# ramp brought the step down to the picture's own (6.2); 4 % redrew visibly more of the picture.
OUTPAINT_BAND_FRACTION = 0.02


def outpaint_canvas(source_png: bytes, margins: list[int] | tuple[int, int, int, int]) -> dict[str, Any]:
    """The picture on a larger canvas for Outpaint: ``canvas_png`` (RGB, the new margin neutral grey),
    ``mask_png`` (8-bit: 255 in the margin, a ramp over the seam band inside, 0 elsewhere) and sizes.

    Deterministic, so the result route rebuilds exactly what the edit was made from.
    """
    np, Image = _imports()
    with Image.open(io.BytesIO(source_png)) as image:
        image.load()
        source = image.convert("RGB")
    width, height = source.size
    left, top, right, bottom = (int(v) for v in margins)
    cw, ch = width + left + right, height + top + bottom
    if cw * ch > MAX_SOURCE_PIXELS:
        raise EditServiceError("picture_too_large", "The outpainted picture would be too large")
    canvas = Image.new("RGB", (cw, ch), (NEUTRAL_GREY, NEUTRAL_GREY, NEUTRAL_GREY))
    canvas.paste(source, (left, top))
    mask = np.full((ch, cw), 255, dtype=np.float32)
    inner = np.zeros((height, width), dtype=np.float32)
    band = max(2, int(round(max(width, height) * OUTPAINT_BAND_FRACTION)))
    ramp_x = np.arange(width, dtype=np.float32)
    ramp_y = np.arange(height, dtype=np.float32)
    for extended, distance, axis in ((left, ramp_x, 1), (right, ramp_x[::-1], 1), (top, ramp_y, 0), (bottom, ramp_y[::-1], 0)):
        if not extended:
            continue
        # Below 255 everywhere inside: the grey goes only on the new margin (prepare greys at 255).
        weight = np.clip(1.0 - (distance + 1.0) / float(band + 1), 0.0, 1.0) * 255.0
        inner = np.maximum(inner, weight[None, :] if axis == 1 else weight[:, None])
    mask[top:top + height, left:left + width] = inner
    mask_u8 = np.clip(np.rint(mask), 0, 255).astype(np.uint8)
    return {"canvas_png": _png(canvas), "mask_png": _png(Image.fromarray(mask_u8, mode="L")),
            "canvas_size": [cw, ch], "source_size": [width, height], "band_px": band}


def _color_match(patch: Any, reference: Any, keep: Any) -> Any:
    """Per-channel mean/std transfer, statistics measured only where ``keep`` (outside the mask)."""
    np, _ = _imports()
    if keep.sum() < 64:
        return patch
    out = patch.astype(np.float64)
    for c in range(3):
        src = out[..., c][keep]
        ref = reference[..., c][keep].astype(np.float64)
        s_std, r_std = float(src.std()) or 1.0, float(ref.std()) or 1.0
        out[..., c] = (out[..., c] - float(src.mean())) * (r_std / s_std) + float(ref.mean())
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


# Area edits (fill, replace, remove, restyle on an area): the edit models redraw the WHOLE crop with a
# shifted tone. Wave 10 ramped the paste in over 4 % of the crop INSIDE a hard selection; that cross-faded the
# OLD picture back in (live 111 / wave 10: a floating mast top and a dot above the new ship at Replace 100 %)
# and left a tone arc at a soft ellipse in a smooth sky (step 2.38 vs the picture's own 0.86). Charter image,
# owner decision 11: after the colour match the output is joined in the gradient domain: a
# correction that is harmonic inside the selection and equals (picture - matched output) on the one-pixel
# ring just OUTSIDE it (a membrane / Poisson seamless clone with the output's own gradients). The composite
# meets the picture at the edge with no step and keeps the model's detail inside; alpha stays the mask, so
# outside stays byte-identical. No ring (the selection covers the whole crop): no correction.
MEMBRANE_COARSE_MIN = 24     # the pyramid stops when the shorter side is at most this
MEMBRANE_ITERS_COARSE = 200  # Jacobi sweeps on the coarsest level
MEMBRANE_ITERS_FINE = 24     # Jacobi sweeps per finer level (each starts from the upsampled coarse answer)
# Local membrane (owner decision 11, refined after p11-img's real runs): the ring difference is split
# into a TONE part, clipped to the ring median +/- MEMBRANE_TONE_CLIP per channel and joined over the whole
# area, and a LOCAL residual (diff - tone) joined the same way but faded linearly to 0 within
# MEMBRANE_LOCAL_PX of the edge. Without the split one outlier on the ring (a dark quay or the new hull
# meeting the edge) spread harmonically over the area: B Fill mean correction R -24, the white cabin cyan.
# Measured by the owner on the 8 stored outputs (w11-local-membrane-table): cast 24.2 -> 5.8 (B),
# 26.2 -> 5.8 (k3), 11.7 -> 4.7 (A), edge step equal, ghost leak equal or lower.
MEMBRANE_TONE_CLIP = 12.0    # T: tone part = clip(diff, median - T, median + T)
MEMBRANE_LOCAL_PX = 3        # D: residual weight 1 on the first pixel inside, 0 from D px on


def _laplace_fill(values: Any, known: Any, inside: Any) -> Any:
    """Harmonic ``M`` on ``inside`` with ``M = values`` on ``known`` (H x W x C float32), coarse-to-fine
    Jacobi on a 2x pyramid; pixels neither known nor inside are free (zero-flux at the box edge)."""
    np, _ = _imports()
    levels = []
    k, i, d = known, inside, values
    while min(k.shape) > MEMBRANE_COARSE_MIN:
        levels.append((k, i, d))
        h2, w2 = (k.shape[0] + 1) // 2, (k.shape[1] + 1) // 2
        pk = np.zeros((h2 * 2, w2 * 2), bool); pk[:k.shape[0], :k.shape[1]] = k
        pi = np.zeros((h2 * 2, w2 * 2), bool); pi[:i.shape[0], :i.shape[1]] = i
        pd = np.zeros((h2 * 2, w2 * 2, d.shape[2]), np.float32); pd[:d.shape[0], :d.shape[1]] = d
        cnt = pk.reshape(h2, 2, w2, 2).sum((1, 3))
        dd = (pd * pk[..., None]).reshape(h2, 2, w2, 2, d.shape[2]).sum((1, 3)) / np.maximum(cnt, 1)[..., None]
        k = cnt > 0
        i = pi.reshape(h2, 2, w2, 2).any((1, 3)) & ~k
        d = dd.astype(np.float32)
    levels.append((k, i, d))
    m = None
    for li, (k, i, d) in enumerate(reversed(levels)):
        if m is None:
            m = np.where(k[..., None], d, np.float32(0.0)).astype(np.float32)
        else:
            up = np.repeat(np.repeat(m, 2, 0), 2, 1)[:k.shape[0], :k.shape[1]]
            m = np.where(k[..., None], d, up).astype(np.float32)
        free = ~k
        for _ in range(MEMBRANE_ITERS_COARSE if li == 0 else MEMBRANE_ITERS_FINE):
            p = np.pad(m, ((1, 1), (1, 1), (0, 0)), mode="edge")
            avg = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]) * np.float32(0.25)
            m[free] = avg[free]
    return m


def membrane(reference: Any, piece: Any, alpha: Any) -> Any:
    """The gradient-domain correction for ``piece`` (crop-sized, colour-matched) pasted over ``reference``
    through ``alpha`` (crop-sized mask 0..255): float32 H x W x 3, zero where nothing is selected, or None
    when the selection has no ring inside the crop (the whole crop is selected)."""
    np, _ = _imports()
    sel = alpha > 0
    dil = sel.copy()
    dil[1:] |= sel[:-1]; dil[:-1] |= sel[1:]; dil[:, 1:] |= sel[:, :-1]; dil[:, :-1] |= sel[:, 1:]
    ring = dil & ~sel
    if not ring.any() or not sel.any():
        return None
    ys, xs = np.nonzero(dil)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    diff = reference[y0:y1, x0:x1].astype(np.float32) - piece[y0:y1, x0:x1].astype(np.float32)
    rg, s = ring[y0:y1, x0:x1], sel[y0:y1, x0:x1]
    med = np.median(diff[rg], axis=0)
    tone_ring = np.clip(diff, med - np.float32(MEMBRANE_TONE_CLIP), med + np.float32(MEMBRANE_TONE_CLIP))
    resid_ring = diff - tone_ring  # tone + residual == diff on the ring: the edge stays exact
    tone = _laplace_fill(np.where(rg[..., None], tone_ring, np.float32(0.0)).astype(np.float32), rg, s)
    local = _laplace_fill(np.where(rg[..., None], resid_ring, np.float32(0.0)).astype(np.float32), rg, s)
    fill = tone + local * _edge_weight(s, MEMBRANE_LOCAL_PX)[..., None]
    out = np.zeros(piece.shape, dtype=np.float32)
    out[y0:y1, x0:x1] = np.where(s[..., None], fill, np.float32(0.0))
    return out


def _edge_weight(inside: Any, depth: int) -> Any:
    """1 on the first pixel inside ``inside``'s edge, falling linearly to 0 at ``depth`` px (4-neighbour
    erosion rounds; the array border counts as inside, like the free box edge of ``_laplace_fill``)."""
    np, _ = _imports()
    weight = np.zeros(inside.shape, dtype=np.float32)
    cur = inside.copy()
    for k in range(depth):
        p = np.pad(cur, 1, mode="edge")
        eroded = cur & p[:-2, 1:-1] & p[2:, 1:-1] & p[1:-1, :-2] & p[1:-1, 2:]
        weight[cur & ~eroded] = np.float32(1.0 - k / depth)
        cur = eroded
    return weight


def unmasked_hash(rgb: Any, mask: Any) -> str:
    """sha256 over the RGB bytes of every pixel whose mask value is 0 (row-major)."""
    np, _ = _imports()
    keep = mask == 0
    return hashlib.sha256(np.ascontiguousarray(rgb[keep]).tobytes()).hexdigest()


# w24-edit (maintainer acceptance finding): Restyle inside a mask (Sky) is pasted like Photoshop's Sky
# Replacement: through the matte, with the output REGISTERED to the picture and the edge DECONTAMINATED. Measured on the
# owner's run (Qwen 2.1 Restyle 70 %, seed 11): the model redraws the whole crop shifted 9-15 px (its own reframing, a
# little more to the right edge), so the new sky beside the mast carried the model's own mast and sails (a ghost mast,
# "cut" sails); a 3-5 px band next to the kept subject carried its old fringe colours (the halo).
MATTE_BLOCK = 64             # px, the blocks the shift is measured on (only blocks wholly on the kept area)
MATTE_SHIFT_MAX_REL = 0.04   # never more than 4 % of the crop side (a model reframes, it does not move the picture)
DECONTAMINATE_PX = 5         # the band of the matte next to the kept subject filled from the new sky just inside it


def _block_shift(a: Any, b: Any) -> tuple[float, float, float]:
    """(dx, dy, peak) that moves ``b`` onto ``a`` (phase correlation, Hann window)."""
    np, _ = _imports()
    n = a.shape[0]
    win = np.outer(np.hanning(n), np.hanning(n)).astype(np.float64)
    fa = np.fft.fft2((a - a.mean()) * win)
    fb = np.fft.fft2((b - b.mean()) * win)
    cross = fa * np.conj(fb)
    corr = np.fft.ifft2(cross / (np.abs(cross) + 1e-9)).real
    iy, ix = np.unravel_index(int(corr.argmax()), corr.shape)
    dy = iy if iy < n // 2 else iy - n
    dx = ix if ix < n // 2 else ix - n
    return float(dx), float(dy), float(corr.max())


def register_output(reference: Any, piece: Any, keep: Any) -> tuple[Any, list[int] | None]:
    """``piece`` (crop-sized RGB) moved onto ``reference`` by the shift measured on ``keep`` (the area the paste keeps):
    per axis a line (offset + scale) fitted to the block shifts. Returns (piece, [dx, dy] the model's mean move, undone) or (piece, None)
    when there is too little kept detail to measure it (then the output is used as it is)."""
    np, _ = _imports()
    from scipy import ndimage  # type: ignore[import-not-found]
    lum = lambda x: x[..., 0] * 0.299 + x[..., 1] * 0.587 + x[..., 2] * 0.114  # noqa: E731
    ra, pa = lum(reference.astype(np.float64)), lum(piece.astype(np.float64))
    h, w = ra.shape
    n, step = MATTE_BLOCK, MATTE_BLOCK // 2
    lim = MATTE_SHIFT_MAX_REL * max(h, w)
    pts = []
    for y0 in range(0, h - n + 1, step):
        for x0 in range(0, w - n + 1, step):
            if not keep[y0:y0 + n, x0:x0 + n].all() or ra[y0:y0 + n, x0:x0 + n].std() < 4.0:
                continue
            dx, dy, peak = _block_shift(ra[y0:y0 + n, x0:x0 + n], pa[y0:y0 + n, x0:x0 + n])
            if peak >= 0.04 and abs(dx) <= lim and abs(dy) <= lim:
                pts.append((x0 + n / 2.0, y0 + n / 2.0, dx, dy, peak))
    if len(pts) < 3:
        return piece, None
    p = np.asarray(pts, dtype=np.float64)
    wts = p[:, 4]
    def fit(pos: Any, val: Any) -> Any:
        if np.ptp(pos) < n:  # one row/column of blocks: an offset only
            return np.array([float(np.average(val, weights=wts)), 0.0])
        a = np.c_[np.ones(len(pos)), pos] * wts[:, None]
        return np.linalg.lstsq(a, val * wts, rcond=None)[0]
    ax, ay = fit(p[:, 0], p[:, 2]), fit(p[:, 1], p[:, 3])
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    sx = xx - (ax[0] + ax[1] * xx)
    sy = yy - (ay[0] + ay[1] * yy)
    moved = np.stack([ndimage.map_coordinates(piece[..., c].astype(np.float64), [sy, sx], order=1, mode="nearest")
                      for c in range(3)], axis=-1)
    # reported as the model's own move (px right, px down) that was undone
    mean = [-int(round(float(np.mean(ax[0] + ax[1] * xx[0])))), -int(round(float(np.mean(ay[0] + ay[1] * yy[:, 0]))))]
    return np.clip(np.rint(moved), 0, 255).astype(np.uint8), mean


def decontaminate(piece: Any, alpha: Any, depth: int = DECONTAMINATE_PX) -> Any:
    """The matte's band within ``depth`` px of the kept area takes the colour of the nearest output pixel deeper inside
    (Photoshop's Decontaminate Colors): the subject's old fringe never shows through the soft edge as a halo."""
    np, _ = _imports()
    from scipy import ndimage  # type: ignore[import-not-found]
    inside = alpha > 0
    kept = alpha < 128
    if not inside.any() or not kept.any():
        return piece
    dist = ndimage.distance_transform_edt(~kept)
    deep = inside & (dist > depth)
    if not deep.any():
        return piece
    _d, (iy, ix) = ndimage.distance_transform_edt(~deep, return_indices=True)
    band = inside & ~deep
    out = piece.copy()
    out[band] = piece[iy[band], ix[band]]
    return out


# w24-edit2 (owner review of 1cdbd6df, decisions 1+2; Photoshop Sky Replacement / Select Sky + Refine Edge, Lightroom
# Select Sky + Edge / Defringe; image-lanes/w24-edit2-STUDY.md). The pieces of a Sky Replacement composite:
SKY_TOL = 22.0          # RGB distance under which a pixel reads as the old sky's local colour
SKY_GAP_REL = 0.004     # an enclosed sky gap (the mast-jib slot) is at most this share of the picture ...
SKY_GAP_RADIUS = 10.0   # ... or at most this many px from its own edge anywhere (a narrow slot of any length)
SKY_LINE_PX = 2         # thin dark lines through the sky (rigging) up to 2 * this + 1 px wide stay subject
SKY_LINE_DARK = 10.0    # ... when at least this much darker than the local sky


# Lane w29-fix2 (P1 IMG-221; the owner's 2912 x 2912 Sky restyle: the result route took ~100 s, past the desktop's 60 s):
# a wide Gaussian costs O(sigma) per pixel (scipy correlate1d, 8 sigma taps). Above NCONV_DIRECT_SIGMA the blur runs on
# a grid shrunk by sigma / NCONV_DIRECT_SIGMA (area mean) and comes back bilinear, the image pyramid every editor uses for
# wide blurs (Photoshop / GIMP Gaussian Blur, OpenCV pyrDown). The normalised result is a smooth local colour, so the
# shrunk grid loses nothing it carries (measured against the direct filter in tests/backend/test_w29_fix2_edit_result.py).
NCONV_DIRECT_SIGMA = 6.0


def _blur(np: Any, ndimage: Any, plane: Any, sigma: float) -> Any:
    if sigma <= NCONV_DIRECT_SIGMA:
        return ndimage.gaussian_filter(plane, sigma)
    h, w = plane.shape
    f = max(1, int(sigma // NCONV_DIRECT_SIGMA))
    hp, wp = -(-h // f) * f, -(-w // f) * f
    padded = np.pad(plane, ((0, hp - h), (0, wp - w)), mode="edge")
    small = padded.reshape(hp // f, f, wp // f, f).mean(axis=(1, 3))
    small = ndimage.gaussian_filter(small, sigma / f)
    # back to full size: sample centre of pixel i lies at (i + 0.5) / f - 0.5 on the small grid
    ys = np.clip((np.arange(h) + 0.5) / f - 0.5, 0, small.shape[0] - 1)
    xs = np.clip((np.arange(w) + 0.5) / f - 0.5, 0, small.shape[1] - 1)
    y0 = np.floor(ys).astype(int); x0 = np.floor(xs).astype(int)
    y1 = np.minimum(y0 + 1, small.shape[0] - 1); x1 = np.minimum(x0 + 1, small.shape[1] - 1)
    wy = (ys - y0)[:, None]; wx = (xs - x0)[None, :]
    rows = small[y0] * (1 - wy) + small[y1] * wy           # separable: rows first on the narrow grid,
    return rows[:, x0] * (1 - wx) + rows[:, x1] * wx       # then the columns at full size (one pass)


def _nconv(img: Any, weight: Any, sigma: float) -> tuple[Any, Any]:
    np, _ = _imports()
    from scipy import ndimage  # type: ignore[import-not-found]
    den = _blur(np, ndimage, weight, sigma)
    num = np.stack([_blur(np, ndimage, img[..., c] * weight, sigma) for c in range(img.shape[2])], -1)
    return num / np.maximum(den, 1e-6)[..., None], den


def local_colour(img: Any, weight: Any, sigmas: tuple[float, ...] = (4.0, 12.0, 40.0, 120.0)) -> Any:
    """The colour of ``img`` near every pixel over ``weight`` (normalised convolution), wider where there is none."""
    np, _ = _imports()
    out = have = None
    for sigma in sigmas:
        col, den = _nconv(img, weight, sigma)
        if out is None:
            out, have = col, den
            continue
        k = np.clip(have / 0.05, 0.0, 1.0)[..., None]
        out = k * out + (1.0 - k) * col
        have = np.maximum(have, den)
    return out


def _lum(x: Any) -> Any:
    return x[..., 0] * 0.299 + x[..., 1] * 0.587 + x[..., 2] * 0.114


def sky_both_sides(sky: Any) -> Any:
    """Pixels with sky on both sides within a line's width (above and below, or left and right): a rope or a stay runs
    through the sky; the top of the horizon band or of a roof has sky on one side only and is no line."""
    np, _ = _imports()
    r = SKY_LINE_PX + 2
    h, w = sky.shape
    pad = np.pad(sky, r, constant_values=False)
    def any_shift(dy0, dy1, dx0, dx1):
        out = np.zeros((h, w), bool)
        for dy in range(dy0, dy1 + 1):
            for dx in range(dx0, dx1 + 1):
                out |= pad[r + dy:r + dy + h, r + dx:r + dx + w]
        return out
    return (any_shift(-r, -1, 0, 0) & any_shift(1, r, 0, 0)) | (any_shift(0, 0, -r, -1) & any_shift(0, 0, 1, r))


def sky_horizon(sky: Any) -> Any:
    """Per column the sky's lowest row, and the line most of the picture's sky ends on (water reflecting the sky is
    not sky)."""
    np, _ = _imports()
    h = sky.shape[0]
    from scipy import ndimage  # type: ignore[import-not-found]
    lowest = np.where(sky.any(0), h - 1 - np.argmax(sky[::-1], 0), -1)
    line = int(np.percentile(lowest[lowest >= 0], 50)) if (lowest >= 0).any() else -1
    # per column: the sky's own lowest row within a mast's reach either side (a skyline dipping by a sun or a roof
    # gap keeps its depth there; water under a flat horizon stays below it)
    local = ndimage.maximum_filter1d(lowest, 41)
    return lowest, line, local


def refine_sky(rgb: Any, mask: Any) -> tuple[Any, dict[str, int]]:
    """A Sky matte (0..255) refined by colour like Select Sky + Refine Edge: sky seen through small enclosed gaps and in
    the frame's top rows joins it; thin dark lines through it (rigging, a mast) leave it, softly; the rest is kept."""
    np, _ = _imports()
    from scipy import ndimage  # type: ignore[import-not-found]
    src = rgb.astype(np.float64)
    h, w = mask.shape
    sky = mask >= 128
    if not sky.any() or sky.all():
        return mask.copy(), {"added": 0, "lines": 0}
    core = ndimage.binary_erosion(sky, iterations=2)
    old = local_colour(src, (core if core.any() else sky).astype(np.float64))
    dist = np.linalg.norm(src - old, axis=2)
    # the horizon: a gap counts only up to the sky's lowest row in its neighbourhood (a sail wide), and never below the
    # line most of the picture's sky ends on
    lowest, line, local = sky_horizon(sky)
    reach = np.maximum(np.minimum(ndimage.maximum_filter1d(lowest, max(41, w // 2)), line), local)
    above = np.arange(h)[:, None] <= reach[None, :]
    near = ~sky & above & (dist < SKY_TOL)
    labels, count = ndimage.label(near)
    add = np.zeros_like(sky)
    if count:
        idx = np.arange(1, count + 1)
        size = ndimage.sum(np.ones((h, w)), labels, idx)
        top = ndimage.maximum((np.arange(h)[:, None] < 3) & np.ones((h, w), bool), labels, idx)
        mean = ndimage.mean(dist, labels, idx)
        radius = ndimage.maximum(ndimage.distance_transform_edt(near), labels, idx)
        ok = ((size <= SKY_GAP_REL * h * w) | (radius <= SKY_GAP_RADIUS) | (top > 0)) & (mean < SKY_TOL * 0.6)
        add = (labels > 0) & ok[np.maximum(labels - 1, 0)]
    lum, old_lum = _lum(src), _lum(old)
    span = 2 * SKY_LINE_PX + 1
    hat = ndimage.grey_closing(lum, size=(span, span)) - lum
    line = sky & (hat > SKY_LINE_DARK) & (old_lum - lum > SKY_LINE_DARK) & sky_both_sides(sky)
    soft = np.clip(1.0 - hat / np.maximum(old_lum - 35.0, 20.0), 0.0, 1.0)
    alpha = np.where(sky | add, mask.astype(np.float64) / 255.0, 0.0)
    alpha = np.where(add, 1.0, alpha)
    alpha = np.where(line, np.minimum(alpha, soft), alpha)
    return np.clip(np.rint(alpha * 255.0), 0, 255).astype(np.uint8), {"added": int(add.sum()), "lines": int(line.sum())}


def sky_composite(source: Any, piece: Any, alpha: Any, sky: Mapping[str, Any]) -> tuple[Any, Any, dict[str, Any]]:
    """Photoshop Sky Replacement on one crop: ``alpha`` (the refined sky matte, 0..255) moved by Shift Edge (into the
    subject only over pixels that read as the old sky) and faded by Fade Edge; the new sky (``piece``) by Brightness and
    Temperature; the edge band decontaminated (foreground = (I - a * old sky) / (1 - a)); the foreground harmonised to the
    new sky by Lighting (Multiply / Screen toward the luminance ratio) and Colour (the chroma shift). Returns the RGB
    result, the new alpha (0..255: where the result differs from the source outside the old mask is that alpha or the
    foreground change) and the facts."""
    np, _ = _imports()
    from scipy import ndimage  # type: ignore[import-not-found]
    src = source.astype(np.float64)
    new = piece.astype(np.float64)
    a0 = alpha.astype(np.float64) / 255.0
    sky_px = a0 >= 0.5
    old = local_colour(src, ndimage.binary_erosion(sky_px, iterations=2).astype(np.float64)) if sky_px.any() else src
    lum_src, lum_old = _lum(src), _lum(old)
    span = 2 * SKY_LINE_PX + 1
    thin = (ndimage.grey_closing(lum_src, size=(span, span)) - lum_src > SKY_LINE_DARK) & (lum_old - lum_src > SKY_LINE_DARK) & sky_both_sides(sky_px)
    _lowest, _line, local = sky_horizon(sky_px)
    # the unmix never reaches below the sky's own lowest row nearby: water lit like the old sky stays water
    upper = np.arange(sky_px.shape[0])[:, None] <= local[None, :]
    oldness = (np.linalg.norm(src - old, axis=2) < SKY_TOL) & ~thin
    shift = int(sky.get("shift_edge", 0))
    a = a0.copy()
    if shift > 0:
        # pixels that plainly read as the old sky go over to the new sky up to 3 x Shift Edge away (a slot beside a stay)
        # (grown only through such pixels: a dark horizon band or a roof line stops it before the water)
        a = np.where(ndimage.binary_dilation(sky_px, iterations=3 * shift, mask=oldness | sky_px) & oldness, 1.0, a)
        # Shift Edge into the subject by colour unmixing (Refine Edge): within 3 x ``shift`` px of the sky, a pixel's sky share
        # is where it lies between the nearest solid subject colour F and the old sky B; thin dark lines never move.
        now = a >= 0.5
        near = ndimage.binary_dilation(now, iterations=3 * shift) & ~now
        solid = ~ndimage.binary_dilation(now, iterations=3 * shift + 2)
        if near.any() and solid.any():
            _d, (iy, ix) = ndimage.distance_transform_edt(~solid, return_indices=True)
            fcol = src[iy, ix]
            bf = old - fcol
            share = np.clip(((src - fcol) * bf).sum(2) / np.maximum((bf * bf).sum(2), 1.0), 0.0, 1.0)
            share = np.where(np.linalg.norm(bf, axis=2) > 30.0, share, 0.0)  # subject and sky alike: no move
            # below the sky's lowest row only a roof's rim reached from the sky through mixed pixels; a band that is no
            # mix of subject and sky (a dark horizon) stops it before the water
            mixed = (share > 0.1) & ~thin
            rim = upper | (ndimage.binary_dilation(now, iterations=shift, mask=mixed) & ~now)
            a = np.where(near & ~thin & rim, np.maximum(a, share), a)
    elif shift < 0:
        a = ndimage.grey_erosion(a, size=(-2 * shift + 1, -2 * shift + 1))
    fade = float(sky.get("fade_edge", 0))
    if fade > 0:  # feathered toward the sky side of the edge only: with Shift Edge 0 nothing outside the mask moves
        a = np.minimum(a, ndimage.gaussian_filter(a, fade * 0.5))
    bright = float(sky.get("brightness", 0)) / 100.0
    temp = float(sky.get("temperature", 0)) / 100.0
    new = new * (1.0 + 0.5 * bright)
    new = new + np.array([20.0, 0.0, -20.0]) * temp
    new = np.clip(new, 0.0, 255.0)
    # decontaminate: the subject's own colour under a soft edge, without the old sky mixed in
    band = a > 0.02
    fg = src.copy()
    solid = a <= 0.02
    if band.any() and solid.any():  # Decontaminate Colors: the subject's own colours spread out under the soft edge
        _d, (iy, ix) = ndimage.distance_transform_edt(~solid, return_indices=True)
        fg[band] = src[iy[band], ix[band]]
    facts: dict[str, Any] = {"shift_edge": shift, "fade_edge": fade}
    light = float(sky.get("lighting", 0)) / 100.0
    colour = float(sky.get("color", 0)) / 100.0
    if (light > 0 or colour > 0) and sky_px.any():
        weight = np.clip(a, 0.0, 1.0)
        mean_new = (new * weight[..., None]).sum((0, 1)) / max(weight.sum(), 1e-6)
        mean_old = (src * sky_px[..., None]).sum((0, 1)) / max(sky_px.sum(), 1)
        ratio = float(np.clip((_lum(mean_new) + 8.0) / (_lum(mean_old) + 8.0), 0.25, 4.0))
        mode = sky.get("lighting_mode", "multiply")
        gain = 1.0 + light * ((min(ratio, 1.0) if mode == "multiply" else max(ratio, 1.0)) - 1.0) * 0.6
        fg = fg * gain
        chroma_new = mean_new - _lum(mean_new)
        chroma_old = mean_old - _lum(mean_old)
        fg = fg + colour * (chroma_new - chroma_old) * np.clip(_lum(fg) / 128.0, 0.25, 1.0)[..., None]
        fg = np.clip(fg, 0.0, 255.0)
        facts.update(lighting=round(light * 100), lighting_mode=mode, color=round(colour * 100), luminance_ratio=round(ratio, 3))
    out = new * a[..., None] + fg * (1.0 - a[..., None])
    return np.clip(np.rint(out), 0, 255).astype(np.uint8), np.clip(np.rint(a * 255.0), 0, 255).astype(np.uint8), facts


# w25-sky (GEM-077, image-lanes/w25-sky-STUDY.md): the Sky Replacement composite v2 (sky_replace.py) - plate edges and
# grain, unmixed rim and thin structures, open-horizon ramp + haze (Luminar Horizon Blending), water reflection (Luminar
# Reflection Amount). A recipe that records ``haze`` or ``reflect`` uses it.
SKY_V2_KEYS = frozenset({"haze", "reflect"})
_SKY_V2: list[Any] = []


class _SkyHelpers:  # the v1 pieces the v2 composite reuses (this module is loaded unregistered)
    local_colour = staticmethod(lambda *a, **k: local_colour(*a, **k))
    sky_horizon = staticmethod(lambda *a, **k: sky_horizon(*a, **k))


_SKY_HELPERS = _SkyHelpers()


def _sky_v2() -> Any:
    if not _SKY_V2:
        import importlib.util
        spec = importlib.util.spec_from_file_location("film_suite_sky_replace", Path(__file__).with_name("sky_replace.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        _SKY_V2.append(module)
    return _SKY_V2[0]


def stitch(source_png: bytes, mask_png: bytes, output_png: bytes, crop: list[int], *, color_match: bool = False,
           neutral: Any = False, seam: bool = False, latent: Mapping[str, Any] | None = None,
           matte: bool = False, sky: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Paste the model output back ONLY inside the soft mask (Remove: the grown area sent grey).

    Returns ``patch_png`` (RGBA at source size, alpha = mask; RGB outside the
    crop is the source so a viewer without alpha still sees the picture),
    ``composite_png`` (RGB) and the proof: ``unmasked_sha256_before/after``,
    ``changed_outside_mask`` (pixel count, must be 0) and the changed pixel
    bbox inside the mask.
    ``matte`` (Restyle, w24-edit): the output is registered to the picture on the kept area and its edge band
    decontaminated before the paste (``registered`` = the mean [dx, dy] moved, None when not measurable).
    ``seam`` (area edits): the matched output is joined in the gradient domain (``membrane``): equal to
    the picture on the selection's edge, the output's own detail inside (``seam`` = "membrane"; None when
    off or when the selection has no edge inside the crop). Alpha stays the mask.
    """
    np, Image = _imports()
    with Image.open(io.BytesIO(source_png)) as image:
        image.load()
        source = np.asarray(image.convert("RGB"), dtype=np.uint8).copy()
    height, width = source.shape[:2]
    mask = decode_mask(mask_png, (width, height))
    x, y, w, h = crop
    mode = _neutral_mode(neutral)
    grow = neutral_grow_px(crop, mode) if mode else 0
    if latent is not None:  # p13-img: pasted through the same soft mask the sampler denoised through
        grow = 0
        mask = soft_mask(mask, crop, latent["work_size"] if "work_size" in latent else _work_of(crop, output_png), latent,
                         source=source)[1]
    elif grow:  # pasted over the same grown area that was sent grey
        mask = edit_region(mask, grow)
    with Image.open(io.BytesIO(output_png)) as image:
        image.load()
        output = image.convert("RGB")
    model_size = list(output.size)
    if output.size != (w, h):
        output = output.resize((w, h), Image.Resampling.LANCZOS)
    piece = np.asarray(output, dtype=np.uint8).copy()
    region_mask = mask[y:y + h, x:x + w]
    registered = None
    if matte:  # w24-edit: Restyle through its matte (registered, decontaminated; no colour match, no membrane)
        piece, registered = register_output(source[y:y + h, x:x + w], piece, region_mask == 0)
        if sky is None:
            piece = decontaminate(piece, region_mask)
    if sky is not None:  # w24-edit2: Photoshop Sky Replacement on the whole picture, with the recorded settings
        return _sky_stitch(source, mask, piece, crop, sky, model_size, registered)
    if color_match:
        piece = _color_match(piece, source[y:y + h, x:x + w], region_mask == 0)
    joined = None
    if seam:
        correction = membrane(source[y:y + h, x:x + w], piece, region_mask)
        if correction is not None:
            piece = np.clip(np.rint(piece.astype(np.float32) + correction), 0, 255).astype(np.uint8)
            joined = "membrane"
    alpha = region_mask.astype(np.uint32)
    base = source[y:y + h, x:x + w].astype(np.uint32)
    blended = (piece.astype(np.uint32) * alpha[..., None] + base * (255 - alpha[..., None]) + 127) // 255
    composite = source.copy()
    composite[y:y + h, x:x + w] = blended.astype(np.uint8)
    patch = np.zeros((height, width, 4), dtype=np.uint8)
    patch[..., :3] = source
    patch[y:y + h, x:x + w, :3] = piece
    patch[..., 3] = mask
    outside = mask == 0
    changed = np.any(composite != source, axis=2)
    before = unmasked_hash(source, mask)
    after = unmasked_hash(composite, mask)
    ys, xs = np.nonzero(changed)
    return {
        "patch_png": _png(Image.fromarray(patch, mode="RGBA")),
        "composite_png": _png(Image.fromarray(composite, mode="RGB")),
        "model_output_size": model_size,
        "unmasked_sha256_before": before,
        "unmasked_sha256_after": after,
        "changed_outside_mask": int((changed & outside).sum()),
        "changed_pixels": int(changed.sum()),
        "changed_bbox": None if xs.size == 0 else [int(xs.min()), int(ys.min()), int(xs.max()) - int(xs.min()) + 1, int(ys.max()) - int(ys.min()) + 1],
        "color_matched": bool(color_match),
        "grow_px": grow,
        "seam": joined,
        "latent_mask": latent is not None,
        **({"matte": True, "registered": registered} if matte else {}),
    }


def _sky_stitch(source: Any, mask: Any, piece: Any, crop: list[int], sky: Mapping[str, Any], model_size: list[int],
                registered: Any) -> dict[str, Any]:
    """The Sky Replacement composite of one Restyle (``stitch`` with ``sky``): the matte refined by colour, the new sky
    through it, the foreground harmonised. The patch is the whole picture (alpha 255 where anything changed), so the
    layer shows exactly this composite; ``outside_by`` names the settings that changed pixels outside the mask."""
    np, Image = _imports()
    height, width = source.shape[:2]
    x, y, w, h = crop
    full = source.copy()
    full[y:y + h, x:x + w] = piece
    # the Sky mask job already refines a new Sky mask; with Shift Edge on, a mask made before is refined here too
    refined, refine_facts = refine_sky(source, mask) if int(sky.get("shift_edge", 0)) > 0 else (mask, {"added": 0, "lines": 0})
    if SKY_V2_KEYS & set(sky):  # w25-sky: settings recorded with haze/reflect = the v2 composite; older recipes rebuild as they were
        comp, alpha, facts = _sky_v2().composite(_SKY_HELPERS, source, full, refined, sky, crop)
    else:
        comp, alpha, facts = sky_composite(source, full, refined, sky)
    changed = np.any(comp != source, axis=2)
    outside = mask == 0
    patch = np.zeros((height, width, 4), dtype=np.uint8)
    patch[..., :3] = comp
    patch[..., 3] = np.where(changed, 255, 0).astype(np.uint8)
    by = [k for k in ("shift_edge", "lighting", "color", "reflect", "haze") if sky.get(k)] if (changed & outside).any() else []
    ys, xs = np.nonzero(changed)
    return {
        "patch_png": _png(Image.fromarray(patch, mode="RGBA")),
        "composite_png": _png(Image.fromarray(comp, mode="RGB")),
        "model_output_size": model_size,
        "unmasked_sha256_before": unmasked_hash(source, mask),
        "unmasked_sha256_after": unmasked_hash(comp, mask),
        "changed_outside_mask": int((changed & outside).sum()),
        "changed_pixels": int(changed.sum()),
        "changed_bbox": None if xs.size == 0 else [int(xs.min()), int(ys.min()), int(xs.max()) - int(xs.min()) + 1, int(ys.max()) - int(ys.min()) + 1],
        "color_matched": False, "grow_px": 0, "seam": None, "latent_mask": False,
        "matte": True, "registered": registered,
        "sky": {**{k: sky[k] for k in sorted(sky)}, **facts, **refine_facts, "outside_by": by},
    }


def _work_of(crop: list[int], output_png: bytes) -> tuple[int, int]:
    """The work size an edit sampled at = the model output's size (the graphs decode the crop's latent)."""
    _np, Image = _imports()
    with Image.open(io.BytesIO(output_png)) as image:
        return image.size


def _multipart(fields: Mapping[str, str], filename: str, data: bytes) -> tuple[bytes, str]:
    boundary = "----filmsuite" + uuid.uuid4().hex
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in fields.items()]
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{filename}\"\r\n"
                 "Content-Type: image/png\r\n\r\n".encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def upload_crop(opener: Any, origin: str, name: str, data: bytes) -> str:
    """Upload the crop to ComfyUI temp under its content name; returns the LoadImage value."""
    if len(data) > UPLOAD_MAX_BYTES:
        raise EditServiceError("picture_too_large", "The crop is too large to upload")
    filename = name[: -len(" [temp]")] if name.endswith(" [temp]") else name
    body, boundary = _multipart({"type": "temp", "overwrite": "true"}, filename, data)
    request = urllib.request.Request(origin + "/upload/image", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with opener(request, timeout=30) as response:
        raw = response.read(1 << 20)
    answer = json.loads(raw.decode("utf-8"))
    stored = answer.get("name") if isinstance(answer, dict) else None
    if stored != filename or (answer.get("subfolder") or "") != "":
        raise EditServiceError("edit_upload_failed", "The image engine did not take the crop")
    return f"{stored} [temp]"


class EditStore:
    """Bounded in-memory edit records, mirrored to small JSON files (write-once masks)."""

    def __init__(self, root: Path | str | None) -> None:
        self.root = Path(root) if root is not None else None
        self._records: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._masks_mem: "OrderedDict[str, bytes]" = OrderedDict()

    def _dir(self) -> Path | None:
        if self.root is None:
            return None
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "masks").mkdir(exist_ok=True)
        return self.root

    def _write_atomic(self, path: Path, data: bytes) -> None:
        fd, temp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            _win_io.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def save_mask(self, mask_png: bytes) -> str:
        digest = hashlib.sha256(mask_png).hexdigest()
        folder = self._dir()
        if folder is not None:
            path = folder / "masks" / f"{digest}.png"
            if not path.exists():
                self._write_atomic(path, mask_png)
        self._masks_mem[digest] = mask_png
        while len(self._masks_mem) > RECORDS_MAX:
            self._masks_mem.popitem(last=False)
        return digest

    def mask(self, digest: str) -> bytes | None:
        if not _HEX64.match(digest or ""):
            return None
        if digest in self._masks_mem:
            return self._masks_mem[digest]
        folder = self._dir()
        if folder is None:
            return None
        path = folder / "masks" / f"{digest}.png"
        try:
            data = _win_io.read_bytes(path)
        except OSError:
            return None
        return data if hashlib.sha256(data).hexdigest() == digest else None

    def put(self, record: dict[str, Any]) -> None:
        edit_id = record["edit_id"]
        self._records[edit_id] = record
        self._records.move_to_end(edit_id)
        while len(self._records) > RECORDS_MAX:
            self._records.popitem(last=False)
        folder = self._dir()
        if folder is not None:
            self._write_atomic(folder / f"{edit_id}.json", json.dumps(record, sort_keys=True).encode("utf-8"))

    # Lane w29-fix2 (IMG-221): the built result answer of one variant (patch PNG inside), kept so a heavy stitch is done
    # once. Key = "<image_producer_job_id>|<composite 0/1>"; files results/<sha256 of key>.json.
    def _result_path(self, key: str) -> Path | None:
        folder = self._dir()
        if folder is None or not isinstance(key, str) or not key:
            return None
        (folder / "results").mkdir(exist_ok=True)
        return folder / "results" / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"

    def put_result(self, key: str, body: dict[str, Any]) -> None:
        path = self._result_path(key)
        if path is not None:
            self._write_atomic(path, json.dumps({"key": key, "body": body}, sort_keys=True).encode("utf-8"))

    def result(self, key: str) -> dict[str, Any] | None:
        path = self._result_path(key)
        if path is None:
            return None
        try:
            kept = json.loads(_win_io.read_bytes(path).decode("utf-8"))
        except (OSError, ValueError):
            return None
        return kept["body"] if isinstance(kept, dict) and kept.get("key") == key and isinstance(kept.get("body"), dict) else None

    def get(self, edit_id: str) -> dict[str, Any] | None:
        if not _EDIT_ID.match(edit_id or ""):
            return None
        record = self._records.get(edit_id)
        if record is not None:
            return record
        folder = self._dir()
        if folder is None:
            return None
        try:
            record = json.loads(_win_io.read_bytes(folder / f"{edit_id}.json").decode("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("edit_id") != edit_id:
            return None
        self._records[edit_id] = record
        return record

    def by_job(self, job_id: str) -> tuple[dict[str, Any], int] | None:
        for record in reversed(self._records.values()):
            for index, member in enumerate(record.get("variants") or []):
                if member.get("image_producer_job_id") == job_id:
                    return record, index
        return None


def now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["EditServiceError", "EditStore", "refine_sky", "sky_composite", "decode_mask", "mask_bbox", "now_ms", "prepare", "soft_mask", "stitch", "unmasked_hash", "upload_crop"]
