"""Regression coverage for pooled privileges, hidden cascades, and absorb phases."""

from contextlib import contextmanager
from unittest import mock
from uuid import uuid4

from django.contrib import admin
from django.db import connection, transaction
from django.test import RequestFactory, TransactionTestCase
from django.utils import timezone
from rest_framework.response import Response

from apps.friends import access, circles, services
from apps.friends.models import AbsorbedItem, FriendThreadMembership, LessonShareGrant, SharedLesson
from apps.friends.test_pr4 import _edge, _lesson, _profile, _tenant
from apps.friends.test_rls_transactions import guc
from apps.friends.views import FriendsView
from apps.integrations.runtime_views import RuntimeNeighborhoodContextView
from apps.lessons.admin import LessonAdmin
from apps.lessons.models import Lesson
from apps.tenants.admin import TenantAdmin, UserAdmin
from apps.tenants.authentication import JWTAuthenticationWithRLS, PersonalAccessTokenAuthentication
from apps.tenants.middleware import reset_rls_context, set_rls_context
from apps.tenants.models import Tenant, User
from apps.tenants.pat_models import PersonalAccessToken, generate_pat
from apps.transcripts.models import TranscriptEvent


class ReviewRegressionTests(TransactionTestCase):
    def setUp(self):
        reset_rls_context(force=True)
        self.addCleanup(lambda: reset_rls_context(force=True))
        self.factory = RequestFactory()

    @contextmanager
    def app_role(self):
        """Transactional test-only grants/posture, all rolled back on exit."""
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                # Match disable_rls's production posture for collector joins.
                cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND rowsecurity")
                protected = {
                    "shared_lessons",
                    "lesson_share_grants",
                    "friend_messages",
                    "friend_sky_memberships",
                    "shared_goal_steps",
                    "shared_goal_milestones",
                    "shared_goal_step_assignments",
                    "shared_goal_step_dependencies",
                    "transcripts_transcriptevent",
                    "transcripts_transcriptcapturequarantine",
                    "transcripts_transcriptindexoutbox",
                }
                for (table,) in cursor.fetchall():
                    if table not in protected:
                        cursor.execute(f"ALTER TABLE {connection.ops.quote_name(table)} DISABLE ROW LEVEL SECURITY")
                cursor.execute("GRANT USAGE ON SCHEMA public TO app_user")
                cursor.execute("GRANT ALL ON ALL TABLES IN SCHEMA public TO app_user")
                cursor.execute("GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO app_user")
                cursor.execute("SET LOCAL ROLE app_user")
            try:
                yield
            finally:
                transaction.set_rollback(True)

    def test_dirty_session_tenant_auth_request_owns_transaction_and_visibility(self):
        """No enclosing atomic: old dispatch/session SET fails this request probe."""
        owner = _tenant("dirty-owner")
        stranger = _tenant("dirty-stranger")
        for tenant in (owner, stranger):
            TranscriptEvent.objects.create(
                tenant=tenant,
                turn_id=uuid4(),
                role="user",
                source_type="ios_queued",
                source_event_id=str(tenant.id),
                channel="ios",
                occurred_at=timezone.now(),
                text_enc=b"",
                content_hash="0" * 64,
            )
        raw, prefix, hashed = generate_pat()
        pat = PersonalAccessToken.objects.create(
            user=owner.user, name="probe", token_prefix=prefix, token_hash=hashed, last_used_at=timezone.now()
        )
        # An ephemeral member of app_user gets exactly the probe's SQL grants.
        # Session SET ROLE deliberately avoids a test-owned outer transaction.
        role = connection.ops.quote_name("rls_probe_" + uuid4().hex)
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE ROLE {role} INHERIT")
            cursor.execute(f"GRANT app_user TO {role}")
            cursor.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
            cursor.execute(f"GRANT SELECT ON transcripts_transcriptevent TO {role}")
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SET ROLE {role}")
                cursor.execute("SELECT set_config('app.service_role', 'true', false)")
            for auth in (JWTAuthenticationWithRLS, PersonalAccessTokenAuthentication):
                statements = []

                def record(execute, sql, params, many, context, statements=statements):
                    if "set_config(" in sql:
                        statements.append(sql)
                    return execute(sql, params, many, context)

                def get(view, request):
                    self.assertTrue(connection.in_atomic_block)
                    self.assertEqual(guc("app.tenant_id"), str(owner.id))
                    self.assertEqual(guc("app.user_id"), str(owner.user_id))
                    self.assertEqual(guc("app.service_role"), "")
                    return Response(list(TranscriptEvent.objects.values_list("tenant_id", flat=True)))

                probe = type(
                    "Probe",
                    (FriendsView,),
                    {
                        "authentication_classes": [auth],
                        "permission_classes": [],
                        "get": get,
                    },
                )
                with (
                    self.subTest(auth=auth.__name__),
                    mock.patch(
                        "rest_framework_simplejwt.authentication.JWTAuthentication.authenticate",
                        return_value=(owner.user, {}),
                    ),
                    mock.patch.object(PersonalAccessToken.objects, "select_related") as pats,
                    connection.execute_wrapper(record),
                ):
                    pats.return_value.get.return_value = pat
                    self.assertFalse(connection.in_atomic_block)
                    response = probe.as_view()(self.factory.get("/probe/", HTTP_AUTHORIZATION=f"Bearer {raw}"))
                    self.assertEqual(response.data, [owner.id])
                    self.assertFalse(connection.in_atomic_block)
                    self.assertEqual(guc("app.tenant_id"), "")
                    self.assertEqual(guc("app.user_id"), "")
                    self.assertEqual(guc("app.service_role"), "true")  # SET LOCAL masked, not mutated
                    self.assertEqual(len(statements), 1)
                    self.assertIn("set_config('app.service_role', '', true)", statements[0])
        finally:
            with connection.cursor() as cursor:
                cursor.execute("RESET ROLE")
                cursor.execute(f"DROP OWNED BY {role}")
                cursor.execute(f"DROP ROLE {role}")

    def test_tenant_context_inside_service_scope_preserves_privilege(self):
        with access.backstop_service_context():
            set_rls_context(tenant_id=uuid4())
            self.assertEqual(guc("app.service_role"), "true")

    def test_departure_revokes_hidden_owner_grants_and_removes_orphan(self):
        owner = _tenant("depart-owner")
        circle = circles.create_circle(owner, owner.user, name="Circle")
        snapshot, _ = access.ensure_shared_lesson(_lesson(owner), owner)
        grant = access.create_grant(snapshot, circle=circle, granted_by=owner.user)
        other = _tenant("depart-other")
        other_snapshot, _ = access.ensure_shared_lesson(_lesson(other), other)
        other_grant = access.create_grant(other_snapshot, circle=circle, granted_by=other.user)
        with self.app_role():
            set_rls_context(tenant_id=owner.id, user_id=owner.user_id, as_tenant=True)
            circles.leave_circle(owner, circle.id)
            with access.backstop_service_context():
                self.assertFalse(SharedLesson.objects.filter(pk=snapshot.pk).exists())
                self.assertFalse(LessonShareGrant.objects.filter(pk=grant.pk).exists())
                self.assertTrue(LessonShareGrant.objects.filter(pk=other_grant.pk, status="active").exists())

    def test_orphan_decision_sees_other_active_grant_hidden_from_owner(self):
        owner, a, b = (_tenant(name) for name in ("orphan-owner", "orphan-a", "orphan-b"))
        edge_a, edge_b = _edge(owner, a), _edge(owner, b)
        snapshot, _ = access.ensure_shared_lesson(_lesson(owner), owner)
        hidden = access.create_grant(snapshot, friendship=edge_a, granted_by=owner.user)
        visible = access.create_grant(snapshot, friendship=edge_b, granted_by=owner.user)
        edge_a.status = "revoked"
        edge_a.save(update_fields=["status"])
        with self.app_role():
            set_rls_context(tenant_id=owner.id, user_id=owner.user_id, as_tenant=True)
            self.assertFalse(LessonShareGrant.objects.filter(pk=hidden.pk).exists())
            access.revoke_grant(visible)
            self.assertTrue(SharedLesson.objects.filter(pk=snapshot.pk).exists())
            with access.backstop_service_context():
                access.revoke_grant(hidden)
                self.assertFalse(SharedLesson.objects.filter(pk=snapshot.pk).exists())

    def test_orphan_cascade_collects_invisible_revoked_grants(self):
        owner, a, b = (_tenant(name) for name in ("cascade-owner", "cascade-a", "cascade-b"))
        edge_a, edge_b = _edge(owner, a), _edge(owner, b)
        snapshot, _ = access.ensure_shared_lesson(_lesson(owner), owner)
        hidden = access.create_grant(snapshot, friendship=edge_a, granted_by=owner.user)
        visible = access.create_grant(snapshot, friendship=edge_b, granted_by=owner.user)
        LessonShareGrant.objects.filter(pk=hidden.pk).update(status="revoked")
        edge_a.status = "revoked"
        edge_a.save(update_fields=["status"])
        with self.app_role():
            set_rls_context(tenant_id=owner.id, user_id=owner.user_id, as_tenant=True)
            self.assertFalse(LessonShareGrant.objects.filter(pk=hidden.pk).exists())
            access.revoke_grant(visible)
            self.assertFalse(SharedLesson.objects.filter(pk=snapshot.pk).exists())
            with access.backstop_service_context():
                self.assertFalse(LessonShareGrant.objects.filter(shared_lesson_id=snapshot.pk).exists())

    def test_admin_collection_and_deletion_scope_includes_hidden_snapshots(self):
        actor = User.objects.create_superuser(username="admin-probe", password="pass")
        request = self.factory.get("/admin/")
        request.user = actor
        for admin_class, model in ((LessonAdmin, Lesson), (TenantAdmin, Tenant), (UserAdmin, User)):
            for operation in ("preview", "single", "bulk"):
                if model is User and operation != "preview":
                    continue  # Existing AppleAdminAtomicInvariantTests cover Azure teardown.
                with self.subTest(model=model.__name__, operation=operation):
                    owner = _tenant(f"admin-{model.__name__}-{operation}")
                    lesson = _lesson(owner)
                    snapshot, _ = access.ensure_shared_lesson(lesson, owner)
                    obj = lesson if model is Lesson else owner if model is Tenant else owner.user
                    model_admin = admin_class(model, admin.site)
                    with self.app_role():
                        self.assertFalse(SharedLesson.objects.filter(pk=snapshot.pk).exists())
                        if operation == "preview":
                            deleted, counts, _, _ = model_admin.get_deleted_objects([obj], request)
                            self.assertTrue(deleted)
                            self.assertEqual(counts[str(SharedLesson._meta.verbose_name_plural)], 1)
                        elif operation == "single":
                            model_admin.delete_model(request, obj)
                        else:
                            model_admin.delete_queryset(request, model.objects.filter(pk=obj.pk))
                        if operation != "preview":
                            with access.backstop_service_context():
                                self.assertFalse(SharedLesson.objects.filter(pk=snapshot.pk).exists())
                        self.assertEqual(guc("app.service_role"), "")

    def test_scrub_publish_failure_is_retryable_and_does_not_drop_proposal_push(self):
        owner, friend = _tenant("publish-owner"), _tenant("publish-friend")
        edge, lesson = _edge(owner, friend), _lesson(owner)
        with (
            mock.patch("apps.cron.publish.publish_task", side_effect=[RuntimeError("unavailable"), None]) as publish,
            mock.patch("apps.friends.notifications.notify_share_proposal") as notify,
        ):
            with transaction.atomic():
                first, created = services.propose_share(owner, lesson, edge)
                self.assertTrue(created)
                publish.assert_not_called()
            self.assertEqual(SharedLesson.objects.get(source_lesson=lesson).scrub_status, "failed")
            notify.assert_called_once()
            with transaction.atomic():
                second, created = services.propose_share(owner, lesson, edge)
                self.assertFalse(created)
            self.assertEqual(first.id, second.id)
            self.assertEqual(publish.call_count, 2)
            self.assertEqual(SharedLesson.objects.get(source_lesson=lesson).scrub_status, "pending")
            notify.assert_called_once()

    def test_failing_notifications_do_not_drop_later_commit_callbacks(self):
        owner, friend = _tenant("notify-owner"), _tenant("notify-friend")
        edge = _edge(owner, friend)
        seen = []
        with (
            mock.patch("apps.friends.notifications.notify_wave_received", side_effect=RuntimeError("failed")),
            mock.patch("apps.friends.notifications.notify_wave_app", side_effect=lambda *_: seen.append("app")),
            mock.patch("apps.friends.notifications.notify_share_proposal", side_effect=RuntimeError("failed")),
            mock.patch("apps.friends.services._enqueue_scrub"),
            transaction.atomic(),
        ):
            services._notify_wave_received(edge)
            services.propose_share(owner, _lesson(owner), edge)
            transaction.on_commit(lambda: seen.append("last"))
        self.assertEqual(seen, ["app", "last"])

    def _chat(self):
        sender, viewer = _tenant("absorb-sender"), _tenant("absorb-viewer")
        _profile(sender, "sender")
        edge = _edge(sender, viewer)
        thread = services.open_thread(sender, str(edge.id))
        with mock.patch("apps.friends.services._notify_friend_message"):
            message, _ = services.send_friend_message(sender, sender.user, str(thread.id), "m1", "private words")
        membership = FriendThreadMembership.objects.get(thread=thread, tenant=viewer)
        return sender, viewer, thread, message, membership

    def _runtime_context(self, viewer):
        with mock.patch("apps.integrations.runtime_views.validate_internal_runtime_request"):
            return RuntimeNeighborhoodContextView.as_view()(self.factory.get("/context/"), tenant_id=viewer.id)

    def test_absorb_redaction_outside_transaction_then_claims_only_materialized_batch(self):
        sender, viewer, thread, message, membership = self._chat()
        from apps.pii.redactor import RedactionOutcome

        def redact(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            self.assertEqual(guc("app.service_role"), "")
            membership.refresh_from_db()
            self.assertEqual(membership.last_absorbed_seq, 0)
            with mock.patch("apps.friends.services._notify_friend_message"):
                services.send_friend_message(sender, sender.user, str(thread.id), "m2", "later")
            return RedactionOutcome(text="safe", confirmed=True, reason="test")

        real_claim = access.claim_absorbed_chat

        def claim(*args):
            self.assertTrue(connection.in_atomic_block)
            self.assertEqual(guc("app.service_role"), "true")
            self.assertEqual(guc("app.tenant_id"), str(viewer.id))
            return real_claim(*args)

        with (
            mock.patch("apps.pii.redactor.redact_user_message_checked", side_effect=redact),
            mock.patch.object(access, "claim_absorbed_chat", side_effect=claim),
        ):
            response = self._runtime_context(viewer)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["chat"][0]["messages"], ["<<untrusted from @sender>> safe <</untrusted>>"])
        membership.refresh_from_db()
        self.assertEqual(membership.last_absorbed_seq, message.seq)
        self.assertEqual(AbsorbedItem.objects.filter(tenant=viewer).count(), 1)
        self.assertFalse(connection.in_atomic_block)
        self.assertEqual(guc("app.tenant_id"), "")

    def test_absorb_opt_out_during_redaction_drops_stale_batch_without_cursor_advance(self):
        _, viewer, _, _, membership = self._chat()
        from apps.pii.redactor import RedactionOutcome

        def redact(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            FriendThreadMembership.objects.filter(pk=membership.pk).update(agent_absorb_enabled=False)
            return RedactionOutcome(text="safe", confirmed=True, reason="test")

        with mock.patch("apps.pii.redactor.redact_user_message_checked", side_effect=redact):
            response = self._runtime_context(viewer)
        self.assertEqual(response.data["chat"], [])
        membership.refresh_from_db()
        self.assertEqual(membership.last_absorbed_seq, 0)
        self.assertFalse(AbsorbedItem.objects.filter(tenant=viewer).exists())

    def test_absorb_competing_request_claims_batch_once(self):
        _, viewer, _, message, membership = self._chat()
        from apps.pii.redactor import RedactionOutcome

        def redact(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            with mock.patch(
                "apps.pii.redactor.redact_user_message_checked",
                return_value=RedactionOutcome(text="safe", confirmed=True, reason="test"),
            ):
                winner = self._runtime_context(viewer)
            self.assertEqual(len(winner.data["chat"]), 1)
            return RedactionOutcome(text="safe", confirmed=True, reason="test")

        with mock.patch("apps.pii.redactor.redact_user_message_checked", side_effect=redact):
            loser = self._runtime_context(viewer)
        self.assertEqual(loser.data["chat"], [])
        membership.refresh_from_db()
        self.assertEqual(membership.last_absorbed_seq, message.seq)
        self.assertEqual(AbsorbedItem.objects.filter(tenant=viewer).count(), 1)

    def test_absorb_departure_or_block_during_redaction_discards_batch(self):
        _, viewer, thread, _, membership = self._chat()
        from apps.pii.redactor import RedactionOutcome

        for change in ("leave", "block"):

            def redact(*args, change=change, **kwargs):
                self.assertFalse(connection.in_atomic_block)
                if change == "leave":
                    FriendThreadMembership.objects.filter(pk=membership.pk).update(left_at=timezone.now())
                else:
                    thread.friendship.status = "blocked"
                    thread.friendship.save(update_fields=["status"])
                return RedactionOutcome(text="safe", confirmed=True, reason="test")

            with (
                self.subTest(change=change),
                mock.patch("apps.pii.redactor.redact_user_message_checked", side_effect=redact),
            ):
                response = self._runtime_context(viewer)
                self.assertEqual(response.data["chat"], [])
                membership.refresh_from_db()
                self.assertEqual(membership.last_absorbed_seq, 0)
                self.assertFalse(AbsorbedItem.objects.filter(tenant=viewer).exists())
            FriendThreadMembership.objects.filter(pk=membership.pk).update(left_at=None)

    def test_absorb_exception_or_failed_ledger_write_keeps_cursor_retryable(self):
        _, viewer, _, _, membership = self._chat()
        from apps.pii.redactor import RedactionOutcome

        with (
            mock.patch("apps.pii.redactor.redact_user_message_checked", side_effect=RuntimeError("interrupted")),
            self.assertRaises(RuntimeError),
        ):
            self._runtime_context(viewer)
        membership.refresh_from_db()
        self.assertEqual(membership.last_absorbed_seq, 0)
        with (
            mock.patch(
                "apps.pii.redactor.redact_user_message_checked",
                return_value=RedactionOutcome(text="safe", confirmed=True, reason="test"),
            ),
            mock.patch.object(services, "_log_absorbed", side_effect=RuntimeError("write failed")),
            self.assertRaises(RuntimeError),
        ):
            self._runtime_context(viewer)
        membership.refresh_from_db()
        self.assertEqual(membership.last_absorbed_seq, 0)
        self.assertFalse(AbsorbedItem.objects.filter(tenant=viewer).exists())
