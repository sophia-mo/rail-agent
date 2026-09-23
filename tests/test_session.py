import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rail_agent.session import login_status, restore_session, save_session, state_directory


class SessionTests(unittest.TestCase):
    def test_session_cookie_saved_privately_and_expired_cookie_not_restored(self):
        cookies = [dict(name="session", value="test", domain="kyfw.12306.cn", path="/", expires=-1),
                   dict(name="expired", value="test", domain="kyfw.12306.cn", path="/", expires=1),
                   dict(name="unrelated", value="test", domain="example.com", path="/", expires=-1)]
        with tempfile.TemporaryDirectory() as directory:
            context = Mock()
            context.cookies.return_value = cookies
            save_session(context, directory)
            file = Path(directory) / 'session.json'
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(json.loads(file.read_text())['cookies']), 2)
            self.assertTrue(restore_session(context, directory))
            context.add_cookies.assert_called_once_with([cookies[0]])

    def test_default_directory_does_not_depend_on_current_working_directory(self):
        with patch.dict('os.environ', {}, clear=True):
            first = state_directory()
            with patch.dict('os.environ', {'RAIL_STATE_DIR': '/tmp/ignored-rail-directory'}):
                self.assertEqual(first, state_directory())
            with patch('pathlib.Path.cwd', return_value=Path('/tmp')):
                self.assertEqual(first, state_directory())

    def test_login_status_requires_server_confirmation(self):
        context = Mock()
        page = Mock(url='https://kyfw.12306.cn/otn/view/index.html')
        page.is_closed.return_value = False
        context.pages = [page]
        for flag in (True, False, "true", None):
            page.evaluate.return_value = flag
            self.assertIs(login_status(context), flag if type(flag) is bool else None)
        context.request.post.assert_not_called()

    def test_login_can_finish_in_another_tab(self):
        context = Mock()
        logged_in = Mock(url='https://kyfw.12306.cn/otn/view/index.html')
        logged_in.is_closed.return_value = False
        logged_in.evaluate.return_value = True
        login = Mock(url='https://kyfw.12306.cn/otn/resources/login.html')
        login.is_closed.return_value = False
        login.evaluate.return_value = False
        context.pages = [logged_in, login]
        self.assertIs(login_status(context), True)
