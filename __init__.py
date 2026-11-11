"""Film Suite agent tools: exact backend routes plus existing pane bridges."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path
from typing import Any


# Stateless migration is also used by top-level, unregistered host loaders.
import importlib.util as _migration_import
_migration_spec = _migration_import.spec_from_file_location("film_suite_legacy_migration", Path(__file__).parent / "dashboard" / "generation" / "legacy_plugin_migration.py")
_migration_module = _migration_import.module_from_spec(_migration_spec)
_migration_spec.loader.exec_module(_migration_module)
state_root = _migration_module.state_root

_ENGINE_PATH = Path(__file__).with_name("dashboard") / "music_analysis.py"
_ENGINE_SPEC = importlib.util.spec_from_file_location("film_suite_agent_music_analysis", _ENGINE_PATH)
if _ENGINE_SPEC is None or _ENGINE_SPEC.loader is None:
    raise RuntimeError("The music-analysis engine is unavailable.")
music_analysis = importlib.util.module_from_spec(_ENGINE_SPEC)
_ENGINE_SPEC.loader.exec_module(music_analysis)

_BRIDGE_PATH = Path(__file__).with_name("dashboard") / "music_bridge.py"
_BRIDGE_SPEC = importlib.util.spec_from_file_location("film_suite_agent_music_bridge", _BRIDGE_PATH)
if _BRIDGE_SPEC is None or _BRIDGE_SPEC.loader is None:
    raise RuntimeError("The Music renderer bridge is unavailable.")
music_bridge = importlib.util.module_from_spec(_BRIDGE_SPEC)
_BRIDGE_SPEC.loader.exec_module(music_bridge)

_IMAGE_BRIDGE_PATH = Path(__file__).with_name("dashboard") / "image_bridge.py"
_IMAGE_BRIDGE_SPEC = importlib.util.spec_from_file_location("film_suite_agent_image_bridge", _IMAGE_BRIDGE_PATH)
if _IMAGE_BRIDGE_SPEC is None or _IMAGE_BRIDGE_SPEC.loader is None:
    raise RuntimeError("The Image renderer bridge is unavailable.")
image_bridge = importlib.util.module_from_spec(_IMAGE_BRIDGE_SPEC)
_IMAGE_BRIDGE_SPEC.loader.exec_module(image_bridge)

# The Continuo frame (lane w1-frame): which page is open, the project and the Script page.
_SUITE_BRIDGE_PATH = Path(__file__).with_name("dashboard") / "routes" / "shared" / "suite_bridge.py"
_SUITE_BRIDGE_SPEC = importlib.util.spec_from_file_location("film_suite_agent_suite_bridge", _SUITE_BRIDGE_PATH)
if _SUITE_BRIDGE_SPEC is None or _SUITE_BRIDGE_SPEC.loader is None:
    raise RuntimeError("The suite frame bridge is unavailable.")
suite_bridge = importlib.util.module_from_spec(_SUITE_BRIDGE_SPEC)
_SUITE_BRIDGE_SPEC.loader.exec_module(suite_bridge)

# Weg A1 push events: tell the pane an action waits, so it need not poll fast.
_EVENTS_PATH = Path(__file__).with_name("dashboard") / "film_events.py"
_EVENTS_SPEC = importlib.util.spec_from_file_location("film_suite_agent_events", _EVENTS_PATH)
if _EVENTS_SPEC is None or _EVENTS_SPEC.loader is None:
    raise RuntimeError("The Continuo event helper is unavailable.")
film_events = importlib.util.module_from_spec(_EVENTS_SPEC)
_EVENTS_SPEC.loader.exec_module(film_events)

_IMAGE_CATALOG_PATH = Path(__file__).with_name("dashboard") / "image_builder" / "krea2-builder-catalog.v1.json"
_IMAGE_RENDER_SPEC_PATH = Path(__file__).with_name("dashboard") / "generation" / "krea2_render_spec.py"
_MODEL_CATALOG_PATH = Path(__file__).with_name("dashboard") / "generation" / "model_catalog.py"

# A pane counts as connected when its last heartbeat is this recent. Weg A1:
# in event mode the renderer beats every 15 s (hidden panes too), 1.5 s / 750 ms
# otherwise.
_BRIDGE_FRESH_SECONDS = 20.0
_SUITE_OPEN_PROJECT_ACK_SECONDS = 20.0
_EVENT_KINDS = frozenset({"beat", "downbeat", "accent", "section"})


def _result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _error(message: str) -> str:
    return _result({"ok": False, "error": message})


def _source_path(raw: Any) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("path is required")
    path = Path(text).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"audio file does not exist: {path}")
    return path


# Lane w34-sec: the one lexical guard against UNC / device paths (dashboard/studio_core/local_path.py, stdlib).
_LOCAL_PATH_SPEC = importlib.util.spec_from_file_location(
    "film_suite_agent_local_path", Path(__file__).parent / "dashboard" / "studio_core" / "local_path.py")
_LOCAL_PATH = importlib.util.module_from_spec(_LOCAL_PATH_SPEC)
_LOCAL_PATH_SPEC.loader.exec_module(_LOCAL_PATH)


class _FolderRefused(Exception):
    """Lane w21-rel (AUD-144): the file is outside the folders the artist approved."""


_ROUTE_TOOLS_MODULE: Any = None


def _route_api() -> Any:
    """The backend facade the route tools dispatch to (agent_tools.backend_api: the mounted one when Desktop runs)."""
    global _ROUTE_TOOLS_MODULE
    if _ROUTE_TOOLS_MODULE is None:
        spec = importlib.util.spec_from_file_location("film_suite_agent_route_tools_roots", Path(__file__).with_name("agent_tools.py"))
        if spec is None or spec.loader is None:
            raise RuntimeError("Continuo route tools are unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _ROUTE_TOOLS_MODULE = module
    return _ROUTE_TOOLS_MODULE.backend_api()


def _approved_source(args: dict[str, Any]) -> Path:
    """Lane w21-rel (AUD-144; reference: the hand route /music/analyze, routes/music/analysis_1.py): the file only
    inside the server's approved folders (project_ref: the project's, else Loose work; approved_roots only narrow),
    checked as Hermes (the same _request_roots + _confined_music_path the page's route calls)."""
    # Lane w34-sec (scan finding 12): a network / device path is refused on its text, before _source_path resolves
    # and stats it (on Windows that opens an SMB session to the named host).
    if _LOCAL_PATH.remote_path_reason(str(args.get("path") or "")) is not None:
        raise _FolderRefused()
    _source_path(args.get("path"))  # the old "path is required" / "does not exist" answers stay
    api = _route_api()
    raw_ref = args.get("project_ref")
    project_ref = api.ProjectReference(**raw_ref) if isinstance(raw_ref, dict) else None
    roots = args.get("approved_roots") or []
    if not isinstance(roots, list) or any(type(root) is not str for root in roots):
        raise ValueError("approved_roots must be a list of folder paths")
    token = api._FILM_CALLER.set("hermes")
    try:
        allowed = api._request_roots(roots, project_ref, path=str(args.get("path")), server_list=True)
        return Path(api._confined_music_path(str(args.get("path")), allowed))
    except api._FolderNotApproved:
        raise _FolderRefused() from None
    finally:
        api._FILM_CALLER.reset(token)


def _folder_refusal() -> str:
    return _result(_route_api()._folder_refusal())


def _analysis(raw: Any) -> tuple[Path, dict[str, Any]]:
    # Lane w21-rel (AUD-144): every legacy music tool passes its whole args; a bare path is never read unchecked.
    args = raw if isinstance(raw, dict) else {"path": raw}
    path = _approved_source(args)
    return path, music_analysis.analyze_cached(path)


def _compact_analysis(path: Path, analysis: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "path": str(path),
        "schema": analysis.get("schema"),
        "duration": analysis.get("duration"),
        "identified_recording": analysis.get("identified_recording"),
        "tempo": analysis.get("tempo_global"),
        "tempo_curve_status": analysis.get("tempo_curve_status"),
        "meter": analysis.get("meter"),
        "sections": analysis.get("sections") or [],
        "event_counts": {
            "beats": len(analysis.get("beats") or []),
            "downbeats": len(analysis.get("downbeats") or []),
            "accents": len(analysis.get("accents") or []),
        },
        "key": {
            "name": analysis.get("key"),
            "mode": analysis.get("mode"),
            "confidence": analysis.get("key_confidence"),
            "planning_constraint": False,
        },
        "brightness": analysis.get("brightness"),
        "integer_spans": analysis.get("integer_spans") or [],
        "summary": music_analysis.summarize(analysis),
    }


def _range(args: dict[str, Any], duration: float) -> tuple[float, float]:
    start = max(0.0, float(args.get("start", 0.0)))
    end = min(duration, float(args.get("end", duration)))
    if end <= start:
        raise ValueError(f"end must be greater than start and within 0..{duration:.3f}s")
    return start, end


def _events_in_range(
    analysis: dict[str, Any], start: float, end: float, kinds: set[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def add(kind: str, time_value: Any, source: dict[str, Any] | None = None) -> None:
        if kind not in kinds:
            return
        try:
            time_number = float(time_value)
        except (TypeError, ValueError):
            return
        if time_number < start - 1e-9 or time_number > end + 1e-9:
            return
        row: dict[str, Any] = {"kind": kind, "time": round(time_number, 4)}
        if source:
            for key in ("label", "confidence", "strength", "salience", "source"):
                if key in source:
                    row[key] = source[key]
        rows.append(row)

    for value in analysis.get("beats") or []:
        add("beat", value)
    for value in analysis.get("downbeats") or []:
        add("downbeat", value)
    for row in analysis.get("accents") or []:
        if isinstance(row, dict):
            add("accent", row.get("time"), row)
    for row in analysis.get("sections") or []:
        if isinstance(row, dict):
            add("section", row.get("start"), row)
    return sorted(rows, key=lambda row: (row["time"], row["kind"]))


def _handle_music_analyze(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        path, analysis = _analysis(args)
        return _result(_compact_analysis(path, analysis))
    except _FolderRefused:
        return _folder_refusal()
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _error(str(exc))


def _handle_music_describe(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        path, analysis = _analysis(args)
        duration = float(analysis.get("duration") or 0.0)
        start, end = _range(args, duration)
        kinds = set(_EVENT_KINDS)
        events = _events_in_range(analysis, start, end, kinds)
        sections = [
            row
            for row in analysis.get("sections") or []
            if isinstance(row, dict)
            and float(row.get("end", 0.0)) > start
            and float(row.get("start", 0.0)) < end
        ]
        accents = [row for row in events if row["kind"] == "accent"]
        accents.sort(key=lambda row: float(row.get("salience") or 0.0), reverse=True)
        return _result(
            {
                "ok": True,
                "path": str(path),
                "range": {"start": start, "end": end, "duration": round(end - start, 4)},
                "identified_recording": analysis.get("identified_recording"),
                "tempo": analysis.get("tempo_global"),
                "meter": analysis.get("meter"),
                "sections": sections,
                "strongest_accents": accents[:12],
                "event_counts": {
                    kind: sum(1 for row in events if row["kind"] == kind)
                    for kind in sorted(kinds)
                },
                "caution": (
                    "Bar-derived timing is withheld because tempo or meter is not fixed."
                    if analysis.get("tempo_global", {}).get("status") != "fixed"
                    or analysis.get("meter", {}).get("status") != "fixed"
                    else None
                ),
            }
        )
    except _FolderRefused:
        return _folder_refusal()
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _error(str(exc))


def _handle_music_events(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        path, analysis = _analysis(args)
        duration = float(analysis.get("duration") or 0.0)
        start, end = _range(args, duration)
        raw_kinds = args.get("kinds") or sorted(_EVENT_KINDS)
        kinds = {str(value).strip().lower() for value in raw_kinds}
        unknown = kinds - _EVENT_KINDS
        if unknown:
            raise ValueError(f"unknown event kinds: {', '.join(sorted(unknown))}")
        events = _events_in_range(analysis, start, end, kinds)
        limit = max(1, min(2000, int(args.get("limit", 500))))
        return _result(
            {
                "ok": True,
                "path": str(path),
                "range": {"start": start, "end": end},
                "kinds": sorted(kinds),
                "events": events[:limit],
                "total": len(events),
                "truncated": len(events) > limit,
            }
        )
    except _FolderRefused:
        return _folder_refusal()
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _error(str(exc))


def _handle_music_spans(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        path, analysis = _analysis(args)
        duration = float(analysis.get("duration") or 0.0)
        minimum = max(0.1, float(args.get("min_s", 5.0)))
        maximum = min(duration, float(args.get("max_s", 20.0)))
        if maximum < minimum:
            raise ValueError("max_s must be greater than or equal to min_s")
        integer_durations = [
            row
            for row in analysis.get("integer_spans") or []
            if minimum <= float(row.get("seconds", 0.0)) <= maximum
        ]
        section_ranges: list[dict[str, Any]] = []
        for section in analysis.get("sections") or []:
            if not isinstance(section, dict):
                continue
            section_start = max(0.0, float(section.get("start", 0.0)))
            section_end = min(duration, float(section.get("end", duration)))
            length = section_end - section_start
            if length < minimum:
                continue
            end = min(section_end, section_start + maximum)
            section_ranges.append(
                {
                    "start": round(section_start, 3),
                    "end": round(end, 3),
                    "duration": round(end - section_start, 3),
                    "section": section.get("label"),
                    "source": section.get("source") or "signal",
                }
            )
        supported = (
            analysis.get("tempo_global", {}).get("status") == "fixed"
            and analysis.get("meter", {}).get("status") == "fixed"
        )
        return _result(
            {
                "ok": True,
                "path": str(path),
                "constraints": {"min_s": minimum, "max_s": maximum},
                "bar_math_supported": supported,
                "integer_durations": integer_durations,
                "section_ranges": section_ranges,
                "caution": None if supported else "No global bar claim: tempo or meter is variable, mixed, ambiguous, or absent.",
            }
        )
    except _FolderRefused:
        return _folder_refusal()
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _error(str(exc))


def _music_bridge_store() -> Any:
    try:
        from hermes_constants import get_hermes_home

        hermes_home = Path(get_hermes_home())
    except (ImportError, OSError, TypeError, ValueError) as exc:
        raise music_bridge.MusicBridgeValidationError("Music bridge storage is unavailable") from exc
    return music_bridge.MusicBridgeStore(state_root(hermes_home / "plugins", "music-bridge"))


def _handle_music_state(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        window_id = args.get("window_id") or None
        max_age = max(0.5, min(60.0, float(args.get("max_age_seconds", _BRIDGE_FRESH_SECONDS))))
        # Only live windows (HER-048); one silent for an hour is removed on read, as on the suite bridge.
        windows = [row for row in _music_bridge_store().list_snapshots(
            window_id=window_id, max_age_seconds=max_age, forget_after_seconds=music_bridge.STALE_WINDOW_SECONDS) if row["connected"]]
        return _result({
            "ok": True,
            "schema": music_bridge.STATE_SCHEMA,
            "windows": windows,
            "count": len(windows),
            "has_fresh_window": any(row["connected"] for row in windows),
        })
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _error(str(exc))


def _handle_music_action(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        store = _music_bridge_store()
        requested_window = args.get("window_id") or None
        windows = store.list_snapshots(max_age_seconds=_BRIDGE_FRESH_SECONDS, forget_after_seconds=music_bridge.STALE_WINDOW_SECONDS)
        fresh = [row for row in windows if row["connected"]]
        if requested_window:
            matches = [row for row in fresh if row["window_id"] == requested_window]
            if not matches:
                return _result({
                    "ok": False,
                    "reason_code": "music_window_unavailable",
                    "message": "The targeted Music renderer is not connected.",
                    "window_id": requested_window,
                })
            target = matches[0]
        elif len(fresh) == 1:
            target = fresh[0]
        elif not fresh:
            return _result({
                "ok": False,
                "reason_code": "music_window_unavailable",
                "message": "No connected Music renderer is available.",
            })
        else:
            return _result({
                "ok": False,
                "reason_code": "music_window_ambiguous",
                "message": "More than one Music renderer is connected; specify window_id.",
                "windows": [row["window_id"] for row in fresh],
            })
        action = str(args.get("action") or "").strip()
        payload = args.get("payload") or {}
        expected_revision = args.get("expected_revision")
        if expected_revision is None:
            expected_revision = target["revision"]
        context_epoch = args.get("context_epoch") or target["state"]["context_epoch"]
        record = store.enqueue_action(
            target["window_id"],
            action,
            payload,
            expected_revision=expected_revision,
            context_epoch=context_epoch,
        )
        film_events.bridge_action("music", target["window_id"], record["action_id"])
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            ack = store.consume_ack(target["window_id"], record["action_id"])
            if ack is not None:
                return _result({
                    "ok": bool(ack["ok"]),
                    "status": "acknowledged",
                    "action_id": record["action_id"],
                    "window_id": target["window_id"],
                    "reason_code": ack["reason_code"],
                    "message": ack["message"],
                    "applied": ack["applied"],
                    "revision": ack["revision"],
                    "context_epoch": ack["context_epoch"],
                })
            time.sleep(0.05)
        return _result({
            "ok": False,
            "status": "pending",
            "action_id": record["action_id"],
            "window_id": target["window_id"],
            "reason_code": "music_renderer_no_ack",
            "message": "The renderer did not acknowledge the Music action within the bounded wait.",
        })
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _error(str(exc))


def _image_bridge_store() -> Any:
    try:
        from hermes_constants import get_hermes_home

        hermes_home = Path(get_hermes_home())
    except (ImportError, OSError, TypeError, ValueError) as exc:
        raise image_bridge.ImageBridgeValidationError("Image bridge storage is unavailable") from exc
    return image_bridge.ImageBridgeStore(state_root(hermes_home / "plugins", "image-bridge"))


def _handle_image_state(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        window_id = args.get("window_id") or None
        max_age = max(0.5, min(60.0, float(args.get("max_age_seconds", _BRIDGE_FRESH_SECONDS))))
        # Only live windows (HER-048); one silent for an hour is removed on read, as on the suite bridge.
        windows = [row for row in _image_bridge_store().list_snapshots(
            window_id=window_id, max_age_seconds=max_age, forget_after_seconds=image_bridge.STALE_WINDOW_SECONDS) if row["connected"]]
        return _result({
            "ok": True,
            "schema": image_bridge.STATE_SCHEMA,
            "windows": windows,
            "count": len(windows),
            "has_fresh_window": any(row["connected"] for row in windows),
            "note": None if windows else "No Image pane has reported state. Open the Image workbench pane first.",
        })
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _error(str(exc))


def _render_spec_choices() -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("film_suite_agent_krea2_render_spec", _IMAGE_RENDER_SPEC_PATH)
    if spec is None or spec.loader is None:
        return {"samplers": [], "schedulers": []}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        "samplers": list(module.SAMPLERS),
        "schedulers": list(module.SCHEDULERS),
        "lora_max": module.LORA_MAX,
        "batch": {"min": module.V2_BATCH_MIN, "max": module.V2_BATCH_MAX},
        "size": {"min_px": module.V2_SIZE_MIN, "max_px": module.V2_SIZE_MAX,
                 "step": module.V2_SIZE_STEP, "max_pixels": module.V2_MAX_PIXELS},
    }


def _image_model_rows() -> list[dict[str, Any]]:
    """The Image models with the picker's availability (the same projection /models/catalog answers).

    ``availability`` is ``runnable`` for every wired model with its files present (a model without a
    render record says so in ``reason``; w39-p2-open); ``unavailable`` rows (missing files, a render that
    failed, a subscription not connected) carry the plain reason the picker shows.
    """
    spec = importlib.util.spec_from_file_location("film_suite_agent_model_catalog", _MODEL_CATALOG_PATH)
    if spec is None or spec.loader is None:
        return []
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    public = module.build_public_catalog(None)
    rows = []
    for family in public["families"]:
        if family["kind"] != "image":
            continue
        for variant in family["variants"]:
            evidence = variant.get("evidence") or {}
            if "availability" not in evidence:
                continue  # never offered by the Suite (no target): not a model choice
            workflow = variant.get("workflow") or {}
            rows.append({
                "family": family["id"], "family_label": family["label"], "variant": variant["id"], "label": variant["label"],
                "target_id": workflow.get("target_id") or (variant["suite_targets"] or [None])[0],
                "operations": list(variant["operations"]), "prompt_grammar": variant["prompt_grammar"],
                "availability": evidence["availability"], "reason": evidence.get("reason"),
                # w1-imgout: batch bounds and LoRA support (offered / untested / unsupported with the reason).
                **({"run": workflow["run"]} if workflow.get("run") else {}),
            })
    return rows


def _handle_image_catalog(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        axis = str(args.get("axis") or "").strip().lower() or None
        if axis is not None and axis not in image_bridge.AXES:
            raise ValueError(f"unknown axis: {axis}; expected one of {', '.join(image_bridge.AXES)}")
        query = str(args.get("query") or "").strip().lower()
        if len(query) > 80:
            raise ValueError("query is too long")
        catalog = json.loads(_IMAGE_CATALOG_PATH.read_text(encoding="utf-8"))
        rows: list[dict[str, Any]] = []
        for option in catalog.get("options", []):
            if axis and option.get("axis") != axis:
                continue
            haystack = " ".join(str(option.get(key) or "") for key in ("key", "label", "group", "source_label")).lower()
            if query and query not in haystack:
                continue
            row: dict[str, Any] = {
                "axis": option.get("axis"),
                "key": option.get("key"),
                "label": option.get("label"),
                "group": option.get("group"),
                "has_tile": bool(option.get("thumb")),
                "neutral": bool(option.get("neutral") or option.get("key") in {"none", "free"}),
                "written_for_nous_girl": bool(option.get("written_for_nous_girl")),
                "adds_text": bool(option.get("text")),
            }
            if option.get("axis") == "lora" and option.get("file"):
                row["lora"] = {"file": option["file"], "default_strength": option.get("strength"), "trigger": option.get("text") or None}
            rows.append(row)
        return _result({
            "ok": True,
            "schema": catalog.get("schema"),
            "axes": list(image_bridge.AXES),
            "options": rows,
            "count": len(rows),
            "sampling": {**(choices := _render_spec_choices()), "profiles": ["turbo", "raw"]},
            "models": _image_model_rows(),
            "notes": [
                f"Pick with continuo_image_action select_tile {{axis, key}}. LoRA is multi-select (max {choices.get('lora_max', 32)}); selecting a picked LoRA again removes it.",
                "Size and batch: set_size {format, mp | width, height, batch}; sampling.size and sampling.batch are the renderer bounds.",
                "Character has no None tile; clear_axis on character is rejected.",
                "models: availability runnable = in the picker and usable (reason says whether a render through the Suite is on record; no record is no reason to avoid it); unavailable models are not in the picker (reason says why: missing files, a failed render, not connected).",
                "models[].run (catalog targets): batch {min, max} for continuo_image_workflow_submit batch (seed + index per picture); loras.available with loras [{file, strength, role?}] from that model's family, else loras.reason says why it is refused.",
                "Options with written_for_nous_girl=true use she/her and hair wording; the pane warns when another character is picked.",
            ],
        })
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _error(str(exc))


def _pick_image_window(store: Any, requested: str | None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    fresh = [row for row in store.list_snapshots(
        max_age_seconds=_BRIDGE_FRESH_SECONDS, forget_after_seconds=image_bridge.STALE_WINDOW_SECONDS) if row["connected"]]
    if requested:
        matches = [row for row in fresh if row["window_id"] == requested]
        if not matches:
            return None, {"ok": False, "reason_code": "image_window_unavailable", "message": "The targeted Image pane is not connected.", "window_id": requested}
        return matches[0], None
    if len(fresh) == 1:
        return fresh[0], None
    if not fresh:
        return None, {"ok": False, "reason_code": "image_window_unavailable", "message": "No connected Image pane is available. Open the Image workbench pane first."}
    return None, {
        "ok": False,
        "reason_code": "image_window_ambiguous",
        "message": "More than one Image pane is connected; specify window_id.",
        "windows": [row["window_id"] for row in fresh],
    }


_VIEW_FILES_KEPT = 12


def _write_view_file(action_id: str, data_url: str) -> str | None:
    """Write a view_edit JPEG to <hermes scratch>/film-suite-views; keep the newest few."""
    import base64

    try:
        from hermes_constants import get_scratch_dir

        folder = Path(get_scratch_dir()) / "film-suite-views"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{action_id}.jpg"
        target.write_bytes(base64.b64decode(data_url.split(",", 1)[1], validate=True))
        old = sorted(folder.glob("act_*.jpg"), key=lambda item: item.stat().st_mtime)[:-_VIEW_FILES_KEPT]
        for item in old:
            item.unlink(missing_ok=True)
        return str(target)
    except Exception:  # noqa: BLE001 - the image result still carries the picture
        return None


def _view_result(store: Any, window_id: str, action_id: str, ack: dict[str, Any]) -> Any:
    """view_edit: the pane's picture as an image the model sees, beside the small acknowledgement."""
    view = store.consume_view(window_id, action_id)
    summary = {
        "ok": bool(ack["ok"]) and view is not None,
        "status": "acknowledged",
        "action": "view_edit",
        "action_id": action_id,
        "window_id": window_id,
        "reason_code": ack["reason_code"] if not ack["ok"] or view is not None else "image_view_missing",
        "message": ack["message"],
        "applied": ack["applied"],
        "revision": ack["revision"],
    }
    if not summary["ok"]:
        return _result(summary)
    # Some tool routes (the desktop tool_call bridge) pass only text on: the picture is also a
    # scratch file the agent opens with its vision tool. The one path the image tools hand out.
    view_path = _write_view_file(action_id, view["data_url"])
    if view_path:
        summary["view_path"] = view_path
    text = _result(summary)
    return {
        "_multimodal": True,
        "content": [{"type": "text", "text": text}, {"type": "image_url", "image_url": {"url": view["data_url"]}}],
        "text_summary": text,
    }


# Lane w27-rows item 3 (P2 IMG-213; owner's window 08.11.: a picture Hermes started with continuo_image_action generate
# was listed by "artist"). A bridge action carries its origin: this tool IS Hermes. The page sends the submit (its
# route call has no agent marker, so the submit noted "artist" first); the acknowledged action names the job it
# started, and that note replaces the page's one in the same job-authors table /jobs/list reads (job_authors_1.py),
# exactly as a Hermes import is listed. Reference: Resolve's render queue / Google Docs history name who queued it.
_HERMES_JOB_KEYS = {"generate": ("job_id",), "vary": ("first_job_id",)}


def _note_hermes_image_jobs(action: str, applied: Any) -> None:
    try:
        keys = _HERMES_JOB_KEYS.get(action)
        if not keys or not isinstance(applied, dict):
            return
        ids = [applied[k] for k in keys if isinstance(applied.get(k), str) and 0 < len(applied[k]) <= 160]
        if not ids:
            return
        import os
        import sqlite3
        import tempfile
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        path = Path(state_root(Path(base) / "hermes")) / "job-authors.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), timeout=5)
        try:
            with connection:
                connection.execute("CREATE TABLE IF NOT EXISTS job_authors (job_id TEXT PRIMARY KEY, by TEXT NOT NULL "
                                   "CHECK (by IN ('artist', 'hermes')), at REAL NOT NULL)")
                connection.executemany("INSERT OR REPLACE INTO job_authors (job_id, by, at) VALUES (?, 'hermes', ?)",
                                       [(job_id, time.time()) for job_id in ids])
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 - a note never fails the action
        pass


def _handle_image_action(args: dict[str, Any], **_kwargs: Any) -> Any:
    try:
        store = _image_bridge_store()
        target, problem = _pick_image_window(store, args.get("window_id") or None)
        if problem is not None:
            return _result(problem)
        action = str(args.get("action") or "").strip()
        if action in image_bridge.EDIT_PICTURE_ACTIONS and "edit" not in target["state"]:
            return _result({"ok": False, "status": "refused", "action": action, "window_id": target["window_id"], "reason_code": "image_no_edit_picture", "message": "Open a picture in Edit first (state.edit is missing)."})
        payload = args.get("payload") or {}
        expected_revision = args.get("expected_revision")
        # Lane w22-img2 (IMG-208): without the agent's own expected_revision the tool sends the revision it read.
        # When the pane's publish of a hand edit answers after that read but before the pane takes the action
        # (seconds under CPU load), the pane refuses image_state_conflict and the action is lost. That is an order
        # of answers, not a time: the tool then waits for the newer published revision and sends once more.
        # An explicit expected_revision is the agent's precondition and keeps its refusal.
        resend = expected_revision is None
        if expected_revision is None:
            expected_revision = target["revision"]
        record = store.enqueue_action(
            target["window_id"],
            action,
            payload,
            expected_revision=expected_revision,
            context_key=target["state"]["context_key"],
        )
        film_events.bridge_action("image", target["window_id"], record["action_id"])
        wait = 30.0 if action in image_bridge.SLOW_ACTIONS else 4.0
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            ack = store.consume_ack(target["window_id"], record["action_id"])
            if ack is not None and resend and not ack["ok"] and ack["reason_code"] == "image_state_conflict":
                resend = False
                newer = ack.get("revision")
                rows = store.list_snapshots(target["window_id"])
                if not (isinstance(newer, int) and newer > expected_revision):
                    newer = rows[0]["revision"] if rows and rows[0]["revision"] > expected_revision else None
                if newer is not None and rows and rows[0]["state"]["context_key"] == target["state"]["context_key"]:
                    expected_revision = newer
                    record = store.enqueue_action(target["window_id"], action, payload, expected_revision=newer,
                                                  context_key=target["state"]["context_key"])
                    film_events.bridge_action("image", target["window_id"], record["action_id"])
                    deadline = time.monotonic() + wait
                    continue
            if ack is not None:
                if action == "view_edit":
                    return _view_result(store, target["window_id"], record["action_id"], ack)
                if ack["ok"]:
                    _note_hermes_image_jobs(action, ack.get("applied"))
                return _result({
                    "ok": bool(ack["ok"]),
                    "status": "acknowledged",
                    "action": action,
                    "action_id": record["action_id"],
                    "window_id": target["window_id"],
                    "reason_code": ack["reason_code"],
                    "message": ack["message"],
                    "applied": ack["applied"],
                    "revision": ack["revision"],
                    "follow_up": (
                        "Accepted means the pane started the work; poll continuo_image_state for the job/vary cells."
                        if ack["ok"] and action in {"generate", "vary"} else None
                    ),
                })
            time.sleep(0.05)
        withdrawn = store.withdraw_action(target["window_id"], record["action_id"])
        return _result({
            "ok": False,
            "status": "withdrawn" if withdrawn else "pending",
            "action": action,
            "action_id": record["action_id"],
            "window_id": target["window_id"],
            "reason_code": "image_renderer_no_ack",
            "message": (
                "The Image pane did not pick up the action; it was withdrawn and will not be applied."
                if withdrawn else
                "The Image pane took the action but did not acknowledge it in time; read continuo_image_state before retrying."
            ),
        })
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _error(str(exc))


def _check_image_bridge_available() -> bool:
    return _IMAGE_BRIDGE_PATH.is_file()


def _check_image_catalog_available() -> bool:
    return _IMAGE_CATALOG_PATH.is_file()


def _check_music_bridge_available() -> bool:
    return _BRIDGE_PATH.is_file()


def _check_music_available() -> bool:
    return _ENGINE_PATH.is_file() and Path(music_analysis.FFMPEG).is_file()


# Lane w21-rel (AUD-144): the scope fields the hand route /music/analyze takes (MusicAnalyzeRequest).
_ROOTS_PROPERTY = {"type": "array", "items": {"type": "string"}, "description": "Optional: narrows the approved folders (never widens them)."}
_PROJECT_REF_PROPERTY = {"type": "object", "description": "Optional: {project_id} of the open project; its folders are the approved ones (else Loose work)."}

_TOOLS = (
    (
        "continuo_music_state",
        "Read the current path-free Music renderer state, including track, selection, transport readiness, playback, playhead, errors, and freshness.",
        {
            "window_id": {"type": "string", "pattern": r"^win_[0-9a-f]{32}$"},
            "max_age_seconds": {"type": "number", "minimum": 0.5, "maximum": 60, "default": 20},
        },
        [],
        _handle_music_state,
        _check_music_bridge_available,
    ),
    (
        "continuo_music_action",
        # Lane p14-sam item 2 (HER-051): the payloads come from the validator's own table (music_bridge.ACTION_PAYLOADS).
        "Request a bounded named Music action and return only a renderer acknowledgement; pending is not success. "
        + music_bridge.payload_description(),
        {
            "window_id": {"type": "string", "pattern": r"^win_[0-9a-f]{32}$"},
            "action": {
                "type": "string",
                "enum": sorted(music_bridge._ALLOWED_ACTIONS),
            },
            "payload": {"type": "object", "default": {}, "description": music_bridge.payload_description()},
            "expected_revision": {"type": "integer", "minimum": 1},
            "context_epoch": {"type": "string", "maxLength": 160},
        },
        ["action"],
        _handle_music_action,
        _check_music_bridge_available,
    ),
    (
        "continuo_music_analyze",
        "Analyze a local music recording and return compact evidence-aware tempo, meter, form, and identity data.",
        {"path": {"type": "string", "description": "Absolute path to a local audio file inside the approved folders."},
         "approved_roots": _ROOTS_PROPERTY, "project_ref": _PROJECT_REF_PROPERTY},
        ["path"],
        _handle_music_analyze,
    ),
    (
        "continuo_music_describe",
        "Describe the musical evidence inside a time range without inventing bar structure when it is unsupported.",
        {
            "path": {"type": "string"},
            "start": {"type": "number", "minimum": 0},
            "end": {"type": "number", "minimum": 0},
            "approved_roots": _ROOTS_PROPERTY,
            "project_ref": _PROJECT_REF_PROPERTY,
        },
        ["path", "start", "end"],
        _handle_music_describe,
    ),
    (
        "continuo_music_events",
        "Return beat, downbeat, accent, and section events within a local recording range.",
        {
            "path": {"type": "string"},
            "start": {"type": "number", "minimum": 0},
            "end": {"type": "number", "minimum": 0},
            "kinds": {"type": "array", "items": {"type": "string", "enum": sorted(_EVENT_KINDS)}},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            "approved_roots": _ROOTS_PROPERTY,
            "project_ref": _PROJECT_REF_PROPERTY,
        },
        ["path", "start", "end"],
        _handle_music_events,
    ),
    (
        "continuo_music_spans",
        "Find usable section-aligned ranges and, only when supported, integer-bar durations for a recording.",
        {
            "path": {"type": "string"},
            "min_s": {"type": "number", "minimum": 0.1, "default": 5},
            "max_s": {"type": "number", "minimum": 0.1, "default": 20},
            "approved_roots": _ROOTS_PROPERTY,
            "project_ref": _PROJECT_REF_PROPERTY,
        },
        ["path"],
        _handle_music_spans,
    ),
)


_IMAGE_ACTION_DESCRIPTION = (
    "Operate the visible Image workbench pane exactly as a click would; returns the pane's acknowledgement "
    "(ok/applied/message/revision), never a guess. Payloads: select_tile {axis, key} (lora toggles, max 4) · "
    "clear_axis {axis} · set_intent {text} · set_manual_prompt {text} (switches to manual; load the skill film-suite:image-prompts before writing it) · set_mode {mode: builder|manual} · "
    "set_sampling {profile?, steps?, cfg?, sampler?, scheduler?, negative?} (negative only on raw) · "
    "set_lora_strength {file, strength 0..2} (card strength; raw applies x0.6) · set_seed {seed} · "
    "set_seed_mode {mode: fixed|increment|decrement|random} (Generate > Seed: what the seed does after each run) · "
    "set_size {format? (catalog ratio like 16:9, or custom), width?, height?, mp? (megapixels, catalog bounds at this aspect), batch?, swap?, aspect_lock?} "
    "(Generate > Format/Size/MP/Batch; state.panel reports seed_mode and size) · generate {} · "
    "keep {name, job_id?} · vary {variable: seed|steps|cfg|sampler|scheduler|profile|lora:<key>|axis:<style|shot|scene|outfit>, values[2..4]} · "
    "vary_stop {} · restore {revision_id} (Use these settings) · open_result {job_id} · start_fresh {} · "
    "run_command {id} (one of state.commands: the rows Image adds to the app's Ctrl+K palette while it is on screen, "
    "run exactly as a click on the row; a row with enabled false is refused with its why) · "
    "set_canvas {zoom?: fit|fill|0.05..32, center?: [x, y] 0..1 of the picture, viewer?: bool (full view: the picture alone)} · "
    "compare {a, b (job ids: the canvas picture, a take picture, History, Kept or Recent), mode?: side|slider, linked?: bool, wipe?: 0..1 (share of A), zoom?: fit|10..3200 (% of A; B follows when linked), swap?: bool} | {mode?, linked?, wipe?, zoom?, swap?} (change the open Compare) | {close: true} "
    "(Compare view: two pictures side by side or under one wipe slider, linked zoom and position, labels name only the settings that differ; Esc closes; state.compare {a, b, mode, linked, zoom, wipe, differs, a_label, b_label, loading}) · "
    "survey {ids: [2..12 job ids]} | {active: id} | {remove: id} (never below 2) | {pick: id} (opens it on the canvas, Survey closes) | {close: true} "
    "(Survey view: the pictures in one fitted grid, each labelled with what differs; state.survey {ids, active, differs, labels, loading}) · "
    "set_develop {light?: {exposure -5..5 EV, contrast|highlights|shadows|whites|blacks -100..100}, "
    "color?: {temp|tint -100..100 (relative to the picture as rendered: temp + warmer, tint + magenta), "
    "vibrance|saturation -100..100}, "
    "mixer?: {<band><Hue|Sat|Lum> -100..100 for band red|orange|yellow|green|aqua|blue|purple|magenta, e.g. orangeSat, blueLum, greenHue (the Color Mixer, like Lightroom's HSL: shifts only the colours of that band; Hue +100 turns the band towards the next band)}, "
    "grading?: {shadowsSat|midtonesSat|highlightsSat|globalSat 0..100 (tint strength), shadowsHue|midtonesHue|highlightsHue|globalHue "
    "0..360 (degrees, 0 red, 120 green, 240 blue), shadowsLum|midtonesLum|highlightsLum|globalLum -100..100, "
    "blending 0..100 (50; wider overlap of the regions), balance -100..100 (+ gives the highlights more of the picture)}, "
    "detail?: {sharpen 0..150, sharpenMasking 0..100 (0 sharpens everything; higher spares flat areas such as skin and sky), "
        "nrLuminance 0..100 (noise reduction), nrDetail 0..100 (default 50; higher keeps more fine detail), nrColor 0..100 (colour noise), "
        "nrColorDetail 0..100 (default 50), nrColorSmoothness 0..100 (default 50; wider colour smoothing)}, "
    "curve?: {highlights|lights|darks|shadows -100..100 (the tone curve regions, brightest to darkest), "
    "splitShadows 10..70 (25), splitMid 20..80 (50), splitHighlights 30..90 (75), "
    "refineSaturation 0..100 (100; lower pulls back the saturation a contrast curve adds), "
    "master|red|green|blue: point curves [[x, y], ...] in 0..1 (input, output), 2..16 points, sorted by x, [] = straight; "
    "master acts on all channels after the regions, then red/green/blue per channel}, "
    "effects?: {texture|clarity|dehaze -100..100, vignette -100..100 (- darkens corners) with vigMidpoint|vigFeather 0..100 "
    "(50), vigRoundness -100..100, vigHighlights 0..100, grain 0..100 with grainSize 0..100 (25), grainRoughness 0..100 (50)}} "
    "(any subset of any group; non-destructive on the picture open in Edit, "
    "one undo step) · reset_develop {group?: light|color|mixer|grading|curve|detail|effects} (one group, or all; one undo step) · "
    "set_develop_before {on} (the canvas shows the Original, like the backslash key; state.develop.before; any change ends it) · "
    "set_develop_clipping {shadows?, highlights?} (paint clipped shadows blue and clipped highlights red over the picture, like Lightroom's J; a view only: not in History, not in view_edit or the histogram; state.develop.clipping) · "
    "set_develop_preset {op: copy|paste|save|apply|delete|preview|preview_end|export|import, name? (save/apply/delete/preview; 1-40 chars), names? (export: the presets to write, default all), file? (import: the text of a preset file, schema studio.image-develop-presets/1; a name that exists gets \" (2)\"; a bad file is refused with the reason), groups? (copy only: [light|color|mixer|grading|curve|detail|effects, ...], like Copy…; remembered as the next Copy… choice)} (Adjust > Presets: preview draws a preset on the picture like hovering its name (no History row, the values stay; state.develop.presets.preview), preview_end restores the picture; copy takes every adjustment group of the picture (or the chosen groups), paste puts exactly the copied groups on the open picture (overwrite); a preset keeps only the groups that were changed when it was saved and apply sets just those; masks, generative layers and the crop are never copied; one undo step \"Paste Settings\" / \"Preset: <name>\"; kept for every picture; refused (image_preset_refused) with the reason; state.develop.presets {names, current, copied}) · "
    "set_develop_white_balance {mode: as_shot|auto|daylight|cloudy|shade|flash (presets: Temp -44 / -9 / +28 / -35 from the 6500 K render, Tint 0)} | {x, y} (0..1 of the picture: the picker on a 5 x 5 px spot) | {x, y, w, h} (an area) | {picking: bool} (the picker on the canvas for the artist; W in Adjust) (sets Color Temp and Tint so that spot reads neutral grey, like a raw developer's white-balance picker; As Shot = Temp 0 / Tint 0, the picture as rendered; Auto = grey world over the unclipped pixels; one undo step \"White Balance: Picked|Auto|As Shot\"; refused (image_white_balance_missed) on a blown-out or black spot; applied.clamped when Temp/Tint reach their end; state.develop.white_balance {mode as_shot|auto|daylight|cloudy|shade|flash|custom, picking}) · "
    "targeted_adjust {group: curve|mixer, x, y (0..1 of the visible picture, after the crop), amount -255..255, channel?: master|red|green|blue (curve; default the channel shown), property?: hue|saturation|luminance (mixer; default the tab shown)} "
    "(Lightroom's Targeted Adjustment tool as one drag: amount is the drag in screen px, up positive; on the Tone Curve one unit lifts the point's output by 1/255; "
    "the input is the value that pixel has where it enters the curve, so the same spot keeps hitting the same point; a point within 6/255 of it moves, else one is added; "
    "the develop values equal those of the same drag in the panel; one undo step \"Point Curve (Hermes)\"; applied {channel, input 0..1, added, points}; "
    "on the Color Mixer the pixel's hue (where it enters the mixer) picks its two neighbouring bands with the mixer's own weights (summing to 1) and each band's property moves by round(amount x weight) slider steps, "
    "e.g. an orange spot moves Orange and a little Yellow; one undo step \"Orange + Yellow Hue (Hermes)\"; applied {property, bands [{band, weight, value}]}; "
    "refused (image_targeted_refused) at 16 points or on a neutral grey spot (the mixer leaves greys alone)) · "
    "auto_tone {sliders?: [exposure|contrast|highlights|shadows|whites|blacks]} (Light > Auto, Lightroom's Auto / Ctrl+U: the Light "
    "sliders from the picture's histogram, the widest tonal scale without clipping; all six, or only the named ones like "
    "Shift+double-click on a slider; one undo step \"Auto Tone\"; it reads the picture, not the sliders: a second call changes nothing; "
    "applied.light has the Light values) · "
    "set_develop_group {group: light|color|mixer|grading|curve|detail|effects, on} (switch one group off and on like Lightroom's panel switch: its values are kept, it draws as neutral; state.develop.off lists the groups that are off; moving one of its values turns it on; one undo step) · "
    "set_develop_masks {masks?, select?: {id, component? 0..7 (0)} (select a mask and component, like a click on its pin), pins?: bool (Show pins), overlay?: {show?, mode? red (colour)|mask (white on black)|bw (colour on black and white)|black|white (the picture on black / white)|edge, colour? red|green|blue|cyan|magenta, opacity? 0..100 (50)} (how the selected mask shows, remembered); masks: [{components: [{kind: linear, a: [x, y], b: [x, y]} | {kind: radial, c: [x, y], rx, ry, angle? -180..180 (0), feather? 0..100 (50)} "
    "| {kind: subject|background|sky} | {kind: object, points: [{x, y, label? 1 keep|0 leave out}] | box: [x, y, w, h]} | {kind: words, text} "
    "| {kind: brush, strokes: [{points: [[x, y], ...] (1..1000, 0..1 of the picture), size? 1..100 (10, the diameter in 1/200 of the long edge), feather? 0..100 (50), flow? 1..100 (100), density? 1..100 (100), mode? add|erase (add)}] (at most 100 strokes, painted in order: a stroke builds up by flow to its density, strokes add by max, erase strokes paint out)} "
    "| {kind: color, samples: [{x, y, w?, h?}] (1..5, 0..1 of the picture: a point reads a 5 x 5 px spot, w/h an area), refine? 0..100 (50: how far in OkLab a colour still counts)} "
    "| {kind: luminance, range?: [lo, hi] (0..1 lightness after white balance and global exposure, [0.5, 1]: fully in between), smooth? 0..100 (20: the fall-off outside the band, up to 0.5)} "
    "| {kind: depth, range?: [lo, hi] (0 near..1 far, [0, 0.5]), smooth? 0..100 (20)} (the picture's depth from the geometry job, read once per picture; state shows status reading|ready|failed), "
    "each with op? add|subtract|intersect (add) and invert?; AI shapes also grow? -100 in..100 out (0, up to 3 % of the long edge) and edge? -100 hard..100 soft (0)], id?, name?, on?, invert?, amount? 0..200 (100), refine? 0..100 (0, snaps the mask's edge to the picture), "
    "local?: {exposure -4..4, temp|tint|contrast|highlights|shadows|whites|blacks|texture|clarity|dehaze|hue|saturation -100..100}}, ...]} "
    "(replaces the whole mask list, at most 8 masks of 1..8 shapes; positions 0..1 of the picture from its top left, may reach past it -1..2, "
    "rx/ry a fraction of the long edge; a linear gradient is full at a and gone at b; shapes combine in order; "
    "local values add to the global ones where the mask is; keep a mask's id to change it (omitted fields take their defaults), [] removes every mask; one undo step; "
    "AI shapes are found by the image engine after the call: state.develop.masks gives each one status finding|found|failed (with reason), and a found one keeps its answer while its request stays the same; "
    "state.develop.masks lists them) · "
    "set_edit_mode {mode: adjust|masks|generate|crop} (the Edit panel on the right; state.develop.mode) · "
    "set_edit_crop {crop?: {x, y, w, h} | null, angle? -45..45 (degrees clockwise, 0.01 steps), straighten?: {x0, y0, x1, y1} (a line along the horizon or a vertical, 0..1 of the turned picture; the angle follows), rotate?: left|right (a quarter turn), flip?: horizontal|vertical, size?: {w, h} (px of the crop, keeps its centre, stays on the picture)} "
    "(Edit > Crop like Lightroom's Crop & Straighten and Photo > Rotate / Flip; at least one key; applied in the order crop, rotate, flip, angle, straighten, size; "
    "crop is 0..1 of the picture as it is turned now, null = Reset crop (the whole Original, no turn); an angle shrinks the frame so no empty corner shows (Constrain to image); masks and layers stay on the picture; "
    "one undo step; the Original never changes; state.develop.crop {x, y, w, h, angle?, rot? 0..3, flip?, px}; view_edit and accept_edit show the crop) · "
    "generate_edit {operation: fill|replace|remove|restyle|relight, prompt? (required for fill, replace, restyle and relight; restyle: the style; relight: the new light, e.g. warm golden light coming from the left), target? (replace: what goes; the new object fills exactly the selection), variants? 1..4, seed?, mask? (a mask id from state.develop.masks, as shown (1) or as text (\"1\"), or \"all\" for the whole picture), model? (qwen-image-2.1 = Qwen-Image Edit, the default and the better one by measure; flux3-klein = FLUX.3 Klein, the alternative; flux2-klein = FLUX.2 Klein, the fast one, about 2-3 s, weaker in Fill and Remove; krea2 = Krea 2 Edit; a model listed in state.develop.generative.warnings for the operation runs too, and the answer's applied.warning names what the matrix measured), strength? 0..1 (not relight; 1 = new content, the area pre-filled neutral; lower keeps more of what is there, 0 = the picture as it is; default fill, remove and replace 1, restyle 0.7; only a model whose state.develop.generative.strength.takes is true takes it, otherwise refused with the reason), sky? (restyle in a Sky mask = Photoshop Sky Replacement: {shift_edge -20..20 px (default 2, into the subject), fade_edge 0..20 (1), brightness and temperature -100..100 of the new sky (0), lighting_mode multiply|screen, lighting 0..100 (50) and color 0..100 (50): how strongly the foreground is lit and coloured like the new sky, haze 0..100 (40): the soft haze band joining sky and water at the horizon, reflect 0..100 (70): how strongly open water mirrors the new sky}; all 0 keeps everything outside the mask byte-identical)} "
    "(Edit > Generative inside the CURRENT selection - select first - or inside mask `mask` (replace a background: a Background mask, then fill with the new scene); the image engine takes about a minute; returns at once, "
    "follow state.develop.generative.job; the result is a layer under develop in state.develop.generative.layers, the Original is never changed) · "
    "outpaint_edit {left?, top?, right?, bottom? (pixels of new canvas per side, each at most 2 x that picture side; omitted sides keep the panel's value), prompt? (optional: what the new area shows), variants? 1..4, seed?, model? (an edit model family from state.develop.generative.models)} "
    "(Edit > Crop > Outpaint: the picture on a larger canvas, the image engine paints the new margin and continues the scene; no selection needed; "
    "returns at once with the canvas size, follow state.develop.outpaint.job; the variants are in state.develop.outpaint.result) · "
    "accept_outpaint {variant? (default the picked one), name?} (keeps that Outpaint variant - the whole larger picture - as a new version and opens it in Edit with the develop settings carried over; the Original stays) · "
    "discard_outpaint {} (drops the Outpaint result shown on the stage; the margins stay) · "
    "set_edit_light {light? 1..3 (which light; it becomes the selected one, default the selected one), add? true (add a light, at most 3; it becomes the selected one), remove? true (remove that light; at least one stays), on? (switch that light on or off; with all off the picture keeps its own light), tone? (warm golden|neutral white|cool blue|soft daylight|dim, moody: the presets), color? (#rrggbb: a free colour), kelvin? 1700..12000 (a colour temperature; Candle 1900, Tungsten 2850, Halogen 3200, Fluorescent 3800, Daylight 5500, Overcast 6500, Shade 7500; one of tone, color, kelvin), from? (from the left|from the right|from the front|from behind|from above|from below), x?, y? (-1..1: where the light stands as seen from the camera, x right, y up), behind? (the light is behind the subject), strength? 0..2, softness? 0..1 (per light: also the softness of its shadow), contrast? 0.5..4, fill? 0..1, shadows? 0..1 (how dark the shadows the lights cast from the picture's depth are; 0 = none), background? 0..1 (the share of the new light on the background around the subject: 1 = like the subject, 0 = the background keeps its own light; only with a subject) (the scene), live? (show the light on the picture)} (opens Generative > Relight and sets the live light on the picture's shape; state.develop.generative.light lists every light in lights and the selected one; read it back in state.develop.generative.light, whose shape says whether the shape is read; generate_edit {operation: relight} without a prompt then renders from that light, the edit model following the lit picture) · "
    "stop_edit {} (stop the running generative edit or Outpaint; variants already finished stay) · "
    "accept_edit {name?} (Accept as version: the picture exactly as Edit shows it - adjustments, masks, shown layers, crop - is kept as a new version "
    "(the next version of a kept picture's work, else a new work named after the prompt) and opens in Edit; the Original stays; returns job_id and revision_id) · "
    "export_edit {format? png8|png16|jpeg|webp|tiff8|tiff16, quality? 1-100, size? {mode original|long_edge|percent, value}, metadata?} "
    "(File > Export of the picture as Edit shows it into exports/images; continuo_image_export without pixels refuses export_has_edits when there are edits) · "
    "set_edit_layer {layer, variant? 0..3, on?, delete?} (pick a variant, hide or delete a generative layer; one undo step) · "
    "remove_background {} (Edit > Generative > Remove background: the image engine finds the subject and a layer that is only a layer mask hides the background - "
    "the picture gets transparency, the Original stays; follow state.develop.generative.layers, operation remove_background; accept_edit keeps a PNG with alpha) · "
    "set_layer_mask {layer, invert?, feather? 0..250 px, density? 0..100 %} (the layer mask of a remove_background layer, like Photoshop's Properties > Masks; one undo step) · "
    "regenerate_layer {layer} (a generative layer whose input changed shows stale true - Source changed; re-runs its operation, prompt, selection, edit model, strength, variants and seed from the pixels below it now into the same layer; one undo step) · "
    "set_edit_selection {op: all|none|invert|grow|shrink|feather|border|smooth|reselect|quick_mask|transform|save|load, px? (grow/shrink 1..500, feather 1..250, border 1..200, smooth 1..100; default the Modify dialog's last value), on? (quick_mask), scale?|scale_x?|scale_y? % / rotate? degrees / dx?|dy? px (transform, about the selection box centre), name? (save; load needs it: a name from the saved list), mode? new|add|subtract|intersect (load)} "
    "(Select > All / Deselect / Inverse and Select > Modify > Expand, Contract, Feather, Border, Smooth on the Edit selection, the same code as the menu and the dialog's OK; one undo step; "
    "state.edit.selection {has, bounds {x, y, w, h} px of the Original, area_px, feathered, tool, refining, mask_crc32}; Generative and Outpaint use this selection) · "
    "set_edit_tool {tool: move|hand|rect|lasso|object|brush|eraser|box|wand} (picks the rail tool like a click; never changes the Edit mode; state.edit.selection.tool) · "
    "set_edit_boxes {boxes?: [{id?, intent add|change|move|keep|remove, desc?, bbox [x, y, w, h] picture px, target_bbox? (move)}] (replaces the list), prompt? (refer to a box as <id>), model?, color_match?, limit? off|selection (selection = the current selection is a hard mask: nothing outside it changes), variants? 1-4, seed?} (opens the Box tool; only the intents state.edit.boxes.intents offers) · "
    "find_edit_boxes {query} (Find… with the local detector: adds a box per hit with the current intent) · run_edit_boxes {variants?, seed?} (Apply: one box edit, the result is a layer; state.edit.boxes.run_why says why it waits) · "
    "refine_edit_selection {radius? 0..250, smart?, smooth? 0..100, feather? 0..250, contrast? 0..100, shift? -100..100, output? selection|png, apply? (default true)} | {cancel: true} "
    "(Select and Mask: opens it with these fields, the rest as the dialog last had them; apply = OK, one undo step; apply false leaves it open for the artist to see; cancel closes it without changes) · "
    "edit_undo {} · edit_redo {} · edit_history_jump {at} (Edit History, shared by selection and develop; "
    "state.develop.history lists the steps, at = current) · view_edit {edge? 256..2048, default 1024} "
    "(returns the Edit picture as shown now, every adjustment applied, as an image; if no image arrives, open "
    "view_path with your vision tool). "
    "generate/vary ack when the pane accepted the submit, not when the picture exists: poll continuo_image_state. "
    "Pending is not success."
)

_IMAGE_TOOLS = (
    (
        "continuo_image_state",
        "Read the Image workbench pane as the user sees it: mode, selected tiles per axis, intent, compiled prompt "
        "(words, omitted, warnings), render_spec, current job and result, vary strip cells, last 20 kept pictures, "
        "develop (Light, Color, Color Mixer, Grading, Tone Curve, Detail and Effects values and Edit History of the picture open in Edit; absent without one), freshness. Path-free.",
        {
            "window_id": {"type": "string", "pattern": r"^win_[0-9a-f]{32}$"},
            "max_age_seconds": {"type": "number", "minimum": 0.5, "maximum": 60, "default": 20},
        },
        [],
        _handle_image_state,
        _check_image_bridge_available,
    ),
    (
        "continuo_image_catalog",
        "List the Image builder options (axis, key, label, LoRA file and default strength, Nous-Girl wording flag) plus "
        "allowed samplers/schedulers, so tiles can be chosen by name, plus the Image models with the picker's availability "
        "(runnable = wired with its files present; the reason says whether a render is on record).",
        {
            "axis": {"type": "string", "enum": list(image_bridge.AXES)},
            "query": {"type": "string", "maxLength": 80, "description": "Case-insensitive match on key, label or group."},
        },
        [],
        _handle_image_catalog,
        _check_image_catalog_available,
    ),
    (
        "continuo_image_action",
        # Lane w19-g7hermes item 4 (HER-052): the payloads also come from the validator's own table (image_bridge.ACTION_PAYLOADS).
        _IMAGE_ACTION_DESCRIPTION + " " + image_bridge.payload_description(),
        {
            "window_id": {"type": "string", "pattern": r"^win_[0-9a-f]{32}$"},
            "action": {"type": "string", "enum": sorted(image_bridge.ALLOWED_ACTIONS)},
            "payload": {"type": "object", "default": {}, "description": image_bridge.payload_description()},
            "expected_revision": {"type": "integer", "minimum": 1},
        },
        ["action"],
        _handle_image_action,
        _check_image_bridge_available,
    ),
)


def _suite_bridge_store() -> Any:
    try:
        from hermes_constants import get_hermes_home

        hermes_home = Path(get_hermes_home())
    except (ImportError, OSError, TypeError, ValueError) as exc:
        raise suite_bridge.SuiteBridgeError("Suite bridge storage is unavailable") from exc
    return suite_bridge.SuiteBridgeStore(state_root(hermes_home / "plugins", "suite-bridge"))


def _handle_suite_state(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        max_age = max(0.5, min(60.0, float(args.get("max_age_seconds", _BRIDGE_FRESH_SECONDS))))
        # HER-045: only connected windows; windows silent for an hour are removed by the read itself.
        windows = [row for row in _suite_bridge_store().snapshots(max_age_seconds=max_age) if row["connected"]]
        return _result({
            "ok": True, "schema": suite_bridge.STATE_SCHEMA, "windows": windows, "count": len(windows),
            "has_fresh_window": bool(windows), "page_labels": suite_bridge.PAGE_LABELS,
            "note": None if windows else "No Continuo page is connected.",
        })
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _error(str(exc))


def _handle_suite_action(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        store = _suite_bridge_store()
        fresh = [row for row in store.snapshots(max_age_seconds=_BRIDGE_FRESH_SECONDS) if row["connected"]]
        requested = args.get("window_id") or None
        followed = None
        if requested:
            named = [row for row in fresh if row["window_id"] == requested]
            # Lane w24-rest (owner 07.11. 09:47): the page reconnected under a new window id by itself (a renderer reload
            # mints a new id) and the action on the old id answered "No connected Continuo page". A gone id with exactly
            # ONE connected page targets that page and the answer names it (followed_from); two or more stay ambiguous.
            if not named and len(fresh) == 1:
                followed = requested
            else:
                fresh = named
        if len(fresh) != 1:
            return _result({"ok": False, "reason_code": "suite_window_unavailable" if not fresh else "suite_window_ambiguous",
                            "message": "No connected Continuo page." if not fresh else "More than one Continuo page is connected; specify window_id.",
                            "windows": [row["window_id"] for row in fresh]})
        target = fresh[0]
        action = str(args.get("action") or "").strip()
        record = store.enqueue(target["window_id"], action, args.get("payload") or {})
        film_events.bridge_action("suite", target["window_id"], record["action_id"])
        # open_project loads the project before the page acknowledges (the Recent tile's /projects/open-recent).
        deadline = time.monotonic() + (_SUITE_OPEN_PROJECT_ACK_SECONDS if action == "open_project" else 4.0)
        while time.monotonic() < deadline:
            ack = store.consume_ack(target["window_id"], record["action_id"])
            if ack is not None:
                return _result({"ok": bool(ack["ok"]), "status": "acknowledged", "action": action, "action_id": record["action_id"],
                                "window_id": target["window_id"], "reason_code": ack["reason_code"], "message": ack["message"], "applied": ack["applied"],
                                **({"followed_from": followed} if followed else {})})
            time.sleep(0.05)
        withdrawn = store.withdraw(target["window_id"], record["action_id"])
        return _result({"ok": False, "status": "withdrawn" if withdrawn else "pending", "action": action, "action_id": record["action_id"],
                        "window_id": target["window_id"], "reason_code": "suite_renderer_no_ack",
                        "message": "The page did not pick up the action; it was withdrawn." if withdrawn else "The page took the action but did not acknowledge it in time."})
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return _error(str(exc))


def _check_suite_bridge_available() -> bool:
    return _SUITE_BRIDGE_PATH.is_file()


_SUITE_TOOLS = (
    (
        "continuo_suite_state",
        "Read the Continuo frame as the artist sees it: the open page (start, script, music=Audio, image, shoot=Video, "
        "timeline=Cut, deliver), the screen (start, page, media), the project, and the Script page (state none|ready|loading|error|no_project, "
        "revision, title, scene list number + heading + act (the author's act mark the scene belongs to, e.g. AKT 1)), and on Image, Video, Audio and Cut the Media column: media {visible, bin all|kept|cut|uncut, "
        "types (image|video|audio shown), query, sort, min_rating, count (items shown), total (in the bin), selected (item id or null)}; media is null "
        "on other pages. On Video (page shoot) video {take: {take_id, barely_moves, motion_activity} | null}: the take on the stage; "
        "barely_moves true = little motion was measured (the stage does not say so): check the take, a locked shot with a small moving subject is right; "
        "re-roll (continuo_video_submit, seed + 1, same args) only when the prompt asked for motion. "
        "On Start (page start) start {recent: [{project_id, name, available}], busy: ''|restore|open} lists its Recent tiles, "
        "also before any project is chosen. Only connected windows are listed. page_labels maps ids to the bar's names. Path-free.",
        {"max_age_seconds": {"type": "number", "minimum": 0.5, "maximum": 60, "default": 20}},
        [],
        _handle_suite_state,
        _check_suite_bridge_available,
    ),
    (
        "continuo_suite_action",
        "Act on the Continuo frame like a click on its bar: open_page {page: start|script|music|image|shoot|timeline|deliver}. "
        "Like Start's tiles: open_project {project_id} (a start.recent entry with available true) or no_project (Loose work); on another page "
        "they switch the open window's project the way the bar's project chip does (unsaved_changes when the artist would be asked first: "
        "nothing changes, the question stays for the artist; project_switch_blocked while Video work runs); "
        "a new project or a folder is the artist's own pick and has no action. "
        "The Media column (Image, Video, Audio, Cut; the same handlers as its controls): media_filter {bin?: all|kept|cut|uncut, types?: [image|video|audio], "
        "query?, sort?: newest|oldest|rating|kept|name}; media_select {id} (an item the column shows, as media.selected names it: imgjob_…, studio:… or a folder file's ref folder:<32 hex> from continuo_media_list, never its path; on Image a picture opens on the stage, a clip in Media); "
        "media_toggle {visible} (Hide/Show; in the narrow tile the drawer). Elsewhere the answer is media_unavailable. "
        "On Cut: cut_inspector {visible} hides or shows the docked inspector like its Hide button and Ctrl+[ (state cut.inspector_visible; "
        "cut_unavailable on another page, cut_too_narrow when the window docks no inspector). "
        "Unsaved work asks first exactly as for the artist (the answer says so); Deliver opens only when the cut has clips. Pending is not success. "
        "On the open Script page (state script.current_scene/element/focus_mode/typewriter) the editor's own commands, the same runs as its "
        "palette rows: script_set_element {type: heading|action|character|parenthetical|dialogue|transition|shot}, script_toggle_dual, "
        "script_previous_scene, script_next_scene, script_go_to_scene {scene}, script_find, script_find_next {query, backwards?}, "
        "script_replace / script_replace_all {query, replacement}, script_undo, script_redo, script_paste_fountain {text}, "
        "script_focus_mode {on?}, script_typewriter {on?}, script_shots {on?} (View > Shots), script_inspector {on?} (the inspector shown or hidden, as its ✕ and Ctrl+[; state script.inspector), script_export {format: pdf|fountain|fdx}, script_frame {shot: sh-...} (+ Frame: Image Create for a shot without picture, prompt from the shot line and heading; the take kept next becomes its picture). script_hermes_undo {change?} / script_hermes_accept {change?} / script_hermes_undo_all / script_hermes_accept_all (your Script changes under review: the artist sees each changed run green with the old text struck; undo or accept one change by its id, or all; only when the artist asks; state script.hermes_did {label, at, before, after, current, changes [{id, scene, before, after}]}), script_hermes_dismiss (= accept all). Text changes by Hermes go through "
        "the continuo_script_* verbs instead (they reach the open page by script.changed).",
        {
            "window_id": {"type": "string", "pattern": r"^win_[0-9a-f]{32}$"},
            "action": {"type": "string", "enum": sorted(suite_bridge.ALL_ACTIONS)},
            "payload": {"type": "object", "default": {}, "description": suite_bridge.payload_description()},
        },
        ["action"],
        _handle_suite_action,
        _check_suite_bridge_available,
    ),
)


def _register_suite_tools(ctx: Any) -> None:
    for name, description, properties, required, handler, check_fn in _SUITE_TOOLS:
        if name == "continuo_suite_action":
            # Lane w19-g7hermes item 4 (HER-052): the payloads from the validator's own table (suite_bridge.ACTION_PAYLOADS).
            description = f"{description} {suite_bridge.payload_description()}"
        ctx.register_tool(
            name=name,
            toolset="continuo_suite",
            schema={"name": name, "description": description,
                    "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}},
            handler=handler,
            check_fn=check_fn,
            emoji="▤",
        )


def _register_image_tools(ctx: Any) -> None:
    for name, description, properties, required, handler, check_fn in _IMAGE_TOOLS:
        ctx.register_tool(
            name=name,
            toolset="continuo_image",
            schema={
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
            handler=handler,
            check_fn=check_fn,
            emoji="▣",
        )


def register(ctx: Any) -> None:
    for entry in _TOOLS:
        if len(entry) == 6:
            name, description, properties, required, handler, check_fn = entry
        else:
            name, description, properties, required, handler = entry
            check_fn = _check_music_available
        ctx.register_tool(
            name=name,
            toolset="continuo_music",
            schema={
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            },
            handler=handler,
            check_fn=check_fn,
            emoji="♫",
        )
    _register_image_tools(ctx)
    _register_suite_tools(ctx)
    spec = importlib.util.spec_from_file_location(
        "film_suite_agent_route_tools", Path(__file__).with_name("agent_tools.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Continuo route tools are unavailable")
    route_tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(route_tools)
    route_tools.register_route_tools(ctx)
    _register_skills(ctx)


# Lane w19-g7hermes item 1 (HER-063; Hermes plugin guide "Bundle skills"): the Continuo skill routes intent to the tool
# families above; Hermes loads it as film-suite:continuo. Hosts without plugin skills keep the tools only.
_SKILLS_DIR = Path(__file__).with_name("skills")
_SKILLS = (
    ("continuo", "Use when the artist works on a film, script, shot, cut or song in Continuo."),
    # Lane w39-skill: how to write prompts for the models the package ships (loaded as film-suite:<name>).
    ("video-prompts", "Use when writing or repairing a MiniMax H3 video prompt for a Continuo video take."),
    ("image-prompts", "Use when writing or repairing a prompt for a Continuo image model (create or edit)."),
)


def _register_skills(ctx: Any) -> None:
    register_skill = getattr(ctx, "register_skill", None)
    if not callable(register_skill):
        return
    for name, description in _SKILLS:
        register_skill(name, _SKILLS_DIR / name / "SKILL.md", description=description)
