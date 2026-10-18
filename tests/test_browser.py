"""Real Edge against local simulated pages, never the school's login or courses."""
import base64
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import wave

from browser import Browser, create_driver
from core import NeedsAttention
from selenium.common.exceptions import NoAlertPresentException


ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(SimpleHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except ConnectionResetError:
            pass

    def guess_type(self, path):
        return 'text/html; charset=utf-8'

    def log_message(self, *_):
        pass


@unittest.skipUnless(os.environ.get('LABSAFE_BROWSER_TESTS') == '1', '需要显式启用本地 Edge 测试')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        folder = Path(cls.temp.name)
        (folder / 'task').write_text((ROOT / 'tests' / 'fixture.html').read_text(encoding='utf-8'), encoding='utf-8')
        (folder / 'document').write_text('<meta charset="utf-8"><div>已学习<br>00:10<br>要求学习<br>03:00</div>'
           '<span id="page">1/2</span><button onclick="document.getElementById(\'page\').innerText=\'2/2\'">下一页</button>'
           '<button onclick="document.getElementById(\'page\').innerText=\'1/2\'">上一页</button>', encoding='utf-8')
        stream = io.BytesIO()
        with wave.open(stream, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
            wav.writeframes(b'\x00\x00' * 16000)
        audio = base64.b64encode(stream.getvalue()).decode('ascii')
        (folder / 'media').write_text('<video muted controls style="width:320px;height:160px" '
           f'src="data:audio/wav;base64,{audio}"></video>', encoding='utf-8')
        (folder / 'nested').write_text('<iframe src="/middle"></iframe>', encoding='utf-8')
        (folder / 'middle').write_text('<iframe src="/media"></iframe>', encoding='utf-8')
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(folder)))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.origin = 'http://127.0.0.1:' + str(cls.server.server_port)
        cls.settings = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
        cls.settings['task_url'] = cls.origin + '/task'
        cls.driver = create_driver(ROOT, cls.settings, 'fixture-tests', headless=True)
        cls.browser = Browser(cls.driver, cls.settings, folder / 'output')

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def setUp(self):
        self.driver.switch_to.window(self.browser.main_handle)
        for handle in self.driver.window_handles:
            if handle != self.browser.main_handle:
                self.driver.switch_to.window(handle); self.driver.close()
        self.driver.switch_to.window(self.browser.main_handle)
        self.driver.get(self.origin + '/task')

    def test_inventory_all_tabs_and_pages_without_learning(self):
        courses, totals = self.browser.inventory()
        self.assertEqual(len(courses), 4)
        self.assertEqual(sum(c.required for c in courses), 2)
        self.assertEqual(totals.required_target, 60)
        self.assertEqual(totals.required_learned, 5)
        self.assertEqual(self.fixture_script('return clicks'), 0)

    def test_questionbank_selector_modal_opens_without_navigation(self):
        self.fixture_script('''
          items[0].title='题库';items[0].module='题库练习';render();
          document.body.insertAdjacentHTML('beforeend', '<div class="ivu-modal" id="banks" style="display:none"><h2>可练习题库</h2><ul class="tkbox"><li>1.化学安全题库</li><li>2.电气安全题库</li><li>9.错题集</li></ul></div>');
          window.open=()=>document.getElementById('banks').style.display='block';
        ''')
        course = self.browser.page_rows('未学', 1)[0]
        self.assertEqual(course.kind_hint, 'questionbank')
        self.assertIsNotNone(self.browser.open_course(course))
        from questionbank import DOM as BANK_DOM
        banks = self.driver.execute_script(BANK_DOM, 'banks')
        self.assertEqual([b['name'] for b in banks], ['1.化学安全题库', '2.电气安全题库'])

    def test_independent_collector_reads_site_feedback_without_exam_submit(self):
        from collect_bank import answer_from_site
        from questionbank import DOM as BANK_DOM
        markup = '''<div class="exercises"><div class="top-tit">在线练习</div>
        <div class="time">00:00:01</div><div class="m-item" id="m-item-1">
        <div class="type">多选题</div><div class="desc"><p><span class="subject-title">测试题</span></p>
        <label><input type="checkbox">A、选项甲</label><label><input type="checkbox">B、选项乙</label>
        <span class="sub" onclick="document.querySelector('#m-item-1 .result-box').textContent='正确答案：B；我的答案：A,B'">提交答案</span>
        <div class="result-box"></div></div></div></div>
        <button id="exam-submit" onclick="document.body.dataset.examSubmit='1'">交卷</button>'''
        self.driver.execute_script('document.body.innerHTML=arguments[0];history.pushState({},"","/questionBank/exercises/23")', markup)
        q = self.driver.execute_script(BANK_DOM, 'sample')['questions'][0]
        self.assertIsNone(answer_from_site(self.browser, q))
        self.assertIn('正确答案：B', q['result'])
        self.assertNotEqual(self.driver.execute_script('return document.body.dataset.examSubmit'), '1')

    def test_mock_choice_does_not_click_submit(self):
        from mock_fill import apply_answer
        markup = '''<div class="exercises"><div class="m-item" id="m-item-1">
          <div class="type">单选题</div><div class="desc"><p><span class="subject-title">测试题</span></p>
          <label><input type="radio" name="one">A、错误</label>
          <label><input type="radio" name="one">B、正确</label></div></div>
          <button id="exam-submit" onclick="document.body.dataset.examSubmit='1'">交卷</button></div>'''
        self.driver.execute_script('document.body.innerHTML=arguments[0];history.pushState({},"","/lab-study-front/routine-exam/75")', markup)
        question = {'id': 'm-item-1', 'type': '单选题', 'title': '测试题', 'options': ['A、错误', 'B、正确']}
        self.assertIsNone(apply_answer(self.driver, question, [1]))
        self.assertEqual(self.driver.execute_script('return document.querySelectorAll("input:checked").length'), 1)
        self.assertNotEqual(self.driver.execute_script('return document.body.dataset.examSubmit'), '1')

    def test_task_load_closes_only_known_questionbank_picker(self):
        self.driver.execute_script('''document.body.insertAdjacentHTML('beforeend', '<div class="ivu-modal"><div class="ivu-modal-header-inner">可练习题库</div><a class="ivu-modal-close" onclick="this.parentElement.remove()">关闭</a><ul class="tkbox"><li>1.化学安全题库</li></ul></div>')''')
        self.browser.load_task()
        self.assertIsNone(self.browser.js('questionbank-picker-close'))
        self.assertEqual(len(self.browser.js('rows')), 2)

    def test_questionbank_waits_for_async_questions_before_sampling(self):
        from questionbank import QuestionBank
        from types import SimpleNamespace
        self.driver.execute_script('''
          const modal=document.createElement('div');modal.className='ivu-modal';
          modal.innerHTML='<div>可练习题库</div><ul class="tkbox"><li>1.化学安全题库</li></ul>';
          document.body.append(modal);
          modal.querySelector('li').onclick=()=>{
            modal.remove();history.pushState({},'', '/questionBank/exercises/23');
            const root=document.createElement('div');root.className='exercises';
            root.innerHTML='<div class="top-tit">在线练习</div><div class="time">00:00:01 练习时间</div>';
            document.body.append(root);
            setTimeout(()=>root.insertAdjacentHTML('beforeend','<div class="m-item" id="m-item-1"><div class="type">单选题</div><div class="desc"><p><span class="subject-title">测试题</span></p><label>A、选项</label></div></div>'),250);
          };
        ''')
        adapter = QuestionBank(SimpleNamespace(browser=self.browser, output=self.browser.output))
        name, sample = adapter.open_bank(0)
        self.assertEqual(name, '1.化学安全题库')
        self.assertEqual(sample['first'], 'm-item-1')
        self.assertEqual(sample['questions'][0]['title'], '测试题')

    def test_questionbank_timer_questions_and_exit_through_site_control(self):
        from questionbank import DOM as BANK_DOM
        markup = '''<div class="exercises"><div class="top-box"><div class="back" onclick="location.href='/task'">退出</div><div class="top-tit">在线练习</div></div><div class="time"><span>00:02:03</span><div>练习时间</div></div><div id="m-item-1" class="m-item"><div class="type">多选题</div><div class="desc"><p><span class="subject-title">题目一</span></p><label>A、选项一</label><label>B、选项二</label></div></div><button onclick="document.querySelector('.m-item').id='m-item-21'">下一页</button></div>'''
        self.driver.execute_script('document.body.innerHTML=arguments[0];history.pushState({},"","/questionBank/exercises/23")', markup)
        data = self.driver.execute_script(BANK_DOM, 'sample')
        self.assertEqual(data['timer'], '00:02:03')
        self.assertEqual(data['questions'][0]['title'], '题目一')
        self.assertEqual(data['questions'][0]['options'], ['A、选项一', 'B、选项二'])
        data['next'].click()
        self.assertEqual(self.driver.execute_script(BANK_DOM, 'sample')['first'], 'm-item-21')
        self.browser.return_to_task()
        self.assertEqual(self.driver.current_url, self.origin + '/task')

    def questionbank_answer_fixture(self, correct='A,C'):
        from questionbank import QuestionBank
        from types import SimpleNamespace
        self.browser.output.mkdir(parents=True, exist_ok=True)
        markup = '''<div class="exercises"><div class="top-box"><div class="top-tit">在线练习</div></div><div class="time">00:15:00 练习时间</div><div id="m-item-1" class="m-item"><div class="type">多选题</div><div class="desc"><p><span class="subject-title">测试废液题</span></p><label><input type="checkbox">A、处置一</label><label><input type="checkbox">B、处置二</label><label><input type="checkbox">C、处置三</label><span class="sub" onclick="window.answerCount++;document.querySelectorAll('input').forEach(e=>e.disabled=true);document.querySelector('.desc').insertAdjacentHTML('beforeend','<div class=result-box>正确答案：'+window.correctAnswer+'<br>我的答案：A,C</div>')">提交答案</span></div></div></div>'''
        markup = markup.replace('window.answerCount++', "document.body.dataset.answerCount=Number(document.body.dataset.answerCount)+1").replace('window.correctAnswer', 'document.body.dataset.correctAnswer')
        self.driver.execute_script('document.body.innerHTML=arguments[0];document.body.dataset.answerCount="0";document.body.dataset.correctAnswer=arguments[1];history.pushState({},"","/questionBank/exercises/23")', markup, correct)
        adapter = QuestionBank(SimpleNamespace(browser=self.browser, output=self.browser.output))
        q = adapter.sample()['questions'][0]
        match = {'indices': [0, 2], 'source': 'fixture', 'pdf_page': 1}
        return adapter, q, match

    def test_verified_questionbank_submits_once_and_rejects_duplicate(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        adapter, q, match = self.questionbank_answer_fixture()
        references = SimpleNamespace(match=lambda item: None if item.get('answered') else match)
        with patch('reference_answers.ReferenceAnswers', return_value=references):
            result = adapter.answer_one(q, match)
            self.assertEqual(result['answer'], 'A,C')
            with self.assertRaises(NeedsAttention):
                adapter.answer_one(q, match)
        self.assertEqual(self.driver.execute_script('return Number(document.body.dataset.answerCount)'), 1)

    def test_incorrect_website_feedback_stops_questionbank(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        adapter, q, match = self.questionbank_answer_fixture(correct='B')
        with patch('reference_answers.ReferenceAnswers', return_value=SimpleNamespace(match=lambda item: match)):
            with self.assertRaisesRegex(NeedsAttention, '不一致'):
                adapter.answer_one(q, match)
        self.assertEqual(self.driver.execute_script('return Number(document.body.dataset.answerCount)'), 1)

    def test_school_iview_pagination_reads_every_page(self):
        self.driver.execute_script('''
          document.querySelector('.el-pagination').className='ivu-page';
          document.querySelector('#current').className='ivu-page-item-active';
          document.querySelector('.btn-next').classList.add('ivu-page-next');
        ''')
        courses, _ = self.browser.inventory()
        self.assertEqual(len(courses), 4)
        self.assertEqual(self.fixture_script('return clicks'), 0)

    def test_unknown_pagination_cannot_silently_omit_courses(self):
        self.driver.execute_script("document.querySelector('.el-pagination').className='unknown-pagination'")
        with self.assertRaises(NeedsAttention):
            self.browser.inventory()

    def fixture_script(self, script):
        return self.driver.execute_script(script)

    def test_course_is_relocated_after_list_reordering(self):
        courses, _ = self.browser.inventory()
        target = next(c for c in courses if c.title == '个人防护装备')
        self.fixture_script('items.reverse();render()')
        self.assertIsNotNone(self.browser.open_course(target))
        self.assertEqual(len(self.driver.window_handles), 2)
        self.browser.return_to_task()
        self.assertEqual(len(self.driver.window_handles), 1)

    def test_document_counter_and_turning(self):
        self.driver.get(self.origin + '/document')
        sample = self.browser.sample()
        self.assertEqual(sample['timer'], {'learned': 10, 'target': 180})
        self.assertEqual(sample['pages'], 2)
        self.browser.turn_document(sample['viewer_context'], 2)
        self.assertEqual(self.browser.sample()['page'], 2)
        self.browser.rewind_document(sample['viewer_context'])
        self.assertEqual(self.browser.sample()['page'], 1)

    def test_nested_iframe_media_and_context_restoration(self):
        self.driver.get(self.origin + '/nested')
        self.browser.wait.until(lambda _: self.browser.sample()['video'] is not None)
        sample = self.browser.sample()
        self.assertEqual(sample['video_context'], (0, 0))
        self.assertTrue(self.browser.play(sample['video_context']))
        self.browser.wait.until(lambda _: self.browser.sample()['video']['ended'])
        self.assertEqual(self.driver.execute_script('return document.querySelectorAll("iframe").length'), 1)

    def test_visible_confirmation_requires_attention(self):
        self.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<div role=dialog>是否继续学习？</div>')")
        with self.assertRaises(NeedsAttention):
            self.browser.blockers()

    def test_school_iview_confirmation_requires_attention(self):
        self.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<div class=ivu-modal>视频已经学习完，是否重新学习!</div>')")
        with self.assertRaises(NeedsAttention):
            self.browser.blockers()

    def test_authorized_finished_video_replay_is_confirmed(self):
        self.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<div class=ivu-modal>视频已经学习完，是否重新学习!<button onclick=\"this.parentElement.remove()\">确定</button></div>')")
        self.settings['restart_finished_video'] = True
        self.browser.blockers()
        self.assertEqual(self.driver.execute_script('return document.querySelectorAll(".ivu-modal").length'), 0)

    def test_other_confirmation_still_requires_manual_action(self):
        self.driver.execute_script("document.body.insertAdjacentHTML('beforeend','<div class=ivu-modal>需要确认安全操作<button>确定</button></div>')")
        self.settings['restart_finished_video'] = True
        with self.assertRaises(NeedsAttention):
            self.browser.blockers()

    def test_native_alert_is_not_automatically_accepted(self):
        self.driver.execute_script("setTimeout(()=>alert('需要确认'),100)")
        def has_alert(_):
            try:
                return self.driver.switch_to.alert
            except NoAlertPresentException:
                return False
        self.browser.wait.until(has_alert)
        with self.assertRaises(NeedsAttention):
            self.browser.blockers()
        self.browser.pause_media()
        self.assertEqual(self.driver.switch_to.alert.text, '需要确认')
        self.driver.switch_to.alert.dismiss()


@unittest.skipUnless(os.environ.get('LABSAFE_BROWSER_TESTS') == '1', '需要显式启用本地 Edge 测试')
class NativeBrowserTests(BrowserTests):
    """Exercise the same contracts through the native debugging transport."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from native_driver import NativeDriver
        cls.launch_driver = cls.driver
        try:
            cls.launch_driver.get(cls.settings['task_url'])
            capabilities = cls.launch_driver.capabilities
            address = capabilities.get('ms:edgeOptions', capabilities.get('goog:chromeOptions', {}))['debuggerAddress']
            cls.driver = NativeDriver(address, cls.settings['task_url'])
        except BaseException:
            cls.launch_driver.quit()
            cls.server.shutdown()
            cls.server.server_close()
            cls.temp.cleanup()
            raise
        cls.browser = Browser(cls.driver, cls.settings, Path(cls.temp.name) / 'native-output')

    @classmethod
    def tearDownClass(cls):
        cls.driver.quit()
        cls.driver = cls.launch_driver
        super().tearDownClass()

    def fixture_script(self, script):
        # Fixture globals belong to the page world; the production adapter only reads DOM.
        result = self.driver.rpc('Runtime.evaluate', {
            'expression': '(function(){' + script + '})()', 'returnByValue': True})
        if result.get('exceptionDetails'):
            raise AssertionError(result['exceptionDetails'])
        return result['result'].get('value')


if __name__ == '__main__':
    unittest.main()
