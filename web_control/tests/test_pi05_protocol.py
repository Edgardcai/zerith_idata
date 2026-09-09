from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

import cv2
import numpy as np

from control.web_control import pi05_protocol as protocol


def _metadata() -> dict:
    return {
        "robot": "zerith_h1_pro",
        "wire_state_dim": 23,
        "wire_action_dim": 23,
        "model_policy_dim": 17,
        "state_order": list(protocol.STATE_ORDER),
        "action_order": list(protocol.ACTION_ORDER),
        "input_gripper_binary": False,
        "output_gripper_binary": True,
        "status_mode": "none",
        "policy_name": "extra metadata is allowed",
    }


def _structured_action(step: int = 0) -> dict:
    left = {key: step / 100.0 + index / 1000.0 for index, key in enumerate(protocol.JOINT_KEYS)}
    right = {key: -step / 100.0 - index / 1000.0 for index, key in enumerate(protocol.JOINT_KEYS)}
    left["gripper"] = protocol.GRIPPER_OPEN_VALUE if step % 2 == 0 else protocol.GRIPPER_CLOSED_VALUE
    right["gripper"] = protocol.GRIPPER_CLOSED_VALUE if step % 2 == 0 else protocol.GRIPPER_OPEN_VALUE
    return {
        "left": left,
        "right": right,
        "lift": {"height": 0.4},
        "waist": {"pitch": 0.1, "yaw": -0.1},
        "head": {"yaw": 0.2, "pitch": -0.2},
        "speed": {"linear": 0.0, "angular": 0.0},
    }


def _action_response() -> dict:
    return {
        "type": "action_chunk",
        "actions": [_structured_action(index) for index in range(protocol.ACTION_HORIZON)],
        "policy_timing": {"infer_ms": 12.0},
        "server_timing": {"total_ms": 13.0},
    }


def _images() -> dict[str, np.ndarray]:
    return {
        name: np.zeros((48, 64, 3), dtype=np.uint8)
        for name in protocol.CAMERA_NAMES
    }


class _FakeWebSocket:
    def __init__(self, responses: list[str | bytes]) -> None:
        self.responses = list(responses)
        self.sent: list[str | bytes] = []
        self.timeouts: list[float] = []
        self.closed = False

    def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    def recv(self, *, timeout: float) -> str | bytes:
        self.timeouts.append(timeout)
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


class Pi05ProtocolEncodingTests(unittest.TestCase):
    def test_bgr_jpeg_is_224_square_with_black_letterbox_and_no_rgb_swap(self) -> None:
        image = np.zeros((100, 200, 3), dtype=np.uint8)
        image[..., 2] = 255

        encoded = protocol.encode_bgr_jpeg_base64(image, quality=95)
        raw = base64.b64decode(encoded, validate=True)
        decoded = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)

        self.assertEqual(decoded.shape, (224, 224, 3))
        self.assertLess(int(decoded[10, 112].max()), 10)
        self.assertGreater(int(decoded[112, 112, 2]), 240)
        self.assertLess(int(decoded[112, 112, 0]), 10)

    def test_observation_request_is_canonical_json_data_and_has_no_rtc(self) -> None:
        images = _images()
        images["cam_high"][..., 1] = 255
        state = np.arange(23, dtype=np.float32)
        state[7] = 0.37
        state[15] = 1.234
        request = protocol.build_observation_request(
            state,
            images,
            "test prompt",
        )

        self.assertEqual(set(request), {"type", "prompt", "observation"})
        self.assertEqual(request["type"], "observation")
        self.assertNotIn("rtc", request)
        self.assertEqual(len(request["observation"]["state"]), 23)
        self.assertEqual(request["observation"]["state"][7], float(state[7]))
        self.assertEqual(request["observation"]["state"][15], float(state[15]))
        self.assertEqual(tuple(request["observation"]["images"]), protocol.CAMERA_NAMES)
        json.dumps(request, allow_nan=False)

    def test_observation_rejects_bad_state_prompt_camera_and_image(self) -> None:
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.build_observation_request(np.zeros(22), _images(), "prompt")
        state = np.zeros(23)
        state[4] = np.nan
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.build_observation_request(state, _images(), "prompt")
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.build_observation_request(np.zeros(23), _images(), "  ")
        missing = _images()
        missing.pop("cam_high")
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.build_observation_request(np.zeros(23), missing, "prompt")
        bad_image = _images()
        bad_image["cam_high"] = np.zeros((10, 10, 3), dtype=np.float32)
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.build_observation_request(np.zeros(23), bad_image, "prompt")


class Pi05ProtocolMetadataTests(unittest.TestCase):
    def test_metadata_accepts_all_server_owned_gripper_and_status_modes(self) -> None:
        for input_binary in (False, True):
            for output_binary in (False, True):
                for status_mode in protocol.STATUS_MODES:
                    metadata = _metadata()
                    metadata["input_gripper_binary"] = input_binary
                    metadata["output_gripper_binary"] = output_binary
                    metadata["status_mode"] = status_mode
                    validated = protocol.validate_metadata(metadata)
                    self.assertIs(validated["input_gripper_binary"], input_binary)
                    self.assertIs(validated["output_gripper_binary"], output_binary)
                    self.assertEqual(validated["status_mode"], status_mode)
                    self.assertEqual(validated["state_order"], list(protocol.STATE_ORDER))
                    self.assertEqual(validated["action_order"], list(protocol.ACTION_ORDER))
        self.assertEqual(
            (protocol.GRIPPER_OPEN_VALUE, protocol.GRIPPER_CLOSED_VALUE),
            (0.0, 1.5),
        )

        for key, bad_value in (
            ("wire_state_dim", 23.0),
            ("input_gripper_binary", 0),
            ("output_gripper_binary", "true"),
            ("status_mode", "left_status"),
            ("status_mode", 0),
        ):
            metadata = _metadata()
            metadata[key] = bad_value
            with self.subTest(key=key), self.assertRaises(protocol.ProtocolValidationError):
                protocol.validate_metadata(metadata)

        for key in ("input_gripper_binary", "output_gripper_binary", "status_mode"):
            metadata = _metadata()
            metadata.pop(key)
            with self.subTest(missing=key), self.assertRaises(protocol.ProtocolValidationError):
                protocol.validate_metadata(metadata)

        metadata = _metadata()
        metadata["state_order"][0], metadata["state_order"][1] = (
            metadata["state_order"][1],
            metadata["state_order"][0],
        )
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.validate_metadata(metadata)

        metadata = _metadata()
        metadata.pop("action_order")
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.validate_metadata(metadata)

    def test_metadata_envelope_and_server_error_are_strict(self) -> None:
        result = protocol.parse_metadata_response({"type": "metadata", "metadata": _metadata()})
        self.assertEqual(result["robot"], "zerith_h1_pro")
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.parse_metadata_response({"type": "metadata", "metadata": {}})
        with self.assertRaises(protocol.PolicyServerError):
            protocol.parse_metadata_response({"type": "error", "error": "traceback"})


class Pi05ProtocolActionTests(unittest.TestCase):
    def test_exact_50_by_23_chunk_preserves_gripper_values(self) -> None:
        response = _action_response()
        response["actions"][0]["left"]["gripper"] = 0.37
        response["actions"][0]["right"]["gripper"] = 1.234
        chunk = protocol.parse_action_chunk(response)
        self.assertEqual(chunk.shape, (50, 23))
        self.assertEqual(chunk.dtype, np.float32)
        self.assertTrue(np.isfinite(chunk).all())
        self.assertEqual(chunk[0, 7], np.float32(0.37))
        self.assertEqual(chunk[0, 15], np.float32(1.234))

    def test_accepts_optional_strict_boolean_success_status(self) -> None:
        for success in (False, True):
            response = _action_response()
            response["is_success"] = success
            chunk = protocol.parse_action_chunk(response)
            self.assertEqual(chunk.shape, (50, 23))

        response = _action_response()
        self.assertEqual(protocol.parse_action_chunk(response).shape, (50, 23))

        for invalid in (None, 0, 1, "false", [], {}):
            response = _action_response()
            response["is_success"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(protocol.ProtocolValidationError):
                protocol.parse_action_chunk(response)

    def test_rejects_wrong_horizon_and_nonfinite_action(self) -> None:
        for count in (0, 49, 51):
            response = _action_response()
            response["actions"] = response["actions"][:count]
            if count == 51:
                response["actions"].append(_structured_action(50))
            with self.subTest(count=count), self.assertRaises(protocol.ProtocolValidationError):
                protocol.parse_action_chunk(response)

        response = _action_response()
        response["actions"][2]["head"]["yaw"] = float("inf")
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.parse_action_chunk(response)

    def test_rejects_missing_extra_or_non_numeric_action_fields(self) -> None:
        action = _structured_action()
        action["left"].pop("joint7")
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.structured_action_to_array(action)

        action = _structured_action()
        action["unexpected"] = {}
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.structured_action_to_array(action)

        action = _structured_action()
        action["right"]["joint1"] = "0.0"
        with self.assertRaises(protocol.ProtocolValidationError):
            protocol.structured_action_to_array(action)


class Pi05ProtocolClientTests(unittest.TestCase):
    def test_client_disables_proxy_and_exchanges_only_json_text(self) -> None:
        fake = _FakeWebSocket(
            [json.dumps({"type": "metadata", "metadata": _metadata()})]
        )
        with mock.patch.object(protocol, "_websocket_connect", return_value=fake) as connect:
            client = protocol.ZerithJsonPolicyClient("192.168.1.154", 9973)
            metadata = client.metadata()
            client.close()

        self.assertEqual(metadata["robot"], "zerith_h1_pro")
        connect.assert_called_once()
        args, kwargs = connect.call_args
        self.assertEqual(args, ("ws://192.168.1.154:9973",))
        self.assertIsNone(kwargs["proxy"])
        self.assertIsNone(kwargs["compression"])
        self.assertEqual(fake.timeouts, [5.0])
        self.assertEqual(json.loads(fake.sent[0]), {"type": "metadata"})
        self.assertIsInstance(fake.sent[0], str)
        self.assertTrue(fake.closed)

    def test_infer_returns_strict_chunk_and_raw_response(self) -> None:
        response = _action_response()
        response["is_success"] = True
        fake = _FakeWebSocket([json.dumps(response)])
        with mock.patch.object(protocol, "_websocket_connect", return_value=fake):
            with protocol.ZerithJsonPolicyClient("server") as client:
                chunk, raw = client.infer(np.zeros(23), _images(), "prompt")

        self.assertEqual(chunk.shape, (50, 23))
        self.assertEqual(raw["type"], "action_chunk")
        self.assertIs(raw["is_success"], True)
        sent = json.loads(fake.sent[0])
        self.assertEqual(sent["type"], "observation")
        self.assertNotIn("rtc", sent)
        self.assertIsInstance(fake.sent[0], str)

    def test_binary_response_invalidates_client_without_reconnect(self) -> None:
        fake = _FakeWebSocket(
            [json.dumps({"type": "metadata", "metadata": _metadata()}).encode("utf-8")]
        )
        with mock.patch.object(protocol, "_websocket_connect", return_value=fake) as connect:
            client = protocol.ZerithJsonPolicyClient("server")
            with self.assertRaises(protocol.ProtocolValidationError):
                client.metadata()
            self.assertFalse(client.usable)
            self.assertTrue(fake.closed)
            with self.assertRaises(protocol.ProtocolClientStateError):
                client.metadata()
        connect.assert_called_once()

    def test_malformed_or_error_response_invalidates_client(self) -> None:
        for response, expected in (
            ("not json", protocol.ProtocolTransportError),
            (json.dumps({"type": "error", "error": "bad request"}), protocol.PolicyServerError),
        ):
            fake = _FakeWebSocket([response])
            with self.subTest(response=response), mock.patch.object(
                protocol,
                "_websocket_connect",
                return_value=fake,
            ):
                client = protocol.ZerithJsonPolicyClient("server")
                with self.assertRaises(expected):
                    client.metadata()
                self.assertFalse(client.usable)
                self.assertTrue(fake.closed)


class Pi05ProtocolHealthTests(unittest.TestCase):
    def test_health_probe_is_direct_read_only_get(self) -> None:
        response = mock.Mock(status=200)
        response.read.return_value = b"OK\n"
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(protocol.http.client, "HTTPConnection", return_value=connection) as constructor:
            result = protocol.probe_healthz("192.168.1.154", 9973, timeout=3.0)

        self.assertEqual(result, "OK")
        constructor.assert_called_once_with("192.168.1.154", 9973, timeout=3.0)
        connection.request.assert_called_once_with(
            "GET",
            "/healthz",
            headers={"Connection": "close"},
        )
        connection.close.assert_called_once_with()

    def test_health_probe_rejects_unexpected_response(self) -> None:
        response = mock.Mock(status=503)
        response.read.return_value = b"warming"
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(protocol.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaises(protocol.ProtocolValidationError):
                protocol.probe_healthz("server")


if __name__ == "__main__":
    unittest.main()
