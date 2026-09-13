"""Проверка прав администратора. ID не хранятся в коде, только в конфигурации."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from logging_config import format_log_event

logger = logging.getLogger(__name__)

ACCESS_DENIED_TEXT = "Недостаточно прав для выполнения этой команды."


def is_admin(user_id: int | None, admin_ids: set[int] | None) -> bool:
    """Проверить, входит ли Telegram user ID в настроенный список администраторов."""
    if user_id is None or not admin_ids:
        return False
    return int(user_id) in admin_ids


def log_access_denied(user_id: int | None, action: str) -> None:
    logger.info(
        format_log_event(
            "admin_access_denied",
            telegram_user_id=user_id if user_id is not None else "none",
            action=action,
            result="denied",
            at=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        )
    )
