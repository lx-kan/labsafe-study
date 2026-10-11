import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from selenium.common.exceptions import NoSuchWindowException, TimeoutException
from browser import Browser, ready_course_tab


class Switch:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        if handle == 'closed':
            raise NoSuchWindowException('Login popup closed while enumerating handles')
        self.driver.current_window_handle = handle
        self.driver.current_url = ('https://school.test/examTask/75' if handle == 'task'
                                   else 'https://ntp.msn.com/edge/ntp')

    def default_content(self):
        pass


class Driver:
    _labsafe_attached = True

    def __init__(self):
        self.current_window_handle = 'task'
        self.current_url = 'https://school.test/examTask/75'
        self.window_handles = ['closed', 'internal', 'task']
        self.switch_to = Switch(self)


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = {'task_url': 'https://school.test/examTask/75',
                         'home_url': 'https://school.test/home', 'exam_name': '目标考试'}
        self.driver = Driver()
        self.browser = Browser(self.driver, self.settings, Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def test_closed_popup_and_internal_tab_do_not_abort_login(self):
        def read(operation):
            self.assertEqual(self.driver.current_window_handle, 'task')
            return '目标考试' if operation == 'body' else {'required': ['00:00:00', '01:00:00'],
                                                    'optional': ['00:00:00', '01:00:00']}
        self.browser.js = read
        self.browser.wait_for_login(0)
        self.assertEqual(self.browser.main_handle, 'task')

    def test_loading_timeout_is_retried_while_waiting_for_user(self):
        calls = []
        def read(operation):
            calls.append(operation)
            if len(calls) == 1:
                raise TimeoutException('Page still loading')
            return '目标考试' if operation == 'body' else {'required': ['00:00:00', '01:00:00'],
                                                    'optional': ['00:00:00', '01:00:00']}
        self.browser.js = read
        with patch('browser.time.sleep', lambda _: None):
            self.browser.wait_for_login(2)
        self.assertGreaterEqual(len(calls), 3)

    def test_only_school_cdp_targets_are_polled(self):
        self.driver._labsafe_debug_address = '127.0.0.1:12345'
        self.driver.window_handles = ['CDwindow-school-id', 'CDwindow-internal-id']
        targets = [{'id': 'school-id', 'type': 'page', 'url': self.settings['task_url']},
                   {'id': 'internal-id', 'type': 'page', 'url': 'https://ntp.msn.com/edge/ntp'}]
        with patch('browser.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(targets).encode())):
            self.assertEqual(self.browser.login_handles(), ['CDwindow-school-id'])

    def test_login_portal_does_not_trigger_early_attachment(self):
        pages = [{'type': 'page', 'url': 'https://school.test/home', 'title': '首页 - 实验室安全综合管理系统'}]
        self.assertFalse(ready_course_tab(pages, self.settings['task_url']))

    def test_empty_task_document_does_not_trigger_attachment(self):
        pages = [{'type': 'page', 'url': self.settings['task_url'], 'title': ''}]
        self.assertFalse(ready_course_tab(pages, self.settings['task_url']))

    def test_target_course_title_triggers_attachment(self):
        pages = [{'type': 'page', 'url': self.settings['task_url'], 'title': '考核内容-实验室安全教育平台'}]
        self.assertTrue(ready_course_tab(pages, self.settings['task_url']))

    def test_explicit_connect_signal_still_requires_target_route(self):
        self.assertFalse(ready_course_tab([{'type': 'page', 'url': 'https://school.test/home', 'title': ''}],
                                         self.settings['task_url'], signal=True))
        self.assertTrue(ready_course_tab([{'type': 'page', 'url': self.settings['task_url'], 'title': ''}],
                                        self.settings['task_url'], signal=True))


if __name__ == '__main__':
    unittest.main()
