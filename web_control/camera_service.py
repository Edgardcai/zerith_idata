#!/usr/bin/env python3
"""Thread-safe, on-demand RGB-D camera service for the H1 web UI.

This module intentionally has no robot-control imports and never talks to
``H1Robot``.  It only wraps the H1 1.3.9 ``CameraClient``.  Streaming is off
after construction and starts only after an explicit ``start()`` or
``set_enabled(True)`` call.

The SDK delivers decoded BGR colour images and raw uint16 depth images in
millimetres.  The service retains the raw latest frame for each stream and
creates a same-size JPEG only when requested.  In particular, it never crops
or resizes a frame; the configured H1 streams therefore remain 640 x 480.

``client_factory`` is injectable so the complete service can be tested without
camera hardware.  A factory receives the same keyword arguments as
``CameraClient`` and must return an object implementing start(), stop(),
get_state(), get_latest_frame(), and get_latest_depth().
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Iterator, Mapping, Optional, Protocol, Tuple

import cv2
import numpy as np


LOGGER = logging.getLogger(__name__)

CAMERA_SDK_PATH = Path("/home/robot/H1_SDK_1.3.9/camera_sdk_python")
DEFAULT_GRPC_TARGET = "localhost:50051"
EXPECTED_WIDTH = 640
EXPECTED_HEIGHT = 480

# Logical names are stable web-API names.  CameraClient names are discovered at
# runtime because the service may add an "rs/" prefix.
EXPECTED_CAMERAS: Mapping[str, str] = {
    "left_wrist": "cam_left_wrist",
    "head": "cam_high",
    "right_wrist": "cam_right_wrist",
}

STREAM_RGB = "rgb"
STREAM_DEPTH = "depth"
VALID_STREAMS = frozenset((STREAM_RGB, STREAM_DEPTH))


class CameraClientProtocol(Protocol):
    """Small protocol implemented by the binary SDK and by test fakes."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def get_state(self, camera_names: Any = None, timeout: float = 5.0) -> Any: ...

    def get_latest_frame(self, cam_name: str) -> Any: ...

    def get_latest_depth(self, cam_name: str) -> Any: ...


ClientFactory = Callable[..., CameraClientProtocol]


class CameraServiceError(RuntimeError):
    """Raised when the camera client cannot be started or an image encoded."""


class UnknownCameraError(KeyError):
    """Raised for a camera identifier that is neither known nor expected."""


class UnknownStreamError(ValueError):
    """Raised when a stream is not ``rgb`` or ``depth``."""


@dataclass(frozen=True)
class FrameSnapshot:
    """Immutable metadata plus an isolated image snapshot.

    ``sdk_postdecode_monotonic_s`` is the timestamp returned by CameraClient
    1.3.9.  Despite wording in the vendor README, local regression testing
    shows that it is assigned after client-side decode/decompression and is not
    the RealSense exposure time.
    """

    camera_name: str
    stream: str
    image: np.ndarray
    sdk_postdecode_monotonic_s: float
    host_getter_monotonic_ns: int
    sequence: int

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])


@dataclass(frozen=True)
class EncodedFrame:
    """JPEG payload and the source-frame metadata used to produce it."""

    data: bytes
    camera_name: str
    stream: str
    sdk_postdecode_monotonic_s: float
    host_getter_monotonic_ns: int
    sequence: int
    width: int
    height: int


@dataclass
class _StreamState:
    image: Optional[np.ndarray] = None
    sdk_timestamp: Optional[float] = None
    received_monotonic_ns: Optional[int] = None
    sequence: int = 0
    polls: int = 0
    unique_frames: int = 0
    duplicate_polls: int = 0
    getter_errors: int = 0
    invalid_frames: int = 0
    encode_errors: int = 0
    last_error: Optional[str] = None
    arrivals_s: Deque[float] = field(default_factory=lambda: deque(maxlen=180))


def _default_client_factory(**kwargs: Any) -> CameraClientProtocol:
    """Import the CPython 3.10 SDK lazily, only when streaming is enabled."""

    sdk_path = str(CAMERA_SDK_PATH)
    if sdk_path not in sys.path:
        sys.path.insert(0, sdk_path)
    try:
        from camera_client import CameraClient
    except Exception as exc:  # pragma: no cover - depends on the host ABI
        raise CameraServiceError(
            "无法导入 H1 CameraClient；请使用 zerith Python 3.10 环境并检查 "
            f"{CAMERA_SDK_PATH}: {exc}"
        ) from exc
    return CameraClient(**kwargs)


class CameraService:
    """Own one CameraClient and expose low-latency latest-frame snapshots.

    Lifecycle calls are synchronous and idempotent.  The actual frame getters
    run in one background thread, while web request threads only copy or JPEG
    encode the retained latest frame.
    """

    def __init__(
        self,
        grpc_target: str = DEFAULT_GRPC_TARGET,
        *,
        connect_timeout: float = 10.0,
        state_timeout: float = 5.0,
        poll_interval_s: float = 0.03,
        stale_after_s: float = 1.0,
        depth_visual_scale: float = 0.03,
        enable_depth: bool = True,
        jpeg_quality: int = 85,
        stop_timeout_s: float = 3.0,
        client_factory: Optional[ClientFactory] = None,
    ) -> None:
        if not grpc_target:
            raise ValueError("grpc_target 不能为空")
        if connect_timeout <= 0 or state_timeout <= 0:
            raise ValueError("连接和状态超时必须大于 0")
        if poll_interval_s <= 0:
            raise ValueError("poll_interval_s 必须大于 0")
        if stale_after_s <= 0:
            raise ValueError("stale_after_s 必须大于 0")
        if depth_visual_scale <= 0:
            raise ValueError("depth_visual_scale 必须大于 0")
        if not 1 <= int(jpeg_quality) <= 100:
            raise ValueError("jpeg_quality 必须在 1..100")
        if stop_timeout_s <= 0:
            raise ValueError("stop_timeout_s 必须大于 0")

        self.grpc_target = grpc_target
        self.connect_timeout = float(connect_timeout)
        self.state_timeout = float(state_timeout)
        self.poll_interval_s = float(poll_interval_s)
        self.stale_after_s = float(stale_after_s)
        self.depth_visual_scale = float(depth_visual_scale)
        self.enable_depth = bool(enable_depth)
        self.jpeg_quality = int(jpeg_quality)
        self.stop_timeout_s = float(stop_timeout_s)
        self._client_factory = client_factory or _default_client_factory

        self._lifecycle_lock = threading.RLock()
        self._lock = threading.RLock()
        self._frames_changed = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._client: Optional[CameraClientProtocol] = None
        self._reader_thread: Optional[threading.Thread] = None

        self._enabled = False
        self._running = False
        self._phase = "disabled"
        self._generation = 0
        self._started_monotonic_ns: Optional[int] = None
        self._stopped_monotonic_ns: Optional[int] = None
        self._last_error: Optional[str] = None
        self._stop_error: Optional[str] = None

        self._camera_info: Dict[str, Dict[str, Any]] = {}
        self._alias_to_name: Dict[str, str] = {}
        self._streams: Dict[Tuple[str, str], _StreamState] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def start(self) -> Dict[str, Any]:
        """Enable all discovered RGB and depth streams.

        Returns a JSON-serialisable status snapshot.  If setup fails, any
        partially constructed SDK client is stopped before an exception is
        raised.
        """

        with self._lifecycle_lock:
            with self._lock:
                if self._enabled and self._running:
                    return self._status_locked(time.monotonic_ns())

            # Recover cleanly from a prior worker failure before reconnecting.
            if self._client is not None or (
                self._reader_thread is not None and self._reader_thread.is_alive()
            ):
                self._stop_locked()

            with self._lock:
                self._enabled = True
                self._running = False
                self._phase = "starting"
                self._last_error = None
                self._stop_error = None
                self._stop_event = threading.Event()
                self._generation += 1
                generation = self._generation
                self._frames_changed.notify_all()

            client: Optional[CameraClientProtocol] = None
            try:
                client = self._client_factory(
                    grpc_target=self.grpc_target,
                    connect_timeout=self.connect_timeout,
                    enable_depth=self.enable_depth,
                )
                client.start()
                state = client.get_state(timeout=self.state_timeout)
                camera_info, aliases, streams = self._parse_state(state)
                if not camera_info:
                    raise CameraServiceError("CameraClient 未发现任何彩色或深度流")

                with self._lock:
                    self._client = client
                    self._camera_info = camera_info
                    self._alias_to_name = aliases
                    self._streams = streams
                    self._started_monotonic_ns = time.monotonic_ns()
                    self._stopped_monotonic_ns = None
                    self._running = True
                    self._phase = "running"

                    thread = threading.Thread(
                        target=self._reader_loop,
                        args=(generation,),
                        name="h1-camera-reader",
                        daemon=True,
                    )
                    self._reader_thread = thread
                    thread.start()
                    self._frames_changed.notify_all()

                return self.get_status()
            except Exception as exc:
                if client is not None:
                    try:
                        client.stop()
                    except Exception as stop_exc:  # pragma: no cover - SDK failure
                        LOGGER.warning("CameraClient cleanup after start failure: %s", stop_exc)
                message = f"相机流启动失败: {exc}"
                with self._lock:
                    self._client = None
                    self._reader_thread = None
                    self._enabled = False
                    self._running = False
                    self._phase = "error"
                    self._last_error = message
                    self._started_monotonic_ns = None
                    self._frames_changed.notify_all()
                if isinstance(exc, CameraServiceError):
                    raise
                raise CameraServiceError(message) from exc

    def stop(self) -> Dict[str, Any]:
        """Disable streaming, join the reader, and release CameraClient."""

        with self._lifecycle_lock:
            self._stop_locked()
            return self.get_status()

    close = stop

    def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        """Explicit web-toggle entry point; streaming defaults to disabled."""

        return self.start() if bool(enabled) else self.stop()

    def _stop_locked(self) -> None:
        """Stop while ``_lifecycle_lock`` is held."""

        with self._lock:
            thread = self._reader_thread
            client = self._client
            self._enabled = False
            if thread is not None or client is not None:
                self._phase = "stopping"
            self._stop_event.set()
            self._frames_changed.notify_all()

        # Getters are documented as non-blocking, so they normally leave within
        # one poll interval.  Do not hold the state lock while joining.
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.stop_timeout_s * 0.5)

        stop_error: Optional[str] = None
        if client is not None:
            try:
                client.stop()
            except Exception as exc:  # pragma: no cover - binary SDK failure
                stop_error = f"CameraClient.stop() 失败: {exc}"
                LOGGER.exception(stop_error)

        if (
            thread is not None
            and thread is not threading.current_thread()
            and thread.is_alive()
        ):
            thread.join(timeout=self.stop_timeout_s * 0.5)

        thread_alive = bool(thread is not None and thread.is_alive())
        if thread_alive:
            extra = "相机读取线程未在超时时间内退出"
            stop_error = f"{stop_error}; {extra}" if stop_error else extra

        with self._lock:
            if self._client is client:
                self._client = None
            if self._reader_thread is thread:
                self._reader_thread = None
            self._running = False
            self._stopped_monotonic_ns = time.monotonic_ns()
            self._stop_error = stop_error
            self._phase = "error" if stop_error else "disabled"
            for stream_state in self._streams.values():
                stream_state.image = None
                stream_state.sdk_timestamp = None
                stream_state.received_monotonic_ns = None
            self._frames_changed.notify_all()

    def __enter__(self) -> "CameraService":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Discovery and background ingestion
    # ------------------------------------------------------------------
    def _parse_state(
        self, state: Any
    ) -> Tuple[
        Dict[str, Dict[str, Any]],
        Dict[str, str],
        Dict[Tuple[str, str], _StreamState],
    ]:
        configs = getattr(state, "camera_configs", None)
        if configs is None:
            raise CameraServiceError("get_state() 返回对象缺少 camera_configs")

        camera_info: Dict[str, Dict[str, Any]] = {}
        aliases: Dict[str, str] = {}
        streams: Dict[Tuple[str, str], _StreamState] = {}

        for config in configs:
            name = str(getattr(config, "camera_name", "")).strip()
            if not name:
                continue
            stream_info: Dict[str, Dict[str, Any]] = {}
            for stream in getattr(config, "streams", ()):
                sdk_type = str(getattr(stream, "type", "")).strip().lower()
                kind = STREAM_RGB if sdk_type in ("color", "rgb") else sdk_type
                if kind == STREAM_DEPTH and not self.enable_depth:
                    continue
                if kind not in VALID_STREAMS:
                    continue
                metadata = self._stream_metadata(stream)
                stream_info[kind] = metadata
                streams[(name, kind)] = _StreamState()

            if not stream_info:
                continue

            logical_name = self._logical_name_for_actual(name)
            camera_info[name] = {
                "camera_name": name,
                "logical_name": logical_name,
                "streams": stream_info,
            }
            aliases[name] = name
            aliases[name.rsplit("/", 1)[-1]] = name
            if logical_name is not None:
                aliases[logical_name] = name

        return camera_info, aliases, streams

    @staticmethod
    def _stream_metadata(stream: Any) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "sdk_type": str(getattr(stream, "type", "")),
            "width": int(getattr(stream, "width", 0) or 0),
            "height": int(getattr(stream, "height", 0) or 0),
            "fps": float(getattr(stream, "fps", 0) or 0),
        }

        intrinsics = getattr(stream, "intrinsics", None)
        has_intrinsics = intrinsics is not None
        has_field = getattr(stream, "HasField", None)
        if callable(has_field):
            try:
                has_intrinsics = bool(has_field("intrinsics"))
            except (TypeError, ValueError):
                pass
        if has_intrinsics and intrinsics is not None:
            metadata["intrinsics"] = {
                "width": int(getattr(intrinsics, "width", 0) or 0),
                "height": int(getattr(intrinsics, "height", 0) or 0),
                "fx": float(getattr(intrinsics, "fx", 0.0) or 0.0),
                "fy": float(getattr(intrinsics, "fy", 0.0) or 0.0),
                "ppx": float(getattr(intrinsics, "ppx", 0.0) or 0.0),
                "ppy": float(getattr(intrinsics, "ppy", 0.0) or 0.0),
                "model": str(getattr(intrinsics, "model", "")),
                "coeffs": [float(value) for value in getattr(intrinsics, "coeffs", ())],
            }
        return metadata

    @staticmethod
    def _logical_name_for_actual(camera_name: str) -> Optional[str]:
        basename = camera_name.rsplit("/", 1)[-1]
        for logical, expected_basename in EXPECTED_CAMERAS.items():
            if basename == expected_basename:
                return logical
        return None

    def _reader_loop(self, generation: int) -> None:
        try:
            while not self._stop_event.is_set():
                with self._lock:
                    if generation != self._generation or not self._enabled:
                        break
                    client = self._client
                    keys = tuple(self._streams)
                if client is None:
                    raise CameraServiceError("reader 启动后 CameraClient 丢失")

                cycle_started = time.monotonic()
                for camera_name, stream in keys:
                    if self._stop_event.is_set():
                        break
                    self._poll_one(client, camera_name, stream)

                remaining = self.poll_interval_s - (time.monotonic() - cycle_started)
                if remaining > 0:
                    self._stop_event.wait(remaining)
        except Exception as exc:  # defensive: per-stream SDK failures are caught below
            message = f"相机读取线程异常退出: {exc}"
            LOGGER.exception(message)
            with self._lock:
                if generation == self._generation:
                    self._last_error = message
                    self._phase = "error"
        finally:
            with self._lock:
                if generation == self._generation:
                    self._running = False
                    if self._enabled and self._phase != "error":
                        self._phase = "error"
                        self._last_error = "相机读取线程意外停止"
                self._frames_changed.notify_all()

    def _poll_one(
        self, client: CameraClientProtocol, camera_name: str, stream: str
    ) -> None:
        key = (camera_name, stream)
        with self._lock:
            state = self._streams.get(key)
            if state is None:
                return
            state.polls += 1

        try:
            result = (
                client.get_latest_frame(camera_name)
                if stream == STREAM_RGB
                else client.get_latest_depth(camera_name)
            )
            host_ns = time.monotonic_ns()
            if result is None:
                return
            if not isinstance(result, (tuple, list)) or len(result) != 2:
                raise ValueError("getter 返回值不是 (ndarray, timestamp)")
            image, sdk_timestamp = result
            image = np.asarray(image)
            timestamp = float(sdk_timestamp)
            if not math.isfinite(timestamp):
                raise ValueError("timestamp 不是有限数")
            self._validate_frame(image, stream)

            with self._frames_changed:
                state = self._streams.get(key)
                if state is None:
                    return
                if state.sdk_timestamp is not None and timestamp == state.sdk_timestamp:
                    state.duplicate_polls += 1
                    return
                state.image = image
                state.sdk_timestamp = timestamp
                state.received_monotonic_ns = host_ns
                state.sequence += 1
                state.unique_frames += 1
                state.last_error = None
                state.arrivals_s.append(host_ns / 1_000_000_000.0)
                self._frames_changed.notify_all()
        except Exception as exc:
            # One camera or one malformed frame must not take down the other five
            # streams.  Health exposes the count and latest error.
            with self._lock:
                state = self._streams.get(key)
                if state is not None:
                    state.getter_errors += 1
                    if isinstance(exc, (TypeError, ValueError)):
                        state.invalid_frames += 1
                    state.last_error = str(exc)

    @staticmethod
    def _validate_frame(image: np.ndarray, stream: str) -> None:
        if stream == STREAM_RGB:
            if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 3:
                raise ValueError(
                    f"RGB 帧应为 HxWx3 uint8 BGR，实际 {image.shape} {image.dtype}"
                )
        elif stream == STREAM_DEPTH:
            if image.dtype != np.uint16 or image.ndim != 2:
                raise ValueError(
                    f"深度帧应为 HxW uint16，实际 {image.shape} {image.dtype}"
                )
        else:  # internal invariant
            raise UnknownStreamError(stream)

        if image.shape[1] != EXPECTED_WIDTH or image.shape[0] != EXPECTED_HEIGHT:
            raise ValueError(
                f"帧分辨率应为 {EXPECTED_WIDTH}x{EXPECTED_HEIGHT}，实际 "
                f"{image.shape[1]}x{image.shape[0]}；服务不会裁切或缩放"
            )

    # ------------------------------------------------------------------
    # Latest-frame access and JPEG/MJPEG helpers
    # ------------------------------------------------------------------
    def get_camera_names(self) -> Tuple[str, ...]:
        """Return discovered SDK camera names in stable robot-position order."""

        with self._lock:
            by_logical = {
                info.get("logical_name"): name for name, info in self._camera_info.items()
            }
            ordered = [
                by_logical[logical]
                for logical in EXPECTED_CAMERAS
                if logical in by_logical
            ]
            ordered_set = set(ordered)
            ordered.extend(sorted(name for name in self._camera_info if name not in ordered_set))
            return tuple(ordered)

    camera_names = get_camera_names

    def available_streams(self, camera: str) -> Tuple[str, ...]:
        with self._lock:
            name = self._resolve_camera_locked(camera)
            if name is None:
                return ()
            return tuple(stream for stream in (STREAM_RGB, STREAM_DEPTH) if (name, stream) in self._streams)

    def resolve_camera(self, camera: str) -> Optional[str]:
        """Resolve SDK name, basename, or left_wrist/head/right_wrist alias."""

        with self._lock:
            return self._resolve_camera_locked(camera)

    def _resolve_camera_locked(self, camera: str) -> Optional[str]:
        key = str(camera).strip()
        if not key:
            raise UnknownCameraError(camera)
        resolved = self._alias_to_name.get(key)
        if resolved is not None:
            return resolved
        if key in EXPECTED_CAMERAS:
            # A known logical camera can legitimately be absent/offline.
            return None
        if key in EXPECTED_CAMERAS.values():
            return None
        raise UnknownCameraError(camera)

    @staticmethod
    def _normalise_stream(stream: str) -> str:
        value = str(stream).strip().lower()
        if value == "color":
            value = STREAM_RGB
        if value not in VALID_STREAMS:
            raise UnknownStreamError(f"未知流 {stream!r}；只能是 rgb 或 depth")
        return value

    def get_latest(
        self, camera: str, stream: str = STREAM_RGB, *, copy: bool = True
    ) -> Optional[FrameSnapshot]:
        """Return the latest frame, or None while disabled/unavailable/waiting.

        Public snapshots are copied by default.  ``copy=False`` returns a
        read-only view for internal high-throughput encoders; it must not be
        retained indefinitely.
        """

        kind = self._normalise_stream(stream)
        with self._lock:
            name = self._resolve_camera_locked(camera)
            if name is None or not self._enabled:
                return None
            return self._snapshot_locked(name, kind, copy_image=copy)

    def _snapshot_locked(
        self, camera_name: str, stream: str, *, copy_image: bool
    ) -> Optional[FrameSnapshot]:
        state = self._streams.get((camera_name, stream))
        if (
            state is None
            or state.image is None
            or state.sdk_timestamp is None
            or state.received_monotonic_ns is None
        ):
            return None
        if copy_image:
            image = state.image.copy()
        else:
            image = state.image.view()
            image.setflags(write=False)
        return FrameSnapshot(
            camera_name=camera_name,
            stream=stream,
            image=image,
            sdk_postdecode_monotonic_s=state.sdk_timestamp,
            host_getter_monotonic_ns=state.received_monotonic_ns,
            sequence=state.sequence,
        )

    def wait_for_frame(
        self,
        camera: str,
        stream: str = STREAM_RGB,
        *,
        after_sequence: Optional[int] = None,
        timeout: Optional[float] = 1.0,
        copy: bool = True,
    ) -> Optional[FrameSnapshot]:
        """Wait for a newer frame without busy-polling.

        Returns None on timeout, disable, unavailable stream, or reader stop.
        """

        kind = self._normalise_stream(stream)
        if timeout is not None and timeout < 0:
            raise ValueError("timeout 不能为负数")
        deadline = None if timeout is None else time.monotonic() + timeout

        with self._frames_changed:
            while True:
                name = self._resolve_camera_locked(camera)
                if name is not None:
                    snapshot = self._snapshot_locked(name, kind, copy_image=copy)
                    if snapshot is not None and (
                        after_sequence is None or snapshot.sequence > after_sequence
                    ):
                        return snapshot
                if not self._enabled or not self._running:
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._frames_changed.wait(remaining)

    def get_encoded_jpeg(
        self,
        camera: str,
        stream: str = STREAM_RGB,
        *,
        quality: Optional[int] = None,
    ) -> Optional[EncodedFrame]:
        """Encode one latest frame without cropping, resizing, or overlay text."""

        kind = self._normalise_stream(stream)
        selected_quality = self.jpeg_quality if quality is None else int(quality)
        if not 1 <= selected_quality <= 100:
            raise ValueError("JPEG quality 必须在 1..100")
        snapshot = self.get_latest(camera, kind, copy=False)
        if snapshot is None:
            return None
        return self._encode_snapshot(snapshot, selected_quality)

    def get_jpeg(
        self,
        camera: str,
        stream: str = STREAM_RGB,
        *,
        quality: Optional[int] = None,
    ) -> Optional[bytes]:
        """Convenience form returning only JPEG bytes for an HTTP response."""

        encoded = self.get_encoded_jpeg(camera, stream, quality=quality)
        return None if encoded is None else encoded.data

    def _encode_snapshot(self, snapshot: FrameSnapshot, quality: int) -> EncodedFrame:
        image = snapshot.image
        if snapshot.stream == STREAM_DEPTH:
            image = self.visualize_depth(image, self.depth_visual_scale)
        try:
            ok, encoded = cv2.imencode(
                ".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
            )
            if not ok:
                raise CameraServiceError("cv2.imencode 返回失败")
            payload = encoded.tobytes()
        except Exception as exc:
            with self._lock:
                state = self._streams.get((snapshot.camera_name, snapshot.stream))
                if state is not None:
                    state.encode_errors += 1
                    state.last_error = f"JPEG 编码失败: {exc}"
            if isinstance(exc, CameraServiceError):
                raise
            raise CameraServiceError(f"JPEG 编码失败: {exc}") from exc

        return EncodedFrame(
            data=payload,
            camera_name=snapshot.camera_name,
            stream=snapshot.stream,
            sdk_postdecode_monotonic_s=snapshot.sdk_postdecode_monotonic_s,
            host_getter_monotonic_ns=snapshot.host_getter_monotonic_ns,
            sequence=snapshot.sequence,
            width=snapshot.width,
            height=snapshot.height,
        )

    @staticmethod
    def visualize_depth(depth_mm: np.ndarray, scale: float = 0.03) -> np.ndarray:
        """Create same-size BGR/JET visualisation while retaining raw data elsewhere."""

        depth = np.asarray(depth_mm)
        CameraService._validate_frame(depth, STREAM_DEPTH)
        if scale <= 0:
            raise ValueError("depth scale 必须大于 0")
        scaled = np.clip(depth.astype(np.float32) * float(scale), 0, 255).astype(
            np.uint8
        )
        colour = cv2.applyColorMap(scaled, cv2.COLORMAP_JET)
        # 0 and uint16 max are invalid/sentinel values, so paint them black in
        # the display only.  The raw millimetre frame remains untouched.
        invalid = (depth == 0) | (depth == np.iinfo(np.uint16).max)
        colour[invalid] = 0
        return colour

    def iter_mjpeg(
        self,
        camera: str,
        stream: str = STREAM_RGB,
        *,
        quality: Optional[int] = None,
        boundary: bytes = b"frame",
        idle_timeout_s: float = 1.0,
    ) -> Iterator[bytes]:
        """Yield multipart MJPEG chunks until streaming is disabled.

        Each browser gets only new unique SDK frames.  Slow browsers naturally
        skip old frames because this service has latest-frame, not queue,
        semantics.
        """

        kind = self._normalise_stream(stream)
        selected_quality = self.jpeg_quality if quality is None else int(quality)
        if not 1 <= selected_quality <= 100:
            raise ValueError("JPEG quality 必须在 1..100")
        if idle_timeout_s <= 0:
            raise ValueError("idle_timeout_s 必须大于 0")
        if not boundary or b"\r" in boundary or b"\n" in boundary:
            raise ValueError("非法 MJPEG boundary")

        last_sequence: Optional[int] = None
        while True:
            snapshot = self.wait_for_frame(
                camera,
                kind,
                after_sequence=last_sequence,
                timeout=idle_timeout_s,
                copy=False,
            )
            if snapshot is None:
                with self._lock:
                    if not self._enabled or not self._running:
                        return
                continue
            last_sequence = snapshot.sequence
            encoded = self._encode_snapshot(snapshot, selected_quality)
            header = (
                b"--"
                + boundary
                + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                + str(len(encoded.data)).encode("ascii")
                + b"\r\nX-Sequence: "
                + str(encoded.sequence).encode("ascii")
                + b"\r\n\r\n"
            )
            yield header + encoded.data + b"\r\n"

    mjpeg_stream = iter_mjpeg

    # ------------------------------------------------------------------
    # Health and statistics
    # ------------------------------------------------------------------
    def get_status(self) -> Dict[str, Any]:
        """Return a JSON-serialisable lifecycle, discovery, and stream report."""

        now_ns = time.monotonic_ns()
        with self._lock:
            return self._status_locked(now_ns)

    status = get_status

    def _status_locked(self, now_ns: int) -> Dict[str, Any]:
        thread_alive = bool(self._reader_thread and self._reader_thread.is_alive())
        discovered_by_logical = {
            info.get("logical_name"): name
            for name, info in self._camera_info.items()
            if info.get("logical_name") is not None
        }
        missing = [logical for logical in EXPECTED_CAMERAS if logical not in discovered_by_logical]
        missing_streams = [
            f"{logical}/{stream}"
            for logical in EXPECTED_CAMERAS
            if logical in discovered_by_logical
            for stream in ((STREAM_RGB, STREAM_DEPTH) if self.enable_depth else (STREAM_RGB,))
            if (discovered_by_logical[logical], stream) not in self._streams
        ]

        camera_reports: Dict[str, Any] = {}
        all_streams_fresh = True
        now_s = now_ns / 1_000_000_000.0
        for name in self.get_camera_names():
            info = self._camera_info[name]
            stream_reports: Dict[str, Any] = {}
            for stream in (STREAM_RGB, STREAM_DEPTH):
                state = self._streams.get((name, stream))
                if state is None:
                    continue
                age_s = (
                    None
                    if state.received_monotonic_ns is None
                    else max(0.0, (now_ns - state.received_monotonic_ns) / 1e9)
                )
                fresh = bool(
                    self._enabled
                    and self._running
                    and age_s is not None
                    and age_s <= self.stale_after_s
                )
                if self._enabled and self._running and not fresh:
                    all_streams_fresh = False
                if state.last_error is not None:
                    all_streams_fresh = False
                image = state.image
                width = None if image is None else int(image.shape[1])
                height = None if image is None else int(image.shape[0])
                dtype = None if image is None else str(image.dtype)
                fps = self._fps_from_arrivals(state.arrivals_s, now_s)
                stream_reports[stream] = {
                    "configured": dict(info["streams"].get(stream, {})),
                    "fresh": fresh,
                    "last_frame_age_s": age_s,
                    "fps_unique_delivered": fps,
                    "sequence": state.sequence,
                    "unique_frames": state.unique_frames,
                    "polls": state.polls,
                    "duplicate_polls": state.duplicate_polls,
                    "getter_errors": state.getter_errors,
                    "invalid_frames": state.invalid_frames,
                    "encode_errors": state.encode_errors,
                    "last_error": state.last_error,
                    "sdk_postdecode_monotonic_s": state.sdk_timestamp,
                    "host_getter_monotonic_ns": state.received_monotonic_ns,
                    "width": width,
                    "height": height,
                    "dtype": dtype,
                    "original_640x480": bool(
                        width == EXPECTED_WIDTH and height == EXPECTED_HEIGHT
                    ),
                }
            camera_reports[name] = {
                "logical_name": info.get("logical_name"),
                "streams": stream_reports,
            }

        expected_reports = {
            logical: {
                "expected_basename": basename,
                "camera_name": discovered_by_logical.get(logical),
                "available": logical in discovered_by_logical,
                "available_streams": (
                    []
                    if logical not in discovered_by_logical
                    else [
                        stream
                        for stream in (STREAM_RGB, STREAM_DEPTH)
                        if (discovered_by_logical[logical], stream) in self._streams
                    ]
                ),
            }
            for logical, basename in EXPECTED_CAMERAS.items()
        }
        uptime_s = (
            None
            if self._started_monotonic_ns is None
            else max(0.0, (now_ns - self._started_monotonic_ns) / 1e9)
        )
        healthy = bool(
            self._enabled
            and self._running
            and thread_alive
            and not missing
            and not missing_streams
            and self._camera_info
            and all_streams_fresh
            and self._last_error is None
        )
        return {
            "enabled": self._enabled,
            "running": self._running,
            "phase": self._phase,
            "healthy": healthy,
            "thread_alive": thread_alive,
            "grpc_target": self.grpc_target,
            "depth_enabled": self.enable_depth,
            "poll_interval_s": self.poll_interval_s,
            "stale_after_s": self.stale_after_s,
            "generation": self._generation,
            "uptime_s": uptime_s,
            "last_error": self._last_error,
            "stop_error": self._stop_error,
            "expected": expected_reports,
            "missing_expected": missing,
            "missing_expected_streams": missing_streams,
            "discovered_count": len(self._camera_info),
            "cameras": camera_reports,
            "timestamp_note": (
                "CameraClient 1.3.9 timestamp 是客户端解码/解压完成后的单调时间，"
                "不是传感器曝光时间"
            ),
        }

    @staticmethod
    def _fps_from_arrivals(arrivals: Deque[float], now_s: float) -> float:
        # A two-second window is responsive yet stable enough for nominal 30 Hz.
        recent = [value for value in arrivals if now_s - value <= 2.0]
        if len(recent) < 2:
            return 0.0
        duration = recent[-1] - recent[0]
        return 0.0 if duration <= 0 else (len(recent) - 1) / duration


__all__ = [
    "CAMERA_SDK_PATH",
    "DEFAULT_GRPC_TARGET",
    "EXPECTED_CAMERAS",
    "EXPECTED_HEIGHT",
    "EXPECTED_WIDTH",
    "CameraClientProtocol",
    "CameraService",
    "CameraServiceError",
    "EncodedFrame",
    "FrameSnapshot",
    "STREAM_DEPTH",
    "STREAM_RGB",
    "UnknownCameraError",
    "UnknownStreamError",
]
