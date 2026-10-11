"""Build a source-only archive and an auditable evidence snapshot."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
import re
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / 'dist'


def build():
    settings = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
    account = sha256(settings['default_account'].encode()).hexdigest()[:12]
    runtime = ROOT / 'runtime' / account
    test_result = (ROOT / 'runtime' / 'test-results-latest.txt').read_text(encoding='utf-8-sig')
    if not re.search(r'Ran [1-9]\d* tests', test_result) or not test_result.rstrip().endswith('OK'):
        raise RuntimeError('Latest full-test result is not successful; archive was not built.')
    validation = json.loads((runtime / 'validation.json').read_text(encoding='utf-8'))
    if not validation.get('both_material_types_passed'):
        raise RuntimeError('Real document and automatic-video validation has not passed.')
    inventory = json.loads((runtime / 'inventory.json').read_text(encoding='utf-8'))
    summary = {
        'captured_at': inventory['captured_at'], 'task_url': inventory['task_url'],
        'totals': inventory['totals'], 'course_count': len(inventory['courses']),
        'tabs': dict(Counter(c['tab'] for c in inventory['courses'])),
        'required_target_sum': sum(c['target'] for c in inventory['courses'] if c['required']),
        'course_completion': 'Still running; consult the live website and runtime/status.json.',
    }
    evidence = {
        'evidence/tests.txt': test_result.encode('utf-8'),
        'evidence/validation.json': json.dumps(validation, ensure_ascii=False, indent=2).encode('utf-8'),
        'evidence/inventory-summary.json': json.dumps(summary, ensure_ascii=False, indent=2).encode('utf-8'),
    }
    switch_fix = ROOT / 'runtime' / 'video-switch-fix-verification.json'
    if switch_fix.exists():
        evidence['evidence/video-switch-fix.json'] = switch_fix.read_bytes()
    current = {c['key']: c for c in inventory['courses']}
    after_delay = {}
    for kind, result in validation['real_site'].items():
        course = current.get(result['key'])
        if course is None or course['learned'] < course['target']:
            raise RuntimeError('Pilot is not still confirmed in the later website snapshot: ' + kind)
        after_delay[kind] = {
            'course': course['title'], 'website_seconds': course['learned'],
            'required_seconds': course['target'], 'captured_at': inventory['captured_at'],
            'seconds_since_first_confirmation': int(inventory['captured_at'] - result['checked_at']),
        }
    evidence['evidence/pilots-after-delay.json'] = json.dumps(after_delay, ensure_ascii=False, indent=2).encode('utf-8')
    allowed = {'.py', '.cmd', '.ps1', '.md'}
    entries = {p.name: p.read_bytes() for p in ROOT.iterdir() if p.is_file() and
               (p.suffix in allowed or p.name in {'requirements.txt', 'requirements-lock.txt', 'settings.json', 'reference-answers.json', '.gitignore'})}
    for path in (ROOT / 'tests').rglob('*'):
        if path.is_file() and path.suffix in {'.py', '.html'}:
            entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    entries.update(evidence)
    entries['SHA256SUMS.txt'] = ''.join(f'{sha256(data).hexdigest()}  {name}\n'
                                       for name, data in sorted(entries.items())).encode('utf-8')
    OUTPUT.mkdir(exist_ok=True)
    target = OUTPUT / 'labsafe-study-source.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr('labsafe-study/' + name, data)
    # Verify exact archive contents and every manifest hash.
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('Archive integrity check failed.')
        names = {name.removeprefix('labsafe-study/') for name in archive.namelist()}
        listed = set()
        for line in archive.read('labsafe-study/SHA256SUMS.txt').decode('utf-8').splitlines():
            expected, name = line.split('  ', 1)
            if sha256(archive.read('labsafe-study/' + name)).hexdigest() != expected:
                raise RuntimeError('Manifest mismatch: ' + name)
            listed.add(name)
        if names != listed | {'SHA256SUMS.txt'}:
            raise RuntimeError('Archive contains unexpected files.')
    stamp = datetime.now(timezone(timedelta(hours=8))).isoformat()
    (OUTPUT / 'package-check.json').write_text(json.dumps({
        'checked_at': stamp, 'archive': target.name, 'files': len(entries),
        'sha256': sha256(target.read_bytes()).hexdigest(), 'manifest_verified': True,
        'includes_browser_profile': False, 'real_pilots_passed': True,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(target)


if __name__ == '__main__':
    build()
