"""The Qwen prompt-enhancer guard, shared by the area-edit route and the catalog targets (no imports, no I/O).

Enhancer guard (p11-imgb, maintainer live proof): the enhancer's rewrite (cleaned by RegexReplace 516)
is sometimes token soup ("a a the a the ...", 6 of 29 runs, all Restyle; the seed is fixed, so a given
picture + text collapses every time) or grounding JSON ("bbox_2d"). With the enhancer on, the compiled
graph gains two NATIVE nodes (the pinned graph and its sha stay unchanged): RegexMatch (re.search,
case-insensitive + dotall) on the cleaned text, and a lazy ComfySwitchNode that passes that text when it
matches, else the plain instruction; the enhancer switch's on_true points at the guard. The pattern
refuses grounding JSON, 8 article-like tokens in a row and texts with fewer than 6 words of 4+ letters;
checked by the owner on all 29 stored enhancer outputs (29 agree) and timed on 30 000 characters.

Loaded as a package module by ``qwen_image_edit_adapter`` and by file path by ``qwen_image_workflow_adapter``
(which the host may load unregistered), so this file imports nothing.
"""
from __future__ import annotations

from typing import Any

PATTERN = (
    r'^(?!.*bbox_2d)(?!.*(?:\b(?:a|an|the|by)\b[\s,."]*){8})(?=(?:.*?\b[A-Za-z]{4,}\b){6})'
)
MATCH_NODE_ID = "911"
SWITCH_NODE_ID = "912"


def insert(compiled: dict[str, Any], switch_node: str, error: type[Exception]) -> None:
    """Route ``switch_node``'s on_true (the cleaned enhancer text) through RegexMatch + ComfySwitchNode, with
    its on_false (the plain instruction) as the fallback. In place; raises ``error`` on a changed insertion point."""
    switch = compiled.get(switch_node)
    if not isinstance(switch, dict) or switch.get("class_type") != "ComfySwitchNode":
        raise error("enhancer guard insertion point changed")
    cleaned, plain = switch["inputs"].get("on_true"), switch["inputs"].get("on_false")
    if not (isinstance(cleaned, list) and len(cleaned) == 2 and isinstance(plain, list) and len(plain) == 2):
        raise error("enhancer guard insertion point changed")
    if MATCH_NODE_ID in compiled or SWITCH_NODE_ID in compiled:
        raise error("enhancer guard node id taken")
    compiled[MATCH_NODE_ID] = {"class_type": "RegexMatch", "inputs": {
        "string": list(cleaned), "regex_pattern": PATTERN,
        "case_insensitive": True, "multiline": False, "dotall": True}}
    compiled[SWITCH_NODE_ID] = {"class_type": "ComfySwitchNode", "inputs": {
        "switch": [MATCH_NODE_ID, 0], "on_true": list(cleaned), "on_false": list(plain)}}
    switch["inputs"]["on_true"] = [SWITCH_NODE_ID, 0]
