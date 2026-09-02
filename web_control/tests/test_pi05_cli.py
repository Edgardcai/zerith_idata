from __future__ import annotations

import argparse
import os
import time
import unittest
from unittest import mock

from control.web_control import pi05_cli as cli


class FakeWebApi:
    def __init__(
        self,
        *,
        start_error: BaseException | None = None,
        dry_run_error: BaseException | None = None,
        takeover_error: BaseException | None = None,
        dry_run_delay: float = 0.0,
        lease_seconds: float = 0.12,
        phase: str = "running",
    ) -> None:
        self.start_error = start_error
        self.dry_run_error = dry_run_error
        self.takeover_error = takeover_error
        self.dry_run_delay = dry_run_delay
        self.lease_seconds = lease_seconds
        self.phase = phase
        self.calls: list[tuple[str, str, dict | None, str | None]] = []

    def request(self, method, path, body=None, *, lease=None):
        self.calls.append((method, path, body, lease))
        if path == "/api/pi05/start":
            if self.start_error is not None:
                raise self.start_error
            return {"phase": "running"}
        if path == "/api/heartbeat":
            return {"ok": True}
        if path == "/api/pi05/status":
            return {"phase": self.phase, "executed_steps": 0}
        if path == "/api/pi05/stop":
            return {"phase": "idle"}
        if path == "/api/takeover":
            if body and body.get("enabled"):
                if self.takeover_error is not None:
                    raise self.takeover_error
                return {
                    "lease_id": "auto-lease",
                    "lease_seconds": self.lease_seconds,
                }
            return {"takeover": False}
        if path == "/api/pi05/reconnect":
            return {"health": "OK"}
        if path == "/api/pi05/disconnect":
            return {"phase": "idle"}
        if path == "/api/pi05/dry-run":
            if self.dry_run_delay:
                time.sleep(self.dry_run_delay)
            if self.dry_run_error is not None:
                raise self.dry_run_error
            return {"phase": "dry_run_ready", "chunk_length": 50}
        raise AssertionError(f"unexpected request: {method} {path}")


def run_args() -> argparse.Namespace:
    return argparse.Namespace(
        confirm_motion=True,
        lease="lease",
        auto_takeover=False,
        prompt="把物体放进盒子",
        prompt_file=None,
        steps_per_chunk=30,
        control_rate_hz=30.0,
        joint_speed_deg_s=30.0,
        policy_host="192.168.1.154",
        policy_port=9973,
        prepare=False,
        web_url="http://127.0.0.1:8080",
        token=None,
        timeout=1.0,
    )


class Pi05CliSafetyTests(unittest.TestCase):
    def test_missing_lease_without_explicit_auto_takeover_is_rejected(self) -> None:
        api = FakeWebApi()
        dry_args = run_args()
        dry_args.lease = None
        run = run_args()
        run.lease = None
        with mock.patch.dict(os.environ, {"H1_CONTROL_LEASE": ""}), mock.patch.object(
            cli,
            "_web_api",
            return_value=api,
        ):
            with self.assertRaisesRegex(cli.CliError, "--auto-takeover"):
                cli._dry_run(dry_args)
            with self.assertRaisesRegex(cli.CliError, "--auto-takeover"):
                cli._run(run)
        self.assertEqual(api.calls, [])

    def test_auto_dry_run_heartbeats_then_releases_without_init(self) -> None:
        api = FakeWebApi(dry_run_delay=0.1, lease_seconds=0.09)
        args = run_args()
        args.lease = None
        args.auto_takeover = True
        with mock.patch.dict(os.environ, {"H1_CONTROL_LEASE": ""}), mock.patch.object(
            cli,
            "_web_api",
            return_value=api,
        ), mock.patch.object(cli, "_print") as output:
            self.assertEqual(cli._dry_run(args), 0)

        paths = [path for _method, path, _body, _lease in api.calls]
        self.assertEqual(paths[0], "/api/takeover")
        self.assertGreaterEqual(paths.count("/api/heartbeat"), 2)
        self.assertIn("/api/pi05/dry-run", paths)
        self.assertEqual(paths[-1], "/api/takeover")
        self.assertTrue(api.calls[0][2]["enabled"])
        self.assertTrue(
            api.calls[0][2]["client_id"].startswith("inference-cli-")
        )
        self.assertEqual(api.calls[-1][2], {"enabled": False})
        self.assertEqual(api.calls[-1][3], "auto-lease")
        self.assertFalse(any("/api/actions/" in path for path in paths))
        printed = output.call_args.args[0]
        self.assertEqual(printed["auto_takeover_note"], cli.AUTO_DRY_RUN_NOTE)

    def test_auto_dry_run_error_stops_before_releasing(self) -> None:
        api = FakeWebApi(dry_run_error=cli.CliError("dry-run failed"))
        args = run_args()
        args.lease = None
        args.auto_takeover = True
        with mock.patch.dict(os.environ, {"H1_CONTROL_LEASE": ""}), mock.patch.object(
            cli,
            "_web_api",
            return_value=api,
        ), mock.patch.object(cli, "_print"):
            with self.assertRaisesRegex(cli.CliError, "dry-run failed"):
                cli._dry_run(args)

        paths = [path for _method, path, _body, _lease in api.calls]
        self.assertLess(paths.index("/api/pi05/stop"), len(paths) - 1)
        self.assertEqual(paths[-1], "/api/takeover")
        self.assertEqual(api.calls[-1][2], {"enabled": False})

    def test_auto_run_prepare_keeps_one_lease_through_start(self) -> None:
        api = FakeWebApi(phase="idle")
        args = run_args()
        args.lease = None
        args.auto_takeover = True
        args.prepare = True
        args.policy_host = "192.168.1.155"
        args.policy_port = 9988
        with mock.patch.dict(os.environ, {"H1_CONTROL_LEASE": ""}), mock.patch.object(
            cli,
            "_web_api",
            return_value=api,
        ), mock.patch.object(cli, "_print"):
            self.assertEqual(cli._run(args), 0)

        paths = [path for _method, path, _body, _lease in api.calls]
        acquire_index = paths.index("/api/takeover")
        reconnect_index = paths.index("/api/pi05/reconnect")
        dry_run_index = paths.index("/api/pi05/dry-run")
        start_index = paths.index("/api/pi05/start")
        release_index = len(paths) - 1
        self.assertLess(acquire_index, reconnect_index)
        self.assertLess(reconnect_index, dry_run_index)
        self.assertLess(dry_run_index, start_index)
        self.assertLess(start_index, release_index)
        self.assertEqual(paths[release_index], "/api/takeover")
        self.assertEqual(
            api.calls[reconnect_index][2],
            {"host": "192.168.1.155", "port": 9988},
        )
        for index, (method, path, _body, lease) in enumerate(api.calls):
            if path in ("/api/heartbeat", "/api/pi05/dry-run", "/api/pi05/start"):
                self.assertEqual(lease, "auto-lease", (index, method, path))
        self.assertEqual(api.calls[release_index][3], "auto-lease")
        self.assertFalse(any("/api/actions/" in path for path in paths))

    def test_auto_takeover_conflict_does_not_stop_the_other_owner(self) -> None:
        api = FakeWebApi(
            takeover_error=cli.CliError("web API HTTP 409: existing owner")
        )
        args = run_args()
        args.lease = None
        args.auto_takeover = True
        args.prepare = True
        with mock.patch.dict(os.environ, {"H1_CONTROL_LEASE": ""}), mock.patch.object(
            cli,
            "_web_api",
            return_value=api,
        ):
            with self.assertRaisesRegex(cli.CliError, "existing owner"):
                cli._run(args)

        self.assertEqual(len(api.calls), 1)
        self.assertEqual(api.calls[0][1], "/api/takeover")
        self.assertTrue(api.calls[0][2]["enabled"])

    def test_auto_run_ctrl_c_stops_before_releasing(self) -> None:
        api = FakeWebApi(start_error=KeyboardInterrupt())
        args = run_args()
        args.lease = None
        args.auto_takeover = True
        with mock.patch.dict(os.environ, {"H1_CONTROL_LEASE": ""}), mock.patch.object(
            cli,
            "_web_api",
            return_value=api,
        ), mock.patch.object(cli, "_print"), mock.patch.object(cli.sys, "stderr"):
            self.assertEqual(cli._run(args), 130)

        paths = [path for _method, path, _body, _lease in api.calls]
        self.assertLess(paths.index("/api/pi05/stop"), len(paths) - 1)
        self.assertEqual(paths[-1], "/api/takeover")
        self.assertEqual(api.calls[-1][2], {"enabled": False})

    def test_reconnect_and_disconnect_only_use_web_api(self) -> None:
        api = FakeWebApi()
        reconnect_args = argparse.Namespace(
            policy_host="192.168.1.155",
            policy_port=9988,
        )
        with mock.patch.object(cli, "_web_api", return_value=api), mock.patch.object(
            cli, "_print"
        ):
            self.assertEqual(cli._reconnect(reconnect_args), 0)
            self.assertEqual(cli._disconnect(argparse.Namespace()), 0)

        self.assertEqual(
            api.calls,
            [
                (
                    "POST",
                    "/api/pi05/reconnect",
                    {"host": "192.168.1.155", "port": 9988},
                    None,
                ),
                ("POST", "/api/pi05/disconnect", {}, None),
            ],
        )

    def test_run_defaults_and_endpoint_aliases(self) -> None:
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "run",
                "--prompt",
                "测试",
                "--lease",
                "lease",
                "--confirm-motion",
            ]
        )
        self.assertEqual(args.steps_per_chunk, 30)
        self.assertEqual(args.control_rate_hz, 30.0)
        self.assertEqual(args.joint_speed_deg_s, 30.0)
        self.assertFalse(args.auto_takeover)
        self.assertFalse(args.prepare)
        self.assertEqual(args.policy_host, "192.168.1.154")
        self.assertEqual(args.policy_port, 9973)

        endpoint = parser.parse_args(
            [
                "reconnect",
                "--policy-host",
                "192.168.1.155",
                "--policy-port",
                "9988",
            ]
        )
        self.assertEqual(endpoint.policy_host, "192.168.1.155")
        self.assertEqual(endpoint.policy_port, 9988)

    def test_run_passes_per_chunk_count_and_rate_to_8080(self) -> None:
        api = FakeWebApi(phase="idle")
        args = run_args()
        args.steps_per_chunk = 30
        args.control_rate_hz = 25.0
        args.joint_speed_deg_s = 20.0
        with mock.patch.object(cli, "_web_api", return_value=api), mock.patch.object(
            cli, "_print"
        ):
            self.assertEqual(cli._run(args), 0)

        self.assertEqual(
            api.calls[0],
            (
                "POST",
                "/api/pi05/start",
                {
                    "prompt": "把物体放进盒子",
                    "steps_per_chunk": 30,
                    "control_rate_hz": 25.0,
                    "joint_speed_deg_s": 20.0,
                    "confirmation": cli.REQUIRED_CONFIRMATION,
                },
                "lease",
            ),
        )

    def test_execution_value_ranges_fail_before_handler(self) -> None:
        common = [
            "run",
            "--prompt",
            "测试",
            "--lease",
            "lease",
            "--confirm-motion",
        ]
        invalid_options = (
            ("--control-rate-hz", "0"),
            ("--control-rate-hz", "-1"),
            ("--control-rate-hz", "nan"),
            ("--control-rate-hz", "inf"),
            ("--joint-speed-deg-s", "0"),
            ("--joint-speed-deg-s", "-1"),
            ("--joint-speed-deg-s", "nan"),
            ("--joint-speed-deg-s", "inf"),
            ("--steps-per-chunk", "0"),
            ("--steps-per-chunk", "51"),
        )
        with mock.patch.object(cli, "_run") as run:
            for option, value in invalid_options:
                with self.subTest(option=option, value=value):
                    with self.assertRaises(SystemExit):
                        cli.main([*common, option, value])
            run.assert_not_called()

    def test_uncertain_start_response_requests_best_effort_pi05_stop(self) -> None:
        api = FakeWebApi(start_error=cli.CliError("start response was lost"))
        with mock.patch.object(cli, "_web_api", return_value=api), mock.patch.object(
            cli, "_print"
        ):
            with self.assertRaisesRegex(cli.CliError, "start response was lost"):
                cli._run(run_args())

        paths = [path for _method, path, _body, _lease in api.calls]
        self.assertEqual(paths[0], "/api/pi05/start")
        self.assertIn("/api/pi05/stop", paths)

    def test_unknown_running_phase_stops_without_another_heartbeat(self) -> None:
        api = FakeWebApi(phase="unexpected-phase")
        with mock.patch.object(cli, "_web_api", return_value=api), mock.patch.object(
            cli, "_print"
        ), mock.patch.object(
            cli.time,
            "sleep",
            side_effect=AssertionError("unknown phase was not handled fail-closed"),
        ) as sleep:
            try:
                result = cli._run(run_args())
            except cli.CliError:
                result = 2

        self.assertEqual(result, 2)
        sleep.assert_not_called()
        paths = [path for _method, path, _body, _lease in api.calls]
        self.assertEqual(
            paths[:3],
            ["/api/pi05/start", "/api/heartbeat", "/api/pi05/status"],
        )
        self.assertEqual(paths[-1], "/api/pi05/stop")
        self.assertEqual(paths.count("/api/heartbeat"), 1)


if __name__ == "__main__":
    unittest.main()
