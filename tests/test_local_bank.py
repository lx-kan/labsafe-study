import json
from pathlib import Path
import tempfile
import unittest

from local_bank import Archive, coverage, fingerprint, parse_answer


def question(options=None, result=''):
    return {'id': 'm-item-1', 'type': '多选题', 'title': '哪些做法正确？',
            'options': options or ['A、戴眼镜', 'B、戴手套', 'C、戴口罩'], 'result': result}


class LocalBankTests(unittest.TestCase):
    def test_answer_feedback_and_reordered_options(self):
        q = question(result='正确答案：A、C\n我的答案：A、B、C')
        with tempfile.TemporaryDirectory() as folder:
            archive = Archive(folder)
            archive.record(q, {'bank': '化学', 'page': 1, 'position': 1})
            shuffled = question(['A、戴口罩', 'B、戴眼镜', 'C、戴手套'])
            self.assertEqual(fingerprint(q), fingerprint(shuffled))
            self.assertEqual(Archive(folder).match(shuffled), ([0, 1], None))
            self.assertEqual(parse_answer('正确答案：A,B\n我的答案：C', q), [0, 1])

    def test_conflict_and_duplicate_source(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Archive(folder)
            source = {'bank': '电气', 'page': 1, 'position': 1}
            archive.record(question(result='正确答案：A\n我的答案：A'), source)
            archive.record(question(result='正确答案：A\n我的答案：A'), source)
            entry = next(iter(archive.data['questions'].values()))
            self.assertEqual(len(entry['sources']), 1)
            self.assertEqual(len(entry['observations']), 1)
            archive.record(question(result='正确答案：B\n我的答案：A'),
                           {'bank': '通识', 'page': 1, 'position': 2})
            self.assertEqual(Archive(folder).match(question()), (None, '答案冲突'))

    def test_unknown_and_missing_stay_unfilled(self):
        with tempfile.TemporaryDirectory() as folder:
            archive = Archive(folder)
            archive.record(question(), {'bank': '化学', 'page': 1, 'position': 1}, '未提交')
            self.assertEqual(archive.match(question()), (None, '尚无正确答案'))
            self.assertEqual(archive.match({**question(), 'type': '填空题'}), (None, '未知题型'))
            self.assertEqual(archive.match({**question(), 'images': [{'local': 'x'}]}),
                             (None, '图片题需要人工核对'))

    def test_coverage_needs_all_pages_and_answers(self):
        entries = {'x': {'status': 'verified'}, 'y': {'status': 'missing'}}
        banks = {'化学': {'finished': True, 'seen': 40, 'total': 40, 'issues': []}}
        self.assertFalse(coverage(banks, entries)['complete'])
        entries['y']['status'] = 'verified'
        self.assertTrue(coverage(banks, entries)['complete'])
        banks['化学']['seen'] = 20
        self.assertFalse(coverage(banks, entries)['complete'])

    def test_judgment_answer(self):
        q = {'type': '判断题', 'title': '示例', 'options': ['对', '错']}
        self.assertEqual(parse_answer('正确答案：错\n我的答案：对', q), [1])
        self.assertIsNone(parse_answer('只有题目解析：错', q))


if __name__ == '__main__':
    unittest.main()
