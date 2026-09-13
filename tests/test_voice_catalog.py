"""Тесты каталога голосов без обращения к ElevenLabs."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from services.voice_catalog import VoiceCatalog, VoiceOption, build_voice_catalog


def _config(**overrides: object) -> SimpleNamespace:
    values = {
        "elevenlabs_voice_id": "voice-default",
        "elevenlabs_default_voice_name": "Основной голос",
        "elevenlabs_female_voice_id": None,
        "elevenlabs_female_voice_name": "Женский голос",
        "elevenlabs_male_voice_id": None,
        "elevenlabs_male_voice_name": "Мужской голос",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class VoiceCatalogTests(unittest.TestCase):
    def test_single_default_voice(self) -> None:
        catalog = build_voice_catalog(_config())
        self.assertEqual([voice.key for voice in catalog.voices], ["default"])
        self.assertEqual(catalog.get_display_name("default"), "Основной голос")
        self.assertEqual(catalog.get_voice_id("default"), "voice-default")

    def test_multiple_voices(self) -> None:
        catalog = build_voice_catalog(
            _config(
                elevenlabs_female_voice_id="voice-female",
                elevenlabs_male_voice_id="voice-male",
            )
        )
        self.assertEqual([voice.key for voice in catalog.voices], ["default", "female", "male"])
        self.assertEqual(catalog.get_display_name("female"), "Женский голос")
        self.assertEqual(catalog.get_voice_id("male"), "voice-male")

    def test_duplicate_voice_ids_are_skipped(self) -> None:
        catalog = build_voice_catalog(
            _config(
                elevenlabs_female_voice_id="voice-default",
                elevenlabs_male_voice_id="voice-male",
            )
        )
        self.assertEqual([voice.key for voice in catalog.voices], ["default", "male"])
        self.assertIsNone(catalog.get("female"))

    def test_arbitrary_voice_key_is_rejected(self) -> None:
        catalog = build_voice_catalog(_config(elevenlabs_female_voice_id="voice-female"))
        self.assertFalse(catalog.is_allowed_key("other"))
        self.assertIsNone(catalog.get("other"))
        self.assertIsNone(catalog.get_voice_id("injected-id"))
        self.assertEqual(catalog.resolve("injected-id").key, "default")

    def test_empty_optional_ids_are_ignored(self) -> None:
        catalog = build_voice_catalog(
            _config(
                elevenlabs_female_voice_id="",
                elevenlabs_male_voice_id="",
            )
        )
        self.assertEqual(len(catalog.voices), 1)

    def test_resolve_falls_back_to_default(self) -> None:
        catalog = VoiceCatalog(
            [
                VoiceOption("default", "Основной голос", "voice-default", None),
                VoiceOption("female", "Женский голос", "voice-female", "female"),
            ]
        )
        self.assertEqual(catalog.resolve("female").voice_id, "voice-female")
        self.assertEqual(catalog.resolve("missing").key, "default")
        self.assertEqual(catalog.resolve(None).key, "default")


if __name__ == "__main__":
    unittest.main()
