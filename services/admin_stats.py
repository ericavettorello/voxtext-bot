"""Периоды и форматирование административной статистики. Даты сравниваются в UTC."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

PERIOD_TODAY = "today"
PERIOD_7D = "7d"
PERIOD_30D = "30d"
PERIOD_ALL = "all"
ALLOWED_PERIODS = frozenset({PERIOD_TODAY, PERIOD_7D, PERIOD_30D, PERIOD_ALL})

PERIOD_LABELS = {
    PERIOD_TODAY: "Сегодня",
    PERIOD_7D: "Последние 7 дней",
    PERIOD_30D: "Последние 30 дней",
    PERIOD_ALL: "За всё время",
}

PERIOD_FILE_LABELS = {
    PERIOD_TODAY: "today",
    PERIOD_7D: "7d",
    PERIOD_30D: "30d",
    PERIOD_ALL: "all",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def period_start(period: str, now: datetime | None = None) -> datetime | None:
    """Вернуть нижнюю границу периода в UTC или None для «всего времени»."""
    if period not in ALLOWED_PERIODS:
        raise ValueError("period")
    current = now or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if period == PERIOD_ALL:
        return None
    if period == PERIOD_TODAY:
        return current.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == PERIOD_7D:
        return current - timedelta(days=7)
    return current - timedelta(days=30)


def period_start_iso(period: str, now: datetime | None = None) -> str | None:
    start = period_start(period, now)
    return None if start is None else start.isoformat()


def format_int(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


def format_cost(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return f"${format(value, 'f')}"


def format_db_datetime(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).replace("T", " ")
    if text.endswith("+00:00"):
        text = text[:-6] + " UTC"
    elif text.endswith("Z"):
        text = text[:-1] + " UTC"
    return text


def format_overview(stats: dict[str, Any]) -> str:
    lines = [
        "📊 Статистика VoxText",
        "",
        "Пользователи:",
        f"• всего: {format_int(stats['users_total'])}",
        f"• новых сегодня: {format_int(stats['new_users_today'])}",
        f"• новых за 7 дней: {format_int(stats['new_users_7d'])}",
        f"• новых за 30 дней: {format_int(stats['new_users_30d'])}",
        f"• активных за 24 часа: {format_int(stats['active_users_24h'])}",
        f"• активных за 7 дней: {format_int(stats['active_users_7d'])}",
        f"• активных за 30 дней: {format_int(stats['active_users_30d'])}",
        "",
        "Озвучивание:",
        f"• всего заданий: {format_int(stats['requests_total'])}",
        f"• успешно: {format_int(stats['success_total'])}",
        f"• с ошибкой: {format_int(stats['failed_total'])}",
        f"• отменено: {format_int(stats['cancelled_total'])}",
        f"• обработано символов: {format_int(stats['characters_total'])}",
        f"• ориентировочно использовано кредитов: {format_int(stats['estimated_credits'])}",
    ]
    cost = format_cost(stats.get("estimated_cost_usd"))
    if cost is not None:
        lines.append(f"• ориентировочная стоимость: {cost}")
    lines.extend(
        [
            "",
            "Источники:",
            f"• обычный текст: {format_int(stats['source_text'])}",
            f"• длинный текст: {format_int(stats['source_long_text'])}",
            f"• TXT: {format_int(stats['source_txt'])}",
            f"• DOCX: {format_int(stats['source_docx'])}",
            f"• PDF: {format_int(stats['source_pdf'])}",
        ]
    )
    if stats.get("popular_voice_name"):
        lines.append("")
        lines.append(f"Популярный голос: {stats['popular_voice_name']}")
    if stats.get("popular_speed") is not None:
        speed = stats["popular_speed"]
        lines.append(f"Популярная скорость: {speed}×")
    if "daily_quota_requests_today" in stats:
        lines.extend(
            [
                "",
                "Дневные лимиты (сегодня):",
                f"• пользователей с лимитом озвучиваний: {format_int(stats.get('daily_quota_users_at_request_limit', 0))}",
                f"• пользователей с лимитом символов: {format_int(stats.get('daily_quota_users_at_character_limit', 0))}",
                f"• учтённых запросов: {format_int(stats.get('daily_quota_requests_today', 0))}",
                f"• учтённых символов: {format_int(stats.get('daily_quota_characters_today', 0))}",
            ]
        )
    return "\n".join(lines)


def format_period_stats(stats: dict[str, Any], period: str) -> str:
    label = PERIOD_LABELS.get(period, period)
    lines = [
        f"📅 Статистика: {label}",
        "",
        f"Новых пользователей: {format_int(stats['new_users'])}",
        f"Активных пользователей: {format_int(stats['active_users'])}",
        f"Всего заданий: {format_int(stats['requests_total'])}",
        f"Успешно: {format_int(stats['success_total'])}",
        f"С ошибкой: {format_int(stats['failed_total'])}",
        f"Символов: {format_int(stats['characters_total'])}",
        f"Ориентировочные кредиты: {format_int(stats['estimated_credits'])}",
    ]
    cost = format_cost(stats.get("estimated_cost_usd"))
    if cost is not None:
        lines.append(f"Ориентировочная стоимость: {cost}")
    lines.extend(
        [
            "",
            "Источники:",
            f"• обычный текст: {format_int(stats['source_text'])}",
            f"• длинный текст: {format_int(stats['source_long_text'])}",
            f"• TXT: {format_int(stats['source_txt'])}",
            f"• DOCX: {format_int(stats['source_docx'])}",
            f"• PDF: {format_int(stats['source_pdf'])}",
        ]
    )
    return "\n".join(lines)
