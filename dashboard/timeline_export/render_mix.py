"""The sound mix of a cut with sound tracks A1-A3: stems, mix.wav, loudness (lane fs-a2).

Only used when the plan has sound tracks; a cut without them renders exactly as before
(render_film's own audio chain, byte-identical ffmpeg argv).

One ffmpeg run renders every source, sample-indexed like the song lane (``atrim`` by start
sample, ``adelay`` by record sample, ``apad``/``atrim`` to the film length): clip gain + track
gain (``volume``), linear fades (``afade curve=tri``: gain k/n at sample k of the fade), a mono
file on both channels at its own level (``pan``, no pan law). Sources go to the stem of their
role: dialogue (A tracks of role dialogue + the picture clips' own sound set to on), music
(role music + the song lane), effects (role effects). The stems are summed (``amix
normalize=0``) to ``mix.wav``; all four are 48 kHz 24-bit WAV with exactly the film's samples.

``film.mp4`` gets the mix through two-pass ``loudnorm`` to the online target: pass 1 measures
the mix, pass 2 applies the measured values (``linear=true``: one gain when the true-peak
ceiling allows it, else ffmpeg's dynamic mode). The ceiling is set 0.5 dB under the target
(-1.5 dBTP) for the AAC encode's overshoot; the report states ebur128 integrated LUFS, true
peak and LRA of the mix (before) and of the film's AAC stream (after).

Delivery loudness (lane w2-cutdel): the target (``target_lufs``, e.g. -14 online / -23 broadcast, or None:
no loudness target) and the true-peak ceiling (``ceiling_dbtp``, default -1.0) are parameters. Without a
target the mix only gets the one gain that brings its measured true peak under the ceiling (never louder).
``normalise_file`` does the same on a rendered film whose sound is not a sound-track mix (song lane + clip
sound): two-pass on the file's own audio, the picture copied. The caller measures the result and fails
closed above the ceiling.

Volume automation (lane fs-duck): a sound clip's ``volume_keys`` (source frames, dB) become one
float32 gain envelope per clip at 48 kHz, computed here with numpy (key positions exact:
source_frame / source_rate * 48000 samples; linear in dB between keys, held outside; gain =
10^(dB/20)) and multiplied into the clip's chain by ``amultiply`` after gain and fades, before
``adelay``: every sample gets exactly its envelope value. The music stem carries the automation
(the three stems still sum to the mix: a re-mixer gets what the artist heard); ``music_dry=True``
adds ``music-dry.wav``: the same music without automation, for a re-mix from scratch.
"""
from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from .errors import ExportError
from .ffmpeg_run import CancelFn, child_env, run_ffmpeg
from .model import Plan

AUDIO_RATE = 48000
TARGET_LUFS = -14.0
TARGET_TP = -1.0
LOUDNORM_TP = -1.5  # 0.5 dB under the target for the AAC encode's inter-sample overshoot
CEILING_MARGIN = 0.5  # LOUDNORM_TP = TARGET_TP - CEILING_MARGIN
LOUDNORM_LRA = 50.0  # LRA "Off": the widest range keeps the mix's own dynamics (linear) whenever the peak allows
# w14-dlv (CUT-069, EBU R128 / Tech 3342): the delivery loudness range; 11 LU for -14 LUFS web delivery. A mix
# whose range is wider is brought inside it by loudnorm's dynamic mode; None = Off (LOUDNORM_LRA).
DEFAULT_LRA = 11.0
ROLES = ("dialogue", "music", "effects")
_SUMMARY = re.compile(r"Summary:(.*)", re.S)


@dataclass
class MixResult:
    files: dict[str, Path]  # mix, dialogue, music, effects
    loudness_before: dict[str, float | None]
    loudnorm: dict[str, str] | None  # pass-1 measurement for pass 2; None: silent mix, not normalised
    warnings: list[str]
    target_lufs: float | None = TARGET_LUFS
    ceiling_dbtp: float = TARGET_TP
    gain_db: float = 0.0  # no target: the gain that keeps the mix under the ceiling (0 or less)
    lra_lu: float | None = DEFAULT_LRA  # the loudness-range setting (None = Off)


def samples(frames: int, rate: Fraction) -> int:
    value = Fraction(frames) * AUDIO_RATE / rate
    return int(value.numerator * 2 + value.denominator) // (2 * value.denominator)  # round half up


def envelope(keys: tuple[tuple[int, float], ...], source_rate: Fraction, start: int, length: int) -> Any:
    """Linear gain per output sample of one clip: sample k is source sample ``start + k`` at 48 kHz."""
    numpy = importlib.import_module("numpy")
    positions = numpy.array([float(Fraction(frame) / source_rate * AUDIO_RATE - start) for frame, _db in keys], numpy.float64)
    levels = numpy.array([db for _frame, db in keys], numpy.float64)
    db = numpy.interp(numpy.arange(length, dtype=numpy.float64), positions, levels)
    return numpy.power(10.0, db / 20.0).astype(numpy.float32)


def tempo_chain(tempo: Fraction, length: int) -> str:
    """A retimed clip's own sound (lane w6-tempo): ``atempo`` changes its speed without changing its
    pitch, then the result is cut or padded to exactly ``length`` samples (the record length)."""
    if tempo == 1:
        return ""
    return f",atempo={float(tempo):.12g},apad=whole_len={length},atrim=end_sample={length},asetpts=N/SR/TB"


def source_samples(length: int, tempo: Fraction) -> int:
    """Source samples a retimed sound reads for ``length`` record samples (rounded up)."""
    value = Fraction(length) * tempo
    return -(-value.numerator // value.denominator)


def _source_chain(index: int, *, start: int, length: int, delay: int, gain_db: float, fade_in: int, fade_out: int,
                  mono: bool, label: str, envelope_index: int | None = None, tempo: Fraction = Fraction(1)) -> str:
    layout = "pan=stereo|c0=c0|c1=c0" if mono else "aformat=sample_fmts=fltp:channel_layouts=stereo"
    read = source_samples(length, tempo)
    chain = [f"[{index}:a:0]aresample={AUDIO_RATE}", "aformat=sample_fmts=fltp" if mono else None, layout,
             f"atrim=start_sample={start}:end_sample={start + read}", "asetpts=N/SR/TB" + tempo_chain(tempo, length)]
    if gain_db:
        chain.append(f"volume={gain_db:g}dB")
    if fade_in:
        chain.append(f"afade=t=in:ss=0:ns={fade_in}:curve=tri")
    if fade_out:
        chain.append(f"afade=t=out:ss={length - fade_out}:ns={fade_out}:curve=tri")
    if envelope_index is not None:
        head = ",".join(part for part in chain if part) + f"[{label}d]"
        keyed = (f"[{envelope_index}:a:0]aformat=sample_fmts=fltp,pan=stereo|c0=c0|c1=c0[{label}e];"
                 f"[{label}d][{label}e]amultiply,adelay=delays={delay}S:all=1[{label}]")
        return f"{head};{keyed}"
    chain.append(f"adelay=delays={delay}S:all=1")
    return ",".join(part for part in chain if part) + f"[{label}]"


def sources(plan: Plan) -> list[dict[str, Any]]:
    """Every audible sound of the cut as one mix input: path, role, sample window, gain, fades."""
    total = plan.total_frames
    solo = plan.sound_solo
    out: list[dict[str, Any]] = []
    if plan.music is not None and not solo:
        music = plan.music
        length = min(music.duration, total - music.record_start)
        out.append({"role": "music", "path": music.info.path, "start": samples(music.source_in, plan.rate),
                    "length": samples(length, plan.rate), "delay": samples(music.record_start, plan.rate),
                    "gain_db": music.gain_db, "fade_in": 0, "fade_out": 0, "mono": music.info.channels == 1, "what": "song lane"})
    if not solo:
        for clip in [*plan.storyline, *plan.overlays]:
            if not (clip.kind == "clip" and clip.enabled and clip.audio and clip.info is not None and clip.source_rate):
                continue
            if clip in plan.overlays and not plan.overlay_enabled:
                continue
            length = min(clip.duration, total - clip.record_start)
            start = Fraction(clip.source_in) / clip.source_rate * AUDIO_RATE
            out.append({"role": "dialogue", "path": clip.info.path, "start": round(start), "length": samples(length, plan.rate),
                        "delay": samples(clip.record_start, plan.rate), "gain_db": 0.0, "fade_in": 0, "fade_out": 0,
                        "mono": False, "what": f"clip sound {clip.id}",
                        "tempo": clip.retime.tempo if clip.retime is not None else Fraction(1)})
    for sound in plan.sounds:
        if sound.record_start >= total:
            continue
        length = min(sound.duration, total - sound.record_start)
        clip_length = samples(length, plan.rate)
        fade_in = min(samples(sound.fade_in, plan.rate), clip_length)
        # A clip cut at the film's end keeps its fade out where the cut puts it: the part past the end is gone.
        fade_out_end = samples(sound.duration, plan.rate)
        fade_out = samples(sound.fade_out, plan.rate)
        fade_out = max(0, min(fade_out, clip_length - fade_in)) if fade_out_end == clip_length else 0
        start = Fraction(sound.source_in) / sound.source_rate * AUDIO_RATE
        out.append({"role": sound.role, "path": sound.info.path, "start": round(start), "length": clip_length,
                    "delay": samples(sound.record_start, plan.rate), "gain_db": sound.gain_db, "fade_in": fade_in,
                    "fade_out": fade_out, "mono": sound.info.channels == 1, "what": f"{sound.track} {sound.id}",
                    "keys": sound.volume_keys, "source_rate": sound.source_rate})
    return out


def render_mix(plan: Plan, folder: Path, *, ffmpeg: Path, should_cancel: CancelFn | None = None,
               on_progress: Callable[[float], None] | None = None, music_dry: bool = False,
               target_lufs: float | None = TARGET_LUFS, ceiling_dbtp: float = TARGET_TP, lra_lu: float | None = DEFAULT_LRA) -> MixResult:
    """mix.wav + dialogue/music/effects.wav in ``folder`` (48 kHz, 24-bit, the film's samples) and the
    pass-1 loudness of the mix; ``music_dry`` adds music-dry.wav (the music without volume automation)."""
    folder.mkdir(parents=True, exist_ok=True)
    work = folder / ".envelopes"
    try:
        return _render_mix(plan, folder, work, ffmpeg=ffmpeg, should_cancel=should_cancel, on_progress=on_progress, music_dry=music_dry,
                           target_lufs=target_lufs, ceiling_dbtp=ceiling_dbtp, lra_lu=lra_lu)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_mix(plan: Plan, folder: Path, work: Path, *, ffmpeg: Path, should_cancel: CancelFn | None,
                on_progress: Callable[[float], None] | None, music_dry: bool,
                target_lufs: float | None = TARGET_LUFS, ceiling_dbtp: float = TARGET_TP, lra_lu: float | None = DEFAULT_LRA) -> MixResult:
    total = samples(plan.total_frames, plan.rate)
    inputs: list[str] = []
    graph: list[str] = []
    by_role: dict[str, list[str]] = {role: [] for role in ROLES}
    warnings: list[str] = []
    dry: list[str] = []
    count = 0
    for index, source in enumerate(sources(plan)):
        inputs += ["-i", str(source["path"])]
        source_index = count
        count += 1
        label = f"s{index}"
        envelope_index = None
        if source.get("keys"):
            work.mkdir(parents=True, exist_ok=True)
            path = work / f"{index}.f32"
            envelope(source["keys"], source["source_rate"], source["start"], source["length"]).tofile(path)
            inputs += ["-f", "f32le", "-ar", str(AUDIO_RATE), "-ac", "1", "-i", str(path)]
            envelope_index = count
            count += 1
        common = dict(start=source["start"], length=source["length"], delay=source["delay"], gain_db=source["gain_db"],
                      fade_in=source["fade_in"], fade_out=source["fade_out"], mono=source["mono"],
                      tempo=source.get("tempo", Fraction(1)))
        graph.append(_source_chain(source_index, label=label, envelope_index=envelope_index, **common))
        by_role[source["role"]].append(label)
        if music_dry and source["role"] == "music":
            graph.append(_source_chain(source_index, label=f"y{index}", **common))
            dry.append(f"y{index}")
    silent_index = count
    inputs += ["-f", "lavfi", "-i", f"anullsrc=r={AUDIO_RATE}:cl=stereo"]
    empty = [role for role in ROLES if not by_role[role]] + (["dry"] if music_dry and not dry else [])
    if empty:
        graph.append(f"[{silent_index}:a:0]atrim=end_sample={total},asetpts=N/SR/TB,asplit={len(empty)}" + "".join(f"[z{role}]" for role in empty))
    for role in ROLES:
        labels = by_role[role]
        tail = f"apad=whole_len={total},atrim=end_sample={total},asetpts=N/SR/TB,aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=2[{role}][m{role}]"
        if labels:
            mixed = "".join(f"[{label}]" for label in labels)
            graph.append(f"{mixed}amix=inputs={len(labels)}:normalize=0:duration=longest,{tail}" if len(labels) > 1 else f"{mixed}{tail}")
        else:
            graph.append(f"[z{role}]{tail}")
    graph.append("".join(f"[m{role}]" for role in ROLES) + f"amix=inputs=3:normalize=0:duration=longest,atrim=end_sample={total},asetpts=N/SR/TB[mix]")
    files = {name: folder / f"{name}.wav" for name in ("mix", *ROLES)}
    if music_dry:
        pad = f"apad=whole_len={total},atrim=end_sample={total},asetpts=N/SR/TB,aformat=sample_fmts=fltp:channel_layouts=stereo[music-dry]"
        if not dry:
            graph.append(f"[zdry]{pad}")
        elif len(dry) == 1:
            graph.append(f"[{dry[0]}]{pad}")
        else:
            graph.append("".join(f"[{label}]" for label in dry) + f"amix=inputs={len(dry)}:normalize=0:duration=longest,{pad}")
        files["music-dry"] = folder / "music-dry.wav"
    command = [str(ffmpeg), "-y", *inputs, "-filter_complex", ";".join(graph)]
    for name, path in files.items():
        command += ["-map", f"[{name}]", "-c:a", "pcm_s24le", "-ar", str(AUDIO_RATE), "-ac", "2", "-map_metadata", "-1",
                    "-fflags", "+bitexact", "-flags:a", "+bitexact", str(path)]
    run_ffmpeg(command, should_cancel=should_cancel, what="Sound mix")
    if on_progress is not None:
        on_progress(0.5)
    for name, path in files.items():
        got = _sample_count(ffmpeg, path)
        if got != total:
            raise ExportError("render_mismatch", f"{name}.wav has {got} samples, the film {total}.")
    before = measure(ffmpeg, files["mix"])
    if before["true_peak_dbtp"] is not None and before["true_peak_dbtp"] > 0:
        warnings.append(f"The mix peaks at {before['true_peak_dbtp']:+.1f} dBTP and clips in mix.wav; lower a track's gain.")
    loudnorm = None
    gain = 0.0
    if before["integrated_lufs"] is None:
        warnings.append("The mix is silent; film.mp4 is not loudness-normalised.")
    elif target_lufs is None:
        gain = ceiling_gain(before["true_peak_dbtp"], ceiling_dbtp)
    else:
        loudnorm = _loudnorm_pass1(ffmpeg, files["mix"], target_lufs, ceiling_dbtp - CEILING_MARGIN, lra_lu)
    return MixResult(files, before, loudnorm, warnings, target_lufs, ceiling_dbtp, gain, lra_lu)


def ceiling_gain(true_peak: float | None, ceiling_dbtp: float) -> float:
    """The one gain (dB, never above 0) that puts a measured true peak CEILING_MARGIN under the ceiling."""
    if true_peak is None:
        return 0.0
    return round(min(0.0, ceiling_dbtp - CEILING_MARGIN - true_peak), 2)


def _lra(lra_lu: float | None) -> float:
    return LOUDNORM_LRA if lra_lu is None else float(lra_lu)


def loudnorm_filter(measured: dict[str, str], target_lufs: float = TARGET_LUFS, true_peak: float = LOUDNORM_TP,
                    lra_lu: float | None = DEFAULT_LRA) -> str:
    return (f"loudnorm=I={target_lufs:g}:TP={true_peak:g}:LRA={_lra(lra_lu):g}:measured_I={measured['input_i']}:"
            f"measured_TP={measured['input_tp']}:measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}:linear=true:print_format=json")


def _loudnorm_pass1(ffmpeg: Path, path: Path, target_lufs: float = TARGET_LUFS, true_peak: float = LOUDNORM_TP,
                    lra_lu: float | None = DEFAULT_LRA) -> dict[str, str]:
    text = _stderr(ffmpeg, path, f"loudnorm=I={target_lufs:g}:TP={true_peak:g}:LRA={_lra(lra_lu):g}:print_format=json")
    start, end = text.rfind("{"), text.rfind("}")
    try:
        values = json.loads(text[start:end + 1])
    except ValueError:
        raise ExportError("loudness_failed", "The loudness of the mix could not be measured.") from None
    keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    if any(not isinstance(values.get(k), str) or not re.fullmatch(r"-?\d+(\.\d+)?", values[k]) for k in keys):
        raise ExportError("loudness_failed", "The loudness of the mix could not be measured.")
    return {k: values[k] for k in keys}


def normalization_type(stderr: str) -> str | None:
    match = re.findall(r'"normalization_type"\s*:\s*"(\w+)"', stderr)
    return match[-1] if match else None


def _stderr(ffmpeg: Path, path: Path, chain: str) -> str:
    try:
        done = subprocess.run([str(ffmpeg), "-hide_banner", "-nostats", "-i", str(path), "-map", "0:a:0", "-af", chain, "-f", "null", "-"],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800, check=False, env=child_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExportError("loudness_failed", "ffmpeg could not measure the sound.") from exc
    if done.returncode != 0:
        raise ExportError("loudness_failed", "ffmpeg could not measure the sound.")
    return done.stderr or ""


def _value(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text)
    if match is None or match.group(1) == "-inf":
        return None
    return round(float(match.group(1)), 1)


def measure(ffmpeg: Path, path: Path) -> dict[str, float | None]:
    """EBU R128 of the first audio stream: integrated LUFS, true peak dBTP, LRA (ebur128's summary)."""
    text = _stderr(ffmpeg, path, "ebur128=peak=true:framelog=quiet")
    found = _SUMMARY.search(text)
    summary = found.group(1) if found else ""
    lufs = _value(r"I:\s+(-?[\d.]+|-inf) LUFS", summary)
    return {"integrated_lufs": None if lufs is None or lufs <= -70.0 else lufs,
            "true_peak_dbtp": _value(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary),
            "lra_lu": _value(r"LRA:\s+(-?[\d.]+|-inf) LU\b", summary)}


def _sample_count(ffmpeg: Path, path: Path) -> int:
    text = _stderr(ffmpeg, path, "astats=measure_perchannel=none:measure_overall=Number_of_samples")
    found = re.findall(r"Number of samples:\s+(\d+)", text)
    return int(found[-1]) if found else 0


def report(mix: MixResult, after: dict[str, float | None] | None, applied: str | None) -> dict[str, Any]:
    return {"target": {"integrated_lufs": mix.target_lufs, "true_peak_dbtp": mix.ceiling_dbtp},
            "before": dict(mix.loudness_before), "after": after, "normalization": applied or "none", "lra_target_lu": mix.lra_lu}


def normalise_file(ffmpeg: Path, source: Path, target: Path, *, target_lufs: float | None, ceiling_dbtp: float,
                   audio_args: list[str], total_samples: int, should_cancel: CancelFn | None = None,
                   lra_lu: float | None = DEFAULT_LRA) -> dict[str, Any]:
    """Delivery loudness on a rendered film whose sound is not a sound-track mix: measure its audio, then
    write ``target`` with the picture copied and the audio through two-pass loudnorm to ``target_lufs`` (or,
    without a target, the one gain under the ceiling). Answers before + the normalization applied;
    ``written`` is False when nothing had to change (a silent film, or no target and already under)."""
    before = measure(ffmpeg, source)
    if before["integrated_lufs"] is None:
        return {"before": before, "normalization": "none", "written": False, "warning": "The film's sound is silent; it is not loudness-normalised."}
    if target_lufs is None:
        gain = ceiling_gain(before["true_peak_dbtp"], ceiling_dbtp)
        if not gain:
            return {"before": before, "normalization": "none", "written": False}
        chain, applied = f"volume={gain:g}dB", f"gain {gain:+g} dB (true-peak ceiling)"
    else:
        tp = ceiling_dbtp - CEILING_MARGIN
        chain = loudnorm_filter(_loudnorm_pass1(ffmpeg, source, target_lufs, tp, lra_lu), target_lufs, tp, lra_lu) + f",aresample={AUDIO_RATE}"
        applied = "loudnorm two-pass"
    run_ffmpeg([str(ffmpeg), "-y", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                "-af", f"{chain},apad=whole_len={total_samples},atrim=end_sample={total_samples},asetpts=N/SR/TB",
                *audio_args, "-ar", str(AUDIO_RATE), "-ac", "2", "-movflags", "+faststart", "-map_metadata", "0", str(target)],
               should_cancel=should_cancel, what="Loudness")
    return {"before": before, "normalization": applied, "written": True}
