import shutil
import unittest
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory

from services.pitstop import (
    PitStopExecutionStatus,
    PitStopProfile,
    PitStopProfileCatalog,
    PitStopService,
    PitStopServiceSettings,
    TransportResult,
)


FIXTURES = Path(__file__).parent / "fixtures" / "pitstop"


class ReportWritingTransport:
    def __init__(
        self,
        local_report_root: Path,
        *,
        returncode: int = 0,
        input_pdf: Path | None = None,
    ):
        self.local_report_root = local_report_root
        self.returncode = returncode
        self.input_pdf = input_pdf
        self.commands = []

    def execute(self, remote_command: str, *, timeout_seconds: float):
        self.commands.append((remote_command, timeout_seconds))
        if self.returncode == 0:
            job_dir = next(self.local_report_root.iterdir())
            shutil.copy(FIXTURES / "warning_report.json", job_dir / "report.json")
            (job_dir / "report.xml").write_text("<report/>", encoding="utf-8")
            if self.input_pdf is not None:
                self.input_pdf.write_bytes(b"modified")
        return TransportResult(self.returncode, "", "")


class PitStopServiceTests(unittest.TestCase):
    def test_runs_report_only_and_returns_parsed_result(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "PDF" / "order.pdf"
            pdf.parent.mkdir()
            pdf.write_bytes(b"%PDF-1.7 test")
            report_root = root / "output_report" / "pitstop"
            transport = ReportWritingTransport(report_root)
            service = self._service(root, report_root, transport)

            result = service.check_pdf(pdf, profile_id="production_check")

            self.assertEqual(result.status, PitStopExecutionStatus.COMPLETED)
            self.assertEqual(result.report.counts.warnings, 1)
            self.assertIsNone(result.report_json_path)
            self.assertIsNone(result.report_xml_path)
            self.assertEqual(list(report_root.iterdir()), [])
            command, timeout = transport.commands[0]
            self.assertIn("-reportJSON", command)
            self.assertIn("-reportXML", command)
            self.assertNotIn("-output", command)
            self.assertEqual(timeout, 7)

    def test_nonzero_exit_becomes_failed_check_without_exposing_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "order.pdf"
            pdf.write_bytes(b"%PDF")
            report_root = root / "reports"
            service = self._service(
                root, report_root, ReportWritingTransport(report_root, returncode=23)
            )

            result = service.check_pdf(pdf, profile_id="production_check")

        self.assertEqual(result.status, PitStopExecutionStatus.FAILED)
        self.assertIn("кодом 23", result.technical_error)
        self.assertIsNone(result.report)

    def test_detects_unexpected_input_pdf_modification(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "order.pdf"
            pdf.write_bytes(b"%PDF original")
            report_root = root / "reports"
            service = self._service(
                root,
                report_root,
                ReportWritingTransport(report_root, input_pdf=pdf),
            )

            result = service.check_pdf(pdf, profile_id="production_check")

        self.assertEqual(result.status, PitStopExecutionStatus.FAILED)
        self.assertIn("исходный PDF изменился", result.technical_error)

    def test_rejects_non_allowlisted_profile(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "order.pdf"
            pdf.write_bytes(b"%PDF")
            report_root = root / "reports"
            service = self._service(root, report_root, ReportWritingTransport(report_root))

            with self.assertRaisesRegex(KeyError, "не разрешён"):
                service.check_pdf(pdf, profile_id="C:/arbitrary/action.eal")

    @staticmethod
    def _service(root, report_root, transport):
        settings = PitStopServiceSettings(
            cli_path=PureWindowsPath("C:/Program Files/Enfocus/PitStopServerCLI.exe"),
            mac_shared_root=root,
            windows_shared_root=PureWindowsPath("C:/Mac/Home/project"),
            report_root=report_root,
            command_timeout_seconds=7,
        )
        catalog = PitStopProfileCatalog(
            [
                PitStopProfile(
                    id="production_check",
                    label="Production check",
                    windows_path=PureWindowsPath("C:/PitStop/Profiles/check.ppp"),
                )
            ]
        )
        return PitStopService(settings=settings, profiles=catalog, transport=transport)


if __name__ == "__main__":
    unittest.main()
