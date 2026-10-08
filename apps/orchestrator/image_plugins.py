"""Which plugin dirs the tenant's RUNNING image actually ships.

The OpenClaw container reports the plugin dirs under ``/opt/nbhd/plugins`` in
its container-started hook; we keep that list on the tenant together with the
``container_image_tag`` it was reported under. ``config_generator`` asks
``image_has_plugin`` before adding a plugin that older images do not carry.

Why: OpenClaw 2026.9.4 never brings its gateway up when ``openclaw.json`` names
a plugin dir the image lacks, and neither the binary version nor
``container_image_tag`` can prove a dir exists (tags are unordered SHAs, and
canary images diverge from the stored tag). On 2026-10-08 opening
``PROJECTS_V2_TENANT_IDS`` to ``*`` crash-looped every woken tenant on an image
without ``nbhd-project-tools``.

Fail closed: an image that predates the report never sends one, so its list is
empty and it is never handed a plugin gated here. A report recorded under a
different image tag is ignored until the new image reports for itself.

Gate any plugin added after this module the same way.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MAX_REPORTED = 64


def _clean(reported: object) -> list[str]:
    if not isinstance(reported, list):
        return []
    ids = {item for item in reported[:_MAX_REPORTED] if isinstance(item, str) and _PLUGIN_ID_RE.match(item)}
    return sorted(ids)


def image_plugin_ids(tenant: object | None) -> frozenset[str]:
    """Plugin ids the tenant's current image reported; empty when unknown."""
    if tenant is None:
        return frozenset()
    reported_tag = getattr(tenant, "image_plugins_tag", "") or ""
    if reported_tag != (getattr(tenant, "container_image_tag", "") or ""):
        return frozenset()
    return frozenset(_clean(getattr(tenant, "image_plugin_ids", None)))


def image_has_plugin(tenant: object | None, plugin_id: str) -> bool:
    return bool(plugin_id) and plugin_id in image_plugin_ids(tenant)


def record_image_plugins(tenant, reported: object) -> bool:
    """Store a boot report. Returns True when the trusted set changed.

    ``reported`` is the hook body's ``plugins`` value; anything that is not a
    list of plugin ids (including the empty body older images send) records an
    empty list, so a tenant moved back to an old image stops being trusted.
    """
    before = image_plugin_ids(tenant)
    ids = _clean(reported)
    tag = getattr(tenant, "container_image_tag", "") or ""
    if list(tenant.image_plugin_ids or []) != ids or (tenant.image_plugins_tag or "") != tag:
        tenant.image_plugin_ids = ids
        tenant.image_plugins_tag = tag
        tenant.save(update_fields=["image_plugin_ids", "image_plugins_tag"])
    return image_plugin_ids(tenant) != before
