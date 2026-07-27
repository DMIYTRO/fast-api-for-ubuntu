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


if __name__ == "__main__":
    unittest.main()
