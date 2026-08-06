import unittest

from services.domain import validate_operator_transition


class DomainStateTests(unittest.TestCase):
    def test_operator_state_machine_accepts_documented_transitions(self):
        validate_operator_transition("passed", "accepted_for_print")
        validate_operator_transition("warning", "returned_for_rework")
        validate_operator_transition("accepted_for_print", "returned_for_rework")

    def test_operator_state_machine_rejects_impossible_transition(self):
        with self.assertRaisesRegex(ValueError, "недопустимый переход"):
            validate_operator_transition("returned_for_rework", "accepted_for_print")

    def test_error_can_be_accepted_only_after_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, "недопустимый переход"):
            validate_operator_transition("error", "accepted_for_print")

        validate_operator_transition(
            "error", "accepted_for_print", confirm_failed_processing=True
        )


if __name__ == "__main__":
    unittest.main()
