"""输入框坐标回归：真实控件优先、窗口隔离、失败时禁止猜测点击。

运行：python tools/test_input_box.py；不连接或操作真实微信。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wechatauto.guia import WeChatGUI


class InputBoxTests(unittest.TestCase):
    def setUp(self):
        self.gui = object.__new__(WeChatGUI)
        self.gui.main_hwnd = 42
        self.gui.origin_x, self.gui.origin_y = 114, 37
        self.gui.render_w, self.gui.render_h = 1531, 975
        self.gui.right_pane_left = 336
        self.gui._auto_recalibrated = False
        self.gui._update_render_rect = Mock()
        self.gui.calibrate_layout = Mock()
        self.gui._probe_input_box = Mock(return_value=None)
        self.gui.wx_click = Mock()
        self.edit = SimpleNamespace(
            AutomationId='main.chat_input_field', IsOffscreen=False,
            BoundingRectangle=SimpleNamespace(left=434, top=877, right=1625, bottom=956))
        self.uia = SimpleNamespace(_win=SimpleNamespace(NativeWindowHandle=42),
                                   window_kind=Mock(return_value='main'),
                                   _chat_input=Mock(return_value=self.edit))
        self.gui._get_uia = Mock(return_value=self.uia)
        sleeper = patch('wechatauto.guia.time.sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_uses_edit_geometry_instead_of_height_ratio(self):
        self.assertEqual(self.gui.get_input_box(), (320, 840, 1511, 919))
        self.gui._probe_input_box.assert_not_called()

    def test_negative_desktop_origin_and_window_resize(self):
        self.gui.origin_x, self.gui.origin_y = -1600, -100
        self.gui.render_w, self.gui.render_h = 900, 700
        self.edit.BoundingRectangle = SimpleNamespace(left=-1400, top=400, right=-750, bottom=550)
        self.assertEqual(self.gui.get_input_box(), (200, 500, 850, 650))

    def test_other_account_window_is_not_used(self):
        self.uia._win.NativeWindowHandle = 99
        self.gui._probe_input_box.return_value = (300, 800, 1500, 950)
        self.assertEqual(self.gui.get_input_box(), (300, 800, 1500, 950))
        self.uia._chat_input.assert_not_called()

    def test_hidden_unrelated_or_invalid_edit_is_rejected(self):
        for field, value in (
            ('IsOffscreen', True), ('AutomationId', 'remark_edit'),
            ('BoundingRectangle', SimpleNamespace(left=434, top=877, right=434, bottom=956)),
            ('BoundingRectangle', SimpleNamespace(left=0, top=0, right=100, bottom=100)),
        ):
            with self.subTest(field=field, value=value):
                old = getattr(self.edit, field)
                setattr(self.edit, field, value)
                self.assertIsNone(self.gui._uia_input_box())
                setattr(self.edit, field, old)

    def test_uia_unavailable_still_uses_successful_pixel_probe(self):
        self.gui._get_uia.return_value = None
        self.gui._probe_input_box.return_value = (300, 800, 1500, 950)
        self.assertEqual(self.gui.get_input_box(), (300, 800, 1500, 950))

    def test_exhausted_probes_do_not_return_estimated_box_or_click(self):
        self.gui._get_uia.return_value = None
        self.assertFalse(self.gui.focus_input())
        self.gui.calibrate_layout.assert_called_once()
        self.gui.wx_click.assert_not_called()

    def test_text_probe_stays_inside_edit_control(self):
        # 控件下方的工具栏有深色图标，但空编辑框不能因此被判断为有文字。
        screen = Image.new('RGB', (1800, 1100), 'white')
        draw = ImageDraw.Draw(screen)
        draw.rectangle((400, 960, 900, 990), fill='black')
        self.gui._grab_screen = Mock(side_effect=screen.crop)
        box = (320, 840, 1511, 919)
        self.assertFalse(self.gui._input_box_has_text(box))
        self.gui._grab_screen.assert_called_once_with((454, 887, 1605, 956))
        draw.rectangle((500, 890, 540, 905), fill='black')
        self.assertTrue(self.gui._input_box_has_text(box))

    def test_too_small_text_region_does_not_capture_invalid_rectangle(self):
        self.gui._grab_screen = Mock()
        self.assertFalse(self.gui._input_box_has_text((10, 10, 30, 20)))
        self.gui._grab_screen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
