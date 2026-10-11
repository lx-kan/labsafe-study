"""Signal a running instance without sharing credentials or touching the browser."""
import argparse
import hashlib
import json
from pathlib import Path

parser = argparse.ArgumentParser(description='暂停/继续本地学习辅助程序')
parser.add_argument('action', choices=['pause', 'resume', 'connect', 'stop'])
settings = json.loads((Path(__file__).resolve().parent / 'settings.json').read_text(encoding='utf-8'))
parser.add_argument('--account', default=settings.get('default_account', 'default'))
args = parser.parse_args()
tag = hashlib.sha256(args.account.encode()).hexdigest()[:12]
folder = Path(__file__).resolve().parent / 'runtime' / tag
folder.mkdir(parents=True, exist_ok=True)
(folder / (args.action + '.flag')).write_text(args.action, encoding='utf-8')
print('已发送信号。继续前请先处理浏览器内提示；停止时会保留专用浏览器和进度。')
