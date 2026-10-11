"""Question-bank browsing through normal UI, with website timer and exit saving."""
from __future__ import annotations

from pathlib import Path
import logging
import re
import time

from core import NeedsAttention, seconds, write_json


DOM = r"""
const visible=e=>!!(e&&e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden');
const text=e=>(e?.innerText||'').trim();
const mode=arguments[0];
if(mode==='banks') {
  const modal=Array.from(document.querySelectorAll('.ivu-modal')).find(e=>visible(e)&&text(e).includes('可练习题库'));
  return modal?Array.from(modal.querySelectorAll('.tkbox li')).filter(visible)
    .map(e=>({name:text(e),element:e})).filter(b=>/^\d+\./.test(b.name)&&!b.name.includes('错题集')):[];
}
if(mode==='sample') {
  const root=document.querySelector('.exercises');
  if(!visible(root)||text(root.querySelector('.top-tit'))!=='在线练习')return null;
  const timer=root.querySelector('.time');
  const match=text(timer).match(/\d+:\d{2}:\d{2}/);
  const items=Array.from(root.querySelectorAll('.m-item'));
  const exact=label=>Array.from(root.querySelectorAll('*')).filter(e=>visible(e)&&text(e)===label)
    .sort((a,b)=>a.querySelectorAll('*').length-b.querySelectorAll('*').length)[0]||null;
  const next=exact('下一页');
  const disabled=e=>!e||e.disabled||e.getAttribute('aria-disabled')==='true'||/(?:^|\s)(?:disabled|is-disabled)(?:\s|$)/.test(e.className||'');
  return {timer:match?.[0]||null,first:items[0]?.id||null,last:items.at(-1)?.id||null,
    questions:items.map(e=>({id:e.id,title:text(e.querySelector('.desc > p .subject-title')),
      type:text(e.querySelector('.type')),options:Array.from(e.querySelectorAll('label')).map(text),
      answered:!!e.querySelector('input:disabled'),result:text(e.querySelector('.result-box'))})),
    next:disabled(next)?null:next,previous:exact('上一页'),exit:root.querySelector('.top-box .back')};
}
if(mode==='scroll') {
  const items=Array.from(document.querySelectorAll('.exercises .m-item'));
  const e=items[Number(arguments[1])%Math.max(1,items.length)];
  if(e)e.scrollIntoView({block:'start',behavior:'instant'});
  return !!e;
}
throw new Error('Unknown questionbank operation');
"""


class QuestionBank:
    def __init__(self, runner):
        self.runner = runner
        self.browser = runner.browser
        self.driver = self.browser.driver
        self.output: Path = runner.output
        self.cursor_path = self.output / 'questionbank-cursor.json'

    def js(self, mode, *args):
        return self.driver.execute_script(DOM, mode, *args)

    def cursor(self):
        import json
        if self.cursor_path.exists():
            return int(json.loads(self.cursor_path.read_text(encoding='utf-8')).get('next_bank', 0))
        return 0

    def sample(self):
        self.browser.blockers()
        if not re.search(r'/questionBank/exercises/\d+(?:[?#].*)?$', self.driver.current_url):
            raise NeedsAttention('题库练习页面已改变，保留当前页面等待检查。')
        data = self.js('sample')
        if not data or not data['timer'] or not data['first']:
            raise NeedsAttention('未识别题库练习计时器或题目。')
        data['seconds'] = seconds(data['timer'])
        return data

    def open_bank(self, index):
        self.browser.wait.until(lambda _: bool(self.js('banks')))
        banks = self.js('banks')
        chosen = banks[index % len(banks)]
        chosen['element'].click()
        def ready(_):
            data = self.js('sample')
            return data if data and data['timer'] and data['first'] else False
        self.browser.wait.until(ready)
        first = self.sample()
        write_json(self.output / 'questionbank-page.json', {
            'captured_at': time.time(), 'bank': chosen['name'], 'questions': first['questions']})
        return chosen['name'], first

    def find_answer(self):
        """Read at most three pages; reserve exactly one unsubmitted exercise."""
        from reference_answers import ReferenceAnswers
        references = ReferenceAnswers()
        for _ in range(3):
            data = self.sample()
            for question in data['questions']:
                match = references.match(question)
                if match:
                    return question, match, data
            if not data['next']:
                break
            previous = data['first']
            data['next'].click()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.js('sample')['first'] == previous:
                time.sleep(0.3)
            if self.js('sample')['first'] == previous:
                break
        return None

    def answer_one(self, question, match):
        """User authorized one verified exercise per timed session, 2026-10-08."""
        from reference_answers import ReferenceAnswers
        current = next((q for q in self.sample()['questions'] if q['id'] == question['id']), None)
        if current is None or ReferenceAnswers().match(current) != match:
            raise NeedsAttention('待提交练习题发生变化，保留页面等待检查。')
        labels = self.driver.find_elements('css selector', '#' + question['id'] + ' label')
        for index in match['indices']:
            labels[index].click()
        if '多选题' in question['type']:
            submit = self.driver.find_elements('css selector', '#' + question['id'] + ' .sub')
            if not submit:
                raise NeedsAttention('多选练习提交按钮未出现，保留页面。')
            submit[0].click()
        def feedback(_):
            data = self.js('sample')
            return next((q for q in data['questions'] if q['id'] == question['id'] and q['result']), None) if data else None
        result = self.browser.wait.until(feedback)
        answers = re.findall(r'(?:正确答案|我的答案)\s*[:：]\s*([A-H,，、\s]+)', result['result'])
        if len(answers) != 2 or re.findall('[A-H]', answers[0]) != re.findall('[A-H]', answers[1]):
            raise NeedsAttention('参考答案与网站反馈不一致，保留页面停止自动答题。')
        record = {'captured_at': time.time(), 'question': question['title'],
                  'answer': answers[1].strip(), 'feedback': result['result'], **match}
        import json
        with (self.output / 'questionbank-answers.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + '\n')
        logging.info('已提交并由网站判定正确: %s（参考 PDF 第 %d 页）。', question['title'], match['pdf_page'])
        return record

    def exit(self):
        data = self.sample()
        if not data.get('exit'):
            raise NeedsAttention('未识别题库自己的退出按钮，不能确认练习保存。')
        data['exit'].click()
        self.browser.wait.until(lambda _: self.browser.is_task())
        self.browser.load_task(refresh=True)
        return data['seconds']

    def learn(self, current, budget=None):
        runner = self.runner
        runner.active_kind = 'questionbank'
        runner.state.begin(current, False, 'questionbank')
        index = self.cursor()
        bank, first = self.open_bank(index)
        prepared = self.find_answer()
        for _ in range(7):
            if prepared:
                break
            logging.info('%s 前三页没有严格匹配的未答题目，继续检查下一题库。', bank)
            self.exit()
            index += 1
            self.browser.open_course(current)
            bank, first = self.open_bank(index)
            prepared = self.find_answer()
        if not prepared:
            raise NeedsAttention('八个题库前三页均未找到题干与全部选项一致的未答题目，保留页面。')
        question, match, first = prepared
        write_json(self.output / 'questionbank-page.json', {
            'captured_at': time.time(), 'bank': bank, 'questions': first['questions'],
            'reserved_question': question['title'], 'reference': match})
        if getattr(self.driver, '_labsafe_native', False):
            self.driver.rpc('Page.bringToFront')
        session = int(runner.settings.get('questionbank_session_seconds', 900))
        wanted = min(current.remaining, session, budget if budget is not None else current.remaining)
        if wanted <= 0:
            raise NeedsAttention('题库本次练习时长必须大于零。')
        started = time.monotonic()
        last_motion = started
        last_browse = started
        initial = first['seconds']
        last = first
        browsed = 0
        pages_seen = {first['first']}
        logging.info('题库浏览: %s，本次目标 %d 秒，按网站练习时间切换。', bank, wanted)
        while True:
            if (self.output / 'stop.flag').exists():
                raise KeyboardInterrupt
            if (self.output / 'pause.flag').exists():
                raise NeedsAttention('用户请求暂停题库浏览。')
            time.sleep(runner.settings['poll_seconds'])
            data = self.sample()
            now = time.monotonic()
            if data['seconds'] < last['seconds']:
                raise NeedsAttention('题库练习计时发生回退，已暂停。')
            if data['seconds'] > last['seconds']:
                last_motion = now
            elapsed = data['seconds'] - initial
            runner.status('learning', title=current.title, kind='questionbank', bank=bank,
                before_seconds=current.learned, required_seconds=current.target,
                site_timer_seconds=data['seconds'], session_target_seconds=wanted,
                session_learned_seconds=elapsed, pages_seen=len(pages_seen),
                answers_submitted=0, next_bank_index=(index + 1) % 8,
                reserved_question=question['title'], reference_pdf_page=match.get('pdf_page'))
            if elapsed >= wanted:
                break
            if now - last_motion >= runner.settings['stall_seconds']:
                raise NeedsAttention('题库练习计时持续未增加，保留当前页面等待检查。')
            if now - last_browse >= 45:
                browsed += 1
                self.js('scroll', browsed * 3)
                last_browse = now
            last = data
        answer = self.answer_one(question, match)
        final = self.exit()
        runner.state.mark_pending(current, {'kind': 'questionbank', 'pilot': False,
            'bank': bank, 'reason': 'questionbank_session_saved_wait_for_server',
            'initial_timer_seconds': initial, 'final_timer_seconds': final,
            'elapsed_seconds': int(time.monotonic() - started), 'pages_seen': sorted(pages_seen),
            'answers_submitted': 1, 'verified_answer': answer})
        write_json(self.cursor_path, {'next_bank': index + 1, 'last_bank': bank, 'updated_at': time.time()})
        runner.active = None
        logging.info('题库本次练习 %d 秒，已点击退出保存；下一次切换题库，等待网站学时确认。', final - initial)
