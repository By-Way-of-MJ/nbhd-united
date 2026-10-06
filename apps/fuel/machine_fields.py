"""Value guards for workout PII exclusions, never ingress requirements.

Only conforming values at these exact locations qualify. Unknown and legacy
values remain prose and receive normal authoring without adding save errors.
"""

from typing import Annotated, Literal

from pydantic import AfterValidator, TypeAdapter, ValidationError

from . import catalog
from .set_contract import METRIC_BODYWEIGHT_REPS, METRIC_HOLD_TIME, METRIC_WEIGHTED_REPS, _Logged


def _catalog_slug(value: str) -> str:
    entry = catalog.match(value)
    if entry is None or entry.slug != value:
        raise ValueError("slug must exactly match a workout catalog entry")
    return value


_LEAF_ADAPTERS = {
    ("role",): TypeAdapter(Literal["primary", "accessory", "warmup", "mobility"]),
    ("catalog_ref", "slug"): TypeAdapter(Annotated[str, AfterValidator(_catalog_slug)]),
    ("catalog_ref", "matched_by"): TypeAdapter(Literal["canonical", "slug", "alias", "plural", "equipment_prefix"]),
    ("sets", "*", "type"): TypeAdapter(Literal[METRIC_WEIGHTED_REPS, METRIC_BODYWEIGHT_REPS, METRIC_HOLD_TIME]),
    ("sets", "*", "logged", "at"): TypeAdapter(Annotated[str, AfterValidator(_Logged.utc_timestamp)]),
}
WORKOUT_MACHINE_PATHS = tuple(
    ".".join((container + "[]", *suffix)).replace(".*", "[]")
    for container in ("exercises", "skills")
    for suffix in _LEAF_ADAPTERS
)


def workout_machine_signature():
    """Invalidate persisted repair cursors when eligible leaf values change."""
    return (
        tuple((path, adapter.json_schema()) for path, adapter in _LEAF_ADAPTERS.items()),
        tuple(entry.slug for entry in catalog._catalog().entries),
        "utc-timestamp-v1",  # Bump if _Logged.utc_timestamp's accepted format changes.
    )


def valid_workout_machine_scalar(path, value):
    """Never exempt objects, unknown vocabulary, aliases, or malformed dates."""
    if not isinstance(value, str) or len(path) < 3 or path[0] not in ("exercises", "skills"):
        return False
    suffix = tuple("*" if isinstance(part, int) else part for part in path[2:])
    adapter = _LEAF_ADAPTERS.get(suffix)
    if adapter is None:
        return False
    try:
        adapter.validate_python(value, strict=True)
    except ValidationError:
        return False
    return True
