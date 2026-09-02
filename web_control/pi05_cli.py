#!/usr/bin/env python3
"""Operator CLI for the web-integrated Zerith Pi0.5 executor.

This tool never imports the vendor robot SDK.  Metadata talks directly to the
JSON policy server; dry-run and motion commands go through the existing 8080
service so that RobotService remains the sole H1Robot owner.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
import secrets
import sys
import threading
import time
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from .pi05_executor import (
    DEFAULT_CONTROL_RATE_HZ,
    DEFAULT_HOST,
    DEFAULT_JOINT_SPEED_DEG_S,
    DEFAULT_PORT,
    REQUIRED_CONFIRMATION,
)
from .pi05_protocol import (
    ACTION_HORIZON,
    Pi05ProtocolError,
    ZerithJsonPolicyClient,
    probe_healthz,
)


class CliError(RuntimeError):
    pass


AUTO_TAKEOVER_CLIENT_PREFIX = "inference-cli-"
AUTO_DRY_RUN_NOTE = (
    "standalone --auto-takeover dry-run releases its lease after validation; "
    "use run --auto-takeover --prepare with the same prompt to preserve the "
    "dry-run gate through start"
)
DEFAULT_LEASE_SECONDS = 3.0


class _HeartbeatGuard:
    """Keep an auto-acquired lease alive across one blocking web request."""

    def __init__(self, api: "WebApi", lease: str, lease_seconds: float) -> None:
        self._api = api
        self._lease = lease
        self._interval = min(0.8, max(0.02, lease_seconds / 3.0))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._error_lock = threading.Lock()
        self._error: BaseException | None = None

    def __enter__(self) -> "_HeartbeatGuard":
        # Renew once before starting a potentially slow reconnect, dry-run, or
        # policy-session begin.  The daemon thread covers the blocking window.
        self._api.request("POST", "/api/heartbeat", {}, lease=self._lease)
        self._thread = threading.Thread(
            target=self._loop,
            name="pi05-cli-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            request_timeout = float(getattr(self._api, "timeout", 1.0))
            thread.join(timeout=max(1.0, min(30.0, request_timeout + 1.0)))
            if thread.is_alive() and exc_type is None:
                raise CliError("automatic takeover heartbeat did not stop cleanly")
        with self._error_lock:
            heartbeat_error = self._error
        if heartbeat_error is not None and exc_type is None:
            raise CliError(
                f"automatic takeover heartbeat failed: {heartbeat_error}"
            ) from heartbeat_error
        return False

    def _loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self._api.request(
                    "POST",
                    "/api/heartbeat",
                    {},
                    lease=self._lease,
                )
            except BaseException as exc:
                with self._error_lock:
                    self._error = exc
                self._stop_event.set()
                return


def _prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        try:
            with open(args.prompt_file, encoding="utf-8") as prompt_file:
                value = prompt_file.read()
        except OSError as exc:
            raise CliError(f"cannot read prompt file: {exc}") from exc
    else:
        value = args.prompt or ""
    value = value.strip()
    if not value:
        raise CliError("prompt must not be empty")
    if len(value) > 1000:
        raise CliError("prompt must not exceed 1000 characters")
    return value


def _web_base(value: str) -> str:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise CliError("--web-url must be an http(s) URL with a hostname")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CliError("--web-url must not contain credentials, query, or fragment")
    return str(value).rstrip("/") + "/"


class WebApi:
    def __init__(self, base_url: str, *, token: str | None, timeout: float) -> None:
        self.base_url = _web_base(base_url)
        self.token = token
        self.timeout = float(timeout)
        self._opener = build_opener(ProxyHandler({}))

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        lease: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-Control-Token"] = self.token
        if lease:
            headers["X-Control-Lease"] = lease
        request = Request(
            urljoin(self.base_url, path.lstrip("/")),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                payload = response.read()
        except HTTPError as exc:
            payload = exc.read()
            try:
                detail = json.loads(payload).get("error")
            except Exception:
                detail = payload.decode("utf-8", errors="replace")[:300]
            raise CliError(f"web API HTTP {exc.code}: {detail}") from exc
        except (OSError, URLError) as exc:
            raise CliError(f"web API request failed: {exc}") from exc
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CliError("web API returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise CliError("web API returned a non-object JSON value")
        return value


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _metadata(args: argparse.Namespace) -> int:
    health = probe_healthz(args.policy_host, args.policy_port, timeout=args.timeout)
    with ZerithJsonPolicyClient(
        args.policy_host,
        args.policy_port,
        open_timeout=args.timeout,
        inference_timeout=args.inference_timeout,
    ) as client:
        metadata = client.metadata(timeout=args.timeout)
    _print({"health": health, "metadata": metadata})
    return 0


def _web_api(args: argparse.Namespace) -> WebApi:
    return WebApi(
        args.web_url,
        token=args.token,
        timeout=args.timeout,
    )


def _lease(args: argparse.Namespace) -> str:
    value = str(args.lease or os.environ.get("H1_CONTROL_LEASE", "")).strip()
    if not value:
        raise CliError(
            "a live --lease (or H1_CONTROL_LEASE), or explicit "
            "--auto-takeover, is required"
        )
    return value


@contextmanager
def _control_lease(
    args: argparse.Namespace,
    api: WebApi,
) -> Iterator[tuple[str, bool, float]]:
    """Yield a supplied lease or explicitly acquire/release one via port 8080."""

    automatic = bool(getattr(args, "auto_takeover", False))
    supplied = str(
        getattr(args, "lease", None)
        or os.environ.get("H1_CONTROL_LEASE", "")
    ).strip()
    if not automatic:
        yield _lease(args), False, DEFAULT_LEASE_SECONDS
        return
    if supplied:
        raise CliError(
            "--auto-takeover cannot be combined with --lease or "
            "H1_CONTROL_LEASE"
        )

    takeover = api.request(
        "POST",
        "/api/takeover",
        {
            "enabled": True,
            "client_id": (
                AUTO_TAKEOVER_CLIENT_PREFIX + secrets.token_urlsafe(8)
            ),
        },
    )
    lease = takeover.get("lease_id")
    if not isinstance(lease, str) or not lease:
        raise CliError("automatic takeover response did not contain a valid lease_id")
    lease_seconds_value = takeover.get("lease_seconds", DEFAULT_LEASE_SECONDS)
    try:
        lease_seconds = float(lease_seconds_value)
    except (TypeError, ValueError):
        lease_seconds = DEFAULT_LEASE_SECONDS
    if not math.isfinite(lease_seconds) or lease_seconds <= 0.0:
        lease_seconds = DEFAULT_LEASE_SECONDS

    try:
        yield lease, True, lease_seconds
    except BaseException as primary_error:
        try:
            api.request(
                "POST",
                "/api/takeover",
                {"enabled": False},
                lease=lease,
            )
        except BaseException as release_error:
            raise CliError(
                f"operation failed ({primary_error}); automatic takeover "
                f"release also failed: {release_error}"
            ) from primary_error
        raise
    else:
        api.request(
            "POST",
            "/api/takeover",
            {"enabled": False},
            lease=lease,
        )


def _best_effort_pi05_stop(api: WebApi) -> None:
    try:
        api.request("POST", "/api/pi05/stop", {})
    except BaseException:
        pass


def _web_status(args: argparse.Namespace) -> int:
    _print(_web_api(args).request("GET", "/api/pi05/status"))
    return 0


def _web_probe(args: argparse.Namespace) -> int:
    _print(_web_api(args).request("POST", "/api/pi05/probe", {}))
    return 0


def _reconnect(args: argparse.Namespace) -> int:
    _print(
        _web_api(args).request(
            "POST",
            "/api/pi05/reconnect",
            {"host": args.policy_host, "port": args.policy_port},
        )
    )
    return 0


def _disconnect(args: argparse.Namespace) -> int:
    _print(_web_api(args).request("POST", "/api/pi05/disconnect", {}))
    return 0


def _dry_run(args: argparse.Namespace) -> int:
    api = _web_api(args)
    prompt = _prompt(args)
    with _control_lease(args, api) as (lease, automatic, lease_seconds):
        try:
            if automatic:
                with _HeartbeatGuard(api, lease, lease_seconds):
                    result = api.request(
                        "POST",
                        "/api/pi05/dry-run",
                        {"prompt": prompt},
                        lease=lease,
                    )
            else:
                result = api.request(
                    "POST",
                    "/api/pi05/dry-run",
                    {"prompt": prompt},
                    lease=lease,
                )
            if automatic:
                result = dict(result)
                result["auto_takeover_note"] = AUTO_DRY_RUN_NOTE
            _print(result)
        except BaseException:
            if automatic:
                # Releasing takeover also stops Pi0.5 on the server, but make
                # the safety order explicit when this operation is abnormal.
                _best_effort_pi05_stop(api)
            raise
    return 0


def _stop(args: argparse.Namespace) -> int:
    _print(_web_api(args).request("POST", "/api/pi05/stop", {}))
    return 0


def _run(args: argparse.Namespace) -> int:
    if not args.confirm_motion:
        raise CliError(
            "motion is disabled; repeat with --confirm-motion after checking the physical E-stop"
    )
    api = _web_api(args)
    prompt = _prompt(args)
    with _control_lease(args, api) as (lease, automatic, lease_seconds):
        last_summary: tuple[Any, ...] | None = None
        try:
            # Auto takeover must keep the same lease alive through the whole
            # prepare gate.  This sequence intentionally contains no robot
            # init/deinit route: an uninitialized robot is rejected by start.
            if automatic:
                with _HeartbeatGuard(api, lease, lease_seconds):
                    if args.prepare:
                        reconnected = api.request(
                            "POST",
                            "/api/pi05/reconnect",
                            {
                                "host": args.policy_host,
                                "port": args.policy_port,
                            },
                        )
                        prepared = api.request(
                            "POST",
                            "/api/pi05/dry-run",
                            {"prompt": prompt},
                            lease=lease,
                        )
                        _print(
                            {
                                "prepare": {
                                    "reconnect": reconnected,
                                    "dry_run": prepared,
                                }
                            }
                        )
                    started = _start_run(api, args, lease, prompt)
            else:
                if args.prepare:
                    reconnected = api.request(
                        "POST",
                        "/api/pi05/reconnect",
                        {
                            "host": args.policy_host,
                            "port": args.policy_port,
                        },
                    )
                    prepared = api.request(
                        "POST",
                        "/api/pi05/dry-run",
                        {"prompt": prompt},
                        lease=lease,
                    )
                    _print(
                        {
                            "prepare": {
                                "reconnect": reconnected,
                                "dry_run": prepared,
                            }
                        }
                    )
                started = _start_run(api, args, lease, prompt)

            _print(started)
            while True:
                # A single heartbeat or status failure is terminal; do not silently
                # retry and continue motion under uncertain operator ownership.
                api.request("POST", "/api/heartbeat", {}, lease=lease)
                status = api.request("GET", "/api/pi05/status")
                summary = (
                    status.get("phase"),
                    status.get("executed_steps"),
                    status.get("fault"),
                )
                if summary != last_summary:
                    _print(status)
                    last_summary = summary
                phase = str(status.get("phase", ""))
                if phase == "fault":
                    _best_effort_pi05_stop(api)
                    return 2
                if phase == "idle":
                    return 0
                if phase not in ("running", "stopping"):
                    raise CliError(
                        f"unexpected Pi0.5 phase {phase!r}; requesting software stop"
                    )
                time.sleep(0.8)
        except KeyboardInterrupt:
            print(
                "\nCtrl+C: requesting Pi0.5 software stop (not robot_deinit)",
                file=sys.stderr,
            )
            api.request("POST", "/api/pi05/stop", {})
            return 130
        except BaseException:
            _best_effort_pi05_stop(api)
            raise


def _start_run(
    api: WebApi,
    args: argparse.Namespace,
    lease: str,
    prompt: str,
) -> dict[str, Any]:
    """Start inside the caller's STOP/release cleanup boundary."""

    return api.request(
        "POST",
        "/api/pi05/start",
        {
            "prompt": prompt,
            "steps_per_chunk": args.steps_per_chunk,
            "control_rate_hz": args.control_rate_hz,
            "joint_speed_deg_s": args.joint_speed_deg_s,
            "confirmation": REQUIRED_CONFIRMATION,
        },
        lease=lease,
    )


def _add_web_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--web-url", default="http://172.16.18.43:8080")
    parser.add_argument("--token", default=os.environ.get("H1_WEB_CONTROL_TOKEN"))
    parser.add_argument("--timeout", type=float, default=20.0)


def _add_prompt_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt")
    group.add_argument("--prompt-file")
    lease_group = parser.add_mutually_exclusive_group()
    lease_group.add_argument(
        "--lease",
        help="live web control lease; or set H1_CONTROL_LEASE",
    )
    lease_group.add_argument(
        "--auto-takeover",
        action="store_true",
        help=(
            "explicitly acquire, heartbeat, and finally release an 8080 "
            "control lease; never initializes or deinitializes the robot"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ZERITH Pi0.5 web-integrated executor CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    metadata = subparsers.add_parser("metadata", help="health + metadata only; no robot access")
    metadata.add_argument("--policy-host", default="192.168.1.154")
    metadata.add_argument("--policy-port", type=int, default=9973)
    metadata.add_argument("--timeout", type=float, default=5.0)
    metadata.add_argument("--inference-timeout", type=float, default=10.0)
    metadata.set_defaults(handler=_metadata)

    status = subparsers.add_parser("status", help="read the 8080 executor state")
    _add_web_options(status)
    status.set_defaults(handler=_web_status)

    probe = subparsers.add_parser("probe", help="ask 8080 to run health + metadata")
    _add_web_options(probe)
    probe.set_defaults(handler=_web_probe)

    reconnect = subparsers.add_parser(
        "reconnect",
        help="ask the 8080 executor to select and validate a policy endpoint",
    )
    _add_web_options(reconnect)
    reconnect.add_argument(
        "--host",
        "--policy-host",
        dest="policy_host",
        default=DEFAULT_HOST,
    )
    reconnect.add_argument(
        "--port",
        "--policy-port",
        dest="policy_port",
        type=int,
        default=DEFAULT_PORT,
    )
    reconnect.set_defaults(handler=_reconnect)

    disconnect = subparsers.add_parser(
        "disconnect",
        help="ask the 8080 executor to stop and close its policy transport",
    )
    _add_web_options(disconnect)
    disconnect.set_defaults(handler=_disconnect)

    dry_run = subparsers.add_parser("dry-run", help="one real-camera inference with zero setters")
    _add_web_options(dry_run)
    _add_prompt_options(dry_run)
    dry_run.set_defaults(handler=_dry_run)

    stop = subparsers.add_parser("stop", help="global Pi0.5 software stop; no lease required")
    _add_web_options(stop)
    stop.set_defaults(handler=_stop)

    run = subparsers.add_parser(
        "run",
        help="continuous chunked execution through the 8080 SDK owner",
    )
    _add_web_options(run)
    _add_prompt_options(run)
    run.add_argument(
        "--steps-per-chunk",
        type=int,
        default=30,
        help="actions consumed from each fixed 50-step server chunk (1..50)",
    )
    run.add_argument(
        "--control-rate-hz",
        type=float,
        default=DEFAULT_CONTROL_RATE_HZ,
    )
    run.add_argument(
        "--joint-speed-deg-s",
        type=float,
        default=DEFAULT_JOINT_SPEED_DEG_S,
        help=(
            "maximum target slew for each of the 14 arm joints, based on "
            "the previous successful command; default 30 deg/s"
        ),
    )
    run.add_argument("--policy-host", default=DEFAULT_HOST)
    run.add_argument("--policy-port", type=int, default=DEFAULT_PORT)
    run.add_argument(
        "--prepare",
        action="store_true",
        help=(
            "on the same lease, reconnect to --policy-host/--policy-port and "
            "run the required dry-run before start"
        ),
    )
    run.add_argument(
        "--confirm-motion",
        action="store_true",
        help="confirm physical E-stop readiness and permit real arm/gripper/lift motion",
    )
    run.set_defaults(handler=_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if (
        not isinstance(args.timeout, (int, float))
        or not math.isfinite(args.timeout)
        or args.timeout <= 0
    ):
        raise SystemExit("--timeout must be positive")
    steps_per_chunk = getattr(args, "steps_per_chunk", 1)
    if (
        isinstance(steps_per_chunk, bool)
        or not isinstance(steps_per_chunk, int)
        or not 1 <= steps_per_chunk <= ACTION_HORIZON
    ):
        raise SystemExit(
            f"--steps-per-chunk must be in 1..{ACTION_HORIZON}"
        )
    control_rate_hz = getattr(args, "control_rate_hz", DEFAULT_CONTROL_RATE_HZ)
    if (
        not isinstance(control_rate_hz, (int, float))
        or not math.isfinite(control_rate_hz)
        or control_rate_hz <= 0
    ):
        raise SystemExit("--control-rate-hz must be positive and finite")
    joint_speed_deg_s = getattr(
        args,
        "joint_speed_deg_s",
        DEFAULT_JOINT_SPEED_DEG_S,
    )
    if (
        not isinstance(joint_speed_deg_s, (int, float))
        or not math.isfinite(joint_speed_deg_s)
        or joint_speed_deg_s <= 0
    ):
        raise SystemExit("--joint-speed-deg-s must be positive and finite")
    policy_port = getattr(args, "policy_port", DEFAULT_PORT)
    if (
        isinstance(policy_port, bool)
        or not isinstance(policy_port, int)
        or not 1 <= policy_port <= 65535
    ):
        raise SystemExit("--port/--policy-port must be in 1..65535")
    try:
        return int(args.handler(args))
    except (CliError, Pi05ProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
