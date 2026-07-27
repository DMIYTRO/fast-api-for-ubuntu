import unittest
from pathlib import Path

from processing.models import FileCheck, OrderCheck, ParsedFilename
from services.repository import InMemoryRunRepository
from services.use_cases import record_completed_cli_run


class SharedUseCaseTests(unittest.TestCase):
    def test_cli_history_uses_standard_run_repository(self):
        parsed = ParsedFilename("42", "1001", 90, 50, 4, 0, "face")
        order = OrderCheck(
            "1001",
            "42",
            files=[
                FileCheck(
                    Path("/input/face.jpg"),
                    parsed=parsed,
                    dpi=300,
                    dpi_x=300,
                    dpi_y=300,
                )
            ],
        )
        repository = InMemoryRunRepository()

        run_id = record_completed_cli_run(
            repository,
            input_path=Path("/input"),
            direction="digital",
            orders=[order],
        )

        run = repository.get_run(run_id)
        self.assertEqual(run["options"]["source"], "cli")
        self.assertEqual(run["status"], "completed")
        self.assertEqual(list(run["orders"]), ["42:1001"])
        self.assertEqual(repository.list_events(run_id)[0].type, "run.completed")


if __name__ == "__main__":
    unittest.main()
