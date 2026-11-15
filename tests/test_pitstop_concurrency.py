"""Concurrency limit, exclusive retries and serial fallback of PitStopService.

All tests use a fake transport; nothing contacts a PitStop host.  Time is
injected (clock and sleep), so the suite does not wait.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from unittest.mock import patch

from server.settings import Settings
from services.pitstop import (
    FailureKind,
    GateConfig,
    PitStopExecutionStatus,
    PitStopGate,
    PitStopProfile,
    PitStopProfileCatalog,
    PitStopService,
    PitStopServiceSettings,
    PitStopTimeoutError,
    PitStopTransportError,
    SSHSettings,
    SSHTransport,
    TransportResult,
    shared_gate_for,
)
from services.pitstop import concurrency
from services.pitstop.failures import classify_cli_exit


FIXTURES = Path(__file__).parent / "fixtures" / "pitstop"
WAIT = 10.0  # upper bound for any wait; a deadlock fails after this


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Call:
    def __init__(self, number, pdf_name, job_id, attempt):
        self.number = number
        self.pdf_name = pdf_name
        self.job_id = job_id
        self.attempt = attempt  # 1 for the first call with this PDF
        self.peak = 1  # most calls in flight while this one ran
        self.start_active = 1


class FakeTransport:
    """Records overlap between calls; behaviour is scripted per call.

    ``script(call)`` returns one of:
    ``("ok",)``, ``("result", returncode, stderr)``, ``("raise", exception)``,
    ``("no_report",)`` (exit 0 without a report), ``("bad_json",)``,
    ``("wrong_shape",)``, ``("modify", pdf_path)``.
    """

    def __init__(self, report_root, script=None, *, hold=None, work_seconds=0.0):
        self.report_root = report_root
        self.script = script or (lambda call: ("ok",))
        self.hold = hold
        self.work_seconds = work_seconds
        self._lock = threading.Lock()
        self.active = []
        self.calls = []
        self.max_in_flight = 0

    def execute(self, remote_command, *, timeout_seconds):
        pdf = re.search(r"'-input' '([^']*)'", remote_command).group(1)
        job_id = re.search(r"([0-9a-f]{32})", remote_command).group(1)
        with self._lock:
            name = PureWindowsPath(pdf).name
            attempt = 1 + sum(1 for c in self.calls if c.pdf_name == name)
            call = Call(len(self.calls) + 1, name, job_id, attempt)
            self.calls.append(call)
            self.active.append(call)
            count = len(self.active)
            for running in self.active:
                running.peak = max(running.peak, count)
            call.start_active = count
            self.max_in_flight = max(self.max_in_flight, count)
        try:
            action = self.script(call)
            if self.hold is not None:
                self.hold.wait(WAIT)
            if self.work_seconds:
                time.sleep(self.work_seconds)
            return self._perform(action, call)
        finally:
            with self._lock:
                self.active.remove(call)

    def _perform(self, action, call):
        job_dir = self.report_root / call.job_id
        kind = action[0]
        if kind == "raise":
            raise action[1]
        if kind == "result":
            return TransportResult(action[1], "", action[2])
        if kind == "ok" or kind == "modify":
            shutil.copy(FIXTURES / "warning_report.json", job_dir / "report.json")
            (job_dir / "report.xml").write_text("<report/>", encoding="utf-8")
            if kind == "modify":
                action[1].write_bytes(b"changed")
        elif kind == "bad_json":
            (job_dir / "report.json").write_text('{"preflightReport": ', encoding="utf-8")
        elif kind == "wrong_shape":
            (job_dir / "report.json").write_text("{}", encoding="utf-8")
        elif kind != "no_report":
            raise AssertionError(f"unknown action {action}")
        return TransportResult(0, "", "")

    def calls_for(self, pdf_name):
        return [call for call in self.calls if call.pdf_name == pdf_name]


class PitStopConcurrencyCase(unittest.TestCase):
    def setUp(self):
        self._directory = TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.report_root = self.root / "reports"
        self.sleeps = []

    def make_pdfs(self, count):
        pdfs = []
        for index in range(count):
            pdf = self.root / f"order-{index}.pdf"
            pdf.write_bytes(b"%PDF-1.7 " + str(index).encode())
            pdfs.append(pdf)
        return pdfs

    def make_service(self, transport, *, max_concurrent=4, gate=None, **overrides):
        settings = PitStopServiceSettings(
            cli_path=PureWindowsPath("C:/Program Files/Enfocus/PitStopServerCLI.exe"),
            mac_shared_root=self.root,
            windows_shared_root=PureWindowsPath("C:/Mac/Home/project"),
            report_root=self.report_root,
            command_timeout_seconds=7,
            max_concurrent=max_concurrent,
            **overrides,
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
        return PitStopService(
            settings=settings,
            profiles=catalog,
            transport=transport,
            gate=gate,
            sleep=self.sleeps.append,
        )

    def run_parallel(self, service, pdfs, *, release=None, transport=None):
        """Run check_pdf for every PDF in its own thread and return results."""
        results = [None] * len(pdfs)
        errors = [None] * len(pdfs)

        def worker(index, pdf):
            try:
                results[index] = service.check_pdf(pdf, profile_id="production_check")
            except BaseException as exc:  # noqa: BLE001 - reported by the test
                errors[index] = exc

        threads = [
            threading.Thread(target=worker, args=(index, pdf), daemon=True)
            for index, pdf in enumerate(pdfs)
        ]
        for thread in threads:
            thread.start()
        if release is not None:
            release()
        for thread in threads:
            thread.join(WAIT)
        self.assertFalse(
            any(thread.is_alive() for thread in threads),
            "проверки не завершились: возможна взаимная блокировка",
        )
        return results, errors


class ConcurrencyLimitTests(PitStopConcurrencyCase):
    def _assert_limit(self, limit):
        pdfs = self.make_pdfs(8)
        hold = threading.Event()
        transport = FakeTransport(self.report_root, hold=hold)
        service = self.make_service(transport, max_concurrent=limit)

        def release():
            self.assertTrue(
                wait_until(lambda: transport.max_in_flight >= limit),
                "лимит параллельности не достигнут",
            )
            time.sleep(0.05)  # let any excess check slip through, if it could
            self.assertEqual(len(transport.active), limit)
            hold.set()

        results, errors = self.run_parallel(service, pdfs, release=release)

        self.assertEqual(errors, [None] * 8)
        self.assertTrue(
            all(r.status is PitStopExecutionStatus.COMPLETED for r in results)
        )
        self.assertEqual(transport.max_in_flight, limit)
        self.assertEqual(len(transport.calls), 8)
        return transport

    def test_never_more_than_one_check_at_a_time(self):
        transport = self._assert_limit(1)
        self.assertTrue(all(call.peak == 1 for call in transport.calls))

    def test_never_more_than_two_checks_at_a_time(self):
        self._assert_limit(2)

    def test_never_more_than_four_checks_at_a_time(self):
        self._assert_limit(4)

    def test_services_sharing_a_transport_share_one_limit(self):
        # The application builds a new PitStopService per run, so the limit
        # must not live in the service instance.
        pdfs = self.make_pdfs(6)
        hold = threading.Event()
        transport = FakeTransport(self.report_root, hold=hold)
        first = self.make_service(transport, max_concurrent=2)
        second = self.make_service(transport, max_concurrent=2)
        self.assertIs(first.gate, second.gate)
        results = []

        def run(service, pdf):
            results.append(service.check_pdf(pdf, profile_id="production_check"))

        threads = [
            threading.Thread(
                target=run, args=((first, second)[index % 2], pdf), daemon=True
            )
            for index, pdf in enumerate(pdfs)
        ]
        for thread in threads:
            thread.start()
        self.assertTrue(wait_until(lambda: transport.max_in_flight >= 2))
        time.sleep(0.05)
        hold.set()
        for thread in threads:
            thread.join(WAIT)

        self.assertEqual(len(results), 6)
        self.assertEqual(transport.max_in_flight, 2)

    def test_separate_transports_do_not_share_a_gate(self):
        config = GateConfig(max_concurrent=2)
        one = FakeTransport(self.report_root)
        other = FakeTransport(self.report_root)
        self.assertIs(shared_gate_for(one, config), shared_gate_for(one, config))
        self.assertIsNot(shared_gate_for(one, config), shared_gate_for(other, config))

    def test_concurrent_checks_use_their_own_report_directories(self):
        pdfs = self.make_pdfs(8)
        hold = threading.Event()
        transport = FakeTransport(self.report_root, hold=hold)
        service = self.make_service(transport, max_concurrent=4)

        results, errors = self.run_parallel(
            service,
            pdfs,
            release=lambda: (
                wait_until(lambda: transport.max_in_flight >= 4),
                hold.set(),
            ),
        )

        self.assertEqual(errors, [None] * 8)
        job_ids = [result.job_id for result in results]
        self.assertEqual(len(set(job_ids)), 8)
        self.assertEqual(len({call.job_id for call in transport.calls}), 8)
        # Successful checks leave no report directories behind.
        self.assertEqual(list(self.report_root.iterdir()), [])
        self.assertEqual(
            {result.input_pdf.name for result in results}, {p.name for p in pdfs}
        )


class TransientFailureRetryTests(PitStopConcurrencyCase):
    def test_transient_failure_is_retried_once_and_exclusively(self):
        pdfs = self.make_pdfs(4)

        def script(call):
            if call.pdf_name == "order-0.pdf" and call.attempt == 1:
                return ("result", 1, "transient")
            return ("ok",)

        transport = FakeTransport(self.report_root, script, work_seconds=0.03)
        service = self.make_service(transport, max_concurrent=4)

        with self.assertLogs("services.pitstop", level="WARNING") as logs:
            results, errors = self.run_parallel(service, pdfs)

        self.assertEqual(errors, [None] * 4)
        self.assertTrue(
            all(r.status is PitStopExecutionStatus.COMPLETED for r in results)
        )
        failing = transport.calls_for("order-0.pdf")
        self.assertEqual([call.attempt for call in failing], [1, 2])
        retry = failing[1]
        # Nothing else ran while the retry was in flight, and nothing started.
        self.assertEqual(retry.peak, 1)
        self.assertEqual(retry.start_active, 1)
        # Every other check ran exactly once.
        for name in ("order-1.pdf", "order-2.pdf", "order-3.pdf"):
            self.assertEqual(len(transport.calls_for(name)), 1)
        self.assertEqual(results[0].report.counts.warnings, 1)
        # The pause before the retry was requested (injected, not slept).
        self.assertEqual(self.sleeps, [1.0])
        warning = "\n".join(logs.output)
        self.assertIn("order-0.pdf", warning)
        self.assertIn("transient", warning)  # first failure reason
        self.assertIn("попытк", warning)
        self.assertIn("эксклюзивном", warning)
        self.assertEqual(self.report_root.exists() and list(self.report_root.iterdir()), [])

    def test_retry_waits_for_checks_already_in_flight(self):
        pdfs = self.make_pdfs(3)
        release_slow = threading.Event()
        slow_started = threading.Event()

        def script(call):
            if call.pdf_name == "order-0.pdf" and call.attempt == 1:
                slow_started.wait(WAIT)
                return ("result", 1, "boom")
            return ("ok",)

        class SlowTransport(FakeTransport):
            def execute(self, remote_command, *, timeout_seconds):
                if "order-1.pdf" in remote_command or "order-2.pdf" in remote_command:
                    slow_started.set()
                    release_slow.wait(WAIT)
                return super().execute(remote_command, timeout_seconds=timeout_seconds)

        transport = SlowTransport(self.report_root, script)
        service = self.make_service(transport, max_concurrent=4)

        def release():
            # The retry of order-0 must not start while the others are held.
            self.assertTrue(
                wait_until(lambda: len(transport.calls_for("order-0.pdf")) >= 1)
            )
            time.sleep(0.1)
            self.assertEqual(len(transport.calls_for("order-0.pdf")), 1)
            release_slow.set()

        results, _ = self.run_parallel(service, pdfs, release=release)

        self.assertTrue(
            all(r.status is PitStopExecutionStatus.COMPLETED for r in results)
        )
        self.assertEqual(transport.calls_for("order-0.pdf")[1].peak, 1)

    def test_failure_on_retry_uses_failure_path_with_clear_message(self):
        pdf = self.make_pdfs(1)[0]
        transport = FakeTransport(
            self.report_root, lambda call: ("result", 1, "profile busy")
        )
        service = self.make_service(transport)

        with self.assertLogs("services.pitstop", level="WARNING"):
            result = service.check_pdf(pdf, profile_id="production_check")

        self.assertEqual(result.status, PitStopExecutionStatus.FAILED)
        self.assertEqual(len(transport.calls), 2)  # exactly one retry
        self.assertIn("после 2 попыток", result.technical_error)
        self.assertIn("кодом 1", result.technical_error)
        self.assertIn("profile busy", result.technical_error)
        self.assertLessEqual(len(result.technical_error), 1000)
        self.assertIsNone(result.report)
        self.assertEqual(result.input_pdf, pdf.resolve())

    def test_retries_can_be_disabled(self):
        pdf = self.make_pdfs(1)[0]
        transport = FakeTransport(self.report_root, lambda call: ("result", 1, "x"))
        service = self.make_service(transport, transient_retries=0)

        result = service.check_pdf(pdf, profile_id="production_check")

        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("попыток", result.technical_error)
        self.assertEqual(self.sleeps, [])

    def test_configured_number_of_retries_is_bounded(self):
        pdf = self.make_pdfs(1)[0]
        transport = FakeTransport(self.report_root, lambda call: ("result", 1, "x"))
        service = self.make_service(transport, transient_retries=2)

        with self.assertLogs("services.pitstop", level="WARNING"):
            result = service.check_pdf(pdf, profile_id="production_check")

        self.assertEqual(len(transport.calls), 3)
        self.assertIn("после 3 попыток", result.technical_error)
        with self.assertRaises(ValueError):
            self.make_service(transport, transient_retries=4)

    def test_transient_failure_kinds_are_retried(self):
        scenarios = {
            "exit 1": ("result", 1, "CLI failed"),
            "ssh connection reset": ("result", 255, "kex_exchange_identification: read: Connection reset by peer"),
            "ssh connection refused": ("result", 255, "ssh: connect to host x port 22: Connection refused"),
            "unknown ssh error": ("result", 255, "something unexpected"),
            "missing report after exit 0": ("no_report",),
            "unparsable report after exit 0": ("bad_json",),
            "transport error": ("raise", PitStopTransportError("connection lost")),
        }
        for label, first in scenarios.items():
            with self.subTest(label):
                self.report_root.mkdir(exist_ok=True)
                pdf = self.make_pdfs(1)[0]
                transport = FakeTransport(
                    self.report_root,
                    lambda call, first=first: first if call.attempt == 1 else ("ok",),
                )
                service = self.make_service(transport)

                with self.assertLogs("services.pitstop", level="WARNING"):
                    result = service.check_pdf(pdf, profile_id="production_check")

                self.assertEqual(result.status, PitStopExecutionStatus.COMPLETED)
                self.assertEqual(len(transport.calls), 2)
                # No leftovers of the failed first attempt after a success.
                self.assertEqual(list(self.report_root.iterdir()), [])

    def test_definitive_failures_are_never_retried(self):
        pdf = self.make_pdfs(1)[0]
        scenarios = {
            "other exit code": ("result", 23, "denied by policy"),
            "ssh permission denied": ("result", 255, "operator@host: Permission denied (publickey)."),
            "ssh host key": ("result", 255, "Host key verification failed."),
            "ssh host key changed": ("result", 255, "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!"),
            "ssh unknown host name": ("result", 255, "ssh: Could not resolve hostname pitstop"),
            "ssh client missing": ("raise", PitStopTransportError("нет ssh", kind=FailureKind.DEFINITIVE)),
            "report with wrong shape": ("wrong_shape",),
            "input changed during check": ("modify", pdf),
            "timeout": ("raise", PitStopTimeoutError("PitStop Server не ответил за отведённое время")),
        }
        for label, action in scenarios.items():
            with self.subTest(label):
                pdf.write_bytes(b"%PDF original")
                transport = FakeTransport(self.report_root, lambda call, a=action: a)
                service = self.make_service(transport)

                result = service.check_pdf(pdf, profile_id="production_check")

                self.assertEqual(result.status, PitStopExecutionStatus.FAILED)
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn("попыток", result.technical_error)
        self.assertEqual(self.sleeps, [])

    def test_input_changed_message_is_kept(self):
        pdf = self.make_pdfs(1)[0]
        transport = FakeTransport(self.report_root, lambda call: ("modify", pdf))
        result = self.make_service(transport).check_pdf(pdf, profile_id="production_check")
        self.assertIn("исходный PDF изменился", result.technical_error)

    def test_path_outside_shared_root_is_definitive_and_skips_the_server(self):
        with TemporaryDirectory() as other:
            pdf = Path(other) / "elsewhere.pdf"
            pdf.write_bytes(b"%PDF")
            transport = FakeTransport(self.report_root)
            result = self.make_service(transport).check_pdf(pdf, profile_id="production_check")

        self.assertEqual(result.status, PitStopExecutionStatus.FAILED)
        self.assertEqual(transport.calls, [])

    def test_classification_table(self):
        self.assertIs(classify_cli_exit(1, ""), FailureKind.TRANSIENT)
        self.assertIs(classify_cli_exit(255, "Connection closed by remote host"), FailureKind.TRANSIENT)
        self.assertIs(classify_cli_exit(255, "Permission denied (publickey)"), FailureKind.DEFINITIVE)
        self.assertIs(classify_cli_exit(2, ""), FailureKind.DEFINITIVE)
        self.assertIs(classify_cli_exit(-9, ""), FailureKind.DEFINITIVE)


class PermitSafetyTests(PitStopConcurrencyCase):
    def test_permits_are_not_leaked_after_exceptions(self):
        pdfs = self.make_pdfs(8)
        failing = [True]

        def script(call):
            if failing[0]:
                return ("raise", RuntimeError("unexpected"))
            return ("ok",)

        transport = FakeTransport(self.report_root, script)
        service = self.make_service(transport, max_concurrent=4)

        results, errors = self.run_parallel(service, pdfs)

        self.assertTrue(all(isinstance(error, RuntimeError) for error in errors))
        self.assertEqual(transport.active, [])

        # Full concurrency must still be available.
        failing[0] = False
        hold = threading.Event()
        transport.hold = hold
        results, errors = self.run_parallel(
            service,
            pdfs,
            release=lambda: (
                self.assertTrue(wait_until(lambda: len(transport.active) == 4)),
                hold.set(),
            ),
        )
        self.assertEqual(errors, [None] * 8)
        self.assertEqual(transport.max_in_flight, 4)

    def test_permits_are_not_leaked_after_failed_retries(self):
        pdfs = self.make_pdfs(6)
        transport = FakeTransport(self.report_root, lambda call: ("result", 1, "x"))
        service = self.make_service(transport, max_concurrent=4)

        with self.assertLogs("services.pitstop", level="WARNING"):
            results, errors = self.run_parallel(service, pdfs)

        self.assertEqual(errors, [None] * 6)
        self.assertTrue(all(r.status is PitStopExecutionStatus.FAILED for r in results))

        transport.script = lambda call: ("ok",)
        transport.hold = hold = threading.Event()
        results, errors = self.run_parallel(
            service,
            pdfs,
            release=lambda: (
                self.assertTrue(wait_until(lambda: len(transport.active) == 4)),
                hold.set(),
            ),
        )
        self.assertEqual(errors, [None] * 6)
        self.assertTrue(
            all(r.status is PitStopExecutionStatus.COMPLETED for r in results)
        )

    def test_exception_inside_gate_sections_releases_them(self):
        gate = PitStopGate(GateConfig(max_concurrent=1))
        with self.assertRaises(KeyError):
            with gate.shared():
                raise KeyError("x")
        with self.assertRaises(KeyError):
            with gate.exclusive():
                raise KeyError("x")
        # Both must be acquirable again right away (would block if leaked).
        done = threading.Event()

        def acquire():
            with gate.shared():
                with_exclusive_after = True
            with gate.exclusive():
                pass
            done.set()

        threading.Thread(target=acquire, daemon=True).start()
        self.assertTrue(done.wait(WAIT))

    def test_simultaneous_transient_failures_do_not_deadlock(self):
        pdfs = self.make_pdfs(4)
        barrier = threading.Barrier(4, timeout=WAIT)

        def script(call):
            if call.attempt == 1:
                barrier.wait()  # all four are in flight, then all four fail
                return ("result", 1, "overlap")
            return ("ok",)

        transport = FakeTransport(self.report_root, script)
        service = self.make_service(transport, max_concurrent=4)

        with self.assertLogs("services.pitstop", level="WARNING"):
            results, errors = self.run_parallel(service, pdfs)

        self.assertEqual(errors, [None] * 4)
        self.assertTrue(
            all(r.status is PitStopExecutionStatus.COMPLETED for r in results)
        )
        retries = [call for call in transport.calls if call.attempt == 2]
        self.assertEqual(len(retries), 4)
        # Retries took turns: each ran alone.
        self.assertTrue(all(call.peak == 1 for call in retries))

    def test_exclusive_blocks_new_checks_and_waits_for_running_ones(self):
        gate = PitStopGate(GateConfig(max_concurrent=4))
        events = []
        shared_in = threading.Event()
        release_shared = threading.Event()

        def shared_worker():
            with gate.shared():
                events.append("shared-start")
                shared_in.set()
                release_shared.wait(WAIT)
                events.append("shared-end")

        def exclusive_worker():
            with gate.exclusive():
                events.append("exclusive")

        first = threading.Thread(target=shared_worker, daemon=True)
        first.start()
        self.assertTrue(shared_in.wait(WAIT))
        second = threading.Thread(target=exclusive_worker, daemon=True)
        second.start()
        time.sleep(0.05)
        self.assertEqual(events, ["shared-start"])  # exclusive is waiting
        release_shared.set()
        first.join(WAIT)
        second.join(WAIT)
        self.assertEqual(events, ["shared-start", "shared-end", "exclusive"])


class SerialFallbackTests(PitStopConcurrencyCase):
    def _fail_four_at_once(self, service, transport_script_holder):
        pdfs = self.make_pdfs(4)
        barrier = threading.Barrier(4, timeout=WAIT)

        def script(call):
            if call.attempt == 1:
                barrier.wait()
                return ("result", 1, "overlap")
            return ("ok",)

        transport_script_holder.script = script
        return self.run_parallel(service, pdfs)

    def test_switches_to_serial_mode_after_threshold_and_logs_once(self):
        clock = FakeClock()
        gate = PitStopGate(
            GateConfig(
                max_concurrent=4,
                degrade_threshold=3,
                degrade_window_seconds=60,
                degrade_cooldown_seconds=600,
            ),
            clock=clock,
        )
        transport = FakeTransport(self.report_root)
        service = self.make_service(transport, gate=gate)
        self.assertEqual(gate.effective_limit, 4)

        with self.assertLogs("services.pitstop", level="WARNING") as logs:
            results, errors = self._fail_four_at_once(service, transport)

        self.assertEqual(errors, [None] * 4)
        self.assertTrue(
            all(r.status is PitStopExecutionStatus.COMPLETED for r in results)
        )
        self.assertTrue(gate.degraded)
        self.assertEqual(gate.effective_limit, 1)
        switches = [
            line for line in logs.output if "последовательный режим" in line
        ]
        self.assertEqual(len(switches), 1)
        self.assertIn("IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT", switches[0])
        self.assertIn("WARNING", switches[0])

        # Later checks run strictly one at a time.
        later = FakeTransport(self.report_root, work_seconds=0.01)
        later_service = self.make_service(later, gate=gate)
        results, errors = self.run_parallel(later_service, self.make_pdfs(6))
        self.assertEqual(errors, [None] * 6)
        self.assertEqual(later.max_in_flight, 1)

        # After the cool-down, concurrency comes back.
        clock.advance(601)
        self.assertFalse(gate.degraded)
        self.assertEqual(gate.effective_limit, 4)
        hold = threading.Event()
        held = FakeTransport(self.report_root, hold=hold)
        held_service = self.make_service(held, gate=gate)
        results, errors = self.run_parallel(
            held_service,
            self.make_pdfs(6),
            release=lambda: (
                self.assertTrue(wait_until(lambda: len(held.active) == 4)),
                hold.set(),
            ),
        )
        self.assertEqual(held.max_in_flight, 4)

    def test_failures_without_overlap_do_not_degrade(self):
        gate = PitStopGate(GateConfig(max_concurrent=4, degrade_threshold=2))
        transport = FakeTransport(
            self.report_root,
            lambda call: ("result", 1, "x") if call.attempt == 1 else ("ok",),
        )
        service = self.make_service(transport, gate=gate)

        for pdf in self.make_pdfs(5):  # one at a time: no concurrency evidence
            service.check_pdf(pdf, profile_id="production_check")

        self.assertFalse(gate.degraded)

    def test_timeouts_of_overlapping_checks_count_without_retry(self):
        gate = PitStopGate(GateConfig(max_concurrent=4, degrade_threshold=3))
        barrier = threading.Barrier(4, timeout=WAIT)

        def script(call):
            barrier.wait()
            return ("raise", PitStopTimeoutError("hang"))

        transport = FakeTransport(self.report_root, script)
        service = self.make_service(transport, gate=gate)

        with self.assertLogs("services.pitstop", level="WARNING"):
            results, errors = self.run_parallel(service, self.make_pdfs(4))

        self.assertEqual(len(transport.calls), 4)  # no retries
        self.assertTrue(gate.degraded)

    def test_old_failures_outside_the_window_are_forgotten(self):
        clock = FakeClock()
        gate = PitStopGate(
            GateConfig(max_concurrent=4, degrade_threshold=3, degrade_window_seconds=60),
            clock=clock,
        )
        self.assertFalse(gate.note_suspect_failure(overlapped=True, reason="a"))
        self.assertFalse(gate.note_suspect_failure(overlapped=True, reason="b"))
        clock.advance(61)
        self.assertFalse(gate.note_suspect_failure(overlapped=True, reason="c"))
        self.assertFalse(gate.note_suspect_failure(overlapped=True, reason="d"))
        self.assertFalse(gate.degraded)
        with self.assertLogs("services.pitstop", level="WARNING"):
            self.assertTrue(gate.note_suspect_failure(overlapped=True, reason="e"))
        self.assertTrue(gate.degraded)

    def test_serial_configuration_never_degrades(self):
        gate = PitStopGate(GateConfig(max_concurrent=1, degrade_threshold=1))
        self.assertFalse(gate.note_suspect_failure(overlapped=True, reason="x"))
        self.assertFalse(gate.degraded)


class SettingsTests(unittest.TestCase):
    def test_default_is_four(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.pitstop_max_concurrent, 4)
        self.assertEqual(
            Settings.__dataclass_fields__["pitstop_max_concurrent"].default, 4
        )

    def test_value_is_read_from_environment(self):
        for raw, expected in (("1", 1), ("2", 2), ("8", 8), (" 3 ", 3)):
            with self.subTest(raw=raw):
                with patch.dict(
                    os.environ, {"IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT": raw}, clear=True
                ):
                    self.assertEqual(Settings.from_env().pitstop_max_concurrent, expected)

    def test_value_is_clamped_to_the_allowed_range(self):
        for raw, expected in (("0", 1), ("-5", 1), ("9", 8), ("100", 8)):
            with self.subTest(raw=raw):
                with patch.dict(
                    os.environ, {"IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT": raw}, clear=True
                ):
                    self.assertEqual(Settings.from_env().pitstop_max_concurrent, expected)

    def test_blank_value_means_default_and_garbage_is_rejected(self):
        with patch.dict(os.environ, {"IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT": ""}, clear=True):
            self.assertEqual(Settings.from_env().pitstop_max_concurrent, 4)
        with patch.dict(os.environ, {"IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT": "many"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()

    def test_service_settings_validate_range(self):
        base = dict(
            cli_path=PureWindowsPath("C:/PitStopServerCLI.exe"),
            mac_shared_root=Path("/mnt/shared"),
            windows_shared_root=PureWindowsPath("C:/shared"),
            report_root=Path("/tmp/reports"),
        )
        for bad in (0, 9):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PitStopServiceSettings(**base, max_concurrent=bad)
        self.assertEqual(PitStopServiceSettings(**base, max_concurrent=8).max_concurrent, 8)

    def test_unset_service_limit_falls_back_to_the_environment_variable(self):
        base = dict(
            cli_path=PureWindowsPath("C:/PitStopServerCLI.exe"),
            mac_shared_root=Path("/mnt/shared"),
            windows_shared_root=PureWindowsPath("C:/shared"),
            report_root=Path("/tmp/reports"),
        )
        with patch.dict(os.environ, {"IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT": "1"}, clear=True):
            self.assertEqual(PitStopServiceSettings(**base).gate_config().max_concurrent, 1)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(PitStopServiceSettings(**base).gate_config().max_concurrent, 4)

    def test_bounds_match_between_settings_and_service(self):
        self.assertEqual(
            concurrency.MAX_CONCURRENT_DEFAULT,
            Settings.__dataclass_fields__["pitstop_max_concurrent"].default,
        )
        for raw, expected in (("0", concurrency.MAX_CONCURRENT_MIN), ("99", concurrency.MAX_CONCURRENT_MAX)):
            with patch.dict(os.environ, {"IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT": raw}, clear=True):
                self.assertEqual(Settings.from_env().pitstop_max_concurrent, expected)
                self.assertEqual(concurrency.max_concurrent_from_env(), expected)


class SSHTransportErrorKindTests(unittest.TestCase):
    @patch("services.pitstop.transport.subprocess.run")
    def test_timeout_has_its_own_kind(self, run):
        run.side_effect = subprocess.TimeoutExpired(["ssh"], 2)
        transport = SSHTransport(SSHSettings("host", "user", Path("/known_hosts")))
        with self.assertRaises(PitStopTimeoutError) as caught:
            transport.execute("command", timeout_seconds=2)
        self.assertIs(caught.exception.kind, FailureKind.TIMEOUT)

    @patch("services.pitstop.transport.subprocess.run")
    def test_missing_ssh_client_is_definitive(self, run):
        run.side_effect = FileNotFoundError("ssh")
        transport = SSHTransport(SSHSettings("host", "user", Path("/known_hosts")))
        with self.assertRaises(PitStopTransportError) as caught:
            transport.execute("command", timeout_seconds=2)
        self.assertIs(caught.exception.kind, FailureKind.DEFINITIVE)


if __name__ == "__main__":
    unittest.main()
