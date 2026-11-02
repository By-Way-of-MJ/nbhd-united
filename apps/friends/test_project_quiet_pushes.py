"""Per-recipient project push rollout; the in-app stream and digest stay intact."""

from types import SimpleNamespace
from unittest.mock import call, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import project_notifications as pushes

# All 14 ptypes emitted by project_notifications, including member_left's two
# branches and the two cron nudges. step_answer has an additional answer gate.
ATTENTION_KINDS = (
    "project_invite",
    "step_ask",
    "step_unblocked",
    "step_needs_look",
    "step_question",
)
QUIET_KINDS = (
    "step_note",
    "milestone_reached",
    "step_released",
    "project_owner",
    "member_left",
    "step_confirmed",
    "step_due",
    "step_still_yours",
)
ALL_KINDS = (*ATTENTION_KINDS, "step_answer", *QUIET_KINDS)
DIRECT_ROUTES = {
    "project_invite",
    "step_ask",
    "step_answer",
    "step_unblocked",
    "step_due",
    "milestone_reached",
    "step_note",
}
CHOICE_ANSWERS = ("dates", "smaller", "no", "other")


def _membership(*, muted=False):
    tenant = SimpleNamespace(id=uuid4())
    return SimpleNamespace(id=uuid4(), tenant=tenant, tenant_id=tenant.id, user=object(), muted=muted)


@override_settings(PROJECT_QUIET_PUSH_TENANT_IDS="")
class ProjectQuietPushTests(SimpleTestCase):
    def setUp(self):
        self.flagged = _membership()
        self.unflagged = _membership()
        self.actor_id = uuid4()
        self.mission_id = uuid4()
        self.step_id = uuid4()
        self.push = self.enterContext(patch("apps.router.push_views._push_to_user_devices", return_value={}))
        self.apns = self.enterContext(patch("apps.common.apns.apns_configured", return_value=True))
        self.blocked = self.enterContext(patch("apps.friends.access.blocked_counterpart_ids", return_value=set()))

    def deliver(self, members=None, *, ptype="step_note", **kwargs):
        return pushes._deliver(
            [self.flagged] if members is None else members,
            ptype=ptype,
            body="A project update",
            mission_id=self.mission_id,
            actor_tenant_id=self.actor_id,
            **kwargs,
        )

    def test_empty_flag_preserves_every_kind_and_routing(self):
        for ptype in ALL_KINDS:
            for step_id in (None, self.step_id):
                with self.subTest(ptype=ptype, step_id=step_id):
                    self.push.reset_mock()
                    self.assertEqual(self.deliver(ptype=ptype, step_id=step_id), 1)
                    route = ptype if ptype in DIRECT_ROUTES else "step_answer" if step_id else "milestone_reached"
                    extra = {"type": route, "mission_id": str(self.mission_id)}
                    if route != ptype:
                        extra["kind"] = ptype
                    if step_id:
                        extra["step_id"] = str(step_id)
                    self.push.assert_called_once_with(
                        self.flagged.user,
                        body="A project update",
                        thread_id=None,
                        collapse_id=f"{ptype}-{step_id or self.mission_id}"[:64],
                        content_available=True,
                        extra=extra,
                    )

    def test_empty_flag_preserves_every_answer(self):
        for answer in ("yes", *CHOICE_ANSWERS):
            with self.subTest(answer=answer):
                self.push.reset_mock()
                self.assertEqual(self.deliver(ptype="step_answer", answer=answer), 1)
                self.push.assert_called_once()

    def test_flagged_recipient_gets_all_six_attention_kinds(self):
        with override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=str(self.flagged.tenant_id)):
            for ptype in (*ATTENTION_KINDS, "step_answer"):
                with self.subTest(ptype=ptype):
                    self.push.reset_mock()
                    self.assertEqual(self.deliver(ptype=ptype, step_id=self.step_id, answer="dates"), 1)
                    self.push.assert_called_once()

    def test_flagged_recipient_gets_none_of_the_other_eight_kinds(self):
        with override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=str(self.flagged.tenant_id)):
            for ptype in QUIET_KINDS:
                with self.subTest(ptype=ptype):
                    self.assertEqual(self.deliver(ptype=ptype, step_id=self.step_id), 0)
        self.push.assert_not_called()

    def test_step_answer_yes_missing_and_unknown_are_quiet(self):
        with override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=str(self.flagged.tenant_id)):
            for answer in ("yes", None, "unknown"):
                with self.subTest(answer=answer):
                    self.assertEqual(self.deliver(ptype="step_answer", answer=answer), 0)
        self.push.assert_not_called()

    def test_step_answer_pushes_each_choice(self):
        with override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=str(self.flagged.tenant_id)):
            for answer in CHOICE_ANSWERS:
                with self.subTest(answer=answer):
                    self.push.reset_mock()
                    self.assertEqual(self.deliver(ptype="step_answer", answer=answer), 1)
                    self.push.assert_called_once()

    def test_answer_notifier_passes_choice_through_to_delivery(self):
        goal = SimpleNamespace(id=self.mission_id)
        step = SimpleNamespace(id=self.step_id, title="Plant trees")
        responder = SimpleNamespace(id=self.actor_id)
        with (
            override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=str(self.flagged.tenant_id)),
            patch("apps.friends.access.mission_memberships") as members,
            patch.object(pushes, "_name", return_value="A neighbor"),
            patch.object(pushes, "_dispatch", side_effect=lambda fn: fn()),
        ):
            members.return_value.filter.return_value.select_related.return_value.defer.return_value = [self.flagged]
            for answer in ("yes", *CHOICE_ANSWERS):
                with self.subTest(answer=answer):
                    self.push.reset_mock()
                    pushes.notify_step_answer(goal, step, answer, responder, self.flagged.tenant_id)
                    self.assertEqual(self.push.call_count, int(answer != "yes"))
                    if answer != "yes":
                        self.assertEqual(
                            self.push.call_args.kwargs["extra"],
                            {"type": "step_answer", "mission_id": str(goal.id), "step_id": str(step.id)},
                        )
            members.return_value.filter.assert_called_with(
                shared_goal=goal, tenant_id=self.flagged.tenant_id, status="active"
            )

    def test_mixed_recipients_only_unflagged_get_quiet_events_in_either_order(self):
        with override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=str(self.flagged.tenant_id)):
            for ptype in (*QUIET_KINDS, "step_answer"):
                for members in ([self.flagged, self.unflagged], [self.unflagged, self.flagged]):
                    with self.subTest(ptype=ptype, flagged_first=members[0] is self.flagged):
                        self.push.reset_mock()
                        self.assertEqual(self.deliver(members, ptype=ptype, answer="yes"), 1)
                        self.push.assert_called_once()
                        self.assertIs(self.push.call_args.args[0], self.unflagged.user)

    def test_mixed_recipients_both_get_attention_events(self):
        with override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=str(self.flagged.tenant_id)):
            for ptype in (*ATTENTION_KINDS, "step_answer"):
                with self.subTest(ptype=ptype):
                    self.push.reset_mock()
                    self.assertEqual(self.deliver([self.flagged, self.unflagged], ptype=ptype, answer="other"), 2)
                    self.assertEqual(
                        [c.args[0] for c in self.push.call_args_list], [self.flagged.user, self.unflagged.user]
                    )

    def test_allowlist_trims_comma_tokens_and_accepts_uuid_spelling(self):
        allowlist = f" , {uuid4()}, {str(self.flagged.tenant_id).upper()}, , invalid "
        with override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=allowlist):
            self.assertEqual(self.deliver([self.flagged, self.unflagged]), 1)
        self.assertIs(self.push.call_args.args[0], self.unflagged.user)

    @override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=" , * , ")
    def test_wildcard_flags_every_recipient(self):
        self.assertEqual(self.deliver([self.flagged, self.unflagged]), 0)
        self.push.assert_not_called()
        self.assertEqual(self.deliver([self.flagged, self.unflagged], ptype="step_ask"), 2)

    def test_invalid_tokens_do_not_enable_flag(self):
        for allowlist in ("invalid", "prefix*suffix", "   ,   "):
            with self.subTest(allowlist=allowlist), override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=allowlist):
                self.push.reset_mock()
                self.assertEqual(self.deliver([self.flagged, self.unflagged]), 2)
                self.assertEqual(self.push.call_count, 2)

    def test_muted_blocked_and_actor_still_excluded_with_flag_on_or_off(self):
        muted = _membership(muted=True)
        blocked = _membership()
        actor = _membership()
        actor.tenant.id = actor.tenant_id = self.actor_id
        self.blocked.return_value = {blocked.tenant_id}
        for allowlist in ("", "*"):
            with self.subTest(allowlist=allowlist), override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=allowlist):
                self.push.reset_mock()
                self.assertEqual(self.deliver([muted, blocked, actor, self.flagged], ptype="step_ask"), 1)
                self.push.assert_called_once()
                self.assertIs(self.push.call_args.args[0], self.flagged.user)
                self.blocked.assert_called_with(self.actor_id)

    def test_return_count_excludes_skips_and_failures_and_continues_after_failure(self):
        failing = _membership()
        self.push.side_effect = [RuntimeError("Transport failed"), {}]
        with (
            override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=str(self.flagged.tenant_id)),
            self.assertLogs(pushes.__name__, level="WARNING"),
        ):
            self.assertEqual(self.deliver([self.flagged, failing, self.unflagged, _membership(muted=True)]), 1)
        self.assertEqual([c.args[0] for c in self.push.call_args_list], [failing.user, self.unflagged.user])

    def test_no_apns_means_no_sends(self):
        self.apns.return_value = False
        self.assertEqual(self.deliver(ptype="step_ask"), 0)
        self.push.assert_not_called()
        self.blocked.assert_not_called()

    def test_no_actor_still_filters_per_recipient_without_block_lookup(self):
        with override_settings(PROJECT_QUIET_PUSH_TENANT_IDS=str(self.flagged.tenant_id)):
            count = pushes._deliver(
                [self.flagged, self.unflagged], ptype="step_due", body="Due tomorrow", mission_id=self.mission_id
            )
        self.assertEqual(count, 1)
        self.blocked.assert_not_called()
        self.assertEqual(self.push.call_args_list, [call(self.unflagged.user, **self.push.call_args.kwargs)])
