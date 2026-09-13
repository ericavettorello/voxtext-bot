"""Конфигурация бота: загрузка настроек из переменных окружения."""

import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE_PATH = "database/voxtext.db"

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(ValueError):
    """Ошибка конфигурации при запуске бота."""


class Config:
    """Параметры приложения, читаемые из .env."""

    def __init__(self) -> None:
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.elevenlabs_api_key = os.getenv("ELEVENLABS_API_KEY", "").strip()
        self.elevenlabs_voice_id = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
        self.elevenlabs_default_voice_name = (
            os.getenv("ELEVENLABS_DEFAULT_VOICE_NAME", "").strip() or "Основной голос"
        )
        self.elevenlabs_female_voice_id = os.getenv("ELEVENLABS_FEMALE_VOICE_ID", "").strip() or None
        self.elevenlabs_female_voice_name = (
            os.getenv("ELEVENLABS_FEMALE_VOICE_NAME", "").strip() or "Женский голос"
        )
        self.elevenlabs_male_voice_id = os.getenv("ELEVENLABS_MALE_VOICE_ID", "").strip() or None
        self.elevenlabs_male_voice_name = (
            os.getenv("ELEVENLABS_MALE_VOICE_NAME", "").strip() or "Мужской голос"
        )
        self.elevenlabs_plan_price_usd, self.elevenlabs_plan_credits = _optional_plan(
            os.getenv("ELEVENLABS_PLAN_PRICE_USD"),
            os.getenv("ELEVENLABS_PLAN_CREDITS"),
        )
        self.database_path = resolve_database_path(os.getenv("DATABASE_PATH"))
        self.tts_chunk_size = _positive_int(os.getenv("TTS_CHUNK_SIZE"), 4500)
        self.max_long_text_chars = _positive_int(os.getenv("MAX_LONG_TEXT_CHARS"), 30000)
        self.tts_chunk_pause_ms = _non_negative_int(os.getenv("TTS_CHUNK_PAUSE_MS"), 150)
        self.max_upload_file_mb = _positive_int(os.getenv("MAX_UPLOAD_FILE_MB"), 10)
        self.max_docx_uncompressed_mb = _positive_int(os.getenv("MAX_DOCX_UNCOMPRESSED_MB"), 50)
        self.max_pdf_pages = _positive_int(os.getenv("MAX_PDF_PAGES"), 100)
        self.max_pdf_content_stream_mb = _positive_int(os.getenv("MAX_PDF_CONTENT_STREAM_MB"), 25)
        self.admin_ids = parse_admin_ids(os.getenv("ADMIN_IDS"))
        self.daily_request_limit = parse_positive_int_setting(
            os.getenv("DAILY_REQUEST_LIMIT"), 5, "DAILY_REQUEST_LIMIT"
        )
        self.daily_character_limit = parse_positive_int_setting(
            os.getenv("DAILY_CHARACTER_LIMIT"), 20000, "DAILY_CHARACTER_LIMIT"
        )
        self.daily_limit_timezone = parse_timezone_name(
            os.getenv("DAILY_LIMIT_TIMEZONE"), "UTC", "DAILY_LIMIT_TIMEZONE"
        )

        missing: list[str] = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.elevenlabs_api_key:
            missing.append("ELEVENLABS_API_KEY")
        if not self.elevenlabs_voice_id:
            missing.append("ELEVENLABS_VOICE_ID")

        if missing:
            names = ", ".join(missing)
            raise ConfigError(
                f"Отсутствуют обязательные переменные окружения: {names}. "
                "Заполните их в файле .env перед запуском."
            )


def parse_admin_ids(raw: str | None) -> set[int]:
    """Разобрать ADMIN_IDS в множество положительных Telegram ID."""
    value = (raw or "").strip()
    if not value:
        return set()
    result: set[int] = set()
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        if not item.isdigit():
            raise ConfigError(
                "Некорректное значение ADMIN_IDS. "
                "Укажите один или несколько положительных Telegram ID через запятую."
            )
        admin_id = int(item)
        if admin_id <= 0:
            raise ConfigError(
                "Некорректное значение ADMIN_IDS. "
                "Укажите один или несколько положительных Telegram ID через запятую."
            )
        result.add(admin_id)
    return result


def parse_positive_int_setting(raw: str | None, default: int, name: str) -> int:
    """Вернуть положительное целое или значение по умолчанию. Некорректное значение останавливает запуск."""
    value = (raw or "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise ConfigError(
            f"Некорректное значение {name}. Укажите положительное целое число."
        ) from None
    if parsed <= 0:
        raise ConfigError(f"Некорректное значение {name}. Укажите положительное целое число.")
    return parsed


def parse_timezone_name(raw: str | None, default: str, name: str) -> str:
    value = (raw or "").strip() or default
    if value.upper() == "UTC":
        return "UTC"
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        raise ConfigError(
            f"Некорректное значение {name}. Укажите действительный часовой пояс, например UTC."
        ) from None
    return value


def resolve_database_path(raw_path: str | None) -> Path:
    """Вернуть абсолютный путь к SQLite относительно корня проекта."""
    value = (raw_path or "").strip() or DEFAULT_DATABASE_PATH
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def _optional_plan(
    raw_price: str | None,
    raw_credits: str | None,
) -> tuple[Decimal | None, int | None]:
    """Вернуть тариф только если обе настройки заполнены корректно."""
    price = _optional_decimal(raw_price)
    credits = _optional_int(raw_credits)
    if price is None or credits is None or price <= 0 or credits <= 0:
        return None, None
    return price, credits


def _optional_decimal(raw: str | None) -> Decimal | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _positive_int(raw: str | None, default: int) -> int:
    value = _optional_int(raw)
    return default if value is None or value <= 0 else value


def _non_negative_int(raw: str | None, default: int) -> int:
    value = _optional_int(raw)
    return default if value is None or value < 0 else value


def _optional_int(raw: str | None) -> int | None:
    value = (raw or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
