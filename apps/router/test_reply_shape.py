from types import SimpleNamespace
from uuid import UUID

from django.test import SimpleTestCase, override_settings

from apps.router.quick_replies import extract_quick_replies
from apps.router.reply_shape import log_reply_shape


@override_settings(ONE_QUESTION_TENANT_IDS="*")
class ReplyShapeTests(SimpleTestCase):
    tenant = SimpleNamespace(id=UUID("00000000-0000-4000-8000-000000000111"))

    def test_counts_visible_questions_without_logging_text(self):
        for text, count, buttons in [
            ("A statement.", 0, 0),
            ("One?", 1, 0),
            ("One? Two? Three?", 3, 0),
            ("どうする？", 1, 0),
            ("One?\n```python\nx = '?'\n```", 1, 0),
            ("~~~\n？ ?\n~~~\nVisible?", 1, 0),
            ("    code?\n\tcode？\nVisible?", 1, 0),
            ("Pick?\n[[quick-replies: A? | B]]", 1, 1),
        ]:
            with self.subTest(text=text):
                visible, labels = extract_quick_replies(text, tenant_id=self.tenant.id)
                with self.assertLogs("apps.router.reply_shape") as logs:
                    log_reply_shape(self.tenant, visible, labels)
                self.assertEqual(
                    logs.records[0].getMessage(), f"reply_shape tenant=00000000 questions={count} buttons={buttons}"
                )
                self.assertEqual(len(logs.records), 1)

    @override_settings(ONE_QUESTION_TENANT_IDS="")
    def test_unflagged_logs_nothing(self):
        with self.assertNoLogs("apps.router.reply_shape"):
            log_reply_shape(self.tenant, "One?", ["A", "B"])

    def test_four_labels_only_for_flagged_tenant(self):
        text = "Pick?\n[[quick-replies: A | B | C | D]]"
        self.assertEqual(extract_quick_replies(text, tenant_id=self.tenant.id), ("Pick?", ["A", "B", "C", "D"]))
        with override_settings(ONE_QUESTION_TENANT_IDS=""), self.assertLogs("apps.router.quick_replies"):
            self.assertEqual(extract_quick_replies(text, tenant_id=self.tenant.id), ("Pick?", None))
