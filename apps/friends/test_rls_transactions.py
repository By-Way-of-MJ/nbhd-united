"""Real transaction boundaries: no TestCase outer atomic hiding regressions."""

from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

from django.db import DatabaseError, connection, transaction
from django.test import RequestFactory, TransactionTestCase, override_settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.common.query_view import BaseQueryView
from apps.friends import access
from apps.friends.project_views import ProjectView
from apps.friends.views import FriendsView, ProfilePhotoView
from apps.integrations import runtime_views
from apps.lessons.views import LessonViewSet
from apps.tenants.middleware import reset_rls_context, set_rls_context


def guc(name):
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [name])
        return cursor.fetchone()[0] or ""


class RLSTransactionTests(TransactionTestCase):
    def setUp(self):
        reset_rls_context(force=True)
        self.addCleanup(lambda: reset_rls_context(force=True))
        self.factory = RequestFactory()

    def test_request_transaction_starts_before_authentication(self):
        owner = str(uuid4())
        seen = []

        class Authentication(BaseAuthentication):
            def authenticate(self, request):
                seen.append(connection.in_atomic_block)
                set_rls_context(tenant_id=owner)
                return None

        def handler(view, request, *args, **kwargs):
            seen.append(connection.in_atomic_block)
            self.assertEqual(guc("app.tenant_id"), owner)
            return Response({})

        for base in (FriendsView, ProjectView, BaseQueryView):
            with self.subTest(base=base.__name__):
                probe = type(
                    "Probe",
                    (base,),
                    {"authentication_classes": [Authentication], "permission_classes": [], "get": handler},
                )
                response = probe.as_view()(self.factory.get("/probe/"))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(connection.in_atomic_block)
                self.assertEqual(guc("app.tenant_id"), "")
        self.assertEqual(seen, [True] * 6)

    def test_runtime_auth_sets_context_inside_request_transaction(self):
        owner = uuid4()
        views = (
            (runtime_views.RuntimeProposeShareView, "post", {"lesson_id": 1}),
            (runtime_views.RuntimeNeighborhoodContextView, "get", {}),
            (runtime_views.RuntimeMissionsView, "get", {}),
            (runtime_views.RuntimeProposeMissionTaskView, "post", {"mission_id": uuid4()}),
            (runtime_views.RuntimeProjectsContextView, "get", {}),
            (runtime_views.RuntimeProjectDraftView, "post", {}),
            (runtime_views.RuntimeProjectProposeView, "post", {"mission_id": uuid4()}),
        )
        seen = []

        def set_context(**kwargs):
            seen.append(connection.in_atomic_block)
            set_rls_context(**kwargs)
            self.assertEqual(guc("app.service_role"), "true")

        def load_tenant(*args):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(guc("app.tenant_id"), str(owner))
            return None, Response({"error": "tenant_not_found"}, status=404)

        with (
            mock.patch.object(runtime_views, "validate_internal_runtime_request"),
            mock.patch("apps.tenants.middleware.set_rls_context", side_effect=set_context),
            mock.patch.object(runtime_views, "_load_tenant_or_404", side_effect=load_tenant),
        ):
            for view, method, kwargs in views:
                with self.subTest(view=view.__name__):
                    response = view.as_view()(getattr(self.factory, method)("/probe/"), tenant_id=owner, **kwargs)
                    self.assertEqual(response.status_code, 404)
                    self.assertFalse(connection.in_atomic_block)
                    self.assertEqual(guc("app.service_role"), "")
        self.assertEqual(seen, [True] * len(views))

    def test_lesson_sharing_and_delete_auth_are_atomic_but_refresh_is_not(self):
        for action in ("share", "revoke_share", "destroy", "refresh", "approve"):
            expected = action in {"share", "revoke_share", "destroy"}

            class Authentication(BaseAuthentication):
                def authenticate(inner, request, expected=expected):
                    self.assertEqual(connection.in_atomic_block, expected)
                    return None

            def handler(view, request, expected=expected, **kwargs):
                self.assertEqual(connection.in_atomic_block, expected)
                return Response({})

            probe = type(
                "Probe",
                (LessonViewSet,),
                {"authentication_classes": [Authentication], "permission_classes": [], action: handler},
            )
            with self.subTest(action=action):
                response = probe.as_view({"post": action})(self.factory.post("/probe/"))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(connection.in_atomic_block)

    def test_photo_moderation_request_does_not_pin_a_transaction(self):
        def post(view, request):
            self.assertFalse(connection.in_atomic_block)
            return Response({})

        probe = type(
            "Probe", (ProfilePhotoView,), {"authentication_classes": [], "permission_classes": [], "post": post}
        )
        self.assertEqual(probe.as_view()(self.factory.post("/probe/")).status_code, 200)

    def test_drf_exception_discards_commit_callbacks(self):
        called = []

        class Probe(FriendsView):
            authentication_classes = []
            permission_classes = []

            def post(self, request):
                transaction.on_commit(lambda: called.append(True))
                raise ValidationError("refused")

        self.assertEqual(Probe.as_view()(self.factory.post("/probe/")).status_code, 400)
        self.assertEqual(called, [])

    def test_service_context_is_local_and_nested_and_preserves_tenant(self):
        owner = str(uuid4())
        statements = []

        def record(execute, sql, params, many, context):
            statements.append(sql)
            return execute(sql, params, many, context)

        with connection.execute_wrapper(record), transaction.atomic():
            set_rls_context(tenant_id=owner)
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('app.service_role', 'false', true)")
            with access.backstop_service_context():
                self.assertTrue(connection.in_atomic_block)
                self.assertEqual(guc("app.service_role"), "true")
                with access.backstop_service_context():
                    self.assertEqual(guc("app.service_role"), "true")
                self.assertEqual(guc("app.service_role"), "true")
            self.assertEqual(guc("app.service_role"), "false")
            self.assertEqual(guc("app.tenant_id"), owner)
        self.assertEqual(guc("app.service_role"), "")
        self.assertEqual(guc("app.tenant_id"), "")
        settings_sql = [sql for sql in statements if "set_config('app.service_role'" in sql]
        self.assertTrue(settings_sql)
        self.assertTrue(all(sql.endswith(", true)") for sql in settings_sql))

    def test_service_context_opens_transaction_and_does_not_leak_at_commit(self):
        seen = []
        self.assertFalse(connection.in_atomic_block)
        with access.backstop_service_context():
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(guc("app.service_role"), "true")
            transaction.on_commit(lambda: seen.append((connection.in_atomic_block, guc("app.service_role"))))
        self.assertEqual(seen, [(False, "")])

    def test_database_error_rolls_back_savepoint_and_restores_outer_context(self):
        with transaction.atomic():
            with self.assertRaises(DatabaseError), access.backstop_service_context(), connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")
            self.assertEqual(guc("app.service_role"), "")
            with access.backstop_service_context():
                with self.assertRaises(ValueError), access.backstop_service_context():
                    raise ValueError("failed inner work")
                self.assertEqual(guc("app.service_role"), "true")
            self.assertEqual(guc("app.service_role"), "")

    @override_settings(FRIENDS_DB_BACKSTOP=False)
    def test_disabled_service_backstop_remains_noop(self):
        with access.backstop_service_context():
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(guc("app.service_role"), "")

    def test_scrub_inference_runs_outside_transaction(self):
        from apps.friends.tasks import scrub_shared_lesson_task

        row = SimpleNamespace(
            source_lesson=SimpleNamespace(text="text", context=""),
            owner_tenant=SimpleNamespace(),
            scrub_status="pending",
            content_hash="",
        )

        def read(*args):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(guc("app.service_role"), "true")
            return row

        def inference():
            self.assertFalse(connection.in_atomic_block)
            raise RuntimeError("unavailable")

        with (
            mock.patch.object(access, "get_shared_lesson", side_effect=read),
            mock.patch("apps.friends.scrub._assert_ner_available", side_effect=inference),
            mock.patch.object(access, "save_scrub_failed"),
        ):
            self.assertEqual(scrub_shared_lesson_task(str(uuid4()))["reason"], "ner_unavailable")

    def test_scrub_publication_waits_for_commit(self):
        from apps.friends.services import _enqueue_scrub

        seen = []
        with mock.patch(
            "apps.cron.publish.publish_task", side_effect=lambda *a, **kw: seen.append(connection.in_atomic_block)
        ):
            with transaction.atomic():
                _enqueue_scrub(SimpleNamespace(id=uuid4()), "hash")
                self.assertEqual(seen, [])
            self.assertEqual(seen, [False])

    def test_real_forced_rls_read_and_restore_as_app_user(self):
        from django.utils import timezone

        from apps.tenants.models import Tenant, User
        from apps.transcripts.models import TranscriptEvent

        user = User.objects.create_user(username="pooling-owner")
        owner = Tenant.objects.create(user=user)
        event = TranscriptEvent.objects.create(
            tenant=owner,
            turn_id=uuid4(),
            role="user",
            source_type="ios_queued",
            source_event_id="pooling-probe",
            channel="ios",
            occurred_at=timezone.now(),
            text_enc=b"",
            content_hash="0" * 64,
        )
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("GRANT USAGE ON SCHEMA public TO app_user")
                cursor.execute("GRANT SELECT ON transcripts_transcriptevent TO app_user")
                cursor.execute("SET LOCAL ROLE app_user")
            self.assertEqual(list(TranscriptEvent.objects.values_list("id", flat=True)), [])
            with access.backstop_service_context():
                self.assertEqual(list(TranscriptEvent.objects.values_list("id", flat=True)), [event.id])
            self.assertEqual(list(TranscriptEvent.objects.values_list("id", flat=True)), [])

            class Authentication(BaseAuthentication):
                def authenticate(inner, request):
                    self.assertTrue(connection.in_atomic_block)
                    set_rls_context(tenant_id=owner.id)
                    return None

            class Probe(FriendsView):
                authentication_classes = [Authentication]
                permission_classes = []

                def get(self, request):
                    return Response(list(TranscriptEvent.objects.values_list("id", flat=True)))

            response = Probe.as_view()(self.factory.get("/probe/"))
            self.assertEqual(response.data, [event.id])
            # Keep test grants and SET ROLE from leaking into other tests.
            transaction.set_rollback(True)

    def test_project_nudge_reads_and_claims_are_atomic_but_delivery_is_not(self):
        from datetime import UTC, datetime, timedelta

        from apps.friends import project_notifications as pushes

        now = datetime(2026, 10, 8, 9, tzinfo=UTC)
        tenant = SimpleNamespace(id=uuid4())
        row = SimpleNamespace(
            id=uuid4(),
            membership=SimpleNamespace(tenant=tenant),
            step_id=uuid4(),
            step=SimpleNamespace(due_date=now.date() + timedelta(days=1), title="A step", shared_goal_id=uuid4()),
            due_nudged_for=None,
            still_yours_nudged_for=None,
            kept_at=None,
            responded_at=None,
        )

        def candidates(*args):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(guc("app.service_role"), "true")
            return [row]

        def claim(*args):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(guc("app.service_role"), "true")
            return True

        def deliver(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(guc("app.service_role"), "")
            return 1

        with (
            mock.patch("apps.friends.project_flags.projects_v2_enabled", return_value=True),
            mock.patch("apps.common.tenant_tz.tenant_tz", return_value=UTC),
            mock.patch.object(access, "due_nudge_candidates", side_effect=candidates),
            mock.patch.object(access, "still_yours_candidates", side_effect=candidates),
            mock.patch.object(access, "claim_due_nudge", side_effect=claim),
            mock.patch.object(access, "claim_still_yours_nudge", side_effect=claim),
            mock.patch.object(pushes, "_deliver", side_effect=deliver) as delivery,
        ):
            self.assertEqual(pushes.run_due_nudges(now), {"claimed": 1, "sent": 1})
            row.step.due_date = now.date() - timedelta(days=4)
            self.assertEqual(pushes.run_still_yours_nudges(now), {"claimed": 1, "sent": 1})
            self.assertEqual(delivery.call_count, 2)

    def test_grant_receiver_database_error_does_not_poison_callers_transaction(self):
        from apps.friends.envelope import _refresh_recipient_on_grant

        class Grant:
            @property
            def shared_lesson(self):
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1 / 0")

        with transaction.atomic():
            _refresh_recipient_on_grant(None, Grant())
            self.assertEqual(guc("app.service_role"), "")

    @override_settings(NBHD_DISABLE_BACKGROUND_THREADS=False)
    def test_project_push_thread_has_its_own_short_service_transaction(self):
        import threading

        from apps.friends import project_notifications as pushes

        threads, seen = [], []
        real_thread = threading.Thread
        assignments = mock.Mock()

        def owner_ids(*args, **kwargs):
            seen.append(("read", connection.in_atomic_block, guc("app.service_role")))
            return []

        assignments.filter.return_value.values_list.side_effect = owner_ids

        def deliver(*args, **kwargs):
            seen.append(("push", connection.in_atomic_block, guc("app.service_role")))
            return 0

        def start_thread(*, target, daemon):
            def run():
                try:
                    seen.append(("start", connection.in_atomic_block, guc("app.tenant_id")))
                    target()
                finally:
                    connection.close()

            thread = real_thread(target=run, daemon=daemon)
            threads.append(thread)
            return thread

        with (
            mock.patch.object(pushes.threading, "Thread", side_effect=start_thread),
            mock.patch.object(access, "project_assignments", return_value=assignments),
            mock.patch.object(pushes, "_members", return_value=[]),
            mock.patch.object(pushes, "_name", return_value="A neighbor"),
            mock.patch.object(pushes, "_deliver", side_effect=deliver),
        ):
            with transaction.atomic():
                set_rls_context(tenant_id=uuid4())
                pushes.notify_step_confirmed(
                    SimpleNamespace(id=uuid4()),
                    SimpleNamespace(id=uuid4(), title="A step"),
                    SimpleNamespace(id=uuid4()),
                )
                self.assertEqual(threads, [])
            self.assertEqual(len(threads), 1)
            threads[0].join(timeout=10)
            self.assertFalse(threads[0].is_alive())
        self.assertEqual(seen, [("start", False, ""), ("read", True, "true"), ("push", False, "")])

    @override_settings(FRIENDS_DB_BACKSTOP=False)
    def test_scrub_terminal_write_keeps_its_transaction_when_backstop_flag_is_off(self):
        from apps.friends.models import SharedLesson

        def update(**kwargs):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(guc("app.service_role"), "true")
            return 1

        rows = mock.Mock()
        rows.update.side_effect = update
        with mock.patch.object(SharedLesson.objects, "filter", return_value=rows):
            access._update_scrub_terminal(uuid4(), scrub_status="failed")
        self.assertEqual(guc("app.service_role"), "")
