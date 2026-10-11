"""Exercise sessions must save via the website and never grant local credit."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core import Course, NeedsAttention, State
from questionbank import QuestionBank
from runner import Runner
from test_runner import Clock


class FakeBank(QuestionBank):
    def __init__(self, runner, clock, frozen=False, reset=False, fail_exit=False):
        super().__init__(runner)
        self.clock, self.frozen, self.reset, self.fail_exit = clock, frozen, reset, fail_exit
        self.exits = 0

    def open_bank(self, index):
        self.started = self.clock.time()
        self.selected_index = index
        return f'Bank {index % 8}', self.sample()

    def sample(self):
        elapsed = int(self.clock.time() - self.started)
        timer = 0 if self.reset and elapsed >= 2 else 5 + (0 if self.frozen else elapsed)
        return {'seconds': timer, 'first': 'm-item-1', 'last': 'm-item-20',
                'questions': [], 'next': None}

    def exit(self):
        if self.fail_exit:
            raise NeedsAttention('Exit failed')
        self.exits += 1
        return self.sample()['seconds']

    def find_answer(self):
        return {'id': 'test', 'title': 'Fixture question'}, {'indices': [0]}, self.sample()

    def answer_one(self, question, match):
        return {'question': 'test', 'answer': 'A', 'source': 'fixture'}


class QuestionBankTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.output = Path(self.temp.name)
        self.clock = Clock()
        self.clock.value = 100
        self.state = State(self.output / 'state.json', 'task', 'account')
        self.settings = {'poll_seconds': 1, 'stall_seconds': 4, 'questionbank_session_seconds': 3,
                         'confirmation_delay_seconds': 300, 'confirmation_timeout_seconds': 600}
        self.runner = Runner(SimpleNamespace(driver=SimpleNamespace()), self.state, self.settings, self.output)
        self.course = Course('bank', '题库', '题库练习', True, 10, 100)
        self.runner.active = self.course
        self.patchers = [patch('questionbank.time.monotonic', self.clock.time),
                         patch('questionbank.time.time', self.clock.time),
                         patch('questionbank.time.sleep', self.clock.sleep)]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.temp.cleanup()

    def test_session_saves_then_waits_for_delayed_site_credit_and_rotates(self):
        bank = FakeBank(self.runner, self.clock)
        bank.learn(self.course)
        self.assertEqual(bank.exits, 1)
        record = self.state.pending()['bank']
        self.assertEqual(record['observation']['final_timer_seconds'], 8)
        self.assertEqual(record['before_seconds'], 10)
        self.assertEqual(bank.cursor(), 1)
        self.assertIsNone(self.runner.active)
        website = Course('bank', '题库', '题库练习', True, 13, 100)
        self.state.reconcile([website], self.clock.time() + 299, 600, delay=300)
        self.assertTrue(self.state.pending())
        self.state.reconcile([website], self.clock.time() + 300, 600, delay=300)
        self.assertFalse(self.state.pending())
        self.assertEqual(self.state.data['courses']['bank']['status'], 'incomplete')
        fresh = FakeBank(self.runner, self.clock)
        fresh.learn(website)
        self.assertEqual(fresh.selected_index, 1)

    def test_remaining_budget_caps_session(self):
        bank = FakeBank(self.runner, self.clock)
        bank.learn(self.course, budget=1)
        self.assertEqual(self.clock.time(), 101)
        self.assertEqual(bank.exits, 1)

    def test_frozen_timer_keeps_page_without_pending_credit(self):
        bank = FakeBank(self.runner, self.clock, frozen=True)
        with self.assertRaisesRegex(NeedsAttention, '持续未增加'):
            bank.learn(self.course)
        self.assertEqual(bank.exits, 0)
        self.assertFalse(self.state.pending())
        self.assertEqual(bank.cursor(), 0)

    def test_timer_reset_keeps_page(self):
        bank = FakeBank(self.runner, self.clock, reset=True)
        with self.assertRaisesRegex(NeedsAttention, '回退'):
            bank.learn(self.course)
        self.assertFalse(self.state.pending())

    def test_unsaved_exit_does_not_advance_bank_or_record_credit(self):
        bank = FakeBank(self.runner, self.clock, fail_exit=True)
        with self.assertRaises(NeedsAttention):
            bank.learn(self.course)
        self.assertFalse(self.state.pending())
        self.assertEqual(bank.cursor(), 0)

    def test_stop_signal_saves_through_existing_runner_recovery(self):
        bank = FakeBank(self.runner, self.clock)
        (self.output / 'stop.flag').write_text('stop')
        with self.assertRaises(KeyboardInterrupt):
            bank.learn(self.course)
        self.assertEqual(bank.exits, 0)
        self.assertEqual(self.state.data['active']['kind'], 'questionbank')


if __name__ == '__main__':
    unittest.main()
