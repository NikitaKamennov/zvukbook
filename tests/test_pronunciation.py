"""Synthetic rules/units regressions. No model, database, server or book files.

Not run automatically by a Windows build; execute only at the owner's request.
"""
import copy
import unittest

from book_renderer.audio import chapter_hash
from book_renderer.pronunciation import normalize_stress, profile_for, validate_rules
from book_renderer.speech import TextSynthesisError, prepare_text, synthesize_text, validate_speech_text


class PronunciationTests(unittest.TestCase):
    def test_flat_and_superscript_area_with_number_agreement(self):
        for unit in ("м2", "м²", "м^2", "кв. м"):
            with self.subTest(unit=unit):
                spoken = prepare_text(f"1 {unit}, 2 {unit}, 5 {unit}.")
                self.assertIn("один квадратный метр", spoken)
                self.assertIn("два квадратных метра", spoken)
                self.assertIn("пять квадратных метров", spoken)
        self.assertEqual(prepare_text("м2"), "квадратные метры")

    def test_distance_volume_speed_and_feminine_units(self):
        spoken = prepare_text("2 м, 5 м3; 3 км/ч, 2 мин, 1 с.")
        for reading in ("два метра", "пять кубических метров", "три километра в час", "две минуты", "одна секунда"):
            self.assertIn(reading, spoken)

    def test_decimal_units_and_date_initial_ambiguity(self):
        self.assertIn("квадратного метра", prepare_text("1,5 м²"))
        self.assertNotIn("грамм", prepare_text("В 2024 г. М. Горький"))
        self.assertNotIn("метры", prepare_text("М. Горький"))
        self.assertIn("то есть", prepare_text("т. е."))
        self.assertIn("и так далее", prepare_text("и т. д."))
        self.assertIn("и так далее. Потом", prepare_text("и т. д. Потом он ушёл."))

    def test_replacements_are_whole_literal_matches_without_cascading(self):
        profile = profile_for({"pronunciation": {"replacements": [
            {"from": "кот", "to": "пёс"}, {"from": "пёс", "to": "волк"}]}})
        self.assertEqual(profile.replace("Кот, скот и пёс."), "пёс, скот и волк.")

    def test_user_abbreviation_override_is_case_insensitive(self):
        profile = profile_for({"pronunciation": {"replacements": [{"from": "ссср", "to": "эс эс эс эр"}]}})
        self.assertEqual(profile.prepare("СССР и ссср"), "эс эс эс эр и эс эс эс эр")

    def test_supported_stress_entry_formats(self):
        for word in ("злодЕй", "ЗлодЕй", "злоде́й", "злод+ей"):
            self.assertEqual(normalize_stress(word), "злод+ей")
        self.assertEqual(normalize_stress("Оле́г"), "ол+ег")
        self.assertEqual(normalize_stress("Ол+ег"), "ол+ег")
        with self.assertRaises(ValueError):
            normalize_stress("ЗЛОДЕЙ")
        with self.assertRaises(ValueError):
            validate_rules({"stresses": ["злОдей", "злодЕй"]})

    def test_manual_stress_survives_the_synthesis_boundary(self):
        profile = profile_for({"pronunciation": {"stresses": ["злодЕй"]}})
        text = profile.prepare("Злодей пришёл.")
        calls = []
        synthesize_text(lambda value: calls.append(value), text, prepared=True)
        self.assertEqual(calls, ["Злод+ей пришёл."])
        validate_speech_text(calls[0])
        with self.assertRaises(TextSynthesisError):
            validate_speech_text("зл+од+ей")

    def test_deleting_rules_restores_source_and_only_affected_chapter_hashes(self):
        book = {"chapters": [{"index": 1, "title": "Первая", "text": "Кот уснул."},
                             {"index": 2, "title": "Вторая", "text": "Лес шумел."}]}
        original = copy.deepcopy(book)
        before = [chapter_hash(chapter, book) for chapter in book["chapters"]]
        book["pronunciation"] = {"replacements": [{"from": "кот", "to": "пёс"}]}
        after = [chapter_hash(chapter, book) for chapter in book["chapters"]]
        self.assertNotEqual(before[0], after[0])
        self.assertEqual(before[1], after[1])
        book["pronunciation"] = {"replacements": [], "stresses": []}
        self.assertEqual(before, [chapter_hash(chapter, book) for chapter in book["chapters"]])
        self.assertEqual(original["chapters"], book["chapters"])

    def test_known_markup_fallback_keeps_manual_stress(self):
        calls = []
        def generate(text):
            calls.append(text)
            if len(calls) == 1:
                raise ValueError("invalid ssml")
            return "synthetic-audio"
        audio, fallback = synthesize_text(generate, "Злод+ей — здесь.", prepared=True)
        self.assertTrue(fallback)
        self.assertEqual(audio, "synthetic-audio")
        self.assertEqual(calls[-1], "Злод+ей здесь.")


if __name__ == "__main__":
    unittest.main()
