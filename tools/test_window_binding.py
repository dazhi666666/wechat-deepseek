"""多窗口绑定回归；不连接或操作真实微信。"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wechatauto.guia import WeChatGUI
from wechatauto.uia_driver import WeChatUIA


class WindowBindingTests(unittest.TestCase):
    def setUp(self):
        self.pids = {11: 101, 22: 202}
        self.controls = {h: SimpleNamespace(ClassName='mmui::MainWindow',
                                           NativeWindowHandle=h) for h in self.pids}
        p = patch('wechatauto.uia_driver.win32gui.IsWindow', side_effect=lambda h: h in self.pids)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(WeChatUIA, '_pid_from_hwnd', side_effect=self.pids.get)
        p.start()
        self.addCleanup(p.stop)

    def engine(self, hwnd=None):
        engine = WeChatUIA(hwnd=hwnd)
        engine._wechat_hwnds = Mock(return_value=[11, 22])
        engine._anchor = Mock(side_effect=self.controls.get)
        engine._activate = Mock()
        engine._hot_activate_accessibility = Mock(return_value=True)
        return engine

    def test_explicit_window_overrides_global_candidate_order(self):
        engine = self.engine(22)
        self.assertTrue(engine.ensure_window(wake=False))
        self.assertEqual(engine._win.NativeWindowHandle, 22)
        engine._anchor.assert_called_once_with(22)
        engine._wechat_hwnds.assert_not_called()

    def test_default_discovery_keeps_existing_behavior(self):
        engine = self.engine()
        self.assertTrue(engine.ensure_window(wake=False))
        self.assertEqual(engine._win.NativeWindowHandle, 11)

    def test_unavailable_bound_tree_does_not_use_other_account(self):
        engine = self.engine(22)
        self.controls[22].ClassName = 'Qt51514QWindowIcon'
        self.assertFalse(engine.ensure_window(wake=False))
        self.assertIsNone(engine._win)
        self.assertFalse(engine.is_materialized())
        engine._anchor.assert_called_with(22)

    def test_closed_or_reused_handle_never_launches_or_uses_other_account(self):
        for replacement in (None, 303):
            with self.subTest(replacement=replacement):
                self.pids[22] = 202
                engine = self.engine(22)
                self.assertTrue(engine.ensure_window(wake=False))
                if replacement is None:
                    del self.pids[22]
                else:
                    self.pids[22] = replacement
                engine.wake = Mock()
                engine._auto_login = Mock()
                self.assertFalse(engine.ensure_window())
                self.assertIsNone(engine._win)
                engine.wake.assert_not_called()
                engine._auto_login.assert_not_called()

    def test_accessibility_recovery_only_touches_bound_window(self):
        engine = self.engine(22)
        with patch('wechatauto.uia_driver.time.sleep'):
            self.assertTrue(engine._wake_accessibility())
        engine._hot_activate_accessibility.assert_called_once_with(22)

    def test_foreign_anchor_is_rejected(self):
        engine = self.engine(22)
        engine._anchor.return_value = self.controls[11]
        engine._anchor.side_effect = None
        self.assertFalse(engine.ensure_window(wake=False))

    def test_gui_creation_refresh_and_window_change_bind_current_handle(self):
        gui = object.__new__(WeChatGUI)
        gui.main_hwnd = 22
        gui._uia = None
        gui._uia_tried = False
        gui._uia_check_ts = 0
        with patch('wechatauto.uia_driver.WeChatUIA') as factory:
            engine = factory.return_value
            engine.ensure_window.return_value = True
            engine.hwnd = 22
            engine._win = self.controls[22]
            self.assertIs(gui._get_uia(), engine)
            factory.assert_called_once_with(hwnd=22)
            factory.reset_mock()
            self.assertIs(gui._get_uia(), engine)
            factory.assert_not_called()
            self.assertIs(gui._get_uia(refresh=True), engine)
            factory.assert_called_once_with(hwnd=22)
            factory.reset_mock()
            gui.main_hwnd = 11
            engine.ensure_window.return_value = False
            self.assertIsNone(gui._get_uia())
            factory.assert_called_once_with(hwnd=11)

    def test_gui_rejects_foreign_cached_root_even_inside_throttle(self):
        import time
        gui = object.__new__(WeChatGUI)
        gui.main_hwnd = 22
        gui._uia_check_ts = time.time()
        gui._uia_tried = True
        gui._uia = SimpleNamespace(hwnd=22, _win=self.controls[11])
        with patch('wechatauto.uia_driver.WeChatUIA') as factory:
            factory.return_value.ensure_window.return_value = False
            self.assertIsNone(gui._get_uia())
            factory.assert_called_once_with(hwnd=22)

    def test_window_switch_clears_fast_send_and_uia_caches(self):
        gui = object.__new__(WeChatGUI)
        gui.main_hwnd = 11
        gui._uia = object()
        gui._uia_tried = True
        gui._uia_check_ts = 100
        gui._uia_window_kind = (11, 'history')
        gui._last_input_box = (1, 2, 3, 4)
        gui._current_chat = '同名会话'
        gui._pending_text = '旧草稿'
        gui._input = SimpleNamespace(_user32=SimpleNamespace(IsWindow=lambda h: h in self.pids))
        gui._find_render_window = Mock(return_value=220)
        gui._get_pid = Mock(side_effect=self.pids.get)
        gui._update_render_rect = Mock()
        self.assertTrue(gui.use_window(22))
        self.assertEqual((gui.main_hwnd, gui.pid, gui.render_hwnd), (22, 202, 220))
        for name in ('_uia', '_last_input_box', '_current_chat', '_pending_text'):
            self.assertIsNone(getattr(gui, name))
        self.assertFalse(gui._uia_tried)
        self.assertEqual(gui._uia_check_ts, 0)
        self.assertIsNone(gui._uia_window_kind)


if __name__ == '__main__':
    unittest.main()
