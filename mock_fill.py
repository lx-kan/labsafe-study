"""Fill a site-confirmed mock or formal exam from the local practice archive.

Separate entry points check the mode; the operator submits the paper manually.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit

from bank_browser import attach
from browser import close_driver
from core import NeedsAttention, write_json
from instance import InstanceLock
from local_bank import Archive, kind


ROOT = Path(__file__).resolve().parent


SAMPLE = r'''
const root=document.querySelector('.exercises');
if(!root || !root.getClientRects().length)return null;
const items=[...root.querySelectorAll('.m-item')].filter(e=>e.getClientRects().length);
return items.map(e=>({id:e.id,type:e.querySelector('.type')?.innerText||'',
 title:e.querySelector('.desc > p .subject-title')?.innerText||'',
 options:[...e.querySelectorAll('label')].map(l=>l.innerText.trim()),
 images:[...e.querySelectorAll('.desc img')].map(_=>({present:true}))}));
'''


def mock_meta(driver):
    # Read only the site's own Vue state and its official current-exam endpoint.
    expression = '''(async()=>{const v=document.querySelector('.exercises')?.__vue__;
      if(v?.$options?.name!=='studentExaming' || v.$route?.name!=='RoutineExam')return null;
      const status=await new Promise(resolve=>v.commonMethods.emitAjax({
        path:'/api/exam_center/current_exam',
        success:r=>resolve(r?{mock:r.isMockExam,examId:r.exam?.id,requestId:r.examRequestId}:null),
        error:()=>resolve(null)}));
      return JSON.stringify({status,routeId:v.$route.params.id,
        requestId:v.$route.query.examRequestId,examTitle:v.$store.state.examDetail?.examName,
        total:v.subjectDataList?.length||0,showType:v.examConfig?.subjectShowType,
        pageEnd:v.pageEnd,submitted:v.submitted,studyId:v.studyId});})()'''
    result = driver.rpc('Runtime.evaluate', {'expression': expression,
                                             'awaitPromise': True, 'returnByValue': True})
    encoded = result.get('result', {}).get('value')
    return json.loads(encoded) if encoded else None


def assert_exam(driver, settings, expected_mock=True):
    mode = '模拟考试' if expected_mock else '正式考试'
    if not re.fullmatch(r'/lab-study-front/routine-exam/\d+(?:/\d+)?', urlsplit(driver.current_url).path):
        raise NeedsAttention(f'当前不是学校网站的{mode}答题页。')
    meta = mock_meta(driver)
    status = meta and meta['status']
    target_exam_id = urlsplit(settings.get('task_url', '')).path.rstrip('/').rsplit('/', 1)[-1]
    actual_mock = status.get('mock') if status else None
    if (not meta or not status or actual_mock not in (0, 1, False, True) or
            bool(actual_mock) != expected_mock or
            str(status['examId']) != str(meta['routeId']) or
            str(meta['routeId']) != target_exam_id or
            str(status['requestId']) != str(meta['requestId']) or
            meta['studyId'] or meta['submitted'] or meta['total'] <= 0):
        raise NeedsAttention(f'网站未明确确认这是当前{mode}；未填任何答案。')
    if meta['showType'] == 3:
        raise NeedsAttention(f'本次{mode}采用单题限时并自动切页，当前填答器未适配；未填任何答案。')
    if meta['showType'] not in (1, 2):
        raise NeedsAttention('网站答题模式未识别；未填任何答案。')
    if not driver.execute_script(SAMPLE):
        # The site's own refresh path can leave subjectIndex=0 although all
        # 100 subjects are loaded. Move only the local view to its first page.
        driver.rpc('Runtime.evaluate', {'expression': '''(()=>{const v=document.querySelector('.exercises')?.__vue__;
          if(v?.subjectIndex===0&&v?.subjectDataList?.length){v.subjectIndex=1;v.isPageEnd();return true}return false;})()''',
            'returnByValue': True})
        time.sleep(.2)
    if not driver.execute_script(SAMPLE):
        raise NeedsAttention(f'{mode}题目尚未加载。')
    return meta


def assert_mock(driver, settings):
    return assert_exam(driver, settings, expected_mock=True)


def selected_options(driver, question_id):
    return driver.execute_script('''const e=document.getElementById(arguments[0]);
    if(!e)return null;
    return [...e.querySelectorAll('label')].map(l=>({
      selected:!!l.querySelector('input')?.checked || /ivu-(?:radio|checkbox)-wrapper-checked/.test(l.className),
      disabled:!!l.querySelector('input')?.disabled}));''', question_id)


def apply_answer(driver, question, wanted):
    labels = driver.find_elements('css selector', '#' + question['id'] + ' label')
    selected = selected_options(driver, question['id'])
    if not selected or len(labels) != len(question['options']) or len(selected) != len(labels):
        return '选项控件与题目不一致'
    if any(x['disabled'] for x in selected):
        return '题目选项不可修改'
    if kind(question['type']) == '多选题':
        to_click = [i for i,x in enumerate(selected) if x['selected'] and i not in wanted]
        to_click += [i for i,x in enumerate(selected) if not x['selected'] and i in wanted]
    else:
        to_click = [] if [i for i,x in enumerate(selected) if x['selected']] == wanted else wanted
    for index in to_click:
        labels[index].click()
        if not re.fullmatch(r'/lab-study-front/routine-exam/\d+(?:/\d+)?', urlsplit(driver.current_url).path):
            return '答题页面已变化'
    checked = selected_options(driver, question['id'])
    if checked is None or [i for i,x in enumerate(checked) if x['selected']] != wanted:
        return '选中状态核对失败'
    return None


def wait_mock_page(driver, settings, timeout, expected_mock=True):
    deadline = time.monotonic() + timeout if timeout else float('inf')
    host = urlsplit(settings['task_url']).hostname
    last_notice = 0
    while time.monotonic() < deadline:
        for page in driver.pages():
            if (urlsplit(page.get('url', '')).hostname == host and
                    re.fullmatch(r'/lab-study-front/routine-exam/\d+(?:/\d+)?',
                                 urlsplit(page.get('url', '')).path)):
                driver.connect(page['id'])
                return
        if time.monotonic() - last_notice > 15:
            mode = '模拟考试' if expected_mock else '正式考试'
            print(f'请在专用 Edge 进入该课程的“{mode}”答题页；程序只等待页面。', flush=True)
            last_notice = time.monotonic()
        time.sleep(2)
    raise NeedsAttention('等待考试页面超时。')


def verify_saved(driver, settings, archive, expected_total, expected_mock=True):
    """Reload the paper and compare website-saved selections on every page."""
    driver.refresh()
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        try:
            meta = assert_exam(driver, settings, expected_mock)
            break
        except NeedsAttention:
            time.sleep(.5)
    else:
        raise NeedsAttention('刷新后题目未恢复，无法核对网站保存的答案。')
    if meta['total'] != expected_total:
        raise NeedsAttention('刷新后试卷题数变化，停止核对。')
    seen = set()
    mismatches = []
    for _ in range(1000):
        questions = driver.execute_script(SAMPLE)
        first_id = questions[0]['id']
        for question in questions:
            if question['id'] in seen:
                raise NeedsAttention('刷新后试卷分页重复。')
            seen.add(question['id'])
            desired, reason = archive.match(question)
            chosen = selected_options(driver, question['id'])
            actual = [i for i,x in enumerate(chosen or []) if x['selected']]
            if reason or actual != desired:
                mismatches.append({'id': question['id'], 'reason': reason or '网站保存选项与题库答案不一致'})
        if len(seen) >= expected_total:
            break
        buttons = driver.execute_script('''return [...document.querySelectorAll('.exercises .next-page-wrap')]
          .filter(e=>e.getClientRects().length&&e.innerText.trim()==='下一页')''')
        if len(buttons) != 1:
            raise NeedsAttention('刷新后无法翻到试卷下一页。')
        buttons[0].click()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            updated = driver.execute_script(SAMPLE)
            if updated and updated[0]['id'] != first_id:
                break
            time.sleep(.25)
        else:
            raise NeedsAttention('刷新后试卷下一页未加载。')
    # Leave the first page visible for the operator's final review.
    driver.rpc('Runtime.evaluate', {'expression': '''(()=>{const v=document.querySelector('.exercises')?.__vue__;
      if(v?.subjectDataList?.length){v.subjectIndex=1;v.isPageEnd();return true}return false;})()''',
        'returnByValue': True})
    return {'seen': len(seen), 'mismatches': mismatches, 'verified_at': time.time()}


def fill(settings, account, expected_mock=True):
    mode = '模拟考试' if expected_mock else '正式考试'
    tag = hashlib.sha256(account.encode()).hexdigest()[:12]
    output = ROOT / 'runtime' / tag
    folder = output / 'questionbank-local'
    archive = Archive(folder)
    if not archive.data['questions']:
        raise NeedsAttention('本地题库为空，请先运行采集题库。')
    lock = InstanceLock(output / 'instance.lock')
    lock.acquire()
    driver = None
    try:
        driver = attach(ROOT, settings, account, settings.get('login_timeout_seconds', 0))
        wait_mock_page(driver, settings, settings.get('login_timeout_seconds', 0), expected_mock)
        meta = assert_exam(driver, settings, expected_mock)
        print(f'网站确认当前为{mode}，共 {meta["total"]} 题。开始填答，交卷由你手动完成。', flush=True)
        report = {'started_at': time.time(), 'exam_id': str(meta['routeId']),
                  'request_id': str(meta['requestId']), 'total': meta['total'],
                  'filled': [], 'unfilled': []}
        report_path = folder / ('mock-fill-report.json' if expected_mock else 'formal-fill-report.json')
        seen_ids = set()
        for _ in range(1000):
            assert_exam(driver, settings, expected_mock)
            questions = driver.execute_script(SAMPLE)
            if not questions:
                raise NeedsAttention(f'{mode}当前页没有题目，已保留填答报告。')
            first_id = questions[0]['id']
            for question in questions:
                if question['id'] in seen_ids:
                    raise NeedsAttention(f'{mode}分页重复，已停止。')
                seen_ids.add(question['id'])
                indices, reason = archive.match(question)
                if reason:
                    report['unfilled'].append({'id': question['id'], 'title': question['title'], 'reason': reason})
                else:
                    issue = apply_answer(driver, question, indices)
                    if issue:
                        report['unfilled'].append({'id': question['id'], 'title': question['title'], 'reason': issue})
                    else:
                        report['filled'].append({'id': question['id'], 'title': question['title']})
                write_json(report_path, report)
            current = assert_exam(driver, settings, expected_mock)
            if current['pageEnd'] or len(seen_ids) >= meta['total']:
                break
            next_buttons = driver.execute_script('''return [...document.querySelectorAll('.exercises .next-page-wrap')]
              .filter(e=>e.getClientRects().length&&e.innerText.trim()==='下一页')''')
            if len(next_buttons) != 1:
                raise NeedsAttention('下一页控件未唯一识别，已保留填答报告。')
            next_buttons[0].click()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                updated = driver.execute_script(SAMPLE)
                if updated and updated[0]['id'] != first_id:
                    break
                time.sleep(.25)
            else:
                raise NeedsAttention('下一页未切换，已保留填答报告。')
        report['finished_at'] = time.time()
        report['seen'] = len(seen_ids)
        report['complete_scan'] = len(seen_ids) == meta['total']
        write_json(report_path, report)
        try:
            report['persistence'] = verify_saved(driver, settings, archive, meta['total'], expected_mock)
        except NeedsAttention as exc:
            report['persistence_error'] = str(exc)
        write_json(report_path, report)
        print(f'{mode}共见到 {len(seen_ids)}/{meta["total"]} 题；已填 {len(report["filled"])}，'
              f'待你处理 {len(report["unfilled"])}。请在浏览器核对后手动交卷。', flush=True)
        if report.get('persistence'):
            print(f'刷新后网站保存核对：{report["persistence"]["seen"]} 题，'
                  f'{len(report["persistence"]["mismatches"])} 题不一致。', flush=True)
        elif report.get('persistence_error'):
            print('刷新后保存核对未完成：' + report['persistence_error'], flush=True)
        print('待处理清单：' + str(report_path), flush=True)
        return 0 if (report['complete_scan'] and not report['unfilled'] and
                     report.get('persistence', {}).get('seen') == meta['total'] and
                     not report['persistence']['mismatches']) else 2
    finally:
        if driver:
            try:
                close_driver(driver)
            except Exception:
                pass
        lock.release()


if __name__ == '__main__':
    settings = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
    parser = argparse.ArgumentParser(description='只为网站明确确认的模拟考试填答；由用户手动交卷')
    parser.add_argument('--account', default=settings.get('default_account', 'default'))
    args = parser.parse_args()
    try:
        sys.exit(fill(settings, args.account))
    except KeyboardInterrupt:
        print('填答已停止；请在浏览器中检查已填选项。', flush=True)
        sys.exit(130)
    except Exception as exc:
        print('填答停止：' + str(exc)[:300], flush=True)
        sys.exit(1)
