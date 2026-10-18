"""Local, resumable question archive. Answers come only from visible site feedback."""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path
import re
import time

from core import write_json


def clean(text):
    # Do not delete punctuation, minus signs, units or internal word boundaries.
    return re.sub(r'\s+', ' ', str(text).replace('\u00a0', ' ')).strip()


def option_text(text):
    return clean(re.sub(r'^\s*[A-Z][、.．:：]\s*', '', text))


def kind(text):
    return next((k for k in ('多选题', '单选题', '判断题') if k in text), None)


def fingerprint(question):
    content = [kind(question['type']), clean(question['title']),
               sorted(option_text(o) for o in question['options']),
               question.get('image_hashes', [])]
    return hashlib.sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()


def parse_answer(feedback, question):
    match = re.search(r'正确答案\s*[:：]\s*(.*?)(?=我的答案|您的答案|你的答案|答案解析|解析[:：]|$)',
                      feedback, flags=re.S)
    if not match:
        return None
    answer = clean(match.group(1))
    if re.fullmatch(r'[A-Z](?:[\s,，、;；]*[A-Z])*[。.]?', answer):
        indices = sorted({ord(letter) - ord('A') for letter in re.findall('[A-Z]', answer)})
    elif kind(question['type']) == '判断题':
        truth = {'正确': True, '对': True, '是': True, '√': True,
                 '错误': False, '错': False, '否': False, '×': False}
        if answer not in truth:
            return None
        indices = [i for i, text in enumerate(question['options'])
                   if option_text(text) in truth and truth[option_text(text)] == truth[answer]]
    else:
        return None
    if (not indices or max(indices) >= len(question['options']) or
            (kind(question['type']) != '多选题' and len(indices) != 1)):
        return None
    return indices


class Archive:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.path = self.folder / 'questions.json'
        self.data = (json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists()
                     else {'version': 1, 'questions': {}})

    def record(self, question, source, reason=None):
        key = fingerprint(question)
        entry = self.data['questions'].setdefault(key, {
            'fingerprint': key, 'type': question['type'], 'title': question['title'],
            'options': question['options'], 'images': question.get('images', []),
            'image_hashes': question.get('image_hashes', []),
            'sources': [], 'observations': [], 'status': 'missing', 'answer_contents': []})
        if source not in entry['sources']:
            entry['sources'].append(source)
        feedback = question.get('result', '')
        indices = parse_answer(feedback, question)
        if indices is not None and not question.get('image_error'):
            contents = sorted(option_text(question['options'][i]) for i in indices)
            observation = {'captured_at': time.time(), 'feedback': feedback,
                           'letters': [chr(65+i) for i in indices], 'answer_contents': contents,
                           'options': question['options'], 'source': source}
            if not any(o['answer_contents'] == contents and o['feedback'] == feedback and
                       o['source'] == source for o in entry['observations']):
                entry['observations'].append(observation)
            variants = {tuple(o['answer_contents']) for o in entry['observations']}
            entry['status'] = 'verified' if len(variants) == 1 else 'conflict'
            entry['answer_contents'] = contents if len(variants) == 1 else []
        entry['reason'] = reason
        entry['updated_at'] = time.time()
        self.save()
        return key

    def match(self, question):
        if kind(question['type']) is None:
            return None, '未知题型'
        if question.get('images') or question.get('image_error'):
            return None, '图片题需要人工核对'
        entry = self.data['questions'].get(fingerprint(question))
        if not entry:
            return None, '本地未收录'
        if entry['status'] != 'verified':
            return None, '答案冲突' if entry['status'] == 'conflict' else '尚无正确答案'
        options = [option_text(o) for o in question['options']]
        if len(options) != len(set(options)):
            return None, '选项内容重复，无法唯一映射'
        indices = [i for i, text in enumerate(options) if text in entry['answer_contents']]
        if not indices or len(indices) != len(entry['answer_contents']):
            return None, '答案无法映射'
        return indices, None

    def save(self):
        write_json(self.path, self.data)

    def export_html(self):
        cards = []
        for entry in self.data['questions'].values():
            e = html.escape
            options = ''.join('<li>' + e(o) + '</li>' for o in entry['options'])
            images = ''.join('<img loading="lazy" style="max-width:100%" src="' + e(i['local']) + '">'
                             for i in entry['images'] if i.get('local'))
            source = ' / '.join(sorted({s['bank'] for s in entry['sources']}))
            if entry['status'] == 'verified' and entry['observations']:
                observed = entry['observations'][-1]
                answer = ','.join(observed['letters']) + '：' + '；'.join(entry['answer_contents'])
            else:
                answer = entry['status']
            cards.append('<article><small>' + e(source + ' · ' + entry['type']) + '</small><h3>' +
                         e(entry['title']) + '</h3>' + images + '<ul>' + options + '</ul><details><summary>查看答案</summary><p>' +
                         e(answer) + '</p></details></article>')
        page = '''<!doctype html><meta charset="utf-8"><title>本地练习题库</title>
<style>body{max-width:900px;margin:30px auto;padding:16px;font:17px/1.7 system-ui;background:#f5f7fa}
input{width:95%;padding:12px}article{background:white;padding:20px;margin:16px 0;border-radius:10px}
small{color:#555}summary{cursor:pointer}</style><h1>本地练习题库</h1>
<p>答案来自网站练习反馈。missing 表示缺失，conflict 表示冲突。</p>
<input placeholder="搜索分类、题干、选项或答案" aria-label="搜索"><p id="count"></p>''' + ''.join(cards) + '''
<script>const cards=[...document.querySelectorAll('article')];function filter(){let n=0;
const q=document.querySelector('input').value.toLowerCase();for(const c of cards){c.hidden=!c.textContent.toLowerCase().includes(q);if(!c.hidden)n++;}
document.querySelector('#count').textContent=`显示 ${n} / ${cards.length} 题`;}
document.querySelector('input').addEventListener('input',filter);filter();</script>'''
        self.folder.mkdir(parents=True, exist_ok=True)
        temporary = self.folder / 'index.html.tmp'
        temporary.write_text(page, encoding='utf-8')
        temporary.replace(self.folder / 'index.html')


def coverage(banks, entries):
    issues = [k for k, q in entries.items() if q['status'] != 'verified']
    complete = bool(banks) and all(b.get('finished') and
        (b.get('total') is None or b['seen'] == b['total']) and not b.get('issues') for b in banks.values())
    return {'complete': complete and not issues, 'banks': banks,
            'unique_questions': len(entries), 'verified': sum(q['status'] == 'verified' for q in entries.values()),
            'unresolved': issues}
