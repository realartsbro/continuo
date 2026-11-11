"""w25-sky (GEM-077): Sky Replacement composite v2 - the compositor's verdict on Install 130 (image-lanes/w25-sky-STUDY.md).

Reference mechanisms, one per step:
- plate: Luminar Sky AI "Defocus" (match the depth of field of the photo) and the compositing rule "match grain": the
  new sky's own border artefacts (a model's VAE edge row, columns the registration pulled in from outside the crop)
  are mirrored from valid plate pixels, its fine detail is softened to the photo's, and the photo's own grain is laid
  back over it.
- matte: Photoshop Select and Mask "Decontaminate Colors" + Sky Replacement "Shift Edge" (unmix the old sky out of the
  subject's rim: F = (I - a B) / (1 - a)); thin structures of either polarity inside the sky (mast, stays, spreaders)
  are kept by unmixing against the local old sky (Photoshop 2021+: "keeps contrast around thin objects").
- horizon: Luminar "Horizon Blending" (a soft transition) and Resolve's Darken-merge gradations: the matte follows a
  clean ramp along an open horizon (no ragged specks of the old shoreline), and a haze band joins sky and water.
- foreground: Photoshop "Lighting Adjustment" + "Color Adjustment" (kept from v1) and Luminar "Reflection Amount" +
  "Water Blur": open water below the horizon takes the new sky, mirrored and blurred, its own ripples kept.
All deterministic (no random numbers): a recorded edit rebuilds byte for byte.
"""
from __future__ import annotations

from typing import Any, Mapping

PLATE_EDGE_PX = 2          # a model's border rows/columns that are always rebuilt (VAE edge)
PLATE_DEAD_REL = 0.2       # a border column/row whose detail is under this share of the plate's is extrapolated
PLATE_DEAD_MAX = 48        # px, at most this many columns/rows per side are rebuilt
GRAIN_SIGMA = 1.5          # the photo's grain = its sky minus a Gaussian of this sigma (clipped to 3 robust std)
SOFT_TARGET = 0.6          # the plate's own fine detail is softened to at most this share of the photo's
SOFT_SIGMAS = (0.0, 0.6, 0.9, 1.2, 1.6, 2.0, 2.5, 3.0)
LINE_TOL = 18.0            # RGB distance from the local old sky under which a pixel inside the sky reads as sky
SLOT_REACH_PX = 40         # px, how deep an enclosed slot of old sky may reach into the subject
SLOT_TOL = 16.0            # RGB distance from the subject-sky mixing line under which a slot pixel is a mix
LINE_MIN_PX = 4            # a structure inside the sky smaller than this is grain, not a stay
HORIZON_OPEN_PX = 6        # a column whose sky ends within this of the horizon line meets open water
HORIZON_RAMP_PX = 4        # the matte's ramp across an open horizon (rows either side)
WATER_COLOUR_TOL = 18.0    # RGB distance from the local water colour where the water matte starts to fall off (to 0 at 2x)
WATER_REACH_PX = 6         # px around the detected water the colour matte may reach (edges, glints, gaps)
HAZE_BELOW_PX = 5.0        # the haze reaches this many rows below the horizon line (Horizon Blending fades across it)
WATER_GRAD = 9.0           # water: gradient magnitude under this (smooth) ...
WATER_TEXTURE = 3.2        # ... and local detail under this; flooded from the horizon


def _lum(np: Any, x: Any) -> Any:
    return x[..., 0] * 0.299 + x[..., 1] * 0.587 + x[..., 2] * 0.114


def clean_plate(np: Any, ndimage: Any, plate: Any, crop: list[int], size: tuple[int, int], sky: Any) -> tuple[Any, dict[str, int]]:
    """The new sky's border artefacts rebuilt by mirroring valid plate pixels (never by stretching an edge pixel).
    Detail is measured on the sky rows/columns only (``sky`` = the crop's sky matte)."""
    x, y, w, h = crop
    H, W = size
    out = plate.copy()
    L = _lum(np, plate)
    rows = sky.mean(1) > 0.5
    cols = sky.mean(0) > 0.5
    dx = np.abs(np.diff(L[rows] if rows.any() else L, axis=1)).mean(0)
    dy = np.abs(np.diff(L[:, cols] if cols.any() else L, axis=0)).mean(1)
    ref_x = float(np.median(dx)) if dx.size else 0.0
    ref_y = float(np.median(dy)) if dy.size else 0.0
    facts = {}

    def dead(prof: Any, ref: float) -> int:
        n = 0
        while n < min(PLATE_DEAD_MAX, prof.size // 4) and prof[n] < PLATE_DEAD_REL * max(ref, 1e-6):
            n += 1
        return n

    sides = {"left": dead(dx, ref_x), "right": dead(dx[::-1], ref_x), "top": dead(dy, ref_y), "bottom": dead(dy[::-1], ref_y)}
    for side, n in sides.items():
        n = n + PLATE_EDGE_PX
        facts[side] = n
        if side == "left":
            src = out[:, n:2 * n + 1][:, ::-1][:, :n]
            out[:, :n] = src
        elif side == "right":
            src = out[:, w - 2 * n - 1:w - n][:, ::-1][:, -n:]
            out[:, w - n:] = src
        elif side == "top":
            out[:n] = out[n:2 * n + 1][::-1][:n]
        else:
            out[h - n:] = out[h - 2 * n - 1:h - n][::-1][-n:]
    return out, facts


def match_grain(np: Any, ndimage: Any, src: Any, new: Any, sky: Any) -> tuple[Any, dict[str, float]]:
    """The plate softened to the photo's sharpness, then the photo's own grain laid over it (inside the sky only)."""
    if sky.sum() < 64:
        return new, {"soften": 0.0}
    hf = lambda img, s: img - ndimage.gaussian_filter(img, s)  # noqa: E731
    Ls, Ln = _lum(np, src), _lum(np, new)
    s_src = float(hf(Ls, 2.0)[sky].std())
    pick = 0.0
    for sigma in SOFT_SIGMAS:
        soft = ndimage.gaussian_filter(Ln, sigma) if sigma else Ln
        pick = sigma
        if float(hf(soft, 2.0)[sky].std()) <= SOFT_TARGET * max(s_src, 0.5):
            break
    out = new.copy()
    if pick:  # sampled over the sky only (normalised convolution): the model's own subject never smears into the sky
        wt = sky.astype(np.float64)
        den = ndimage.gaussian_filter(wt, pick)
        for c in range(3):
            soft_c = ndimage.gaussian_filter(new[..., c] * wt, pick) / np.maximum(den, 1e-6)
            out[..., c] = np.where(den > 0.05, soft_c, new[..., c])
    grain = hf(Ls, GRAIN_SIGMA)
    spread = 1.4826 * float(np.median(np.abs(grain[sky]))) if sky.any() else 0.0
    grain = np.clip(grain, -3.0 * spread, 3.0 * spread)
    out = out + grain[..., None]
    return out, {"soften": pick, "grain": round(spread, 3)}


def subject_colour(np: Any, src: Any, dist: Any, allowed: Any, radius: int = 3) -> Any:
    """Per pixel the colour of the neighbour within ``radius`` px (not sky) that differs most from the old sky: the
    subject's own colour for unmixing an edge pixel (Refine Edge's foreground estimate). A thin sail's luff takes its
    sail colour, not the mast highlight on the far side."""
    H, W = dist.shape
    best = np.where(allowed, dist, -1.0)
    pad_d = np.pad(best, radius, constant_values=-1.0)
    pad_c = np.pad(src, ((radius, radius), (radius, radius), (0, 0)), mode="edge")
    # Which offset won per pixel (0 = none: the pixel's own colour); the colours are read once at the end. Copying
    # the whole picture for each of the 29 / 49 offsets cost 5.5 s of a 2912^2 stitch (same result, bit for bit:
    # tests/backend/test_w39_sky_subject_colour.py).
    offsets = [(0, 0)]
    won = np.zeros((H, W), np.intp)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dy * dy + dx * dx > radius * radius:
                continue
            d = pad_d[radius + dy:radius + dy + H, radius + dx:radius + dx + W]
            better = d > best
            np.copyto(best, d, where=better)
            offsets.append((dy, dx))
            won[better] = len(offsets) - 1
    off = np.asarray(offsets, np.intp)
    rows = np.arange(radius, radius + H, dtype=np.intp)[:, None] + off[:, 0][won]
    cols = np.arange(radius, radius + W, dtype=np.intp)[None, :] + off[:, 1][won]
    return pad_c[rows, cols], best


def composite(svc: Any, source: Any, full: Any, alpha: Any, sky: Mapping[str, Any], crop: list[int]) -> tuple[Any, Any, dict[str, Any]]:
    """Sky Replacement v2 on the whole picture. ``full`` = the source with the model's crop pasted in; ``alpha`` = the
    refined sky matte (0..255). Returns (RGB uint8, alpha uint8, facts)."""
    import numpy as np  # type: ignore[import-not-found]
    from scipy import ndimage  # type: ignore[import-not-found]
    H, W = alpha.shape
    src = source.astype(np.float64)
    a0 = alpha.astype(np.float64) / 255.0
    sky_px = a0 >= 0.5
    facts: dict[str, Any] = {"version": 2, "shift_edge": int(sky.get("shift_edge", 0)), "fade_edge": float(sky.get("fade_edge", 0))}
    if not sky_px.any():
        return source.copy(), alpha.copy(), facts
    # ---- the plate (item 2) ----
    x, y, w, h = crop
    plate, edges = clean_plate(np, ndimage, full[y:y + h, x:x + w].astype(np.float64), crop, (H, W), sky_px[y:y + h, x:x + w])
    new = src.copy()
    new[y:y + h, x:x + w] = plate
    facts["plate_rebuilt"] = edges
    # ---- the old sky's local colour, robust to what stands in it ----
    core = ndimage.binary_erosion(sky_px, iterations=2)
    old = svc.local_colour(src, (core if core.any() else sky_px).astype(np.float64))
    dist = np.linalg.norm(src - old, axis=2)
    clean = (core if core.any() else sky_px) & (dist < LINE_TOL)
    if clean.sum() > 64:
        old = svc.local_colour(src, clean.astype(np.float64))
        dist = np.linalg.norm(src - old, axis=2)
    new, grain = match_grain(np, ndimage, src, new, ndimage.binary_erosion(sky_px, iterations=3))
    facts.update(grain)
    bright = float(sky.get("brightness", 0)) / 100.0
    temp = float(sky.get("temperature", 0)) / 100.0
    new = np.clip(new * (1.0 + 0.5 * bright) + np.array([20.0, 0.0, -20.0]) * temp, 0.0, 255.0)
    # ---- the matte (item 1) ----
    a = a0.copy()
    fg = src.copy()
    lowest, line, _local = svc.sky_horizon(sky_px)
    above = np.arange(H)[:, None] <= (line + 1)
    # (a) thin structures inside the sky (either polarity): unmixed against the local old sky
    struct = sky_px & above & (dist > LINE_TOL)
    lab, n = ndimage.label(struct, structure=np.ones((3, 3)))
    if n:
        size = ndimage.sum(np.ones((H, W)), lab, np.arange(1, n + 1))
        struct = (lab > 0) & (size >= LINE_MIN_PX)[np.maximum(lab - 1, 0)]
    peak = ndimage.maximum_filter(np.where(struct, dist, 0.0), size=5)
    share_line = np.clip(1.0 - dist / np.maximum(peak, 40.0), 0.0, 1.0)
    grow = ndimage.binary_dilation(struct, iterations=1) & sky_px & above
    a = np.where(grow, np.minimum(a, share_line), a)
    facts["lines"] = int(struct.sum())
    shift = int(sky.get("shift_edge", 0))
    # (b0) the frame's top rows above sky columns (a scan/VAE border the AI mask leaves out) join the sky
    top_k = 4 if shift > 0 else 0  # like every move of the matte INTO the subject: Shift Edge
    col_sky = sky_px[top_k:top_k + 3].all(0)
    a[:top_k] = np.where(col_sky[None, :], 1.0, a[:top_k])
    facts["top_rows"] = int(col_sky.sum())
    # (b) the subject's rim: Shift Edge px into the subject, unmixed (F = nearest solid subject colour, B = old sky)
    if shift > 0:
        reach = 2 * shift
        now = a >= 0.5
        near = ndimage.binary_dilation(now, iterations=reach) & ~now & (np.arange(H)[:, None] <= line)
        solid = ~ndimage.binary_dilation(now, iterations=reach + 2)
        if near.any() and solid.any():
            fcol, _best = subject_colour(np, src, dist, ~now)
            bf = old - fcol
            share = np.clip(((src - fcol) * bf).sum(2) / np.maximum((bf * bf).sum(2), 1.0), 0.0, 1.0)
            share = np.where(np.linalg.norm(bf, axis=2) > 30.0, share, 0.0)
            # contiguous with the sky through mixed pixels only (a subject's own light patch deeper in stays)
            mixed = near & (share > 0.08)
            mixed = ndimage.binary_propagation(ndimage.binary_dilation(now, iterations=1) & mixed, mask=mixed)
            a = np.where(mixed, np.maximum(a, share), a)
            facts["rim_px"] = int(mixed.sum())
        # (b2) a narrow slot of old sky enclosed by the subject (mast - forestay - jib head), reached from the sky through
        # pixels that are mostly old sky in colour (Select Sky's "sky seen through gaps"); unmixed against the subject
        deep = SLOT_REACH_PX
        solid2 = ~ndimage.binary_dilation(now, iterations=deep + 2)
        if solid2.any():
            fcol, _best = subject_colour(np, src, dist, ~now, 4)
            bf = old - fcol
            share2 = np.clip(((src - fcol) * bf).sum(2) / np.maximum((bf * bf).sum(2), 1.0), 0.0, 1.0)
            # a mix of subject and old sky lies on the segment F..B (small residual); a light subject colour (a beard,
            # a white hull) does not, however close its distance to the sky
            resid = np.linalg.norm(src - (fcol + share2[..., None] * bf), axis=2)
            skyish = ~now & (share2 > 0.45) & (resid < SLOT_TOL) & (np.arange(H)[:, None] < line - 2) & ~struct
            skyish &= ndimage.binary_dilation(now, iterations=deep)
            slot = ndimage.binary_propagation(ndimage.binary_dilation(now, iterations=1) & skyish, mask=skyish)
            a = np.where(slot, np.maximum(a, share2), a)
            facts["slot_px"] = int(slot.sum())
    elif shift < 0:
        a = ndimage.grey_erosion(a, size=(-2 * shift + 1, -2 * shift + 1))
    # (c) the open horizon: a clean ramp (no specks of the old shoreline)
    smooth_low = ndimage.median_filter(lowest.astype(np.float64), size=31, mode="nearest")
    open_col = (lowest >= 0) & (np.abs(lowest - line) <= HORIZON_OPEN_PX) & (np.abs(smooth_low - line) <= HORIZON_OPEN_PX)
    open_col = ndimage.binary_opening(open_col, iterations=3)
    rows = np.arange(H)[:, None].astype(np.float64)
    ramp = np.clip((smooth_low[None, :] + 0.5 - rows) / (2.0 * HORIZON_RAMP_PX) * 2.0 + 0.5, 0.0, 1.0)
    band = open_col[None, :] & (np.abs(rows - smooth_low[None, :]) <= HORIZON_RAMP_PX + 1) & ~(grow | struct)
    # the ramp reaches below the mask's own line only with Haze (Luminar Horizon Blending); without it the water never
    # takes the new sky and zero settings keep the outside byte-identical
    a = np.where(band, ramp if float(sky.get("haze", 0)) > 0 else np.minimum(a, ramp), a)
    facts["horizon_open_cols"] = int(open_col.sum())
    # small islands of either kind left along the horizon band go with their surroundings
    hb = np.zeros((H, W), bool)
    hb[max(line - 12, 0):min(line + 12, H)] = True
    hb &= ~(grow | struct)
    for is_sky in (True, False):  # sky specks in the water go; old-sky specks in the new sky join it
        lab, n = ndimage.label(hb & ((a >= 0.5) == is_sky))
        if n:
            size = ndimage.sum(np.ones((H, W)), lab, np.arange(1, n + 1))
            small = (lab > 0) & (size <= 40)[np.maximum(lab - 1, 0)]
            if is_sky:
                a = np.where(small & (rows > smooth_low[None, :]), 0.0, a)
            elif shift > 0:
                a = np.where(small & (dist < LINE_TOL * 1.5), 1.0, a)
    fade = float(sky.get("fade_edge", 0))
    if fade > 0:
        a = np.where(grow | struct, a, np.minimum(a, ndimage.gaussian_filter(a, fade * 0.5)))
    # pixels the matte gave over to the sky beyond the model's own sky (rim, slot, top rows) take the plate's local sky
    # colour (plus the photo's grain): the model may have drawn its own mast or sail there
    added = (a > 0.02) & ~ndimage.binary_erosion(sky_px, iterations=1) & ~(grow | struct)
    if added.any():
        inner_sky = ndimage.binary_erosion(sky_px, iterations=3)
        fill = svc.local_colour(new, (inner_sky if inner_sky.any() else sky_px).astype(np.float64), (2.0, 5.0, 12.0, 40.0))
        spread = float(grain.get("grain", 0.0))
        fine = new - np.stack([ndimage.gaussian_filter(new[..., c], GRAIN_SIGMA) for c in range(3)], -1)
        _d, (iy, ix) = ndimage.distance_transform_edt(~inner_sky if inner_sky.any() else ~sky_px, return_indices=True)
        g = np.clip(fine[iy, ix], -3.0 * spread, 3.0 * spread)  # the plate's own grain from the nearest clean sky
        new = np.where(added[..., None], fill + g, new)
        facts["added_px"] = int(added.sum())
    a = np.clip(a, 0.0, 1.0)
    # decontaminate (Select and Mask "Decontaminate Colors"): under a soft alpha the colour of the nearest solid subject
    # pixel, so no old sky shows through the edge; the unmixed colour where it is a valid colour close to that
    soft = a > 0.02
    solid = a <= 0.02
    if soft.any() and solid.any():
        _d, (iy, ix) = ndimage.distance_transform_edt(~solid, return_indices=True)
        near_col = src[iy, ix]
        unmix = (src - a[..., None] * old) / np.maximum(1.0 - a, 1e-3)[..., None]
        valid = (a < 0.7) & np.all((unmix >= 0.0) & (unmix <= 255.0), axis=2) & (np.linalg.norm(unmix - near_col, axis=2) < 60.0)
        fg = np.where(soft[..., None], np.where(valid[..., None], unmix, near_col), fg)
    # ---- foreground harmonise (item 3): lighting + colour (Photoshop), reflection (Luminar) ----
    light = float(sky.get("lighting", 0)) / 100.0
    colour = float(sky.get("color", 0)) / 100.0
    reflect = float(sky.get("reflect", 0)) / 100.0
    hz_rows = slice(max(line - max(H // 12, 8), 0), max(line - 1, 1))
    hz_sky = sky_px.copy(); hz_sky[: hz_rows.start] = False; hz_sky[hz_rows.stop:] = False
    mean_new = new[hz_sky].mean(0) if hz_sky.any() else new[sky_px].mean(0)
    mean_old = src[hz_sky].mean(0) if hz_sky.any() else src[sky_px].mean(0)
    if light > 0 or colour > 0:
        ratio = float(np.clip((_lum(np, mean_new[None])[0] + 8.0) / (_lum(np, mean_old[None])[0] + 8.0), 0.25, 4.0))
        mode = sky.get("lighting_mode", "multiply")
        # v2: the full luminance ratio at 100 (v1 capped it at 0.6 x; on Install 130 the foreground stayed warm-bright)
        gain = 1.0 + light * ((min(ratio, 1.0) if mode == "multiply" else max(ratio, 1.0)) - 1.0)
        fg = fg * gain
        chroma_new = mean_new - _lum(np, mean_new[None])[0]
        chroma_old = mean_old - _lum(np, mean_old[None])[0]
        fg = fg + colour * (chroma_new - chroma_old) * np.clip(_lum(np, fg) / 128.0, 0.25, 1.0)[..., None]
        facts.update(lighting=round(light * 100), lighting_mode=mode, color=round(colour * 100), luminance_ratio=round(ratio, 3))
    water = np.zeros((H, W))
    if reflect > 0 and 0 < line < H - 4:
        Ls = _lum(np, src)
        grad = ndimage.gaussian_gradient_magnitude(Ls, 1.0)
        tex = ndimage.uniform_filter(np.abs(Ls - ndimage.gaussian_filter(Ls, 1.5)), 5)
        below = rows > line + 1
        ok = below & (grad < WATER_GRAD) & (tex < WATER_TEXTURE)
        seed = np.zeros((H, W), bool)
        seed[min(line + 2, H - 1):min(line + 6, H)] = True
        seed &= ok & open_col[None, :]
        region = ndimage.binary_propagation(seed, mask=ok)
        region = ndimage.binary_opening(region, iterations=2)
        # the rows between the horizon line and the water (a thin shoreline, the edge's own gradient) belong to it where
        # the gap is a few rows: the reflection reaches the line (no new seam under it; the haze band softens it)
        top = np.where(region.any(0), np.argmax(region, 0), H)
        gap = (top - line > 1) & (top - line <= 5) & open_col
        region |= gap[None, :] & below & (rows < top[None, :])
        # a soft water matte by colour (Luminar detects the water and blends its reflection through a matte): every pixel
        # below the horizon weighs by how close it is to the local water colour, so an object's edge pixel mixing water
        # takes part of the reflection (no old-water halo) and glints or ripples the gradient test left out join it
        if region.any():
            wcol = svc.local_colour(src, region.astype(np.float64), (6.0, 18.0, 60.0))
            wd = np.linalg.norm(src - wcol, axis=2)
            soft_w = np.clip(1.0 - (wd - WATER_COLOUR_TOL) / WATER_COLOUR_TOL, 0.0, 1.0)
            # pockets of the same water enclosed by objects (between a post and its rope) are not connected to the open
            # water; anywhere below the horizon a pixel whose colour is the water's (its colour range, like Select Color
            # Range on the detected water) joins, within reach of the water's rows
            near_rows = region & (rows <= line + max(H // 10, 12)) & open_col[None, :]  # the open water by the horizon
            wr = src[near_rows] if near_rows.sum() > 64 else src[region]
            lo_c, hi_c = np.percentile(wr, 5, axis=0) - 8.0, np.percentile(wr, 95, axis=0) + 8.0
            in_range = np.all((src >= lo_c) & (src <= hi_c), axis=2) & below
            pockets = in_range & ~region
            lab, n = ndimage.label(pockets)
            if n:
                size = ndimage.sum(np.ones((H, W)), lab, np.arange(1, n + 1))
                pockets = (lab > 0) & (size >= 6)[np.maximum(lab - 1, 0)]
            region = region | pockets
            reach = ndimage.binary_dilation(region, iterations=WATER_REACH_PX) & below
            region_soft = np.where(region, 1.0, np.where(reach, soft_w, 0.0))
            # an edge pixel unmixed: the share of water in it along object colour -> water colour
            _d, (iy, ix) = ndimage.distance_transform_edt(reach & (soft_w <= 0.0) | ~reach, return_indices=True)
            ocol = src[iy, ix]
            ow = wcol - ocol
            mix = np.clip(((src - ocol) * ow).sum(2) / np.maximum((ow * ow).sum(2), 1.0), 0.0, 1.0)
            ring = reach & ~region & (soft_w < 1.0)
            region_soft = np.where(ring, np.maximum(region_soft, mix * (np.linalg.norm(ow, axis=2) > 30.0)), region_soft)
            water = region_soft
            region = region_soft > 0.02
        else:
            water = region.astype(np.float64)
        if region.any():
            k = np.clip(rows - line, 0.0, None)
            span = float(max(np.nonzero(region.any(1))[0].max() - line, 1))
            ys = np.clip(line - 1 - k * (line / span), 0, line - 1).astype(np.intp)
            mirror = new[ys[:, 0]]
            mirror = np.stack([ndimage.gaussian_filter(mirror[..., c], (3.0, 8.0)) for c in range(3)], -1)
            detail = _lum(np, fg) - ndimage.gaussian_filter(_lum(np, fg), 6.0)
            # the water keeps its own brightness relation to the sky (darker than the sky it mirrors) and its ripples
            dim = float(np.clip((_lum(np, src[region].mean(0)[None])[0] + 8.0) / (_lum(np, mean_old[None])[0] + 8.0), 0.4, 1.0))
            target = mirror * dim + detail[..., None] * 0.9
            fresnel = (1.0 - 0.4 * np.clip(k / span, 0.0, 1.0))  # grazing at the horizon: the water is the sky's mirror
            wgt = (reflect * fresnel * water)[..., None]
            fg = fg * (1.0 - wgt) + target * wgt
        facts.update(reflect=round(reflect * 100), water_px=int(region.sum()))
    fg = np.clip(fg, 0.0, 255.0)
    out = new * a[..., None] + fg * (1.0 - a[..., None])
    # ---- the haze band (item 4): sky and water joined like Luminar's Horizon Blending ----
    haze = float(sky.get("haze", 0)) / 100.0
    if haze > 0 and 0 < line < H - 4:
        hb_px = max(2.0, haze * 0.06 * H)
        lowrow = np.where(open_col, smooth_low, float(line))
        dyy = lowrow[None, :] - rows
        under = np.clip(lowrow + max(HAZE_BELOW_PX, 0.5 * hb_px) + 3, 0, H - 1).astype(np.intp)  # the water below the band, not its shoreline
        water_col = out[under, np.arange(W)]
        sky_col = out[np.clip(lowrow - hb_px, 0, H - 1).astype(np.intp), np.arange(W)]
        hazecol = 0.6 * water_col + 0.4 * sky_col
        hazecol = np.stack([ndimage.gaussian_filter1d(hazecol[:, c], 30.0) for c in range(3)], -1)
        strength = min(1.0, 0.35 + 0.65 * haze)
        above_w = np.clip(1.0 - dyy / hb_px, 0.0, 1.0) ** 1.6 * (dyy >= -0.5) * a  # the sky side, only where the sky is
        # across the line: the distant shoreline under the horizon hazes toward the sky (atmospheric perspective)
        reach_below = max(HAZE_BELOW_PX, 0.5 * hb_px)
        below_w = np.clip(1.0 + dyy / reach_below, 0.0, 1.0) * (dyy < -0.5) * open_col[None, :]
        wy = strength * np.maximum(above_w, below_w)
        out = out * (1.0 - wy[..., None]) + hazecol[None, :, :] * wy[..., None]
        # the step itself softened over a few rows on open columns
        soft_rows = open_col[None, :] & (np.abs(rows - lowrow[None, :]) <= 2)
        blurred = np.stack([ndimage.gaussian_filter1d(out[..., c], 1.2, axis=0) for c in range(3)], -1)
        out = np.where(soft_rows[..., None], blurred, out)
        facts.update(haze=round(haze * 100), haze_px=round(hb_px, 1))
    a_out = np.maximum(a, np.clip(water, 0.0, 1.0) * reflect)
    return np.clip(np.rint(out), 0, 255).astype(np.uint8), np.clip(np.rint(a_out * 255.0), 0, 255).astype(np.uint8), facts
