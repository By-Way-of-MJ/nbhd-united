"""Plain-text project input boundary; caps apply after Unicode cleanup."""

import unicodedata

from rest_framework.exceptions import ValidationError


def clean_text(value, *, limit=500, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError("Text must be a string.")
    # Preserve word boundaries at whitespace controls; remove formatting controls,
    # including zero-width and bidi overrides/isolates. No markup interpretation.
    value = "".join(
        " " if c.isspace() else c for c in value if c.isspace() or unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
    )
    value = " ".join(value.split())
    if len(value) > limit:
        raise ValidationError(f"Text must be at most {limit} characters.")
    if required and not value:
        raise ValidationError("A title is required.")
    return value


def clean_payload(value):
    """Normalize free text within the legacy goal target JSON without changing shape."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        return [clean_payload(item) for item in value]
    if isinstance(value, dict):
        return {clean_text(key, limit=120): clean_payload(item) for key, item in value.items()}
    return value
