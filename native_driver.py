"""Minimal UI transport over Edge's native debugging protocol.

Does not start a WebDriver browser session or read/modify authentication cookies.
Selenium waits and exceptions are reused by the existing semantic page adapter.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
import time
import urllib.request
from urllib.parse import urlsplit

import websocket
from selenium.common.exceptions import (NoAlertPresentException, NoSuchWindowException,
                                        StaleElementReferenceException, WebDriverException)


class Element:
    def __init__(self, driver, path, frame):
        self.driver, self.path, self.frame = driver, path, frame

    def expression(self):
        return '(function(){let e=document.documentElement;for(const i of ' + json.dumps(self.path) + \
            '){e=e?.children[i];}return e;})()'

    def remote(self):
        value = self.driver.evaluate(self.expression(), frame=self.frame, by_value=False)
        if not value.get('objectId') or value.get('subtype') == 'null':
            raise StaleElementReferenceException('The page element changed')
        return value['objectId']

    def click(self):
        object_id = self.remote()
        try:
            self.driver.rpc('Page.bringToFront')
            result = self.driver.rpc('Runtime.callFunctionOn', {
                'objectId': object_id, 'userGesture': True,
                'functionDeclaration': 'function(){this.scrollIntoView({block:"center"});this.click();}'})
            if result.get('exceptionDetails'):
                raise WebDriverException('Page control click failed')
        finally:
            try:
                self.driver.rpc('Runtime.releaseObject', {'objectId': object_id})
            except WebDriverException as exc:
                # A successful click may replace the page and destroy its JS context.
                if not any(t in str(exc) for t in ('Cannot find context', 'Cannot find object')):
                    raise

    def is_displayed(self):
        expression = '(function(){const e=' + self.expression() + \
            ';return !!(e&&e.getClientRects().length&&getComputedStyle(e).visibility!=="hidden");})()'
        return bool(self.driver.evaluate(expression, frame=self.frame).get('value'))


class Dialog:
    def __init__(self, driver):
        self.driver = driver

    @property
    def text(self):
        return self.driver.dialog_message

    def dismiss(self):
        self.driver.rpc('Page.handleJavaScriptDialog', {'accept': False})
        self.driver.dialog_message = None

    def accept(self):
        self.driver.rpc('Page.handleJavaScriptDialog', {'accept': True})
        self.driver.dialog_message = None


class Switch:
    def __init__(self, driver):
        self.driver = driver

    def window(self, handle):
        self.driver.connect(handle)

    def default_content(self):
        self.driver.frame_stack = []

    def parent_frame(self):
        if self.driver.frame_stack:
            self.driver.frame_stack.pop()

    def frame(self, element):
        object_id = element.remote()
        try:
            node = self.driver.rpc('DOM.describeNode', {'objectId': object_id})['node']
            frame_id = node.get('frameId') or node.get('contentDocument', {}).get('frameId')
            if not frame_id:
                raise WebDriverException('Frame owner has no frame ID')
            self.driver.frame_stack.append(frame_id)
        finally:
            self.driver.rpc('Runtime.releaseObject', {'objectId': object_id})

    @property
    def alert(self):
        if not self.driver.dialog_message:
            # Renderer RPCs can block while a modal dialog is open.
            self.driver.drain_events()
        if self.driver.dialog_message is None:
            raise NoAlertPresentException('No browser dialog')
        return Dialog(self.driver)


class NativeDriver:
    def __init__(self, address, preferred_url):
        self._labsafe_debug_address = address
        self._labsafe_attached = True
        self._labsafe_native = True
        self.sequence = 0
        self.socket = None
        self.frame_stack = []
        self.dialog_message = None
        self.switch_to = Switch(self)
        self.service = self
        pages = self.pages()
        wanted = urlsplit(preferred_url)
        siblings = [p for p in pages if urlsplit(p.get('url', '')).netloc == wanted.netloc and
                    urlsplit(p.get('url', '')).path.startswith(wanted.path.rsplit('/', 1)[0] + '/')]
        selected = next((p for p in pages if p.get('url') == preferred_url),
                        siblings[0] if siblings else None)
        if selected is None:
            raise NoSuchWindowException('No dedicated browser page')
        self.connect(selected['id'])

    def pages(self):
        with urllib.request.urlopen('http://' + self._labsafe_debug_address + '/json', timeout=3) as handle:
            return [p for p in json.load(handle) if p.get('type') == 'page']

    @property
    def window_handles(self):
        return [p['id'] for p in self.pages()]

    def connect(self, handle):
        if self.socket and self.current_window_handle == handle:
            return
        page = next((p for p in self.pages() if p['id'] == handle), None)
        if page is None:
            raise NoSuchWindowException('Dedicated page closed')
        if self.socket:
            self.socket.close()
        self.socket = websocket.create_connection(page['webSocketDebuggerUrl'], suppress_origin=True, timeout=15)
        self.current_window_handle = handle
        self.frame_stack = []
        self.dialog_message = None
        self.rpc('Page.enable')

    def rpc(self, method, params=None):
        self.sequence += 1
        sequence = self.sequence
        try:
            self.socket.send(json.dumps({'id': sequence, 'method': method, 'params': params or {}}))
            while True:
                value = json.loads(self.socket.recv())
                if value.get('method') == 'Page.javascriptDialogOpening':
                    self.dialog_message = value['params']['message']
                elif value.get('method') == 'Page.javascriptDialogClosed':
                    self.dialog_message = None
                if value.get('id') == sequence:
                    if value.get('error'):
                        raise WebDriverException(value['error'].get('message', 'Native protocol failure'))
                    return value.get('result', {})
        except (OSError, ValueError, websocket.WebSocketException) as exc:
            raise WebDriverException(type(exc).__name__) from exc

    def drain_events(self):
        previous = self.socket.gettimeout()
        self.socket.settimeout(0.05)
        try:
            while True:
                try:
                    value = json.loads(self.socket.recv())
                except websocket.WebSocketTimeoutException:
                    break
                if value.get('method') == 'Page.javascriptDialogOpening':
                    self.dialog_message = value['params']['message']
                elif value.get('method') == 'Page.javascriptDialogClosed':
                    self.dialog_message = None
        finally:
            self.socket.settimeout(previous)

    @property
    def frame(self):
        return self.frame_stack[-1] if self.frame_stack else self.rpc('Page.getFrameTree')['frameTree']['frame']['id']

    def evaluate(self, expression, frame=None, by_value=True):
        self.drain_events()
        if self.dialog_message is not None:
            raise WebDriverException('Browser dialog requires manual action')
        frame_id = frame or self.frame
        context = self.rpc('Page.createIsolatedWorld', {'frameId': frame_id, 'worldName': 'labsafe-ui'})['executionContextId']
        result = self.rpc('Runtime.evaluate', {'expression': expression, 'contextId': context,
                                             'returnByValue': by_value, 'userGesture': True})
        if result.get('exceptionDetails'):
            raise WebDriverException(result['exceptionDetails'].get('text', 'Page script failed'))
        return result['result']

    def decode(self, value, frame):
        if isinstance(value, dict):
            if '__labsafe_element__' in value:
                return Element(self, value['__labsafe_element__'], frame)
            return {key: self.decode(item, frame) for key, item in value.items()}
        if isinstance(value, list):
            return [self.decode(item, frame) for item in value]
        return value

    def execute_script(self, script, *arguments):
        # Serialize only the DOM references needed by the page adapter; create no globals.
        pack = r'''
        function pack(v){
          if(v instanceof Element){let p=[],e=v;while(e.parentElement){
            p.unshift(Array.from(e.parentElement.children).indexOf(e));e=e.parentElement;}
            return {__labsafe_element__:p};}
          if(Array.isArray(v))return v.map(pack);
          if(v&&typeof v==='object')return Object.fromEntries(Object.entries(v).map(([k,x])=>[k,pack(x)]));
          return v===undefined?null:v;
        }
        '''
        frame = self.frame
        expression = '(function(){' + pack + 'return JSON.stringify(pack((function(){' + script + \
            '}).apply(null,' + json.dumps(arguments, ensure_ascii=False) + ')));})()'
        encoded = self.evaluate(expression, frame=frame).get('value')
        return self.decode(json.loads(encoded), frame)

    @property
    def current_url(self):
        return self.evaluate('location.href').get('value', '')

    @property
    def title(self):
        return self.evaluate('document.title').get('value', '')

    def get(self, url):
        self.frame_stack = []
        result = self.rpc('Page.navigate', {'url': url})
        if result.get('errorText'):
            raise WebDriverException('Navigation failed')
        time.sleep(0.8)

    def refresh(self):
        self.frame_stack = []
        self.rpc('Page.reload')
        time.sleep(0.8)

    def close(self):
        handle = self.current_window_handle
        self.rpc('Page.close')
        self.socket.close()
        self.socket = None
        self.current_window_handle = None

    def save_screenshot(self, path):
        encoded = self.rpc('Page.captureScreenshot', {'format': 'png'})['data']
        Path(path).write_bytes(base64.b64decode(encoded))
        return True

    def find_elements(self, by, selector):
        if by != 'css selector':
            raise ValueError('Only CSS selectors are used by this adapter')
        return self.execute_script('return Array.from(document.querySelectorAll(arguments[0]));', selector)

    def set_page_load_timeout(self, seconds):
        self.page_load_timeout = seconds

    def quit(self):
        if self.socket:
            self.socket.close()
            self.socket = None

    def is_connectable(self):
        return False

    def stop(self):
        self.quit()
