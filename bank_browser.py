"""Dedicated browser attachment; no study scheduler or study-state writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit

from core import NeedsAttention, write_json
from native_driver import NativeDriver


def attach(root, settings, account, timeout=60):
    tag = hashlib.sha256(account.encode()).hexdigest()[:12]
    connection = root / 'runtime' / tag / 'browser-connection.json'
    port = None
    if connection.exists():
        try:
            saved = json.loads(connection.read_text(encoding='utf-8'))
            candidate = int(saved['port'])
            with urllib.request.urlopen(f'http://127.0.0.1:{candidate}/json/version', timeout=2) as response:
                version = json.load(response)
            if version['webSocketDebuggerUrl'] == saved['identity']:
                port = candidate
        except (OSError, ValueError, KeyError):
            pass
    if port is None:
        binaries = [Path(os.environ.get(name, default)) / 'Microsoft/Edge/Application/msedge.exe'
                    for name, default in [('PROGRAMFILES(X86)', r'C:\Program Files (x86)'),
                                          ('PROGRAMFILES', r'C:\Program Files')]]
        binary = next((p for p in binaries if p.exists()), None)
        if not binary:
            raise NeedsAttention('没有找到 Edge。')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        subprocess.Popen([str(binary), f'--remote-debugging-port={port}',
            f'--user-data-dir={root / "browser-profile" / tag}', '--no-first-run',
            '--window-size=1400,950', settings['portal_url']],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=1) as response:
                    version = json.load(response)
                write_json(connection, {'port': port, 'identity': version['webSocketDebuggerUrl']})
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise NeedsAttention('Edge 调试连接未就绪；请关闭占用同一配置的窗口再重试。')
                time.sleep(.3)
    deadline = time.monotonic() + timeout if timeout else float('inf')
    host = urlsplit(settings['task_url']).hostname
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/json', timeout=3) as response:
            pages = json.load(response)
        page = next((p for p in pages if p.get('type') == 'page' and
                     urlsplit(p.get('url', '')).hostname == host), None)
        if page:
            driver = NativeDriver(f'127.0.0.1:{port}', page['url'])
            driver._labsafe_keep_open = True
            return driver
        time.sleep(1)
    raise NeedsAttention('请在专用 Edge 中打开学校平台后重试。')
