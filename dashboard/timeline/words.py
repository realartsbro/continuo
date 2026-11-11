"""What an edit is called, in the editor's words (lane w39-words).

One phrase per timeline op, the word the artist's own edits already use for the same action (Blade, Swap take, Move
marker, Fit to beats ...) or, where the page has none, the one Resolve / Premiere use. An agent write without its own
label is labelled from these phrases; History maps a label stored before this table through it when shown.
The renderer twin (src/timeline/ops/timeline-words.js, there `OP_WORDS`) holds the same table;
tests/backend/test_w39_words.py checks both.

The table is named OP_PHRASES, not *WORDS: its values are names of edits (History, the "Hermes did" line), never a
refusal or error text, so the no-instruction gate's message-table rule (tests/backend/test_no_instruction_texts.py,
names with ERRORS / MESSAGES / WORDS ...) does not apply to it; that test also pins that no phrase is ever used as one.
"""
from __future__ import annotations

OP_PHRASES: dict[str, str] = {
    "add_audio_track": "Add sound track",
    "add_hold": "Add hold",
    "add_marker": "Add marker",
    "add_speed_key": "Add speed key",
    "add_timeline": "New cut",
    "add_transition": "Add transition",
    "append": "Add to end",
    "auto_duck": "Duck music",
    "clear_volume_keys": "Clear volume keys",
    "delete": "Delete",
    "delete_timeline": "Delete cut",
    "duplicate_timeline": "Duplicate cut",
    "extract_range": "Extract IN–OUT",
    "fit_to_beats": "Fit to beats",
    "fit_to_fill": "Fit to fill",
    "freeze_frame": "Freeze frame",
    "insert": "Insert",
    "lift_range": "Lift IN–OUT",
    "link_sound": "Detach sound",
    "move": "Move",
    "move_marker": "Move marker",
    "move_speed_key": "Move speed key",
    "overwrite": "Place on track",
    "place_audio": "Place sound",
    "place_music": "Place song",
    "place_with_parent": "Place continuation",
    "put_slot": "Update shot takes",
    "remove_audio_track": "Remove sound track",
    "remove_marker": "Remove marker",
    "remove_music": "Remove song",
    "remove_speed_key": "Remove speed key",
    "remove_transition": "Remove transition",
    "remove_volume_key": "Remove volume key",
    "rename_timeline": "Rename cut",
    "roll": "Roll cut",
    "set_active": "Show cut",
    "set_audio": "Sound on/off",
    "set_enabled": "Clip on/off",
    "set_fades": "Fade",
    "set_format": "Format",
    "set_gain": "Gain",
    "set_label": "Rename clip",
    "set_marker": "Edit marker",
    "set_retime": "Speed",
    "set_speed_curve": "Speed",
    "set_track": "Track settings",
    "set_transition": "Change transition",
    "set_volume_key": "Volume key",
    "slide": "Slide",
    "slip": "Slip",
    "snap_speed_key": "Snap speed key",
    "split": "Blade",
    "swap_take": "Swap take",
    "trim": "Trim",
    "trim_to_picture": "Trim song to picture",
    "unlink_sound": "Unlink sound",
}


def phrase(op: str) -> str:
    """The editor's word for one op; an unknown name reads as words, never with underscores."""
    word = OP_PHRASES.get(op)
    if word:
        return word
    text = str(op).replace("_", " ").strip()
    return text[:1].upper() + text[1:]


LABEL_MAX = 120
SHOWN = 3


def phrases_of(names: list[str]) -> list[str]:
    """Distinct phrases in the order done."""
    return list(dict.fromkeys(phrase(n) for n in names))


def more_edits(n: int) -> str:
    """The count of edits a label does not name, saying what it counts (lane w39-p2-detail, rule 5): " +1 edit"."""
    return f" +{n} edit{'' if n == 1 else 's'}" if n > 0 else ""


def label_for(ops: list[dict], by: str) -> str:
    """The revision label of a write without its own label: the first three distinct phrases, then "+N edits";
    "Hermes: " before it when Hermes wrote it. Whole phrases only, never cut, within LABEL_MAX."""
    said = phrases_of([op.get("op", "") for op in ops])
    head = "Hermes: " if by == "hermes" else ""
    shown = SHOWN
    while True:
        rest = len(said) - shown
        label = head + ", ".join(said[:shown]) + more_edits(rest)
        if len(label) <= LABEL_MAX or shown == 1:
            return label[:LABEL_MAX]
        shown -= 1
