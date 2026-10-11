"""UI-only learning loop with a real-site pilot gate and delayed confirmation."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import logging
import math
import time

from selenium.common.exceptions import WebDriverException

from core import (Course, NeedsAttention, State, TimerTarget, duration,
                  select_course, write_json)


class Runner:
    def __init__(self, browser, state: State, settings: dict, output: Path, validate_only=False):
        self.browser, self.state, self.settings, self.output = browser, state, settings, output
        self.validate_only = validate_only
        self.state.recover_interrupted()
        self.active = None
        self.active_kind = None
        self.active_pilot = False
        self.idle_since = None

    def status(self, phase, **extra):
        write_json(self.output / 'status.json', {'phase': phase, 'updated_at': time.time(), **extra})

    def snapshot(self, refresh=True):
        self.browser.load_task(refresh=refresh)
        courses, totals = self.browser.inventory()
        self.browser.export_inventory(courses, totals)
        self.state.reconcile(courses, time.time(), self.settings['confirmation_timeout_seconds'],
                             delay=self.settings['confirmation_delay_seconds'])
        write_json(self.output / 'validation.json', {'real_site': self.state.data['validation'],
                    'both_material_types_passed': self.state.validated})
        self.status('checking', totals=asdict(totals), courses=len(courses),
                    pending=len(self.state.pending()), validated=self.state.validated)
        logging.info('必学 %s/%s；选学 %s/%s；待确认 %d',
                     duration(totals.required_learned), duration(totals.required_target),
                     duration(totals.optional_learned), duration(totals.optional_target), len(self.state.pending()))
        return courses, totals

    def pilot_candidate(self, courses):
        remaining_kinds = [k for k in ('document', 'video')
                           if self.state.data['validation'].get(k, {}).get('status') != 'passed']
        pending = self.state.pending()
        for kind in remaining_kinds:
            if any(r['observation'].get('pilot') and r['observation']['kind'] == kind
                   for r in pending.values()):
                continue
            choices = [c for c in courses if c.kind_hint == kind and not c.complete and c.key not in pending]
            choices.sort(key=lambda c: (not c.required, c.remaining))
            if choices:
                return choices[0]
        if pending:
            return None
        raise NeedsAttention('首次真实验证需要至少一项未完成文档和一项未完成视频；当前列表无法提供。')

    def checkpoint_due(self):
        pending = self.state.pending()
        if not pending:
            return float('inf')
        return min(r['submitted_at'] + self.settings['confirmation_delay_seconds'] for r in pending.values())

    def wait_for_site_check(self, deadline):
        """Wait without navigating or enumerating course pages; keep controls responsive."""
        logging.info('等待网站记录更新，暂停课程列表扫描。')
        while time.time() < deadline:
            if (self.output / 'stop.flag').exists():
                raise KeyboardInterrupt
            if (self.output / 'pause.flag').exists():
                raise NeedsAttention('用户请求暂停。处理完后运行“继续学习.cmd”。')
            self.status('waiting_for_server', pending=len(self.state.pending()),
                        next_check_at=deadline, remaining_seconds=max(0, int(deadline - time.time())))
            time.sleep(max(0, min(self.settings['poll_seconds'], deadline - time.time())))

    def learn(self, course: Course, pilot=False, budget=None, opened=False):
        current = course if opened else self.browser.open_course(course)
        if current is None:
            return
        self.active, self.active_pilot = current, pilot
        self.state.begin(current, pilot)
        budget = min(current.remaining, budget) if budget is not None else current.remaining
        logging.info('%s: %s，网站已学 %s，要求 %s', '真实试运行' if pilot else '学习',
                     current.title, duration(current.learned), duration(current.target))
        if current.kind_hint == 'questionbank':
            from questionbank import QuestionBank
            QuestionBank(self).learn(current, budget)
            return
        samples_deadline = time.monotonic() + 30
        first = None
        unmatched_target = None
        while time.monotonic() < samples_deadline:
            if (self.output / 'stop.flag').exists():
                raise KeyboardInterrupt
            if (self.output / 'pause.flag').exists():
                raise NeedsAttention('用户请求暂停。处理完后运行“继续学习.cmd”。')
            sample = self.browser.sample()
            if sample['video'] or (sample['timer'] and current.kind_hint != 'video'):
                if sample['timer'] and sample['timer']['target'] != current.target:
                    # The SPA renders a 00:00 timer before the lesson data arrives.
                    # A previous lesson's timer can also remain during navigation.
                    if unmatched_target is None:
                        logging.info('材料计时器尚未加载一致，等待页面数据: %s（列表 %s，当前 %s）',
                                     current.title, duration(current.target), duration(sample['timer']['target']))
                    unmatched_target = sample['timer']['target']
                    self.status('waiting_for_material', title=current.title,
                                required_seconds=current.target, page_required_seconds=unmatched_target)
                else:
                    first = sample
                    if unmatched_target is not None:
                        logging.info('材料计时器加载完成，要求已一致: %s，%s',
                                     current.title, duration(current.target))
                    break
            time.sleep(self.settings['poll_seconds'])
        if first is None:
            if unmatched_target is not None:
                raise NeedsAttention('等待学习页加载 30 秒后，要求仍与课程列表不同: '
                                     + current.title + '（列表 ' + duration(current.target)
                                     + '，学习页 ' + duration(unmatched_target) + '）。已保留页面。')
            raise NeedsAttention('未识别材料播放器或阅读计时器（视频目录页不能视作文档）: ' + current.title)
        kind = 'video' if first['video'] else 'document'
        self.active_kind = kind
        self.state.begin(current, pilot, kind)
        if len(first['counters']) > 1:
            raise NeedsAttention('此课程包含多个课时计时器，需要核对目录结构后适配，已暂停。')
        if kind == 'document' and not first['timer']:
            raise NeedsAttention('文档缺少可识别的阅读计时器。')
        if kind == 'document' and first['page'] and first['page'] > 1:
            self.browser.rewind_document(first['viewer_context'])
            first = self.browser.sample()
        if first['timer'] and first['timer']['target'] != current.target:
            raise NeedsAttention('学习页要求与课程列表要求不同，不能按单课自动完成。')
        timer = TimerTarget(current.learned, current.learned + budget, first['timer']['learned']) if first['timer'] else None
        minimum_playing = budget
        upload_interval = self.settings.get('video_upload_interval_seconds', 15)
        if kind == 'video' and timer and upload_interval > 0:
            # The site's video timer only uploads complete 15-second intervals.
            # Returning immediately after a short top-up discards its last interval.
            minimum_playing = math.ceil(budget / upload_interval) * upload_interval + self.settings['poll_seconds']
            logging.info('视频本次需补 %s，持续实际播放至少 %s，等待完整学时上传周期。',
                         duration(budget), duration(minimum_playing))
        if kind == 'video':
            self.browser.play(first['video_context'])
        self.browser.driver.save_screenshot(str(self.output / f'pilot-{kind}-before.png')) if pilot else None
        started = time.monotonic()
        last = first
        last_check = started
        last_motion = started
        last_counter_motion = started
        last_playback_motion = started
        played_wall = 0.0
        recovery_used = False
        seen_pages = {first['page']} if first['page'] else set()
        reason = ''
        checkpoint_deferred = False
        while True:
            if (self.output / 'stop.flag').exists():
                raise KeyboardInterrupt
            if (self.output / 'pause.flag').exists():
                raise NeedsAttention('用户请求暂停。处理完后运行“继续学习.cmd”。')
            time.sleep(self.settings['poll_seconds'])
            sample = self.browser.sample()
            now = time.monotonic()
            delta = min(now - last_check, self.settings['poll_seconds'] * 2)
            last_check = now
            elapsed = now - started
            counter = sample['timer']['learned'] if sample['timer'] else None
            if first['timer'] and sample['timer'] and sample['timer']['target'] != first['timer']['target']:
                raise NeedsAttention('学习期间计时器对应的课时发生变化。')
            if counter is not None and last['timer'] and counter > last['timer']['learned']:
                last_motion = now
                last_counter_motion = now
            if kind == 'video':
                video = sample['video']
                if video is None:
                    if now - last_motion >= self.settings['stall_seconds']:
                        raise NeedsAttention('播放器持续消失，需人工检查。')
                else:
                    if video['error']:
                        raise NeedsAttention('视频播放错误，错误码: ' + str(video['error']))
                    if abs(video['rate'] - 1) > 0.01:
                        self.browser.play(sample['video_context'])
                    previous = last['video']
                    advancing = previous and video['time'] > previous['time']
                    if advancing and not previous['paused'] and previous['ready'] >= 2:
                        played_wall += delta
                        last_motion = now
                        last_playback_motion = now
                    if video['paused'] and not video['ended'] and not recovery_used:
                        self.browser.play(sample['video_context'])
                        recovery_used = True
                    if video['ended']:
                        needs_replay = ((counter is None or not timer.met(counter)) if timer else False) or played_wall < minimum_playing
                        if needs_replay:
                            logging.info('视频已结束，学习计时仍未足够；从头正常重播。')
                            self.browser.play(sample['video_context'])
                            recovery_used = False
                        else:
                            reason = 'video_ended_wait_for_server'
                    elif timer and counter is not None and timer.met(counter) and played_wall >= minimum_playing:
                        reason = 'site_timer_reached'
                    elif timer is None and played_wall >= budget:
                        # Observed active wall time schedules a server check, never grants credit.
                        reason = 'observed_playing_interval_wait_for_server'
            else:
                if sample['page']:
                    seen_pages.add(sample['page'])
                if sample['pages'] and sample['page']:
                    desired = min(sample['pages'], 1 + int(elapsed * sample['pages'] / max(1, budget)))
                    self.browser.turn_document(sample['viewer_context'], desired)
                if counter is not None and timer.met(counter):
                    if not sample['pages'] or len(seen_pages) == sample['pages']:
                        reason = 'site_timer_reached'
                    elif elapsed > budget + 60:
                        raise NeedsAttention('阅读计时已足，但文档翻页未完成，请人工检查查看器。')
            checkpoint_due = not reason and time.time() >= self.checkpoint_due()
            if checkpoint_due and not checkpoint_deferred:
                logging.info('前一课记录到核对时间，当前%s继续学习至网页计时达标后再核对。',
                             '视频' if kind == 'video' else '文档')
                checkpoint_deferred = True
            self.status('pilot' if pilot else 'learning', title=current.title, kind=kind,
                        before_seconds=current.learned, required_seconds=current.target,
                        site_timer_seconds=counter, observed_playing_seconds=int(played_wall),
                        minimum_playing_seconds=minimum_playing if kind == 'video' else None,
                        server_check_deferred=checkpoint_deferred)
            if reason:
                break
            if kind == 'video' and timer and now - last_counter_motion >= self.settings['stall_seconds']:
                raise NeedsAttention('网站学习计时持续未增加，当前视频已保留；请检查页面计时后继续。')
            if kind == 'video' and now - last_playback_motion >= self.settings['stall_seconds']:
                raise NeedsAttention('视频实际播放进度持续未增加，已保留页面；不能仅凭网页计时切换。')
            if now - last_motion >= self.settings['stall_seconds']:
                raise NeedsAttention('计时或播放已停滞超过设定时间，请检查前台窗口和页面提示。')
            last = sample
        observation = {'kind': kind, 'pilot': pilot, 'reason': reason,
                       'timer_mode': timer.mode if timer else 'no_live_timer',
                       'initial_timer_seconds': first['timer']['learned'] if first['timer'] else None,
                       'final_timer_seconds': counter, 'observed_playing_seconds': int(played_wall),
                       'elapsed_seconds': int(elapsed), 'document_pages_seen': sorted(seen_pages)}
        observation['server_check_deferred'] = checkpoint_deferred
        if kind == 'video':
            observation['minimum_playing_seconds'] = minimum_playing
            observation['video_upload_interval_seconds'] = upload_interval
        if pilot:
            self.browser.driver.save_screenshot(str(self.output / f'pilot-{kind}-after.png'))
        self.browser.return_to_task()
        self.state.mark_pending(current, observation)
        self.active = None
        logging.info('已返回总览，等待网站确认: %s', current.title)

    def recover_active(self):
        if self.active is not None:
            self.browser.return_to_task()
            self.state.mark_pending(self.active, {'kind': self.active_kind or 'unknown',
                                    'pilot': self.active_pilot, 'reason': 'interrupted_wait_for_server'})
            if self.active_kind == 'questionbank':
                # Browsing alone did not save credit in the real-site pilot.
                # Retain the attempt, then read the website afresh on restart.
                record = self.state.data['courses'][self.active.key]
                record['status'] = 'incomplete'
                record['observation']['reason'] = 'interrupted_questionbank_recheck_website'
                self.state.save()
            self.active = None

    def run(self):
        while True:
            if (self.output / 'stop.flag').exists():
                raise KeyboardInterrupt
            courses, totals = self.snapshot()
            all_required_met = all(c.complete for c in courses if c.required)
            if totals.met and all_required_met and not self.state.pending():
                self.status('complete', totals=asdict(totals), validated=self.state.validated)
                logging.info('网站确认必学、选学已达标。学习辅助结束。')
                return
            if self.validate_only and self.state.validated and not self.state.pending():
                self.status('validation_complete', totals=asdict(totals))
                logging.info('文档与视频的真实计时验证均通过。')
                return
            due = self.checkpoint_due()
            if self.state.pending() and time.time() >= due:
                # A delayed record gets one full recheck per minute, not continuous pagination.
                self.wait_for_site_check(time.time() + 60)
                continue
            pilot = not self.state.validated
            selected = self.pilot_candidate(courses) if pilot else select_course(courses, totals, self.state)
            if selected:
                self.idle_since = None
                budget = None if pilot or selected.required else max(1, totals.optional_target - totals.optional_learned)
                self.learn(selected, pilot=pilot, budget=budget)
            else:
                if self.idle_since is None:
                    self.idle_since = time.time()
                if not self.state.pending() and time.time() - self.idle_since >= self.settings['confirmation_timeout_seconds']:
                    raise NeedsAttention('未学条目已处理，但总时长未达标；请核对网站要求和已学记录。')
                self.wait_for_site_check(max(time.time() + 60, due) if self.state.pending()
                                         else time.time() + 60)

    def wait_for_resume(self, reason):
        while True:
            self.browser.pause_media()
            self.browser.diagnostic(reason)
            self.status('paused', reason=reason)
            logging.warning('%s', reason)
            logging.warning('请处理浏览器中的问题，再双击“继续学习.cmd”。按 Ctrl+C 可退出。')
            # Continuing requires an action after this pause, not a stale signal.
            signal = self.output / 'resume.flag'
            signal.unlink(missing_ok=True)
            while not signal.exists():
                if (self.output / 'stop.flag').exists():
                    raise KeyboardInterrupt
                time.sleep(1)
            signal.unlink(missing_ok=True)
            (self.output / 'pause.flag').unlink(missing_ok=True)
            try:
                self.browser.driver.switch_to.default_content()
                self.browser.close_questionbank_picker()
                self.browser.blockers()
                if self.active is not None and self.active_kind is None:
                    self.learn(self.active, pilot=self.active_pilot, opened=True)
                    return
                self.recover_active()
                self.browser.load_task(refresh=True)
                courses, _ = self.browser.inventory()
                retried = self.state.retry_expired(courses, time.time(), self.settings['confirmation_timeout_seconds'])
                if retried:
                    logging.warning('已按网站当前记录重排 %d 项未确认课程，失败记录已保留。', len(retried))
                return
            except (NeedsAttention, WebDriverException) as exc:
                reason = str(exc)[:300] if isinstance(exc, NeedsAttention) else type(exc).__name__
