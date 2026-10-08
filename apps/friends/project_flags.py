"""Shared API/tenant-payload rollout gate, using the existing comma-list helper."""

from django.conf import settings

from apps.router.chat_gates import _tenant_allowed


def projects_v2_enabled(tenant):
    return _tenant_allowed(tenant, "PROJECTS_V2_TENANT_IDS")


def project_tools_ready(tenant):
    """Projects v2 is on AND the tenant's running image ships the project plugin.

    The rollout flag alone opens Projects in the app. The assistant's project
    tools also need the plugin dir in the image, so anything that loads the
    plugin or tells the assistant to call it must ask this instead.
    """
    from apps.orchestrator.image_plugins import image_has_plugin

    plugin_id = str(getattr(settings, "OPENCLAW_PROJECT_TOOLS_PLUGIN_ID", "nbhd-project-tools") or "").strip()
    return projects_v2_enabled(tenant) and image_has_plugin(tenant, plugin_id)
