"""Deterministic clock tests of learning transitions, not mirror assertions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import Course, NeedsAttention, State, Totals
from instance import InstanceLock
from runner import Runner


class Clock:
    value = 100.0

    def time(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class Driver:
    def save_screenshot(self, _):
        return True


class MaterialBrowser:
    def __init__(self, clock, kind='document', session=False, frozen=False, pages=2, ends=200,
                 no_timer=False, timer_speed=1):
        self.clock, self.kind, self.session, self.frozen = clock, kind, session, frozen
        self.pages, self.page, self.ends = pages, 1, ends
        self.driver = Driver()
        self.return_count = 0
        self.play_count = 0
        self.video_started = None
        self.no_timer, self.timer_speed = no_timer, timer_speed

    def open_course(self, course):
        self.course = course
        self.started = self.clock.time()
        return course

    def sample(self):
        elapsed = self.clock.time() - self.started
        learned = (0 if self.session else self.course.learned) + (0 if self.frozen else int(elapsed*self.timer_speed))
        timer = {'learned': learned, 'target': self.course.target}
        video_elapsed = self.clock.time() - (self.video_started if self.video_started is not None else self.started)
        video = None if self.kind == 'document' else {'time': min(video_elapsed, self.ends), 'duration': self.ends,
                  'ready': 4, 'ended': video_elapsed >= self.ends, 'paused': False, 'error': None, 'rate': 1}
        return {'timer': None if self.no_timer else timer, 'counters': [] if self.no_timer else [timer], 'video': video,
                'page': self.page if self.kind == 'document' else None,
                'pages': self.pages if self.kind == 'document' else None,
                'viewer_context': (), 'video_context': ()}

    def play(self, _):
        self.video_started = self.clock.time()
        self.play_count += 1
        return True

    def turn_document(self, _, target):
        if self.page < target:
            self.page += 1

    def return_to_task(self):
        self.return_count += 1


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.clock = Clock()
        self.state = State(self.folder / 'state.json', 'task/75', 'default')
        self.settings = {'poll_seconds': 1, 'stall_seconds': 4,
                         'video_upload_interval_seconds': 0,
                         'confirmation_delay_seconds': 300, 'confirmation_timeout_seconds': 600}
        self.course = Course('current', '设备安全', '安全手册', True, 90, 92)
        self.patchers = [patch('runner.time.time', self.clock.time),
                         patch('runner.time.monotonic', self.clock.time),
                         patch('runner.time.sleep', self.clock.sleep)]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def runner(self, browser):
        return Runner(browser, self.state, self.settings, self.folder)

    def test_document_cumulative_timer_and_pages_then_pending(self):
        browser = MaterialBrowser(self.clock)
        self.runner(browser).learn(self.course, pilot=True)
        record = self.state.pending()['current']
        self.assertEqual(record['observation']['document_pages_seen'], [1, 2])
        self.assertEqual(record['observation']['final_timer_seconds'], 92)
        self.assertEqual(browser.return_count, 1)
        self.assertFalse(self.state.validated)

    def test_finished_video_is_replayed_until_required_interval(self):
        browser = MaterialBrowser(self.clock, kind='video', ends=3, timer_speed=0.5)
        course = Course('current', '消防安全', '微课堂', True, 0, 8)
        self.runner(browser).learn(course, pilot=True)
        self.assertGreater(browser.play_count, 1)
        self.assertGreaterEqual(self.state.pending()['current']['observation']['final_timer_seconds'], 8)
        self.assertFalse(self.state.validated)

    def test_session_timer_does_not_require_repeating_previous_credit(self):
        browser = MaterialBrowser(self.clock, session=True)
        self.runner(browser).learn(self.course)
        observation = self.state.pending()['current']['observation']
        self.assertEqual(observation['timer_mode'], 'session')
        self.assertEqual(observation['elapsed_seconds'], 2)

    def test_loading_timer_is_not_treated_as_a_requirement_mismatch(self):
        for placeholder in (0, 120):
            with self.subTest(placeholder=placeholder):
                browser = MaterialBrowser(self.clock, pages=1)
                sample = browser.sample
                def delayed_sample():
                    data = sample()
                    if self.clock.time() - browser.started < 3:
                        data['timer']['target'] = placeholder
                    return data
                with patch.object(browser, 'sample', side_effect=delayed_sample):
                    self.runner(browser).learn(Course('current', '异步加载文档', '安全知识', False, 0, 6))
                observation = self.state.pending()['current']['observation']
                self.assertEqual(observation['initial_timer_seconds'], 3)
                self.assertGreaterEqual(observation['final_timer_seconds'], 6)
                self.assertEqual(observation['reason'], 'site_timer_reached')
                self.assertEqual(browser.return_count, 1)

    def test_persistent_requirement_mismatch_keeps_page_without_pending_credit(self):
        browser = MaterialBrowser(self.clock, pages=1)
        sample = browser.sample
        def mismatched_sample():
            data = sample()
            data['timer']['target'] = 180
            return data
        with patch.object(browser, 'sample', side_effect=mismatched_sample):
            with self.assertRaisesRegex(NeedsAttention, '列表 00:01:00，学习页 00:03:00'):
                self.runner(browser).learn(Course('current', '时长真正不同', '安全知识', False, 0, 60))
        self.assertEqual(self.clock.time(), 130)
        self.assertEqual(browser.return_count, 0)
        self.assertFalse(self.state.pending())
        self.assertIn('active', self.state.data)

    def test_timer_loading_wait_responds_to_stop(self):
        browser = MaterialBrowser(self.clock, pages=1)
        sample = browser.sample
        def loading_sample():
            data = sample()
            data['timer']['target'] = 0
            (self.folder / 'stop.flag').touch()
            return data
        with patch.object(browser, 'sample', side_effect=loading_sample):
            with self.assertRaises(KeyboardInterrupt):
                self.runner(browser).learn(self.course)
        self.assertEqual(browser.return_count, 0)
        self.assertFalse(self.state.pending())

    def test_video_end_does_not_grant_credit(self):
        browser = MaterialBrowser(self.clock, kind='video', frozen=True, ends=3, no_timer=True)
        course = Course('current', '消防安全', '微课堂', True, 0, 60)
        self.runner(browser).learn(course, pilot=True)
        record = self.state.pending()['current']
        self.assertGreaterEqual(record['observation']['observed_playing_seconds'], course.target)
        self.assertEqual(record['status'], 'pending')
        self.assertFalse(self.state.validated)

    def test_video_introduction_timer_is_not_treated_as_document(self):
        browser = MaterialBrowser(self.clock, kind='document')
        course = Course('current', '消防安全', '微课堂', True, 0, 60)
        with self.assertRaises(NeedsAttention):
            self.runner(browser).learn(course, pilot=True)
        self.assertEqual(browser.return_count, 0)
        self.assertFalse(self.state.pending())

    def test_optional_learning_budget_can_stop_before_whole_video(self):
        browser = MaterialBrowser(self.clock, kind='video', frozen=True, no_timer=True)
        course = Course('current', '选学消防', '微课堂', False, 0, 60)
        self.runner(browser).learn(course, budget=10)
        observation = self.state.pending()['current']['observation']
        self.assertEqual(observation['observed_playing_seconds'], 10)
        self.assertIsNone(observation['final_timer_seconds'])

    def test_short_video_topups_wait_for_complete_upload_interval(self):
        self.settings['video_upload_interval_seconds'] = 15
        for remaining in (3, 10, 100):
            with self.subTest(remaining=remaining):
                browser = MaterialBrowser(self.clock, kind='video', ends=500)
                course = Course('current', '视频短时补学', '安全视频', False, 300-remaining, 300)
                self.runner(browser).learn(course)
                observation = self.state.pending()['current']['observation']
                expected = ((remaining+14)//15)*15+1
                self.assertEqual(observation['observed_playing_seconds'], expected)
                self.assertEqual(observation['minimum_playing_seconds'], expected)
                self.assertGreaterEqual(observation['final_timer_seconds'], 300)
                self.assertEqual(observation['reason'], 'site_timer_reached')

    def test_short_video_replays_until_upload_interval_is_covered(self):
        self.settings['video_upload_interval_seconds'] = 15
        browser = MaterialBrowser(self.clock, kind='video', ends=3)
        self.runner(browser).learn(Course('current', '结尾补学', '安全视频', False, 57, 60))
        observation = self.state.pending()['current']['observation']
        self.assertGreater(browser.play_count, 1)
        self.assertGreaterEqual(observation['observed_playing_seconds'], 16)
        self.assertGreaterEqual(observation['final_timer_seconds'], 60)

    def test_advancing_page_timer_cannot_substitute_for_frozen_video(self):
        self.settings['video_upload_interval_seconds'] = 15
        browser = MaterialBrowser(self.clock, kind='video')
        sample = browser.sample
        def frozen_video_sample():
            data = sample()
            data['video']['time'] = 0
            return data
        with patch.object(browser, 'sample', side_effect=frozen_video_sample):
            with self.assertRaisesRegex(NeedsAttention, '实际播放进度持续未增加'):
                self.runner(browser).learn(Course('current', '播放卡住', '安全视频', False, 57, 60))
        self.assertEqual(browser.return_count, 0)
        self.assertFalse(self.state.pending())

    def test_stalled_document_keeps_durable_active_record(self):
        browser = MaterialBrowser(self.clock, frozen=True, pages=1)
        with self.assertRaises(NeedsAttention):
            self.runner(browser).learn(self.course)
        self.assertEqual(self.state.data['active']['course']['key'], 'current')
        self.assertEqual(self.state.pending(), {})

    def test_manual_pause_is_not_confirmation(self):
        browser = MaterialBrowser(self.clock)
        (self.folder / 'pause.flag').touch()
        with self.assertRaises(NeedsAttention):
            self.runner(browser).learn(self.course)
        self.assertIn('active', self.state.data)
        self.assertFalse(self.state.validated)

    def test_pending_checkpoint_does_not_interrupt_optional_document(self):
        old = Course('old', '前一课', '安全手册', True, 0, 5)
        self.state.mark_pending(old, {'kind': 'document'}, now=100)
        self.settings['confirmation_delay_seconds'] = 3
        browser = MaterialBrowser(self.clock, pages=1)
        course = Course('current', '下一课', '安全手册', False, 0, 60)
        self.runner(browser).learn(course)
        self.assertEqual(self.state.pending()['current']['observation']['reason'],
                         'site_timer_reached')
        observation = self.state.pending()['current']['observation']
        self.assertEqual(observation['final_timer_seconds'], 60)
        self.assertEqual(observation['elapsed_seconds'], 60)
        self.assertTrue(observation['server_check_deferred'])
        self.assertEqual(set(self.state.pending()), {'old', 'current'})

    def test_overdue_checkpoint_does_not_interrupt_resumed_optional_document(self):
        for session in (False, True):
            with self.subTest(session=session):
                self.state.mark_pending(Course('old', '前一课', '安全手册', False, 0, 5),
                                        {'kind': 'document'}, now=self.clock.time() - 301)
                browser = MaterialBrowser(self.clock, session=session, timer_speed=0.5)
                self.runner(browser).learn(Course('current', '选学补学', '安全手册', False, 45, 60))
                observation = self.state.pending()['current']['observation']
                self.assertEqual(observation['reason'], 'site_timer_reached')
                self.assertEqual(observation['elapsed_seconds'], 30)
                self.assertEqual(observation['final_timer_seconds'], 15 if session else 60)
                self.assertTrue(observation['server_check_deferred'])
                self.assertEqual(browser.return_count, 1)

    def test_pending_checkpoint_does_not_interrupt_video(self):
        self.state.mark_pending(Course('old', '前一课', '安全手册', True, 0, 5), {'kind': 'document'}, now=100)
        self.settings['confirmation_delay_seconds'] = 3
        browser = MaterialBrowser(self.clock, kind='video')
        self.runner(browser).learn(Course('current', '本课视频', '微课堂', True, 0, 8))
        observation = self.state.pending()['current']['observation']
        self.assertEqual(observation['final_timer_seconds'], 8)
        self.assertTrue(observation['server_check_deferred'])

    def test_slow_site_timer_is_required_even_when_playing_budget_is_met(self):
        browser = MaterialBrowser(self.clock, kind='video', timer_speed=0.5)
        self.runner(browser).learn(Course('current', '计时滞后视频', '微课堂', True, 0, 8))
        observation = self.state.pending()['current']['observation']
        self.assertGreaterEqual(observation['elapsed_seconds'], 16)
        self.assertEqual(observation['final_timer_seconds'], 8)

    def test_frozen_site_timer_pauses_instead_of_switching(self):
        browser = MaterialBrowser(self.clock, kind='video', frozen=True)
        with self.assertRaises(NeedsAttention):
            self.runner(browser).learn(Course('current', '计时卡住视频', '微课堂', True, 0, 8))
        self.assertEqual(browser.return_count, 0)
        self.assertFalse(self.state.pending())

    def test_single_instance_lock_releases_after_close(self):
        a = InstanceLock(self.folder / 'instance.lock')
        b = InstanceLock(self.folder / 'instance.lock')
        a.acquire()
        try:
            with self.assertRaises(RuntimeError):
                b.acquire()
        finally:
            a.release()
        b.acquire()
        b.release()

    def test_pending_questionbank_waits_without_rescanning_then_resumes(self):
        bank = Course('bank', '题库', '题库练习', True, 0, 900)
        self.state.data['validation'] = {'document': {'status': 'passed'}, 'video': {'status': 'passed'}}
        self.state.mark_pending(bank, {'kind': 'questionbank'}, now=self.clock.time())
        runner = self.runner(MaterialBrowser(self.clock))
        checks = []
        def snapshot():
            checks.append(self.clock.time())
            learned = 60 if self.clock.time() >= 400 else 0
            current = Course('bank', '题库', '题库练习', True, learned, 900)
            self.state.reconcile([current], self.clock.time(), 600, delay=300)
            return [current], Totals(learned, 900, 0, 0)
        resumed = []
        def learn(course, **kwargs):
            resumed.append((course.key, course.learned, self.clock.time()))
            raise KeyboardInterrupt
        with patch.object(runner, 'snapshot', side_effect=snapshot), patch.object(runner, 'learn', side_effect=learn):
            with self.assertRaises(KeyboardInterrupt):
                runner.run()
        self.assertEqual(checks, [100, 400])
        self.assertEqual(resumed, [('bank', 60, 400)])

    def test_unchanged_pending_records_are_checked_at_most_once_per_minute(self):
        bank = Course('bank', '题库', '题库练习', True, 0, 900)
        self.state.mark_pending(bank, {'kind': 'questionbank'}, now=-200)
        runner = self.runner(MaterialBrowser(self.clock))
        checks = []
        def snapshot():
            checks.append(self.clock.time())
            if len(checks) == 3:
                raise KeyboardInterrupt
            return [bank], Totals(0, 900, 0, 0)
        with patch.object(runner, 'snapshot', side_effect=snapshot):
            with self.assertRaises(KeyboardInterrupt):
                runner.run()
        self.assertEqual(checks, [100, 160, 220])

    def test_confirmation_wait_responds_to_stop_without_navigating(self):
        runner = self.runner(MaterialBrowser(self.clock))
        def sleep(seconds):
            self.clock.sleep(seconds)
            (self.folder / 'stop.flag').touch()
        with patch('runner.time.sleep', side_effect=sleep):
            with self.assertRaises(KeyboardInterrupt):
                runner.wait_for_site_check(400)
        self.assertEqual(self.clock.time(), 101)


if __name__ == '__main__':
    unittest.main()
