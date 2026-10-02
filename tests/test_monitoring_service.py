"""Lifecycle tests without camera access, real windows, or user settings."""
import queue
import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

import config
from app import TrayApplication
from app_lock import AppLockSignal
from monitoring_service import MonitoringService, MonitoringTransitionError
from user_settings import AppSettings


class CameraOwnershipTests(unittest.TestCase):
    def service(self):
        store = Mock()
        store.load.return_value = Mock()
        return MonitoringService(store)

    def test_failed_stop_prevents_camera_acquisition(self):
        service = self.service()
        worker = Mock()
        worker.is_alive.return_value = True
        service._thread = worker
        service._stop_event = threading.Event()
        with self.assertRaises(MonitoringTransitionError):
            service.acquire_camera(object())
        self.assertTrue(service._stop_event.is_set())
        self.assertIsNone(service._camera_owner)
        self.assertEqual(service.status_snapshot()[0], "error")

    def test_exclusive_owner_blocks_restart_until_matching_release(self):
        service = self.service()
        started = threading.Event()
        def monitor(*, stop_event, **kwargs):
            started.set()
            stop_event.wait(3)
            return 0
        with patch("monitoring_service.monitoring.run", side_effect=monitor) as run:
            try:
                self.assertTrue(service._transition(True, False))
                self.assertTrue(started.wait(1))
                owner = object()
                service.acquire_camera(owner)
                self.assertFalse(service.is_running())
                self.assertFalse(service._transition(True, True))
                service.start_async()
                service.restart_async()
                self.assertEqual(run.call_count, 1)
                service.release_camera(object())
                self.assertFalse(service._transition(True, False))
                service.release_camera(owner)
                started.clear()
                self.assertTrue(service._transition(True, False))
                self.assertTrue(started.wait(1))
                self.assertEqual(run.call_count, 2)
            finally:
                service.shutdown()

    def test_pending_lock_blocks_another_camera_owner(self):
        service = self.service()
        service.app_lock_signal.request()
        with self.assertRaises(MonitoringTransitionError):
            service.acquire_camera(object())
        self.assertIsNone(service._camera_owner)

    def test_settings_apply_only_after_old_worker_has_exited(self):
        service = self.service()
        worker = Mock()
        worker.is_alive.side_effect = [True, False]
        stop = threading.Event()
        service._thread, service._stop_event = worker, stop
        def apply():
            self.assertTrue(stop.is_set())
            worker.join.assert_called_once()
            self.assertIsNone(service._thread)
        service._settings_store.load.return_value.apply_to_runtime.side_effect = apply
        self.assertTrue(service._transition(False, False, True))
        self.assertEqual(service.status_snapshot()[0], "paused")

    def test_settings_are_not_applied_after_stop_timeout(self):
        service = self.service()
        service._thread = Mock(is_alive=Mock(return_value=True))
        service._stop_event = threading.Event()
        self.assertFalse(service._transition(True, True))
        service._settings_store.load.assert_not_called()

    def test_pause_signals_worker_before_background_transition_runs(self):
        service = self.service()
        service._stop_event = threading.Event()
        with patch("monitoring_service.threading.Thread"):
            service.pause_async()
        self.assertTrue(service._stop_event.is_set())

    def test_late_start_cannot_override_a_newer_pause(self):
        service = self.service()
        queued = []
        def deferred_thread(*, target, args, **kwargs):
            queued.append(lambda: target(*args))
            return Mock()
        with patch("monitoring_service.threading.Thread", side_effect=deferred_thread):
            service.start_async()
            service.pause_async()
        queued[1]()
        queued[0]()
        self.assertFalse(service.is_running())
        service._settings_store.load.assert_not_called()

    def test_old_start_is_invalidated_by_an_exclusive_camera_session(self):
        service = self.service()
        queued = []
        def deferred_thread(*, target, args, **kwargs):
            queued.append(lambda: target(*args))
            return Mock()
        with patch("monitoring_service.threading.Thread", side_effect=deferred_thread):
            service.start_async()
        owner = object()
        service.acquire_camera(owner)
        service.release_camera(owner)
        queued[0]()
        self.assertFalse(service.is_running())
        service._settings_store.load.assert_not_called()

    def test_new_pause_during_join_cancels_the_inflight_restart(self):
        service = self.service()
        worker = Mock()
        worker.is_alive.side_effect = [True, False]
        service._thread = worker
        service._stop_event = threading.Event()
        generation = service._new_request(True, True)
        worker.join.side_effect = lambda **kwargs: service._new_request(False)
        self.assertFalse(service._transition(True, True, request_generation=generation))
        service._settings_store.load.assert_not_called()
        self.assertFalse(service.is_running())

    def test_camera_pause_preserves_intent_but_explicit_pause_clears_it(self):
        service = self.service()
        self.assertFalse(service.should_resume_monitoring())
        with patch("monitoring_service.threading.Thread"):
            service.start_async()
        self.assertTrue(service.should_resume_monitoring())
        owner = object()
        service.acquire_camera(owner)
        self.assertFalse(service.is_running())
        self.assertTrue(service.should_resume_monitoring())
        service.release_camera(owner)
        self.assertTrue(service.should_resume_monitoring())
        service.pause_blocking()
        self.assertFalse(service.should_resume_monitoring())
        service.acquire_camera(owner)
        service.release_camera(owner)
        self.assertFalse(service.should_resume_monitoring())
        with patch("monitoring_service.threading.Thread"):
            service.restart_async()
        self.assertTrue(service.should_resume_monitoring())
        service.shutdown()
        self.assertFalse(service.should_resume_monitoring())

    def test_pending_restart_is_restored_after_registration(self):
        service = self.service()
        application = RegistrationCoordinationTests().application()
        application._service = service
        application._registration_window = Mock()
        application._registration_cancel_button = None
        application._registration_progress = None
        application._registration_status_variable = None
        started = threading.Event()
        queued = []
        def monitor(*, stop_event, **kwargs):
            started.set()
            stop_event.wait(3)
            return 0
        def deferred_thread(*, target, args=(), **kwargs):
            queued.append(lambda: target(*args))
            return Mock()
        with patch("monitoring_service.monitoring.run", side_effect=monitor) as run:
            try:
                service.start_async()
                self.assertTrue(started.wait(1))
                with patch("monitoring_service.threading.Thread", side_effect=deferred_thread):
                    service.restart_async()
                self.assertFalse(service.is_running())
                application._registration_resume_after = service.should_resume_monitoring()
                self.assertTrue(application._registration_resume_after)
                with patch("app.register_face_from_camera", return_value="template"):
                    application._face_registration_worker(AppSettings.defaults())
                self.assertIsNone(service._camera_owner)
                self.assertFalse(queued[0]())
                started.clear()
                with patch("app.messagebox.showinfo"):
                    application._poll_face_registration()
                self.assertTrue(started.wait(1))
                self.assertTrue(service.is_running())
                self.assertEqual(run.call_count, 2)
            finally:
                service.shutdown()

    def test_pending_restart_is_restored_after_manual_lock(self):
        from test_app_lock import ManualLockTests
        service = self.service()
        application = ManualLockTests().application(True)
        application._service = service
        started = threading.Event()
        queued = []
        def monitor(*, stop_event, **kwargs):
            started.set()
            stop_event.wait(3)
            return 0
        def deferred_thread(*, target, args=(), **kwargs):
            queued.append(lambda: target(*args))
            return Mock()
        with patch("monitoring_service.monitoring.run", side_effect=monitor) as run:
            try:
                service.start_async()
                self.assertTrue(started.wait(1))
                with patch("monitoring_service.threading.Thread", side_effect=deferred_thread):
                    service.restart_async()
                    self.assertFalse(service.is_running())
                    application._request_manual_app_lock()
                self.assertTrue(application._resume_after_manual_lock)
                queued[1]()  # Prepare the manual lock and acquire its camera lease.
                self.assertFalse(queued[0]())  # The older restart remains invalidated.
                application._poll_app_lock()
                self.assertEqual(service.app_lock_signal.state, "locked")
                started.clear()
                application._app_lock_unlocked()
                self.assertTrue(started.wait(1))
                self.assertTrue(service.is_running())
                self.assertEqual(run.call_count, 2)
                self.assertIsNone(service._camera_owner)
            finally:
                service.shutdown()

    def test_failed_worker_clears_intent_and_can_be_started_again(self):
        service = self.service()
        failed = threading.Event()
        restarted = threading.Event()
        attempts = []
        def monitor(*, stop_event, **kwargs):
            attempts.append(stop_event)
            if len(attempts) == 1:
                failed.set()
                return 1
            restarted.set()
            stop_event.wait(3)
            return 0
        with patch("monitoring_service.monitoring.run", side_effect=monitor):
            try:
                service.start_async()
                self.assertTrue(failed.wait(1))
                service._thread.join(1)
                self.assertEqual(service.status_snapshot()[0], "error")
                self.assertFalse(service.should_resume_monitoring())
                service.start_async()
                self.assertTrue(restarted.wait(1))
                self.assertTrue(service.should_resume_monitoring())
            finally:
                service.shutdown()


class RegistrationCoordinationTests(unittest.TestCase):
    def application(self):
        application = TrayApplication.__new__(TrayApplication)
        application._service = Mock()
        application._service.app_lock_signal = AppLockSignal()
        application._settings_store = Mock()
        application._face_template_store = Mock()
        application._registration_cancel_event = threading.Event()
        application._registration_queue = queue.Queue()
        application._shutdown_started = threading.Event()
        return application

    def test_registration_blocks_competing_ui_operations(self):
        application = self.application()
        application._resume(None, None)
        application._toggle_monitoring()
        application._toggle_inference_device()
        application._on_debug_camera_selected()
        application._toggle_privacy_blur_setting()
        application._request_manual_app_lock()
        application._show_settings()
        application._delete_registered_face()
        application._save_settings({}, Mock(), AppSettings.defaults())
        application._service.start_async.assert_not_called()
        application._service.restart_async.assert_not_called()
        application._service.acquire_camera.assert_not_called()
        application._settings_store.save.assert_not_called()
        application._face_template_store.delete.assert_not_called()

    def test_failed_pause_does_not_start_registration(self):
        application = self.application()
        application._service.acquire_camera.side_effect = MonitoringTransitionError("stop failed")
        with patch("app.register_face_from_camera") as register:
            application._face_registration_worker(AppSettings.defaults())
        register.assert_not_called()
        self.assertEqual(application._registration_queue.get_nowait()[0], "error")

    def test_completion_is_published_after_camera_lease_is_released(self):
        application = self.application()
        def release(owner):
            self.assertTrue(application._registration_queue.empty())
        application._service.release_camera.side_effect = release
        with patch("app.register_face_from_camera", return_value="template"):
            application._face_registration_worker(AppSettings.defaults())
        owner = application._service.acquire_camera.call_args.args[0]
        application._service.release_camera.assert_called_once_with(owner)
        self.assertEqual(application._registration_queue.get_nowait(), ("success", "template"))

    def test_cancelled_registration_releases_lease_without_opening_camera(self):
        application = self.application()
        application._registration_cancel_event.set()
        with patch("app.register_face_from_camera") as register:
            application._face_registration_worker(AppSettings.defaults())
        register.assert_not_called()
        application._service.release_camera.assert_called_once()
        self.assertEqual(application._registration_queue.get_nowait()[0], "cancelled")

    def test_success_preserves_previously_paused_monitoring(self):
        application = self.application()
        application._registration_window = Mock()
        application._registration_resume_after = False
        application._registration_cancel_button = None
        application._registration_progress = None
        application._registration_status_variable = None
        application._settings_store.load.return_value = Mock(presence_mode="REGISTERED_FACE")
        application._registration_queue.put(("success", object()))
        with patch("app.messagebox.showinfo"):
            application._poll_face_registration()
        application._service.start_async.assert_not_called()
        self.assertFalse(application._registration_active())

    def test_completion_is_not_lost_when_ui_drains_a_full_queue(self):
        application = self.application()
        message_queue = Mock()
        message_queue.put_nowait.side_effect = [queue.Full, None]
        message_queue.get_nowait.side_effect = queue.Empty
        application._registration_queue = message_queue
        application._queue_registration_message("success", "template")
        self.assertEqual(message_queue.put_nowait.call_count, 2)
        message_queue.put_nowait.assert_called_with(("success", "template"))


class SettingsApplicationTests(unittest.TestCase):
    def test_privacy_toggle_preserves_a_pending_restart(self):
        service = MonitoringService(Mock())
        application = TrayApplication.__new__(TrayApplication)
        application._service = service
        application._settings_store = Mock()
        application._settings_store.load.return_value = replace(
            AppSettings.defaults(), camera_monitoring_mode="CONTINUOUS",
        )
        application._settings_privacy_blur_variable = None
        application._hide_privacy_blur = Mock()
        application._tray_icon = Mock()
        with patch("monitoring_service.threading.Thread"), \
                patch.object(service, "restart_async", wraps=service.restart_async) as restart, \
                patch.object(service, "refresh_settings_async") as refresh:
            service.restart_async()
            self.assertFalse(service.is_running())
            application._toggle_privacy_blur_setting()
        self.assertTrue(service.should_resume_monitoring())
        self.assertEqual(restart.call_count, 2)
        refresh.assert_not_called()
        application._settings_store.save.assert_called_once()
        service.shutdown()

    def test_toggle_during_pending_start_requests_pause(self):
        service = MonitoringService(Mock())
        application = TrayApplication.__new__(TrayApplication)
        application._service = service
        queued = []
        def deferred_thread(*, target, args, **kwargs):
            queued.append(lambda: target(*args))
            return Mock()
        with patch("monitoring_service.threading.Thread", side_effect=deferred_thread):
            service.start_async()
            self.assertFalse(service.is_running())
            self.assertTrue(service.should_resume_monitoring())
            application._toggle_monitoring()
        self.assertFalse(service.should_resume_monitoring())
        queued[1]()
        queued[0]()
        self.assertFalse(service.is_running())
        service._settings_store.load.assert_not_called()
        service.shutdown()

    def test_oled_preference_does_not_reload_models(self):
        from test_app_lock import SettingsUiTests
        helper = SettingsUiTests()
        application = helper.application()
        old = replace(AppSettings.defaults(), app_lock_oled_protection=True)
        variables = helper.variables(old, mode="SYSTEM", new="")
        variables["app_lock_oled_protection"].get.return_value = False
        with patch.object(AppSettings, "apply_to_runtime") as apply, \
                patch.object(config, "PRIVACY_BLUR_HOTKEY", config.PRIVACY_BLUR_HOTKEY):
            application._save_settings(variables, Mock(), old)
        apply.assert_not_called()
        application._service.restart_async.assert_not_called()
        self.assertFalse(application._settings_store.save.call_args.args[0].app_lock_oled_protection)

    def test_camera_settings_are_not_applied_before_worker_restart(self):
        from test_app_lock import SettingsUiTests
        helper = SettingsUiTests()
        application = helper.application()
        old = AppSettings.defaults()
        variables = helper.variables(old, mode="SYSTEM", new="")
        variables["camera_name"].get.return_value = "synthetic new camera"
        with patch.object(AppSettings, "apply_to_runtime") as apply:
            application._save_settings(variables, Mock(), old)
        apply.assert_not_called()
        application._service.restart_async.assert_called_once()


if __name__ == "__main__":
    unittest.main()
