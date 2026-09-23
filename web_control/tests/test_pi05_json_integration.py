"""Exercise the observation-only wire protocol without accessing hardware."""

from contextlib import contextmanager
from http import HTTPStatus
import base64
import json
import threading
import unittest

import cv2
import numpy as np
from websockets.sync.server import serve

from control.web_control.pi05_executor import Pi05Executor, Pi05StateError, REQUIRED_CONFIRMATION
from control.web_control.pi05_protocol import ProtocolValidationError
from control.web_control.tests.test_pi05_executor import FakeCamera, FakeRobot
from control.web_control.tests.test_pi05_protocol import _action_response


@contextmanager
def observation_server(response):
    requests = []
    paths = []

    def process_request(connection, request):
        paths.append(request.path)
        if request.path == "/healthz":
            return connection.respond(HTTPStatus.NOT_FOUND, "unsupported")
        return None

    def handler(connection):
        for message in connection:
            request = json.loads(message)
            requests.append(request)
            result = response if request.get("type") == "observation" else {
                "type": "error", "error": "only observation is supported"
            }
            connection.send(json.dumps(result))

    with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.socket.getsockname()[1], requests, paths
        finally:
            server.shutdown()
            thread.join(timeout=3)


class ObservationOnlyIntegrationTests(unittest.TestCase):
    def test_connect_dry_run_and_reconnect_without_optional_endpoints(self):
        with observation_server(_action_response()) as (port, requests, paths):
            robot = FakeRobot()
            executor = Pi05Executor(robot, FakeCamera(), host="127.0.0.1", port=port)
            try:
                status = executor.reconnect("127.0.0.1", port)["status"]
                self.assertTrue(status["connected"])
                self.assertFalse(status["protocol_ok"])
                self.assertEqual(requests, [])
                with self.assertRaisesRegex(Pi05StateError, "dry-run"):
                    executor.start("任务指令", "lease", confirmation=REQUIRED_CONFIRMATION)

                executor.dry_run("任务指令", "lease")
                self.assertTrue(executor.status()["protocol_ok"])
                self.assertEqual(len(requests), 1)
                request = requests[0]
                self.assertEqual(set(request), {"type", "prompt", "observation"})
                self.assertEqual(request["type"], "observation")
                self.assertEqual(request["prompt"], "任务指令")
                np.testing.assert_allclose(request["observation"]["state"], robot.state)
                images = request["observation"]["images"]
                self.assertEqual(set(images), {"cam_high", "cam_left_wrist", "cam_right_wrist"})
                for encoded in images.values():
                    jpeg = base64.b64decode(encoded, validate=True)
                    decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                    self.assertEqual(decoded.shape, (224, 224, 3))

                status = executor.probe()["status"]
                self.assertTrue(status["connected"])
                self.assertFalse(status["protocol_ok"])
                self.assertFalse(status["dry_run_ok"])
                self.assertEqual(len(requests), 1)
                self.assertEqual(paths, ["/", "/"])
                self.assertEqual(robot.begin_calls, 0)
                self.assertEqual(robot.steps, [])
            finally:
                executor.close()

    def test_missing_success_is_accepted_over_websocket(self):
        response = _action_response()
        response.pop("is_success")
        with observation_server(response) as (port, requests, paths):
            robot = FakeRobot()
            executor = Pi05Executor(robot, FakeCamera(), host="127.0.0.1", port=port)
            try:
                executor.probe()
                result = executor.dry_run("任务指令", "lease")
                self.assertFalse(result["is_success"])
                self.assertTrue(executor.status()["protocol_ok"])
                self.assertEqual(len(requests), 1)
                self.assertEqual(robot.steps, [])
            finally:
                executor.close()

    def test_invalid_actual_responses_fault_without_becoming_ready(self):
        short_chunk = _action_response()
        short_chunk["actions"].pop()
        wrong_success = _action_response()
        wrong_success["is_success"] = 1
        for response in (short_chunk, wrong_success):
            with self.subTest(response_keys=list(response)), observation_server(response) as (port, requests, paths):
                robot = FakeRobot()
                executor = Pi05Executor(robot, FakeCamera(), host="127.0.0.1", port=port)
                try:
                    executor.probe()
                    with self.assertRaises(ProtocolValidationError):
                        executor.dry_run("任务指令", "lease")
                    status = executor.status()
                    self.assertEqual(status["phase"], "fault")
                    self.assertFalse(status["connected"])
                    self.assertFalse(status["protocol_ok"])
                    self.assertFalse(status["dry_run_ok"])
                    self.assertEqual(len(requests), 1)
                    self.assertEqual(paths, ["/"])
                    self.assertEqual(robot.steps, [])
                finally:
                    executor.close()


if __name__ == "__main__":
    unittest.main()
