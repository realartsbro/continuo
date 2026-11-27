"""MiniMax-H3-Image (the official model, not PrunaAI's P-H3-Image): the Image Create target ``minimax-h3-image-create-v1``.

The pinned graph is a proven recipe (renders on ComfyUI 0.45.0): 6 steps on the simple
schedule, split after step 3 (``SplitSigmas``): er_sde with the seed's noise, then LCM with its noise falling from 0.95
to 0.90 (``SamplerLCM`` s_noise / s_noise_end, no new noise), CFG 1 against a zeroed negative. Width and height go
straight into ``MiniMaxH3ImageConditioning`` (ResolutionSelector stops at 16 MP and has no 2.39:1).

Up to ``ONE_PASS_MP`` (16 MP, the model card's best size) that is the whole picture. Above it the same graph first makes
the picture at 16 MP in the chosen format, then a second pass refines it at the asked size: lanczos scale, VAE encode,
``KSamplerAdvanced`` euler / simple over the same steps from ``second_pass_start`` (4 of 6), the same seed and the
first pass's conditioning (the conditioning does not depend on the size, so the 32B text encoder runs once). The second
pass nodes (manifest ``second_pass.nodes``) are pruned at 16 MP and below; the picture then comes from the first decode.
The artist prompt is written byte-for-byte.

References (IMG-228): up to ``MAX_REFERENCES`` owned pictures go into the conditioning's ``images.image_N`` (LoadImage
1000+i), named ``<Picture N>`` in the prompt; a role other than the plain ``reference`` is named in one visible suffix
(``<Picture N> is the <role> reference.``), as P-H3-Image's edit does. The size stays the asked one. With a reference
above ``image_workflow_run.SOFT_WITH_REFERENCES_MP`` the run says so (``warnings``); nothing is refused.
"""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path
from typing import Any, Mapping

_CREATE = None
_RUN = None


def _run():
    """image_workflow_run.py by path: the one place of the 4 MP reference fact."""
    global _RUN
    if _RUN is None:
        spec = importlib.util.spec_from_file_location("h3_image_run_facts", Path(__file__).with_name("image_workflow_run.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _RUN = module
    return _RUN


def _create():
    global _CREATE
    if _CREATE is None:
        path = Path(__file__).with_name("catalog_create_workflow_adapter.py")
        spec = importlib.util.spec_from_file_location("h3_image_create_base", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CREATE = module
    return _CREATE


WorkflowRequestError = _create().WorkflowRequestError
REQUEST_SCHEMA = _create().REQUEST_SCHEMA
FAMILY = "minimax_h3_image"
TARGET = "minimax-h3-image-create-v1"
ONE_PASS_MP = 16.0  # model card "Sizes": about 16 MP is the best single pass (working range about 4 to 20 MP)
MAX_REFERENCES = 9  # orchestrator's maximum for 1.2.0 (the card: up to 16 references; the node takes 100)
GRID = 32  # MiniMaxH3ImageConditioning width/height step (ComfyUI 0.45.0 object_info)
MP = 1024 * 1024


def _ratio(option: str) -> float:
    """``"2.39:1 (Scope)"`` -> 2.39; ``"16:9 (Widescreen)"`` -> 16/9."""
    a, b = str(option).split(" ")[0].split(":")
    return float(a) / float(b)


def size_at(option: str, megapixels: float) -> tuple[int, int]:
    """Width x height of the format at ``megapixels`` (2^20 pixels each), each side on the 32 grid."""
    ratio = _ratio(option)
    scale = math.sqrt(megapixels * MP / ratio)
    return max(GRID, round(ratio * scale / GRID) * GRID), max(GRID, round(scale / GRID) * GRID)


class H3ImageWorkflowAdapter(_create().CatalogCreateWorkflowAdapter):
    """The shared create compile, plus the size and the second pass above 16 MP."""

    def __init__(self, *, asset_root: Path | None = None):
        here = Path(__file__).resolve()
        repository = here.parents[4] / "workflows"
        installed = here.parents[1] / "workflows"
        pinned = repository / TARGET / "manifest.json"
        self.asset_root = Path(asset_root) if asset_root is not None else (repository if pinned.is_file() else installed)

    def _entry(self, target_id: str) -> tuple[str, dict]:
        catalog = _create()._shared().load_catalog_module().catalog_view()  # shared, read-only
        for family in catalog["families"]:
            if family["id"] != FAMILY:
                continue
            for variant in family["variants"]:
                workflow = variant.get("workflow")
                if workflow and workflow["descriptor"]["target_id"] == target_id:
                    return family["id"], workflow
        raise WorkflowRequestError("workflow target not offered")

    def load_assets(self, target_id: str) -> tuple[dict, dict]:
        family, entry = self._entry(target_id)
        shared = _create()._shared()
        folder = self.asset_root / target_id
        manifest = shared._read_asset(folder, "manifest.json", entry["manifest_sha256"])
        graph = shared._read_asset(folder, "workflow-api.json", entry["api_sha256"])
        if (manifest.get("schema") != "studio.image-workflow/1" or manifest.get("id") != target_id
                or manifest.get("family") != family or manifest.get("operation") != "create"
                or manifest.get("descriptor") != entry["descriptor"]
                or manifest["workflow"]["api_sha256"] != entry["api_sha256"]
                or manifest["workflow"]["node_count"] != len(graph)
                or manifest["references"]["max"] != MAX_REFERENCES):
            raise WorkflowRequestError("workflow identity mismatch")
        return manifest, graph

    @staticmethod
    def _values(manifest: dict, controls: Any) -> dict:
        values = _create().CatalogCreateWorkflowAdapter._values(manifest, controls)
        offered = {c["id"]: c for c in manifest["descriptor"]["controls"]}
        for key in ("switch_at", "second_pass_start"):
            if key not in controls:
                values[key] = min(values[key], values["steps"] - 1)
            elif values[key] >= values["steps"]:
                label = offered[key]["label"]
                raise _create()._shared()._refused_control(
                    f"{label} must be before the last step", offered[key], {"min": 0, "max": values["steps"] - 1})
        return values

    def _sizes(self, request: Mapping[str, Any]) -> tuple[dict, tuple[int, int], tuple[int, int]]:
        manifest, _ = self.load_assets(request["target_id"])
        values = self._values(manifest, request["controls"])
        target = size_at(values["aspect_ratio"], values["megapixels"])
        first = target if values["megapixels"] <= ONE_PASS_MP else size_at(values["aspect_ratio"], ONE_PASS_MP)
        return values, first, target

    def _references(self, manifest: dict, refs: Any) -> list[dict]:
        shared = _create()._shared()
        if type(refs) is not list or len(refs) > manifest["references"]["max"]:
            raise WorkflowRequestError("reference count invalid")
        roles = next(r["roles"] for r in manifest["descriptor"]["media_roles"] if r["id"] == "references")
        for ref in refs:
            if (type(ref) is not dict or set(ref) != {"image", "role"} or type(ref["image"]) is not str
                    or not shared._UPLOAD.fullmatch(ref["image"]) or ref["role"] not in roles):
                raise WorkflowRequestError("reference image or role invalid")
        return refs

    def compile(self, request: Mapping[str, Any]) -> dict:
        if type(request) is not dict or "references" not in request:
            raise WorkflowRequestError("request shape invalid")
        manifest, _ = self.load_assets(request["target_id"])
        refs = self._references(manifest, request["references"])
        graph = super().compile({**request, "references": []})
        spec = manifest["references"]
        conditioning = graph[spec["conditioning_node"]]["inputs"]
        for index, ref in enumerate(refs):
            node_id = str(spec["reference_node_base"] + index)
            if node_id in graph:
                raise WorkflowRequestError("reference node collision")
            graph[node_id] = {"class_type": "LoadImage", "inputs": {"image": ref["image"]}}
            conditioning[f"images.image_{index + 1}"] = [node_id, 0]
        named = [f"<Picture {i + 1}> is the {ref['role']} reference." for i, ref in enumerate(refs) if ref["role"] != "reference"]
        if named:
            prompt_node = graph[manifest["prompt"]["node_id"]]["inputs"]
            prompt_node["value"] = prompt_node["value"] + " " + " ".join(named)
        values, first, target = self._sizes(request)
        size = graph[manifest["size"]["node_id"]]["inputs"]
        size["width"], size["height"] = first
        second = manifest["second_pass"]
        scale, _encode, sampler, decode = second["nodes"]
        if values["megapixels"] <= second["above_megapixels"]:
            for node_id in second["nodes"]:
                graph.pop(node_id)
            graph[second["output_node"]]["inputs"]["images"] = [second["first_pass_decode"], 0]
            return graph
        graph[scale]["inputs"]["width"], graph[scale]["inputs"]["height"] = target
        graph[sampler]["inputs"]["noise_seed"] = values["seed"]
        graph[sampler]["inputs"]["steps"] = values["steps"]
        graph[second["output_node"]]["inputs"]["images"] = [decode, 0]
        return graph

    def output_size(self, request: dict, references: list[dict]) -> tuple[int, int]:
        """The asked size (explicit); references never set it."""
        return self._sizes(request)[2]

    def warnings(self, request: Mapping[str, Any]) -> list[str]:
        """Facts about how this request runs (the submit answer's ``warnings``, "code: fact"); never a refusal."""
        refs = request.get("references") if isinstance(request, Mapping) else None
        controls = request.get("controls") if isinstance(request, Mapping) else None
        if not refs or not isinstance(controls, Mapping):
            return []
        soft = _run().SOFT_WITH_REFERENCES_MP
        try:
            megapixels = float(self._values(self.load_assets(request["target_id"])[0], dict(controls))["megapixels"])
        except (ValueError, TypeError, KeyError):
            return []
        if megapixels <= soft:
            return []
        return [f"reference_soft_above_4mp: with a reference picture, results turn soft above {soft:g} MP"]


def create_adapter():
    """Catalog adapter-factory protocol; generic admission has no model branch."""
    return H3ImageWorkflowAdapter()
