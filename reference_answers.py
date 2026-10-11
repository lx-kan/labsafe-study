"""Read-only matching to a public university reference. Never clicks or submits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import unicodedata

SOURCE = 'https://syzx.sdsmu.edu.cn/_upload/article/files/68/5c/caefec194d1fbf6efcbd138663d4/b36430f2-2323-4d3c-80cb-2b67d0eed9b6.pdf'
INDEX = Path(__file__).resolve().parent / 'reference-answers.json'


def digest(text):
    text = unicodedata.normalize('NFKC', text)
    text = re.sub(r'\s+', '', text)
    text = re.sub(r'[，,。:：;；?？!！、]', '', text)
    text = re.sub(r'\(\)|_+', '', text)
    text = text.rstrip('.')
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def build(pdf, output=INDEX):
    import fitz
    doc = fitz.open(pdf)
    parts, positions = [], []
    offset = 0
    for page_num, page in enumerate(doc, 1):
        lines = [line.strip() for line in page.get_text().splitlines()
                 if line.strip() and '山东第二医科大学医学研究实验中心' not in line]
        # This identified PDF prints its page number before the body after two covers.
        # Keep every other numeric line: a wrapped option can contain a bare number.
        if page_num >= 3 and lines and lines[0] == str(page_num - 2):
            lines = lines[1:]
        part = '\n'.join(lines) + '\n'
        positions.append((offset, page_num))
        parts.append(part)
        offset += len(part)
    text = ''.join(parts)
    starts = list(re.finditer(r'(?m)^\d+\.\s+', text))
    entries = []
    for i, start in enumerate(starts):
        block = text[start.end():starts[i+1].start() if i+1 < len(starts) else len(text)]
        options = list(re.finditer(r'(?m)^([A-H])\.\s*', block))
        if len(options) < 2:
            continue
        title = block[:options[0].start()]
        answer = re.search(r'[（(]\s*([A-H](?:[、,，\s]*[A-H])*)\s*[）)]', title)
        if not answer:
            continue
        letters = re.findall('[A-H]', answer.group(1))
        title = title[:answer.start()] + title[answer.end():]
        labels = [m.group(1) for m in options]
        if labels != list('ABCDEFGH')[:len(labels)] or not set(letters) <= set(labels):
            continue
        texts = [block[m.end():options[j+1].start() if j+1 < len(options) else len(block)]
                 for j,m in enumerate(options)]
        texts[-1] = re.split(r'\n(?:单选题|多选题|判断题|第[一二三四五六七八九十]+章)', texts[-1])[0]
        page = max(p for off,p in positions if off <= start.start())
        entries.append({'title_hash': digest(title), 'option_hashes': [digest(t) for t in texts],
                        'answer_hashes': [digest(texts[labels.index(l)]) for l in letters], 'pdf_page': page})
    data = {'source': SOURCE, 'reference_date': '2024-03',
            'source_sha256': hashlib.sha256(Path(pdf).read_bytes()).hexdigest(), 'entries': entries}
    Path(output).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Built {len(entries)} strict reference entries: {output}')


class ReferenceAnswers:
    def __init__(self, index=INDEX):
        self.data = json.loads(Path(index).read_text(encoding='utf-8'))
        self.by_title = {}
        for entry in self.data['entries']:
            self.by_title.setdefault(entry['title_hash'], []).append(entry)

    def match(self, question):
        if question.get('answered'):
            return None
        options = [re.sub(r'^\s*[A-H][、.．]\s*', '', t) for t in question['options']]
        hashes = [digest(t) for t in options]
        if len(hashes) != len(set(hashes)):
            return None
        candidates = []
        for entry in self.by_title.get(digest(question['title']), []):
            if sorted(hashes) != sorted(entry['option_hashes']):
                continue
            selected = [i for i,h in enumerate(hashes) if h in entry['answer_hashes']]
            if not selected or ('单选题' in question.get('type', '') and len(selected) != 1):
                continue
            candidates.append((selected, entry['pdf_page']))
        if not candidates or len({tuple(indices) for indices,page in candidates}) != 1:
            return None
        return {'indices': candidates[0][0], 'source': self.data['source'], 'pdf_page': candidates[0][1]}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Build read-only hashed references from the public PDF')
    parser.add_argument('pdf', type=Path)
    args = parser.parse_args()
    build(args.pdf)
