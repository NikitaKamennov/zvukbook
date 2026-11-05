"""Synthetic regressions for the reported speech preparation failure.

No model, OCR, server, runtime or personal library is used. Run only explicitly;
the Windows build does not run these tests.
"""
import unittest

from book_renderer.speech import (TextSynthesisError, normalize_ocr_letters,
                                  prepare_text, synthesize_text, validate_speech_text)


class SpeechPreparationTests(unittest.TestCase):
    def test_uppercase_numbered_abbreviation_reaches_generator_as_russian_words(self):
        calls = []
        sentinel = object()
        def generate(text):
            calls.append(text)
            return sentinel
        audio, fallback = synthesize_text(generate, "НБВ1.")
        self.assertIs(audio, sentinel)
        self.assertFalse(fallback)
        self.assertEqual(calls, ["эн бэ вэ один."])

    def test_all_supported_uppercase_consonants_have_letter_names(self):
        # Exercises the full alphabet accepted by the abbreviation parser,
        # including the case mismatch that previously raised KeyError('Н').
        self.assertEqual(prepare_text("БВ ГД ЖЗ ЙК ЛМ НП РС ТФ ХЦ ЧШ ЩН"),
                         "бэ вэ гэ дэ жэ зэ й ка эль эм эн пэ эр эс тэ эф ха цэ че ша ща эн")

    def test_mixed_ocr_abbreviation_is_repaired_before_number_and_letter_expansion(self):
        # Latin H and B look like Cyrillic Н and В, with a Cyrillic Б between.
        self.assertEqual(prepare_text("HБB1."), "эн бэ вэ один.")

    def test_mixed_ocr_words_are_repaired_without_rewriting_latin_formulas(self):
        self.assertEqual(normalize_ocr_letters("Мир cостоит из aтомов. CO2, H2O, часть IV."),
                         "Мир состоит из атомов. CO2, H2O, часть IV.")

    def test_cyrillic_en_is_accepted_and_latin_h_is_reported_with_codepoint(self):
        validate_speech_text("Н")
        with self.assertRaisesRegex(TextSynthesisError, r"U\+0048 LATIN CAPITAL LETTER H"):
            validate_speech_text("H")

    def test_real_numbers_and_punctuation_reach_generator_as_valid_speech(self):
        prepared = prepare_text("«13,8 млрд лет», гелий-4; HБВ1.")
        self.assertIn("тринадцать целых восемь десятых миллиарда", prepared)
        self.assertRegex(prepared, r"гелий\s*-\s*четыре")
        self.assertIn("эн бэ вэ один", prepared)
        validate_speech_text(prepared)


if __name__ == "__main__":
    unittest.main()
