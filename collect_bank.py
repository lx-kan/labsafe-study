"""Collect all completed required course-bank exercises through the site's UI."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import time

from selenium.common.exceptions import TimeoutException

from bank_browser import attach
from browser import Browser, close_driver
from core import NeedsAttention, write_json
from instance import InstanceLock
from local_bank import Archive, coverage, fingerprint, kind, parse_answer
from questionbank import DOM


ROOT = Path(__file__).resolve().parent


def page_meta(driver):
    expression = '''(()=>{const v=document.querySelector('.exercises')?.__vue__;
        if(v?.$options?.name!=='questionBankExercises')return null;
        return JSON.stringify({page:v.practicePage,size:v.size,total:v.pageConfig?.subjectCount,
                               pageEnd:v.pageEnd});})()'''
    result = driver.rpc('Runtime.evaluate', {'expression': expression, 'returnByValue': True})
    encoded = result.get('result', {}).get('value')
    data = json.loads(encoded) if encoded else None
    if not data or not isinstance(data['page'], int) or not isinstance(data['total'], int) or data['total'] < 1:
        raise NeedsAttention('题库页码或网站总题数未加载，不能确认采集范围。')
    return data


def locate_required_bank(browser):
    browser.load_task()
    browser.choose_tab('已学')
    found = []
    for _ in range(100):
        pager = browser.js('pager')
        for row in browser.js('rows'):
            if row['required'] and row['title'].strip() == '题库' and '题库' in row['module']:
                found.append((pager['active'], row))
        if found:
            break
        if not browser.next_page(pager['active']):
            break
    if len(found) != 1:
        raise NeedsAttention(f'已学列表中找到 {len(found)} 个必学“题库”任务，需要人工核对。')
    page, row = found[0]
    if not row.get('action'):
        raise NeedsAttention('已学题库的“去学习”控件未出现。')
    return page, row


def open_picker(browser):
    page, row = locate_required_bank(browser)
    row['action'].click()
    browser.wait.until(lambda _: bool(browser.driver.execute_script(DOM, 'banks')))
    banks = browser.driver.execute_script(DOM, 'banks')
    names = [bank['name'] for bank in banks]
    if not names or len(names) != len(set(names)):
        raise NeedsAttention('题库分类为空或重复。')
    return page, banks


def image_metadata(driver, question_id):
    return driver.execute_script('''const item=document.getElementById(arguments[0]);
    return item?[...item.querySelectorAll('.desc img')].map((img,index)=>({index,
      width:img.naturalWidth,height:img.naturalHeight,loaded:img.complete&&img.naturalWidth>0})):[];''', question_id)


def capture_images(driver, folder, question):
    images = image_metadata(driver, question['id'])
    if not images:
        return
    question['images'] = []
    question['image_hashes'] = []
    for item in images:
        if not item['loaded']:
            question['image_error'] = True
            continue
        # Capture the rendered image; no signed image URL or account token is persisted.
        rect = driver.execute_script('''const e=document.getElementById(arguments[0])?.querySelectorAll('.desc img')[arguments[1]];
          if(!e)return null;const r=e.getBoundingClientRect();return {x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height};''',
          question['id'], item['index'])
        if not rect or rect['width'] <= 0 or rect['height'] <= 0:
            question['image_error'] = True
            continue
        try:
            shot = driver.rpc('Page.captureScreenshot', {'format': 'png', 'captureBeyondViewport': True,
                'clip': {'x': max(0, rect['x']), 'y': max(0, rect['y']),
                         'width': rect['width'], 'height': rect['height'], 'scale': 1}})
            content = base64.b64decode(shot['data'])
            digest = hashlib.sha256(content).hexdigest()
            image_path = folder / 'images' / (digest + '.png')
            image_path.parent.mkdir(parents=True, exist_ok=True)
            if not image_path.exists():
                image_path.write_bytes(content)
            question['images'].append({'local': 'images/' + image_path.name,
                                       'width': item['width'], 'height': item['height']})
            question['image_hashes'].append(digest)
        except Exception:
            question['image_error'] = True


def answer_from_site(browser, question):
    driver = browser.driver
    qid = question['id']
    labels = driver.find_elements('css selector', '#' + qid + ' label')
    if not labels or len(labels) != len(question['options']):
        return '选项控件与可见选项不一致'
    selected = list(range(len(labels))) if kind(question['type']) == '多选题' else [0]
    if not selected:
        return '没有可选选项'
    for i in selected:
        labels[i].click()
    if kind(question['type']) == '多选题':
        submit = driver.find_elements('css selector', '#' + qid + ' .sub')
        if not submit:
            return '多选提交按钮未出现'
        submit[0].click()
    def feedback(_):
        data = driver.execute_script(DOM, 'sample')
        if not data:
            return False
        return next((q for q in data['questions'] if q['id'] == qid and q['result']), False)
    try:
        updated = browser.wait.until(feedback)
        question['result'] = updated['result']
        question['answered'] = updated['answered']
        return None
    except TimeoutException:
        # Re-read the same item after timeout. A click can have reached the server.
        updated = feedback(None)
        if updated:
            question['result'] = updated['result']
            question['answered'] = updated['answered']
            return None
        return '提交后未显示正确答案，未重复提交'


def collect(settings, account, max_questions=None):
    tag = hashlib.sha256(account.encode()).hexdigest()[:12]
    output = ROOT / 'runtime' / tag
    folder = output / 'questionbank-local'
    folder.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=folder / 'collect.log', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s', encoding='utf-8')
    lock = InstanceLock(output / 'instance.lock')
    lock.acquire()
    driver = None
    archive = Archive(folder)
    state_path = folder / 'progress.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'banks': {}}
    done_this_run = 0
    try:
        driver = attach(ROOT, settings, account, settings.get('login_timeout_seconds', 0))
        browser = Browser(driver, settings, output)
        page, banks = open_picker(browser)
        print(f'已学第 {page} 页：找到必学题库，共 {len(banks)} 个分类。', flush=True)
        names = [x['name'] for x in banks]
        # A newly added category invalidates a previous complete summary.
        state['banks'] = {name: state['banks'].get(name, {}) for name in names}
        for bank_index, name in enumerate(names):
            if bank_index:
                _, banks = open_picker(browser)
                if [x['name'] for x in banks] != names:
                    raise NeedsAttention('题库分类顺序发生变化，请检查。')
            banks[bank_index]['element'].click()
            browser.wait.until(lambda _: bool(driver.execute_script(DOM, 'sample')))
            seen_page_signatures = set()
            bank = {'finished': False, 'pages': 0, 'seen': 0, 'total': None, 'issues': []}
            state['banks'][name] = bank
            while True:
                browser.blockers()
                meta = page_meta(driver)
                data = driver.execute_script(DOM, 'sample')
                if not data or not data['questions']:
                    raise NeedsAttention(f'{name} 第 {meta["page"]+1} 页没有题目。')
                page_number = meta['page'] + 1
                signature = (page_number, tuple(q['id'] for q in data['questions']))
                if signature in seen_page_signatures:
                    raise NeedsAttention(f'{name} 分页重复，停止防止漏题。')
                seen_page_signatures.add(signature)
                bank['total'] = meta['total']
                bank['pages'] = page_number
                for index, question in enumerate(data['questions'], 1):
                    capture_images(driver, folder, question)
                    source = {'bank': name, 'page': page_number, 'position': index,
                              'site_question_id': question['id']}
                    reason = None
                    old = archive.data['questions'].get(fingerprint(question))
                    if question['result']:
                        pass
                    elif old and old['status'] == 'verified':
                        pass
                    elif not kind(question['type']):
                        reason = '未知题型'
                    elif question.get('image_error'):
                        reason = '图片加载或保存失败'
                    elif question['answered']:
                        reason = '网站已答但未显示正确答案'
                    else:
                        reason = answer_from_site(browser, question)
                    if question['result'] and parse_answer(question['result'], question) is None:
                        reason = '网站答案格式未识别'
                    archive.record(question, source, reason)
                    if reason:
                        bank['issues'].append({'page': page_number, 'position': index,
                                               'id': question['id'], 'reason': reason})
                    bank['seen'] += 1
                    done_this_run += 1
                    write_json(state_path, state)
                    if max_questions is not None and done_this_run >= max_questions:
                        archive.export_html()
                        print(f'试采集已处理 {done_this_run} 题；输出：{folder}', flush=True)
                        return 0
                archive.export_html()
                print(f'{name}: 第 {page_number} 页，{bank["seen"]}/{bank["total"]} 题', flush=True)
                if meta['pageEnd']:
                    bank['finished'] = bank['seen'] == bank['total'] and not bank['issues']
                    write_json(state_path, state)
                    break
                if not data.get('next'):
                    raise NeedsAttention(f'{name} 未到末页但找不到下一页按钮。')
                data['next'].click()
                browser.wait.until(lambda _: page_meta(driver)['page'] == page_number)
            # Use the site's own exit control so the practice session is saved.
            final = driver.execute_script(DOM, 'sample')
            if not final or not final.get('exit'):
                raise NeedsAttention('题库退出按钮未识别。')
            final['exit'].click()
            browser.wait.until(lambda _: browser.is_task())
            print(f'{name} 完成，网站题数 {bank["total"]}，采集 {bank["seen"]}。', flush=True)
        summary = coverage(state['banks'], archive.data['questions'])
        write_json(folder / 'summary.json', summary)
        archive.export_html()
        print(f'已核对 {len(names)} 个分类；共 {summary["unique_questions"]} 道不重复题，'
              f'有正确答案 {summary["verified"]} 道，待处理 {len(summary["unresolved"])} 道。', flush=True)
        print('输出：' + str(folder), flush=True)
        return 0 if summary['complete'] else 2
    finally:
        if driver:
            try:
                close_driver(driver)
            except Exception:
                pass
        lock.release()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='采集已学必学题库的全部分类、题目与网站正确答案')
    settings = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
    parser.add_argument('--account', default=settings.get('default_account', 'default'))
    parser.add_argument('--max-questions', type=int, help='试运行：仅处理指定数量的题目')
    args = parser.parse_args()
    if args.max_questions is not None and args.max_questions <= 0:
        parser.error('--max-questions 必须大于零')
    try:
        sys.exit(collect(settings, args.account, args.max_questions))
    except KeyboardInterrupt:
        print('采集已中断；逐题保存的结果保留在本地，可重新运行继续。', flush=True)
        sys.exit(130)
    except Exception as exc:
        print('采集停止：' + str(exc)[:300], flush=True)
        logging.exception('采集停止')
        sys.exit(1)
