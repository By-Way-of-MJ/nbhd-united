"""Profile photos: upload pipeline (clean + safety check, fail closed), who can see a
photo, change/remove, and reporting."""

import io
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from PIL import Image
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from . import access, services
from .models import ContentReport, Friendship, NeighborPhoto, NeighborProfile
from .test_pr6 import _edge, _profile, _tenant


def _jpeg(size=(800, 600), color=(90, 120, 160), exif_gps=False) -> bytes:
    image = Image.new("RGB", size, color)
    out = io.BytesIO()
    kwargs = {}
    if exif_gps:
        exif = Image.Exif()
        exif[0x010F] = "PhoneMaker"  # Make
        exif[0x8825] = {2: (35.0, 41.0, 0.0)}  # GPSInfo → latitude
        kwargs["exif"] = exif
    image.save(out, format="JPEG", **kwargs)
    return out.getvalue()


def _moderation(flagged=False):
    return SimpleNamespace(results=[SimpleNamespace(flagged=flagged)])


@override_settings(OPENAI_API_KEY="test-key")
class ProfilePhotoTests(TestCase):
    def setUp(self):
        self.a, self.b, self.c, self.d = [_tenant("ph_" + n) for n in "abcd"]
        self.pa = _profile(self.a, "aya")
        self.pb = _profile(self.b, "ben")
        self.pc = _profile(self.c, "cleo")
        self.pd = _profile(self.d, "dan")
        self.ab = _edge(self.a, self.b)

    def client_for(self, tenant):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(tenant.user).access_token}")
        return client

    def upload(self, tenant, data, flagged=False):
        with patch("openai.resources.moderations.Moderations.create", return_value=_moderation(flagged)) as check:
            response = self.client_for(tenant).post(
                "/api/v1/friends/profile/photo/",
                {"photo": SimpleUploadedFile("me.jpg", data, content_type="image/jpeg")},
                format="multipart",
            )
        return response, check

    # ── Upload ──────────────────────────────────────────────────────────────

    def test_upload_is_cleaned_squared_and_checked(self):
        response, check = self.upload(self.a, _jpeg(exif_gps=True))
        self.assertEqual(response.status_code, 200)
        self.pa.refresh_from_db()
        self.assertEqual(self.pa.photo_version, 1)
        self.assertEqual(response.json()["photo_url"], f"/api/v1/friends/photos/{self.pa.id}/?v=1")
        check.assert_called_once()
        stored = bytes(NeighborPhoto.objects.get(profile=self.pa).image)
        with Image.open(io.BytesIO(stored)) as image:
            self.assertEqual(image.size, (512, 512))
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(len(image.getexif()), 0)  # no location/metadata survives

    def test_flagged_photo_is_refused_and_nothing_is_stored(self):
        response, _ = self.upload(self.a, _jpeg(), flagged=True)
        self.assertEqual(response.status_code, 422)
        self.assertFalse(NeighborPhoto.objects.filter(profile=self.pa).exists())
        self.pa.refresh_from_db()
        self.assertIsNone(self.pa.photo_version)

    def test_check_that_cannot_run_fails_closed(self):
        with patch("openai.resources.moderations.Moderations.create", side_effect=RuntimeError("down")):
            response = self.client_for(self.a).post(
                "/api/v1/friends/profile/photo/",
                {"photo": SimpleUploadedFile("me.jpg", _jpeg(), content_type="image/jpeg")},
                format="multipart",
            )
        self.assertEqual(response.status_code, 503)
        self.assertFalse(NeighborPhoto.objects.filter(profile=self.pa).exists())

    @override_settings(OPENAI_API_KEY="")
    def test_missing_key_fails_closed(self):
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            response, check = self.upload(self.a, _jpeg())
        self.assertEqual(response.status_code, 503)
        check.assert_not_called()

    def test_not_an_image_or_too_small_is_refused_before_the_check(self):
        for data in (b"not an image at all", _jpeg(size=(40, 40))):
            response, check = self.upload(self.a, data)
            self.assertEqual(response.status_code, 400)
            check.assert_not_called()

    # ── Change / remove ─────────────────────────────────────────────────────

    def test_changing_bumps_the_version_and_removing_deletes_the_bytes(self):
        self.upload(self.a, _jpeg())
        self.upload(self.a, _jpeg(color=(200, 40, 40)))
        self.pa.refresh_from_db()
        self.assertEqual(self.pa.photo_version, 2)
        self.assertEqual(NeighborPhoto.objects.filter(profile=self.pa).count(), 1)
        response = self.client_for(self.a).delete("/api/v1/friends/profile/photo/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(NeighborPhoto.objects.filter(profile=self.pa).exists())
        self.pa.refresh_from_db()
        self.assertIsNone(self.pa.photo_version)
        self.assertIsNone(self.client_for(self.a).get("/api/v1/friends/profile/").json()["photo_url"])

    # ── Who can see it ──────────────────────────────────────────────────────

    def test_neighbor_sees_it_stranger_and_blocked_do_not(self):
        self.upload(self.a, _jpeg())
        url = f"/api/v1/friends/photos/{self.pa.id}/?v=1"
        ok = self.client_for(self.b).get(url)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok["Content-Type"], "image/jpeg")
        self.assertIn("immutable", ok["Cache-Control"])
        self.assertEqual(self.client_for(self.c).get(url).status_code, 404)  # not connected
        self.assertEqual(self.client_for(self.a).get(url).status_code, 200)  # myself
        Friendship.objects.filter(id=self.ab.id).update(status=Friendship.Status.BLOCKED)
        self.assertEqual(self.client_for(self.b).get(url).status_code, 404)

    def test_someone_in_the_same_project_sees_it(self):
        self.upload(self.a, _jpeg())
        ad = _edge(self.a, self.d)
        goal = services.create_mission(self.a, self.a.user, member_friendship_ids=[str(ad.id)], title="Garden")
        services.join_mission(self.d, self.d.user, goal.id)
        url = f"/api/v1/friends/photos/{self.pa.id}/?v=1"
        self.assertEqual(self.client_for(self.d).get(url).status_code, 200)
        self.assertEqual(self.client_for(self.c).get(url).status_code, 404)

    def test_neighbor_lists_carry_the_photo_url(self):
        self.upload(self.a, _jpeg())
        home = self.client_for(self.b).get("/api/v1/friends/home/").json()
        [aya] = [n for n in home["neighbors"] if n["handle"] == "aya"]
        self.assertEqual(aya["photo_url"], f"/api/v1/friends/photos/{self.pa.id}/?v=1")

    # ── Report ──────────────────────────────────────────────────────────────

    def test_reporting_hides_it_for_the_reporter_and_is_recorded(self):
        self.upload(self.a, _jpeg())
        response = self.client_for(self.b).post(
            "/api/v1/friends/report/",
            {"target_kind": "profile_photo", "target_id": str(self.pa.id), "reason": "Not appropriate"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["hidden"])
        report = ContentReport.objects.get(target_kind="profile_photo")
        self.assertEqual(report.photo_profile_id, self.pa.id)
        url = f"/api/v1/friends/photos/{self.pa.id}/?v=1"
        self.assertEqual(self.client_for(self.b).get(url).status_code, 404)
        # Still hidden after Aya changes it.
        self.upload(self.a, _jpeg(color=(10, 200, 10)))
        self.assertEqual(self.client_for(self.b).get(f"/api/v1/friends/photos/{self.pa.id}/?v=2").status_code, 404)

    def test_cannot_report_a_photo_you_cannot_see(self):
        self.upload(self.a, _jpeg())
        response = self.client_for(self.c).post(
            "/api/v1/friends/report/",
            {"target_kind": "profile_photo", "target_id": str(self.pa.id), "reason": "x"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(ContentReport.objects.exists())

    def test_url_helper(self):
        self.assertIsNone(access.photo_url(NeighborProfile(photo_version=None)))
