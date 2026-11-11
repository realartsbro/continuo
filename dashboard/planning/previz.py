"""studio.previz/1: one previz set per scene (lane w20-plan2, Previz stage 1; planning board round 2 panels 6a/6b/6c).

Contract 2 (owner steer, architecture review): the set holds shared geometry and figure INSTANCES plus sparse per-shot
overrides of a figure's position, turn and pose. It never holds a camera: the shot record (shot_plan.py) is the only
camera authority. Units metres, y up, right-handed; a figure stands on the floor at (x, z), ``yaw`` in degrees
(0 = facing +z, toward the default camera), pose ``stand`` | ``sit``.

Document: {schema, revision, scenes {s-xxxxxxxx: {floor {w, d}, boxes [{id bx-, name, pos [x, y, z] (centre),
size [w, h, d], yaw}], figures [{id fig-, name, element el-?|null, version v-?|null, pos [x, z], yaw, pose}],
overrides {sh-: {fig-: {pos?, yaw?, pose?}}}, kept {sh-: {item, sig}}}}, proposals [{id pr-, by, scene, label, at,
state open|undone|dismissed, before {set, cameras {sh-: camera|null}}, after {...}}]}.

Contract 3: a Hermes change is ONE proposal with its before/after delta (the scene's set and every shot camera it
touched); ``undo_check`` restores only while every touched value still equals ``after`` (else proposal_conflict).
Pure; the route unit owns the files, the lock and the shot record.
"""
from __future__ import annotations

import copy
import math
import re
import secrets
from typing import Any

SCHEMA = "studio.previz/1"
SCENE_ID_RE = re.compile(r"^s-[0-9a-f]{8}$")
SHOT_ID_RE = re.compile(r"^sh-[0-9a-f]{8}$")
FIG_RE = re.compile(r"^fig-[0-9a-f]{8}$")
BOX_RE = re.compile(r"^bx-[0-9a-f]{8}$")
ELEMENT_ID_RE = re.compile(r"^el-[0-9a-f]{8}$")
VERSION_RE = re.compile(r"^v-[0-9a-f]{8}$")
ITEM_RE = re.compile(r"^(imgjob_[0-9a-f]{32}|file:[0-9a-f]{64})$")
KEPT_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,3})?Z$")
POSES = ("stand", "sit", "walk", "coil", "lean")  # w23-pvlook: walk (FrameForge / Cine Tracer posable actors; a stride for blocking); w39-p2-previz2: coil (hands at the waist on a coil of rope, weight on one leg), lean (a hand out on a rail)
# w23-pvlook (study: FrameForge sun/key, Cine Tracer time of day, Blender passepartout): the set's light (auto = from the
# scene heading's INT/EXT and time of day; or a fixed time) and the passepartout's opacity outside the frame (0-1).
LIGHTS = ("auto", "day", "dawn", "dusk", "night")
WORLD = 100.0
MAX_BOXES, MAX_FIGURES, MAX_PROPOSALS = 200, 40, 20
CAMERA_WORDS = {"camera", "cameras", "lens", "lens_mm", "start", "end", "at"}


class PrevizRefusal(Exception):
    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code, self.message = reason_code, message


def empty() -> dict[str, Any]:
    return {"schema": SCHEMA, "revision": 0, "scenes": {}, "proposals": []}


def valid_document(doc: Any) -> bool:
    return isinstance(doc, dict) and doc.get("schema") == SCHEMA and isinstance(doc.get("scenes"), dict)


def default_set() -> dict[str, Any]:
    return {"floor": {"w": 8.0, "d": 8.0}, "boxes": [], "figures": [], "overrides": {}, "kept": {}}


def scene_of(doc: dict[str, Any], scene: str) -> dict[str, Any]:
    return copy.deepcopy(doc["scenes"].get(scene) or default_set())


def _num(v: Any, lo: float, hi: float, where: str) -> float:
    if type(v) not in (int, float) or not math.isfinite(v) or not lo <= v <= hi:
        raise PrevizRefusal("set_invalid", f"{where} is a number from {lo} to {hi}.")
    return float(v)


def _vec(v: Any, n: int, where: str, lo: float = -WORLD, hi: float = WORLD) -> list[float]:
    if not isinstance(v, list) or len(v) != n:
        raise PrevizRefusal("set_invalid", f"{where} is a list of {n} numbers (metres).")
    return [_num(x, lo, hi, where) for x in v]


def _name(v: Any, where: str) -> str:
    if not isinstance(v, str) or len(v) > 60 or any(ord(c) < 32 for c in v):
        raise PrevizRefusal("set_invalid", f"{where} is one line of at most 60 characters.")
    return " ".join(v.split())


def _no_camera(raw: dict[str, Any], where: str) -> None:
    hit = CAMERA_WORDS & set(raw)
    if hit:
        raise PrevizRefusal("camera_not_here", f"{where} holds no camera ({sorted(hit)[0]}): a shot's camera lives in its shot record (continuo_previz_camera).")


def _figure(raw: Any, old: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PrevizRefusal("figure_invalid", "A figure is an object.")
    _no_camera(raw, "A figure")
    if set(raw) - {"id", "name", "element", "version", "pos", "yaw", "pose"}:
        raise PrevizRefusal("figure_invalid", "A figure is {id?, name, element?, version?, pos [x, z], yaw, pose}.")
    fig = dict(old or {"id": "fig-" + secrets.token_hex(4), "name": "", "element": None, "version": None, "pos": [0.0, 0.0], "yaw": 0.0, "pose": "stand"})
    if "id" in raw and raw["id"] is not None:
        if not isinstance(raw["id"], str) or not FIG_RE.match(raw["id"]):
            raise PrevizRefusal("figure_invalid", "A figure id is fig-<8 hex>.")
        fig["id"] = raw["id"]
    if "name" in raw:
        fig["name"] = _name(raw["name"], "figure.name")
    for key, rx in (("element", ELEMENT_ID_RE), ("version", VERSION_RE)):
        if key in raw:
            if raw[key] is not None and (not isinstance(raw[key], str) or not rx.match(raw[key])):
                raise PrevizRefusal("figure_invalid", f"figure.{key} is an id from continuo_elements_read or null.")
            fig[key] = raw[key]
    if "pos" in raw:
        fig["pos"] = _vec(raw["pos"], 2, "figure.pos [x, z]")
    if "yaw" in raw:
        fig["yaw"] = _num(raw["yaw"], -360, 360, "figure.yaw (degrees)")
    if "pose" in raw:
        if raw["pose"] not in POSES:
            raise PrevizRefusal("figure_invalid", "figure.pose is stand, sit, walk, coil or lean.")
        fig["pose"] = raw["pose"]
    return fig


def _box(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PrevizRefusal("box_invalid", "A box is an object.")
    _no_camera(raw, "A box")
    if set(raw) - {"id", "name", "pos", "size", "yaw"}:
        raise PrevizRefusal("box_invalid", "A box is {id?, name, pos [x, y, z], size [w, h, d], yaw}.")
    bid = raw.get("id") or "bx-" + secrets.token_hex(4)
    if not isinstance(bid, str) or not BOX_RE.match(bid):
        raise PrevizRefusal("box_invalid", "A box id is bx-<8 hex>.")
    return {"id": bid, "name": _name(raw.get("name", "Box"), "box.name"), "pos": _vec(raw.get("pos", [0, 0.5, 0]), 3, "box.pos"),
            "size": _vec(raw.get("size", [1, 1, 1]), 3, "box.size", 0.01, 100), "yaw": _num(raw.get("yaw", 0), -360, 360, "box.yaw")}


def _override(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {"pos", "yaw", "pose"}:
        raise PrevizRefusal("override_invalid", "A per-shot override is {pos?, yaw?, pose?}.")
    out: dict[str, Any] = {}
    if "pos" in raw:
        out["pos"] = _vec(raw["pos"], 2, "override.pos [x, z]")
    if "yaw" in raw:
        out["yaw"] = _num(raw["yaw"], -360, 360, "override.yaw")
    if "pose" in raw:
        if raw["pose"] not in POSES:
            raise PrevizRefusal("override_invalid", "override.pose is stand, sit, walk, coil or lean.")
        out["pose"] = raw["pose"]
    return out


def apply_set(doc: dict[str, Any], scene: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Validate and apply a set patch {floor?, boxes? (full list), figures? (full list), overrides? {shot: {fig: o|null}},
    kept? {shot: {item, sig}|null}} to one scene; returns the new set (nothing stored on a refusal)."""
    if not isinstance(scene, str) or not SCENE_ID_RE.match(scene):
        raise PrevizRefusal("scene_invalid", "A scene id is s-<8 hex> (continuo_script_read).")
    if not isinstance(patch, dict):
        raise PrevizRefusal("set_invalid", "The set patch is an object.")
    _no_camera(patch, "The set")
    if set(patch) - {"floor", "boxes", "figures", "overrides", "kept", "light", "matte"}:
        raise PrevizRefusal("set_invalid", "The set is {floor?, boxes?, figures?, overrides?, kept?, light?, matte?}.")
    s = scene_of(doc, scene)
    if "light" in patch:
        if patch["light"] not in LIGHTS:
            raise PrevizRefusal("set_invalid", "light is auto (from the scene heading), day, dawn, dusk or night.")
        s["light"] = patch["light"]
    if "matte" in patch:
        s["matte"] = round(_num(patch["matte"], 0, 1, "matte (the passepartout's opacity)"), 3)
    if "floor" in patch:
        f = patch["floor"]
        if not isinstance(f, dict) or set(f) != {"w", "d"}:
            raise PrevizRefusal("set_invalid", "floor is {w, d} in metres.")
        s["floor"] = {"w": _num(f["w"], 1, 200, "floor.w"), "d": _num(f["d"], 1, 200, "floor.d")}
    if "boxes" in patch:
        if not isinstance(patch["boxes"], list) or len(patch["boxes"]) > MAX_BOXES:
            raise PrevizRefusal("box_invalid", f"boxes is a list of at most {MAX_BOXES}.")
        s["boxes"] = [_box(b) for b in patch["boxes"]]
    if "figures" in patch:
        if not isinstance(patch["figures"], list) or len(patch["figures"]) > MAX_FIGURES:
            raise PrevizRefusal("figure_invalid", f"figures is a list of at most {MAX_FIGURES}.")
        old = {f["id"]: f for f in s["figures"]}
        s["figures"] = [_figure(f, old.get(f.get("id")) if isinstance(f, dict) else None) for f in patch["figures"]]
    ids = [f["id"] for f in s["figures"]] + [b["id"] for b in s["boxes"]]
    if len(set(ids)) != len(ids):
        raise PrevizRefusal("set_invalid", "Every figure and box appears once.")
    if "overrides" in patch:
        if not isinstance(patch["overrides"], dict):
            raise PrevizRefusal("override_invalid", "overrides is {shot: {figure: override|null}}.")
        for shot, per in patch["overrides"].items():
            if not SHOT_ID_RE.match(str(shot)) or not isinstance(per, dict):
                raise PrevizRefusal("override_invalid", "overrides is {sh-…: {fig-…: override|null}}.")
            slot = s["overrides"].setdefault(shot, {})
            for fig, o in per.items():
                if not FIG_RE.match(str(fig)):
                    raise PrevizRefusal("override_invalid", "An override names a figure id fig-<8 hex>.")
                if o is None:
                    slot.pop(fig, None)
                else:
                    slot[fig] = _override(o)
            if not slot:
                s["overrides"].pop(shot, None)
    if "kept" in patch:
        if not isinstance(patch["kept"], dict):
            raise PrevizRefusal("set_invalid", "kept is {shot: {item, sig}|null}.")
        for shot, k in patch["kept"].items():
            if not SHOT_ID_RE.match(str(shot)):
                raise PrevizRefusal("set_invalid", "kept names shot ids.")
            if k is None:
                s["kept"].pop(shot, None)
            elif not isinstance(k, dict) or set(k) - {"item", "sig", "at"} or not ITEM_RE.match(str(k.get("item")))                     or not re.fullmatch(r"[0-9a-f]{8,64}", str(k.get("sig"))) or ("at" in k and not KEPT_AT_RE.match(str(k["at"]))):
                raise PrevizRefusal("set_invalid", "kept is {item: a Media item id, sig: hex, at?: UTC time}.")
            else:
                # w21-pv item 6: ``at`` (when the frame was kept) feeds the footer's "Previz frame of 1.2 · 11:42".
                s["kept"][shot] = {"item": k["item"], "sig": k["sig"], **({"at": k["at"]} if "at" in k else {})}
    known = {f["id"] for f in s["figures"]}
    for shot in list(s["overrides"]):
        s["overrides"][shot] = {f: o for f, o in s["overrides"][shot].items() if f in known}
        if not s["overrides"][shot]:
            del s["overrides"][shot]
    return s


# --- blocking (Hermes' continuo_previz_block_scene): a starting set and cameras from the script -----------------------
DISTANCE = {"ECU": 0.45, "CU": 0.8, "MCU": 1.15, "MCS": 1.5, "MS": 1.9, "MWS": 2.8, "FS": 3.4, "WS": 5.2, "EWS": 9.0, "INSERT": 0.55}
# Lane w22-pv item 3: a close size on a named figure is framed like the page's defaultCamera (previz-eval.js SPAN, the
# usual size chart): the span in metres from the top of the head down, shown by the lens' vertical field of view on a
# 36 mm 16:9 sensor; CU = head 0.62 of the frame, chin above the bottom edge (the distance table read a CU as an MCU).
SPAN = {"ECU": 0.18, "CU": 0.37, "MCU": 0.56, "INSERT": 0.34, "MCS": 0.72, "MS": 0.9, "MWS": 1.3}
_TOP = {"stand": 1.735, "sit": 1.415, "walk": 1.735, "coil": 1.715, "lean": 1.725}
# Lane w23-pvlook (d), the same table as previz-eval.js HEADROOM (owner read: the 85 mm CU had no headroom): the air above
# the head as a share of the frame height per size.
HEADROOM = {"CU": 0.08, "MCU": 0.1, "MCS": 0.1, "MS": 0.1, "MWS": 0.1}


def _framed(size: str | None, lens: float, figure: dict[str, Any]) -> tuple[float, float] | None:
    """(distance, aim height) that frames ``figure`` at ``size`` with ``lens``; None for sizes the table does not fix."""
    if size not in SPAN:
        return None
    top = _TOP.get(figure.get("pose") or "stand", _TOP["stand"])
    span = min(SPAN[size], top + 0.15)
    vfov = 2 * math.atan((36 / (16 / 9)) / 2 / lens)
    if size in HEADROOM:  # w23-pvlook (d): the top of the head sits HEADROOM of the frame height below its top edge
        return span / (2 * math.tan(vfov / 2)), max(top * 0.45, top - span * (0.5 - HEADROOM[size]))
    return span / (2 * math.tan(vfov / 2)), max(top * 0.45, top + span * 0.06 - span / 2)


def _fig_pos(i: int, n: int) -> list[float]:
    if n == 1:
        return [0.0, 0.0]
    span = min(3.0, 1.2 * (n - 1))
    x = -span / 2 + span * i / (n - 1)
    return [round(x, 3), round(-0.3 * abs(x), 3)]


def _facing(fig: dict[str, Any], figures: list[dict[str, Any]]) -> float:
    """Yaw (degrees, 0 faces +z) toward the other figures' centre, cheated 30 degrees toward the room's front."""
    others = [f for f in figures if f is not fig]
    cx = sum(f["pos"][0] for f in others) / len(others) - fig["pos"][0]
    cz = sum(f["pos"][1] for f in others) / len(others) - fig["pos"][1]
    a = math.degrees(math.atan2(cx, cz))
    a -= math.copysign(min(30.0, abs(a)), a)
    return round(a, 1)


def gaze(fig: dict[str, Any], figures: list[dict[str, Any]]) -> float:
    """Where a figure looks (yaw, degrees): at the nearest other figure within 6 m, the head turned at most 55 degrees
    from the body (the renderer's turnHead, previz-render.js); else where the body faces."""
    others = sorted((math.hypot(f["pos"][0] - fig["pos"][0], f["pos"][1] - fig["pos"][1]), i) for i, f in enumerate(figures) if f is not fig)
    yaw = float(fig.get("yaw") or 0)
    if not others or others[0][0] >= 6:
        return yaw
    o = figures[others[0][1]]
    want = math.degrees(math.atan2(o["pos"][0] - fig["pos"][0], o["pos"][1] - fig["pos"][1]))
    d = (want - yaw + 540) % 360 - 180
    return yaw + max(-55.0, min(55.0, d))


def _lead(target: dict[str, Any], figures: list[dict[str, Any]], size: str | None, lens: float, dist: float) -> float:
    """Sideways aim offset (metres along +x) for a medium single on ``target`` with a partner within 4 m: the aim moves
    toward the partner by 0.18 of the frame width, so the subject sits on the third away from the partner (it looks
    into the frame) and the partner's near shoulder enters the frame edge. Close sizes and lone figures: 0."""
    if size not in ("MCS", "MS", "MWS", "FS"):
        return 0.0
    near = sorted((abs(f["pos"][0] - target["pos"][0]), f["pos"][0] - target["pos"][0]) for f in figures
                  if f is not target and math.hypot(f["pos"][0] - target["pos"][0], f["pos"][1] - target["pos"][1]) <= 4.0)
    if not near or near[0][0] < 0.2:
        return 0.0
    return math.copysign(0.18 * dist * 36 / lens, near[0][1])


# --- the set kit (lane w39-p2-previz; study docs/studies/w39-previz-STUDY.md: FrameForge / Cine Tracer libraries, the
# Tein open-source previs' props.ts): a set piece is a box whose NAME picks a piece the renderer builds in code
# (previz-kit.js), scaled to the box. The words below are the kit; any other name stays a plain box. Order matters:
# the first word found in the name wins ("Ferry hull" is a hull, "Cabin lamp" a lamp).
KIT = (
    ("hull", ("hull",)), ("lamp", ("lamp", "lantern")), ("lighthouse", ("lighthouse",)), ("ferry", ("ferry", "boat")),
    ("pier", ("pier", "jetty", "dock")), ("water", ("water", "sea")), ("shore", ("shore",)), ("rocks", ("rocks", "rock")),
    ("cabin", ("cabin", "room")), ("bollard", ("bollard",)), ("rope", ("rope",)), ("suitcase", ("suitcase",)),
    ("crate", ("crate",)), ("bench", ("bench",)), ("chair", ("chair",)), ("table", ("table",)), ("console", ("console", "desk")),
)
KIT_NAMES = tuple(k for k, _ in KIT)


def kit_of(name: str) -> str | None:
    """The kit piece a box name picks (None: a plain box)."""
    words = re.findall(r"[a-z]+", str(name).lower())
    for kind, keys in KIT:
        if any(k in words for k in keys):
            return kind
    return None


def _b(name: str, pos: list[float], size: list[float], yaw: float = 0) -> dict[str, Any]:
    return {"name": name, "pos": pos, "size": size, "yaw": yaw}


# Starting sets by the scene heading's place (EXT/INT and the words of the location); metres, the figures stand around
# the origin on y = 0, the cameras look from +z. A heading without a known place keeps the old start (a back wall, and
# a table indoors).
_SEA = re.compile(r"\b(HARBOU?R|PIER|DOCKS?|PORT|QUAY|JETTY|MARINA|WHARF)\b")
_BOAT = re.compile(r"\b(FERRY|BOAT|SHIP|YACHT|TRAWLER)\b")
_LIGHTHOUSE = re.compile(r"\bLIGHTHOUSE\b")
_CABIN = re.compile(r"\b(CABIN|WHEELHOUSE|BRIDGE)\b")


def starting_set(heading: str) -> tuple[list[dict[str, Any]], dict[str, float] | None]:
    """(boxes, floor or None) for a scene heading."""
    h = heading.upper()
    interior = h.lstrip().startswith(("INT", "I/E"))
    if interior and (_CABIN.search(h) or _BOAT.search(h)):
        # w39-p2-previz2 (bench 3.1: \"does not read as a cabin, the lamp emits nothing\"): the lamp hangs over the console
        # (the helm under the forward windows), a bench along the side wall, a table by it
        # w39-p2-previz3 (bench 3.1: the lamp hung 0.2 m over the helmsman's crown, so no insert of it could leave his
        # head out, and an 85 mm insert at 2 m holds 0.5 m: a lamp at 1.95 m and the light it throws never shared it):
        # a low pendant over the cabin table, its shade 0.35 m over the top, so the insert holds the lamp and its pool
        # w39-p2-previz4 (sheet 3.1: "a lamp over an empty slab, no subject"; the script: "A single lamp. The ferryman turns
        # a brass key."): the pendant hangs over the console's key, so the insert holds the lamp, the helmsman's hand on
        # the key and the console it lights
        boxes = [_b("Cabin", [0, 1.2, 0], [4.4, 2.4, 3.4]), _b("Cabin lamp", [-0.45, 1.5, -1.3], [0.26, 0.26, 0.26]),
                 _b("Console", [-0.7, 0.5, -1.4], [1.5, 1.0, 0.5]), _b("Bench", [1.85, 0.23, 0.2], [0.45, 0.46, 1.7]),
                 _b("Table", [1.3, 0.37, -0.9], [0.7, 0.74, 0.6])]
        if _BOAT.search(h):
            boxes += [_b("Ferry hull", [0, -0.65, 0.6], [5.6, 1.3, 13]), _b("Water", [0, -1.3, 0], [100, 0.1, 100])]
        return boxes, {"w": 4.4, "d": 3.4}
    if _LIGHTHOUSE.search(h):
        # a rocky point running out into the sea, the tower on its far end (standing on the shore's top at y = 0)
        return [_b("Water", [0, -0.8, -20], [100, 0.1, 100]), _b("Shore", [0, -0.4, -6], [16, 0.8, 20]),
                _b("Lighthouse", [-3.5, 7.5, -11], [3.2, 15, 3.2]), _b("Rocks", [2.6, 0.35, -1.5], [2.6, 0.7, 2.2]),
                _b("Rocks", [-2.2, 0.3, 1.6], [2.0, 0.6, 1.6])], None
    if _SEA.search(h) or (not interior and _BOAT.search(h)):
        # a pier out into the water, the ferry berthed broadside across its end, bollards at the edge
        return [_b("Water", [0, -1.2, -20], [100, 0.1, 100]), _b("Pier", [0, -0.6, -3], [3.2, 1.2, 16]),
                _b("Ferry", [0, 0.6, -14.2], [3.4, 3.6, 10.5], 90), _b("Bollard", [1.35, 0.25, -1.2], [0.3, 0.5, 0.3]),
                _b("Bollard", [1.35, 0.25, -10.4], [0.3, 0.5, 0.3]), _b("Crate", [-1.15, 0.35, -4.5], [0.7, 0.7, 0.7])], None
    boxes = [_b("Back wall", [0, 1.4, -3.0], [8, 2.8, 0.15])]
    if interior:
        boxes.append(_b("Table", [1.6, 0.37, -1.4], [1.2, 0.74, 0.7]))
    return boxes, None


# What a scene's action says a person does (\"INES (30s) sits on her suitcase.\"): a pose, and the seat or the thing in
# hand that the sentence names, placed at the figure.
_SIT = re.compile(r"\b(sits?|sitting|seated|perches?)\b", re.I)
_WALK = re.compile(r"\b(walks?|walking|climbs?|climbing|crosses|strides?)\b", re.I)
_COIL = re.compile(r"\b(coils?|coiling|winds?|winding|hauls?|hauling)\b", re.I)  # w39-p2-previz2: hands at work on a rope
_HELM = re.compile(r"\b(key|wheel|helm|engine|throttle|console|steers?)\b", re.I)
_LEAN = re.compile(r"\b(leans?|leaning)\b|\bhand on the (rail|railing|wheel)\b", re.I)
_PROPS = {"suitcase": ([0.62, 0.45, 0.26], "Suitcase"), "bench": ([1.4, 0.45, 0.42], "Bench"),
          "crate": ([0.6, 0.45, 0.6], "Crate"), "chair": ([0.46, 0.46, 0.46], "Chair"), "rope": ([0.55, 0.12, 0.55], "Rope")}


def names_in(name: str, text: str) -> bool:
    """True when the scene's text names the person as a word (\"Ines smiles\")."""
    return bool(name) and re.search(rf"\b{re.escape(name)}\b", text, re.I) is not None


_NOT_NAMES = {"CLOSE", "WIDE", "ON", "THE", "INSERT", "ANGLE", "POV", "CU", "MS", "WS", "EWS", "ECU", "MCU", "INT", "EXT",
              "DAY", "NIGHT", "DUSK", "DAWN", "CONT'D", "V.O", "O.S", "CUT", "TO", "FADE", "IN", "OUT", "LATER", "A", "AN",
              "AND", "OF", "SFX", "SOUND", "MUSIC", "TV", "OK", "I"}


def introduced_names(text: str) -> list[str]:
    """People a scene introduces in its action by the screenplay convention (a NAME in capitals, \"MARA sits on a
    crate\"), in order; for a scene whose people never speak (no character cue, so no derived character)."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = [x.strip() for x in para.strip().split("\n") if x.strip()]
        if not lines or lines[0].startswith(("=", "#", "[[", "Title:")) or lines[0].upper() == lines[0]:
            continue
        for w in re.findall(r"\b[A-Z][A-Z'\-]{1,}\b", " ".join(lines)):
            if w not in _NOT_NAMES and kit_of(w) is None and w not in out:
                out.append(w)
    return out


# A shot whose record has no Size yet takes one from its shot line's words (the usual screenplay shot words).
def size_from_line(text: str) -> str | None:
    t = f" {text.upper()} "
    for words, size in ((("EXTREME WIDE", "ESTABLISHING", " EWS "), "EWS"), (("EXTREME CLOSE", " ECU "), "ECU"),
                        (("INSERT",), "INSERT"), (("CLOSE", " CU "), "CU"), (("WIDE", "FULL", " WS "), "WS"),
                        (("MEDIUM", " MS ", " ON THE ", " ON "), "MS")):
        if any(w in t for w in words):
            return size
    return None


def _action_lines(text: str, name: str) -> list[str]:
    """The action paragraphs of a Fountain scene that name ``name`` (no heading, synopsis, shot line or dialogue)."""
    out = []
    for para in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = [x.strip() for x in para.strip().split("\n") if x.strip()]
        if not lines or lines[0].startswith(("=", "#", "[[", "Title:")) or lines[0].upper() == lines[0]:
            continue  # a synopsis / section / note, or a heading, shot line or character cue (dialogue follows it)
        body = " ".join(lines)
        out += [s for s in re.split(r"(?<=[.!?])\s", body) if names_in(name, s)]
    return out


def _action(text: str, name: str) -> tuple[str | None, str | None]:
    """(pose or None, a prop the sentence names or None) for the first action sentence that names ``name``."""
    for s in _action_lines(text, name):
        pose = ("sit" if _SIT.search(s) else "coil" if _COIL.search(s) else "lean" if _LEAN.search(s)
                else "walk" if _WALK.search(s) else None)
        prop = next((k for k in _PROPS if re.search(rf"\b{k}\b", s, re.I)), None)
        if pose or prop:
            return pose, prop
    return None, None


# A shot that names no figure but a set piece (INSERT THE CABIN LAMP, WIDE ON THE LIGHTHOUSE) frames that piece: the
# vertical span is the piece's height (or its width / depth seen in perspective) times this share per size.
_PIECE_SPAN = {"INSERT": 1.5, "ECU": 1.1, "CU": 1.3, "MCU": 1.6, "MCS": 1.8, "MS": 2.0, "MWS": 2.4, "FS": 1.3, "WS": 1.25, "EWS": 4.5}


def _ndc(pos: list[float], at: list[float], lens: float, p: list[float], aspect: float = 16 / 9) -> tuple[float, float]:
    """Where ``p`` lands in the frame of a level camera at ``pos`` aimed at ``at`` (36 mm sensor): (x, y), -1..1 is the
    frame (y up); a point behind the lens is (9, 9)."""
    fx, fy, fz = at[0] - pos[0], at[1] - pos[1], at[2] - pos[2]
    n = math.hypot(fx, fy, fz) or 1.0
    fx, fy, fz = fx / n, fy / n, fz / n
    rx, rz = -fz, fx
    rn = math.hypot(rx, rz) or 1.0
    rx, rz = rx / rn, rz / rn
    ux, uy, uz = -rz * fy, rz * fx - rx * fz, rx * fy
    vx, vy, vz = p[0] - pos[0], p[1] - pos[1], p[2] - pos[2]
    depth = vx * fx + vy * fy + vz * fz
    if depth <= 0.05:
        return 9.0, 9.0
    th = (36 / aspect) / 2 / lens
    return (vx * rx + vz * rz) / depth / (th * aspect), (vx * ux + vy * uy + vz * uz) / depth / th


def _in_frame(pos: list[float], at: list[float], lens: float, p: list[float], aspect: float = 16 / 9) -> bool:
    """True when the point ``p`` lies inside the frame of a level camera at ``pos`` aimed at ``at`` (36 mm sensor)."""
    fx, fy, fz = at[0] - pos[0], at[1] - pos[1], at[2] - pos[2]
    n = math.hypot(fx, fy, fz) or 1.0
    fx, fy, fz = fx / n, fy / n, fz / n
    rx, rz = -fz, fx  # right = forward x up
    rn = math.hypot(rx, rz) or 1.0
    rx, rz = rx / rn, rz / rn
    ux, uy, uz = -rz * fy, rz * fx - rx * fz, rx * fy  # up = right x forward (right has no y)
    vx, vy, vz = p[0] - pos[0], p[1] - pos[1], p[2] - pos[2]
    depth = vx * fx + vy * fy + vz * fz
    if depth <= 0.05:
        return False
    th = (36 / aspect) / 2 / lens
    return abs((vx * rx + vz * rz) / depth) < th * aspect and abs((vx * ux + vy * uy + vz * uz) / depth) < th


def _piece_camera(box: dict[str, Any], size: str | None, lens: float, angle: str | None,
                  boxes: list[dict[str, Any]] = (), figures: list[dict[str, Any]] = ()) -> tuple[list[float], list[float], list[float]]:
    """(camera position, aim, unit direction from the aim to the camera) that frames a set piece by its size."""
    w, hgt, d = box["size"]
    kind = kit_of(box["name"])
    side = kind in ("ferry", "hull")  # a boat reads from three-quarter side, a pier along its length
    if kind == "lighthouse" and size in ("WS", "EWS", "FS"):
        hgt = hgt * 1.15  # the beam's head and the gallery need air above the cap
    span = max(hgt, (max(w, d) if side else w) * 0.5625, 0.35 * (0 if side else d)) * _PIECE_SPAN.get(size or "MS", 2.0)
    if kind in ("ferry", "hull") and size == "EWS":
        span *= 0.5  # w39-p2-previz2 (bench 3.2: \"underexposed and small\"): the boat fills about half the frame, the sea round it
    vfov = 2 * math.atan((36 / (16 / 9)) / 2 / lens)
    dist = max(0.6, span / (2 * math.tan(vfov / 2)))
    if side:
        dist += w / 2
    tx, tz = box["pos"][0], box["pos"][2]
    top = box["pos"][1] + hgt / 2
    ty = max(box["pos"][1], 0.0) if hgt < 4 else box["pos"][1] - hgt * 0.15  # a tower: aim at its lower third
    if kind == "lamp":
        ty -= span * 0.2  # an insert on a lamp: the lamp in the upper third, the light it throws below it (bench: hgt * 0.9 put it out of an 85 mm frame)
    a = math.radians((box.get("yaw") or 0) + (55 if side else 20 if size in ("FS", "WS", "EWS") else 0))
    dx, dz = math.sin(a), math.cos(a)
    h = {"low": 0.45, "high": max(ty, 1.6) + 0.5 + 0.15 * span, "overhead": top + span, "ground": 0.25}.get(angle or "eye", 2.4 if size in ("WS", "EWS") else 1.6)
    # inside a room (a cabin around the piece) the camera stays under its ceiling and inside its walls
    room = next((b for b in boxes if kit_of(b["name"]) == "cabin" and b is not box and w < b["size"][0] and d < b["size"][2]
                 and abs(tx - b["pos"][0]) < b["size"][0] / 2 and abs(tz - b["pos"][2]) < b["size"][2] / 2), None)
    if room is not None and kind == "lamp":
        # w39-p2-previz3 (bench 3.1: \"a lamp, a ceiling and two crowns cut at the bottom edge\"; the script calls it an
        # INSERT): the lamp alone against the night window behind it, at its height, tilted down a little (the lamp in the
        # upper third, the pool of light under it below), no head in frame. The lens backs away from the window wall the lamp is nearest to,
        # at least far enough to hold the whole shade; among the angles round that line (0, +-10 .. +-40 degrees) the
        # first whose frame holds no figure's head wins
        rx, rz, rw, rd = room["pos"][0], room["pos"][2], room["size"][0] / 2 - 0.2, room["size"][2] / 2 - 0.2
        back = 1.0 if tz <= rz else -1.0  # away from the nearer (forward) wall
        under = next((b for b in boxes if kit_of(b["name"]) in ("console", "table", "bench") and abs(b["pos"][0] - tx) < b["size"][0] / 2
                      and abs(b["pos"][2] - tz) < b["size"][2] / 2), None)
        low = under["pos"][1] + under["size"][1] / 2 if under is not None else box["pos"][1] - hgt
        top = box["pos"][1] + hgt / 2
        ty = (top + low) / 2 - 0.04  # the whole shade and the lit top under it (the pool in the lower third)
        dist = max(dist, (top - low + 0.3) / (2 * math.tan(vfov / 2)))
        if under is not None and kit_of(under["name"]) == "console":
            # w39-p2-previz4 (sheet 3.1: the hand and the key cut by the bottom edge): over a console the aim sits a third
            # of the way up from its top (the key and the hand on it mid-frame, the shade in the top of the frame)
            # (85 mm holds 0.24 m per metre: the shade's mouth and the key on the console top, not the whole shade)
            mouth = box["pos"][1] - hgt / 2
            ty = (mouth + low + 0.1) / 2
            dist = max(0.6, (top - low + 0.25) / (2 * math.tan(vfov / 2)))
        ceil_y = room["pos"][1] + room["size"][1] / 2 - 0.15
        h = min(ceil_y, top + 0.35 * dist)  # from above the shade, looking down (the shot's High), under the ceiling: the
        # table reads as a surface (bench: a lens above the ceiling saw only the outside of the room)
        want = dist
        # w39-p2-previz4 (the lamp over the helm: the helmsman stands on the room's side of it): the lens goes round the
        # whole lamp (15 degree steps, the room's side first); among the headless views the first with room for the full
        # distance wins, else the headless one with the most room (a view across the console: the hand, not the face)
        best = None
        for off in sorted(range(0, 360, 15), key=lambda o: (min(o, 360 - o), -o)):
            a = math.radians(off)
            dx_, dz_ = math.sin(a), back * math.cos(a)
            room_d = min(((rx + math.copysign(rw, dx_) - tx) / dx_) if abs(dx_) > 1e-6 else 99,
                         ((rz + math.copysign(rd, dz_) - tz) / dz_) if abs(dz_) > 1e-6 else 99)
            dd = min(want, room_d)
            pos = [round(tx + dx_ * dd, 3), round(h, 3), round(tz + dz_ * dd, 3)]
            if any(_in_frame(pos, [tx, ty, tz], lens, [f["pos"][0], y, f["pos"][1]]) for f in figures for y in
                   (_TOP.get(f.get("pose") or "stand", _TOP["stand"]), _TOP.get(f.get("pose") or "stand", _TOP["stand"]) - 0.23)):
                continue
            if best is None or dd > best[0] + 1e-6:
                best = (dd, pos)
            if dd >= want - 1e-6:
                break
        pos = best[1] if best is not None else pos
        if under is not None and kit_of(under["name"]) == "console":
            # w39-p2-previz4: from where the lens stands, the aim's height that holds the shade's mouth and the console's
            # top at the front edge under the lamp (the key, the hand on it) evenly inside the frame
            ya = math.radians(under.get("yaw") or 0)
            fz = under["size"][2] * 0.3
            keep = ([tx, box["pos"][1], tz], [under["pos"][0] + fz * math.sin(ya), low + 0.06, under["pos"][2] + fz * math.cos(ya)])
            ty = min((low + 0.01 * i for i in range(int((box["pos"][1] - low) * 100))),
                     key=lambda y: max(abs(_ndc(pos, [tx, y, tz], lens, p)[1]) for p in keep))
        return pos, [round(tx, 3), round(ty, 3), round(tz, 3)], [pos[0] - tx, 0.0, pos[2] - tz]
    if room is not None:
        h = min(h, room["pos"][1] + room["size"][1] / 2 - 0.12)
        wall_z = room["pos"][2] + math.copysign(room["size"][2] / 2 - 0.15, dz)  # the wall the camera backs toward
        dist = min(dist, max(0.6, (wall_z - tz) / dz if abs(dz) > 0.05 else dist))
    pos = [round(tx + dx * dist, 3), round(h, 3), round(tz + dz * dist, 3)]
    return pos, [round(tx, 3), round(ty, 3), round(tz, 3)], [dx, 0.0, dz]


def _piece_named(text: str, boxes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The smallest kit piece whose kit word the shot line names (WIDE ON THE FERRY -> Ferry hull)."""
    words = set(re.findall(r"[a-z]+", text.lower()))
    hits = [b for b in boxes if kit_of(b["name"]) and words & set(re.findall(r"[a-z]+", b["name"].lower()))
            and kit_of(b["name"]) not in ("water", "shore")]
    return min(hits, key=lambda b: b["size"][0] * b["size"][1] * b["size"][2]) if hits else None


def block(current: dict[str, Any], scene_heading: str, people: list[dict[str, Any]], shots: list[dict[str, Any]],
          cameras: dict[str, Any], text: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """A starting blocking: figures for the scene's people that have none yet (facing the room's front), a floor and a
    starting set when the set is empty (w39-p2-previz: kit pieces for the place in the heading, see starting_set), and
    a camera for every shot without one: placed by its Size at a distance in front of the figure the shot names (else
    a set piece it names, else the group), its Angle sets the height, a Movement gives an End. ``text`` (the scene's
    script) gives each new figure its pose and prop (\"sits on her suitcase\"). Returns (new set, {shot: camera}) for
    the shots it gives a camera; existing figures, boxes and cameras stay."""
    s = copy.deepcopy(current)
    if not s["boxes"]:
        boxes, floor = starting_set(scene_heading)
        s["boxes"] = [_box(b) for b in boxes]
        if floor:
            s["floor"] = dict(floor)
    have = {f["name"].casefold() for f in s["figures"]}
    new = [p for p in people if p["name"].casefold() not in have]
    total = len(s["figures"]) + len(new)
    for i, p in enumerate(new, start=len(s["figures"])):
        s["figures"].append(_figure({"name": p["name"], "element": p.get("element"), "version": p.get("version"), "pos": _fig_pos(i, total), "yaw": 0, "pose": "stand"}))
    # w23-pvlook2 item 4 (owner read: "both figures face the lens: a blocked two-hander faces each other"): the figures
    # this block places turn toward the others (their centre), cheated 30 degrees toward the room's front (+z) so the
    # cameras there still see three-quarter faces (the usual stage cheat); figures the artist placed keep their turn.
    placed = {id(f) for f in s["figures"][len(s["figures"]) - len(new):]}
    for f in s["figures"]:
        if id(f) in placed and len(s["figures"]) > 1:
            f["yaw"] = _facing(f, s["figures"])
    # w39-p2-previz: the action gives a new figure its pose and the prop it uses (a seat under it, a rope at its feet)
    for f in s["figures"]:
        if id(f) not in placed or not text:
            continue
        pose, prop = _action(text, f["name"])
        if pose:
            f["pose"] = pose
        # (agent run: "MARA sits on a crate" while the set's crate stood 4 m away) the prop goes to the figure unless
        # one of that kind is already at its feet
        if prop and not any(kit_of(b["name"]) == prop and math.hypot(b["pos"][0] - f["pos"][0], b["pos"][2] - f["pos"][1]) < 0.6
                            for b in s["boxes"]):
            size, label = _PROPS[prop]
            seat = pose == "sit" and prop != "rope"
            a = math.radians(f["yaw"])
            off = (0.0, -0.08) if seat else (0.45, 0.25)
            px = f["pos"][0] + off[0] * math.cos(a) + off[1] * math.sin(a)
            pz = f["pos"][1] - off[0] * math.sin(a) + off[1] * math.cos(a)
            s["boxes"].append(_box(_b(label, [round(px, 3), round(size[1] / 2, 3), round(pz, 3)], size, f["yaw"])))
    # w39-p2-previz2: a person whose action works the helm (a key, the wheel, the engine) stands at the set's console,
    # facing it; the others keep their places, turned toward that person
    console = next((b for b in s["boxes"] if kit_of(b["name"]) == "console"), None)
    if console is not None and text:
        for f in s["figures"]:
            if id(f) in placed and any(_HELM.search(x) for x in _action_lines(text, f["name"])):
                # (w39-p2-previz4: 0.26 m from the console's front, so his hand reaches the key: previz-render's reach)
                # (and his right shoulder behind the key, a quarter of the console's width right of its centre)
                f["pos"] = [round(console["pos"][0] + console["size"][0] * 0.25 + 0.17, 3), round(console["pos"][2] + console["size"][2] / 2 + 0.26, 3)]
                f["yaw"] = 160.0
                for o in s["figures"]:
                    if o is not f and id(o) in placed:
                        o["yaw"] = round(math.degrees(math.atan2(f["pos"][0] - o["pos"][0], f["pos"][1] - o["pos"][1])), 1)
                break
    out: dict[str, Any] = {}
    for shot in shots:
        if cameras.get(shot["id"]):
            continue
        d = dict(shot.get("details") or {})
        if not d.get("size") and size_from_line(shot.get("text", "")):
            d["size"] = size_from_line(shot.get("text", ""))  # (the record keeps its own details; only the framing uses it)
        text_of = shot.get("text", "")
        target = next((f for f in s["figures"] if f["name"] and f["name"].upper() in text_of.upper()), None)
        piece = _piece_named(text_of, s["boxes"]) if target is None else None
        if piece is not None:
            pos, at, _dir = _piece_camera(piece, d.get("size"), float(d.get("lens") or 35), d.get("angle"), s["boxes"], s["figures"])
            if d.get("size") in ("WS", "EWS") and s["figures"] and kit_of(piece["name"]) not in ("ferry", "hull"):
                # w39-p2-previz2 (bench 1.1, 2.1: \"figures a few pixels tall\"): people carry a wide shot: the camera comes
                # in along its line until a standing figure is 0.16 (WS 0.2) of the frame height, aimed between the
                # people and the place, so the place still reads behind them
                vh = math.atan((36 / (16 / 9)) / 2 / float(d.get("lens") or 35))
                gx = sum(f["pos"][0] for f in s["figures"]) / len(s["figures"])
                gz = sum(f["pos"][1] for f in s["figures"]) / len(s["figures"])
                want = 1.735 / ((0.2 if d.get("size") == "WS" else 0.16) * 2 * math.tan(vh))
                hx, hz = pos[0] - gx, pos[2] - gz
                have = math.hypot(hx, hz)
                if have > want:
                    k = want / have
                    pos = [round(gx + hx * k, 3), pos[1], round(gz + hz * k, 3)]
                    at = [round(gx * 0.55 + at[0] * 0.45, 3), at[1], round(gz * 0.55 + at[2] * 0.45, 3)]
            cam = {"lens_mm": d.get("lens") or 35, "start": {"pos": pos, "at": at}}
            move = d.get("move")
            if move in ("dolly_in", "dolly_out", "truck", "pedestal", "crane", "arc", "tilt"):
                v = [at[i] - pos[i] for i in range(3)]
                end = {"dolly_in": [pos[i] + v[i] * 0.4 for i in range(3)], "dolly_out": [pos[i] - v[i] * 0.5 for i in range(3)],
                       "truck": [pos[0] + _dir[2] * 1.5, pos[1], pos[2] - _dir[0] * 1.5], "pedestal": [pos[0], pos[1] + 0.6, pos[2]],
                       "crane": [pos[0] + _dir[0] * 1.5, pos[1] + 3.0, pos[2] + _dir[2] * 1.5],
                       "arc": [at[0] + math.sin(math.atan2(-v[0], -v[2]) + 0.6) * math.hypot(v[0], v[2]), pos[1],
                               at[2] + math.cos(math.atan2(-v[0], -v[2]) + 0.6) * math.hypot(v[0], v[2])],
                       "tilt": list(pos)}[move]
                end_at = [at[0], at[1] + max(0.7, piece["size"][1] * 0.35), at[2]] if move == "tilt" else list(at)
                cam["end"] = {"pos": [round(x, 3) for x in end], "at": [round(x, 3) for x in end_at]}
            out[shot["id"]] = cam
            continue
        if target is not None:
            tx, tz, ty = target["pos"][0], target["pos"][1], 1.55 if d.get("size") in ("ECU", "CU", "MCU", "MCS") else 1.2
        elif s["figures"]:
            tx = sum(f["pos"][0] for f in s["figures"]) / len(s["figures"])
            tz, ty = sum(f["pos"][1] for f in s["figures"]) / len(s["figures"]), 1.2
        else:
            tx, tz, ty = 0.0, 0.0, 1.0
        dist = DISTANCE.get(d.get("size"), DISTANCE["MS"]) * (float(d.get("lens") or 35) / 35)
        framed = _framed(d.get("size"), float(d.get("lens") or 35), target) if target is not None else None
        if framed is not None:
            dist, ty = max(framed[0], 0.52), framed[1]
        h = {"high": ty + 1.2, "low": max(0.35, ty - 0.8), "overhead": ty + 3.0, "ground": 0.25}.get(d.get("angle"), ty)
        if framed is not None and d.get("size") in ("ECU", "CU", "MCU") and d.get("angle") in (None, "eye"):
            # w39-p2-previz2 (bench 1.2: the horizon cut through the chin): a level close-up's lens sits a little under
            # the eyes, so the horizon (the lens height) falls under the chin; the aim below keeps the size's headroom
            h = ty - dist * 0.55 * math.tan(math.atan((36 / (16 / 9)) / 2 / float(d.get("lens") or 35)))
        close_level = target is not None and d.get("size") in ("ECU", "CU", "MCU") and d.get("angle") in (None, "eye")
        if framed is not None and d.get("size") in HEADROOM and (d.get("angle") in ("high", "low", "ground") or close_level):
            # w23-pvlook2 item 6 (owner read: "the Low angle barely reads"): a tilted camera aims so the head keeps the
            # size's headroom (as the page's aimAt), so a Low looks up at the figure instead of at its chest
            top = _TOP.get(target.get("pose") or "stand", _TOP["stand"])
            vh = math.atan((36 / (16 / 9)) / 2 / float(d.get("lens") or 35))
            hr = math.atan((0.5 - HEADROOM[d["size"]]) * 2 * math.tan(vh))
            if d.get("angle") == "low" and any(kit_of(b["name"]) in ("water", "shore") for b in s["boxes"]):
                # w39-p2-previz2 (bench 1.3: \"only sky behind him, the low camera loses the place\"): outdoors a Low keeps
                # the horizon in the frame's lower part: the lens rises until its tilt is at most 0.8 of the half field
                while math.atan2(top - h, dist) - hr > 0.8 * vh and h < 0.9:  # (a Low stays at hip height or under)
                    h = min(0.9, h + 0.02)
            up = math.atan2(top - h, dist) - hr
            ty = h + math.tan(up) * dist
        # w23-pvlook2 item 4 (owner read: "Ines vanishes from the 1.3 MS"): a medium single on one figure of several
        # leads toward its partner (_lead), so the partner stays in the frame's edge and the subject looks into the frame
        lead = _lead(target, s["figures"], d.get("size"), float(d.get("lens") or 35), dist) if target is not None else 0.0
        # w39-p2-previz (bench 1.2: "the back of her head"): a close single comes round toward the face, half way
        # between the room's front (+z) and where the figure faces, so a figure turned to its partner shows three-quarter
        ox, oz = 0.0, 1.0
        if target is not None and d.get("size") in ("ECU", "CU", "MCU"):
            # w39-p2-previz2 (bench 1.2: \"a one-eyed blob turned away\"): a close single sees the face three-quarter: the
            # camera stands 16 degrees off the line the figure LOOKS along (its gaze at the partner), on the room's front side
            # (w39-p2-previz3, bench 1.2: at 28 degrees the mannequin's subtle face read as \"turned away\": both sockets,
            # the nose plane and a narrower far cheek need the lens nearer the gaze line; 16 still shows the far cheek narrower and the partner's shoulder out of the frame)
            g = gaze(target, s["figures"])
            # (w39-p2-previz3, bench 1.2: at 16 degrees the partner she looks at stood between the lens and her as a blurred
            # wedge): the angle opens from 20 in 2 degree steps, up to 34, until the line from the lens to her head passes every
            # other figure by 0.45 m (its shoulder may touch the frame edge: a dirty single)
            def clear(a: float) -> bool:
                cx_, cz_ = target["pos"][0] + math.sin(math.radians(a)) * dist, target["pos"][1] + math.cos(math.radians(a)) * dist
                for o in s["figures"]:
                    if o is target:
                        continue
                    vx, vz = target["pos"][0] - cx_, target["pos"][1] - cz_
                    t = max(0.0, min(1.0, ((o["pos"][0] - cx_) * vx + (o["pos"][1] - cz_) * vz) / (vx * vx + vz * vz or 1)))
                    if math.hypot(cx_ + vx * t - o["pos"][0], cz_ + vz * t - o["pos"][1]) < 0.45:
                        return False
                return True
            off = next((a for a in range(20, 36, 2) if clear(max((g + a, g - a), key=lambda b: math.cos(math.radians(b))))), 34)
            c = max((g + off, g - off), key=lambda a: math.cos(math.radians(a)))
            ox, oz = math.sin(math.radians(c)), math.cos(math.radians(c))
        elif target is not None and d.get("size") in ("MCS", "MS"):
            fa = math.radians(target.get("yaw") or 0)
            vx, vz = math.sin(fa), 1 + math.cos(fa)
            n = math.hypot(vx, vz)
            if n > 0.2:
                ox, oz = vx / n, vz / n
        # (the lead runs along the camera's right, so the partner stays at the frame's edge from the turned camera too)
        start = {"pos": [round(tx + ox * dist, 3), round(h, 3), round(tz + oz * dist, 3)], "at": [round(tx + lead * oz, 3), round(ty, 3), round(tz - lead * ox, 3)]}
        cam: dict[str, Any] = {"lens_mm": d.get("lens") or 35, "start": start}
        move = d.get("move")
        if move in ("dolly_in", "dolly_out", "truck", "pedestal", "crane", "arc"):
            px, py, pz = start["pos"]
            end = {"dolly_in": [tx + ox * dist * 0.55, py, tz + oz * dist * 0.55], "dolly_out": [tx + ox * dist * 1.6, py, tz + oz * dist * 1.6], "truck": [px + 1.2, py, pz],
                   "pedestal": [px, py + 0.6, pz], "crane": [px, py + 1.8, pz + 0.6],
                   "arc": [round(tx + math.sin(0.6) * dist, 3), py, round(tz + math.cos(0.6) * dist, 3)]}[move]
            cam["end"] = {"pos": [round(v, 3) for v in end], "at": list(start["at"])}
        out[shot["id"]] = cam
    return s, out


# --- proposals (contract 3) -------------------------------------------------------------------------------------------
def propose(doc: dict[str, Any], scene: str, by: str, label: str, at: str, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    for p in doc.setdefault("proposals", []):
        if p["state"] == "open" and p["scene"] == scene:
            p["state"] = "accepted"  # a newer change on the same scene closes the older one (it stays as history)
    proposal = {"id": "pr-" + secrets.token_hex(4), "by": by, "scene": scene, "label": label[:120], "at": at, "state": "open",
                "before": copy.deepcopy(before), "after": copy.deepcopy(after)}
    doc["proposals"] = (doc["proposals"] + [proposal])[-MAX_PROPOSALS:]
    return proposal


def find_proposal(doc: dict[str, Any], pid: str | None) -> dict[str, Any]:
    open_ = [p for p in doc.get("proposals") or [] if p["state"] == "open"]
    hit = next((p for p in open_ if p["id"] == pid), None) if pid else (open_[-1] if open_ else None)
    if hit is None:
        raise PrevizRefusal("proposal_unknown", "No open Hermes change with that id (continuo_previz_read proposals).")
    return hit


def undo_check(proposal: dict[str, Any], current_set: dict[str, Any], current_cameras: dict[str, Any]) -> None:
    """Undo restores only what is still exactly as the proposal left it; anything changed since is a conflict."""
    changed = []
    if proposal["after"].get("set") is not None and _strip(current_set) != _strip(proposal["after"]["set"]):
        changed.append("the set")
    for shot, cam in (proposal["after"].get("cameras") or {}).items():
        if current_cameras.get(shot) != cam:
            changed.append(shot)
    if changed:
        raise PrevizRefusal("proposal_conflict", f"Changed since Hermes' change: {', '.join(changed)}. Nothing was undone.")


def _strip(s: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if s is None else {k: v for k, v in s.items() if k != "kept"}


def check_document(doc: Any) -> str | None:
    """None when ``doc`` is a well-formed studio.previz/1 document, else the problem (architecture review finding 2):
    every scene is re-validated through the same rules a write uses, so a stored set equals what apply_set would keep."""
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        return "not a studio.previz/1 document"
    if set(doc) - {"schema", "revision", "scenes", "proposals"}:
        return "unknown top-level field"
    if type(doc.get("revision")) is not int or doc["revision"] < 0:
        return "revision is not an integer >= 0"
    scenes, props = doc.get("scenes"), doc.get("proposals", [])
    if not isinstance(scenes, dict) or len(scenes) > 2000 or not isinstance(props, list) or len(props) > MAX_PROPOSALS:
        return "scenes / proposals are not bounded"
    try:
        for scene, s in scenes.items():
            err = _check_set(scene, s)
            if err:
                return err
        for p in props:
            if not isinstance(p, dict) or set(p) != {"id", "by", "scene", "label", "at", "state", "before", "after"}:
                return "a proposal has the wrong fields"
            if not re.fullmatch(r"pr-[0-9a-f]{8}", str(p["id"])) or p["by"] not in ("artist", "hermes") or not SCENE_ID_RE.match(str(p["scene"])) \
                    or p["state"] not in ("open", "accepted", "undone", "dismissed") or not isinstance(p["label"], str) or len(p["label"]) > 120:
                return "a proposal is invalid"
            for side in (p["before"], p["after"]):
                if not isinstance(side, dict) or set(side) - {"set", "cameras"}:
                    return "a proposal delta is invalid"
                if side.get("set") is not None and _check_set(p["scene"], side["set"]):
                    return "a proposal set is invalid"
                cams = side.get("cameras") or {}
                if not isinstance(cams, dict) or not all(SHOT_ID_RE.match(str(k)) and (v is None or isinstance(v, dict)) for k, v in cams.items()):
                    return "a proposal camera delta is invalid"
    except PrevizRefusal as exc:
        return exc.message
    return None


def _check_set(scene: Any, s: Any) -> str | None:
    # w23-pvlook: light and matte are optional (a set written before them has neither: auto light, the default matte)
    if not isinstance(scene, str) or not SCENE_ID_RE.match(scene) or not isinstance(s, dict) or not {"floor", "boxes", "figures", "overrides", "kept"} <= set(s) <= {"floor", "boxes", "figures", "overrides", "kept", "light", "matte"}:
        return f"scene {str(scene)[:20]} is not a set"
    probe = {"scenes": {}}
    again = apply_set(probe, scene, {k: copy.deepcopy(s[k]) for k in ("floor", "boxes", "figures", "overrides", "kept", "light", "matte") if k in s})
    if again != s:
        return f"scene {scene} holds values a write would not keep"
    return None


def _proposal_shots(p: dict[str, Any]) -> list[str]:
    """The shots a proposal changes: its cameras and (lane w24-rest) the shots whose per-shot figure override changed."""
    shots = set((p["after"].get("cameras") or {}).keys())
    before, after = (p["before"].get("set") or {}).get("overrides") or {}, (p["after"].get("set") or {}).get("overrides") or {}
    shots |= {shot for shot in set(before) | set(after) if before.get(shot) != after.get(shot)}
    return sorted(shots)


def public(doc: dict[str, Any], scene: str | None = None) -> dict[str, Any]:
    scenes = {k: v for k, v in doc["scenes"].items() if scene is None or k == scene}
    if scene is not None and scene not in scenes:
        scenes[scene] = default_set()
    props = [{k: p[k] for k in ("id", "by", "scene", "label", "at", "state")} | {"shots": _proposal_shots(p)}
             for p in doc.get("proposals") or [] if scene is None or p["scene"] == scene]
    return {"schema": SCHEMA, "revision": int(doc.get("revision") or 0), "scenes": copy.deepcopy(scenes), "proposals": props,
            "conventions": {"units": "metres", "up": "+y", "handedness": "right", "yaw": "degrees, 0 faces +z"}}
