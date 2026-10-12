import unittest
from asr_comparison_metrics import edits, normalize, score


class ScoringTests(unittest.TestCase):
    def test_numeric_and_spoken_file_notation_equivalence(self):
        self.assertEqual(normalize('八零八零，一八七六七，三十秒'), normalize('8080,18767,30秒'))
        self.assertEqual(normalize('八千零八十'), '8080')
        self.assertEqual(normalize('main 点 py，README 点 md'), normalize('main.py README.md'))
        self.assertEqual(normalize('main点py，README点md'), normalize('main.py README.md'))

    def test_substitution_deletion_and_insertion_are_counted(self):
        self.assertEqual(edits('当前', '浪天'), (2, 2, 0, 0))
        self.assertEqual(edits('当前项目', '当前'), (2, 0, 2, 0))
        self.assertEqual(edits('项目', '当前项目'), (2, 0, 0, 2))

    def test_metrics_keep_terms_and_do_not_rewrite_wrong_words(self):
        result = score('检查当前项目 Codex', '检查浪天项目 CODEX', ['Codex', 'GitHub'])
        self.assertEqual(result['edit_errors'], 2)
        self.assertEqual(result['term_matches'], {'Codex': True, 'GitHub': False})


if __name__ == '__main__':
    unittest.main()
