"""A2UI surfaces reach the iOS app only for tenants on CHAT_SURFACES_TENANT_IDS.

Detail (``_serialize_message``) always carries ``a2ui`` (``[]`` when empty or
gated off); the ``?since=`` feed omits it when empty, exactly like ``panels``.
A surface-only assistant row counts as worth showing only when gated on.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.router.chat_history import build_since_page
from apps.router.chat_views import _serialize_message
from apps.router.models import AppChatMessage, ProactiveOutbound
from apps.router.surfaces.validate import validate_surface

SENTINEL = "private-sentinel"


def surface(*, surface_id="tonight", text="You slept well"):
    return {
        "version": "1.0",
        "createSurface": {
            "surfaceId": surface_id,
            "catalogId": "nbhd/v1",
            "components": [
                {"id": "root", "component": "Column", "children": ["title"]},
                {"id": "title", "component": "Text", "text": text},
            ],
            "dataModel": {"sleep": {"summary": "You slept 5h 6m."}},
        },
    }


INVALID = {"version": "1.0", "createSurface": {"surfaceId": "Bad Id", "catalogId": "nbhd/v1", "components": []}}


class ChatA2UIExposureTests(TestCase):
    def setUp(self):
        from apps.router.models import ChatThread
        from apps.tenants.models import Tenant, User
        from apps.tenants.test_utils import seed_internal_key

        self.user = User.objects.create_user(username="a2ui", telegram_chat_id=98765)
        self.tenant = Tenant.objects.create(user=self.user, status="active")
        self.thread = ChatThread.objects.create(tenant=self.tenant, user=self.user)
        seed_internal_key(self.tenant)
        self.surface = surface()
        self.expected = validate_surface(self.surface)

    def _gate_on(self):
        return override_settings(CHAT_SURFACES_TENANT_IDS=str(self.tenant.id))

    def _gate_off(self):
        return override_settings(CHAT_SURFACES_TENANT_IDS="")

    def _app_message(self, *, reply_text="Hello", a2ui=None):
        return AppChatMessage.objects.create(
            tenant=self.tenant,
            user=self.user,
            thread=self.thread,
            client_msg_id=f"a2ui-{timezone.now().timestamp()}",
            user_text="Hi",
            status=AppChatMessage.Status.READY,
            reply_text=reply_text,
            replied_at=timezone.now(),
            a2ui=a2ui,
        )

    def _feed(self):
        messages, _cursor = build_since_page(self.tenant, str(self.thread.id), cursor=None, limit=50)
        return messages

    def _assistant_rows(self, messages):
        return [row for row in messages if row["role"] == "assistant"]

    def test_gated_on_detail_and_feed_carry_surfaces(self):
        row = self._app_message(a2ui=[self.surface])
        with self._gate_on():
            detail = _serialize_message(row)
            messages = self._feed()
        self.assertEqual(detail["a2ui"], [self.expected])
        assistant = self._assistant_rows(messages)
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["a2ui"], [self.expected])

    def test_gated_off_detail_is_empty_list_and_feed_omits_key(self):
        row = self._app_message(a2ui=[self.surface])
        with self._gate_off():
            detail = _serialize_message(row)
            messages = self._feed()
        self.assertEqual(detail["a2ui"], [])
        assistant = self._assistant_rows(messages)
        self.assertEqual(len(assistant), 1)
        self.assertNotIn("a2ui", assistant[0])

    def test_detail_always_carries_a2ui_key_even_without_surfaces(self):
        row = self._app_message(a2ui=None)
        with self._gate_on():
            self.assertEqual(_serialize_message(row)["a2ui"], [])
        with self._gate_off():
            self.assertEqual(_serialize_message(row)["a2ui"], [])

    def test_surface_only_app_reply_appears_in_feed_only_when_gated_on(self):
        row = self._app_message(reply_text="", a2ui=[self.surface])
        with self._gate_off():
            self.assertEqual(self._assistant_rows(self._feed()), [])
        with self._gate_on():
            assistant = self._assistant_rows(self._feed())
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["id"], f"app:{row.id}:1")
        self.assertEqual(assistant[0]["a2ui"], [self.expected])

    def test_surface_only_proactive_send_appears_in_feed_only_when_gated_on(self):
        send = ProactiveOutbound.objects.create(
            tenant=self.tenant,
            channel=ProactiveOutbound.Channel.APP,
            channel_user_id=str(self.user.id),
            message_text="",
            a2ui=[self.surface],
        )
        with self._gate_off():
            self.assertEqual(self._assistant_rows(self._feed()), [])
        with self._gate_on():
            assistant = self._assistant_rows(self._feed())
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["id"], f"cron:{send.id}")
        self.assertEqual(assistant[0]["a2ui"], [self.expected])

    def test_feed_resolves_live_task_ref_for_gated_tenant(self):
        from apps.common.tenant_tz import tenant_today
        from apps.journal.models import Task

        today = tenant_today(self.tenant)
        task = Task.objects.create(tenant=self.tenant, title="Water the plants", due_date=today, status="open")
        payload = surface()
        payload["createSurface"]["dataModel"] = {"tasks": {"$ref": "tasks:due_today"}}
        self._app_message(a2ui=[payload])
        with self._gate_on():
            assistant = self._assistant_rows(self._feed())
        self.assertEqual(len(assistant), 1)
        self.assertEqual(
            assistant[0]["a2ui"][0]["createSurface"]["dataModel"]["tasks"],
            [
                {
                    "id": str(task.id),
                    "title": "Water the plants",
                    "due_date": today.isoformat(),
                    "status": "open",
                    "done": False,
                }
            ],
        )

    def test_invalid_stored_surface_is_dropped_from_output(self):
        row = self._app_message(reply_text=SENTINEL, a2ui=[self.surface, INVALID])
        with self._gate_on(), self.assertLogs("apps.router.surfaces.store", level="INFO") as logs:
            detail = _serialize_message(row)
            self.assertEqual(detail["a2ui"], [self.expected])
            messages = self._feed()
        assistant = self._assistant_rows(messages)
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["a2ui"], [self.expected])
        self.assertTrue(any("a2ui_surface_dropped code=bad_envelope" in line for line in logs.output))
        self.assertFalse(any(SENTINEL in line for line in logs.output))
