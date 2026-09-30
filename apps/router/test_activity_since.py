"""The "since you were last here" feed: write events, the owner endpoint, counters, retention."""

from __future__ import annotations

import secrets
import uuid
from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.datebook.models import DeviceCommand
from apps.fuel.models import Workout, WorkoutPlan
from apps.journal.models import Document, Goal
from apps.platform_logs.models import ToolContractEvent
from apps.router.activity_views import since_items
from apps.router.document_write_guard import record_runtime_write_activity, record_runtime_write_event
from apps.router.models import RuntimeWriteActivity, RuntimeWriteEvent
from apps.router.tasks import purge_runtime_write_events_task
from apps.tenants.models import Tenant, User
from apps.tenants.test_utils import seed_internal_key

URL = "/api/v1/activity/since/"
EVENTS_URL = "/api/v1/activity/since/events"


def _tenant() -> Tenant:
    user = User.objects.create_user(
        username=f"since_{secrets.token_hex(4)}",
        email=f"{secrets.token_hex(4)}@example.com",
    )
    return Tenant.objects.create(user=user, status=Tenant.Status.ACTIVE, container_fqdn="oc-since.example.com")


def _doc(tenant, title="Tuesday note", slug="tuesday", kind="project") -> Document:
    return Document.objects.create(tenant=tenant, kind=kind, slug=slug, title=title, markdown="x")


def _command(tenant, *, state=DeviceCommand.State.EXECUTED, item_count=1) -> DeviceCommand:
    now = timezone.now()
    executed = {}
    if state == DeviceCommand.State.EXECUTED:
        executed = {
            "started_at": now,
            "resolved_at": now,
            "journaled_at": now,
            "execution_status": DeviceCommand.ExecutionStatus.SUCCEEDED,
            "result_id": secrets.token_hex(6),
        }
    return DeviceCommand.objects.create(
        **executed,
        tenant=tenant,
        request_id=secrets.token_hex(6),
        request_digest="0" * 64,
        command_type=DeviceCommand.CommandType.CALENDAR_CREATE,
        state=state,
        item_count=item_count,
        target_installation_id="install",
        target_gateway_epoch=1,
        expires_at=timezone.now() + timedelta(days=1),
    )


def _event(tenant, kind, ref, verb="updated", ago=timedelta(minutes=5)) -> RuntimeWriteEvent:
    event = RuntimeWriteEvent.objects.create(tenant=tenant, kind=kind, ref=ref, verb=verb)
    RuntimeWriteEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - ago)
    return event


def _since(hours=24):
    return (timezone.now() - timedelta(hours=hours)).isoformat()


class ChokepointTests(TestCase):
    def setUp(self):
        self.tenant = _tenant()

    def test_without_kind_only_upserts_the_timestamp(self):
        record_runtime_write_activity(self.tenant)
        self.assertTrue(RuntimeWriteActivity.objects.filter(tenant=self.tenant).exists())
        self.assertFalse(RuntimeWriteEvent.objects.exists())

    def test_with_kind_upserts_and_records_ids_only(self):
        doc_id = uuid.uuid4()
        record_runtime_write_activity(
            self.tenant,
            kind="journal_doc",
            ref={"document_id": doc_id, "title": "Secret plans", "markdown": "body"},
            verb="created",
        )
        self.assertTrue(RuntimeWriteActivity.objects.filter(tenant=self.tenant).exists())
        event = RuntimeWriteEvent.objects.get()
        self.assertEqual((event.kind, event.verb), ("journal_doc", "created"))
        self.assertEqual(event.ref, {"document_id": str(doc_id)})

    def test_defaults_to_updated(self):
        record_runtime_write_event(self.tenant, kind="fuel", ref={"workout_id": uuid.uuid4()})
        self.assertEqual(RuntimeWriteEvent.objects.get().verb, "updated")

    def test_bad_input_records_nothing_and_never_raises(self):
        record_runtime_write_event(self.tenant, kind="finance", ref={"id": "1"})
        record_runtime_write_event(self.tenant, kind="fuel", ref={"title": "no ids here"})
        record_runtime_write_event(self.tenant, kind="fuel", ref={"workout_id": "x"}, verb="deleted")
        record_runtime_write_event(self.tenant, kind="fuel", ref={"workout_id": "y" * 200})
        self.assertFalse(RuntimeWriteEvent.objects.exists())

    def test_insert_failure_is_swallowed(self):
        with patch.object(RuntimeWriteEvent.objects, "create", side_effect=RuntimeError("db down")):
            record_runtime_write_activity(self.tenant, kind="fuel", ref={"workout_id": uuid.uuid4()})
        self.assertTrue(RuntimeWriteActivity.objects.filter(tenant=self.tenant).exists())


@override_settings(NBHD_INTERNAL_API_KEY="test-internal-key")
class RuntimeWriteWiringTests(TestCase):
    """The runtime write views record what they wrote; a broken insert never breaks them."""

    def setUp(self):
        self.tenant = _tenant()
        seed_internal_key(self.tenant)
        self.client = APIClient()
        self.headers = {"HTTP_X_NBHD_INTERNAL_KEY": "test-internal-key", "HTTP_X_NBHD_TENANT_ID": str(self.tenant.id)}

    def _put_doc(self, **body):
        return self.client.put(
            f"/api/v1/integrations/runtime/{self.tenant.id}/document/", body, format="json", **self.headers
        )

    def test_document_put_records_created_then_updated(self):
        resp = self._put_doc(kind="project", slug="garden", title="Garden", markdown="one")
        self.assertEqual(resp.status_code, 201)
        resp = self._put_doc(kind="project", slug="garden", markdown="two")
        self.assertEqual(resp.status_code, 200)
        doc = Document.objects.get(tenant=self.tenant, slug="garden")
        events = list(RuntimeWriteEvent.objects.order_by("id").values_list("kind", "verb", "ref"))
        self.assertEqual(
            events,
            [
                ("journal_doc", "created", {"document_id": str(doc.id)}),
                ("journal_doc", "updated", {"document_id": str(doc.id)}),
            ],
        )

    def test_memory_document_is_not_recorded(self):
        resp = self._put_doc(kind="memory", slug="memory", markdown="facts")
        self.assertIn(resp.status_code, (200, 201))
        self.assertFalse(RuntimeWriteEvent.objects.exists())

    def test_document_append_and_daily_note_record(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/document/append/",
            {"kind": "project", "slug": "reno", "content": "added a line"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/daily-note/append/",
            {"content": "went for a walk"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        kinds = sorted(Document.objects.get(id=e.ref["document_id"]).kind for e in RuntimeWriteEvent.objects.all())
        self.assertEqual(kinds, ["daily", "project"])

    def test_goal_create_records_horizons_goal(self):
        resp = self.client.post(
            f"/api/v1/integrations/runtime/{self.tenant.id}/goals/",
            {"title": "Run a half marathon"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201)
        event = RuntimeWriteEvent.objects.get()
        self.assertEqual((event.kind, event.verb), ("horizons_goal", "created"))
        self.assertTrue(Goal.objects.filter(id=event.ref["goal_id"], tenant=self.tenant).exists())

    def test_fuel_log_and_skip_record(self):
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/log/",
            {"category": "cardio", "activity": "Run Day", "date": "2026-09-29"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        workout_id = resp.data["id"]
        resp = self.client.post(
            f"/api/v1/fuel/runtime/{self.tenant.id}/workouts/{workout_id}/skip/",
            {"reason": "rain"},
            format="json",
            **self.headers,
        )
        self.assertEqual(resp.status_code, 200)
        events = list(RuntimeWriteEvent.objects.order_by("id").values_list("kind", "verb", "ref"))
        self.assertEqual(
            events,
            [("fuel", "created", {"workout_id": workout_id}), ("fuel", "updated", {"workout_id": workout_id})],
        )

    def test_broken_event_insert_never_breaks_the_write(self):
        with patch.object(RuntimeWriteEvent.objects, "create", side_effect=RuntimeError("db down")):
            resp = self._put_doc(kind="project", slug="safe", title="Safe", markdown="kept")
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(Document.objects.filter(tenant=self.tenant, slug="safe").exists())
        self.assertFalse(RuntimeWriteEvent.objects.exists())


class ActivitySinceEndpointTests(TestCase):
    def setUp(self):
        self.tenant = _tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def _get(self, since=None):
        return self.client.get(URL, {"since": since or _since()})

    def test_requires_auth(self):
        self.assertIn(APIClient().get(URL, {"since": _since()}).status_code, (401, 403))

    def test_since_is_required_and_parsed(self):
        self.assertEqual(self.client.get(URL).status_code, 400)
        self.assertEqual(self.client.get(URL, {"since": "yesterday"}).status_code, 400)

    def test_empty(self):
        resp = self._get()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {"items": []})

    def test_another_tenants_events_never_leak(self):
        other = _tenant()
        other_doc = _doc(other, title="Their diary")
        _event(other, "journal_doc", {"document_id": str(other_doc.id)})
        # Even an event row pointing at the other tenant's document resolves to nothing.
        _event(self.tenant, "journal_doc", {"document_id": str(other_doc.id)})
        self.assertEqual(self._get().data["items"], [])

    def test_item_shape_and_title_resolution(self):
        self.tenant.pii_entity_map = {"[PERSON_1]": {"name": "Nana"}}
        self.tenant.save(update_fields=["pii_entity_map"])
        doc = _doc(self.tenant, title="Visit [PERSON_1]", slug="visit", kind="project")
        _event(self.tenant, "journal_doc", {"document_id": str(doc.id)}, verb="created")
        [item] = self._get().data["items"]
        self.assertEqual(item["kind"], "journal_doc")
        self.assertEqual(item["verb"], "created")
        self.assertEqual(item["count"], 1)
        self.assertEqual(item["title"], "Visit Nana")
        self.assertEqual(item["ref"], {"document_id": str(doc.id), "doc_kind": "project", "slug": "visit"})
        self.assertIn("at", item)

    def test_title_reflects_the_live_row(self):
        doc = _doc(self.tenant, title="Old name")
        _event(self.tenant, "journal_doc", {"document_id": str(doc.id)})
        Document.objects.filter(id=doc.id).update(title="New name")
        self.assertEqual(self._get().data["items"][0]["title"], "New name")

    def test_dedupes_by_item_keeping_the_latest_verb(self):
        doc = _doc(self.tenant)
        _event(self.tenant, "journal_doc", {"document_id": str(doc.id)}, verb="created", ago=timedelta(hours=2))
        _event(self.tenant, "journal_doc", {"document_id": str(doc.id)}, verb="updated", ago=timedelta(hours=1))
        items = self._get().data["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["verb"], "updated")

    def test_calendar_events_group_into_one_item(self):
        a = _command(self.tenant, item_count=1)
        b = _command(self.tenant, item_count=2)
        pending = _command(self.tenant, state=DeviceCommand.State.PENDING)
        workout = Workout.objects.create(tenant=self.tenant, date=date(2026, 9, 30), activity="Run Day")
        _event(self.tenant, "calendar", {"command_id": str(a.id)}, verb="created", ago=timedelta(minutes=30))
        _event(self.tenant, "fuel", {"workout_id": str(workout.id)}, ago=timedelta(minutes=20))
        _event(self.tenant, "calendar", {"command_id": str(b.id)}, verb="created", ago=timedelta(minutes=10))
        _event(self.tenant, "calendar", {"command_id": str(pending.id)}, verb="created", ago=timedelta(minutes=5))
        items = self._get().data["items"]
        self.assertEqual([i["kind"] for i in items], ["calendar", "fuel"])
        self.assertEqual(items[0]["count"], 3)
        self.assertEqual(items[0]["title"], "3 events added")
        self.assertEqual(items[0]["ref"], {})
        self.assertEqual(items[1]["title"], "Run Day")
        self.assertEqual(items[1]["ref"], {"workout_id": str(workout.id), "date": "2026-09-30"})

    def test_deleted_resources_are_dropped(self):
        doc = _doc(self.tenant)
        goal = Goal.objects.create(tenant=self.tenant, title="Half marathon")
        plan = WorkoutPlan.objects.create(
            tenant=self.tenant, name="Base block", start_date=date(2026, 9, 1), weeks=4, days_per_week=3
        )
        _event(self.tenant, "journal_doc", {"document_id": str(doc.id)})
        _event(self.tenant, "horizons_goal", {"goal_id": str(goal.id)})
        _event(self.tenant, "fuel", {"plan_id": str(plan.id)})
        _event(self.tenant, "fuel", {"workout_id": str(uuid.uuid4())})
        _event(self.tenant, "calendar", {"command_id": "None"})
        doc.delete()
        titles = sorted(i["title"] for i in self._get().data["items"])
        self.assertEqual(titles, ["Base block", "Half marathon"])

    def test_newest_first_capped_at_twelve(self):
        for n in range(15):
            doc = _doc(self.tenant, title=f"Doc {n}", slug=f"doc-{n}")
            _event(self.tenant, "journal_doc", {"document_id": str(doc.id)}, ago=timedelta(minutes=100 - n))
        items = self._get().data["items"]
        self.assertEqual(len(items), 12)
        self.assertEqual(items[0]["title"], "Doc 14")
        self.assertEqual(items[-1]["title"], "Doc 3")

    def test_since_filters_older_events(self):
        old = _doc(self.tenant, title="Old", slug="old")
        new = _doc(self.tenant, title="New", slug="new")
        _event(self.tenant, "journal_doc", {"document_id": str(old.id)}, ago=timedelta(hours=5))
        _event(self.tenant, "journal_doc", {"document_id": str(new.id)}, ago=timedelta(hours=1))
        self.assertEqual([i["title"] for i in self._get(_since(hours=2)).data["items"]], ["New"])

    def test_query_budget(self):
        doc = _doc(self.tenant)
        goal = Goal.objects.create(tenant=self.tenant, title="Goal")
        workout = Workout.objects.create(tenant=self.tenant, date=date(2026, 9, 30), activity="Run")
        command = _command(self.tenant)
        _event(self.tenant, "journal_doc", {"document_id": str(doc.id)})
        _event(self.tenant, "horizons_goal", {"goal_id": str(goal.id)})
        _event(self.tenant, "fuel", {"workout_id": str(workout.id)})
        _event(self.tenant, "calendar", {"command_id": str(command.id)})
        since = timezone.now() - timedelta(days=1)
        # One scan of the events, then one title lookup per kind present.
        with self.assertNumQueries(5):
            items = since_items(self.tenant, since)
        self.assertEqual(len(items), 4)


class ActivitySinceCounterTests(TestCase):
    def setUp(self):
        self.tenant = _tenant()
        self.client = APIClient()
        self.client.force_authenticate(user=self.tenant.user)

    def test_counts_are_content_free(self):
        resp = self.client.post(
            EVENTS_URL,
            {"event": "tap", "kind": "fuel", "title": "Run Day", "document_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual(resp.status_code, 204)
        row = ToolContractEvent.objects.get()
        self.assertEqual((row.namespace, row.tool_name, row.reason_code), ("activity_since", "activity-since", "tap"))
        self.assertEqual(row.detail, {"kind": "fuel"})
        self.assertIsNone(row.tenant_id)

    def test_shown_without_kind(self):
        self.assertEqual(self.client.post(EVENTS_URL, {"event": "shown"}, format="json").status_code, 204)
        self.assertEqual(ToolContractEvent.objects.get().detail, {})

    def test_rejects_unknown_event_or_kind(self):
        self.assertEqual(self.client.post(EVENTS_URL, {"event": "Run Day"}, format="json").status_code, 400)
        self.assertEqual(
            self.client.post(EVENTS_URL, {"event": "tap", "kind": "Visit Nana"}, format="json").status_code, 400
        )
        self.assertFalse(ToolContractEvent.objects.exists())

    def test_requires_auth(self):
        self.assertIn(APIClient().post(EVENTS_URL, {"event": "tap"}, format="json").status_code, (401, 403))


class RetentionTests(TestCase):
    def test_purge_keeps_thirty_days(self):
        tenant = _tenant()
        keep = _event(tenant, "fuel", {"workout_id": "a"}, ago=timedelta(days=29))
        _event(tenant, "fuel", {"workout_id": "b"}, ago=timedelta(days=31))
        self.assertEqual(purge_runtime_write_events_task(batch_size=1), {"deleted": 1})
        self.assertEqual(list(RuntimeWriteEvent.objects.values_list("id", flat=True)), [keep.id])
