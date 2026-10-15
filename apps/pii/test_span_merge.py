"""Preserve decoder overlaps; resolve only disagreement across windows."""

from unittest import TestCase
from unittest.mock import patch

from apps.pii.span_merge import _merge_spans


def span(start, end, label, score=1.0):
    return {"start": start, "end": end, "entity_group": label, "score": score}


def observed(start, end, label, window, context, score=1.0):
    return {**span(start, end, label, score), "_window": window, "_context": context}


class CrossWindowConflictTests(TestCase):
    def test_same_window_overlap_preserves_decoder_detections(self):
        raw = [observed(0, 10, "contact.phone", 0, 0), observed(2, 10, "financial.bank_account", 0, 2)]
        result = _merge_spans(raw, "1234567890")
        self.assertEqual(
            [(s["entity_group"], s["start"], s["end"]) for s in result],
            [
                ("contact.phone", 0, 10),
                ("financial.bank_account", 2, 10),
            ],
        )
        self.assertTrue(all(not any(k.startswith("_") for k in s) for s in result))

    def test_same_window_adjacent_parts_do_not_create_a_false_conflict(self):
        raw = [
            observed(0, 5, "identity.person_name", 0, 1),
            observed(5, 10, "identity.person_name", 0, 5),
            observed(4, 8, "org.company_name", 0, 4),
        ]
        result = _merge_spans(raw, "John Smith")
        self.assertEqual(len(result), 2)

    def test_coobserved_decoder_overlap_is_not_a_cross_window_disagreement(self):
        raw = [
            observed(0, 10, label, window, 5)
            for window in (0, 1)
            for label in ("contact.phone", "financial.bank_account")
        ]
        result = _merge_spans(raw, "1234567890")
        self.assertEqual(len(result), 2)

    def test_new_cross_window_extension_does_not_inherit_same_window_exemption(self):
        raw = [
            observed(0, 10, "FIRSTNAME", 0, 1),
            observed(5, 10, "LASTNAME", 0, 5),
            observed(5, 16, "LASTNAME", 1, 20),
        ]
        result = _merge_spans(raw, "John Smith Jones")
        self.assertEqual([(s["start"], s["end"]) for s in result], [(0, 16)])

    def test_more_context_wins_label_but_preserves_whole_range(self):
        raw = [observed(0, 10, "FIRSTNAME", 0, 1, 1), observed(5, 10, "LASTNAME", 1, 20, 0.8)]
        result = _merge_spans(raw, "John Smith")
        self.assertEqual([(s["entity_group"], s["start"], s["end"]) for s in result], [("LASTNAME", 0, 10)])
        self.assertEqual(result[0]["word"], "John Smith")
        self.assertEqual(result[0]["score"], 1)

    def test_equal_context_prefers_higher_score_and_input_order_is_irrelevant(self):
        raw = [observed(0, 10, "FIRSTNAME", 0, 10, 0.7), observed(5, 10, "LASTNAME", 1, 10, 0.9)]
        result = _merge_spans(raw, "John Smith")
        self.assertEqual(result[0]["entity_group"], "LASTNAME")
        self.assertEqual(result, _merge_spans(list(reversed(raw)), "John Smith"))

    def test_unsafe_type_switches_keep_both_ranges_and_labels(self):
        for a, b in (
            ("identity.person_name", "org.company_name"),
            ("identity.person_name", "contact.address"),
            ("contact.phone", "financial.bank_account"),
            ("identity.date_of_birth", "identity.national_id"),
            ("BUILDINGNUMBER", "STREET"),
            ("PIN", "PASSWORD"),
        ):
            with self.subTest(pair=(a, b)):
                result = _merge_spans([observed(0, 10, a, 0, 2), observed(5, 10, b, 1, 20)], "John Smith")
                self.assertEqual([(s["entity_group"], s["start"], s["end"]) for s in result], [(a, 0, 10), (b, 5, 10)])

    def test_unsafe_conflict_preserves_both_noncontained_ranges(self):
        raw = [observed(0, 10, "identity.person_name", 0, 2), observed(5, 16, "contact.address", 1, 20)]
        result = _merge_spans(raw, "John Smith Jones")
        self.assertEqual(
            [(s["entity_group"], s["start"], s["end"]) for s in result],
            [("identity.person_name", 0, 10), ("contact.address", 5, 16)],
        )
        self.assertEqual({i for s in result for i in range(s["start"], s["end"])}, set(range(16)))

    def test_same_type_id_aliases_keep_context_winner_and_union(self):
        raw = [observed(0, 8, "identity.ssn", 0, 4), observed(2, 10, "identity.tax_id", 1, 8)]
        result = _merge_spans(raw, "1234567890")
        self.assertEqual((result[0]["entity_group"], result[0]["start"], result[0]["end"]), ("identity.tax_id", 0, 10))

    def test_structurally_invalid_extended_union_keeps_original_labels(self):
        raw = [observed(0, 8, "identity.ssn", 0, 4), observed(2, 12, "identity.tax_id", 1, 8)]
        result = _merge_spans(raw, "12345678\nabc")
        self.assertEqual([(s["start"], s["end"]) for s in result], [(0, 8), (2, 12)])

    def test_transitive_conflict_union_keeps_all_ranges(self):
        raw = [
            observed(0, 6, "FIRSTNAME", 0, 1),
            observed(4, 12, "LASTNAME", 1, 2),
            observed(10, 16, "FULLNAME", 2, 30),
        ]
        result = _merge_spans(raw, "John Smith Jones")
        self.assertEqual([(s["entity_group"], s["start"], s["end"]) for s in result], [("FULLNAME", 0, 16)])


class BaselinePreservationTests(TestCase):
    def preserve(self, baseline, later, text):
        from apps.pii.span_merge import _preserve_baseline

        return _preserve_baseline(baseline, later, text)

    def test_unsafe_conflict_falls_back_without_losing_baseline_or_isolated_tail(self):
        text = "John Smith ... Jane Doe"
        baseline = [span(0, 10, "identity.person_name")]
        later = [observed(5, 10, "contact.address", 1, 20), observed(15, 23, "identity.person_name", 1, 30)]
        result = self.preserve(baseline, later, text)
        self.assertEqual(result[0], baseline[0])
        self.assertEqual([(s["start"], s["end"]) for s in result], [(0, 10), (15, 23)])

    def test_dedup_can_expose_prefix_so_pristine_baseline_must_survive(self):
        from apps.pii.redactor import DetectedEntity, _deduplicate_overlapping

        original = DetectedEntity(entity_type="PERSON", start=0, end=10, score=1)
        suffix = DetectedEntity(entity_type="LOCATION", start=5, end=10, score=1)
        self.assertEqual(_deduplicate_overlapping([original, suffix]), [suffix])
        result = self.preserve(
            [span(0, 10, "identity.person_name")], [observed(5, 10, "contact.address", 1, 20)], "John Smith"
        )
        self.assertEqual(result, [span(0, 10, "identity.person_name")])

    def test_same_label_extension_cannot_change_baseline_allowlist_or_validation(self):
        text = "John Smith Jones"
        baseline = [span(0, 10, "identity.person_name")]
        result = self.preserve(baseline, [observed(5, 16, "identity.person_name", 1, 20)], text)
        self.assertEqual(result, baseline)

    def test_word_snap_and_two_character_address_context_cannot_touch_baseline(self):
        for text, baseline, extra in (
            ("JohnSmith", span(0, 4, "identity.person_name"), observed(4, 9, "contact.address", 1, 20)),
            ("12  Baker Street", span(0, 2, "BUILDINGNUMBER"), observed(4, 16, "STREET", 1, 20)),
        ):
            with self.subTest(label=baseline["entity_group"]):
                self.assertEqual(self.preserve([baseline], [extra], text), [baseline])

    def test_later_conflict_is_omitted_but_independent_later_span_survives(self):
        text = "John Smith ... Jane Doe ... 1234567890"
        baseline = [span(0, 10, "identity.person_name")]
        later = [
            observed(15, 23, "identity.person_name", 1, 5),
            observed(27, 37, "contact.phone", 1, 5),
            observed(29, 37, "financial.bank_account", 2, 10),
        ]
        result = self.preserve(baseline, later, text)
        self.assertEqual([(s["start"], s["end"]) for s in result], [(0, 10), (15, 23)])

    def test_pattern_bridge_cannot_change_original_overlap_resolution(self):
        from types import SimpleNamespace

        # A validated pattern can bridge a baseline span and a later detection.
        # Protect it even though it is appended outside the neural adapter.
        recognizer = SimpleNamespace(analyze=lambda **kw: [SimpleNamespace(start=0, end=20)])
        baseline = [span(0, 5, "identity.person_name")]
        with patch("apps.pii.engine.get_pattern_recognizers", return_value={"EMAIL_ADDRESS": recognizer}):
            result = self.preserve(baseline, [observed(15, 20, "contact.email", 1, 20)], "Alice ... user@email.test")
        self.assertEqual(result, baseline)

    def test_full_text_hybrid_tail_and_later_neural_additions_remain_position_sorted(self):
        text = "John Smith ... Jane Doe ... Alice Jones"
        baseline = [span(0, 10, "identity.person_name"), span(27, 38, "identity.person_name")]
        result = self.preserve(baseline, [observed(15, 23, "identity.person_name", 1, 20)], text)
        self.assertEqual([(s["start"], s["end"]) for s in result], [(0, 10), (15, 23), (27, 38)])
        self.assertEqual(result[0], baseline[0])
        self.assertEqual(result[2], baseline[1])

    def test_added_spans_cannot_overflow_shared_response_cap(self):
        text = "John Smith ... Jane Doe ... Alice Jones"
        baseline = [span(0, 10, "identity.person_name")]
        later = [observed(15, 23, "identity.person_name", 1, 20), observed(27, 38, "identity.person_name", 1, 20)]
        with patch("apps.pii.shared_client.MAX_SPANS", 2):
            result = self.preserve(baseline, later, text)
        self.assertEqual(result[0], baseline[0])
        self.assertEqual(len(result), 2)
        self.assertEqual((result[1]["start"], result[1]["end"]), (15, 23))

    def test_actual_downstream_filters_preserve_baseline_for_allowlists_and_tiers(self):
        from apps.pii.config import DEBERTA_LABEL_MAP
        from apps.pii.redactor import _detect_pii, _filter_results

        text = "John Smith ... Jane Doe ... user@example.com"
        baseline = [span(0, 10, "identity.person_name")]
        later = [observed(5, 10, "contact.address", 1, 20), observed(15, 23, "identity.person_name", 1, 30)]
        result = self.preserve(baseline, later, text)
        for entities in (["PERSON"], list(set(DEBERTA_LABEL_MAP.values()))):
            for names in (set(), {"Smith"}, {"Jane Doe"}, {"John Smith"}):
                coverage = []
                for raw in (baseline, result):
                    with patch("apps.pii.engine.get_pii_pipeline", return_value=lambda text, raw=raw: raw):
                        filtered = _filter_results(_detect_pii(text, entities, 0.5), text, names)
                    coverage.append({i for s in filtered for i in range(s.start, s.end)})
                self.assertFalse(coverage[0] - coverage[1])
