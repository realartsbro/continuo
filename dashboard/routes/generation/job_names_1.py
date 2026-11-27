from __future__ import annotations

# --- A job's label = where it runs + its workflow (lane w14-shell item 3; GEM-063, GEM-085; frame decision 6) -------
# Reference: DaVinci Resolve's render queue and Media Encoder's queue name a job by its preset (where and how it
# renders) beside its output file name, never by its content. /jobs/list answers per job, besides its file name
# (result_name), ``workflow_label`` = "<runs on> · <workflow>", read from the registered catalogs, so it scales to any
# workflow and no model title is written here:
#   runs on   the generation registry's provider display label for the target (comfyui-local -> "Local");
#   workflow  the registry's target label ("MiniMax H3 · Frames"), else the model catalog's family + variant label
#             whose suite_targets name the target (the family alone when several variants share one target:
#             "Krea 2"). A target neither catalog knows answers its id; no target answers None.
# Read once per process (both catalogs are static files); Hermes' continuo_jobs_list reads the same field.


def _job_workflow_labels() -> dict[str, str]:
    cached = globals().get("_JOB_WORKFLOW_LABELS_CACHE")
    if isinstance(cached, dict):
        return cached
    labels: dict[str, str] = {}
    runs_on: dict[str, str] = {}
    local = "Local"
    try:
        for provider in _generation_registry.catalog().get("providers", []):
            where = provider.get("display_label") or provider.get("label") or provider.get("id")
            if provider.get("location") == "local":
                local = where
            for target in provider.get("targets", []):
                runs_on[target["id"]] = where
                labels[target["id"]] = target.get("label") or target["id"]
    except Exception:  # noqa: BLE001 - without the registry the model catalog still names
        pass
    try:
        families = _model_catalog_module.catalog_view().get("families", [])
    except Exception:  # noqa: BLE001 - without the model catalog the registry's labels stay
        families = []
    shared: dict[str, list[tuple[str, str]]] = {}
    for family in families:
        for variant in family.get("variants", []):
            for target_id in variant.get("suite_targets") or []:
                shared.setdefault(target_id, []).append((str(family.get("label") or family.get("id")), str(variant.get("label") or variant.get("id"))))
    for target_id, named in shared.items():
        family_names = {f for f, _v in named}
        if target_id in labels:
            continue  # the registry names its own targets (each workflow its own label)
        labels[target_id] = named[0][0] + " · " + named[0][1] if len(named) == 1 else (named[0][0] if len(family_names) == 1 else target_id)
    # Lane w27-rows item 2 (P1 IMG-216; maintainer window: a P-H3-Image Create job read "p-h3-image-lora-t2i-v1"):
    # an Image workflow the model catalog offers through its workflow descriptor (no suite target) is named by its
    # family, as the Image page's Model field names it: the family's one Create variant is the family alone
    # ("P-H3-Image", "FLUX.2 Klein"), any other variant family + variant ("P-H3-Image Edit", "Krea 2 Identity Edit V2").
    for family in families:
        variants = family.get("variants", [])
        creates = [v for v in variants if list(v.get("operations") or []) == ["create"]]
        for variant in variants:
            target_id = ((variant.get("workflow") or {}).get("descriptor") or {}).get("target_id")
            if not isinstance(target_id, str) or not target_id or target_id in labels:
                continue
            name = str(family.get("label") or family.get("id"))
            alone = len(creates) == 1 and variant is creates[0]
            labels[target_id] = name if alone else f"{name} {variant.get('label') or variant.get('id')}"
    # Lane w26-player (GEM-056): an Image edit workflow (Fill/Replace/... on Qwen-Image 2.1 Edit, FLUX Klein Edit) is
    # named by its edit-capabilities family label, as the Edit panel's Model field names it ("Local · Qwen-Image 2.1
    # Edit"), never by its workflow id. Reference: Resolve's render queue names a job by its preset.
    try:
        edit_contract = globals().get("_edit_contract_module")
        for family in (edit_contract().load_capabilities().get("families", []) if callable(edit_contract) else []):
            for operation in (family.get("operations") or {}).values():
                target_id = operation.get("target_id") if isinstance(operation, dict) else None
                if isinstance(target_id, str) and target_id and target_id not in labels and family.get("label"):
                    labels[target_id] = str(family["label"])
    except Exception:  # noqa: BLE001 - without the edit capabilities the other catalogs still name
        pass
    # A model-catalog target the registry does not list (the Image workflows) runs on the local provider.
    out = {target_id: f"{runs_on.get(target_id, local)} · {label}" for target_id, label in labels.items()}
    # Lane w27-rows item 2: a picture brought in (Import… on Image or Media, Hermes' continuo_image_import) is no model
    # run: its row reads "Import · shot-06.png". The stored workflow id stays (jobs written before keep resolving).
    import_target = globals().get("_MEDIA_IMPORT_TARGET_ID")
    if isinstance(import_target, str) and import_target:
        out[import_target] = "Import"
    globals()["_JOB_WORKFLOW_LABELS_CACHE"] = out
    return out


def _job_workflow_label(target_id: Any) -> str | None:
    if not isinstance(target_id, str) or not target_id:
        return None
    return _job_workflow_labels().get(target_id) or _job_workflow_label_unlisted(target_id) or target_id


# Lane w35-ui-3 (critic run 3 #12): Jobs showed "yue2-song-v1", "relayvsr-video-upscale-v1", "minimax-h3-finish-v1". The
# map above reads the registry's LISTED targets and the model catalog; two workflow kinds are in neither: the registry's
# operations on a finished take (H3 Finish, RelayVSR upscale: resolvable, never listed) and the pinned music targets
# (music_gen/targets.py: SA3, YuE2, Foundation-1). They are named from their own registrations, read once per id; the
# id stays the row's target_id (Details).
def _job_workflow_label_unlisted(target_id: str) -> str | None:
    known = globals().setdefault("_JOB_WORKFLOW_LABELS_UNLISTED", {})
    if target_id in known:
        return known[target_id]
    label = None
    try:
        registry = _generation_registry.DEFAULT_REGISTRY
        places = {p["id"]: p.get("display_label") or p.get("label") or p["id"] for p in _generation_registry.catalog().get("providers", [])}
        for provider_id in registry.provider_ids():
            try:
                resolved = registry.resolve(provider_id, target_id)
            except Exception:  # noqa: BLE001 - not this provider's
                continue
            label = f"{places.get(provider_id, resolved.provider_label)} · {resolved.label}" if resolved.label else None
            break
    except Exception:  # noqa: BLE001 - without the registry the music targets still name
        label = None
    music = globals().get("_music_gen_targets")
    if label is None and music is not None and target_id in getattr(music, "TARGET_IDS", ()):
        try:
            name = music.load_target(target_id)["manifest"]["label"]
            local = next((v.split(" · ", 1)[0] for v in _job_workflow_labels().values() if v.startswith("Local · ")), "Local")
            label = f"{local} · {name}" if isinstance(name, str) and name else None
        except Exception:  # noqa: BLE001 - an unreadable manifest answers its id
            label = None
    known[target_id] = label
    return label


_jobs_list_before_names = jobs_list


async def _jobs_list_with_names(request: JobsListRequest) -> dict[str, Any]:
    answer = await _jobs_list_before_names(request)
    if isinstance(answer, dict) and isinstance(answer.get("jobs"), list):
        for job in answer["jobs"]:
            if isinstance(job, dict):
                job["workflow_label"] = _job_workflow_label(job.get("target_id"))
    return answer


jobs_list = _jobs_list_with_names  # noqa: F811 - the facade name answers the same as the route
for _route in project_router.routes:
    if getattr(_route, "path", None) == "/jobs/list" and "POST" in getattr(_route, "methods", ()):
        _route.endpoint = _jobs_list_with_names
        _route.dependant.call = _jobs_list_with_names
