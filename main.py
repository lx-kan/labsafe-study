from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
import sys

from browser import Browser, create_driver, close_driver
from core import NeedsAttention, State, safe_url, write_json
from runner import Runner
from instance import InstanceLock
from selenium.common.exceptions import WebDriverException


ROOT = Path(__file__).resolve().parent


def main():
    settings = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
    parser = argparse.ArgumentParser(description='独立实验室安全课程辅助；手动登录，正常计时，网站确认。')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--inspect', action='store_true', help='只读取课程总览和两类列表，不打开学习材料')
    mode.add_argument('--run', action='store_true', help='先验证文档/视频计时，验证后继续完整学习')
    mode.add_argument('--validate-only', action='store_true', help='只完成文档与视频的真实网站试运行')
    parser.add_argument('--account', default=settings.get('default_account', 'default'), help='本地账号标签；不同账号必须使用不同标签')
    parser.add_argument('--login-timeout', type=int, help='等待手动登录秒数；0 为持续等待，直到进入课程页或按 Ctrl+C')
    parser.add_argument('--questionbank-session-seconds', type=int,
                        help='题库每次浏览时长，默认 900 秒；退出保存后轮换下一题库')
    args = parser.parse_args()
    if args.questionbank_session_seconds is not None:
        if args.questionbank_session_seconds <= 0:
            parser.error('题库每次浏览时长必须大于零。')
        settings['questionbank_session_seconds'] = args.questionbank_session_seconds
    if args.login_timeout is not None:
        settings['login_timeout_seconds'] = args.login_timeout
    if settings['login_timeout_seconds'] < 0:
        parser.error('登录等待时间不能为负数。')
    if settings['poll_seconds'] <= 0 or settings['confirmation_timeout_seconds'] <= settings['confirmation_delay_seconds']:
        parser.error('轮询必须为正数，确认超时必须大于更新延迟。')
    tag = hashlib.sha256(args.account.encode()).hexdigest()[:12]
    output = ROOT / 'runtime' / tag
    output.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(output / 'study.log', encoding='utf-8')])
    driver = None
    runner = None
    lock = InstanceLock(output / 'instance.lock')
    try:
        lock.acquire()
        (output / 'stop.flag').unlink(missing_ok=True)
        driver = create_driver(ROOT, settings, args.account)
        browser = Browser(driver, settings, output)
        browser.wait_for_login(settings['login_timeout_seconds'])
        if args.inspect:
            courses, totals = browser.inventory()
            browser.export_inventory(courses, totals)
            write_json(output / 'status.json', {'phase': 'inspection_complete', 'course_count': len(courses)})
            logging.info('检查完成，报告: %s', output / 'inventory.json')
            return 0
        state = State(output / 'state.json', safe_url(settings['task_url']), args.account)
        runner = Runner(browser, state, settings, output, args.validate_only)
        while True:
            try:
                runner.run()
                return 0
            except NeedsAttention as exc:
                runner.wait_for_resume(str(exc))
            except WebDriverException as exc:
                runner.wait_for_resume('浏览器操作异常: ' + type(exc).__name__ + '，请检查窗口后继续。')
    except KeyboardInterrupt:
        if runner:
            try:
                runner.recover_active()
            except Exception as exc:
                logging.warning('退出时未能核对活动课程，重启将重新读取网站记录: %s', type(exc).__name__)
            runner.status('stopped')
        logging.info('已停止，本地进度和专用浏览器配置已保留。')
        return 130
    except Exception as exc:
        # Driver exceptions can contain page source or signed URLs; record the type and a bounded message.
        message = str(exc)[:500] if isinstance(exc, (NeedsAttention, ValueError)) else type(exc).__name__
        logging.error('启动或页面操作失败: %s', message)
        write_json(output / 'status.json', {'phase': 'error', 'error': message})
        return 1
    finally:
        if driver:
            try:
                close_driver(driver)
            except Exception:
                pass
        lock.release()


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
