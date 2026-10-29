"""发送提交后不重复投递的回归；不连接或操作真实微信。"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wechatauto.guia import WeChatGUI, VK_RETURN
from wechatauto.uia_driver import WeChatUIA


class SendRetryTests(unittest.TestCase):
    def setUp(self):
        self.gui = object.__new__(WeChatGUI)
        self.gui._current_chat = '测试会话'
        self.gui._last_input_box = (300, 840, 1500, 920)
        self.gui._input = SimpleNamespace(key=Mock())
        self.gui.send_button_region = (1200, 900, 1400, 950)
        self.gui._pending_text = '测试正文'
        self.gui._pending_text_ok = Mock(return_value=True)
        self.gui._input_box_has_text = Mock(return_value=True)
        self.gui.input_text = Mock(return_value=True)
        self.gui._get_uia = Mock(return_value=None)
        self.gui.ensure_visible = Mock(return_value=True)
        self.gui._chat_is_open = Mock(return_value=True)
        self.gui.open_chat = Mock(return_value=True)
        self.gui.ocr = Mock(return_value=[])
        self.gui._send_watermark = Mock(return_value={'seq': 10})
        self.gui._wait_landed = Mock(return_value=True)
        for target in ('wechatauto.guia.rhythm.gate', 'wechatauto.guia.rhythm.nap',
                       'wechatauto.guia.time.sleep'):
            p = patch(target)
            p.start()
            self.addCleanup(p.stop)

    def test_fast_uncertain_result_does_not_repaste_or_fall_back(self):
        uia = Mock()
        self.gui._get_uia.return_value = uia
        result = self.gui.send_msg('测试正文', who='测试会话')
        self.assertEqual(result['status'], '失败')
        self.assertIn('重复', result['message'])
        self.gui.input_text.assert_called_once()
        self.gui._input.key.assert_called_once_with(VK_RETURN)
        uia.send_text.assert_not_called()

    def test_full_ocr_path_stops_after_uncertain_submit(self):
        self.gui._last_input_box = None
        result = self.gui.send_msg('测试正文', who='测试会话')
        self.assertEqual(result['status'], '失败')
        self.gui.input_text.assert_called_once()
        self.gui._input.key.assert_called_once_with(VK_RETURN)

    def test_pre_submit_rejection_can_use_uia_fallback(self):
        self.gui._pending_text_ok.return_value = False
        uia = Mock()
        uia.current_chat.return_value = '测试会话'
        uia.send_text.return_value = True
        self.gui._get_uia.return_value = uia
        self.assertEqual(self.gui.send_msg('测试正文', who='测试会话')['status'], '成功')
        self.gui._input.key.assert_not_called()
        uia.send_text.assert_called_once_with('测试正文')

    def test_submit_waits_for_clear_without_pressing_enter_again(self):
        self.gui._input_box_has_text.side_effect = [True, True, False]
        self.assertTrue(self.gui.click_send())
        self.gui._input.key.assert_called_once_with(VK_RETURN)

    def test_pending_or_empty_rejection_records_no_submission(self):
        self.gui._send_attempted = True
        self.gui._pending_text_ok.return_value = False
        self.assertFalse(self.gui.click_send(fast=True))
        self.assertFalse(self.gui._send_attempted)
        self.gui._pending_text_ok.return_value = True
        self.gui._input_box_has_text.return_value = False
        self.assertFalse(self.gui.click_send())
        self.assertFalse(self.gui._send_attempted)
        self.gui._input.key.assert_not_called()

    def test_uia_enter_exception_cannot_trigger_second_send(self):
        engine = object.__new__(WeChatUIA)
        engine.window_kind = Mock(return_value='main')
        engine.ensure_window = Mock(return_value=True)
        edit = Mock()
        edit.SendKeys.side_effect = RuntimeError('提交后连接断开')
        engine._chat_input = Mock(return_value=edit)
        engine._paste_into = Mock()
        engine._paste_landed = Mock(return_value=(True, '测试正文', 1.0))
        engine.current_chat = Mock(return_value='测试会话')
        engine.back_to_chat_tab = Mock(return_value=True)
        self.gui._last_input_box = None
        self.gui._get_uia.return_value = engine
        result = self.gui.send_msg('测试正文', who='测试会话')
        self.assertEqual(result['status'], '失败')
        edit.SendKeys.assert_called_once_with('{Enter}', waitTime=0.05)
        engine.back_to_chat_tab.assert_not_called()
        self.gui.input_text.assert_not_called()

    def test_verified_send_does_not_retry_after_database_timeout(self):
        self.gui._get_uia.return_value = Mock(send_text=Mock(return_value=True))
        self.gui._wait_landed.return_value = False
        self.assertEqual(self.gui.send_msg('测试正文', who='测试会话', verify=True)['status'], '失败')
        self.gui._get_uia.return_value.send_text.assert_called_once()
        self.gui.input_text.assert_not_called()


if __name__ == '__main__':
    unittest.main()
