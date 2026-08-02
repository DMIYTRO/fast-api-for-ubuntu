import unittest
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory

from services.pitstop import SharedPathError, mac_shared_path_to_windows


class PitStopPathTests(unittest.TestCase):
    def test_maps_only_relative_part_into_windows_share(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "PDF" / "Заказ 1.pdf"

            mapped = mac_shared_path_to_windows(
                path,
                mac_shared_root=root,
                windows_shared_root=PureWindowsPath("C:/Mac/Home/project"),
            )

        self.assertEqual(mapped, PureWindowsPath("C:/Mac/Home/project/PDF/Заказ 1.pdf"))

    def test_rejects_path_outside_share(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(SharedPathError, "разрешённой"):
                mac_shared_path_to_windows(
                    root.parent / "secret.pdf",
                    mac_shared_root=root,
                    windows_shared_root=PureWindowsPath("C:/Mac/Home/project"),
                )


if __name__ == "__main__":
    unittest.main()
