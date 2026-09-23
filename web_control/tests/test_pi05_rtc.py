"""Hardware-free RTC protocol, timing and integration regressions."""

import copy
import json
import threading
import time
import unittest
import numpy as np
from websockets.sync.server import serve
from control.web_control.pi05_protocol import (
    ACTION_ORDER,
    STATE_ORDER,
    ZerithJsonPolicyClient,
    ProtocolValidationError,
)
from control.web_control.pi05_rtc import (
    RtcConfig,
    RtcBuffer,
    DelayEstimator,
    envelope,
    validate_rtc_metadata,
    validate_response,
)
from control.web_control.pi05_rtc_runtime import OneWorker
from control.web_control.pi05_executor import Pi05Executor, REQUIRED_CONFIRMATION
from control.web_control.tests.test_pi05_executor import FakeRobot, FakeCamera


def metadata():
    return dict(
        rtc_supported=True,
        rtc_protocol_version=1,
        action_horizon=50,
        wire_state_dim=23,
        wire_action_dim=23,
        model_action_dim=32,
        rtc_history_trained_dim=17,
        rtc_padding_start=17,
        input_gripper_binary=True,
        output_gripper_binary=True,
        rtc_action_space_id="test-17-v1",
        state_order=list(STATE_ORDER),
        action_order=list(ACTION_ORDER),
    )


def actions():
    a = np.zeros((50, 23))
    a[:, 0] = np.arange(50) * 0.001
    a[:, 7] = 1.5
    a[:, 16] = 0.2
    return a


def raw_response(ids):
    return dict(
        type="action_chunk",
        session_id=ids["session_id"],
        request_id=ids["request_id"],
        episode_epoch=ids["rtc"]["episode_epoch"],
        phase_epoch=ids["rtc"]["phase_epoch"],
        actions_model=np.zeros((50, 32)).tolist(),
        rtc=dict(
            guided=(
                "previous_actions" in ids["rtc"] or "previous_chunk_model" in ids["rtc"]
            ),
            action_time_origin="request_observation",
            client_must_discard_elapsed_prefix=True,
        ),
    )


class RtcFakePolicy:
    def __init__(self):
        self.usable = True
        self.requests = []
        self.active = 0
        self.max_active = 0
        self.delay = 0.01
        self.owner_threads = set()
        self.closed = threading.Event()
        self.bad = None
        self.meta = metadata()
        self.block_after = None

    def metadata(self, **kw):
        self.owner_threads.add(threading.get_ident())
        return self.meta.copy()

    def infer_rtc(self, state, images, prompt, *, identifiers, metadata, **kw):
        self.owner_threads.add(threading.get_ident())
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.requests.append(copy.deepcopy(identifiers))
            if self.block_after and len(self.requests) >= self.block_after:
                self.closed.wait(3)
                raise TimeoutError("fake timed out")
            time.sleep(self.delay)
            raw = raw_response(identifiers)
            if self.bad == "nan":
                raw["actions_model"][0][0] = float("nan")
            if self.bad == "stale":
                raw["session_id"] = "previous-session"
            return actions(), validate_response(raw, identifiers, metadata), raw
        finally:
            self.active -= 1

    def close(self):
        self.usable = False
        self.closed.set()


class RtcContractTests(unittest.TestCase):
    def test_metadata_disabled_and_wrong_schema(self):
        for key, val in [
            ("rtc_supported", False),
            ("wire_action_dim", 18),
            ("rtc_protocol_version", 2),
            ("action_order", []),
        ]:
            with self.subTest(key=key), self.assertRaises(ProtocolValidationError):
                validate_rtc_metadata({**metadata(), key: val})

    def test_bootstrap_and_exact_original_cursor_suffix(self):
        c = RtcConfig()
        r = envelope(c, "s", 0)
        self.assertEqual(r["rtc"]["executed_steps"], 0)
        self.assertNotIn("previous_actions", r["rtc"])
        self.assertNotIn("previous_chunk_model", r["rtc"])
        model = np.zeros((50, 32))
        model[:, 0] = np.arange(50)
        for s in (15, 18):
            r = envelope(c, "s", s, s=s, previous_chunk_model=model[s:])
            self.assertEqual(
                (r["rtc"]["executed_steps"], r["rtc"]["execution_horizon"]), (s, s)
            )
            self.assertEqual(np.shape(r["rtc"]["previous_chunk_model"]), (50 - s, 32))
            self.assertEqual(r["rtc"]["previous_chunk_model"][0][0], s)
        with self.assertRaises(ProtocolValidationError):
            envelope(c, "s", 1, s=15, previous_chunk_model=model)

    def test_handoff_k6_plus_nine_steps(self):
        b = RtcBuffer()
        a = actions()
        m = np.zeros((50, 32))
        self.assertEqual(
            b.handoff(
                a, m, observation_tick=100, handoff_tick=106, expected_generation=0
            ),
            6,
        )
        self.assertEqual(b.actions.shape, (50, 23))
        self.assertEqual(b.actions[b.cursor, 0], a[6, 0])
        for _ in range(9):
            b.cursor += 1
        self.assertEqual(b.cursor, 15)
        self.assertEqual(b.actions_model[b.cursor :].shape, (35, 32))

    def test_late_control_handoff_no_double_slice(self):
        b = RtcBuffer()
        b.handoff(
            actions(),
            np.zeros((50, 32)),
            observation_tick=100,
            handoff_tick=109,
            expected_generation=0,
        )
        self.assertEqual(b.cursor, 9)
        self.assertAlmostEqual(b.actions[b.cursor, 0], 0.009)

    def test_stale_generation_exhaustion_and_missed_clock(self):
        b = RtcBuffer()
        with self.assertRaises(ProtocolValidationError):
            b.handoff(
                actions(),
                np.zeros((50, 32)),
                observation_tick=0,
                handoff_tick=50,
                expected_generation=0,
            )
        b.handoff(
            actions(),
            np.zeros((50, 32)),
            observation_tick=0,
            handoff_tick=6,
            expected_generation=0,
        )
        with self.assertRaises(ProtocolValidationError):
            b.handoff(
                actions(),
                np.zeros((50, 32)),
                observation_tick=6,
                handoff_tick=7,
                expected_generation=0,
            )
        b.cursor += 1
        self.assertEqual(b.advance_clock(10), 3)
        self.assertEqual(b.cursor, 10)

    def test_invalid_identifiers_model_and_response_contract(self):
        ids = envelope(RtcConfig(), "session", 3)
        for change in (
            "session",
            "request",
            "epoch",
            "shape",
            "padding",
            "nan",
            "guided",
            "origin",
        ):
            raw = raw_response(ids)
            if change == "session":
                raw["session_id"] = "old"
            elif change == "request":
                raw["request_id"] = 2
            elif change == "epoch":
                raw["phase_epoch"] = 1
            elif change == "shape":
                raw["actions_model"].pop()
            elif change == "padding":
                raw["actions_model"][0][17] = 1
            elif change == "nan":
                raw["actions_model"][0][0] = float("nan")
            elif change == "guided":
                raw["rtc"]["guided"] = True
            else:
                raw["rtc"]["action_time_origin"] = "arrival"
            with self.subTest(change=change), self.assertRaises(
                ProtocolValidationError
            ):
                validate_response(raw, ids, metadata())

    def test_no_delay_clamping(self):
        d = DelayEstimator(RtcConfig())
        d.observe(27)
        self.assertEqual(d.steps, 28)
        with self.assertRaises(ProtocolValidationError):
            RtcConfig().check_schedule(28, d.steps)
        with self.assertRaises(ProtocolValidationError):
            RtcConfig.parse({"max_guidance_weight": 6})
        with self.assertRaises(ProtocolValidationError):
            envelope(RtcConfig(), "s", 0, s=1)

    def test_one_worker_bounded_slot(self):
        w = OneWorker()
        gate = threading.Event()
        try:
            w.submit(lambda: gate.wait(1))
            with self.assertRaises(RuntimeError):
                w.submit(lambda: None)
            gate.set()
            self.assertTrue(w.done.wait(1))
            w.take()
            self.assertFalse(w.pending)
        finally:
            gate.set()
            w.close()


class RtcExecutionTests(unittest.TestCase):
    def setUp(self):
        self.robot = FakeRobot()
        self.camera = FakeCamera()
        self.policy = RtcFakePolicy()
        self.ex = Pi05Executor(
            self.robot, self.camera, policy_factory=lambda h, p: self.policy
        )
        self.ex.probe()

    def tearDown(self):
        self.ex.close()

    def dry(self, **kw):
        return self.ex.dry_run(
            "Grasp Sprite with the left hand.", "lease", scheduling_mode="rtc", **kw
        )

    def test_warmup_dry_run_zero_setters_and_physical_history(self):
        result = self.dry()
        self.assertEqual(self.robot.steps, [])
        self.assertEqual(self.robot.begin_calls, 0)
        self.assertEqual(self.robot.hold_calls, [])
        self.assertEqual(self.policy.max_active, 1)
        self.assertEqual(len(self.policy.owner_threads), 1)
        req = self.policy.requests
        self.assertEqual(req[0]["rtc"]["executed_steps"], 0)
        self.assertNotEqual(req[1]["session_id"], req[2]["session_id"])
        self.assertEqual(req[2]["request_id"], 0)
        guided = req[3]["rtc"]
        s = guided["executed_steps"]
        self.assertEqual(guided["execution_horizon"], s)
        self.assertEqual(np.shape(guided["previous_actions"]), (50 - s, 23))
        self.assertNotIn("previous_chunk_model", guided)
        self.assertTrue(
            np.all(
                np.asarray(guided["previous_actions"])[:, 16] == self.robot.state[16]
            )
        )
        self.assertTrue(set(np.asarray(guided["previous_actions"])[:, 7]) <= {0, 1.5})
        self.assertEqual(result["scheduling_mode"], "rtc")
        self.assertTrue(self.ex.status()["dry_run_ok"])

    def test_slow_network_control_continues_and_stop_drains(self):
        self.dry()
        self.policy.delay = 0.13
        self.ex.start(
            "Grasp Sprite with the left hand.",
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            scheduling_mode="rtc",
        )
        end = time.monotonic() + 3
        while (
            len(self.robot.steps) < 32
            and time.monotonic() < end
            and self.ex.status()["phase"] != "fault"
        ):
            time.sleep(0.01)
        self.assertGreaterEqual(len(self.robot.steps), 32, self.ex.status())
        self.assertEqual(self.policy.max_active, 1)
        self.ex.stop()
        n = len(self.robot.steps)
        time.sleep(0.05)
        self.assertEqual(len(self.robot.steps), n)

    def test_stale_camera_latches_before_setters(self):
        self.camera.stale = True
        with self.assertRaises(ProtocolValidationError):
            self.dry()
        self.assertEqual(self.robot.steps, [])

    def test_disabled_rtc_no_silent_sync(self):
        self.policy.meta["rtc_supported"] = False
        with self.assertRaises(ProtocolValidationError):
            self.dry()
        self.assertEqual(len(self.policy.requests), 0)
        self.assertEqual(self.ex.status()["phase"], "fault")

    def test_invalid_model_no_motor_calls(self):
        self.policy.bad = "nan"
        with self.assertRaises(ProtocolValidationError):
            self.dry()
        self.assertEqual(self.robot.steps, [])

    def test_schedule_change_requires_new_dry_run(self):
        self.dry()
        with self.assertRaisesRegex(Exception, "scheduling mode"):
            self.ex.start(
                "Grasp Sprite with the left hand.",
                "lease",
                confirmation=REQUIRED_CONFIRMATION,
                scheduling_mode="sync",
            )
        self.assertEqual(self.robot.begin_calls, 0)

    def test_timeout_holds_not_replays(self):
        self.dry()
        self.policy.block_after = len(self.policy.requests) + 2
        self.ex.inference_timeout_s = 0.12
        self.ex.start(
            "Grasp Sprite with the left hand.",
            "lease",
            confirmation=REQUIRED_CONFIRMATION,
            scheduling_mode="rtc",
        )
        end = time.monotonic() + 3
        while self.ex.status()["phase"] != "fault" and time.monotonic() < end:
            time.sleep(0.01)
        self.assertEqual(self.ex.status()["phase"], "fault")
        self.assertLessEqual(len(self.robot.steps), 50)
        self.ex.stop()
        self.assertTrue(self.robot.hold_calls)


class RtcJsonTests(unittest.TestCase):
    def test_json_websocket_metadata_bootstrap_guided(self):
        received = []

        def handler(ws):
            for message in ws:
                data = json.loads(message)
                received.append(data)
                if data["type"] == "metadata":
                    ws.send(json.dumps({"type": "metadata", "metadata": metadata()}))
                    continue
                raw = raw_response(data)

                def structured(row):
                    return dict(
                        left={
                            **{f"joint{i+1}": float(row[i]) for i in range(7)},
                            "gripper": float(row[7]),
                        },
                        right={
                            **{f"joint{i+1}": float(row[i + 8]) for i in range(7)},
                            "gripper": float(row[15]),
                        },
                        lift={"height": float(row[16])},
                        waist={"pitch": float(row[17]), "yaw": float(row[18])},
                        head={"yaw": float(row[19]), "pitch": float(row[20])},
                        speed={"linear": float(row[21]), "angular": float(row[22])},
                    )

                raw["actions"] = [structured(a) for a in actions()]
                ws.send(json.dumps(raw))

        server = serve(handler, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with ZerithJsonPolicyClient(
                "127.0.0.1", server.socket.getsockname()[1]
            ) as client:
                md = validate_rtc_metadata(client.metadata())
                images = {
                    n: np.zeros((8, 8, 3), dtype=np.uint8)
                    for n in ("cam_high", "cam_left_wrist", "cam_right_wrist")
                }
                a, m, _ = client.infer_rtc(
                    np.zeros(23),
                    images,
                    "test",
                    identifiers=envelope(RtcConfig(), "s", 0),
                    metadata=md,
                )
                client.infer_rtc(
                    np.zeros(23),
                    images,
                    "test",
                    identifiers=envelope(
                        RtcConfig(), "s", 1, s=15, previous_chunk_model=m[15:]
                    ),
                    metadata=md,
                )
                self.assertEqual(a.shape, (50, 23))
                self.assertEqual(len(received[2]["rtc"]["previous_chunk_model"]), 35)
                self.assertNotIn("previous_actions", received[1])
                self.assertNotIn("episode_epoch", received[1])
        finally:
            server.shutdown()
            thread.join(2)


class RtcServerVariantTests(unittest.TestCase):
    def test_9979_16d_metadata_aliases_and_padding(self):
        md = metadata()
        md.update(
            rtc_history_trained_dim=16,
            rtc_padding_start=16,
            policy_dim=16,
            model_policy_dim=16,
        )
        aliases = {
            "lift.height": "lift.target_height",
            "speed.linear": "base.linear_velocity",
            "speed.angular": "base.angular_velocity",
        }
        md["action_order"] = [aliases.get(k, k) for k in md["action_order"]]
        md["state_order"] = [
            aliases.get(k, k) if k != "lift.height" else k for k in md["state_order"]
        ]
        self.assertEqual(validate_rtc_metadata(md)["rtc_padding_start"], 16)
        ids = envelope(RtcConfig(), "s", 0)
        raw = raw_response(ids)
        raw["actions_model"][0][16] = 0.2
        with self.assertRaises(ProtocolValidationError):
            validate_response(raw, ids, md)


class RtcClockBoundaryTests(unittest.TestCase):
    def test_runner_recomputes_k_at_control_handoff(self):
        from control.web_control.pi05_rtc_runtime import RtcRunner, Reply
        from control.web_control.pi05_executor import (
            _normalise_inference_plan,
            _Observation,
        )

        now = [106 / 30]
        ex = Pi05Executor(FakeRobot(), FakeCamera(), clock=lambda: now[0])
        plan = _normalise_inference_plan("test")
        runner = RtcRunner(ex, plan, "lease", None, np.zeros(23), dry=True)
        runner.base = 0
        runner.metadata = metadata()
        ids = envelope(RtcConfig(), runner.session, 0)
        obs = _Observation(np.zeros(23), {}, {"cam_high": 1}, 100 / 30)
        reply = Reply(
            actions(),
            np.zeros((50, 32)),
            raw_response(ids),
            obs,
            100 / 30,
            100,
            0,
            0,
            6,
            ids,
            103 / 30,
        )
        try:
            # Response was ready at tick 103; control is only able to admit it
            # at tick 106. The actual control clock must override the old tick.
            self.assertEqual(runner.activate(reply, 103), 106)
            self.assertEqual(runner.buffer.cursor, 6)
            self.assertEqual(runner.handoffs[-1]["actual_k"], 6)
            runner.phase_epoch += 1
            with self.assertRaises(ProtocolValidationError):
                runner.activate(reply, 106)
        finally:
            runner.close()
            ex.close()


class RtcCliTests(unittest.TestCase):
    def test_cli_exposes_rtc_without_overwriting_task_mode(self):
        from control.web_control.pi05_cli import build_parser, _task_payload

        args = build_parser().parse_args(
            [
                "dry-run",
                "--scheduling-mode",
                "rtc",
                "--inference-mode",
                "dual_continuous",
                "--prompt",
                "test",
            ]
        )
        payload = _task_payload(args, "test")
        self.assertEqual(payload["scheduling_mode"], "rtc")
        self.assertEqual(payload["inference_mode"], "dual_continuous")
        self.assertEqual(payload["rtc_options"]["trigger_index"], 15)

    def test_initialized_sdk_retention_does_not_mask_successful_dry_run(self):
        from unittest import mock
        from control.web_control.pi05_cli import _release_automatic_takeover, CliError

        api = mock.Mock()
        api.request.side_effect = [
            CliError("web API HTTP 409: 不能释放 SDK"),
            {"takeover": False, "policy_session": {"active": False}},
        ]
        with mock.patch("sys.stderr"):
            _release_automatic_takeover(api, "lease")
        self.assertEqual(api.request.call_count, 2)
        api.request.side_effect = [
            CliError("web API HTTP 409: 不能释放 SDK"),
            {"takeover": True, "policy_session": {"active": False}},
        ]
        with self.assertRaises(CliError):
            _release_automatic_takeover(api, "lease")
