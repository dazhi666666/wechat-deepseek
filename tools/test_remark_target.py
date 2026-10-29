"""备注目标回归：屏外精确联系人、重复显示名和保存前目标变化。

运行：python tools/test_remark_target.py；不连接或修改真实微信资料。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wechatauto.remark import RemarkOps


class RemarkTargetTests(unittest.TestCase):
    def test_similar_visible_row_does_not_hide_exact_contact_on_other_page(self):
        ops = RemarkOps()
        exact = SimpleNamespace(Name='测试目标')
        similar = SimpleNamespace(Name='ZZZ_测试目标_同事')
        pages = [[similar], [SimpleNamespace(Name='其他好友')], [exact]]
        page = [0]
        ops._rows = lambda *_: pages[page[0]]
        ops._root = lambda *_: (42, object())
        ops._rewind_contacts = lambda *_: page.__setitem__(0, 1)
        ops._scroll_contacts = lambda *_a, **_k: (page.__setitem__(0, min(2, page[0] + 1)) or True)
        self.assertIs(ops.find_contact_row(object(), '测试目标'), exact)

    def test_only_similar_contact_is_not_accepted(self):
        ops = RemarkOps()
        ops._rows = lambda *_: [SimpleNamespace(Name='测试目标_同事')]
        ops._root = lambda *_: (42, object())
        ops._rewind_contacts = Mock()
        ops._scroll_contacts = Mock(return_value=False)
        self.assertIsNone(ops.find_contact_row(object(), '测试目标'))

    def test_exact_visible_contact_needs_no_scroll(self):
        ops = RemarkOps()
        exact = SimpleNamespace(Name='测试目标')
        ops._rows = lambda *_: [SimpleNamespace(Name='测试目标_同事'), exact]
        ops._rewind_contacts = Mock()
        self.assertIs(ops.find_contact_row(object(), '测试目标'), exact)
        ops._rewind_contacts.assert_not_called()

    def test_write_requires_unique_effective_display_name_before_navigation(self):
        for hits in (
            [], [{'username': 'b', 'remark': '测试目标_同事'}],
            [{'username': 'a', 'remark': '测试目标'}, {'username': 'b', 'remark': '测试目标'}],
            [{'username': 'a', 'nick_name': '测试目标', 'remark': '另一个备注'}],
        ):
            for verify in (False, True):
                with self.subTest(hits=hits, verify=verify):
                    ops = RemarkOps(db=SimpleNamespace(search_contact=Mock(return_value=hits)))
                    ops.open_profile = Mock()
                    result = ops.set_remark('测试目标', '新备注', verify=verify)
                    self.assertFalse(result['ok'])
                    self.assertIn('no-exact-contact', result['reason'])
                    ops.open_profile.assert_not_called()

    def test_unremarked_nickname_and_remark_resolve_exactly(self):
        for row in ({'username': 'a', 'nick_name': '测试目标', 'remark': ''},
                    {'username': 'a', 'nick_name': '旧昵称', 'remark': '测试目标'}):
            ops = RemarkOps(db=SimpleNamespace(search_contact=lambda _: [row]))
            self.assertEqual(ops._username_of('测试目标'), 'a')

    def make_write_ops(self, before):
        ops = RemarkOps(db=SimpleNamespace(search_contact=lambda _: [{'username': 'a', 'remark': '测试目标'}]))
        ops.open_profile = Mock(return_value={'ok': True, 'profile': object()})
        ops._open_remark_menu = Mock(return_value={'ok': True, 'menu_hwnd': 42, 'item': object()})
        ops._click = Mock(return_value={'ok': True})
        ops._wait_edit_window = Mock(return_value=(43, object()))
        ops._current_remark = Mock(return_value=before)
        ops._type_remark = Mock(return_value={'ok': True})
        ops._close_edit_windows = Mock()
        ops._find = Mock(return_value=object())
        ops._window_hwnd = Mock(return_value=43)
        ops.back_to_chat = Mock()
        ops._remark_landed = Mock(return_value=True)
        return ops

    @patch('wechatauto.remark.time.sleep')
    def test_changed_profile_is_closed_without_typing_or_saving(self, _sleep):
        ops = self.make_write_ops('另一位好友')
        result = ops.set_remark('测试目标', '新备注')
        self.assertFalse(result['ok'])
        self.assertIn('target-mismatch', result['reason'])
        ops._type_remark.assert_not_called()
        self.assertEqual(ops._click.call_count, 1)  # 只打开编辑窗，没有点完成。
        ops._close_edit_windows.assert_called_once()

    @patch('wechatauto.remark.time.sleep')
    def test_verified_write_and_explicit_clear_still_work(self, _sleep):
        for value in ('新备注', ''):
            with self.subTest(value=value):
                ops = self.make_write_ops('测试目标')
                result = ops.set_remark('测试目标', value, allow_empty=(value == ''))
                self.assertTrue(result['ok'])
                self.assertTrue(result['verified'])
                ops._remark_landed.assert_called_once_with('a', value)


if __name__ == '__main__':
    unittest.main()
