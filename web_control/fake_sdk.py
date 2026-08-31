"""Deterministic in-process H1 SDK simulator for tests and UI demonstrations.

It implements only the interfaces consumed by :mod:`robot_service`.  It never
imports the vendor binary and can never move hardware.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any


class _Modes:
    UNINITIALIZED = 0
    LOW_LEVEL = 1
    HIGH_LEVEL = 2
    GRAVITY_COMPENSATION_LEVEL = 3


class Motor_Control:
    def __init__(self) -> None:
        self.Position = 0.0
        self.Speed = 0.0
        self.Torque = 0.0
        self.KP = -1.0
        self.KD = -1.0


class Motor_Information:
    def __init__(self) -> None:
        self.Position_Actual = 0.0
        self.Speed_Actual = 0.0
        self.Torque_Actual = 0.0
        self.KP_Actual = -1.0
        self.KD_Actual = -1.0
        self.Error_flag = 0


@dataclass
class _Power:
    status: int = 2
    temperature: int = 28
    soc: int = 80


class FakeH1Robot:
    def __init__(self) -> None:
        self.connected = False
        self.mode = 0
        self.init_state = 0
        self.states = {motor_id: Motor_Information() for motor_id in range(23)}
        self.calls: list[tuple[Any, ...]] = []
        self.fail_next_set = False
        self._lock = threading.RLock()

    def robot_connect(self) -> bool:
        self.calls.append(("robot_connect",))
        self.connected = True
        return True

    def isRobotConnected(self) -> bool:
        return self.connected

    def getCurrentMode(self) -> int:
        return self.mode

    def getInitState(self) -> int:
        return self.init_state

    def switchControlMode(self, mode: int) -> bool:
        self.calls.append(("switchControlMode", int(mode)))
        if self.init_state not in (0, 4):
            return False
        self.mode = int(mode)
        return True

    def robot_init(self) -> bool:
        self.calls.append(("robot_init",))
        if self.mode == 0 or self.init_state not in (0, 4):
            return False
        self.init_state = 2
        return True

    def robot_deinit(self) -> bool:
        self.calls.append(("robot_deinit",))
        if self.init_state != 2:
            return False
        self.init_state = 4
        return True

    def _set_position(self, name: str, motor_id: int, control: Motor_Control) -> bool:
        with self._lock:
            self.calls.append((name, int(motor_id), float(control.Position)))
            if self.fail_next_set:
                self.fail_next_set = False
                return False
            state = self.states[int(motor_id)]
            state.Position_Actual = float(control.Position)
            state.Speed_Actual = float(control.Speed)
            state.Torque_Actual = float(control.Torque)
            state.KP_Actual = float(control.KP)
            state.KD_Actual = float(control.KD)
            return True

    def setArm_low(self, motor_id: int, control: Motor_Control) -> bool:
        return self._set_position("setArm_low", motor_id, control)

    def setWaist_low(self, motor_id: int, control: Motor_Control) -> bool:
        return self._set_position("setWaist_low", motor_id, control)

    def setHead_low(self, motor_id: int, control: Motor_Control) -> bool:
        return self._set_position("setHead_low", motor_id, control)

    def setGripper_low(
        self,
        motor_id: int,
        control: Motor_Control,
        hold_torque: bool = True,
    ) -> bool:
        self.calls.append(("gripper_hold_torque", int(motor_id), bool(hold_torque)))
        return self._set_position("setGripper_low", motor_id, control)

    def setChassis_low(self, motor_id: int, control: Motor_Control) -> bool:
        with self._lock:
            self.calls.append(("setChassis_low", int(motor_id), float(control.Speed)))
            if self.fail_next_set:
                self.fail_next_set = False
                return False
            self.states[int(motor_id)].Speed_Actual = float(control.Speed)
            return True

    def _get(self, motor_id: int):
        return True, self.states[int(motor_id)]

    getChassisState = _get
    getWaistState = _get
    getHeadState = _get
    getArmState = _get
    getGripperState = _get

    def getChassisSpeedState(self):
        left = self.states[0].Speed_Actual
        right = self.states[1].Speed_Actual
        return True, [left, right], [(left + right) / 2.0, right - left]

    def getPowerChargeState(self):
        return True, _Power()


class FakeSdk:
    MotorControlMode = _Modes
    Motor_Control = Motor_Control
    EtherCAT_Motor_Index = int


def make_fake_pair() -> tuple[FakeSdk, FakeH1Robot]:
    return FakeSdk(), FakeH1Robot()


__all__ = ["FakeH1Robot", "FakeSdk", "make_fake_pair"]
