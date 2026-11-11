"""Exact reference movie: one H.264/AAC MP4 from a resolved plan (TIMELINE-INPUT.md 6.1, option D).

Two passes, each frame-exact by frame count (never ``-t``):

1. One lossless segment per storyline item at the sequence rate and format:
   moving sources are seeked to ``source_in`` (input seek half a frame early,
   which lands on the exact frame even with long GOPs), conformed with
   ``fps=`` and scaled to fit with padding; stills are held; gaps and disabled
   clips are black.  Overlay (V2) clips get the same treatment without padding.
2. Concat (demuxer) -> overlay V2 at its record frames -> mix the music lane
   (trimmed by sample index, delayed to its record frame, gain) with clip
   audio only where ``audio: "on"`` -> H.264 + AAC, ``-frames:v total``.

The result is counted with ffprobe and the render fails closed when the
decoded frame count differs from the storyline length.

``studio.timeline/1.1`` (picture side): the sequence format's ``fit`` is fit (letterbox), fill
(scale up and crop the centre) or stretch (scale to the frame). A retimed clip is rendered BY FRAME
INDEX: record frame ``k`` shows source frame ``PlanClip.source_frame_at(k)`` exactly (``nearest``),
or for a slow clip with ``blend`` / ``flow`` the frames between two source frames are made by
ffmpeg ``framerate`` (frame blending) or ``minterpolate`` (motion-compensated). A V1 transition is
its own segment between the two clip bodies: the left clip's frames after its out point and the
right clip's frames before its in point (their handles) are rendered and mixed per frame,
``w = (k + 1) / (D + 1)`` for frame ``k`` of ``D``: dissolve ``A (1 - w) + B w``; dip ``A`` to the
colour over the first half (``2w``), the colour to ``B`` over the second (``2w - 1``).

Film master (lane w2-cutdel): ``quality`` ``prores_422hq`` / ``prores_4444`` (prores_ks, Apple vendor
tag) and ``dnxhr_hqx`` (10-bit 4:2:2) write a ``.mov`` with PCM 24-bit 48 kHz sound next to review /
lossless (H.264/AAC ``.mp4``). The master is verified with ffprobe before it is put in place: codec and
profile, PCM 24-bit stereo, decoded frame count = the storyline, video and audio duration = frames / rate
(half a frame) and the audio's samples; any mismatch fails closed (``render_mismatch``).

Delivery loudness: ``loudness={"target_lufs": -14 | -23 | ... | None, "ceiling_dbtp": -1.0}``. A cut with
sound tracks hands it to the mix (render_mix); any other film's sound is measured and normalised after the
render (render_mix.normalise_file). Either way the written film is MEASURED (ebur128 integrated LUFS and
true peak) and a true peak above the ceiling fails closed (``loudness_over_ceiling``). Without ``loudness``
a cut with sound tracks keeps its -14 LUFS / -1 dBTP default and one without renders as it always did.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from .errors import ExportError
from .ffmpeg_run import CancelFn, child_env, run_ffmpeg
from .model import Plan, PlanClip
from .resolver import count_frames
from .timebase import ffmpeg_rate, ffmpeg_seconds, seconds

AUDIO_RATE = 48000
QUALITY = {
    # Default: visually transparent review quality, plays everywhere.
    "review": ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-profile:v", "high"],
    # Mathematically lossless 4:2:0 (x264 qp 0): proves cut frames bit-exact; large files.
    "lossless": ["-c:v", "libx264", "-preset", "veryfast", "-qp", "0", "-pix_fmt", "yuv420p"],
}
# Measured in DaVinci Resolve 21.0.3.7 free (EXPORT-PROOF-RESOLVE.md finding 5): x264 -qp 0 writes
# H.264 "High 4:4:4 Predictive", which the free edition cannot decode ("Cannot find the codec").
# The lossless preset stays (it proves cut frames bit-exact); the export says so.
RESOLVE_FREE_DECODES = {"review": True, "lossless": False}
# Film masters (.mov, PCM 24-bit): the encoder arguments and what ffprobe must read back.
MASTER = {
    # Apple ProRes family in Apple's order (ProRes White Paper 2022; Resolve 20 Deliver lists the same):
    # FFmpeg prores_ks profiles 0-5, the only free ProRes encoder on Windows (BCK-038: owner decision).
    "prores_proxy": {"video": ["-c:v", "prores_ks", "-profile:v", "0", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"],
                     "codec": "prores", "profile": "Proxy"},
    "prores_lt": {"video": ["-c:v", "prores_ks", "-profile:v", "1", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"],
                  "codec": "prores", "profile": "LT"},
    "prores_422": {"video": ["-c:v", "prores_ks", "-profile:v", "2", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"],
                   "codec": "prores", "profile": "Standard"},
    "prores_422hq": {"video": ["-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"],
                     "codec": "prores", "profile": "HQ"},
    "prores_4444": {"video": ["-c:v", "prores_ks", "-profile:v", "4", "-vendor", "apl0", "-pix_fmt", "yuv444p10le"],
                    "codec": "prores", "profile": "4444"},
    "prores_4444xq": {"video": ["-c:v", "prores_ks", "-profile:v", "5", "-vendor", "apl0", "-pix_fmt", "yuv444p10le"],
                      "codec": "prores", "profile": "XQ"},  # ffprobe names profile 5 "XQ"
    "dnxhr_hqx": {"video": ["-c:v", "dnxhd", "-profile:v", "dnxhr_hqx", "-pix_fmt", "yuv422p10le"],
                  "codec": "dnxhd", "profile": "DNXHR HQX"},
}
QUALITY.update({name: spec["video"] for name, spec in MASTER.items()})
QUALITIES = tuple(QUALITY)
MASTER_AUDIO = ["-c:a", "pcm_s24le"]
REVIEW_AUDIO = ["-c:a", "aac", "-b:a", "256k"]


def film_suffix(quality: str) -> str:
    """The film's container: QuickTime for a master, MP4 for review / lossless."""
    return ".mov" if quality in MASTER else ".mp4"
_SEGMENT_CODEC = ["-c:v", "libx264", "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv420p"]

ProgressFn = Callable[[float, str], None]


@dataclass
class RenderResult:
    path: Path
    frames: int
    rate: Fraction
    width: int
    height: int
    sha256: str
    bytes: int
    clips: list[dict[str, Any]] = field(default_factory=list)
    loudness: dict[str, Any] | None = None  # sound tracks only: target, before, after, stems
    master: dict[str, Any] | None = None  # a film master's ffprobe read-back (codec, profile, frames)


def _fit_chain(width: int, height: int, *, pad: bool, fit: str = "fit") -> str:
    if fit == "stretch":
        return f"scale={width}:{height}:flags=lanczos,setsar=1,format=yuv420p"
    if fit == "fill":
        return (f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={width}:{height}:(iw-ow)/2:(ih-oh)/2,setsar=1,format=yuv420p")
    scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"
    tail = f",pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black" if pad else ""
    return f"{scale}{tail},setsar=1,format=yuv420p"


def _clip_input(clip: PlanClip, rate: Fraction) -> list[str]:
    """Input options of one clip source, seeked to its first frame."""
    info = clip.info
    assert info is not None
    if info.is_still:
        return ["-loop", "1", "-framerate", ffmpeg_rate(rate), "-i", str(info.path)]
    assert clip.source_rate is not None
    if info.vfr:
        return ["-i", str(info.path)]  # frame-index conform: no seek by time (see _conform)
    seek = max(Fraction(0), (Fraction(clip.source_in) - Fraction(1, 2)) / clip.source_rate)
    return ["-ss", ffmpeg_seconds(seek), "-i", str(info.path)]


def _conform(clip: PlanClip) -> str:
    """Timestamps a clip's frames carry into the sequence-rate conform.

    A constant-rate source keeps its own (seeked to ``source_in``). A variable-frame-rate source
    (a phone clip) is conformed BY FRAME INDEX: frame ``source_in`` is the first one and every
    decoded frame gets the next slot of its nominal rate, so the cut shows each recorded frame once,
    in order, exactly as the interchange media copy (export_package.conform_vfr) does."""
    info = clip.info
    if info is None or info.is_still or not info.vfr or clip.source_rate is None:
        return "setpts=PTS-STARTPTS"
    return f"select=gte(n\\,{clip.source_in}),setpts=N/({ffmpeg_rate(clip.source_rate)})/TB"


def _segment_command(ffmpeg: Path, clip: PlanClip, plan: Plan, out: Path, *, pad: bool) -> list[str]:
    rate = ffmpeg_rate(plan.rate)
    if clip.kind == "gap" or clip.info is None or not clip.enabled:
        return [str(ffmpeg), "-y", "-f", "lavfi", "-i", f"color=black:s={plan.width}x{plan.height}:r={rate}",
                "-frames:v", str(clip.duration), "-vf", "setsar=1,format=yuv420p", *_SEGMENT_CODEC, "-an", str(out)]
    if clip.retime is not None and _mapped(clip):
        return _mapped_command(ffmpeg, clip, plan, out, pad=pad, first=0, count=clip.duration)
    if clip.retime is not None:
        return _retimed_command(ffmpeg, clip, plan, out, pad=pad, first=0, count=clip.duration)
    chain = f"{_conform(clip)},fps={rate},{_fit_chain(plan.width, plan.height, pad=pad, fit=plan.fit)}"
    return [str(ffmpeg), "-y", *_clip_input(clip, plan.rate), "-map", "0:v:0", "-vf", chain,
            "-frames:v", str(clip.duration), "-fps_mode", "cfr", "-r", rate, *_SEGMENT_CODEC, "-an", str(out)]


def _range_command(ffmpeg: Path, clip: PlanClip, plan: Plan, out: Path, first: int, count: int) -> list[str]:
    """Record offsets ``first .. first + count - 1`` of a storyline clip (negative or past its
    duration: its handles), at the sequence format."""
    if first == 0 and count == clip.duration:
        return _segment_command(ffmpeg, clip, plan, out, pad=True)
    if clip.kind == "gap" or clip.info is None or not clip.enabled or clip.is_still:
        return _segment_command(ffmpeg, replace(clip, duration=count), plan, out, pad=True)
    if clip.retime is not None and _mapped(clip):
        return _mapped_command(ffmpeg, clip, plan, out, pad=True, first=first, count=count)
    if clip.retime is not None:
        return _retimed_command(ffmpeg, clip, plan, out, pad=True, first=first, count=count)
    shifted = clip.source_frame_at(first, plan.rate)
    return _segment_command(ffmpeg, replace(clip, source_in=shifted, duration=count), plan, out, pad=True)


def _retimed_command(ffmpeg: Path, clip: PlanClip, plan: Plan, out: Path, *, pad: bool, first: int, count: int) -> list[str]:
    """A retimed clip by frame index. Source frames ``lo .. hi`` are cut from the source (seeked half
    a frame early like every clip), reversed when the clip plays backwards, then each input frame
    ``j`` gets the first record frame that shows it, ``ceil((j + B) / q) - first`` with ``q`` the
    source frames per record frame and ``B = floor(first * q)``; frames no record frame shows are
    dropped and ``fps`` repeats the held ones. ``blend`` / ``flow`` (slow clips only) make the frames
    between with ``framerate`` / ``minterpolate`` instead of repeating."""
    rate = plan.rate
    retime = clip.retime
    assert retime is not None and clip.source_rate is not None and clip.info is not None
    frames = [clip.source_frame_at(first + k, rate) for k in (0, count - 1)]
    lo, hi = min(frames), max(frames)
    if retime.freeze:
        lo = hi = clip.source_in
    slow = retime.sampling != "nearest" and not retime.freeze and clip.step(rate) < 1
    # Making frames after the last shown source frame needs the next one (when the file has it).
    extra_lo = 1 if slow and retime.reverse and lo > 0 else 0
    extra_hi = 1 if slow and not retime.reverse and hi + 1 < clip.info.frames else 0
    lo, hi = lo - extra_lo, hi + extra_hi
    seek = max(Fraction(0), (Fraction(lo) - Fraction(1, 2)) / clip.source_rate)
    fit = _fit_chain(plan.width, plan.height, pad=pad, fit=plan.fit)
    r = ffmpeg_rate(rate)
    head = f"trim=end_frame={hi - lo + 1},setpts=PTS-STARTPTS," + ("reverse," if retime.reverse else "")
    if retime.freeze:
        chain = f"trim=end_frame=1,loop=loop={count}:size=1:start=0,settb=expr={rate.denominator}/{rate.numerator},setpts=N"
    else:
        q = clip.step(rate)
        n, d = q.numerator, q.denominator
        base = (Fraction(first) * q).numerator // (Fraction(first) * q).denominator
        if slow:
            # Input frame j sits at record time (j + B) / q - first (fractional); the filter makes the frames between.
            make = ("framerate=fps=" + r + ":interp_start=0:interp_end=255:scene=100" if retime.sampling == "blend"
                    else f"minterpolate=fps={r}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
            chain = (f"{head}settb=expr={rate.denominator}/{rate.numerator * n},"
                     f"setpts=(N+{base})*{d}-{first * n},{make},fps={r}:start_time=0,tpad=stop_mode=clone:stop={count}")
        else:
            pts = f"floor(((N+{base})*{d}+{n - 1})/{n})"
            keep = f"eq(floor(max({first}\\,floor(((n+{base})*{d}+{n - 1})/{n}))*{n}/{d})\\,n+{base})"
            chain = (f"{head}settb=expr={rate.denominator}/{rate.numerator},setpts='max(0\\,{pts}-{first})',"
                     f"select='{keep}',fps={r},tpad=stop_mode=clone:stop={count}")
    return [str(ffmpeg), "-y", "-ss", ffmpeg_seconds(seek), "-i", str(clip.info.path), "-map", "0:v:0",
            "-vf", f"{chain},{fit}", "-frames:v", str(count), "-fps_mode", "cfr", "-r", r, *_SEGMENT_CODEC, "-an", str(out)]


# --- speed curves and AI frames (lane w6-tempo) ----------------------------------------------------
# A clip with a speed curve, or one whose in-between frames come from RIFE (sampling ``ai``), is
# rendered by MAPPING: the source frames it reads (reversed when it plays backwards) are fitted to the
# sequence format and, when a part is slower than 100 %, made ``MULT`` times denser (blend: ffmpeg
# framerate, flow: minterpolate, ai: RIFE through the local ComfyUI; source frame i lands on dense
# frame i * MULT, measured). Record frame ``k`` shows source frame ``floor(P(k))`` when ``P(k)`` is
# whole, the clip runs at 100 % or faster there, or sampling is nearest; otherwise the in-between
# nearest to ``P(k)``. ``P`` is speed_curve.position (the one mapping). Frames are piped as raw
# yuv420p into the lossless segment.
MULT = 8


def _mapped(clip: PlanClip) -> bool:
    retime = clip.retime
    return retime is not None and not retime.freeze and (bool(retime.curve) or retime.sampling == "ai")


def _positions(clip: PlanClip, rate: Fraction, first: int, count: int) -> tuple[list[Fraction], int, int]:
    """Exact source positions (source frames after the clip's first shown one, in play order) of
    record offsets ``first ..``; plus the lowest and highest source frame they need."""
    from .model import speed_curve
    sc = speed_curve()
    q = clip.source_rate / rate
    keys = clip.keys() or sc.constant_keys(clip.retime.speed)
    out = []
    for k in range(first, first + count):
        if k < 0:
            out.append(Fraction(k) * q * Fraction(keys[0]["percent"]) / 100)
        elif k > clip.duration:
            out.append(sc.position(keys, q, clip.duration) + Fraction(k - clip.duration) * q * Fraction(keys[-1]["percent"]) / 100)
        else:
            out.append(sc.position(keys, q, k))
    used = clip.source_frames_used(rate)
    floors = [p.numerator // p.denominator for p in out]
    if clip.retime.reverse:
        top = clip.source_in + used - 1
        frames = [top - f for f in floors]
    else:
        frames = [clip.source_in + f for f in floors]
    return out, min(frames), max(frames)


def _mapped_command(ffmpeg: Path, clip: PlanClip, plan: Plan, out: Path, *, pad: bool, first: int, count: int) -> list[str]:
    """Not an argv: a marker list run_mapped understands (``["__mapped__", ...]``)."""
    return ["__mapped__", str(ffmpeg), clip.id, str(out), "1" if pad else "0", str(first), str(count)]


def _steps(positions: list[Fraction]) -> list[Fraction]:
    """Source frames advanced from each record frame to the next (the last repeats the one before)."""
    if len(positions) < 2:
        return [Fraction(1)] * len(positions)
    out = [b - a for a, b in zip(positions, positions[1:])]
    return out + [out[-1]]


def run_mapped(command: list[str], plan: Plan, *, on_frame=None, should_cancel=None, comfy=None) -> None:
    """Render one mapped piece (see MULT above). Source frames are exact wherever a record frame sits
    on a whole source position or the clip runs at 100 % or faster; only slow parts take in-betweens."""
    import subprocess
    _marker, ffmpeg, clip_id, out, pad, first, count = command
    clip = next(c for c in [*plan.storyline, *plan.overlays] if c.id == clip_id)
    first, count = int(first), int(count)
    rate, retime = plan.rate, clip.retime
    positions, lo, hi = _positions(clip, rate, first, count)
    steps = _steps(positions)
    slowest = min((st for st in steps if st > 0), default=Fraction(1))
    mult = 1 if retime.sampling == "nearest" or slowest >= 1 else MULT
    if mult > 1:  # the in-betweens after the last shown frame need the next one (when the file has it)
        lo, hi = (max(0, lo - 1), hi) if retime.reverse else (lo, min(clip.info.frames - 1, hi + 1))
    used = clip.source_frames_used(rate)
    origin = (clip.source_in + used - 1 - hi) if retime.reverse else (lo - clip.source_in)
    fit = _fit_chain(plan.width, plan.height, pad=pad == "1", fit=plan.fit)
    seek = max(Fraction(0), (Fraction(lo) - Fraction(1, 2)) / clip.source_rate)
    head = f"trim=end_frame={hi - lo + 1},setpts=PTS-STARTPTS," + ("reverse," if retime.reverse else "")
    size = plan.width * plan.height * 3 // 2
    work = Path(out).parent / f".{Path(out).stem}-map"
    work.mkdir(exist_ok=True)
    quiet = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    try:
        src = work / "src.yuv"
        run_ffmpeg([ffmpeg, "-y", "-ss", ffmpeg_seconds(seek), "-i", str(clip.info.path), "-map", "0:v:0", "-vf", f"{head}{fit}",
                    "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", str(src)],
                   should_cancel=should_cancel, what=f"Segment {clip.id} source frames")
        n_src = src.stat().st_size // size
        dense = None
        if mult > 1:
            dense = work / "dense.yuv"
            if retime.sampling == "ai":
                if comfy is None:
                    from . import rife_comfy as comfy
                comfy.interpolate(Path(ffmpeg), src, (plan.width, plan.height), n_src, mult, dense, work, should_cancel=should_cancel)
            else:
                make = (f"framerate=fps={24 * mult}:interp_start=0:interp_end=255:scene=100" if retime.sampling == "blend"
                        else f"minterpolate=fps={24 * mult}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1")
                run_ffmpeg([ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{plan.width}x{plan.height}", "-framerate", "24",
                            "-i", str(src), "-vf", f"{make},format=yuv420p", "-fps_mode", "passthrough", "-f", "rawvideo", str(dense)],
                           should_cancel=should_cancel, what=f"Segment {clip.id} in-betweens")
        encoder = subprocess.Popen([ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{plan.width}x{plan.height}",
                                    "-framerate", ffmpeg_rate(rate), "-i", "-", "-frames:v", str(count), *_SEGMENT_CODEC, "-an", str(out)],
                                   stdin=subprocess.PIPE, stderr=subprocess.DEVNULL, **quiet, env=child_env())
        try:
            with src.open("rb") as fs, (dense.open("rb") if dense is not None else open(src, "rb")) as fd:
                def read(handle, index: int, total: int) -> bytes:
                    index = max(0, min(total - 1, index))
                    handle.seek(index * size)
                    return handle.read(size)
                n_dense = (dense.stat().st_size // size) if dense is not None else n_src
                for written, (p, st) in enumerate(zip(positions, steps), 1):
                    rel = p - origin
                    whole = rel.numerator // rel.denominator
                    scaled = rel * mult
                    tick = (2 * scaled.numerator + scaled.denominator) // (2 * scaled.denominator)  # nearest in-between
                    if dense is None or st >= 1 or rel.denominator == 1:
                        frame = read(fs, whole, n_src)
                    else:
                        frame = read(fd, tick, n_dense)
                    encoder.stdin.write(frame)
                    if on_frame is not None:
                        on_frame(written)
                    if should_cancel is not None and should_cancel():
                        raise ExportError("cancelled", "The export was cancelled.")
        finally:
            try:
                encoder.stdin.close()
            except OSError:
                pass
            encoder.wait(timeout=600)
        if encoder.returncode != 0:
            raise ExportError("render_failed", f"Segment {clip.id} could not be written.")
    finally:
        shutil.rmtree(work, ignore_errors=True)


DIP_COLOUR = {"dip_black": (16, 128, 128), "dip_white": (235, 128, 128)}  # limited-range yuv420p


def transition_weight(k: int, duration: int) -> Fraction:
    """The B weight of transition frame ``k`` of ``duration``: never 0 or 1 inside the transition."""
    return Fraction(k + 1, duration + 1)


def _transition_command(ffmpeg: Path, kind: str, a: Path, b: Path, duration: int, out: Path, rate: Fraction) -> list[str]:
    # Frame k from its timestamp (blend's N is not 0 on the first frame here: measured, ffmpeg 7.1).
    w = f"((round(T*{rate.numerator}/{rate.denominator})+1)/{duration + 1})"
    if kind == "dissolve":
        exprs = {"all_expr": f"A*(1-{w})+B*{w}"}
    else:
        colour = DIP_COLOUR[kind]
        exprs = {f"c{plane}_expr": f"if(lte({w}\\,0.5)\\,A*(1-2*{w})+{c}*2*{w}\\,{c}*(2-2*{w})+B*(2*{w}-1))"
                 for plane, c in enumerate(colour)}
    blend = "blend=" + ":".join(f"{key}='{value}'" for key, value in exprs.items())
    return [str(ffmpeg), "-y", "-i", str(a), "-i", str(b), "-filter_complex",
            f"[0:v:0]setpts=PTS-STARTPTS[a];[1:v:0]setpts=PTS-STARTPTS[b];[a][b]{blend},format=yuv420p[v]",
            "-map", "[v]", "-frames:v", str(duration), "-fps_mode", "cfr", "-r", ffmpeg_rate(rate), *_SEGMENT_CODEC, "-an", str(out)]


def storyline_pieces(plan: Plan) -> list[tuple[str, Any]]:
    """V1 in record order: ``("clip", (clip, first, count))`` bodies and ``("transition", t)``."""
    by_left = {t.left: t for t in plan.transitions}
    by_right = {t.right: t for t in plan.transitions}
    pieces: list[tuple[str, Any]] = []
    for clip in plan.storyline:
        first = by_right[clip.id].after if clip.id in by_right else 0
        last = clip.duration - (by_left[clip.id].before if clip.id in by_left else 0)
        if last > first:
            pieces.append(("clip", (clip, first, last - first)))
        if clip.id in by_left:
            pieces.append(("transition", by_left[clip.id]))
    return pieces


def _samples(frames: int, rate: Fraction) -> int:
    value = Fraction(frames) * AUDIO_RATE / rate
    return int(value.numerator * 2 + value.denominator) // (2 * value.denominator)  # round half up


def _audio_chain(index: int, start_sample: int, length: int, delay: int, gain_db: float, label: str,
                 tempo: Fraction = Fraction(1)) -> str:
    gain = f",volume={gain_db}dB" if gain_db else ""
    from .render_mix import source_samples, tempo_chain
    return (
        f"[{index}:a:0]aresample={AUDIO_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"atrim=start_sample={start_sample}:end_sample={start_sample + source_samples(length, tempo)},asetpts=N/SR/TB"
        f"{tempo_chain(tempo, length)}{gain},"
        f"adelay=delays={delay}S:all=1[{label}]"
    )


def render_film(
    plan: Plan,
    out_path: Path,
    *,
    ffmpeg: Path,
    ffprobe: Path,
    quality: str = "review",
    work_dir: Path | None = None,
    progress: ProgressFn | None = None,
    should_cancel: CancelFn | None = None,
    audio_dir: Path | None = None,
    music_dry: bool = False,
    loudness: dict[str, Any] | None = None,
    subtitles: dict[str, Any] | None = None,
) -> RenderResult:
    """``subtitles`` (lane w23-subs): a subtitle entry (studio.subtitles/1, cropped to the plan) burned into the
    picture with subtitle_raster's PNGs, the same pictures the Cut viewer shows.
    ``audio_dir``: where a cut with sound tracks A1-A3 writes mix.wav and the D/M/E stems; the
    film then carries that mix through two-pass loudnorm (render_mix). Without sound tracks the
    audio chain below is the one it always was."""
    if quality not in QUALITY:
        raise ExportError("invalid_request", "Quality must be " + ", ".join(QUALITIES) + ".")
    target_lufs, ceiling, lra = _delivery(loudness)
    out_path = Path(out_path)
    work = Path(work_dir) if work_dir is not None else out_path.parent / f".render-{uuid.uuid4().hex}"
    work.mkdir(parents=True, exist_ok=False)
    total = plan.total_frames
    overlays = [clip for clip in plan.overlays if plan.overlay_enabled and clip.enabled and clip.record_start < total]
    budget = total * 2 + sum(min(clip.duration, total - clip.record_start) for clip in overlays)
    done = 0

    def report(stage: str, frames: int) -> None:
        if progress is not None:
            progress(min(1.0, (done + frames) / max(1, budget)), stage)

    try:
        segments: list[Path] = []
        clips = {clip.id: clip for clip in plan.storyline}

        def render_piece(command: list[str], segment: Path, frames: int, what: str) -> None:
            last = [0]

            def on_frame(frame: int, _last: list[int] = last) -> None:
                _last[0] = frame
                report("segments", frame)

            if command[0] == "__mapped__":
                run_mapped(command, plan, on_frame=on_frame, should_cancel=should_cancel)
                last[0] = count_frames(ffprobe, segment)
            else:
                run_ffmpeg(command, on_frame=on_frame, should_cancel=should_cancel, what=what)
            if last[0] != frames:
                raise ExportError("render_mismatch", f"{what} has {last[0]} frames, expected {frames}.")

        for index, (kind, piece) in enumerate(storyline_pieces(plan)):
            segment = work / f"s{index:04d}.mkv"
            if kind == "clip":
                clip, first, count = piece
                render_piece(_range_command(ffmpeg, clip, plan, segment, first, count), segment, count, f"Segment {clip.id}")
                done += count
            else:
                left, right = clips[piece.left], clips[piece.right]
                a, b = work / f"s{index:04d}a.mkv", work / f"s{index:04d}b.mkv"
                render_piece(_range_command(ffmpeg, left, plan, a, left.duration - piece.before, piece.duration), a, piece.duration, f"Transition {piece.id} out of {left.id}")
                render_piece(_range_command(ffmpeg, right, plan, b, -piece.before, piece.duration), b, piece.duration, f"Transition {piece.id} into {right.id}")
                render_piece(_transition_command(ffmpeg, piece.kind, a, b, piece.duration, segment, plan.rate), segment, piece.duration, f"Transition {piece.id}")
                done += piece.duration
            segments.append(segment)
        overlay_files: list[tuple[PlanClip, Path, int]] = []
        for index, clip in enumerate(overlays):
            length = min(clip.duration, total - clip.record_start)
            segment = work / f"o{index:04d}.mkv"
            command = _segment_command(ffmpeg, clip, plan, segment, pad=False)
            command[command.index("-frames:v") + 1] = str(length)
            run_ffmpeg(command, on_frame=lambda frame: report("overlays", frame), should_cancel=should_cancel,
                       what=f"Overlay {clip.id}")
            done += length
            overlay_files.append((clip, segment, length))
        concat = work / "concat.txt"
        concat.write_text("".join(f"file '{segment.name}'\n" for segment in segments), encoding="utf-8")

        inputs: list[str] = ["-f", "concat", "-safe", "0", "-i", str(concat)]
        graph: list[str] = []
        video = "0:v:0"
        next_index = 1
        for number, (clip, segment, length) in enumerate(overlay_files):
            inputs += ["-i", str(segment)]
            offset = ffmpeg_seconds(seconds(clip.record_start, plan.rate))
            graph.append(f"[{next_index}:v:0]setpts=PTS-STARTPTS+{offset}/TB[ov{number}]")
            out_label = f"v{number}"
            # A segment carries no duration for its last frame, so framesync sees the
            # overlay's EOF at that frame's start and ``pass`` would drop it.  Repeat the
            # last frame and let the timeline ``enable`` pin exactly the record frames.
            last_frame = clip.record_start + length - 1
            graph.append(f"[{video}][ov{number}]overlay=x=(W-w)/2:y=(H-h)/2:eof_action=repeat:format=yuv420:"
                         f"enable='between(n,{clip.record_start},{last_frame})'[{out_label}]")
            video = out_label
            next_index += 1
        audio_labels: list[str] = []
        total_samples = _samples(total, plan.rate)
        mixed = None
        if plan.sound_tracks:
            from . import render_mix
            mixed = render_mix.render_mix(plan, Path(audio_dir) if audio_dir is not None else work / "audio", ffmpeg=ffmpeg,
                                          should_cancel=should_cancel, music_dry=music_dry,
                                          **({} if loudness is None else {"target_lufs": target_lufs, "ceiling_dbtp": ceiling, "lra_lu": lra}))
            inputs += ["-i", str(mixed.files["mix"])]
            chain = f"[{next_index}:a:0]asetpts=N/SR/TB"
            if mixed.loudnorm is not None:
                tp = mixed.ceiling_dbtp - render_mix.CEILING_MARGIN
                chain += f",{render_mix.loudnorm_filter(mixed.loudnorm, mixed.target_lufs, tp, mixed.lra_lu)},aresample={AUDIO_RATE}"
            elif mixed.gain_db:
                chain += f",volume={mixed.gain_db:g}dB"
            graph.append(f"{chain},apad=whole_len={total_samples},atrim=end_sample={total_samples},asetpts=N/SR/TB[aout]")
            next_index += 1
        elif plan.music is not None:
            music = plan.music
            length = min(music.duration, total - music.record_start)
            inputs += ["-i", str(music.info.path)]
            graph.append(_audio_chain(next_index, _samples(music.source_in, plan.rate), _samples(length, plan.rate),
                                      _samples(music.record_start, plan.rate), music.gain_db, "music"))
            audio_labels.append("music")
            next_index += 1
        for clip in [] if mixed is not None else [*plan.storyline, *overlays]:
            if not (clip.kind == "clip" and clip.enabled and clip.audio and clip.info is not None and clip.source_rate):
                continue
            length = min(clip.duration, total - clip.record_start)
            start = Fraction(clip.source_in) / clip.source_rate * AUDIO_RATE
            inputs += ["-i", str(clip.info.path)]
            label = f"ca{len(audio_labels)}"
            graph.append(_audio_chain(next_index, round(start), _samples(length, plan.rate),
                                      _samples(clip.record_start, plan.rate), 0.0, label,
                                      clip.retime.tempo if clip.retime is not None else Fraction(1)))
            audio_labels.append(label)
            next_index += 1
        if mixed is not None:
            pass
        elif audio_labels:
            mix = "".join(f"[{label}]" for label in audio_labels)
            graph.append(f"{mix}amix=inputs={len(audio_labels)}:normalize=0:duration=longest,"
                         f"apad=whole_len={total_samples},atrim=end_sample={total_samples},asetpts=N/SR/TB[aout]")
        else:
            inputs += ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
            graph.append(f"[{next_index}:a:0]atrim=end_sample={total_samples},asetpts=N/SR/TB[aout]")
        if subtitles is not None:
            # Lane w23-subs (CUT-011, Resolve "Burn into video"): one RGBA stream of the subtitle states, each held for
            # its exact frames (fps at the cut's rate), laid over the picture before the colour tag.
            from . import subtitle_raster
            burn = subtitle_raster.burn_inputs(subtitles, plan.width, plan.height, total, ffmpeg_rate(plan.rate), work / "subtitles")
            if burn is not None:
                index = inputs.count("-i")
                inputs += burn
                graph.append(subtitle_raster.burn_filter(index, ffmpeg_rate(plan.rate), matrix=plan.color.matrix))
                graph.append(f"[{video}][subs]overlay=x=0:y=0:eof_action=pass:format=yuv420[vsubs]")  # in the picture's own 4:2:0, frames without a subtitle stay bit-identical
                video = "vsubs"
        tag = plan.color.setparams()  # the cut's colour space on every frame (CUT-061, BCK-037)
        if video == "0:v:0":
            graph.append(f"[0:v:0]{tag}[vout]")
        else:
            graph.append(f"[{video}]{tag}[vout]")
        suffix = film_suffix(quality)
        temp = out_path.with_name(f".{out_path.stem}.{uuid.uuid4().hex}{suffix}")
        audio_args = MASTER_AUDIO if quality in MASTER else REVIEW_AUDIO
        command = [
            str(ffmpeg), "-y", *inputs, "-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
            "-frames:v", str(total), *QUALITY[quality], *plan.color.ffmpeg_args(), "-r", ffmpeg_rate(plan.rate),
            *audio_args, "-ar", str(AUDIO_RATE), "-ac", "2",
            "-movflags", "+faststart", "-map_metadata", "-1",
            "-metadata", f"title={plan.name}", "-metadata", "comment=studio.timeline-render/1", str(temp),
        ]
        result_loudness = None
        normalised = temp.with_name(temp.stem + ".loud" + suffix)
        try:
            run_ffmpeg(command, on_frame=lambda frame: report("film", frame), should_cancel=should_cancel, what="Film")
            # w28-fix item 0 (CUT-182): the whole-file frame count is a pass of its own (~60 s for 7200 frames), so it
            # names its stage (Deliver: "Checking frames") and a Stop during it cancels instead of being ignored.
            report("checking", total)
            measured = count_frames(ffprobe, temp, should_cancel) if should_cancel is not None else count_frames(ffprobe, temp)
            if should_cancel is not None and should_cancel():
                raise ExportError("cancelled", "The export was cancelled.")
            if mixed is not None:
                from . import render_mix
                applied = ("loudnorm two-pass" if mixed.loudnorm is not None
                           else f"gain {mixed.gain_db:+g} dB (true-peak ceiling)" if mixed.gain_db else None)
                after = render_mix.measure(ffmpeg, temp) if (mixed.loudnorm is not None or loudness is not None) else None
                result_loudness = render_mix.report(mixed, after, applied)
                result_loudness["warnings"] = list(mixed.warnings)
                result_loudness["stems"] = {name: path for name, path in mixed.files.items()}
            if measured != total:
                raise ExportError("render_mismatch", f"The film has {measured} frames, the storyline {total}.")
            if mixed is None and loudness is not None:
                from . import render_mix
                normed = render_mix.normalise_file(ffmpeg, temp, normalised, target_lufs=target_lufs, ceiling_dbtp=ceiling,
                                                   audio_args=audio_args, total_samples=total_samples, should_cancel=should_cancel, lra_lu=lra)
                if normed["written"]:
                    os.replace(normalised, temp)
                result_loudness = {"target": {"integrated_lufs": target_lufs, "true_peak_dbtp": ceiling},
                                   "before": normed["before"], "after": render_mix.measure(ffmpeg, temp),
                                   "normalization": normed["normalization"], "lra_target_lu": lra,
                                   "warnings": [normed["warning"]] if normed.get("warning") else [], "stems": {}}
            if result_loudness is not None and result_loudness["after"] is not None:
                peak = result_loudness["after"]["true_peak_dbtp"]
                limit = result_loudness["target"]["true_peak_dbtp"]
                if peak is not None and peak > limit:
                    raise ExportError("loudness_over_ceiling",
                                      f"The film's true peak is {peak:+.1f} dBTP, above the ceiling of {limit:+.1f} dBTP; nothing was exported.",
                                      {"true_peak_dbtp": peak, "ceiling_dbtp": limit, "integrated_lufs": result_loudness["after"]["integrated_lufs"]})
            master = verify_master(ffprobe, temp, quality, frames=total, rate=plan.rate, samples=total_samples) if quality in MASTER else None
            os.replace(temp, out_path)
        finally:
            for leftover in (temp, normalised):
                if leftover.exists():
                    leftover.unlink()
        if progress is not None:
            progress(1.0, "done")
        digest = hashlib.sha256()
        with out_path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return RenderResult(
            path=out_path, frames=total, rate=plan.rate, width=plan.width, height=plan.height,
            sha256=digest.hexdigest(), bytes=out_path.stat().st_size, clips=clip_table(plan), loudness=result_loudness,
            master=master,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _delivery(loudness: dict[str, Any] | None) -> tuple[float | None, float, float | None]:
    """(target_lufs or None, ceiling_dbtp, lra_lu or None = Off) of a delivery loudness request; refused
    when out of range. lra_lu defaults to 11 LU (EBU R128 loudness range, w14-dlv CUT-069)."""
    from .render_mix import DEFAULT_LRA
    if loudness is None:
        return None, -1.0, DEFAULT_LRA
    if not isinstance(loudness, dict) or set(loudness) - {"target_lufs", "ceiling_dbtp", "lra_lu"}:
        raise ExportError("invalid_request", "loudness takes target_lufs, ceiling_dbtp and lra_lu.")
    lra = loudness.get("lra_lu", DEFAULT_LRA)
    if lra is not None and (isinstance(lra, bool) or not isinstance(lra, (int, float)) or not 1 <= lra <= 20):
        raise ExportError("invalid_request", "loudness.lra_lu must be a number in 1..20 (or null: Off).")
    target = loudness.get("target_lufs")
    ceiling = loudness.get("ceiling_dbtp", -1.0)
    for value, low, high, name in ((target, -40, -5, "target_lufs"), (ceiling, -12, 0, "ceiling_dbtp")):
        if value is None and name == "target_lufs":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
            raise ExportError("invalid_request", f"loudness.{name} must be a number in {low}..{high}.")
    return (None if target is None else float(target)), float(ceiling), (None if lra is None else float(lra))


def probe_streams(ffprobe: Path, path: Path) -> list[dict[str, Any]]:
    import json
    import subprocess
    entries = "stream=codec_type,codec_name,profile,pix_fmt,sample_rate,channels,duration,duration_ts"
    try:
        done = subprocess.run([str(ffprobe), "-v", "error", "-show_entries", entries, "-of", "json", str(path)],
                              capture_output=True, text=True, encoding="utf-8", timeout=600, check=True, env=child_env())
        return json.loads(done.stdout).get("streams") or []
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ExportError("render_mismatch", f"ffprobe could not read {path.name}.") from exc


def verify_master(ffprobe: Path, path: Path, quality: str, *, frames: int, rate: Fraction, samples: int) -> dict[str, Any]:
    """Fail closed unless the master reads back as written: codec + profile, PCM 24-bit 48 kHz stereo,
    the decoded frame count, video and audio duration = frames / rate (half a frame) and the samples."""
    spec = MASTER[quality]
    streams = probe_streams(ffprobe, path)
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    problems = []
    if len(video) != 1 or video[0].get("codec_name") != spec["codec"] or video[0].get("profile") != spec["profile"]:
        problems.append(f"video is {[(s.get('codec_name'), s.get('profile')) for s in video]}, expected {spec['codec']} {spec['profile']}")
    if len(audio) != 1 or audio[0].get("codec_name") != "pcm_s24le" or audio[0].get("sample_rate") != str(AUDIO_RATE) or audio[0].get("channels") != 2:
        problems.append(f"audio is {[(s.get('codec_name'), s.get('sample_rate'), s.get('channels')) for s in audio]}, expected pcm_s24le 48000 2")
    expected = Fraction(frames) / rate
    for stream, name in ((video[:1], "video"), (audio[:1], "audio")):
        if not stream:
            continue
        try:
            got = Fraction(stream[0]["duration"])
        except (KeyError, ValueError, TypeError):
            problems.append(f"{name} has no duration")
            continue
        if abs(got - expected) > Fraction(1, 2) / rate:
            problems.append(f"{name} lasts {float(got):.4f} s, the cut {float(expected):.4f} s")
    if audio and str(audio[0].get("duration_ts")) != str(samples):
        problems.append(f"audio has {audio[0].get('duration_ts')} samples, the film {samples}")
    counted = count_frames(ffprobe, path)
    if counted != frames:
        problems.append(f"{counted} frames decoded, the storyline has {frames}")
    if problems:
        raise ExportError("render_mismatch", "The film master did not verify; nothing was exported.", {"problems": problems})
    return {"codec": spec["codec"], "profile": spec["profile"], "pix_fmt": video[0].get("pix_fmt"), "frames": counted,
            "duration_seconds": round(float(expected), 6), "audio": {"codec": "pcm_s24le", "sample_rate": AUDIO_RATE, "channels": 2, "samples": samples}}


def clip_table(plan: Plan) -> list[dict[str, Any]]:
    """Manifest rows: clip -> source ref, source in/out frames, record in/out, provenance."""
    rows: list[dict[str, Any]] = []
    for clip in [*plan.storyline, *plan.overlays]:
        row: dict[str, Any] = {
            "id": clip.id, "track": clip.track, "type": clip.kind,
            "record_in": clip.record_start, "record_out": clip.record_end, "duration_frames": clip.duration,
            "enabled": clip.enabled,
        }
        if clip.kind == "clip" and clip.info is not None:
            row.update({
                "source": dict(clip.source or {}),
                "still": clip.is_still,
                "source_in": clip.source_in if not clip.is_still else None,
                "source_out": clip.source_in + clip.source_frames_used(plan.rate) if not clip.is_still else None,
                "source_rate": None if clip.source_rate is None else {"num": clip.source_rate.numerator, "den": clip.source_rate.denominator},
                "source_frames": clip.info.frames,
                "audio": "on" if clip.audio else "off",
                "file_name": clip.info.path.name,
            })
            if clip.retime is not None:
                row["retime"] = {"speed": {"num": clip.retime.speed.numerator, "den": clip.retime.speed.denominator},
                                 "reverse": clip.retime.reverse, "sampling": clip.retime.sampling, "freeze": clip.retime.freeze}
            if clip.info.provenance:
                row["provenance"] = dict(clip.info.provenance)
        rows.append(row)
    return rows
