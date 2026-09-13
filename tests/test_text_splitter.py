"""Тесты разделения длинного текста без ElevenLabs."""

from __future__ import annotations

import unittest

from utils.text_splitter import normalize_text, split_text_into_chunks


class TextSplitterTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self) -> None:
        chunks = split_text_into_chunks("Привет, мир!", 4500)
        self.assertEqual(chunks, ["Привет, мир!"])

    def test_splits_by_paragraphs(self) -> None:
        text = "Первый абзац.\n\nВторой абзац.\n\nТретий абзац."
        chunks = split_text_into_chunks(text, 20)
        self.assertTrue(all(len(chunk) <= 20 for chunk in chunks))
        self.assertGreaterEqual(len(chunks), 2)
        self.assertIn("Первый", "".join(chunks))
        self.assertIn("Третий", "".join(chunks))

    def test_large_paragraph_splits_by_sentences(self) -> None:
        text = "Prima frase. Seconda frase! Terza frase?"
        chunks = split_text_into_chunks(text, 18)
        self.assertTrue(all(len(chunk) <= 18 for chunk in chunks))
        self.assertGreater(len(chunks), 1)

    def test_long_sentence_splits_by_words(self) -> None:
        text = "alpha bravo charlie delta echo foxtrot"
        chunks = split_text_into_chunks(text, 12)
        self.assertTrue(all(len(chunk) <= 12 for chunk in chunks))
        self.assertGreater(len(chunks), 1)
        self.assertEqual(" ".join(chunks).replace("  ", " "), normalize_text(text))

    def test_long_word_is_hard_split(self) -> None:
        word = "A" * 25
        chunks = split_text_into_chunks(word, 10)
        self.assertTrue(all(len(chunk) <= 10 for chunk in chunks))
        self.assertEqual("".join(chunks), word)

    def test_chunks_never_exceed_limit(self) -> None:
        text = "Русский. English. Italiano.\n\n" + ("слово " * 80)
        chunks = split_text_into_chunks(text, 40)
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 40 for chunk in chunks))

    def test_no_text_is_lost_and_order_is_kept(self) -> None:
        text = "Uno. Due.\n\nTre quattro cinque."
        chunks = split_text_into_chunks(text, 12)
        words_source = normalize_text(text).split()
        words_chunks = " ".join(chunks).split()
        self.assertEqual(words_chunks, words_source)

    def test_empty_after_normalize(self) -> None:
        self.assertEqual(split_text_into_chunks("   \n\n  ", 100), [])


if __name__ == "__main__":
    unittest.main()
