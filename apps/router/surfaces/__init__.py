"""A2UI v1.0 surface allowlist and validator."""

from .catalog import ALLOWED_ACTIONS, CATALOG_ID
from .validate import SurfaceError, validate_surface

__all__ = ["ALLOWED_ACTIONS", "CATALOG_ID", "SurfaceError", "validate_surface"]
