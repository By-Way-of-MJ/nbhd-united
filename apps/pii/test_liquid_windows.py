"""Model-free tests for tokenizer-owned overflow windows."""

import re
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from apps.pii.liquid_engine import _LiquidPiiPipeline


class FakeClock:
    def __init__(self, now=0.0):
        self.now = now

    def monotonic(self):
        return self.now


class FakeTokenizer:
    def __init__(self):
        self.calls = 0

    def __call__(self, text, **kwargs):
        self.calls += 1
        offsets = [(i, i + 1) for i, char in enumerate(text) for _ in range(2 if char == "界" else 1)]
        return {
            "input_ids": [999] + list(range(len(offsets))),
            "attention_mask": [1] * (len(offsets) + 1),
            "offset_mapping": [(0, 0)] + offsets,
            "special_tokens_mask": [1] + [0] * len(offsets),
        }


class LiquidWindowTests(TestCase):
    def pipeline(self, failure=None, *, clock=None, durations=()):
        tokenizer = FakeTokenizer()
        windows = []
        durations = iter(durations)

        def predict(text, tok, model, hybrid):
            encoded = tok.encoding
            self.assertLessEqual(len(encoded["input_ids"]), 2048)
            self.assertTrue(all(0 <= a <= b <= len(text) for a, b in encoded["offset_mapping"]))
            windows.append(text)
            if failure and len(windows) == 2:
                raise failure
            if clock is not None:
                clock.now += next(durations)
            return [
                {"type": "identity.person_name", "start": m.start(), "end": m.end()}
                for m in re.finditer("John Smith|TAIL_NAME", text[: max(b for _, b in encoded["offset_mapping"])])
            ]

        decoder = SimpleNamespace(predict=Mock(side_effect=predict))
        return _LiquidPiiPipeline(tokenizer, object(), decoder), tokenizer, windows

    def test_fitting_text_is_one_prediction_with_identical_span_shape(self):
        pipeline, tokenizer, windows = self.pipeline()
        text = "a" * 2037 + "John Smith"
        result = pipeline(text)
        self.assertEqual(
            result,
            [{"entity_group": "identity.person_name", "word": "John Smith", "score": 1.0, "start": 2037, "end": 2047}],
        )
        self.assertEqual(windows, [text])
        self.assertEqual(tokenizer.calls, 1)

    def test_overflow_preserves_boundary_and_tail_with_one_tokenization(self):
        pipeline, tokenizer, windows = self.pipeline()
        text = "a" * 2042 + " John Smith" + " b" * 900 + "TAIL_NAME"
        result = pipeline(text)
        self.assertEqual([(s["start"], s["end"]) for s in result], [(2043, 2053), (3853, 3862)])
        self.assertEqual(tokenizer.calls, 1)
        self.assertGreater(len(windows), 1)
        self.assertTrue(all(s["word"] == text[s["start"] : s["end"]] for s in result))

    def test_multitoken_unicode_character_is_not_split_or_lost(self):
        pipeline, tokenizer, windows = self.pipeline()
        text = "界" * 2500 + "TAIL_NAME"
        result = pipeline(text)
        self.assertEqual(result[-1]["start"], 2500)
        self.assertEqual(tokenizer.calls, 1)
        self.assertEqual(windows[0], text)
        self.assertTrue(all(len(part) <= 2047 for part in windows[1:]))

    def test_failure_never_returns_partial_spans(self):
        failure = TimeoutError("synthetic")
        pipeline, _, _ = self.pipeline(failure)
        with self.assertRaises(TimeoutError) as caught:
            pipeline("John Smith" + "a" * 4000)
        self.assertIs(caught.exception, failure)

    def test_only_special_tokens_empty_input_and_exact_overflow(self):
        pipeline, tokenizer, windows = self.pipeline()
        self.assertEqual(pipeline(""), [])
        self.assertEqual(windows, [""])
        windows.clear()
        self.assertEqual(pipeline("a" * 2048), [])
        self.assertEqual(len(windows), 2)
        self.assertEqual(tokenizer.calls, 2)

    def test_cross_window_suffix_keeps_pristine_baseline_label_and_range(self):
        pipeline, _, _ = self.pipeline()
        text = "a" * 2010 + "John Smith" + "b" * 1000

        def predict(part, tok, model, hybrid):
            whole = part.find("John Smith")
            if part.startswith("a" * 2000):
                return [{"type": "FIRSTNAME", "start": whole, "end": whole + 10}]
            suffix = part.find("Smith")
            return [{"type": "LASTNAME", "start": suffix, "end": suffix + 5}]

        pipeline._decoder.predict.side_effect = predict
        self.assertEqual(
            pipeline(text),
            [{"entity_group": "FIRSTNAME", "word": "John Smith", "start": 2010, "end": 2020, "score": 1.0}],
        )

    def test_deadline_before_baseline_raises_timeout(self):
        from unittest.mock import patch

        from apps.pii.shared_client import SharedPiiError

        pipeline, _, windows = self.pipeline()
        with (
            patch("apps.pii.liquid_engine.time.monotonic", return_value=2.0),
            self.assertRaises(SharedPiiError) as caught,
        ):
            pipeline("John Smith " + "a " * 2000, deadline=1.0)
        self.assertEqual(caught.exception.outcome, "timeout")
        self.assertEqual(windows, [])

    def test_deadline_after_window_one_returns_exact_baseline(self):
        from unittest.mock import patch

        clock = FakeClock()
        pipeline, _, windows = self.pipeline(clock=clock, durations=[2.0])
        counts = Mock()
        with patch("apps.pii.liquid_engine.time", clock):
            result = pipeline("John Smith " + "a " * 2000, deadline=1.0, on_window_deadline=counts)
        self.assertEqual(
            result,
            [{"entity_group": "identity.person_name", "word": "John Smith", "start": 0, "end": 10, "score": 1.0}],
        )
        self.assertEqual(len(windows), 1)
        counts.assert_called_once_with(1, 3)

    def test_deadline_after_window_two_of_three_preserves_completed_spans(self):
        from unittest.mock import patch

        clock = FakeClock()
        pipeline, _, windows = self.pipeline(clock=clock, durations=[0.1, 1.2])
        text = "John Smith " + "a " * 1100 + "TAIL_NAME " + "b " * 1000
        counts = Mock()
        with patch("apps.pii.liquid_engine.time", clock):
            result = pipeline(text, deadline=1.0, on_window_deadline=counts)
        self.assertEqual([(s["start"], s["end"]) for s in result], [(0, 10), (2211, 2220)])
        self.assertEqual(len(windows), 2)
        counts.assert_called_once_with(2, 3)

    def test_last_window_finishing_after_deadline_returns_completed_result(self):
        from unittest.mock import patch

        clock = FakeClock()
        pipeline, _, windows = self.pipeline(clock=clock, durations=[0.1, 1.2])
        counts = Mock()
        with patch("apps.pii.liquid_engine.time", clock):
            result = pipeline("a " * 1500 + "TAIL_NAME", deadline=1.0, on_window_deadline=counts)
        self.assertEqual([(s["start"], s["end"]) for s in result], [(3000, 3009)])
        self.assertEqual(len(windows), 2)
        counts.assert_called_once_with(2, 2)

    def test_full_text_hybrid_baseline_reuses_exact_truncated_first_inference(self):
        pipeline, tokenizer, windows = self.pipeline()
        text = "界" * 2500 + " HYBRID_ONLY"
        calls = []

        def predict(part, tok, model, hybrid):
            calls.append((part, tok.encoding))
            # The real hybrid decoder scans the FULL text after truncating
            # neural input. Simulate a context rule requiring the whole input.
            if part == text:
                return [{"type": "contact.email", "start": 2501, "end": len(text)}]
            return []

        pipeline._decoder.predict.side_effect = predict
        result = pipeline(text)
        self.assertEqual(
            result,
            [{"entity_group": "contact.email", "start": 2501, "end": len(text), "word": "HYBRID_ONLY", "score": 1.0}],
        )
        self.assertEqual(tokenizer.calls, 1)
        self.assertEqual(len(calls), 3)
        # Origin truncates at exactly 2048 IDs, even inside a multibyte char.
        self.assertEqual(calls[0][1]["input_ids"], [999] + list(range(2047)))
        self.assertEqual(calls[0][0], text)

    def test_unsafe_cross_window_conflict_never_raises_or_exposes_original_name(self):
        pipeline, _, _ = self.pipeline()
        text = "a " * 1000 + "John Smith" + " b" * 500

        def predict(part, tok, model, hybrid):
            name = part.index("John Smith")
            if part == text:
                return [{"type": "identity.person_name", "start": name, "end": name + 10}]
            return [{"type": "contact.address", "start": name + 5, "end": name + 10}]

        pipeline._decoder.predict.side_effect = predict
        result = pipeline(text)
        self.assertEqual(
            result,
            [{"entity_group": "identity.person_name", "word": "John Smith", "start": 2000, "end": 2010, "score": 1.0}],
        )

    def test_insufficient_foreground_budget_does_not_start_window_two(self):
        from unittest.mock import patch

        clock = FakeClock()
        pipeline, _, windows = self.pipeline(clock=clock, durations=[3.0])
        counts = Mock()
        with patch("apps.pii.liquid_engine.time", clock):
            result = pipeline("John Smith " + "a " * 2000, deadline=5.0, on_window_deadline=counts)
        self.assertEqual([(s["start"], s["end"]) for s in result], [(0, 10)])
        self.assertEqual(len(windows), 1)
        self.assertEqual(clock.now, 3.0)
        counts.assert_called_once_with(1, 3)

    def test_sufficient_and_background_budgets_run_all_windows(self):
        from unittest.mock import patch

        for deadline, durations in ((5.0, [1.0, 1.0, 1.0]), (30.0, [4.0, 5.0, 4.0])):
            with self.subTest(deadline=deadline):
                clock = FakeClock()
                pipeline, _, windows = self.pipeline(clock=clock, durations=durations)
                counts = Mock()
                text = "John Smith " + "a " * 2000 + "TAIL_NAME"
                with patch("apps.pii.liquid_engine.time", clock):
                    result = pipeline(text, deadline=deadline, on_window_deadline=counts)
                self.assertEqual([(s["start"], s["end"]) for s in result], [(0, 10), (4011, 4020)])
                self.assertEqual(len(windows), 3)
                counts.assert_not_called()

    def test_admission_uses_slowest_completed_window_and_inclusive_threshold(self):
        from unittest.mock import patch

        for deadline, durations, expected in ((5.0, [0.5, 2.0], 2), (2.5, [1.0, 1.0], 2), (2.49, [1.0], 1)):
            with self.subTest(deadline=deadline):
                clock = FakeClock()
                pipeline, _, windows = self.pipeline(clock=clock, durations=durations)
                text = "John Smith " + "a " * (2000 if deadline == 5.0 else 1500)
                with patch("apps.pii.liquid_engine.time", clock):
                    result = pipeline(text, deadline=deadline)
                self.assertEqual(len(windows), expected)
                self.assertEqual(result[0]["word"], "John Smith")
