"""Explicit provider and target catalog for the Studio generation boundary.

This module is intentionally declarative.  Providers and targets are registered
in Python, rather than discovered from the filesystem or imported from a
runtime-supplied manifest.  Only the small public capability projection leaves
this boundary; provider credentials and execution metadata do not have a place
in the registration types.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Iterable as IterableABC
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contract import ProviderCapabilities, public_reason


def _load_h3_megapixels():
    """The one H3 megapixel table (``h3_megapixels.py`` next to this file; lane p13-vid VID-005).

    Loaded by path so this module works both as a package member and when loaded alone by path."""
    import hashlib as _hashlib
    import importlib.util as _importlib_util
    import sys as _sys
    from pathlib import Path as _Path

    path = _Path(__file__).with_name("h3_megapixels.py")
    name = "film_suite_h3_megapixels_" + _hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]
    module = _sys.modules.get(name)
    if module is None:
        spec = _importlib_util.spec_from_file_location(name, path)
        module = _importlib_util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _sys.modules[name] = module
    return module


H3_MP = _load_h3_megapixels()


CATALOG_SCHEMA = "studio.generation-catalog/1"
_ALLOWED_CONTROL_KINDS = frozenset(
    {"textarea", "integer", "select", "boolean", "image", "keyframes", "video", "audio", "informational"}
)
_PROVIDER_PUBLIC_KEYS = frozenset({"id", "label", "endpoint_label", "targets", "descriptor_version", "location", "icon", "display_label", "selector_kind"})
_CATALOG_PUBLIC_KEYS = frozenset({"schema", "providers"})
_TARGET_PUBLIC_KEYS = frozenset(
    {"id", "label", "input_kind", "controls", "output", "admission", "health_policy", "descriptor_version", "capabilities", "max_active", "cost", "eta_label", "media_roles", "form_kind", "state_binding", "request_contract", "mode", "display_label", "continuation_role", "control_extension", "finish_operation", "model_family"}
)
_CONTROL_PUBLIC_KEYS = frozenset(
    {"id", "kind", "required", "min", "max", "step", "options", "default", "prompt_policy"}
)
_OUTPUT_PUBLIC_KEYS = frozenset({"kind", "resolution", "audio", "fps"})
_ADMISSION_PUBLIC_KEYS = frozenset({"max_active_studio_jobs", "min_submit_interval_seconds"})
_FORBIDDEN_KEY_PARTS = (
    "secret",
    "token",
    "password",
    "credential",
    "workflow",
    "manifest",
    "download",
    "url",
    "path",
    "pointer",
    "node",
    "raw",
    "dependency_error",
)
_FORBIDDEN_PUBLIC_TEXT = re.compile(
    r"(?i)(?:secret|token|password|credential|api[\s_-]*key|access[\s_-]*key|private[\s_-]*key|bearer|authorization|cookie|session)"
)
_URI_LIKE_TEXT = re.compile(r"(?i)\b[a-z][a-z0-9+.-]{0,31}:|\b(?:url|uri)\s*:")
_PATH_LIKE_TEXT = re.compile(r"(?i)(?:^[a-z]:[\\/]?|[\\/]|(?:^|\s)\.{1,2}[\\/])")
_HOST_PORT_TEXT = re.compile(
    r"(?i)^(?:[a-z][a-z0-9.-]*|(?:\d{1,3}\.){3}\d{1,3}):\d{1,5}(?:[/\?#].*)?$"
)
# w39-p2-tester item 4: the one configured ComfyUI address (generation/comfy_endpoint.py, loaded by path).
import importlib.util as _endpoint_util
from pathlib import Path as _EndpointPath
_endpoint_spec = _endpoint_util.spec_from_file_location("film_suite_comfy_endpoint", _EndpointPath(__file__).resolve().parents[1] / "generation" / "comfy_endpoint.py")
_endpoint = _endpoint_util.module_from_spec(_endpoint_spec)
_endpoint_spec.loader.exec_module(_endpoint)
_ALLOWED_PUBLIC_TEXT = frozenset({"127.0.0.1:8188"})  # plus any engine label (_endpoint.is_engine_label: this machine, any port, or FILM_SUITE_COMFY_URL)


class GenerationDomainError(ValueError):
    """Stable provider-neutral domain failure with a finite public reason."""

    def __init__(self, reason_code: str, message: str | None = None) -> None:
        self.reason_code = reason_code if reason_code in {"invalid_request", "provider_busy", "unknown"} else "unknown"
        self.public_message = public_reason(self.reason_code)
        super().__init__(message or self.public_message)

    def to_result(self) -> dict[str, str]:
        return {"reason_code": self.reason_code}


class InvalidRequestError(GenerationDomainError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__("invalid_request", message=message)


class DuplicateRegistrationError(ValueError):
    """Raised when explicit provider or target registration is ambiguous."""


@dataclass(frozen=True)
class TargetSpec:
    """A target's public, finite capability description."""

    id: str
    label: str
    input_kind: str
    controls: Iterable[Mapping[str, Any]] = ()
    output: Mapping[str, Any] = None  # type: ignore[assignment]
    admission: Mapping[str, Any] = None  # type: ignore[assignment]
    health_policy: str | None = None
    descriptor: Mapping[str, Any] = None  # Presentation only; never part of execution snapshots.

    def __post_init__(self) -> None:
        _require_lookup_identity(self.id, "target id")
        _validate_public_text(self.id, "target id")
        _validate_public_text(self.label, "target label")
        _validate_public_text(self.input_kind, "target input_kind")
        controls = _validate_controls(self.controls, "target controls")
        output = _validate_output(self.output, "target output")
        admission = _validate_admission(self.admission, "target admission")
        if self.health_policy is not None and self.health_policy != "static_only":
            raise ValueError("target health_policy must be static_only")
        if self.health_policy is not None:
            _validate_public_text(self.health_policy, "target health_policy")
        capabilities = ProviderCapabilities(
            provider_id="registry",
            target_id=self.id,
            label=self.label,
            input_kind=self.input_kind,
            controls=controls,
            output=output,
            admission=admission,
            health_policy=self.health_policy,
        ).to_dict()
        _assert_public(capabilities)
        object.__setattr__(self, "controls", _freeze_json(controls))
        object.__setattr__(self, "output", _freeze_json(output))
        object.__setattr__(self, "admission", _freeze_json(admission))
        object.__setattr__(self, "descriptor", _freeze_json(_validate_descriptor(self.descriptor)))

    @property
    def capabilities(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "input_kind": self.input_kind,
            "controls": _thaw_json(self.controls),
            "output": _thaw_json(self.output),
            "admission": _thaw_json(self.admission),
            "health_policy": self.health_policy,
        }

    def to_public_dict(self) -> dict[str, Any]:
        payload = self.capabilities
        _validate_mapping_keys(payload, _TARGET_PUBLIC_KEYS, "target")
        _validate_public_text(payload["id"], "target.id")
        _validate_public_text(payload["label"], "target.label")
        _validate_public_text(payload["input_kind"], "target.input_kind")
        payload["controls"] = _validate_controls(payload["controls"], "target.controls")
        payload["output"] = _validate_output(payload["output"], "target.output")
        payload["admission"] = _validate_admission(payload["admission"], "target.admission")
        if payload["health_policy"] is not None and payload["health_policy"] != "static_only":
            raise ValueError("target health_policy must be static_only")
        _assert_public(payload)
        descriptor = _thaw_json(self.descriptor)
        metadata = descriptor.pop("control_metadata")
        additional_roles = descriptor.pop("media_roles")
        payload.update(descriptor)
        payload["max_active"] = payload["admission"].get("max_active_studio_jobs", 1)
        payload["controls"] = [{**control, **metadata.get(control["id"], {"label": control["id"], "summary_format": "{label} {value}"})} for control in payload["controls"]]
        payload["media_roles"] = [copy.deepcopy(control) for control in payload["controls"] if control["kind"] in {"image", "keyframes", "video", "audio"}]
        payload["media_roles"].extend({**control, **metadata.get(control["id"], {"label": control["id"], "summary_format": None})} for control in additional_roles)
        return payload


@dataclass(frozen=True)
class ResolvedTarget:
    provider_id: str
    provider_label: str
    target_id: str
    label: str
    input_kind: str
    capabilities: dict[str, Any]


@dataclass(frozen=True)
class _ProviderSpec:
    id: str
    label: str
    endpoint_label: str | None
    targets: tuple[TargetSpec, ...]
    location: str = "cloud"
    icon: str = "cloud"
    display_label: str = ""
    selector_kind: str = "model"


class ProviderRegistry:
    """An explicit registry with startup-time uniqueness checks."""

    def __init__(self) -> None:
        self._providers: dict[str, _ProviderSpec] = {}
        self._target_owner: dict[str, str] = {}
        # Operations on a finished take (lane w1-h3: H3 finishing): resolvable for submit, never
        # listed in the catalog (they are not a Model the Shoot pane offers; their route starts them).
        self._operations: dict[str, TargetSpec] = {}

    def register_operation(self, provider_id: str, target: TargetSpec) -> None:
        _require_lookup_identity(provider_id, "provider_id")
        if provider_id not in self._providers or not isinstance(target, TargetSpec):
            raise InvalidRequestError()
        if target.id in self._target_owner:
            raise DuplicateRegistrationError(f"duplicate target registration: {target.id}")
        self._target_owner[target.id] = provider_id
        self._operations[target.id] = target

    def register_provider(
        self,
        provider_id: str,
        label: str,
        *,
        targets: Iterable[TargetSpec] = (),
        endpoint_label: str | None = None,
        location: str = "cloud",
        icon: str = "cloud",
        display_label: str | None = None,
        selector_kind: str = "model",
    ) -> None:
        _require_lookup_identity(provider_id, "provider_id")
        _validate_public_text(provider_id, "provider_id")
        _require_text(label, "label")
        if endpoint_label is not None:
            _require_text(endpoint_label, "endpoint_label")
            if not _endpoint.is_engine_label(endpoint_label):
                raise ValueError("endpoint_label must be this machine's or the configured ComfyUI address")
        _assert_public({"id": provider_id, "label": label, "endpoint_label": endpoint_label})
        if provider_id in self._providers:
            raise DuplicateRegistrationError(f"duplicate provider registration: {provider_id}")
        target_list = tuple(targets)
        seen: set[str] = set()
        for target in target_list:
            if not isinstance(target, TargetSpec):
                raise TypeError("targets must contain TargetSpec values")
            _require_lookup_identity(target.id, "target_id")
            _validate_public_text(target.id, "target.id")
            if target.id in seen or target.id in self._target_owner:
                raise DuplicateRegistrationError(f"duplicate target registration: {target.id}")
            seen.add(target.id)
        if location not in {"local", "cloud"} or icon not in {"pc", "cloud"} or selector_kind not in {"model", "recipe"}:
            raise ValueError("invalid provider presentation descriptor")
        _validate_public_text(display_label or label, "provider display label")
        self._providers[provider_id] = _ProviderSpec(provider_id, label, endpoint_label, target_list, location, icon, display_label or label, selector_kind)
        self._target_owner.update({target.id: provider_id for target in target_list})

    def register_target(self, provider_id: str, target: TargetSpec) -> None:
        _require_lookup_identity(provider_id, "provider_id")
        provider = self._providers.get(provider_id)
        if provider is None:
            raise InvalidRequestError()
        if not isinstance(target, TargetSpec):
            raise TypeError("target must be a TargetSpec")
        _require_lookup_identity(target.id, "target_id")
        _validate_public_text(target.id, "target.id")
        if target.id in self._target_owner:
            raise DuplicateRegistrationError(f"duplicate target registration: {target.id}")
        self._providers[provider_id] = _ProviderSpec(
            provider.id,
            provider.label,
            provider.endpoint_label,
            (*provider.targets, target),
            provider.location, provider.icon, provider.display_label, provider.selector_kind,
        )
        self._target_owner[target.id] = provider_id

    def resolve(self, provider_id: str, target_id: str) -> ResolvedTarget:
        _require_lookup_identity(provider_id, "provider_id")
        _require_lookup_identity(target_id, "target_id")
        provider = self._providers.get(provider_id)
        if provider is None:
            raise InvalidRequestError()
        target = next((candidate for candidate in provider.targets if candidate.id == target_id), None)
        if target is None and self._target_owner.get(target_id) == provider_id:
            target = self._operations.get(target_id)
        if target is None:
            raise InvalidRequestError()
        return ResolvedTarget(
            provider_id=provider.id,
            provider_label=provider.label,
            target_id=target.id,
            label=target.label,
            input_kind=target.input_kind,
            capabilities=target.capabilities,
        )

    get = resolve
    require_target = resolve

    def provider_ids(self) -> tuple[str, ...]:
        return tuple(self._providers)

    def target_ids(self, provider_id: str) -> tuple[str, ...]:
        _require_lookup_identity(provider_id, "provider_id")
        provider = self._providers.get(provider_id)
        if provider is None:
            raise InvalidRequestError()
        return tuple(target.id for target in provider.targets)

    def to_catalog(self) -> dict[str, Any]:
        providers = []
        for provider in self._providers.values():
            provider_payload: dict[str, Any] = {
                "id": provider.id,
                "label": provider.label,
                "targets": [target.to_public_dict() for target in provider.targets],
                "descriptor_version": 1,
                "location": provider.location,
                "icon": provider.icon,
                "display_label": provider.display_label,
                "selector_kind": provider.selector_kind,
            }
            if provider.endpoint_label is not None:
                provider_payload["endpoint_label"] = provider.endpoint_label
            _validate_mapping_keys(provider_payload, _PROVIDER_PUBLIC_KEYS, "provider")
            _validate_public_text(provider_payload["id"], "provider.id")
            _validate_public_text(provider_payload["label"], "provider.label")
            if "endpoint_label" in provider_payload:
                if not _endpoint.is_engine_label(provider_payload["endpoint_label"]):
                    raise ValueError("provider.endpoint_label must be this machine's or the configured ComfyUI address")
                _validate_public_text(provider_payload["endpoint_label"], "provider.endpoint_label")
            _assert_public(provider_payload)
            providers.append(provider_payload)
        catalog_payload = {"schema": CATALOG_SCHEMA, "providers": copy.deepcopy(providers)}
        _validate_mapping_keys(catalog_payload, _CATALOG_PUBLIC_KEYS, "catalog")
        _validate_public_text(catalog_payload["schema"], "catalog.schema")
        return catalog_payload

    catalog = to_catalog

    def to_json(self) -> str:
        return json.dumps(self.to_catalog(), ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(nested) for key, nested in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(nested) for nested in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(nested) for nested in value]
    return copy.deepcopy(value)


def _require_text(value: Any, field_name: str) -> str:
    _validate_public_text(value, field_name)
    return value


def _require_lookup_identity(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError(f"{field_name} is invalid")
    try:
        hash(value)
    except TypeError as exc:
        raise InvalidRequestError(f"{field_name} is invalid") from exc
    return value


def _validate_public_text(value: Any, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    try:
        hash(value)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be hashable text") from exc
    stripped = value.strip()
    try:
        hash(stripped)
    except TypeError as exc:
        raise ValueError(f"{field_name} must be hashable text") from exc
    if stripped in _ALLOWED_PUBLIC_TEXT or _endpoint.is_engine_label(stripped) or (field_name == "catalog.schema" and stripped == CATALOG_SCHEMA):
        return
    if (
        _FORBIDDEN_PUBLIC_TEXT.search(value)
        or _URI_LIKE_TEXT.search(value)
        or _PATH_LIKE_TEXT.search(value)
        or _HOST_PORT_TEXT.match(stripped)
    ):
        raise ValueError(f"{field_name} contains sensitive, path-like, or endpoint-like text")


def _validate_mapping_keys(value: Any, allowed: frozenset[str], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    for key in value:
        if not isinstance(key, str):
            raise ValueError(f"{field_name} keys must be text")
        try:
            hash(key)
        except TypeError as exc:
            raise ValueError(f"{field_name} keys must be hashable text") from exc
        if key not in allowed:
            raise ValueError(f"{field_name}.{key} is not public capability data")
    return value


def _validate_finite_number(
    value: Any,
    field_name: str,
    *,
    integer: bool = False,
    minimum: int | float | None = None,
) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    if integer and not isinstance(value, int):
        raise ValueError(f"{field_name} must be a whole number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field_name} must be at least {minimum}")
    return value


def _validate_public_scalar(value: Any, field_name: str) -> Any:
    if isinstance(value, str):
        _validate_public_text(value, field_name)
        return value
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return _validate_finite_number(value, field_name)
    raise ValueError(f"{field_name} must be a public scalar value")


def _as_public_list(value: Any, field_name: str) -> list[Any]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, IterableABC):
        raise ValueError(f"{field_name} must be a list")
    return list(value)


def _validate_controls(value: Any, field_name: str) -> list[dict[str, Any]]:
    controls = [] if value is None else _as_public_list(value, field_name)
    validated: list[dict[str, Any]] = []
    for index, raw_control in enumerate(controls):
        control_name = f"{field_name}[{index}]"
        control = _validate_mapping_keys(raw_control, _CONTROL_PUBLIC_KEYS, control_name)
        if "id" not in control or "kind" not in control:
            raise ValueError(f"{control_name} requires id and kind")
        _validate_public_text(control["id"], f"{control_name}.id")
        _validate_public_text(control["kind"], f"{control_name}.kind")
        kind = control["kind"]
        if kind not in _ALLOWED_CONTROL_KINDS:
            raise ValueError("target control kind is not supported")

        normalized: dict[str, Any] = {"id": control["id"], "kind": kind}
        if "required" in control:
            if not isinstance(control["required"], bool):
                raise ValueError(f"{control_name}.required must be boolean")
            normalized["required"] = control["required"]

        numeric_values: dict[str, int | float] = {}
        for numeric_key in ("min", "max", "step"):
            if numeric_key in control:
                numeric_values[numeric_key] = _validate_finite_number(
                    control[numeric_key],
                    f"{control_name}.{numeric_key}",
                    integer=kind == "integer",
                )
        if "min" in numeric_values and "max" in numeric_values and numeric_values["min"] > numeric_values["max"]:
            raise ValueError(f"{control_name}.min cannot exceed max")
        if "step" in numeric_values and numeric_values["step"] <= 0:
            raise ValueError(f"{control_name}.step must be positive")
        normalized.update(numeric_values)

        options: list[Any] | None = None
        if "options" in control:
            if kind != "select":
                raise ValueError(f"{control_name}.options requires a select control")
            options = [
                _validate_public_scalar(option, f"{control_name}.options[{option_index}]")
                for option_index, option in enumerate(_as_public_list(control["options"], f"{control_name}.options"))
            ]
            normalized["options"] = options

        if "default" in control:
            default = control["default"]
            if kind == "boolean" and not isinstance(default, bool):
                raise ValueError(f"{control_name}.default must be boolean")
            elif kind == "integer":
                default = _validate_finite_number(default, f"{control_name}.default", integer=True)
            else:
                default = _validate_public_scalar(default, f"{control_name}.default")
            if options is not None and default not in options:
                raise ValueError(f"{control_name}.default must be one of options")
            normalized["default"] = default

        if "prompt_policy" in control:
            _validate_public_text(control["prompt_policy"], f"{control_name}.prompt_policy")
            normalized["prompt_policy"] = control["prompt_policy"]
        validated.append(normalized)
    return validated


def _validate_output(value: Any, field_name: str) -> dict[str, Any]:
    output = {} if value is None else dict(_validate_mapping_keys(value, _OUTPUT_PUBLIC_KEYS, field_name))
    normalized: dict[str, Any] = {}
    for key, nested in output.items():
        if key == "fps":
            normalized[key] = _validate_finite_number(nested, f"{field_name}.{key}", minimum=0)
        else:
            _validate_public_text(nested, f"{field_name}.{key}")
            normalized[key] = nested
    return normalized


def _validate_admission(value: Any, field_name: str) -> dict[str, Any]:
    admission = {} if value is None else dict(_validate_mapping_keys(value, _ADMISSION_PUBLIC_KEYS, field_name))
    normalized: dict[str, Any] = {}
    if "max_active_studio_jobs" in admission:
        normalized["max_active_studio_jobs"] = _validate_finite_number(
            admission["max_active_studio_jobs"],
            f"{field_name}.max_active_studio_jobs",
            integer=True,
            minimum=1,
        )
    if "min_submit_interval_seconds" in admission:
        normalized["min_submit_interval_seconds"] = _validate_finite_number(
            admission["min_submit_interval_seconds"],
            f"{field_name}.min_submit_interval_seconds",
            minimum=0,
        )
    return normalized


def _assert_public(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if not isinstance(key, str):
                raise ValueError("public capability keys must be text")
            folded = key.casefold()
            if any(part in folded for part in _FORBIDDEN_KEY_PARTS):
                raise ValueError("internal execution metadata is not public capability data")
            _assert_public(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _assert_public(nested)
    elif isinstance(value, str):
        _validate_public_text(value, "public capability text")
    elif isinstance(value, float):
        _validate_finite_number(value, "public capability number")
    elif value is None or isinstance(value, (bool, int)):
        return
    else:
        raise ValueError("public capabilities must contain only JSON values")


def _target(
    target_id: str,
    label: str,
    *,
    controls: Iterable[Mapping[str, Any]],
    output: Mapping[str, Any],
    admission: Mapping[str, Any],
    health_policy: str | None = None,
    input_kind: str = "text_to_video",
    descriptor: Mapping[str, Any] | None = None,
) -> TargetSpec:
    return TargetSpec(
        target_id,
        label,
        input_kind,
        controls=controls,
        output=output,
        admission=admission,
        health_policy=health_policy,
        descriptor=descriptor,
    )


_FEATURES = frozenset({"keyframes", "continuation", "version", "generated_audio", "grounding", "cancel"})
_DESCRIPTOR_KEYS = frozenset({"cancel_notice", "capabilities", "cost", "eta_label", "form_kind", "state_binding", "request_contract", "mode", "display_label", "control_metadata", "media_roles", "continuation_role", "control_extension", "finish_operation", "model_family"})
_METADATA_KEYS = frozenset({"page_label", "label", "summary_format", "max_length", "placeholder", "option_labels", "role_label", "summary_strip_annotation", "note"})


def _validate_descriptor(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Closed, immutable presentation data; never admitted to execution snapshots."""
    raw = {} if value is None else dict(_validate_mapping_keys(value, _DESCRIPTOR_KEYS, "descriptor"))
    features = dict(_validate_mapping_keys(raw.get("capabilities", {}), _FEATURES, "descriptor.capabilities"))
    if any(type(flag) is not bool for flag in features.values()):
        raise ValueError("capability flags must be boolean")
    result = {
        "descriptor_version": 1,
        "capabilities": {feature: features.get(feature, False) for feature in sorted(_FEATURES)},
        "cost": {"kind": "unknown"}, "eta_label": None,
        "form_kind": "controls", "state_binding": "catalog", "request_contract": "versioned",
        "mode": "text_to_video", "display_label": None, "control_metadata": {}, "media_roles": [],
    }
    for key, allowed in (("form_kind", {"controls", "directed"}), ("state_binding", {"catalog", "directed_v1", "sampling_v1"}), ("request_contract", {"legacy", "versioned"}), ("mode", {"text_to_video", "image_to_video", "keyframes_to_video", "video_continuation", "references_to_video"})):
        if key in raw:
            if raw[key] not in allowed:
                raise ValueError(f"invalid descriptor {key}")
            result[key] = raw[key]
    cost = dict(_validate_mapping_keys(raw.get("cost", {"kind": "unknown"}), frozenset({"kind", "label", "full_label"}), "descriptor.cost"))
    if cost.get("kind") not in {"free", "per_second", "unknown"}:
        raise ValueError("invalid cost kind")
    for key in ("label", "full_label"):
        if key in cost:
            _validate_public_text(cost[key], f"descriptor.cost.{key}")
    result["cost"] = cost
    for key in ("eta_label", "display_label", "cancel_notice"):
        if key in raw:
            _validate_public_text(raw[key], f"descriptor.{key}")
            result[key] = raw[key]
    # lane fs-obvb: what a take continuation does (only on those targets, so other targets keep their bytes)
    if "continuation_role" in raw:
        if raw["continuation_role"] not in {"extend", "prepend", "bridge"}:
            raise ValueError("invalid descriptor continuation_role")
        result["continuation_role"] = raw["continuation_role"]
    # Wave 3 (model-agnostic renderer): which extras a target has, as data instead of ids in the page.
    # control_extension names the /generation/<id>/controls descriptor route, finish_operation the
    # take operation that finishes its takes, model_family the /models/catalog family of its files.
    for key in ("control_extension", "finish_operation", "model_family"):
        if key in raw:
            _validate_public_text(raw[key], f"descriptor.{key}")
            result[key] = raw[key]
    result["media_roles"] = _validate_controls(raw.get("media_roles", []), "descriptor.media_roles")
    if any(role["kind"] not in {"image", "keyframes", "video", "audio"} for role in result["media_roles"]):
        raise ValueError("descriptor media roles must use a media control kind")
    metadata = raw.get("control_metadata", {})
    if not isinstance(metadata, Mapping):
        raise ValueError("control metadata must be an object")
    for control_id, entry in metadata.items():
        _validate_public_text(control_id, "control metadata id")
        data = dict(_validate_mapping_keys(entry, _METADATA_KEYS, "control metadata"))
        for key, text in data.items():
            if key == "max_length":
                _validate_finite_number(text, key, integer=True, minimum=1)
            elif key == "summary_strip_annotation":
                if type(text) is not bool:
                    raise ValueError("summary_strip_annotation must be boolean")
            elif key == "option_labels":
                if not isinstance(text, Mapping):
                    raise ValueError("option_labels must be an object")
                _assert_public(text)
            elif key == "summary_format" and text is None:
                pass  # Explicitly excluded from the collapsed summary.
            else:
                _validate_public_text(text, key)
        result["control_metadata"][control_id] = data
    return result


def _control_metadata() -> dict[str, dict[str, Any]]:
    return {
        "prompt": {"label": "Prompt", "summary_format": None},
        "duration": {"label": "Duration (s)", "summary_format": "{value} s"},
        "duration_seconds": {"label": "Duration (s)", "summary_format": "{value} s"},
        "seed": {"label": "Seed", "summary_format": "seed {value}"},
        "aspect_ratio": {"label": "Aspect", "summary_format": "{value}", "summary_strip_annotation": True},
        "ratio": {"label": "Aspect", "summary_format": "{value}", "option_labels": {"adaptive": "Adaptive"}},
        "megapixels": {"label": "Megapixels", "summary_format": None},
        "resolution": {"label": "Resolution", "summary_format": "{value}"},
        "negative_prompt": {"label": "Avoid", "summary_format": None, "placeholder": "What should not appear"},
        "first_frame": {"label": "Opening frame", "summary_format": None, "role_label": "Opening frame"},
        "last_frame": {"label": "Closing frame", "summary_format": None, "role_label": "Closing frame"},
        "reference_images": {"label": "References", "summary_format": None, "role_label": "Reference"},
        "reference_videos": {"label": "Reference clips", "summary_format": None, "role_label": "Reference"},
        "input_video": {"label": "Clip to edit or extend", "summary_format": None, "role_label": "Edit or extend"},
        "audio": {"label": "Sound", "summary_format": None},
        "generate_audio": {"label": "Audio", "summary_format": "audio"},
        "grounding": {"label": "Grounding", "summary_format": "grounding"},
        "version": {"label": "Model version", "summary_format": None, "placeholder": "Latest"},
        "input_image": {"label": "Opening frame", "summary_format": None},
        "keyframes": {"label": "Keyframes", "summary_format": None},
        "prompt_extend": {"label": "Let Wan rewrite the prompt", "page_label": "Rewrite prompt for the model", "summary_format": "prompt rewritten"},
    }


def _directed_descriptor(target_id: str) -> dict[str, Any]:
    media = {
        "image-to-video": {"id": "input_image", "kind": "image", "required": True, "max": 1},
        "keyframes-to-video": {"id": "keyframes", "kind": "keyframes", "required": True, "max": 10},
        "video-continuation": {"id": "input_video", "kind": "video", "required": True, "max": 1},
    }.get(target_id)
    return {
        "capabilities": {"keyframes": target_id == "keyframes-to-video", "continuation": target_id == "video-continuation", "version": True, "generated_audio": True, "grounding": True, "cancel": False},
        "cost": {"kind": "unknown"}, "form_kind": "directed", "state_binding": "directed_v1", "request_contract": "legacy",
        "mode": target_id.replace("-", "_"), "display_label": "FLUX 3", "control_metadata": _control_metadata(), "media_roles": [media] if media is not None else [],
    }


def _sampling_descriptor() -> dict[str, Any]:
    return {"capabilities": {"generated_audio": True, "cancel": True}, "state_binding": "sampling_v1", "finish_operation": "minimax-h3-finish-v1", "model_family": "minimax_h3", "control_metadata": _control_metadata()}


_H3_FIT_NOTE = "Stills and clips are cropped to the video frame from the centre."


def _h3_conditioning_descriptor(mode: str) -> dict[str, Any]:
    """Pinned local H3 conditioning targets: keyframe pane + catalog media slots."""
    metadata = _control_metadata()
    metadata["keyframes"] = {**metadata["keyframes"], "note": _H3_FIT_NOTE}
    metadata["control_video"] = {"label": "Motion guide", "summary_format": None, "role_label": "Motion guide", "note": _H3_FIT_NOTE}
    metadata["steps"] = {"label": "Steps", "summary_format": "{value} steps"}
    metadata["sampler_name"] = {"label": "Sampler", "summary_format": None}
    metadata["scheduler"] = {"label": "Scheduler", "summary_format": None}
    # Sound (lane fs-h3aud, measured in fs-h3audio): what each input does, for the page and the agent.
    metadata["reference_audios"] = {"label": "Voice & sound", "summary_format": None, "role_label": "Voice or music",
                                    "note": "Up to 3 files, each 2 to 15 s, 15 s together; role voice_of (subject) or music_bed; needs a reference picture or clip."}
    metadata["reference_video_sound"] = {"label": "Use this clip's sound", "summary_format": None,
                                         "note": "Indices of reference clips whose own sound becomes the shot's sound; off by default."}
    metadata["sound_track"] = {"label": "Sound track", "summary_format": None,
                               "note": "Plays exactly as given from the first frame; the picture follows it; cut to the shot length."}
    return {"capabilities": {"keyframes": True, "generated_audio": True, "cancel": True}, "state_binding": "catalog", "mode": mode,
            "control_extension": "h3", "finish_operation": "minimax-h3-finish-v1", "model_family": "minimax_h3", "control_metadata": metadata}


def _h3_continuation_descriptor(role: str) -> dict[str, Any]:
    """Extend / prepend / bridge an H3 take that carries its sidecar."""
    metadata = _control_metadata()
    metadata["source"] = {"label": "Take", "summary_format": None, "role_label": "Take",
                          "note": "A take made with its continuation data (.mctx.safetensors beside the mp4)."}
    metadata["target"] = {"label": "Fill gap to", "summary_format": None, "role_label": "Fill gap to",
                          "note": "The second take; same picture size as the first."}
    metadata["steps"] = {"label": "Steps", "summary_format": "{value} steps"}
    metadata["sampler_name"] = {"label": "Sampler", "summary_format": None}
    metadata["scheduler"] = {"label": "Scheduler", "summary_format": None}
    # capabilities.continuation stays off: it means the trimmed-clip continuation of the Continue
    # mode (the renderer re-encodes a range there, which would break a take's pair).
    return {"capabilities": {"generated_audio": True, "cancel": True}, "state_binding": "catalog",
            "mode": "video_continuation", "continuation_role": role, "finish_operation": "minimax-h3-finish-v1", "model_family": "minimax_h3", "control_metadata": metadata}


def _reference_descriptor() -> dict[str, Any]:
    metadata = _control_metadata()
    metadata["prompt"] = {**metadata["prompt"], "max_length": 5000}
    return {"capabilities": {"generated_audio": True, "cancel": False}, "cancel_notice": "GMI Cloud cannot stop a take once it has started", "eta_label": "about 3-6 min", "control_metadata": metadata}


# Lane p5-vid (VID-020, charter Video decision 2; artist 27.10.: "auf flux3(video) und wan koennen wir vorerst
# verzichten, weil lokal unser fokus ist, bezahlen wollen wir nichts"): the paid video targets FLUX 3 (Nous Portal)
# and Wan 3.0 (GMI Cloud) are neither offered nor accepted. A module constant on purpose: no env var, no setting,
# no tool reaches it; bringing a paid target back is a new decision of the artist. The provider code stays.
PAID_VIDEO_TARGETS_OFFERED = False
PAID_VIDEO_PROVIDER_IDS = frozenset({"flux3-managed", "gmi-cloud"})
PAID_TARGET_REASON_CODE = "paid_target_not_offered"
PAID_TARGET_MESSAGE = "Paid models are not offered."


def is_paid_video_provider(provider_id: Any) -> bool:
    """True for a paid video provider id the active registry does not offer.

    The product registry is built from ``PAID_VIDEO_TARGETS_OFFERED``; only a test fixture registry
    (``create_default_registry(paid_targets=True)``) registers them, to test the kept provider logic.
    """
    return type(provider_id) is str and provider_id in PAID_VIDEO_PROVIDER_IDS and provider_id not in DEFAULT_REGISTRY.provider_ids()


def paid_target_refusal() -> dict[str, Any]:
    """The finite refusal of a paid video target; built before any network or credential access."""
    return {"error": PAID_TARGET_MESSAGE, "reason_code": PAID_TARGET_REASON_CODE, "reason_text": PAID_TARGET_MESSAGE}


def create_default_registry(*, paid_targets: bool = False) -> ProviderRegistry:
    """Build the finite v1 catalog without reading any external files.

    ``paid_targets=True`` is for tests of the kept provider logic only (a fixture registry); the product
    always builds the catalog with the module constant ``PAID_VIDEO_TARGETS_OFFERED``.
    """
    include_paid = bool(paid_targets) or PAID_VIDEO_TARGETS_OFFERED
    registry = ProviderRegistry()
    if include_paid:
        _register_flux3_managed(registry)
    _register_local(registry)
    if include_paid:
        _register_gmi_cloud(registry)
    _register_local_operations(registry)
    return registry


def _register_flux3_managed(registry: ProviderRegistry) -> None:
    flux_controls = (
        {"id": "prompt", "kind": "textarea", "required": True},
        {"id": "duration", "kind": "integer", "min": 5, "max": 20, "step": 1},
        {"id": "aspect_ratio", "kind": "select", "options": ["auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16", "9:21"]},
        {"id": "generate_audio", "kind": "boolean"},
        {"id": "grounding", "kind": "boolean"},
    )
    flux_output = {"kind": "video", "resolution": "720p", "audio": "optional"}
    flux_admission = {"max_active_studio_jobs": 1, "min_submit_interval_seconds": 63}
    registry.register_provider("flux3-managed", "Nous", display_label="Nous Portal")
    for target_id, label in (
        ("text-to-video", "Text to Video"),
        ("image-to-video", "Image to Video"),
        ("keyframes-to-video", "Keyframes to Video"),
        ("video-continuation", "Video Continuation"),
    ):
        registry.register_target(
            "flux3-managed",
            _target(
                target_id,
                label,
                controls=flux_controls,
                output=flux_output,
                admission=flux_admission,
                descriptor=_directed_descriptor(target_id),
            ),
        )


def _register_local(registry: ProviderRegistry) -> None:
    h3_controls = (
        {"id": "prompt", "kind": "textarea", "required": True, "prompt_policy": "literal"},
        {"id": "duration_seconds", "kind": "integer", "min": 4, "max": 15, "step": 1, "default": 5},
        {"id": "seed", "kind": "integer", "min": 0, "max": 4294967295, "default": 1},
        {
            "id": "aspect_ratio",
            "kind": "select",
            "options": [
                "21:9 (Ultrawide)",
                "16:9 (Widescreen)",
                "4:3 (Standard)",
                "1:1 (Square)",
                "3:4 (Portrait Standard)",
                "9:16 (Portrait Widescreen)",
            ],
            "default": "16:9 (Widescreen)",
        },
        H3_MP.control("minimax-h3-t2v"),
    )
    registry.register_provider("comfyui-local", "ComfyUI Local", endpoint_label=_endpoint.current_label(), location="local", icon="pc", display_label="Local", selector_kind="recipe")
    registry.register_target(
        "comfyui-local",
        _target(
            "minimax-h3-t2v",
            "MiniMax H3 · Text to Video",
            controls=h3_controls,
            descriptor=_sampling_descriptor(),
            output={"kind": "video", "fps": 24, "audio": "required_generated"},
            admission={"max_active_studio_jobs": 1, "min_submit_interval_seconds": 0},
            health_policy="static_only",
        ),
    )
    # Versioned sampling target over the same pinned graph (v1 stays
    # byte-stable): steps / sampler / scheduler and the Draft / Native sizes (h3_megapixels).
    registry.register_target(
        "comfyui-local",
        _target(
            "minimax-h3-t2v-sampling-v2",
            "MiniMax H3 · Text to Video · Sampling",
            controls=(
                *h3_controls[:-1],
                {"id": "steps", "kind": "integer", "min": 1, "max": 100, "step": 1, "default": 20},
                {"id": "sampler_name", "kind": "select", "options": ["res_multistep", "euler"], "default": "res_multistep"},
                {"id": "scheduler", "kind": "select", "options": ["simple", "beta"], "default": "simple"},
                H3_MP.control("minimax-h3-t2v-sampling-v2"),
            ),
            descriptor=_sampling_descriptor(),
            output={"kind": "video", "fps": 24, "audio": "required_generated"},
            admission={"max_active_studio_jobs": 1, "min_submit_interval_seconds": 0},
            health_policy="static_only",
        ),
    )

    # Pinned local H3 conditioning targets (lane fs-h3b3, SPEC fs-h3a §2/§12):
    # one pinned native graph per checkpoint; absent roles are removed from the
    # graph structurally.  Turbo route defaults (er_sde / beta / 12, 0.5 MP).
    h3_conditioning_controls = (
        *h3_controls[:-1],
        H3_MP.control("minimax-h3-frames-v1"),
        {"id": "steps", "kind": "integer", "min": 1, "max": 100, "step": 1, "default": 12},
        {"id": "sampler_name", "kind": "select", "options": ["er_sde", "res_multistep", "euler"], "default": "er_sde"},
        {"id": "scheduler", "kind": "select", "options": ["beta", "simple"], "default": "beta"},
        {"id": "keyframes", "kind": "keyframes", "max": 10},
    )
    for target_id, label, mode, extra in (
        ("minimax-h3-frames-v1", "MiniMax H3 · Frames", "keyframes_to_video", ({"id": "sound_track", "kind": "audio", "max": 1},)),
        (
            "minimax-h3-references-v1",
            "MiniMax H3 · References",
            "references_to_video",
            (
                {"id": "reference_images", "kind": "image", "max": 9},
                {"id": "reference_videos", "kind": "video", "max": 3},
                {"id": "control_video", "kind": "video", "max": 1},
                {"id": "reference_audios", "kind": "audio", "max": 3},
                {"id": "reference_video_sound", "kind": "informational"},
                {"id": "sound_track", "kind": "audio", "max": 1},
            ),
        ),
    ):
        registry.register_target(
            "comfyui-local",
            _target(
                target_id,
                label,
                controls=tuple(H3_MP.control(target_id) if c.get("id") == "megapixels" else c for c in (*h3_conditioning_controls, *extra)),
                descriptor=_h3_conditioning_descriptor(mode),
                output={"kind": "video", "fps": 24, "audio": "required_generated"},
                admission={"max_active_studio_jobs": 1, "min_submit_interval_seconds": 0},
                health_policy="static_only",
                input_kind=mode,
            )
        )

    # Continuation targets on take sidecars (lane fs-obvb, fs-obvpm A/B winner):
    # one take (extend / prepend) or two (bridge) with a verified .mctx pair.
    # Sampling defaults are the A/B arm's (res_multistep / simple / 20).
    continuation_controls = (
        {"id": "prompt", "kind": "textarea", "required": True, "prompt_policy": "literal"},
        {"id": "duration_seconds", "kind": "integer", "min": 2, "max": 14, "step": 1, "default": 6},
        {"id": "seed", "kind": "integer", "min": 0, "max": 4294967295, "default": 1},
        {"id": "steps", "kind": "integer", "min": 1, "max": 100, "step": 1, "default": 20},
        {"id": "sampler_name", "kind": "select", "options": ["res_multistep", "er_sde", "euler"], "default": "res_multistep"},
        {"id": "scheduler", "kind": "select", "options": ["simple", "beta"], "default": "simple"},
        {"id": "source", "kind": "video", "required": True, "max": 1},
    )
    for target_id, label, extra in (
        ("minimax-h3-extend-mctx-v1", "MiniMax H3 · Extend", ()),
        ("minimax-h3-prepend-mctx-v1", "MiniMax H3 · Prepend", ()),
        ("minimax-h3-bridge-mctx-v1", "MiniMax H3 · Fill gap", ({"id": "target", "kind": "video", "required": True, "max": 1},)),
    ):
        registry.register_target(
            "comfyui-local",
            _target(
                target_id,
                label,
                controls=(*continuation_controls, *extra),
                descriptor=_h3_continuation_descriptor(target_id.split("-")[2]),
                output={"kind": "video", "fps": 24, "audio": "required_generated"},
                admission={"max_active_studio_jobs": 1, "min_submit_interval_seconds": 0},
                health_policy="static_only",
                input_kind="video_continuation",
            ),
        )



def _register_gmi_cloud(registry: ProviderRegistry) -> None:
    # Wan 3.0 through the GMI Cloud request queue (contract:
    # docs/plans/film-suite-gmi-wan3-contract.md).  Media controls are image,
    # video and audio slots; the backend uploads local files and sends only the
    # hosted model id pinned in gmi_cloud_provider.MODEL_ID.
    wan3_controls = (
        {"id": "prompt", "kind": "textarea", "required": True, "prompt_policy": "literal"},
        {"id": "negative_prompt", "kind": "textarea"},
        {"id": "first_frame", "kind": "image", "max": 1},
        {"id": "last_frame", "kind": "image", "max": 1},
        {"id": "reference_images", "kind": "image", "max": 10},
        {"id": "reference_videos", "kind": "video", "max": 5},
        {"id": "input_video", "kind": "video", "max": 1},
        {"id": "audio", "kind": "audio", "max": 1},
        {"id": "duration_seconds", "kind": "integer", "min": 2, "max": 30, "step": 1, "default": 5},
        {"id": "resolution", "kind": "select", "options": ["480P", "720P", "1080P"], "default": "720P"},
        {
            "id": "ratio",
            "kind": "select",
            "options": ["adaptive", "16:9", "9:16", "1:1", "4:3", "3:4"],
            "default": "adaptive",
        },
        {"id": "seed", "kind": "integer", "min": 0, "max": 2147483647},
        {"id": "prompt_extend", "kind": "boolean", "default": False},
    )
    registry.register_provider("gmi-cloud", "GMI Cloud")
    registry.register_target(
        "gmi-cloud",
        _target(
            "wan3-video",
            "Wan 3.0",
            controls=wan3_controls,
            descriptor=_reference_descriptor(),
            output={"kind": "video", "fps": 30, "audio": "required_generated"},
            admission={"max_active_studio_jobs": 2, "min_submit_interval_seconds": 0},
            health_policy="static_only",
            input_kind="references_to_video",
        ),
    )


def _register_local_operations(registry: ProviderRegistry) -> None:
    # H3 finishing (lane w1-h3): latent upscale + RIFE 48 fps on a finished take, its own operation.
    registry.register_operation(
        "comfyui-local",
        _target(
            "minimax-h3-finish-v1",
            "MiniMax H3 · Finish",
            controls=({"id": "upscale", "kind": "boolean", "default": True}, {"id": "rife", "kind": "boolean", "default": False}),
            output={"kind": "video", "audio": "required_generated"},
            admission={"max_active_studio_jobs": 1, "min_submit_interval_seconds": 0},
            health_policy="static_only",
            input_kind="video_continuation",
        ),
    )
    # Video upscale (lane w23-vsr, VID-153): RelayVSR on any finished video take, the default video upscaler.
    registry.register_operation(
        "comfyui-local",
        _target(
            "relayvsr-video-upscale-v1",
            "RelayVSR · Video upscale",
            controls=(
                {"id": "model", "kind": "select", "options": ["auto", "x2", "x4"], "default": "auto"},
                {"id": "size", "kind": "select", "options": ["auto", "native", "1080p", "1440p", "2160p", "custom"], "default": "auto"},
                {"id": "filter", "kind": "select", "options": ["lanczos", "bicubic", "bilinear", "area", "nearest-exact"], "default": "lanczos"},
            ),
            output={"kind": "video", "audio": "required_generated"},
            admission={"max_active_studio_jobs": 1, "min_submit_interval_seconds": 0},
            health_policy="static_only",
            input_kind="video_continuation",
        ),
    )


DEFAULT_REGISTRY = create_default_registry()


def catalog() -> dict[str, Any]:
    return DEFAULT_REGISTRY.to_catalog()


def catalog_json() -> str:
    return DEFAULT_REGISTRY.to_json()


def resolve(provider_id: str, target_id: str) -> ResolvedTarget:
    return DEFAULT_REGISTRY.resolve(provider_id, target_id)


__all__ = [
    "CATALOG_SCHEMA",
    "DEFAULT_REGISTRY",
    "DuplicateRegistrationError",
    "GenerationDomainError",
    "InvalidRequestError",
    "PAID_TARGET_MESSAGE",
    "PAID_TARGET_REASON_CODE",
    "PAID_VIDEO_PROVIDER_IDS",
    "PAID_VIDEO_TARGETS_OFFERED",
    "ProviderRegistry",
    "ResolvedTarget",
    "TargetSpec",
    "catalog",
    "catalog_json",
    "create_default_registry",
    "is_paid_video_provider",
    "paid_target_refusal",
    "resolve",
]
