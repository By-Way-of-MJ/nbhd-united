"""Settings for proactive signals ("Let nbhd notice things for me").

Kept out of ``apps/tenants/views.py`` on purpose: that module is digest-pinned
by ``apps/orchestrator/test_openclaw_round_nine`` (request-path parity).
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Tenant


class ProactiveSettingsView(APIView):
    """Get/update "Let nbhd notice things for me" (proactive calendar heads-ups).

    ``enabled`` is the user's own opt-in; delivery additionally needs the
    PROACTIVE_SIGNALS_TENANT_IDS canary gate. ``work_countries`` are ISO-2
    codes whose public holidays are checked against meeting dates.
    """

    permission_classes = [IsAuthenticated]

    def _payload(self, tenant):
        from apps.router.chat_gates import _tenant_allowed
        from apps.router.proactive_signals import COUNTRY_LABEL, COUNTRY_TZ, GATE_SETTING

        return {
            "enabled": tenant.proactive_signals_enabled,
            "work_countries": list(tenant.work_countries or []),
            # Whether this account is in the rollout yet (the toggle is inert until then).
            "available": _tenant_allowed(tenant, GATE_SETTING),
            "supported_countries": [{"code": code, "label": COUNTRY_LABEL.get(code, code)} for code in COUNTRY_TZ],
        }

    def get(self, request):
        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(self._payload(tenant))

    def patch(self, request):
        from apps.router.proactive_signals import COUNTRY_TZ, clean_countries

        try:
            tenant = request.user.tenant
        except Tenant.DoesNotExist:
            return Response({"detail": "No tenant found."}, status=status.HTTP_404_NOT_FOUND)
        update_fields = []
        if "enabled" in request.data:
            enabled = request.data["enabled"]
            if not isinstance(enabled, bool):
                return Response({"enabled": ["Must be true or false."]}, status=status.HTTP_400_BAD_REQUEST)
            tenant.proactive_signals_enabled = enabled
            update_fields.append("proactive_signals_enabled")
        if "work_countries" in request.data:
            raw = request.data["work_countries"]
            if not isinstance(raw, list) or len(raw) > 10:
                return Response(
                    {"work_countries": ["Send a list of up to 10 country codes."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            unknown = [c for c in raw if str(c or "").strip().upper() not in COUNTRY_TZ]
            if unknown:
                return Response(
                    {"work_countries": ["Unsupported country code. Use one from supported_countries."]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            tenant.work_countries = clean_countries(raw)
            update_fields.append("work_countries")
        if update_fields:
            tenant.save(update_fields=[*update_fields, "updated_at"])
        return Response(self._payload(tenant))
