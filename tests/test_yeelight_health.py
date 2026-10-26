"""Bulb health checks use read-only replies and never infer OFF from silence."""

import copy
from datetime import datetime, timedelta
import socket
import threading
import types
import unittest
from unittest import mock

from PyQt6.QtWidgets import QApplication

import yeelight_health as health
import yeelight_pc_companion as app
from tests.test_ui import UiTestCase


def device(name="Desk Lamp", ip="192.168.1.50", enabled=True):
    return {"id": "manual:health-" + ip, "name": name, "ip": ip, "enabled": enabled}


class TestPowerProbe(unittest.TestCase):
    def probe(self, payload, peer="192.168.1.50", entry=None):
        sock = mock.Mock()
        sock.recvfrom.return_value = (payload, (peer, 1982))
        self.addCleanup(sock.close.assert_called_once)
        with mock.patch("yeelight.main.send_discovery_packet", return_value=sock) as send:
            result = health.probe_device_power(entry or device())
        send.assert_called_once_with(
            timeout=health.HEALTH_PROBE_TIMEOUT_SECONDS, ip_address="192.168.1.50"
        )
        return result

    def test_on_and_off_are_confirmed_from_the_reply(self):
        for power in ("on", "off"):
            with self.subTest(power=power):
                result = self.probe(("HTTP/1.1 200 OK\r\npower: " + power + "\r\n").encode())
                self.assertEqual(power, result.state)

    def test_silence_is_not_an_off_bulb(self):
        sock = mock.Mock()
        sock.recvfrom.side_effect = socket.timeout()
        with mock.patch("yeelight.main.send_discovery_packet", return_value=sock):
            result = health.probe_device_power(device())
        self.assertEqual(health.NOT_RESPONDING, result.state)
        sock.close.assert_called_once()

    def test_a_network_error_is_isolated(self):
        with mock.patch("yeelight.main.send_discovery_packet", side_effect=OSError("unavailable")):
            result = health.probe_device_power(device())
        self.assertEqual(health.NOT_RESPONDING, result.state)

    def test_missing_or_malformed_power_is_unknown(self):
        for payload in (b"HTTP/1.1 200 OK\r\nmodel: color\r\n", b"invalid: data: extra\r\n"):
            with self.subTest(payload=payload):
                self.assertEqual(health.UNKNOWN, self.probe(payload).state)

    def test_a_different_device_at_the_address_is_not_used_for_recovery(self):
        entry = device()
        entry["id"] = "yeelight:0x1234"
        result = self.probe(b"HTTP/1.1 200 OK\r\nid: 0x5678\r\npower: off\r\n", entry=entry)
        self.assertEqual(health.UNKNOWN, result.state)

    def test_unrelated_datagrams_cannot_supply_the_power_state(self):
        sock = mock.Mock()
        sock.recvfrom.side_effect = [
            (b"HTTP/1.1 200 OK\r\npower: off\r\n", ("192.168.1.99", 1982)),
            socket.timeout(),
        ]
        with mock.patch("yeelight.main.send_discovery_packet", return_value=sock):
            result = health.probe_device_power(device())
        self.assertEqual(health.NOT_RESPONDING, result.state)
        sock.close.assert_called_once()

    def test_unrelated_packets_cannot_extend_the_deadline(self):
        sock = mock.Mock()
        sock.recvfrom.return_value = (b"power: off\r\n", ("192.168.1.99", 1982))
        with mock.patch("yeelight.main.send_discovery_packet", return_value=sock), mock.patch.object(
            health.time, "monotonic", side_effect=[0.0, 0.1, 3.0]
        ):
            result = health.probe_device_power(device())
        self.assertEqual(health.NOT_RESPONDING, result.state)
        self.assertEqual(1, sock.recvfrom.call_count)
        sock.close.assert_called_once()


class TestSolarDarkState(unittest.TestCase):
    def test_night_and_both_solar_buffers_keep_the_existing_rule(self):
        now = datetime(2026, 10, 25, 12, 0)
        cases = [
            (2, 14, -10, True),
            (18, 6, -6, False),
            (12.5, 0.5, -11.5, True),
            (23.5, 11.5, -0.5, True),
        ]
        for sunrise_hours, sunset_hours, previous_hours, expected in cases:
            with self.subTest(sunrise=sunrise_hours, sunset=sunset_hours):
                observer = mock.Mock()
                observer.next_rising.return_value.datetime.return_value = now + timedelta(hours=sunrise_hours)
                observer.next_setting.return_value.datetime.return_value = now + timedelta(hours=sunset_hours)
                observer.previous_rising.return_value.datetime.return_value = now + timedelta(hours=previous_hours)
                date = types.SimpleNamespace(datetime=lambda: now)
                config = {"location": {"latitude": "44", "longitude": "20", "elevation": "75", "light_buffer_hours": "1"}}
                with mock.patch("ephem.Observer", return_value=observer), mock.patch.object(app, "datetime") as clock:
                    clock.now.return_value = date
                    self.assertEqual(expected, app.calculate_solar_dark_state(config))


class TestPeriodicBulbHealth(UiTestCase):
    def active_window(self, dark=True):
        config = self.default_config()
        config["integrations"]["yeelight_connector"]["enabled"] = False
        window = self.window(config)
        window.solar_thread.stop()
        QApplication.processEvents()
        window._automation_paused = False
        window._last_solar_is_dark = dark
        self._patch(app, "calculate_solar_dark_state", lambda config: dark)
        self.syncs = []
        self._patch(window, "trigger_resume", lambda **kwargs: self.syncs.append(True))
        return window

    def check(self, window, states, before_delivery=None):
        def probe(entry):
            return health.DeviceHealth(
                entry["id"], entry["name"], entry["ip"], states[entry["ip"]]
            )

        with mock.patch.object(app, "probe_device_power", side_effect=probe) as queried:
            window._start_yeelight_health_check()
            worker = window._yeelight_health_thread
            self.assertIsNotNone(worker)
            self.assertTrue(worker.wait(2000), "the health worker did not finish")
            if before_delivery:
                before_delivery()
            QApplication.processEvents()
        return queried

    def test_periodic_timer_uses_thirty_minutes(self):
        window = self.window()
        self.assertEqual(30 * 60 * 1000, window._yeelight_health_timer.interval())
        self.assertTrue(window._yeelight_health_timer.isActive())

    def test_the_timer_starts_the_real_background_worker(self):
        window = self.active_window()
        result = health.DeviceHealth("manual:health", "Desk Lamp", "192.168.1.50", "on")
        with mock.patch.object(app, "probe_device_power", return_value=result) as probe:
            window._yeelight_health_timer.timeout.emit()
            self.assertTrue(window._yeelight_health_thread.wait(2000))
            QApplication.processEvents()
        self.assertEqual(1, probe.call_count)
        self.assertIn("On", window.lbl_yeelight_health_details.text())

    def test_a_probe_error_does_not_prevent_checking_the_next_bulb(self):
        window = self.active_window()
        config = copy.deepcopy(window.config)
        config["lights"]["devices"][1]["enabled"] = True
        self.save_config(config)
        result = health.DeviceHealth("manual:health", "Lightstrip", "192.168.1.51", "on")
        with mock.patch.object(app, "probe_device_power", side_effect=[ValueError(), result]):
            window._start_yeelight_health_check()
            self.assertTrue(window._yeelight_health_thread.wait(2000))
            QApplication.processEvents()
        self.assertIn("Unknown", window.lbl_yeelight_health_details.text())
        self.assertIn("Lightstrip (192.168.1.51): On", window.lbl_yeelight_health_details.text())

    def test_cancelling_an_inflight_check_stops_before_the_next_bulb(self):
        window = self.active_window()
        config = copy.deepcopy(window.config)
        config["lights"]["devices"][1]["enabled"] = True
        self.save_config(config)
        started = threading.Event()
        release = threading.Event()

        def probe(entry):
            started.set()
            release.wait(2)
            return health.DeviceHealth(entry["id"], entry["name"], entry["ip"], "off")

        with mock.patch.object(app, "probe_device_power", side_effect=probe) as queried:
            window._start_yeelight_health_check()
            worker = window._yeelight_health_thread
            try:
                self.assertTrue(started.wait(1))
                window._cancel_yeelight_health_check()
            finally:
                release.set()
                self.assertTrue(worker.wait(2000))
                QApplication.processEvents()
        self.assertEqual(1, queried.call_count)
        self.assertEqual([], self.syncs)

    def test_confirmed_off_at_night_runs_sync_even_without_the_connector(self):
        window = self.active_window()
        queried = self.check(window, {"192.168.1.50": "off"})
        self.assertEqual(1, queried.call_count, "disabled bulbs must not be queried")
        self.assertEqual([True], self.syncs)
        self.assertIn("Off", window.lbl_yeelight_health_details.text())

    def test_healthy_bulbs_do_not_run_sync(self):
        window = self.active_window()
        self.check(window, {"192.168.1.50": "on"})
        self.assertEqual([], self.syncs)
        self.assertIn("On", window.lbl_yeelight_health_details.text())

    def test_a_bulb_without_power_only_gets_a_ui_warning(self):
        window = self.active_window()
        self.check(window, {"192.168.1.50": health.NOT_RESPONDING})
        self.assertEqual([], self.syncs)
        for text in ("Desk Lamp", "192.168.1.50", "Not responding", "power", "network"):
            self.assertIn(text, window.lbl_yeelight_health_details.text())
            self.assertIn(text, window.lbl_yeelight_device_health.text())

    def test_an_unresponsive_peer_does_not_prevent_recovery_of_an_off_bulb(self):
        window = self.active_window()
        config = copy.deepcopy(window.config)
        config["lights"]["devices"][1]["enabled"] = True
        self.save_config(config)
        self.check(window, {"192.168.1.50": health.NOT_RESPONDING, "192.168.1.51": "off"})
        self.assertEqual([True], self.syncs)
        self.assertIn("Not responding", window.lbl_yeelight_health_details.text())

    def test_off_during_the_day_is_not_a_failure(self):
        window = self.active_window(dark=False)
        self.check(window, {"192.168.1.50": "off"})
        self.assertEqual([], self.syncs)

    def test_unknown_power_does_not_trigger_recovery(self):
        window = self.active_window()
        self.check(window, {"192.168.1.50": health.UNKNOWN})
        self.assertEqual([], self.syncs)
        self.assertIn("Unknown", window.lbl_yeelight_health_details.text())

    def test_a_followup_does_not_create_a_sync_loop(self):
        window = self.active_window()
        self.check(window, {"192.168.1.50": "off"})
        self.check(window, {"192.168.1.50": "off"})
        self.assertEqual([True], self.syncs)
        window._last_yeelight_recovery_at -= 30 * 60 + 1
        self.check(window, {"192.168.1.50": "off"})
        self.assertEqual([True, True], self.syncs)

    def test_a_later_success_clears_the_warning(self):
        window = self.active_window()
        self.check(window, {"192.168.1.50": health.NOT_RESPONDING})
        self.check(window, {"192.168.1.50": "on"})
        self.assertNotIn("Not responding", window.lbl_yeelight_health_details.text())
        self.assertIn("On", window.lbl_yeelight_health_details.text())

    def test_sleeping_paused_and_restoring_do_not_start_a_probe(self):
        window = self.active_window()
        for name in ("_system_sleeping", "_sleep_transition_active", "_automation_paused"):
            with self.subTest(name=name):
                setattr(window, name, True)
                with mock.patch.object(app, "YeelightHealthCheckThread") as worker:
                    window._start_yeelight_health_check()
                    worker.assert_not_called()
                setattr(window, name, False)
        window.restore_thread = mock.Mock()
        window.restore_thread.isRunning.return_value = True
        with mock.patch.object(app, "YeelightHealthCheckThread") as worker:
            window._start_yeelight_health_check()
            worker.assert_not_called()

    def test_sleep_before_result_delivery_prevents_sync(self):
        window = self.active_window()
        self.check(window, {"192.168.1.50": "off"}, lambda: setattr(window, "_system_sleeping", True))
        self.assertEqual([], self.syncs)

    def test_a_result_from_before_a_resume_is_ignored(self):
        window = self.active_window()
        self.check(window, {"192.168.1.50": "off"}, window._cancel_yeelight_health_check)
        self.assertEqual([], self.syncs)

    def test_sunrise_before_result_delivery_prevents_sync(self):
        window = self.active_window()
        self.check(window, {"192.168.1.50": "off"}, lambda: setattr(window, "_last_solar_is_dark", False))
        self.assertEqual([], self.syncs)

    def test_changed_configuration_invalidates_a_result(self):
        window = self.active_window()
        config = copy.deepcopy(window.config)
        config["lights"]["devices"][0]["enabled"] = False
        self.check(window, {"192.168.1.50": "off"}, lambda: self.save_config(config))
        self.assertEqual([], self.syncs)
        self.assertNotIn("Desk Lamp", window.lbl_yeelight_health_details.text())

    def test_fresh_solar_calculation_prevents_sync_after_sunrise(self):
        window = self.active_window()
        self._patch(app, "calculate_solar_dark_state", lambda config: False)
        self.check(window, {"192.168.1.50": "off"})
        self.assertEqual([], self.syncs)

    def test_a_failed_solar_calculation_cannot_trigger_sync(self):
        window = self.active_window()
        self._patch(app, "calculate_solar_dark_state", mock.Mock(side_effect=ValueError("unavailable")))
        self.check(window, {"192.168.1.50": "off"})
        self.assertEqual([], self.syncs)

    def test_disabled_restoration_does_not_run_checks(self):
        window = self.active_window()
        config = copy.deepcopy(window.config)
        config["automation"]["restore_apps_on_wake"] = False
        self.save_config(config)
        with mock.patch.object(app, "YeelightHealthCheckThread") as worker:
            window._start_yeelight_health_check()
            worker.assert_not_called()


class TestHealthRecoveryPowerBoundary(UiTestCase):
    def test_automatic_sync_cannot_clear_an_active_sleep(self):
        window = self.window()
        window._automation_paused = False
        window._system_sleeping = True
        with mock.patch.object(app, "RestoreEngineThread") as worker:
            window.trigger_resume(automatic=True)
        worker.assert_not_called()
        self.assertTrue(window._system_sleeping)

    def test_sleep_during_recovery_setup_prevents_the_worker_start(self):
        window = self.window()
        window._automation_paused = False
        worker = mock.Mock()
        worker.isRunning.return_value = False

        def create_worker(manager):
            window._system_sleeping = True
            window._sleep_transition_active = True
            return worker

        with mock.patch.object(app, "RestoreEngineThread", side_effect=create_worker):
            window.trigger_resume(automatic=True)
        worker.start.assert_not_called()
        self.assertFalse(worker.running)
        self.assertTrue(window._system_sleeping)

    def test_a_restore_completion_schedules_a_followup_before_qthread_finishes(self):
        window = self.window()
        window._automation_paused = False
        window.restore_thread = mock.Mock()
        window.restore_thread.isRunning.return_value = True
        window.on_resume_completed()
        self.assertEqual(health.HEALTH_CHECK_SETTLE_MS, window._yeelight_health_timer.interval())

    def test_cancellation_does_not_wait_for_the_probe_on_the_suspend_path(self):
        window = self.window()
        health_worker = mock.Mock()
        window._yeelight_health_thread = health_worker
        restore = types.SimpleNamespace(isRunning=lambda: False, running=True)
        window.restore_thread = restore
        with mock.patch.object(app, "terminate_processes_win32", return_value=1), mock.patch.object(
            app, "fire_and_forget_off_devices", return_value={}
        ), mock.patch.object(app.time, "sleep"), mock.patch.object(app, "get_running_processes_win32", return_value=set()):
            window._execute_suspend_actions()
        health_worker.requestInterruption.assert_called_once()
        health_worker.wait.assert_not_called()
        self.assertFalse(restore.running, "a not-yet-started restore must also be cancelled")
        window._yeelight_health_thread = None


if __name__ == "__main__":
    unittest.main()
