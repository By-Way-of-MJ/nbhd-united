"""Shared API/tenant-payload rollout gate, using the existing comma-list helper."""

from apps.router.chat_gates import _tenant_allowed


def projects_v2_enabled(tenant):
    return _tenant_allowed(tenant, "PROJECTS_V2_TENANT_IDS")
