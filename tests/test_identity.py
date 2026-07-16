from __future__ import annotations

import unittest

from recruit_crawler.identity import normalize_scalar


class TextNormalizationTests(unittest.TestCase):
    def test_normalize_scalar_decodes_single_visible_punctuation_escape(self) -> None:
        self.assertEqual(
            normalize_scalar(r"\[주니어\] 데이터 엔지니어 \(Growth Data Engineer\)\, 서울"),
            "[주니어] 데이터 엔지니어 (Growth Data Engineer), 서울",
        )
        self.assertEqual(normalize_scalar(r"페이타랩\(패스오더\)"), "페이타랩(패스오더)")

    def test_normalize_scalar_preserves_doubled_escape_introducer(self) -> None:
        self.assertEqual(normalize_scalar(r"\\[주니어\\]"), r"\[주니어\]")


if __name__ == "__main__":
    unittest.main()
