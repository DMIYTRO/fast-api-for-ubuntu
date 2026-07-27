import unittest
from unittest.mock import patch

from core.resampler import resample_image


class ResamplerCommandTests(unittest.TestCase):
    @patch("core.resampler.run_command")
    @patch("core.resampler.shutil.which", return_value="/usr/bin/magick")
    def test_uses_cover_crop_and_rotation_without_stretch(self, _which, run):
        resample_image("input.tif", "/tmp/output.tif", 94.0, 54.0, rotation_degrees=90)
        command = run.call_args.args[0]
        self.assertIn("-rotate", command)
        self.assertIn("90", command)
        self.assertIn("-extent", command)
        self.assertFalse(any(value.endswith("!") for value in command))
        self.assertNotIn("-colorspace", command)


if __name__ == "__main__":
    unittest.main()
