"""Блокировка параллельных экспортов одного администратора."""

from __future__ import annotations


class AdminExportLock:
    """Один активный экспорт на администратора. Каталоги заданий изолированы."""

    def __init__(self) -> None:
        self._user_ids: set[int] = set()

    def try_acquire(self, user_id: int) -> bool:
        if user_id in self._user_ids:
            return False
        self._user_ids.add(user_id)
        return True

    def release(self, user_id: int) -> None:
        self._user_ids.discard(user_id)

    def __contains__(self, user_id: int) -> bool:
        return user_id in self._user_ids


admin_export_lock = AdminExportLock()
