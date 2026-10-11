"""Semantic DOM adapter. All credit is produced by normal site UI activity."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import json
import logging
import os
import re
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit

from selenium import webdriver
from selenium.common.exceptions import (NoAlertPresentException, StaleElementReferenceException,
                                        TimeoutException, WebDriverException)
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options
from selenium.webdriver.edge.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.selenium_manager import SeleniumManager

from core import Course, NeedsAttention, Totals, merge_courses, normalized, safe_url, seconds, write_json


DOM = r"""
const mode=arguments[0];
const visible=e=>!!(e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden');
const text=e=>(e?.innerText || '').trim();
const all=selector=>Array.from(document.querySelectorAll(selector)).filter(visible);
const tight=predicate=>all('body *').filter(e=>predicate(text(e))).sort((a,b)=>text(a).length-text(b).length);
const times=t=>t.match(/\d+:\d{2}(?::\d{2})?/g)||[];
const disabled=e=>!e || e.disabled || e.getAttribute('aria-disabled')==='true' ||
  /(?:^|\s)(?:disabled|is-disabled|ant-pagination-disabled|ivu-page-disabled)(?:\s|$)/.test(e.className||'');
if(mode==='body') return document.body?.innerText || '';
if(mode==='header') {
  function pair(label) {
    const matches=tight(t=>t.includes(label) && (t.match(/\d+:\d{2}:\d{2}/g)||[]).length===2);
    if(!matches.length) return null;
    return text(matches[0]).match(/\d+:\d{2}:\d{2}/g);
  }
  return {required:pair('必学时长'),optional:pair('选学时长')};
}
if(mode==='rows') {
  const rows=all('tr').filter(r=>r.querySelectorAll('td').length>=3 && /\d+:\d{2}:\d{2}/.test(text(r)));
  return rows.map((r,index)=>{
    const cells=Array.from(r.querySelectorAll('td'));
    const name=cells[0];
    const named=name.querySelector('p.ppp,[title],a');
    let title=named?.getAttribute('title') || text(named) || text(name);
    title=title.replace(/^\s*(必学|选学)\s*/,'');
    const action=Array.from(r.querySelectorAll('a,button,[role=button]')).find(e=>/去学习/.test(text(e)));
    const link=action?.getAttribute('href') || name.querySelector('a[href]')?.getAttribute('href');
    const url=link && !/^(javascript:|#)/.test(link)?new URL(link,location.href).href:'';
    return {title,module:text(cells[1]),progress:text(cells[2]),required:/必学/.test(text(name)),
      row:index,url,id:r.getAttribute('data-id') || r.getAttribute('data-key') || '',action:action||null};
  });
}
if(mode==='tab') {
  const label=arguments[1];
  const list=tight(t=>t===label);
  return list.find(e=>e.matches('[role=tab],.el-tabs__item,.ant-tabs-tab,.ivu-tabs-tab,button,a')) || list[0] || null;
}
if(mode==='return') {
  const labels=['返回课程主页','返回'];
  for(const label of labels){
    const matches=tight(t=>t===label);
    const chosen=matches.find(e=>e.matches('a,button,[role=button]')) || matches[0];
    if(chosen)return chosen;
  }
  return null;
}
if(mode==='questionbank-picker-close') {
  const modal=all('.ivu-modal').find(e=>text(e.querySelector('.ivu-modal-header-inner'))==='可练习题库'
    && e.querySelector('.tkbox li'));
  return modal?.querySelector('.ivu-modal-close')||null;
}
if(mode==='pager') {
  const root=all('.el-pagination,.ant-pagination,.ivu-page,[aria-label*=分页],[aria-label*=pagination]')[0];
  if(!root) {
    const count=text(document.body).match(/共\s*(\d+)\s*条/);
    return {active:1,next:null,first:null,total:count?Number(count[1]):null};
  }
  const active=root.querySelector('.active,.is-active,.ant-pagination-item-active,.ivu-page-item-active,[aria-current=page]');
  const next=root.querySelector('.btn-next,.ant-pagination-next,.ivu-page-next,[aria-label*=下一],[aria-label*=Next]');
  const first=Array.from(root.querySelectorAll('li,button,a')).find(e=>text(e)==='1');
  const count=text(root).match(/共\s*(\d+)\s*条/);
  return {active:Number(text(active))||1,next:disabled(next)?null:next,
          first:first||null,total:count?Number(count[1]):null};
}
if(mode==='dialogs') {
  return all('[role=dialog],.el-message-box,.el-dialog,.ant-modal-content,.layui-layer-dialog,.ivu-modal')
    .map(text).filter(Boolean);
}
if(mode==='replay') {
  const dialog=all('.ivu-modal,.el-message-box,.el-dialog,[role=dialog]')
    .find(e=>/视频已经学习完[，,]?\s*是否重新学习/.test(text(e)));
  return dialog?Array.from(dialog.querySelectorAll('button'))
    .find(e=>visible(e)&&text(e)==='确定')||null:null;
}
if(mode==='sample') {
  let counters=tight(t=>/已学习\s*[:：]?\s*\d+:\d{2}/.test(t) && /要求学习\s*[:：]?\s*\d+:\d{2}/.test(t));
  counters=counters.filter(e=>!counters.some(other=>other!==e && e.contains(other)));
  const parsed=counters.map(e=>{
    const t=text(e); const before=t.match(/已学习\s*[:：]?\s*(\d+:\d{2}(?::\d{2})?)/);
    const target=t.match(/要求学习\s*[:：]?\s*(\d+:\d{2}(?::\d{2})?)/);
    return {learned:before?.[1],target:target?.[1],active:!!e.closest('.active,.on,.current,.cur'),element:e};
  });
  const v=all('video')[0];
  const page=tight(t=>/^(\d+)\s*\/\s*(\d+)$/.test(t))[0];
  const pageMatch=page?text(page).match(/^(\d+)\s*\/\s*(\d+)$/):null;
  const next=tight(t=>t==='下一页')[0];
  const previous=tight(t=>t==='上一页')[0];
  const selected=parsed.find(c=>c.active) || (parsed.length===1?parsed[0]:null);
  return {counters:parsed,timer:selected||null,page:pageMatch?Number(pageMatch[1]):null,
    pages:pageMatch?Number(pageMatch[2]):null,next:disabled(next)?null:next,previous:previous||null,
    video:v?{time:Number.isFinite(v.currentTime)?v.currentTime:0,
      duration:Number.isFinite(v.duration)?v.duration:0,paused:v.paused,ended:v.ended,
      ready:v.readyState,rate:v.playbackRate,error:v.error?.code||null}:null};
}
if(mode==='play') {
  const v=all('video')[0];
  if(!v) return false;
  v.playbackRate=1;
  v.play().catch(()=>{});
  return true;
}
if(mode==='pause') {all('video').forEach(v=>v.pause()); return true;}
throw new Error('Unknown DOM operation: '+mode);
"""


def ready_course_tab(pages, task_url, signal=False):
    matching = [p for p in pages if p.get('type') == 'page'
                and safe_url(p.get('url', '')).rstrip('/') == task_url.rstrip('/')]
    if signal:
        return bool(matching)
    return any(p.get('title') and '安全' in p['title'] and 'labsafe.lzjtu.edu.cn' not in p['title']
               for p in matching)


def create_driver(root: Path, settings: dict, account: str, headless=False):
    import hashlib
    profile_key = hashlib.sha256(account.encode()).hexdigest()[:12]
    os.environ['SE_CACHE_PATH'] = str(root / '.cache' / 'selenium')
    os.environ['SE_AVOID_STATS'] = 'true'
    options = Options()
    options.set_capability('unhandledPromptBehavior', 'ignore')
    path = root / settings.get('driver_path', 'drivers/msedgedriver.exe')
    if headless:
        options.add_argument('--headless=new')
        options.add_argument('--no-first-run')
        options.add_argument('--disable-background-networking')
        options.add_argument('--window-size=1400,950')
        driver = webdriver.Edge(options=options, service=Service(str(path)) if path.is_file() else Service())
    else:
        # Start the installed Edge normally using our own profile, then connect.
        if path.is_file() or settings.get('browser_backend') == 'native':
            edge_candidates = [Path(os.environ.get('PROGRAMFILES(X86)', r'C:\Program Files (x86)')) /
                               'Microsoft/Edge/Application/msedge.exe',
                               Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) /
                               'Microsoft/Edge/Application/msedge.exe']
            binary = next((p for p in edge_candidates if p.is_file()), None)
            if binary is None:
                raise NeedsAttention('未找到已安装的 Microsoft Edge。')
        else:
            resolved = SeleniumManager().binary_paths(['--browser', 'edge'])
            path, binary = Path(resolved['driver_path']), Path(resolved['browser_path'])
        output = root / 'runtime' / profile_key
        connection_file = output / 'browser-connection.json'
        port = None
        if connection_file.exists():
            try:
                saved = json.loads(connection_file.read_text(encoding='utf-8'))
                candidate = int(saved['port'])
                if not 1 <= candidate <= 65535:
                    raise ValueError('invalid port')
                with urllib.request.urlopen(f'http://127.0.0.1:{candidate}/json/version', timeout=1) as response:
                    live = json.load(response)
                if live.get('webSocketDebuggerUrl') == saved.get('identity'):
                    port = candidate
            except (OSError, ValueError, KeyError):
                pass
        if port is None:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            command = [str(binary), f'--remote-debugging-port={port}',
                       f'--user-data-dir={root / "browser-profile" / profile_key}',
                       '--no-first-run', '--window-size=1400,950',
                       '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
                       '--disable-backgrounding-occluded-windows',
                       settings.get('portal_url', settings['home_url'])]
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 25
        try:
            while True:
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1) as response:
                        version = json.load(response)
                        write_json(connection_file, {'port': port, 'identity': version['webSocketDebuggerUrl']})
                        break
                except (OSError, ValueError):
                    if time.monotonic() >= deadline:
                        raise NeedsAttention('专用 Edge 启动失败或此账号配置仍被另一个 Edge 占用。')
                    time.sleep(0.3)
            ready = output / 'connect.flag'
            ready.unlink(missing_ok=True)
            print('专用 Edge 已打开或复用，等待课程总览。', flush=True)
            print('请在学校入口登录并打开课程总览；标题正确后自动连接。', flush=True)
            print('若课程已打开但程序未连接，可双击“登录完成.cmd”。按 Ctrl+C 可退出。', flush=True)
            login_deadline = (time.monotonic() + settings['login_timeout_seconds']
                              if settings['login_timeout_seconds'] > 0 else float('inf'))
            while True:
                if (output / 'stop.flag').exists():
                    raise KeyboardInterrupt
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=2) as handle:
                    pages = json.load(handle)
                task_tab_open = ready_course_tab(pages, settings['task_url'], signal=True)
                if ready_course_tab(pages, settings['task_url'], signal=ready.exists()):
                    ready.unlink(missing_ok=True)
                    break
                if time.monotonic() >= login_deadline:
                    raise NeedsAttention('等待独立浏览器登录超时。默认 login_timeout_seconds=0 可持续等待。')
                write_json(output / 'status.json', {'phase': 'manual_browser_login',
                    'updated_at': time.time(), 'webdriver_connected': False,
                    'task_tab_open': task_tab_open, 'waiting_indefinitely': settings['login_timeout_seconds'] == 0})
                time.sleep(3)
            options.debugger_address = f'127.0.0.1:{port}'
            if settings.get('browser_backend') == 'native':
                from native_driver import NativeDriver
                driver = NativeDriver(options.debugger_address, settings['task_url'])
            else:
                driver = webdriver.Edge(options=options, service=Service(str(path)))
            driver._labsafe_debug_address = f'127.0.0.1:{port}'
        except BaseException:
            # Preserve the independent login window on interruption before attachment.
            raise
    driver._labsafe_attached = not headless
    driver._labsafe_keep_open = bool(settings.get('keep_browser_open')) and not headless
    driver.set_page_load_timeout(40)
    return driver


def close_driver(driver):
    if getattr(driver, '_labsafe_keep_open', False):
        if getattr(driver, '_labsafe_native', False):
            driver.quit()
        else:
            driver.service.stop()
        return
    if getattr(driver, '_labsafe_attached', False):
        try:
            import json
            import websocket
            address = driver._labsafe_debug_address
            with urllib.request.urlopen('http://' + address + '/json/version', timeout=2) as handle:
                endpoint = json.load(handle)['webSocketDebuggerUrl']
            connection = websocket.create_connection(endpoint, suppress_origin=True, timeout=2)
            try:
                connection.send(json.dumps({'id': 1, 'method': 'Browser.close'}))
            finally:
                connection.close()
        except (OSError, AttributeError, KeyError, WebDriverException):
            pass
    if driver.service.is_connectable():
        driver.quit()
    else:
        driver.service.stop()


class Browser:
    def __init__(self, driver, settings: dict, output: Path):
        self.driver, self.settings, self.output = driver, settings, output
        self.main_handle = driver.current_window_handle
        self.task_url = settings['task_url']
        self.wait = WebDriverWait(driver, 20)

    def js(self, operation, *args):
        return self.driver.execute_script(DOM, operation, *args)

    def is_task(self):
        return safe_url(self.driver.current_url).rstrip('/') == self.task_url.rstrip('/')

    def check_title(self):
        if normalized(self.settings['exam_name']) not in normalized(self.js('body')):
            raise NeedsAttention('未看到目标考试名称；请登录并打开目标课程总览页。')

    def close_questionbank_picker(self):
        if not self.is_task():
            return
        close = self.js('questionbank-picker-close')
        if close:
            close.click()
            self.wait.until(lambda _: self.js('questionbank-picker-close') is None)

    def load_task(self, refresh=False):
        self.driver.switch_to.window(self.main_handle)
        self.driver.switch_to.default_content()
        self.close_questionbank_picker()
        for attempt in range(2):
            try:
                if refresh and self.is_task():
                    self.driver.refresh()
                elif not self.is_task():
                    self.driver.get(self.task_url)
                self.wait.until(lambda _: self.js('header')['required'] is not None and
                                normalized(self.settings['exam_name']) in normalized(self.js('body')))
                self.blockers()
                return
            except (TimeoutException, StaleElementReferenceException):
                if attempt:
                    raise NeedsAttention('课程总览加载失败，已自动重试一次。')
                self.driver.refresh()

    def wait_for_login(self, timeout):
        print('请先在专用 Edge 的初始平台界面手动登录：' + self.settings.get('portal_url', self.settings['home_url']), flush=True)
        print('登录后通过“去学习”或地址栏打开：' + self.task_url, flush=True)
        print('检测到目标考试的必学/选学总览后，程序自动继续。', flush=True)
        if not getattr(self.driver, '_labsafe_attached', False):
            try:
                self.driver.get(self.task_url)
            except TimeoutException:
                pass
        deadline = time.monotonic() + timeout if timeout > 0 else float('inf')
        last_status = 0
        while time.monotonic() < deadline:
            if (self.output / 'stop.flag').exists():
                raise KeyboardInterrupt
            last_url, last_body_length = '', None
            for handle in self.login_handles():
                try:
                    self.driver.switch_to.window(handle)
                    self.driver.switch_to.default_content()
                    last_url = safe_url(self.driver.current_url)
                    if self.is_task():
                        body = self.js('body')
                        last_body_length = len(body)
                        if normalized(self.settings['exam_name']) in normalized(body):
                            header = self.js('header')
                            if header['required'] and header['optional']:
                                self.main_handle = handle
                                return
                except WebDriverException:
                    # Login can close a popup or navigate while handles are being enumerated.
                    # A stale window or a slow loading page must not close the user's other window.
                    continue
            if time.monotonic() - last_status >= 10:
                write_json(self.output / 'status.json', {'phase': 'waiting_for_login',
                    'task_url': self.task_url, 'updated_at': time.time(),
                    'current_url': last_url, 'body_characters': last_body_length,
                    'waiting_indefinitely': timeout == 0})
                last_status = time.monotonic()
            time.sleep(3)
        raise NeedsAttention('等待登录超时。浏览器配置已保留，重新启动可以继续。')

    def login_handles(self):
        handles = self.driver.window_handles
        address = getattr(self.driver, '_labsafe_debug_address', None)
        if not address:
            return handles
        try:
            with urllib.request.urlopen('http://' + address + '/json', timeout=2) as response:
                targets = json.load(response)
            host = urlsplit(self.task_url).hostname
            ids = [t['id'].lower() for t in targets if t.get('type') == 'page'
                   and urlsplit(t.get('url', '')).hostname == host]
            return [h for h in handles if any(h.lower().endswith(target_id) for target_id in ids)]
        except (OSError, ValueError, KeyError):
            return handles

    def blockers(self):
        try:
            alert = self.driver.switch_to.alert
            raise NeedsAttention('需要人工处理浏览器提示: ' + alert.text[:200])
        except NoAlertPresentException:
            pass
        messages = self.js('dialogs')
        if (messages and self.settings.get('restart_finished_video', False) and
                all(re.search(r'视频已经学习完[，,]?\s*是否重新学习', m) for m in messages)):
            replay = self.js('replay')
            if replay:
                logging.info('视频已播完但需要继续累计学习时长，确认从头正常播放。')
                replay.click()
                time.sleep(0.5)
                messages = self.js('dialogs')
        if messages:
            raise NeedsAttention('需要人工处理页面提示: ' + ' / '.join(messages)[:300])
        if self.driver.find_elements(By.CSS_SELECTOR, 'input[type=password]'):
            if any(e.is_displayed() for e in self.driver.find_elements(By.CSS_SELECTOR, 'input[type=password]')):
                raise NeedsAttention('登录失效，请重新登录。')

    def totals(self):
        self.driver.switch_to.default_content()
        data = self.js('header')
        if not data['required'] or not data['optional']:
            raise NeedsAttention('未识别必学/选学总时长，不能安全决定结束条件。')
        return Totals(*map(seconds, data['required']), *map(seconds, data['optional']))

    def wait_table(self):
        self.wait.until(lambda _: bool(self.js('rows')) or
                        bool(re.search('暂无数据|暂无记录|没有数据', self.js('body'))))

    def choose_tab(self, label):
        element = self.js('tab', label)
        if not element:
            raise NeedsAttention(f'未识别“{label}”标签。')
        element.click()
        time.sleep(0.6)
        self.wait_table()
        first = self.js('pager')['first']
        if first:
            first.click()
            self.wait.until(lambda _: self.js('pager')['active'] == 1)
            time.sleep(0.4)

    def page_rows(self, tab, page):
        return [Course.from_row(raw, tab, page) for raw in self.js('rows')]

    def next_page(self, old_page):
        element = self.js('pager')['next']
        if not element:
            return False
        before_rows = [(r['title'], r['module'], r['progress']) for r in self.js('rows')]
        element.click()
        self.wait.until(lambda _: self.js('pager')['active'] != old_page)
        self.wait.until(lambda _: [(r['title'], r['module'], r['progress']) for r in self.js('rows')] != before_rows)
        self.wait_table()
        return True

    def inventory(self):
        self.driver.switch_to.default_content()
        self.check_title()
        self.blockers()
        totals = self.totals()
        found = []
        for tab in ('未学', '已学'):
            self.choose_tab(tab)
            fingerprints = set()
            tab_count = 0
            while True:
                self.blockers()
                pager = self.js('pager')
                rows = self.page_rows(tab, pager['active'])
                fingerprint = tuple(c.key for c in rows)
                if fingerprint in fingerprints and rows:
                    raise NeedsAttention('分页内容重复，不能保证课程列表完整。')
                fingerprints.add(fingerprint)
                found.extend(rows)
                tab_count += len(rows)
                if not self.next_page(pager['active']):
                    if pager['total'] is not None and tab_count != pager['total']:
                        raise NeedsAttention(f'{tab}列表抓取 {tab_count} 条，与页面总数 {pager["total"]} 不符。')
                    break
        if not found and (totals.required_target > 0 or totals.optional_target > 0):
            raise NeedsAttention('两个列表均为空，但考试要求有学习时长，无法核对条目。')
        return merge_courses(found), totals

    def rewind_document(self, path):
        self.context(path)
        try:
            for _ in range(200):
                data = self.js('sample')
                if not data['page'] or data['page'] == 1:
                    return
                if not data['previous']:
                    raise NeedsAttention('文档恢复在中间页，但未识别“上一页”控件。')
                before = data['page']
                data['previous'].click()
                self.wait.until(lambda _: self.js('sample')['page'] != before)
            raise NeedsAttention('文档页数超过自动翻页限制，需要人工核对。')
        finally:
            self.driver.switch_to.default_content()

    def open_course(self, course):
        self.load_task()
        self.choose_tab('未学')
        while True:
            pager = self.js('pager')
            raws = self.js('rows')
            for raw in raws:
                current = Course.from_row(raw, '未学', pager['active'])
                if current.key == course.key:
                    if current.complete:
                        return None
                    if not raw.get('action'):
                        raise NeedsAttention('未识别该课程的“去学习”按钮: ' + course.title)
                    old_handles = set(self.driver.window_handles)
                    old_url = self.driver.current_url
                    raw['action'].click()
                    def navigated(_):
                        return (set(self.driver.window_handles) - old_handles or
                                self.driver.current_url != old_url or not self.js('rows') or
                                (current.kind_hint == 'questionbank' and
                                 any('可练习题库' in m for m in self.js('dialogs'))))
                    self.wait.until(navigated)
                    new = set(self.driver.window_handles) - old_handles
                    if new:
                        self.driver.switch_to.window(next(iter(new)))
                    return current
            if not self.next_page(pager['active']):
                raise NeedsAttention('课程未在未学列表找到，需要重新读取进度: ' + course.title)

    def contexts(self, path=(), depth=0):
        if depth > 4:
            return
        yield path
        frames = self.driver.find_elements(By.CSS_SELECTOR, 'iframe,frame')
        for index in range(len(frames)):
            try:
                fresh = self.driver.find_elements(By.CSS_SELECTOR, 'iframe,frame')
                self.driver.switch_to.frame(fresh[index])
            except (WebDriverException, IndexError):
                continue
            try:
                yield from self.contexts(path + (index,), depth + 1)
            finally:
                self.driver.switch_to.parent_frame()

    def context(self, path):
        self.driver.switch_to.default_content()
        for index in path:
            frames = self.driver.find_elements(By.CSS_SELECTOR, 'iframe,frame')
            if index >= len(frames):
                raise NeedsAttention('播放器 iframe 已变化。')
            self.driver.switch_to.frame(frames[index])

    def sample(self):
        self.driver.switch_to.default_content()
        self.blockers()
        result = {'video': None, 'timer': None, 'page': None, 'pages': None,
                  'video_context': (), 'viewer_context': (), 'counters': []}
        try:
            for path in self.contexts():
                self.blockers()
                data = self.js('sample')
                if data['video'] and result['video'] is None:
                    result.update(video=data['video'], video_context=path)
                if data['timer'] and result['timer'] is None:
                    result['timer'] = {k: seconds(data['timer'][k]) for k in ('learned', 'target')}
                if data['counters']:
                    result['counters'].extend({k: seconds(c[k]) for k in ('learned', 'target')}
                                             for c in data['counters'])
                if data['pages'] and result['pages'] is None:
                    result.update(page=data['page'], pages=data['pages'], viewer_context=path)
        finally:
            self.driver.switch_to.default_content()
        return result

    def play(self, path):
        self.context(path)
        result = self.js('play')
        self.driver.switch_to.default_content()
        return result

    def turn_document(self, path, target_page):
        self.context(path)
        try:
            data = self.js('sample')
            element = data['next'] if data['page'] and data['page'] < target_page else None
            if element:
                element.click()
        finally:
            self.driver.switch_to.default_content()

    def pause_media(self):
        try:
            self.driver.switch_to.default_content()
            # A native modal must be handled manually before any renderer operations.
            try:
                self.driver.switch_to.alert
                return
            except NoAlertPresentException:
                pass
            for _ in self.contexts():
                self.js('pause')
        except WebDriverException:
            pass
        finally:
            try:
                self.driver.switch_to.default_content()
            except WebDriverException:
                pass

    def return_to_task(self):
        self.pause_media()
        current = self.driver.current_window_handle
        if '/questionBank/exercises/' in self.driver.current_url:
            exits = self.driver.find_elements(By.CSS_SELECTOR, '.exercises .top-box .back')
            if not exits or not exits[0].is_displayed():
                raise NeedsAttention('题库退出控件未识别，保留页面等待检查。')
            exits[0].click()
            self.wait.until(lambda _: self.is_task())
            self.load_task(refresh=True)
            return
        # Let the site's own return handler finish recording the active lesson.
        back = self.js('return')
        if back:
            back.click()
            time.sleep(1)
        if current != self.main_handle:
            self.driver.close()
            self.driver.switch_to.window(self.main_handle)
        elif not self.is_task():
            self.driver.get(self.task_url)
        elif not back:
            self.driver.get(self.task_url)
        self.load_task(refresh=True)

    def diagnostic(self, reason):
        self.output.mkdir(parents=True, exist_ok=True)
        data = {'reason': reason, 'time': time.time()}
        try:
            data.update(url=safe_url(self.driver.current_url), title=self.driver.title)
            self.driver.switch_to.default_content()
            if '/examTask/' in self.driver.current_url:
                self.driver.save_screenshot(str(self.output / 'attention.png'))
                data['header'] = self.js('header')
                data['row_count'] = len(self.js('rows'))
        except WebDriverException:
            data['browser_prompt_or_unavailable'] = True
        write_json(self.output / 'attention.json', data)

    def export_inventory(self, courses, totals):
        write_json(self.output / 'inventory.json', {'captured_at': time.time(),
            'task_url': self.task_url, 'totals': asdict(totals), 'courses': [asdict(c) for c in courses]})
