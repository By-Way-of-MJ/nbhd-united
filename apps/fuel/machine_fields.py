"""Strict leaf validation shared by workout ingress and PII exclusions.

Only these exact locations qualify. Legacy rows and non-serializer writers
still get value checks during PII traversal; malformed values remain prose.
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


def _leaves(value, parts, path=()):
    if not parts:
        yield path, value
    elif parts[0] == "*" and isinstance(value, list):
        for index, child in enumerate(value):
            yield from _leaves(child, parts[1:], (*path, index))
    elif isinstance(value, dict) and parts[0] in value:
        yield from _leaves(value[parts[0]], parts[1:], (*path, parts[0]))


def workout_machine_errors(detail):
    """Validate supplied leaves before normalization or legacy grandfathering.

    Missing optional keys are allowed. Unknown extension keys are left to PII,
    including catalog_ref metadata outside the two explicit string leaves.
    """
    errors = []
    for container in ("exercises", "skills"):
        for suffix, adapter in _LEAF_ADAPTERS.items():
            for path, value in _leaves(detail, (container, "*", *suffix)):
                try:
                    adapter.validate_python(value, strict=True)
                except ValidationError as exc:
                    for error in exc.errors(include_url=False, include_context=False, include_input=False):
                        errors.append({**error, "loc": [*path, *error["loc"]]})
    return errors
