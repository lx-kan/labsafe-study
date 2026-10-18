"""Explicit entry for a site-confirmed formal exam; never submits the paper."""
import argparse
import json
from pathlib import Path
import sys

from mock_fill import fill


if __name__ == '__main__':
    root = Path(__file__).resolve().parent
    settings = json.loads((root / 'settings.json').read_text(encoding='utf-8'))
    parser = argparse.ArgumentParser(description='只填写网站明确确认的正式考试；由用户手动交卷')
    parser.add_argument('--account', default=settings.get('default_account', 'default'))
    args = parser.parse_args()
    try:
        sys.exit(fill(settings, args.account, expected_mock=False))
    except KeyboardInterrupt:
        print('填答已停止；请在浏览器中检查已填选项。', flush=True)
        sys.exit(130)
    except Exception as exc:
        print('填答停止：' + str(exc)[:300], flush=True)
        sys.exit(1)
