"""Единый каталог разрешённых скоростей речи."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_SPEECH_SPEED = 1.0


@dataclass(frozen=True)
class SpeedOption:
    key: str
    value: float
    button_label: str
    title: str

    @property
    def display(self) -> str:
        return f"{self.title} — {self.value}×"

    @property
    def confirmation(self) -> str:
        return f"Скорость озвучивания изменена: {self.display}"


SPEED_OPTIONS: tuple[SpeedOption, ...] = (
    SpeedOption(
        key="slow",
        value=0.85,
        button_label="🐢 Медленно — 0.85×",
        title="Медленно",
    ),
    SpeedOption(
        key="normal",
        value=1.0,
        button_label="▶️ Обычная — 1.0×",
        title="Обычная",
    ),
    SpeedOption(
        key="fast",
        value=1.15,
        button_label="🐇 Быстро — 1.15×",
        title="Быстро",
    ),
)

ALLOWED_SPEED_KEYS = frozenset(option.key for option in SPEED_OPTIONS)
ALLOWED_SPEED_VALUES = frozenset(option.value for option in SPEED_OPTIONS)
_OPTIONS_BY_KEY = {option.key: option for option in SPEED_OPTIONS}


def get_speed_by_key(speed_key: str) -> SpeedOption | None:
    if speed_key not in ALLOWED_SPEED_KEYS:
        return None
    return _OPTIONS_BY_KEY.get(speed_key)


def get_speed_by_value(value: float | None) -> SpeedOption:
    if value is None:
        return _OPTIONS_BY_KEY["normal"]
    for option in SPEED_OPTIONS:
        if abs(float(value) - option.value) < 0.001:
            return option
    return _OPTIONS_BY_KEY["normal"]


def resolve_speech_speed(value: float | None) -> float:
    """Вернуть разрешённую скорость или значение по умолчанию."""
    return get_speed_by_value(value).value


def is_allowed_speed(value: float) -> bool:
    return any(abs(float(value) - option.value) < 0.001 for option in SPEED_OPTIONS)
