import logging
import tempfile
import unittest
from pathlib import Path

from server.logging_config import configure_logging, shutdown_logging
from server.settings import Settings


class PersistentLoggingTests(unittest.TestCase):
    def test_rotates_journal_and_keeps_native_fault_file(self):
        with tempfile.TemporaryDirectory() as directory:
            try:
                log_dir = Path(directory) / "logs"
                settings = Settings(
                    database_url="sqlite://",
                    password_hash=None,
                    log_dir=log_dir,
                    log_max_bytes=1024,
                    log_backup_count=2,
                )
                log_path = configure_logging(settings)
                logger = logging.getLogger("image_magic.test")
                for index in range(100):
                    logger.info(
                        "rotation-check index=%d payload=%s", index, "x" * 80
                    )

                self.assertTrue(log_path.is_file())
                self.assertTrue((log_dir / "image-magic.log.1").is_file())
                self.assertTrue((log_dir / "image-magic-fault.log").is_file())
            finally:
                shutdown_logging()


if __name__ == "__main__":
    unittest.main()
