import unittest

from processing.resample_policy import ResampleDecision, analyze_resample


class ResamplePolicyTests(unittest.TestCase):
    def test_metadata_noise_is_accepted(self):
        plan = analyze_resample((94.0, 54.1), (94.0, 54.0), (300.0, 300.0))
        self.assertEqual(plan.decision, ResampleDecision.ACCEPT)

    def test_small_difference_is_auto_corrected(self):
        plan = analyze_resample((94.0, 54.3), (94.0, 54.0), (300.0, 300.0))
        self.assertEqual(plan.decision, ResampleDecision.AUTO_CORRECT)
        self.assertAlmostEqual(plan.crop_mm[1], 0.3)

    def test_medium_crop_requires_confirmation(self):
        plan = analyze_resample((960.0, 560.0), (94.0, 54.0), (300.0, 300.0))
        self.assertEqual(plan.decision, ResampleDecision.ASK_CONFIRMATION)

    def test_bad_orientation_and_proportions_are_rejected(self):
        plan = analyze_resample((202.0, 214.0), (214.0, 152.0), (300.0, 300.0))
        self.assertEqual(plan.decision, ResampleDecision.REJECT)

    def test_only_mismatched_side_needs_rotation(self):
        face = analyze_resample((94.0, 54.0), (94.0, 54.0), (300.0, 300.0))
        back = analyze_resample((54.0, 94.0), (94.0, 54.0), (300.0, 300.0))
        self.assertEqual(face.rotation_degrees, 0)
        self.assertEqual(back.rotation_degrees, 90)
        self.assertEqual(back.decision, ResampleDecision.AUTO_CORRECT)

    def test_small_enlargement_below_minimum_dpi_is_rejected(self):
        plan = analyze_resample((93.5, 54.0), (94.0, 54.0), (250.0, 250.0))
        self.assertEqual(plan.decision, ResampleDecision.REJECT)


if __name__ == "__main__":
    unittest.main()
