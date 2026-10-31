"""POST /api/v1/tenants/device-timezone/ — the app reports its IANA zone.

HealthKit is the only other path a device zone reaches the server, so users
without Health sync never get the "move your reminders to local time?" ask.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from .models import Tenant, User, UserSituation

URL = "/api/v1/tenants/device-timezone/"


@override_settings(NBHD_DISABLE_BACKGROUND_THREADS=True)
class DeviceTimezoneViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.user = User.objects.create_user(username="device-tz", password="pw")
        self.tenant = Tenant.objects.create(
            user=self.user,
            status=Tenant.Status.ACTIVE,
            situational_context_enabled=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _post(self, payload):
        return self.client.post(URL, payload, format="json")

    def test_unauthenticated_is_401(self):
        client = APIClient()
        response = client.post(URL, {"device_tz": "Asia/Tokyo"}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_new_zone_records_situation_and_pushes_once(self):
        with patch("apps.orchestrator.workspace_envelope.push_user_md_in_background") as push:
            response = self._post({"device_tz": "America/New_York"})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {"ok": True, "changed": True})
        situation = UserSituation.objects.get(tenant=self.tenant)
        self.assertEqual(situation.device_tz, "America/New_York")
        self.assertEqual(situation.device_tz_source_device, "app")
        push.assert_called_once()
        self.assertEqual(push.call_args.kwargs.get("trigger"), "place_observation")

    def test_same_zone_within_window_is_unchanged_and_does_not_push(self):
        with patch("apps.orchestrator.workspace_envelope.push_user_md_in_background") as push:
            first = self._post({"device_tz": "America/New_York"})
            self.assertEqual(first.status_code, 200, first.data)
            self.assertTrue(first.data["changed"])
            push.assert_called_once()
            push.reset_mock()

            second = self._post({"device_tz": "America/New_York"})

        self.assertEqual(second.status_code, 200, second.data)
        self.assertEqual(second.data, {"ok": True, "changed": False})
        push.assert_not_called()

    def test_invalid_iana_zone_is_a_noop_not_an_error(self):
        response = self._post({"device_tz": "Not/AZone"})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {"ok": True, "changed": False})
        self.assertFalse(UserSituation.objects.filter(tenant=self.tenant).exists())
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "UTC")

    def test_self_heals_an_untouched_utc_profile(self):
        self.user.timezone = "UTC"
        self.user.save(update_fields=["timezone"])

        response = self._post({"device_tz": "Asia/Tokyo"})

        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "Asia/Tokyo")

    def test_does_not_override_an_explicit_profile_timezone(self):
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])

        response = self._post({"device_tz": "America/New_York"})

        self.assertEqual(response.status_code, 200, response.data)
        self.user.refresh_from_db()
        self.assertEqual(self.user.timezone, "Asia/Tokyo")

    def test_disabled_situational_context_records_nothing(self):
        self.tenant.situational_context_enabled = False
        self.tenant.save(update_fields=["situational_context_enabled"])

        response = self._post({"device_tz": "Asia/Tokyo"})

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {"ok": True, "changed": False})
        self.assertFalse(UserSituation.objects.filter(tenant=self.tenant).exists())

    def test_invalid_body_returns_400(self):
        for payload in ({}, {"device_tz": 123}, [], {"other": "Asia/Tokyo"}):
            with self.subTest(payload=payload):
                response = self._post(payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.data, {"error": "invalid_body"})

    def test_user_without_tenant_returns_404(self):
        user = User.objects.create_user(username="no-tenant-device-tz", password="pw")
        self.client.force_authenticate(user)

        response = self._post({"device_tz": "Asia/Tokyo"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, {"error": "no_tenant"})
