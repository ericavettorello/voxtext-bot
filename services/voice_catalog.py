"""Каталог голосов из переменных окружения. В callback_data только короткие ключи."""

from __future__ import annotations

from dataclasses import dataclass

from config import Config


SAFE_VOICE_KEYS = frozenset({"default", "female", "male"})


@dataclass(frozen=True)
class VoiceOption:
    key: str
    display_name: str
    voice_id: str
    gender: str | None
    credit_multiplier: float = 1.0


class VoiceCatalog:
    """Доступные голоса: основной всегда, женский и мужской — только при наличии ID."""

    def __init__(self, voices: list[VoiceOption]) -> None:
        if not voices:
            raise ValueError("Каталог голосов не может быть пустым.")
        self._voices = list(voices)
        self._by_key = {voice.key: voice for voice in self._voices}

    @property
    def voices(self) -> list[VoiceOption]:
        return list(self._voices)

    @property
    def default(self) -> VoiceOption:
        return self._by_key["default"]

    def get(self, voice_key: str) -> VoiceOption | None:
        if voice_key not in SAFE_VOICE_KEYS:
            return None
        return self._by_key.get(voice_key)

    def resolve(self, voice_key: str | None) -> VoiceOption:
        """Вернуть голос по ключу или основной, если ключ недопустим."""
        voice = self.get(voice_key or "")
        if voice is not None:
            return voice
        return self.default

    def get_voice_id(self, voice_key: str) -> str | None:
        voice = self.get(voice_key)
        return None if voice is None else voice.voice_id

    def get_display_name(self, voice_key: str) -> str | None:
        voice = self.get(voice_key)
        return None if voice is None else voice.display_name

    def is_allowed_key(self, voice_key: str) -> bool:
        return voice_key in self._by_key


def build_voice_catalog(config: Config) -> VoiceCatalog:
    """Собрать каталог из конфигурации. Дубликаты Voice ID отбрасываются."""
    voices: list[VoiceOption] = []
    seen_ids: set[str] = set()

    candidates = [
        VoiceOption(
            key="default",
            display_name=config.elevenlabs_default_voice_name,
            voice_id=config.elevenlabs_voice_id,
            gender=None,
        ),
        VoiceOption(
            key="female",
            display_name=config.elevenlabs_female_voice_name,
            voice_id=config.elevenlabs_female_voice_id or "",
            gender="female",
        ),
        VoiceOption(
            key="male",
            display_name=config.elevenlabs_male_voice_name,
            voice_id=config.elevenlabs_male_voice_id or "",
            gender="male",
        ),
    ]

    for candidate in candidates:
        if candidate.key != "default" and not candidate.voice_id:
            continue
        if candidate.voice_id in seen_ids:
            continue
        seen_ids.add(candidate.voice_id)
        voices.append(candidate)

    return VoiceCatalog(voices)
