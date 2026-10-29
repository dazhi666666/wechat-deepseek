"""发送校验数据库必须属于 GUI 窗口；不连接或操作真实微信。"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wechatauto.guia import WeChatGUI


class DatabaseBindingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for account in ('account_a', 'account_b'):
            (self.root/account/'db_storage').mkdir(parents=True)
        self.opened = [self.file('account_b')]
        self.gui = object.__new__(WeChatGUI)
        self.gui.main_hwnd = 22
        self.gui.pid = 202
        self.gui._get_pid = Mock(return_value=202)
        self.process = Mock()
        self.process.open_files.side_effect = lambda: self.opened
        patches = [
            patch('wechatauto.db.auto_detect_db_dir', return_value=str(self.root)),
            patch('psutil.Process', return_value=self.process),
            patch('wechatauto.db.WeChatDB', side_effect=lambda **kw: SimpleNamespace(
                account=kw.get('account', 'account_a'), db_dir=kw.get('db_dir', str(self.root)))),
        ]
        self.detect, self.process_factory, self.database_factory = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def file(self, account, suffix='contact/contact.db'):
        return SimpleNamespace(path=str(self.root/account/'db_storage'/suffix))

    def test_bound_account_overrides_default_recent_account(self):
        self.assertEqual(self.gui._get_db().account, 'account_b')
        self.database_factory.assert_called_once_with(db_dir=str(self.root), account='account_b')

    def test_same_account_reuses_database_but_rechecks_process_files(self):
        db = self.gui._get_db()
        self.assertIs(self.gui._get_db(), db)
        self.database_factory.assert_called_once()
        self.assertEqual(self.process.open_files.call_count, 2)

    def test_multiple_files_of_same_account_are_not_ambiguous(self):
        self.opened.append(self.file('account_b', 'message/message_0.db'))
        self.assertEqual(self.gui._get_db().account, 'account_b')

    def test_no_account_or_multiple_accounts_never_use_default_selection(self):
        for files in ([], [self.file('account_a'), self.file('account_b')]):
            with self.subTest(files=files):
                self.opened = files
                with self.assertRaises(RuntimeError):
                    self.gui._get_db()
                self.database_factory.assert_not_called()

    def test_files_outside_database_storage_do_not_establish_ownership(self):
        self.opened = [SimpleNamespace(path=str(self.root/'account_b'/'config.json')),
                       SimpleNamespace(path=str(self.root.parent/'other'/'contact.db'))]
        with self.assertRaises(RuntimeError):
            self.gui._get_db()
        self.database_factory.assert_not_called()

    def test_process_enumeration_errors_are_chained_as_unknown_account(self):
        errors = [psutil.NoSuchProcess(202), psutil.AccessDenied(202),
                  psutil.ZombieProcess(202), PermissionError('无法读取进程文件'),
                  FileNotFoundError('进程已退出'), OSError('进程读取失败')]
        for stage in ('process', 'files'):
            for error in errors:
                with self.subTest(stage=stage, error=type(error).__name__):
                    self.process_factory.side_effect = error if stage == 'process' else None
                    self.process.open_files.side_effect = error if stage == 'files' else lambda: self.opened
                    with self.assertRaisesRegex(RuntimeError, '无法唯一确认微信窗口的数据库账号') as raised:
                        self.gui._get_db()
                    self.assertIs(raised.exception.__cause__, error)
                    self.assertIsNone(self.gui._cached_db)
                    self.assertIsNone(self.gui._cached_db_key)
                    self.database_factory.assert_not_called()

    def test_enumeration_failure_discards_cache_and_recovery_rechecks_account(self):
        for recovered_account in ('account_b', 'account_a'):
            with self.subTest(account=recovered_account):
                self.opened = [self.file('account_b')]
                self.process.open_files.side_effect = lambda: self.opened
                first = self.gui._get_db()
                self.process.open_files.side_effect = psutil.AccessDenied(202)
                with self.assertRaises(RuntimeError):
                    self.gui._get_db()
                self.assertIsNone(self.gui._cached_db)
                self.assertIsNone(self.gui._cached_db_key)
                self.opened = [self.file(recovered_account)]
                self.process.open_files.side_effect = lambda: self.opened
                recovered = self.gui._get_db()
                self.assertIsNot(recovered, first)
                self.assertEqual(recovered.account, recovered_account)

    def test_unexpected_enumeration_errors_are_not_hidden(self):
        error = ValueError('意外的程序错误')
        self.process.open_files.side_effect = error
        with self.assertRaises(ValueError) as raised:
            self.gui._get_db()
        self.assertIs(raised.exception, error)
        self.database_factory.assert_not_called()

    def test_database_constructor_errors_are_not_labeled_as_enumeration_failure(self):
        error = OSError('数据库初始化失败')
        self.database_factory.side_effect = error
        with self.assertRaises(OSError) as raised:
            self.gui._get_db()
        self.assertIs(raised.exception, error)
        self.assertIsNone(self.gui._cached_db)
        self.assertIsNone(self.gui._cached_db_key)

    def test_verification_callers_keep_their_failure_results(self):
        self.process.open_files.side_effect = psutil.AccessDenied(202)
        self.assertEqual(self.gui._verify_usernames('target'), ['target'])
        self.assertIsNone(self.gui._send_watermark('target'))
        self.assertFalse(self.gui._verify_sent('正文', 'target'))
        self.assertEqual(self.gui._target_seq('target'), 0)
        self.assertFalse(self.gui._verify_attachment_sent('test.txt', 'target'))
        self.database_factory.assert_not_called()

    def test_missing_database_root_does_not_auto_pick(self):
        self.detect.return_value = None
        with self.assertRaises(RuntimeError):
            self.gui._get_db()
        self.database_factory.assert_not_called()

    def test_closed_or_reused_window_cannot_reuse_cached_database(self):
        for pid in (0, 303):
            with self.subTest(pid=pid):
                self.gui._get_pid.return_value = 202
                self.gui._get_db()
                self.gui._get_pid.return_value = pid
                with self.assertRaises(RuntimeError):
                    self.gui._get_db()

    def test_account_changes_in_same_process_rebuild_database(self):
        first = self.gui._get_db()
        self.opened = [self.file('account_a')]
        second = self.gui._get_db()
        self.assertIsNot(second, first)
        self.assertEqual(second.account, 'account_a')

    def test_database_constructor_cannot_silently_switch_account(self):
        self.database_factory.side_effect = None
        self.database_factory.return_value = SimpleNamespace(account='account_a')
        with self.assertRaises(RuntimeError):
            self.gui._get_db()
        self.assertIsNone(getattr(self.gui, '_cached_db', None))

    def test_switch_window_clears_database_cache(self):
        self.gui._get_db()
        self.gui._input = SimpleNamespace(_user32=SimpleNamespace(IsWindow=lambda h: True))
        self.gui._find_render_window = Mock(return_value=330)
        self.gui._update_render_rect = Mock()
        self.gui._get_pid.return_value = 303
        self.assertTrue(self.gui.use_window(33))
        self.assertIsNone(self.gui._cached_db)

    def test_rebinding_reused_handle_clears_all_cached_window_state(self):
        self.gui._get_db()
        self.gui._uia = object()
        self.gui._current_chat = '旧会话'
        self.gui._input = SimpleNamespace(_user32=SimpleNamespace(IsWindow=lambda h: True))
        self.gui._find_render_window = Mock(return_value=220)
        self.gui._update_render_rect = Mock()
        self.gui._get_pid.return_value = 303
        self.assertTrue(self.gui.use_window(22))
        self.assertIsNone(self.gui._cached_db)
        self.assertIsNone(self.gui._uia)
        self.assertIsNone(self.gui._current_chat)


if __name__ == '__main__':
    unittest.main()
