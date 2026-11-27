"""Calibrated thresholds for honest tempo / meter status (lane fs-beat, 2026-10-20).

Every constant carries the measurement it was chosen from.  Change a value only with a new
measurement on held-out data, and update the numbers here in the same commit.
"""

from __future__ import annotations

CALIBRATION_VERSION = "fs-beat-2026-10-20"

# ---- Beat This! (final0, minimal decoder): calibrated on GTZAN, held out from final0 ----
# Local tempo = 60 / running median of 16 inter-beat intervals (8 each side), first and
# last 8 beats trimmed.  "variable" if its IQR exceeds this share of its median.
# GTZAN n=993: 104 tracks above 4 % (tempo Acc1 0.481, CMLt 0.472) vs 889 below
# (Acc1 0.86x).  Synthetic: the 90->140 ramp 17.96 %, the 100->130 step 23.3 %, the
# 4/4->3/4 change 48.1 %; steady 120/96/174/72 BPM 0.0-2.9 %.
TEMPO_VARIABLE_IQR_PCT = 4.0
LOCAL_TEMPO_HALF_WINDOW_BEATS = 8
# "ambiguous" when a double-time rival is strong: the mean beat probability at the
# midpoints between beats is >= this share of the mean at the beats; or when the beats
# themselves are weak: the 10th percentile of beat probability is below WEAK.
# GTZAN: 22 tracks ambiguous (Acc1 0.227, Acc2 0.591); 867 fixed (Acc1 0.881, Acc2 0.990).
TEMPO_DOUBLE_RIVAL_RATIO = 0.4
TEMPO_WEAK_BEAT_P10 = 0.6
# Reported confidences are the measured tempo Acc1 of each status class on GTZAN, i.e.
# "tracks that got this status had the right tempo this often".
TEMPO_CONFIDENCE = {"fixed": 0.881, "variable": 0.481, "ambiguous": 0.227}
# Meter: the downbeat probability at every beat folded over 3 and 4 beats (best phase);
# 2 and 6 are never chosen (allowing them lowered GTZAN accuracy, 0.942-0.952 vs 0.953).
# "fixed" when the 3-vs-4 fold margin >= MARGIN and >= AGREEMENT of the decoded bars have
# that length.  GTZAN n=990: 745 fixed at precision 0.980; 245 ambiguous (their folded
# value is still right 0.837 of the time and is kept as the candidate).
METER_FOLD_CHOICES = (3, 4)
METER_FOLD_MARGIN_MIN = 0.05
METER_BAR_AGREEMENT_MIN = 0.6
METER_CONFIDENCE = {"fixed": 0.98, "ambiguous": 0.837}

# ---- librosa fallback ("librosa_v6"): calibrated on Ballroom (n=144) + synthetic (n=7) ----
# Same local-tempo IQR rule on the librosa beats.  Ballroom: 16 tracks above 4 % with
# Acc1 0.375 vs 0.656 below; synthetic ramp 13.7 %, step 5.3 %.
FALLBACK_TEMPO_VARIABLE_IQR_PCT = 4.0
# Octave-family margin of the comb filter below this -> "ambiguous".
# Ballroom: 43 tracks below 0.2 with Acc1 0.326 vs 101 at/above with Acc1 0.752.
# Synthetic: the 174 BPM DnB read as 86.97 has margin 0.074 (caught).
FALLBACK_OCTAVE_MARGIN_MIN = 0.2
# Meter on the fallback is never "fixed": on Ballroom no confidence threshold reaches
# useful precision (>= 0.50: 79 calls, precision 0.595; >= 0.60: 36, 0.500; >= 0.65: 12,
# 0.250; the most confident call, 0.785, says 7 for a waltz).  The detected value is kept
# as an "ambiguous" candidate and only {2, 3, 4, 6} may be offered at all.
FALLBACK_METER_FIXED_MIN_CONFIDENCE = None
FALLBACK_METER_ALLOWED = (2, 3, 4, 6)

CALIBRATION = {
    "beat_this": {
        "dataset": "GTZAN 999/1000 clips (held out from final0), beat_this_annotations labels; synthetic n=7",
        "tempo_status_counts": {"fixed": 867, "ambiguous": 22, "variable": 104},
        "meter_fixed_precision": 0.980,
        "meter_fixed_share": "745/990",
    },
    "librosa_v6": {
        "dataset": "Ballroom n=144 (CPJKU annotations; 35 waltzes) + synthetic n=7",
        "octave_margin_split": {"below": [43, 0.326], "at_or_above": [101, 0.752]},
        "meter_precision_by_confidence": {"0.50": [79, 0.595], "0.60": [36, 0.500], "0.65": [12, 0.250]},
    },
}
