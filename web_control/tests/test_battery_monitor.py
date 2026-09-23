import threading
import time
import unittest

from control.web_control.battery_monitor import BatteryMonitor


class BatteryMonitorTests(unittest.TestCase):
    def test_valid_zero_full_and_charging(self):
        for percent in (0, 20, 62, 100):
            for charging in (False, True):
                self.assertEqual(
                    BatteryMonitor.normalize(dict(fresh=True, percent=percent, charging=charging)),
                    dict(percent=percent, charging=charging),
                )

    def test_invalid_or_stale_readings_are_unknown(self):
        for value in (None, True, "62", -1, 101, float("nan"), float("inf")):
            self.assertIsNone(BatteryMonitor.normalize(dict(fresh=True, percent=value)))
        for data in (None, [], {}, dict(fresh=False, percent=62), dict(percent=62)):
            self.assertIsNone(BatteryMonitor.normalize(data))

    def test_cached_reading_expires(self):
        monitor = BatteryMonitor.__new__(BatteryMonitor)
        monitor._lock = threading.Lock()
        monitor._value = dict(percent=62, charging=False)
        monitor._received = time.monotonic()
        self.assertEqual(monitor.snapshot(), monitor._value)
        monitor._received -= 5
        self.assertIsNone(monitor.snapshot())
