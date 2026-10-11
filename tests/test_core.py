from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from core import (Course, NeedsAttention, State, TimerTarget, Totals, duration,
                  merge_courses, safe_url, seconds, select_course)


def course(key='a', required=True, learned=10, target=60, module='安全手册'):
    return Course(key, '课程' + key, module, required, learned, target)


class TimeTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(seconds('12:29:00'), 44940)
        self.assertEqual(seconds('03:00'), 180)
        self.assertEqual(seconds('120:00'), 7200)
        self.assertEqual(duration(36764), '10:12:44')
        self.assertEqual(duration(12510), '03:28:30')

    def test_invalid(self):
        for value in ('00:60', '1:60:00', '-1:10', '12', 'x:00'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                seconds(value)

    def test_session_and_cumulative_timers(self):
        session = TimerTarget(90, 120, 0)
        cumulative = TimerTarget(90, 120, 90)
        self.assertEqual(session.threshold, 30)
        self.assertTrue(session.met(30))
        self.assertFalse(cumulative.met(30))
        self.assertTrue(cumulative.met(120))
        self.assertEqual(TimerTarget(0, 120, 10).threshold, 120)

    def test_url_redaction(self):
        self.assertEqual(safe_url('https://a/lesson/4?token=secret&courseId=1#fragment'),
                         'https://a/lesson/4?courseId=1')


class IdentityTests(unittest.TestCase):
    def test_progress_does_not_change_identity(self):
        row = {'title': '必学 设备安全', 'module': '安全手册', 'progress': '已学习: 00:00:10 / 00:01:00',
               'required': True, 'row': 0}
        a = Course.from_row(row, '未学', 1)
        row.update(progress='已学习: 00:01:00 / 00:01:00', row=5)
        b = Course.from_row(row, '已学', 4)
        self.assertEqual(a.key, b.key)
        self.assertEqual(merge_courses([a, b])[0].learned, 60)

    def test_duplicate_in_one_tab_is_not_silently_lost(self):
        with self.assertRaises(NeedsAttention):
            merge_courses([course(), replace(course(), page=2)])

    def test_changed_requirement_is_not_silently_merged(self):
        with self.assertRaises(NeedsAttention):
            merge_courses([course(), replace(course(), tab='已学', target=70)])


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.json'
        self.state = State(self.path, 'task/75', 'default')

    def tearDown(self):
        self.temp.cleanup()

    def test_pending_survives_restart(self):
        self.state.mark_pending(course(), {'kind': 'document', 'pilot': True, 'reason': 'site_timer_reached'}, now=100)
        restored = State(self.path, 'task/75', 'default')
        self.assertIn('a', restored.pending())
        restored.reconcile([course(learned=60)], 401, 600)
        self.assertEqual(restored.data['courses']['a']['status'], 'confirmed')
        self.assertEqual(restored.data['validation']['document']['before_seconds'], 10)

    def test_partial_credit_allows_remaining_work(self):
        self.state.mark_pending(course(), {'kind': 'video', 'pilot': True}, now=100)
        self.state.reconcile([course(learned=35)], 401, 600)
        self.assertEqual(self.state.pending(), {})
        self.assertEqual(self.state.data['courses']['a']['status'], 'incomplete')
        self.assertFalse(self.state.validated)

    def test_partial_update_waits_for_site_confirmation_delay(self):
        self.state.mark_pending(course(), {'kind': 'video'}, now=100)
        self.state.reconcile([course(learned=35)], 150, 600, delay=300)
        self.assertIn('a', self.state.pending())
        self.state.reconcile([course(learned=60)], 401, 600, delay=300)
        self.assertEqual(self.state.data['courses']['a']['status'], 'confirmed')

    def test_retries_retain_prior_confirmation_evidence(self):
        self.state.mark_pending(course(), {'kind': 'video'}, now=100)
        self.state.reconcile([course(learned=35)], 401, 600)
        self.state.mark_pending(course(learned=35), {'kind': 'video'}, now=410)
        history = self.state.pending()['a']['history']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['before_seconds'], 10)
        self.assertEqual(history[0]['after_seconds'], 35)

    def test_missing_row_is_not_confirmation(self):
        self.state.mark_pending(course(), {'kind': 'video'}, now=100)
        self.state.reconcile([], 401, 600)
        self.assertIn('a', self.state.pending())
        with self.assertRaises(NeedsAttention):
            self.state.reconcile([], 700, 600)

    def test_local_confirmed_cannot_override_website_regression(self):
        self.state.mark_pending(course(), {'kind': 'document'}, now=100)
        self.state.reconcile([course(learned=60)], 401, 600)
        self.state.reconcile([course(learned=20)], 402, 600)
        self.assertEqual(self.state.data['courses']['a']['status'], 'incomplete')

    def test_hard_interruption_becomes_pending(self):
        self.state.begin(course(), True, 'video')
        restored = State(self.path, 'task/75', 'default')
        restored.recover_interrupted()
        self.assertIn('a', restored.pending())
        self.assertFalse(restored.data['courses']['a']['observation']['pilot'])

    def test_account_mismatch(self):
        self.state.save()
        with self.assertRaises(NeedsAttention):
            State(self.path, 'task/75', 'other')

    def test_expired_confirmation_can_be_retried_after_resume(self):
        self.state.mark_pending(course(), {'kind': 'document', 'pilot': True}, now=100)
        self.assertEqual(self.state.retry_expired([course()], 401, 600), [])
        self.assertEqual(self.state.retry_expired([course()], 701, 600), ['a'])
        self.assertEqual(self.state.pending(), {})
        self.assertEqual(self.state.data['courses']['a']['website_seconds_at_retry'], 10)
        self.assertFalse(self.state.validated)

    def test_pilot_requires_both_types(self):
        self.state.mark_pending(course(), {'kind': 'document', 'pilot': True, 'reason': 'site_timer_reached'}, now=100)
        self.state.reconcile([course(learned=60)], 401, 600)
        self.assertFalse(self.state.validated)
        self.state.mark_pending(course('b'), {'kind': 'video', 'pilot': True, 'reason': 'video_ended_wait_for_server'}, now=100)
        self.state.reconcile([course('b', learned=60)], 401, 600)
        self.assertTrue(self.state.validated)

    def test_required_first_then_optional_until_target(self):
        required, optional = course(), course('b', required=False)
        totals = Totals(0, 60, 0, 60)
        self.assertEqual(select_course([optional, required], totals, self.state), required)
        self.state.mark_pending(required, {'kind': 'document'}, now=100)
        self.assertIsNone(select_course([optional, required], totals, self.state))
        self.state.reconcile([replace(required, learned=60), optional], 401, 600)
        self.assertEqual(select_course([optional], totals, self.state), optional)
        self.assertIsNone(select_course([optional], Totals(60, 60, 60, 60), self.state))

    def test_pending_optional_credit_prevents_extra_course(self):
        optional = course('b', required=False, learned=0, target=60)
        self.state.mark_pending(optional, {'kind': 'video'}, now=100)
        self.assertIsNone(select_course([course('c', required=False)], Totals(60, 60, 0, 60), self.state))


if __name__ == '__main__':
    unittest.main()
