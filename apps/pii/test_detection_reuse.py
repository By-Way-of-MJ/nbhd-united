"""Operation-scoped raw neural spans never replace policy or receipt checks."""

from time import monotonic
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .redactor import _detect_pii, _neural_detector_available, reuse_detections


class DetectionReuseTests(SimpleTestCase):
    def detect(self, text="Meet Dana", entities=None, threshold=0.5, **kwargs):
        return _detect_pii(text, entities if entities is not None else ["PERSON"], threshold, **kwargs)

    def test_success_reused_but_threshold_and_entities_are_applied_each_time(self):
        detector = Mock(return_value=[{"entity_group": "FIRSTNAME", "score": 0.8, "start": 5, "end": 9}])
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=detector),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}) as patterns,
            reuse_detections(),
        ):
            self.assertEqual(len(self.detect()), 1)
            self.assertEqual(self.detect(threshold=0.9), [])
            self.assertEqual(self.detect(entities=[]), [])
            self.assertTrue(_neural_detector_available())
        self.assertEqual(detector.call_count, 1)
        self.assertEqual(patterns.call_count, 3)

    def test_failures_are_not_cached_and_cache_is_discarded_on_scope_exit(self):
        detector = Mock(side_effect=[RuntimeError("offline"), [], [], []])
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=detector),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
        ):
            with self.assertRaisesMessage(ValueError, "exit"), reuse_detections():
                self.detect()
                self.assertFalse(_neural_detector_available())
                self.detect()
                self.assertTrue(_neural_detector_available())
                self.detect()
                raise ValueError("exit")
            self.detect()
            with reuse_detections():
                self.detect()
        self.assertEqual(detector.call_count, 4)

    def test_changed_text_and_deadline_calls_run_detector_again(self):
        detector = Mock(return_value=[])
        with (
            patch("apps.pii.engine.get_pii_pipeline", return_value=detector),
            patch("apps.pii.engine.get_pattern_recognizers", return_value={}),
            reuse_detections(),
        ):
            self.detect()
            self.detect("Meet someone")
            self.detect(deadline=monotonic() + 60)
            self.detect()
        self.assertEqual(detector.call_count, 3)
