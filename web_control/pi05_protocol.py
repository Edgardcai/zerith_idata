"""Strict, hardware-free protocol helpers for the Zerith Pi0.5 policy.

This module only performs HTTP health checks, WebSocket JSON exchange, image
encoding, and protocol validation.  It never imports the robot SDK, opens a
camera, reconnects a failed WebSocket, or sends a motor command.
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import threading
from collections.abc import Mapping
from numbers import Real
from typing import Any

import cv2
import numpy as np
from websockets.sync.client import connect as _websocket_connect

CAMERA_NAMES = ("cam_high", "cam_left_wrist", "cam_right_wrist")
JOINT_KEYS = tuple(f"joint{index}" for index in range(1, 8))
ARM_AND_GRIPPER_KEYS = (*JOINT_KEYS, "gripper")

STATE_DIM = 23
ACTION_DIM = 23
ACTION_HORIZON = 50
MODEL_POLICY_DIM = 17

GRIPPER_INDICES = (7, 15)
GRIPPER_OPEN_VALUE = 0.0
GRIPPER_CLOSED_VALUE = 1.5
GRIPPER_VALUE_TOLERANCE = 1e-6

STATE_ORDER = (
    *(f"left.{key}" for key in ARM_AND_GRIPPER_KEYS),
    *(f"right.{key}" for key in ARM_AND_GRIPPER_KEYS),
    "lift.height",
    "waist.pitch",
    "waist.yaw",
    "head.yaw",
    "head.pitch",
    "base.linear_velocity",
    "base.angular_velocity",
)
ACTION_ORDER = (
    *(f"left.{key}" for key in ARM_AND_GRIPPER_KEYS),
    *(f"right.{key}" for key in ARM_AND_GRIPPER_KEYS),
    "lift.target_height",
    "waist.pitch",
    "waist.yaw",
    "head.yaw",
    "head.pitch",
    "base.linear_velocity",
    "base.angular_velocity",
)

_ACTION_TOP_LEVEL_KEYS = frozenset(("left", "right", "lift", "waist", "head", "speed"))
_MAX_WEBSOCKET_MESSAGE_BYTES = 16 * 1024 * 1024


class Pi05ProtocolError(Exception):
    """Base class for protocol, transport, and client-state failures."""


class ProtocolValidationError(Pi05ProtocolError):
    """A local value or remote response violates the fixed protocol."""


class PolicyServerError(Pi05ProtocolError):
    """The policy server returned a JSON error response."""


class ProtocolTransportError(Pi05ProtocolError):
    """A health probe or WebSocket exchange failed."""


class ProtocolClientStateError(Pi05ProtocolError):
    """The caller attempted to reuse a closed, failed, or busy client."""


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ProtocolValidationError(f"Port must be an integer in [1, 65535], got {port!r}")
    return port


def _validate_host(host: str) -> str:
    if not isinstance(host, str) or not host.strip():
        raise ProtocolValidationError("Host must be a non-empty string")
    host = host.strip()
    if "://" in host or any(character in host for character in "/?#@") or any(character.isspace() for character in host):
        raise ProtocolValidationError(f"Host must not contain a URL scheme, path, credentials, or whitespace: {host!r}")
    return host


def _validate_timeout(timeout: float, name: str) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, Real):
        raise ProtocolValidationError(f"{name} must be a positive finite number")
    value = float(timeout)
    if not math.isfinite(value) or value <= 0.0:
        raise ProtocolValidationError(f"{name} must be a positive finite number")
    return value


def _websocket_uri(host: str, port: int) -> str:
    host = _validate_host(host)
    _validate_port(port)
    uri_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"ws://{uri_host}:{port}"


def resize_with_pad(image: np.ndarray, height: int = 224, width: int = 224) -> np.ndarray:
    """Resize a uint8 BGR image without stretching and add black padding."""
    image = np.asarray(image)
    if image.ndim != 3 or image.shape[2] != 3:
        raise ProtocolValidationError(f"Expected an HWC three-channel BGR image, got {image.shape}")
    if image.dtype != np.uint8:
        raise ProtocolValidationError(f"Expected a uint8 BGR image, got {image.dtype}")
    if isinstance(height, bool) or isinstance(width, bool) or not isinstance(height, int) or not isinstance(width, int):
        raise ProtocolValidationError("Image target height and width must be integers")
    if height <= 0 or width <= 0 or image.shape[0] <= 0 or image.shape[1] <= 0:
        raise ProtocolValidationError(
            f"Invalid source or target image dimensions: source={image.shape}, target=({height}, {width})"
        )

    source_height, source_width = image.shape[:2]
    scale = min(height / source_height, width / source_width)
    resized_height = max(1, round(source_height * scale))
    resized_width = max(1, round(source_width * scale))
    resized = cv2.resize(image, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    top = (height - resized_height) // 2
    left = (width - resized_width) // 2
    canvas[top : top + resized_height, left : left + resized_width] = resized
    return canvas


def encode_bgr_jpeg_base64(image: np.ndarray, *, quality: int = 90) -> str:
    """Encode a BGR frame directly with OpenCV as a padded 224x224 JPEG."""
    if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
        raise ProtocolValidationError(f"JPEG quality must be an integer in [1, 100], got {quality!r}")
    prepared = resize_with_pad(image, height=224, width=224)
    success, encoded = cv2.imencode(
        ".jpg",
        prepared,
        [cv2.IMWRITE_JPEG_QUALITY, quality],
    )
    if not success:
        raise ProtocolValidationError("OpenCV failed to encode a camera frame as JPEG")
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def _state_array(state: Any) -> np.ndarray:
    try:
        array = np.asarray(state, dtype=np.float32)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProtocolValidationError("State must contain numeric values") from exc
    if array.shape != (STATE_DIM,):
        raise ProtocolValidationError(f"Expected Zerith state shape ({STATE_DIM},), got {array.shape}")
    if not np.isfinite(array).all():
        raise ProtocolValidationError("State contains NaN or Inf")
    return array


def build_observation_request(
    state: Any,
    images: Mapping[str, np.ndarray],
    prompt: str,
    *,
    jpeg_quality: int = 90,
) -> dict[str, Any]:
    """Build the canonical non-RTC observation request."""
    state_array = _state_array(state)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ProtocolValidationError("Prompt must be a non-empty string")
    if not isinstance(images, Mapping):
        raise ProtocolValidationError("Images must be a mapping of canonical camera names to BGR frames")
    missing = [name for name in CAMERA_NAMES if name not in images]
    if missing:
        raise ProtocolValidationError(f"Missing camera frames: {missing}")

    return {
        "type": "observation",
        "prompt": prompt,
        "observation": {
            "state": state_array.tolist(),
            "images": {
                name: encode_bgr_jpeg_base64(images[name], quality=jpeg_quality)
                for name in CAMERA_NAMES
            },
        },
    }


def _require_object(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolValidationError(f"Expected {path} to be a JSON object")
    return value


def _require_exact_keys(value: Mapping[str, Any], expected: set[str] | frozenset[str], path: str) -> None:
    actual = set(value)
    if actual != set(expected):
        missing = sorted(set(expected) - actual)
        unexpected = sorted(actual - set(expected))
        raise ProtocolValidationError(f"Invalid keys at {path}: missing={missing}, unexpected={unexpected}")


def _finite_number(source: Mapping[str, Any], key: str, path: str) -> float:
    if key not in source:
        raise ProtocolValidationError(f"Missing numeric field {path}.{key}")
    value = source[key]
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ProtocolValidationError(f"Expected finite JSON number at {path}.{key}, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise ProtocolValidationError(f"Expected finite JSON number at {path}.{key}, got {value!r}")
    return result


def _canonical_gripper(value: float, path: str) -> float:
    if math.isclose(value, GRIPPER_OPEN_VALUE, rel_tol=0.0, abs_tol=GRIPPER_VALUE_TOLERANCE):
        return GRIPPER_OPEN_VALUE
    if math.isclose(value, GRIPPER_CLOSED_VALUE, rel_tol=0.0, abs_tol=GRIPPER_VALUE_TOLERANCE):
        return GRIPPER_CLOSED_VALUE
    raise ProtocolValidationError(
        f"{path} must be {GRIPPER_OPEN_VALUE} or {GRIPPER_CLOSED_VALUE} "
        f"within {GRIPPER_VALUE_TOLERANCE}, got {value}"
    )


def structured_action_to_array(action: Mapping[str, Any]) -> np.ndarray:
    """Validate and flatten one binary-gripper structured 23-D action."""
    action = _require_object(action, "action")
    _require_exact_keys(action, _ACTION_TOP_LEVEL_KEYS, "action")

    left = _require_object(action["left"], "action.left")
    right = _require_object(action["right"], "action.right")
    lift = _require_object(action["lift"], "action.lift")
    waist = _require_object(action["waist"], "action.waist")
    head = _require_object(action["head"], "action.head")
    speed = _require_object(action["speed"], "action.speed")

    arm_keys = frozenset(ARM_AND_GRIPPER_KEYS)
    _require_exact_keys(left, arm_keys, "action.left")
    _require_exact_keys(right, arm_keys, "action.right")
    _require_exact_keys(lift, frozenset(("height",)), "action.lift")
    _require_exact_keys(waist, frozenset(("pitch", "yaw")), "action.waist")
    _require_exact_keys(head, frozenset(("yaw", "pitch")), "action.head")
    _require_exact_keys(speed, frozenset(("linear", "angular")), "action.speed")

    values = [
        *(_finite_number(left, key, "action.left") for key in ARM_AND_GRIPPER_KEYS),
        *(_finite_number(right, key, "action.right") for key in ARM_AND_GRIPPER_KEYS),
        _finite_number(lift, "height", "action.lift"),
        _finite_number(waist, "pitch", "action.waist"),
        _finite_number(waist, "yaw", "action.waist"),
        _finite_number(head, "yaw", "action.head"),
        _finite_number(head, "pitch", "action.head"),
        _finite_number(speed, "linear", "action.speed"),
        _finite_number(speed, "angular", "action.speed"),
    ]
    values[7] = _canonical_gripper(values[7], "action.left.gripper")
    values[15] = _canonical_gripper(values[15], "action.right.gripper")
    result = np.asarray(values, dtype=np.float32)
    if result.shape != (ACTION_DIM,) or not np.isfinite(result).all():
        raise ProtocolValidationError(f"Invalid Zerith action: shape={result.shape}")
    return result


def parse_action_chunk(response: Mapping[str, Any]) -> np.ndarray:
    """Validate an exact 50x23 no-status action response."""
    response = _require_object(response, "response")
    if response.get("type") == "error":
        raise PolicyServerError(f"Policy server error: {response.get('error', response)!s}")
    if response.get("type") != "action_chunk":
        raise ProtocolValidationError(f"Expected action_chunk response, got {response.get('type')!r}")
    if "is_success" in response:
        raise ProtocolValidationError("The configured no-status policy unexpectedly returned is_success")
    actions = response.get("actions")
    if not isinstance(actions, list):
        raise ProtocolValidationError("Policy response actions must be a JSON array")
    if len(actions) != ACTION_HORIZON:
        raise ProtocolValidationError(
            f"Expected exactly {ACTION_HORIZON} actions, got {len(actions)}"
        )
    chunk = np.stack([structured_action_to_array(action) for action in actions])
    if chunk.shape != (ACTION_HORIZON, ACTION_DIM) or not np.isfinite(chunk).all():
        raise ProtocolValidationError(
            f"Expected a finite action chunk shape ({ACTION_HORIZON}, {ACTION_DIM}), got {chunk.shape}"
        )
    return chunk


def validate_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the fixed robot layout while allowing unrelated policy metadata."""
    metadata = _require_object(metadata, "metadata")
    scalar_expected: dict[str, Any] = {
        "robot": "zerith_h1_pro",
        "wire_state_dim": STATE_DIM,
        "wire_action_dim": ACTION_DIM,
        "model_policy_dim": MODEL_POLICY_DIM,
        "input_gripper_binary": False,
        "output_gripper_binary": True,
        "status_mode": "none",
    }
    for key, expected in scalar_expected.items():
        if key not in metadata:
            raise ProtocolValidationError(f"Metadata is missing {key!r}")
        actual = metadata[key]
        if isinstance(expected, bool):
            matches = actual is expected
        elif isinstance(expected, int):
            matches = type(actual) is int and actual == expected
        else:
            matches = type(actual) is type(expected) and actual == expected
        if not matches:
            raise ProtocolValidationError(f"Metadata mismatch for {key!r}: got {actual!r}, expected {expected!r}")

    for key, expected in (("state_order", STATE_ORDER), ("action_order", ACTION_ORDER)):
        actual = metadata.get(key)
        if not isinstance(actual, list) or actual != list(expected):
            raise ProtocolValidationError(f"Metadata mismatch for {key!r}: got {actual!r}, expected {list(expected)!r}")
    return dict(metadata)


def parse_metadata_response(response: Mapping[str, Any]) -> dict[str, Any]:
    response = _require_object(response, "response")
    if response.get("type") == "error":
        raise PolicyServerError(f"Policy server error: {response.get('error', response)!s}")
    if response.get("type") != "metadata":
        raise ProtocolValidationError(f"Expected metadata response, got {response.get('type')!r}")
    return validate_metadata(response.get("metadata"))


def probe_healthz(host: str, port: int = 9973, *, timeout: float = 3.0) -> str:
    """Perform only ``GET /healthz`` without consulting HTTP proxy settings."""
    host = _validate_host(host)
    port = _validate_port(port)
    timeout = _validate_timeout(timeout, "Health timeout")
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", "/healthz", headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read(1024)
    except Exception as exc:
        raise ProtocolTransportError(f"Health probe failed for {host}:{port}: {exc}") from exc
    finally:
        connection.close()
    if response.status != 200 or body.strip() != b"OK":
        preview = body[:200].decode("utf-8", errors="replace")
        raise ProtocolValidationError(
            f"Unexpected health response from {host}:{port}: status={response.status}, body={preview!r}"
        )
    return "OK"


class ZerithJsonPolicyClient:
    """One-shot-connection synchronous client with no reconnect behavior."""

    def __init__(
        self,
        host: str,
        port: int = 9973,
        *,
        open_timeout: float = 10.0,
        inference_timeout: float = 10.0,
    ) -> None:
        self.host = _validate_host(host)
        self.port = _validate_port(port)
        self.uri = _websocket_uri(self.host, self.port)
        self.inference_timeout = _validate_timeout(inference_timeout, "Inference timeout")
        open_timeout = _validate_timeout(open_timeout, "WebSocket open timeout")
        self._request_lock = threading.Lock()
        self._closed = False
        self._failed = False
        try:
            self.websocket = _websocket_connect(
                self.uri,
                compression=None,
                max_size=_MAX_WEBSOCKET_MESSAGE_BYTES,
                open_timeout=open_timeout,
                close_timeout=2.0,
                proxy=None,
            )
        except Exception as exc:
            self._failed = True
            raise ProtocolTransportError(f"Unable to open policy WebSocket {self.uri}: {exc}") from exc

    def __enter__(self) -> "ZerithJsonPolicyClient":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @property
    def usable(self) -> bool:
        return not self._closed and not self._failed

    def health(self, *, timeout: float = 3.0) -> str:
        """Run the independent read-only HTTP health probe."""
        return probe_healthz(self.host, self.port, timeout=timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.websocket.close()
        except Exception:
            pass

    def _invalidate(self) -> None:
        self._failed = True
        self.close()

    def _request(self, payload: Mapping[str, Any], *, timeout: float) -> dict[str, Any]:
        if not self.usable:
            raise ProtocolClientStateError("Policy client is closed or failed; create a new client only after operator confirmation")
        timeout = _validate_timeout(timeout, "Receive timeout")
        if not self._request_lock.acquire(blocking=False):
            raise ProtocolClientStateError("Only one outstanding request is allowed per WebSocket")
        try:
            message = json.dumps(dict(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            self.websocket.send(message)
            response_message = self.websocket.recv(timeout=timeout)
            if not isinstance(response_message, str):
                raise ProtocolValidationError("Policy server must return a WebSocket text frame containing JSON")
            response = json.loads(response_message)
            if not isinstance(response, dict):
                raise ProtocolValidationError("Policy server returned a non-object JSON value")
            return response
        except ProtocolClientStateError:
            raise
        except Exception as exc:
            self._invalidate()
            if isinstance(exc, Pi05ProtocolError):
                raise
            raise ProtocolTransportError(f"Policy WebSocket request failed: {exc}") from exc
        finally:
            self._request_lock.release()

    def metadata(self, *, timeout: float = 5.0) -> dict[str, Any]:
        response = self._request({"type": "metadata"}, timeout=timeout)
        try:
            return parse_metadata_response(response)
        except Exception:
            self._invalidate()
            raise

    def infer(
        self,
        state: Any,
        images: Mapping[str, np.ndarray],
        prompt: str,
        *,
        jpeg_quality: int = 90,
        timeout: float | None = None,
    ) -> tuple[np.ndarray, dict[str, Any]]:
        request = build_observation_request(
            state,
            images,
            prompt,
            jpeg_quality=jpeg_quality,
        )
        response = self._request(
            request,
            timeout=self.inference_timeout if timeout is None else timeout,
        )
        try:
            return parse_action_chunk(response), response
        except Exception:
            self._invalidate()
            raise


__all__ = [
    "ACTION_DIM",
    "ACTION_HORIZON",
    "ACTION_ORDER",
    "ARM_AND_GRIPPER_KEYS",
    "CAMERA_NAMES",
    "GRIPPER_CLOSED_VALUE",
    "GRIPPER_INDICES",
    "GRIPPER_OPEN_VALUE",
    "GRIPPER_VALUE_TOLERANCE",
    "JOINT_KEYS",
    "MODEL_POLICY_DIM",
    "Pi05ProtocolError",
    "PolicyServerError",
    "ProtocolClientStateError",
    "ProtocolTransportError",
    "ProtocolValidationError",
    "STATE_DIM",
    "STATE_ORDER",
    "ZerithJsonPolicyClient",
    "build_observation_request",
    "encode_bgr_jpeg_base64",
    "parse_action_chunk",
    "parse_metadata_response",
    "probe_healthz",
    "resize_with_pad",
    "structured_action_to_array",
    "validate_metadata",
]
