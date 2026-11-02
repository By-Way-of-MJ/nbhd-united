"""Reserved identity for person-requested, self-cleaning prep dispatches."""

STEP_PREP_NOW_PREFIX = "Step Prep Now "


def _is_system_dispatch(row) -> bool:
    return row.source == "system" and row.creation_path == "internal" and row.name.startswith(STEP_PREP_NOW_PREFIX)
