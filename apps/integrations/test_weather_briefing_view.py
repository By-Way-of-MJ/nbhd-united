"""``POST /runtime/<tenant>/weather/briefing/`` writes the note's weather section itself."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.journal.models import Document
from apps.orchestrator.briefing_weather import (
    NO_LOCATION_MESSAGE_LINE,
    NO_LOCATION_SECTION_LINE,
    UNAVAILABLE_MESSAGE_LINE,
    DayForecast,
    WeatherReport,
)
from apps.tenants.models import Tenant
from apps.tenants.test_utils import seed_internal_key

User = get_user_model()

_FETCH = "apps.orchestrator.briefing_weather.fetch_briefing_weather"


def _ok_report():
    return WeatherReport(
        status="ok",
        source="profile_city",
        location_label="Osaka",
        unit="C",
        today=DayForecast(date="2026-09-30", temp_min=18.0, temp_max=24.0, condition="partly cloudy"),
        tomorrow=DayForecast(date="2026-10-01", temp_min=16.0, temp_max=22.0, condition="rain", precip_prob_max=80),
    )


@override_settings(NBHD_INTERNAL_API_KEY="test-key")
class RuntimeWeatherBriefingViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="weather-view", password="pass", timezone="Asia/Tokyo")
        self.tenant = Tenant.objects.create(user=self.user, status=Tenant.Status.ACTIVE)
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {
            "HTTP_X_NBHD_INTERNAL_KEY": "test-key",
            "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id),
        }
        self.url = f"/api/v1/integrations/runtime/{self.tenant.id}/weather/briefing/"

    def _post(self, body=None):
        return self.client.post(self.url, body or {}, format="json", **self.headers)

    def _note(self, slug):
        return Document.objects.get(tenant=self.tenant, kind=Document.Kind.DAILY, slug=slug)

    def test_requires_internal_auth(self):
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, 401)

    def test_invalid_date_is_400(self):
        response = self._post({"date": "yesterday"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "invalid_request")

    @patch(_FETCH, return_value=_ok_report())
    def test_ok_writes_weather_section_and_returns_verbatim_line(self, mock_fetch):
        response = self._post({"date": "2026-09-30"})

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.data["status"], "ok")
        self.assertEqual(response.data["source"], "profile_city")
        self.assertEqual(response.data["location_label"], "Osaka")
        self.assertEqual(response.data["message_line"], "Osaka: 18–24°C, partly cloudy — light layers.")
        self.assertEqual(response.data["date"], "2026-09-30")
        mock_fetch.assert_called_once()
        self.assertEqual(mock_fetch.call_args.kwargs["on_date"].isoformat(), "2026-09-30")

        note = self._note("2026-09-30")
        self.assertIn("## Weather\n**Today:** 18–24°C, partly cloudy. Light layers.\n", note.markdown)
        self.assertIn("**Tomorrow:** 16–22°C, rain.", note.markdown)
        self.assertEqual(
            response.data["section_markdown"],
            "**Today:** 18–24°C, partly cloudy. Light layers.\n**Tomorrow:** 16–22°C, rain.",
        )

    @patch(_FETCH, return_value=_ok_report())
    def test_preserves_other_sections_and_is_idempotent(self, _mock_fetch):
        Document.objects.create(
            tenant=self.tenant,
            kind=Document.Kind.DAILY,
            slug="2026-09-30",
            title="2026-09-30",
            markdown=(
                "# 2026-09-30\n\n"
                "## Morning Report\n### Overnight Summary\n- quiet night\n\n"
                "## Weather\n**Today:** \n**Tomorrow:** \n\n"
                "## News & Interests\n- headline\n\n"
                "### 07:05 — MJ\nEarly entry.\n"
            ),
        )

        first = self._post({"date": "2026-09-30"})
        after_first = self._note("2026-09-30").markdown
        second = self._post({"date": "2026-09-30"})
        after_second = self._note("2026-09-30").markdown

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(after_first, after_second)
        self.assertIn("## Morning Report\n### Overnight Summary\n- quiet night\n", after_first)
        self.assertIn("## News & Interests\n- headline\n", after_first)
        self.assertTrue(after_first.endswith("### 07:05 — MJ\nEarly entry.\n"))
        self.assertEqual(after_first.count("## Weather"), 1)
        self.assertIn(
            "## Weather\n**Today:** 18–24°C, partly cloudy. Light layers.\n**Tomorrow:** 16–22°C, rain.\n", after_first
        )
        self.assertNotIn("**Today:** \n", after_first)

    @patch(_FETCH, return_value=WeatherReport(status="no_location"))
    def test_no_location_writes_the_honest_line(self, _mock_fetch):
        response = self._post({"date": "2026-09-30"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "no_location")
        self.assertIsNone(response.data["location_label"])
        self.assertEqual(response.data["message_line"], NO_LOCATION_MESSAGE_LINE)
        self.assertIn(f"## Weather\n{NO_LOCATION_SECTION_LINE}\n", self._note("2026-09-30").markdown)

    @patch(
        _FETCH, return_value=WeatherReport(status="unavailable", source="profile_coords", error="forecast_ReadTimeout")
    )
    def test_unavailable_writes_the_honest_line_without_the_reason(self, _mock_fetch):
        response = self._post({"date": "2026-09-30"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "unavailable")
        self.assertEqual(response.data["message_line"], UNAVAILABLE_MESSAGE_LINE)
        markdown = self._note("2026-09-30").markdown
        self.assertIn("## Weather\n**Today:** Weather couldn't be fetched this morning.\n", markdown)
        self.assertNotIn("ReadTimeout", markdown)

    @patch(_FETCH, return_value=_ok_report())
    def test_date_defaults_to_tenant_local_today(self, mock_fetch):
        from apps.common.tenant_tz import tenant_today

        response = self._post()

        self.assertEqual(response.status_code, 200)
        expected = tenant_today(self.tenant)
        self.assertEqual(response.data["date"], str(expected))
        self.assertEqual(mock_fetch.call_args.kwargs["on_date"], expected)
        self.assertTrue(
            Document.objects.filter(tenant=self.tenant, kind=Document.Kind.DAILY, slug=str(expected)).exists()
        )
