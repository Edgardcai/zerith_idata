"""RTC v1 contract and clock/buffer primitives; never imports a robot SDK."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, asdict
import math
from typing import Any, Mapping
import numpy as np

from .pi05_protocol import ACTION_ORDER, STATE_ORDER, ProtocolValidationError


@dataclass(frozen=True)
class RtcConfig:
    trigger_index: int = 15
    delay_initial_steps: int = 6
    safety_reserve_steps: int = 2
    num_denoising_steps: int = 10
    max_guidance_weight: float = 5.0
    delay_window: int = 20
    delay_margin_steps: int = 1
    max_observation_age_s: float = 0.25
    warmup_timeout_s: float = 120.0
    dry_run_chunks: int = 3

    @classmethod
    def parse(cls, value=None):
        if value is None:
            return cls()
        if not isinstance(value, Mapping) or set(value) - set(cls.__dataclass_fields__):
            raise ProtocolValidationError("Unknown RTC configuration fields")
        try:
            result = cls(**value)
        except TypeError as exc:
            raise ProtocolValidationError(str(exc)) from exc
        limits = {
            "trigger_index": (1, 49),
            "delay_initial_steps": (0, 24),
            "safety_reserve_steps": (0, 49),
            "num_denoising_steps": (1, 50),
            "delay_window": (1, 1000),
            "delay_margin_steps": (0, 49),
            "dry_run_chunks": (2, 20),
        }
        for name, (lo, hi) in limits.items():
            v = getattr(result, name)
            if type(v) is not int or not lo <= v <= hi:
                raise ProtocolValidationError(
                    f"rtc_options.{name} must be an integer in {lo}..{hi}"
                )
        for name in (
            "max_guidance_weight",
            "max_observation_age_s",
            "warmup_timeout_s",
        ):
            v = getattr(result, name)
            if (
                isinstance(v, bool)
                or not isinstance(v, (int, float))
                or not math.isfinite(v)
            ):
                raise ProtocolValidationError(f"rtc_options.{name} must be finite")
        if not 0 <= result.max_guidance_weight <= 5:
            raise ProtocolValidationError("max_guidance_weight must be in 0..5")
        if (
            not 0 < result.max_observation_age_s <= 2
            or not 0 < result.warmup_timeout_s <= 600
        ):
            raise ProtocolValidationError("Invalid RTC observation age/warmup timeout")
        result.check_schedule(result.trigger_index, result.delay_initial_steps)
        return result

    def check_schedule(self, s: int, d: int):
        if not (
            0 < s < 50
            and 0 <= d <= s
            and s <= 50 - d
            and s + d + self.safety_reserve_steps <= 50
        ):
            raise ProtocolValidationError(
                f"RTC latency/configuration infeasible: s={s}, d={d}, reserve={self.safety_reserve_steps}; hold and reconfigure"
            )

    def as_dict(self):
        return asdict(self)


def scheduling_options(mode, options=None):
    if mode not in ("sync", "rtc"):
        raise ProtocolValidationError("scheduling_mode must be sync or rtc")
    if mode == "sync" and options:
        raise ProtocolValidationError("sync must not include RTC history/configuration")
    return RtcConfig.parse(options)


def validate_rtc_metadata(value):
    if not isinstance(value, Mapping):
        raise ProtocolValidationError("RTC metadata must be an object")
    expected = {
        "rtc_supported": True,
        "rtc_protocol_version": 1,
        "action_horizon": 50,
        "wire_state_dim": 23,
        "wire_action_dim": 23,
        "model_action_dim": 32,
    }
    for key, expected_value in expected.items():
        if (
            type(value.get(key)) is not type(expected_value)
            or value[key] != expected_value
        ):
            raise ProtocolValidationError(
                f"RTC metadata {key}: expected {expected_value!r}, got {value.get(key)!r}"
            )
    trained_dim = value.get("rtc_history_trained_dim")
    if (
        type(trained_dim) is not int
        or trained_dim not in (16, 17)
        or value.get("rtc_padding_start") != trained_dim
    ):
        raise ProtocolValidationError(
            "RTC supports Zerith trained dimensions 16 or 17, with matching padding_start"
        )
    for key in ("model_policy_dim", "policy_dim"):
        if key in value and value[key] != trained_dim:
            raise ProtocolValidationError(f"RTC {key} contradicts trained dimensions")
    for key in ("input_gripper_binary", "output_gripper_binary"):
        if type(value.get(key)) is not bool:
            raise ProtocolValidationError(f"RTC metadata missing boolean {key}")
    if (
        not isinstance(value.get("rtc_action_space_id"), str)
        or not value["rtc_action_space_id"]
    ):
        raise ProtocolValidationError("RTC metadata missing rtc_action_space_id")
    # Orders are required: do not silently run another robot's convention.
    for key, expected_order in (
        ("state_order", STATE_ORDER),
        ("action_order", ACTION_ORDER),
    ):
        aliases = {
            "lift.target_height": "lift.height",
            "base.linear_velocity": "speed.linear",
            "base.angular_velocity": "speed.angular",
        }
        if tuple(aliases.get(v, v) for v in value.get(key, ())) != tuple(
            expected_order
        ):
            raise ProtocolValidationError(
                f"RTC metadata {key} differs from the explicit Zerith 23-D adapter"
            )
    return dict(value)


def envelope(
    config,
    session_id,
    request_id,
    episode_epoch=0,
    phase_epoch=0,
    *,
    s=0,
    d=None,
    previous_actions=None,
    previous_chunk_model=None,
):
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolValidationError("RTC requires non-empty session_id")
    for name, v in (
        ("request_id", request_id),
        ("episode_epoch", episode_epoch),
        ("phase_epoch", phase_epoch),
        ("executed_steps", s),
    ):
        if type(v) is not int or v < 0:
            raise ProtocolValidationError(f"RTC {name} must be a nonnegative integer")
    d = config.delay_initial_steps if d is None else d
    if type(d) is not int or d < 0:
        raise ProtocolValidationError("RTC delay must be a nonnegative integer")
    if previous_actions is not None and previous_chunk_model is not None:
        raise ProtocolValidationError("RTC history formats are mutually exclusive")
    has_history = previous_actions is not None or previous_chunk_model is not None
    if has_history:
        config.check_schedule(s, d)
    elif s != 0:
        raise ProtocolValidationError("RTC bootstrap executed_steps must be zero")
    else:
        config.check_schedule(config.trigger_index, d)
    rtc = dict(
        enabled=True,
        protocol_version=1,
        episode_epoch=episode_epoch,
        phase_epoch=phase_epoch,
        executed_steps=s,
        estimated_delay_steps=d,
        execution_horizon=s if has_history else config.trigger_index,
        safety_reserve_steps=config.safety_reserve_steps,
        num_denoising_steps=config.num_denoising_steps,
        max_guidance_weight=config.max_guidance_weight,
    )
    for key, v, dim in (
        ("previous_actions", previous_actions, 23),
        ("previous_chunk_model", previous_chunk_model, 32),
    ):
        if v is not None:
            a = np.asarray(v, dtype=np.float64)
            if a.shape != (50 - s, dim) or not np.isfinite(a).all():
                raise ProtocolValidationError(
                    f"RTC {key} must have finite shape {(50-s,dim)}"
                )
            if dim == 32 and np.any(a[:, 17:] != 0):
                raise ProtocolValidationError("RTC model history padding must be zero")
            rtc[key] = a.tolist()
    return dict(session_id=session_id, request_id=request_id, rtc=rtc)


def validate_response(raw, identifiers, metadata):
    if raw.get("type") != "action_chunk":
        raise ProtocolValidationError(
            f"RTC expected action_chunk: {raw.get('error', raw.get('type'))}"
        )
    for key in ("session_id", "request_id"):
        if (
            type(raw.get(key)) is not type(identifiers[key])
            or raw.get(key) != identifiers[key]
        ):
            raise ProtocolValidationError(f"Stale/mismatched RTC {key}")
    for key in ("episode_epoch", "phase_epoch"):
        if type(raw.get(key)) is not int or raw.get(key) != identifiers["rtc"][key]:
            raise ProtocolValidationError(f"Stale/mismatched RTC {key}")
    rtc = raw.get("rtc")
    if (
        not isinstance(rtc, Mapping)
        or rtc.get("action_time_origin") != "request_observation"
        or rtc.get("client_must_discard_elapsed_prefix") is not True
    ):
        raise ProtocolValidationError(
            "RTC response lacks observation-time/prefix-discard contract"
        )
    if type(rtc.get("guided")) is not bool:
        raise ProtocolValidationError("RTC response missing guided flag")
    guided = any(
        k in identifiers["rtc"] for k in ("previous_actions", "previous_chunk_model")
    )
    if rtc["guided"] != guided:
        raise ProtocolValidationError(
            "RTC server did not use the requested bootstrap/guided branch"
        )
    if (
        "rtc_action_space_id" in rtc
        and rtc["rtc_action_space_id"] != metadata["rtc_action_space_id"]
    ):
        raise ProtocolValidationError(
            "RTC action space changed; reconnect and bootstrap"
        )
    model = np.asarray(raw.get("actions_model"), dtype=np.float64)
    if (
        model.shape != (50, 32)
        or not np.isfinite(model).all()
        or np.any(model[:, metadata["rtc_padding_start"] :] != 0)
    ):
        raise ProtocolValidationError(
            "RTC actions_model must be finite (50,32) with zero metadata-declared padding"
        )
    return model.copy()


class DelayEstimator:
    """Rolling maximum of full observation-to-control-handoff latency + margin."""

    def __init__(self, config):
        self.config = config
        self.samples = deque(maxlen=config.delay_window)

    @property
    def steps(self):
        return max(
            self.config.delay_initial_steps,
            max(self.samples, default=0) + self.config.delay_margin_steps,
        )

    def observe(self, elapsed_steps):
        self.samples.append(int(elapsed_steps))


class RtcBuffer:
    """Full chunk with an original cursor (including discarded elapsed prefix)."""

    def __init__(self):
        self.actions = None
        self.actions_model = None
        self.cursor = 0
        self.generation = 0
        self.last_tick = None

    def handoff(
        self, actions, model, *, observation_tick, handoff_tick, expected_generation
    ):
        if self.generation != expected_generation:
            raise ProtocolValidationError("Stale RTC buffer generation")
        k = handoff_tick - observation_tick
        if not 0 <= k < 50:
            raise ProtocolValidationError(
                f"RTC response exhausted before handoff: k={k}"
            )
        self.actions = np.asarray(actions, dtype=np.float64).copy()
        self.actions_model = np.asarray(model, dtype=np.float64).copy()
        if self.actions.shape != (50, 23) or not np.isfinite(self.actions).all():
            raise ProtocolValidationError("Invalid physical RTC chunk")
        self.cursor = k
        self.generation += 1
        self.last_tick = handoff_tick
        return k

    def advance_clock(self, tick):
        missed = 0 if self.last_tick is None else max(0, tick - self.last_tick - 1)
        self.cursor += missed
        self.last_tick = tick
        return missed
