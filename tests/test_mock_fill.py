import unittest
from unittest.mock import patch

from core import NeedsAttention
from mock_fill import assert_exam, assert_mock, apply_answer


class FakeLabel:
    def __init__(self, states, index, multi):
        self.states, self.index, self.multi = states, index, multi

    def click(self):
        if self.multi:
            self.states[self.index] = not self.states[self.index]
        else:
            self.states[:] = [i == self.index for i in range(len(self.states))]


class FakeDriver:
    current_url = 'https://school.test/lab-study-front/routine-exam/75?examRequestId=1'

    def __init__(self, multi=False):
        self.states = [False, False, False]
        self.multi = multi

    def find_elements(self, *_):
        return [FakeLabel(self.states, i, self.multi) for i in range(3)]


class MockFillTests(unittest.TestCase):
    def test_formal_exam_is_rejected_before_choices(self):
        driver = FakeDriver()
        meta = {'status': {'mock': 0, 'examId': 75, 'requestId': '1'},
                'routeId': '75', 'requestId': '1', 'studyId': None,
                'submitted': False, 'total': 10, 'showType': 1}
        with patch('mock_fill.mock_meta', return_value=meta):
            with self.assertRaises(NeedsAttention):
                assert_mock(driver, {'task_url': 'https://school.test/lab-study-front/examTask/75'})
        self.assertEqual(driver.states, [False] * 3)

    def test_formal_entry_accepts_only_matching_formal_attempt(self):
        driver = FakeDriver()
        driver.execute_script = lambda *_: [{'id': 'm-item-1'}]
        meta = {'status': {'mock': 0, 'examId': 75, 'requestId': '1'},
                'routeId': '75', 'requestId': '1', 'studyId': None,
                'submitted': False, 'total': 10, 'showType': 2}
        settings = {'task_url': 'https://school.test/lab-study-front/examTask/75'}
        with patch('mock_fill.mock_meta', return_value=meta):
            self.assertEqual(assert_exam(driver, settings, expected_mock=False), meta)
        meta['status']['mock'] = 1
        with patch('mock_fill.mock_meta', return_value=meta):
            with self.assertRaises(NeedsAttention):
                assert_exam(driver, settings, expected_mock=False)
        self.assertEqual(driver.states, [False] * 3)

    def test_choice_updates_only_needed_labels(self):
        question = {'id': 'm-item-1', 'type': '多选题', 'options': ['A', 'B', 'C']}
        driver = FakeDriver(multi=True)
        driver.states = [True, True, False]
        with patch('mock_fill.selected_options', side_effect=lambda *_: [
                {'selected': x, 'disabled': False} for x in driver.states]):
            self.assertIsNone(apply_answer(driver, question, [1, 2]))
        self.assertEqual(driver.states, [False, True, True])

    def test_reload_with_zero_subject_index_restores_first_page(self):
        driver = FakeDriver()
        driver.ready = False
        driver.execute_script = lambda *_: [{'id': 'm-item-1'}] if driver.ready else []
        def reset_first_page(*_):
            driver.ready = True
        driver.rpc = reset_first_page
        meta = {'status': {'mock': 1, 'examId': 75, 'requestId': '1'},
                'routeId': '75', 'requestId': '1', 'studyId': None,
                'submitted': False, 'total': 100, 'showType': 2}
        with patch('mock_fill.mock_meta', return_value=meta):
            self.assertEqual(assert_mock(driver, {'task_url':
                'https://school.test/lab-study-front/examTask/75'}), meta)
        self.assertTrue(driver.ready)


if __name__ == '__main__':
    unittest.main()
