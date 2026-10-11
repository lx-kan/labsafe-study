import json
from pathlib import Path
import tempfile
import unittest

from reference_answers import digest, ReferenceAnswers


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'reference.json'
        self.entry = {'title_hash': digest('物质安全'),
                      'option_hashes': [digest('正确处置'), digest('错误处置')],
                      'answer_hashes': [digest('正确处置')], 'pdf_page': 1}
        self.question = {'title': '物质安全？', 'type': '单选题',
                         'options': ['A、错误处置', 'B、正确处置']}

    def tearDown(self):
        self.temp.cleanup()

    def references(self, entries=None):
        self.path.write_text(json.dumps({'source': 'fixture', 'entries': entries or [self.entry]}))
        return ReferenceAnswers(self.path)

    def test_reordered_options_choose_content_instead_of_letter(self):
        self.assertEqual(self.references().match(self.question)['indices'], [1])

    def test_one_changed_option_or_question_is_not_a_match(self):
        changed = {**self.question, 'options': ['A、错误处置', 'B、其他处置']}
        self.assertIsNone(self.references().match(changed))
        self.assertIsNone(self.references().match({**self.question, 'title': '其他物质安全'}))

    def test_conflicting_reference_answers_stop_matching(self):
        conflict = {**self.entry, 'answer_hashes': [digest('错误处置')]}
        self.assertIsNone(self.references([self.entry, conflict]).match(self.question))

    def test_numeric_sign_decimal_inequality_and_units_are_preserved(self):
        for a,b in [('4℃','-4℃'), ('1.0mg','10mg'), ('<5V','>5V'), ('5V','5mV'), ('0.5%','5%')]:
            self.assertNotEqual(digest(a), digest(b))

    def test_already_answered_and_duplicate_options_are_skipped(self):
        self.assertIsNone(self.references().match({**self.question, 'answered': True}))
        self.assertIsNone(self.references().match({**self.question, 'options': ['A、正确处置', 'B、正确处置']}))


if __name__ == '__main__':
    unittest.main()
