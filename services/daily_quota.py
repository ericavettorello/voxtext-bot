"""Ежедневные лимиты озвучивания: дата UTC, сообщения и учёт резерва."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from logging_config import format_log_event
from services.admin_access import is_admin
from texts import (
    DAILY_BOTH_EXHAUSTED,
    DAILY_CHARACTERS_EXHAUSTED,
    DAILY_LIMIT_STATUS,
    DAILY_REMAINING_BLOCK,
    DAILY_REQUESTS_EXHAUSTED,
    DAILY_UNLIMITED_STATUS,
)

logger = logging.getLogger(__name__)

DEFAULT_DAILY_REQUEST_LIMIT = 5
DEFAULT_DAILY_CHARACTER_LIMIT = 20000
DEFAULT_DAILY_LIMIT_TIMEZONE = "UTC"
REASON_REQUESTS = "requests"
REASON_CHARACTERS = "characters"
REASON_BOTH = "both"
QUOTA_UNAVAILABLE = "Не удалось проверить дневной лимит. Попробуйте позже."


class DailyQuotaError(Exception):
    """Ошибка хранилища дневной квоты."""

    def __init__(self, user_message: str = QUOTA_UNAVAILABLE) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass(frozen=True)
class DailyQuotaSettings:
    request_limit: int = DEFAULT_DAILY_REQUEST_LIMIT
    character_limit: int = DEFAULT_DAILY_CHARACTER_LIMIT
    timezone_name: str = DEFAULT_DAILY_LIMIT_TIMEZONE


@dataclass(frozen=True)
class QuotaResult:
    allowed: bool
    unlimited: bool = False
    reason: str | None = None
    used_requests: int = 0
    used_characters: int = 0
    remaining_requests: int = 0
    remaining_characters: int = 0
    request_limit: int = DEFAULT_DAILY_REQUEST_LIMIT
    character_limit: int = DEFAULT_DAILY_CHARACTER_LIMIT
    usage_date: str = ""
    reset_label: str = "00:00 UTC"
    reserved: bool = False


def format_quota_int(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def reset_label(timezone_name: str) -> str:
    return f"00:00 {timezone_name or DEFAULT_DAILY_LIMIT_TIMEZONE}"


def usage_date_today(timezone_name: str = DEFAULT_DAILY_LIMIT_TIMEZONE, now: datetime | None = None) -> str:
    """Вернуть дату учёта YYYY-MM-DD в заданном поясе."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    zone = _zoneinfo(timezone_name)
    return current.astimezone(zone).date().isoformat()


def unlimited_quota(settings: DailyQuotaSettings, now: datetime | None = None) -> QuotaResult:
    usage_date = usage_date_today(settings.timezone_name, now)
    return QuotaResult(
        allowed=True,
        unlimited=True,
        remaining_requests=settings.request_limit,
        remaining_characters=settings.character_limit,
        request_limit=settings.request_limit,
        character_limit=settings.character_limit,
        usage_date=usage_date,
        reset_label=reset_label(settings.timezone_name),
        reserved=False,
    )


def build_quota_result(
    *,
    used_requests: int,
    used_characters: int,
    settings: DailyQuotaSettings,
    usage_date: str,
    reserved: bool = False,
    allowed: bool | None = None,
    reason: str | None = None,
) -> QuotaResult:
    remaining_requests = max(0, settings.request_limit - used_requests)
    remaining_characters = max(0, settings.character_limit - used_characters)
    return QuotaResult(
        allowed=True if allowed is None else allowed,
        reason=reason,
        used_requests=used_requests,
        used_characters=used_characters,
        remaining_requests=remaining_requests,
        remaining_characters=remaining_characters,
        request_limit=settings.request_limit,
        character_limit=settings.character_limit,
        usage_date=usage_date,
        reset_label=reset_label(settings.timezone_name),
        reserved=reserved,
    )


def denial_reasons(used_requests: int, used_characters: int, extra_characters: int, settings: DailyQuotaSettings) -> str | None:
    requests_blocked = used_requests + 1 > settings.request_limit
    characters_blocked = used_characters + extra_characters > settings.character_limit
    if requests_blocked and characters_blocked:
        return REASON_BOTH
    if requests_blocked:
        return REASON_REQUESTS
    if characters_blocked:
        return REASON_CHARACTERS
    return None


def format_quota_denied(result: QuotaResult, text_length: int) -> str:
    reset = result.reset_label
    request_limit = format_quota_int(result.request_limit)
    character_limit = format_quota_int(result.character_limit)
    if result.reason == REASON_BOTH:
        return DAILY_BOTH_EXHAUSTED.format(
            request_limit=request_limit,
            text_length=format_quota_int(text_length),
            remaining_characters=format_quota_int(result.remaining_characters),
            character_limit=character_limit,
            reset_label=reset,
        )
    if result.reason == REASON_CHARACTERS:
        return DAILY_CHARACTERS_EXHAUSTED.format(
            text_length=format_quota_int(text_length),
            remaining_characters=format_quota_int(result.remaining_characters),
            character_limit=character_limit,
            reset_label=reset,
        )
    return DAILY_REQUESTS_EXHAUSTED.format(request_limit=request_limit, reset_label=reset)


def format_remaining_block(result: QuotaResult) -> str:
    return DAILY_REMAINING_BLOCK.format(
        remaining_requests=format_quota_int(result.remaining_requests),
        request_limit=format_quota_int(result.request_limit),
        remaining_characters=format_quota_int(result.remaining_characters),
        character_limit=format_quota_int(result.character_limit),
    )


def append_remaining(existing: str, result: QuotaResult | None) -> str:
    if result is None or result.unlimited:
        return existing
    return f"{existing}\n\n{format_remaining_block(result)}"


def format_limit_status(result: QuotaResult) -> str:
    if result.unlimited:
        return DAILY_UNLIMITED_STATUS
    return DAILY_LIMIT_STATUS.format(
        used_requests=format_quota_int(result.used_requests),
        request_limit=format_quota_int(result.request_limit),
        used_characters=format_quota_int(result.used_characters),
        character_limit=format_quota_int(result.character_limit),
        remaining_requests=format_quota_int(result.remaining_requests),
        remaining_characters=format_quota_int(result.remaining_characters),
        reset_label=result.reset_label,
    )


async def try_reserve_daily_quota(
    database,
    telegram_user_id: int,
    character_count: int,
    settings: DailyQuotaSettings,
    admin_ids: set[int] | None = None,
    source_type: str = "text",
    now: datetime | None = None,
) -> QuotaResult:
    """Атомарно проверить и зарезервировать дневную квоту. Администраторы не учитываются."""
    if is_admin(telegram_user_id, admin_ids):
        result = unlimited_quota(settings, now)
        logger.info(
            format_log_event(
                "daily_quota_reserved",
                telegram_user_id=telegram_user_id,
                source_type=source_type,
                unlimited="yes",
                chars=character_count,
                usage_date=result.usage_date,
            )
        )
        return result
    try:
        result = await database.try_reserve_daily_quota(
            telegram_user_id,
            character_count,
            settings,
            now=now,
        )
    except Exception as exc:
        logger.exception(
            format_log_event(
                "daily_quota_update_failed",
                telegram_user_id=telegram_user_id,
                action="reserve",
                source_type=source_type,
                chars=character_count,
                exception_class=type(exc).__name__,
            )
        )
        raise DailyQuotaError from exc
    if result.allowed:
        logger.info(
            format_log_event(
                "daily_quota_reserved",
                telegram_user_id=telegram_user_id,
                source_type=source_type,
                chars=character_count,
                used_requests=result.used_requests,
                used_characters=result.used_characters,
                remaining_requests=result.remaining_requests,
                remaining_characters=result.remaining_characters,
                usage_date=result.usage_date,
            )
        )
        return result
    logger.info(
        format_log_event(
            "daily_quota_denied",
            telegram_user_id=telegram_user_id,
            source_type=source_type,
            reason=result.reason or "unknown",
            chars=character_count,
            used_requests=result.used_requests,
            used_characters=result.used_characters,
            remaining_requests=result.remaining_requests,
            remaining_characters=result.remaining_characters,
            usage_date=result.usage_date,
        )
    )
    return result


async def get_daily_quota_status(
    database,
    telegram_user_id: int,
    settings: DailyQuotaSettings,
    admin_ids: set[int] | None = None,
    now: datetime | None = None,
) -> QuotaResult:
    if is_admin(telegram_user_id, admin_ids):
        return unlimited_quota(settings, now)
    try:
        return await database.get_daily_quota_status(telegram_user_id, settings, now=now)
    except Exception as exc:
        logger.exception(
            format_log_event(
                "daily_quota_update_failed",
                telegram_user_id=telegram_user_id,
                action="status",
                exception_class=type(exc).__name__,
            )
        )
        raise DailyQuotaError from exc


async def release_daily_quota(
    database,
    result: QuotaResult | None,
    telegram_user_id: int,
    character_count: int,
    source_type: str = "text",
) -> None:
    """Снять резерв, если платный запрос к ElevenLabs ещё не мог состояться."""
    if result is None or not result.reserved or result.unlimited:
        return
    try:
        released = await database.release_daily_quota(
            telegram_user_id,
            character_count,
            result.usage_date,
        )
        logger.info(
            format_log_event(
                "daily_quota_released",
                telegram_user_id=telegram_user_id,
                source_type=source_type,
                chars=character_count,
                used_requests=released.used_requests,
                used_characters=released.used_characters,
                usage_date=result.usage_date,
            )
        )
    except Exception as exc:
        logger.exception(
            format_log_event(
                "daily_quota_update_failed",
                telegram_user_id=telegram_user_id,
                action="release",
                source_type=source_type,
                exception_class=type(exc).__name__,
            )
        )


def log_quota_completed(result: QuotaResult | None, telegram_user_id: int, source_type: str, chars: int) -> None:
    if result is None:
        return
    logger.info(
        format_log_event(
            "daily_quota_completed",
            telegram_user_id=telegram_user_id,
            source_type=source_type,
            chars=chars,
            unlimited="yes" if result.unlimited else "no",
            used_requests=result.used_requests,
            used_characters=result.used_characters,
            remaining_requests=result.remaining_requests,
            remaining_characters=result.remaining_characters,
            usage_date=result.usage_date,
        )
    )


def _zoneinfo(timezone_name: str):
    name = (timezone_name or DEFAULT_DAILY_LIMIT_TIMEZONE).strip() or DEFAULT_DAILY_LIMIT_TIMEZONE
    if name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return timezone.utc
