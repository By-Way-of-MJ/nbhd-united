"""GET /api/v1/tenants/profile/ reports the tenant's status.

The iOS app polls the profile after a new sign-up and leaves "Setting up your
space…" only when status == "active". The field was missing, so every new
account waited out the app's timeout (App Review rejected iOS 2.2.3).
"""

from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Tenant, User
from .services import create_tenant


class ProfileStatusTests(TestCase):
    def _client(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}")
        return client

    def test_status_follows_provisioning_to_active(self):
        tenant = create_tenant(display_name="New Space", telegram_chat_id=920_000_001)
        tenant.status = Tenant.Status.PROVISIONING
        tenant.save(update_fields=["status"])
        client = self._client(tenant.user)

        self.assertEqual(client.get("/api/v1/tenants/profile/").data["status"], "provisioning")

        tenant.status = Tenant.Status.ACTIVE
        tenant.save(update_fields=["status"])
        response = client.get("/api/v1/tenants/profile/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "active")
        self.assertEqual(str(response.data["id"]), str(tenant.user.id))

    def test_user_without_tenant_gets_empty_status(self):
        user = User.objects.create_user(username="no-tenant", email="nt@example.test", password="x-pass-123")
        response = self._client(user).get("/api/v1/tenants/profile/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "")
