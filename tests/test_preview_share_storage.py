from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from uuid import uuid4

from server.database import Database
from services.file_lifecycle import FileLifecycle
from services.preview_storage import preview_order_directory, preview_run_directory
from services.return_preview import custom_return_preview_path
from services.sql_repository import SqlRunRepository
from scripts.migrate_previews_to_share import ambiguous_custom_keys, migrate_run


class PreviewShareStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.input = self.root / "input"
        self.input.mkdir()
        self.share = self.root / "shared" / "Previews"
        self.database = Database(f"sqlite:///{self.root / 'runs.sqlite3'}")
        self.database.create_schema()
        self.repository = SqlRunRepository(self.database.session_factory, recover_interrupted=False)
        self.run_id = uuid4().hex

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def test_migration_dry_run_copy_verify_and_relink(self):
        legacy = self.input / "Previews" / "art-face_preview.png"
        legacy.parent.mkdir()
        legacy.write_bytes(b"compact")
        legacy.with_name("art-face_preview_large.png").write_bytes(b"detailed")
        custom = self.input / "Previews" / "Custom" / "1_return-preview.png"
        custom.parent.mkdir()
        custom.write_bytes(b"custom")
        collage = self.input / "Previews" / "Return" / "1_return-preview.png"
        collage.parent.mkdir()
        collage.write_bytes(b"return")
        run = {
            "id": self.run_id, "status": "completed", "stage": "completed", "progress": 100,
            "options": {"input_path": str(self.input), "direction": "digital"},
            "orders": {"c:1": {
                "order_id": "1", "customer_id": "c", "status": "passed", "passed": True,
                "preview_paths": [str(legacy)], "files": [{
                    "path": str(self.input / "art-face.jpg"), "name": "art-face.jpg",
                    "preview_path": str(legacy), "errors": [], "warnings": [],
                }],
            }},
        }
        self.repository.create_run(run)
        dry = migrate_run(self.repository, self.run_id, self.share)
        self.assertEqual(dry, {"previews": 1, "detailed": 1, "custom": 1, "return": 1, "ambiguous": 0, "missing": 0})
        self.assertFalse(self.share.exists())
        self.assertEqual(self.repository.get_run(self.run_id)["orders"]["c:1"]["preview_paths"], [str(legacy)])

        applied = migrate_run(self.repository, self.run_id, self.share, apply=True)
        self.assertEqual(applied, dry)
        migrated = self.repository.get_run(self.run_id)
        target = Path(migrated["orders"]["c:1"]["preview_paths"][0])
        self.assertTrue(target.is_relative_to(preview_order_directory(self.share, self.run_id, "c:1")))
        self.assertEqual(target.read_bytes(), b"compact")
        self.assertEqual(target.with_name("art-face_preview_large.png").read_bytes(), b"detailed")
        self.assertEqual(migrated["options"]["preview_root"], str(self.share.resolve()))
        self.assertEqual(self.repository.get_run(self.run_id)["orders"]["c:1"]["files"][0]["preview_path"], str(target))
        self.assertEqual(legacy.read_bytes(), b"compact")
        self.assertEqual((preview_run_directory(self.share, self.run_id) / "Custom" / custom.name).read_bytes(), b"custom")
        self.assertEqual((preview_run_directory(self.share, self.run_id) / "Return" / collage.name).read_bytes(), b"return")
        self.assertEqual(migrate_run(self.repository, self.run_id, self.share, apply=True)["previews"], 0)

    def test_custom_preview_prefers_share_and_falls_back_to_legacy(self):
        legacy = self.input / "Previews" / "Custom" / "1_return-preview.png"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"old")
        self.assertEqual(custom_return_preview_path("1", input_path=self.input, preview_root=self.share, run_id=self.run_id), legacy)
        current = preview_run_directory(self.share, self.run_id) / "Custom" / legacy.name
        current.parent.mkdir(parents=True)
        current.write_bytes(b"new")
        self.assertEqual(custom_return_preview_path("1", input_path=self.input, preview_root=self.share, run_id=self.run_id), current)

    def test_migration_skips_custom_files_shared_by_multiple_runs(self):
        custom = self.input / "Previews" / "Custom" / "1_return-preview.png"
        custom.parent.mkdir(parents=True)
        custom.write_bytes(b"ambiguous")
        for run_id in (self.run_id, uuid4().hex):
            self.repository.create_run({
                "id": run_id, "status": "completed", "stage": "completed", "progress": 100,
                "options": {"input_path": str(self.input), "direction": "digital"},
                "orders": {"c:1": {"order_id": "1", "customer_id": "c", "status": "passed", "files": []}},
            })
        ambiguous = ambiguous_custom_keys(self.repository)
        self.assertIn((str(self.input.resolve()), "1"), ambiguous)
        result = migrate_run(self.repository, self.run_id, self.share, apply=True,
                             ambiguous_custom_keys=ambiguous)
        self.assertEqual(result["custom"], 0)
        self.assertEqual(result["ambiguous"], 1)
        self.assertFalse(self.share.exists())

    def test_lifecycle_keeps_share_preview_outside_source_folder(self):
        source = self.input / "art-face.jpg"
        source.write_bytes(b"art")
        pdf = self.input / "PDF" / "art.pdf"
        pdf.parent.mkdir()
        pdf.write_bytes(b"pdf")
        preview = preview_order_directory(self.share, self.run_id, "c:1") / "art-face_preview.png"
        preview.parent.mkdir(parents=True)
        preview.write_bytes(b"compact")
        order = {"order_id": "1", "files": [{"path": str(source)}], "pdf_path": str(pdf), "preview_paths": [str(preview)]}
        transition = FileLifecycle(self.input).accept_for_print(order)
        target = Path(transition.preview_paths[0])
        self.assertEqual(target, preview.parent / "Processed" / preview.name)
        self.assertTrue(target.is_file())
        self.assertFalse(preview.exists())


if __name__ == "__main__":
    unittest.main()
