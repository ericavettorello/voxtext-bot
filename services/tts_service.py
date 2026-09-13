"""Подключение к TTS-провайдеру (ElevenLabs) для синтеза речи из текста."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Iterator

import httpx
from elevenlabs.client import ElevenLabs
from elevenlabs.core.api_error import ApiError
from elevenlabs.types.voice_settings import VoiceSettings

from logging_config import format_log_event, sanitize_log_value
from services.speech_speed import DEFAULT_SPEECH_SPEED, resolve_speech_speed

logger = logging.getLogger(__name__)

MODEL_ID = "eleven_multilingual_v2"
OUTPUT_FORMAT = "mp3_44100_128"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMP_DIR = PROJECT_ROOT / "temp"

USER_ERROR_QUOTA = "Закончился доступный лимит озвучивания."
USER_ERROR_AUTH = "Не удалось авторизоваться в сервисе озвучивания. Проверьте API-ключ."
USER_ERROR_VOICE = "Выбранный голос не найден. Проверьте Voice ID."
USER_ERROR_FORBIDDEN = "API-ключ не имеет доступа к функции Text to Speech."
USER_ERROR_RATE_LIMIT = "Сервис временно ограничил частоту запросов. Попробуйте позднее."
USER_ERROR_UNAVAILABLE = "Сервис озвучивания временно недоступен. Попробуйте позднее."
USER_ERROR_FREE_TIER = (
    "Бесплатный аккаунт ElevenLabs ограничен. "
    "Этот голос нельзя использовать через API на текущем тарифе. "
    "Выберите собственный голос или измените тариф."
)
USER_ERROR_UNKNOWN = "Не удалось создать аудио. Подробности записаны в журнал."
USER_ERROR_GENERIC = USER_ERROR_UNKNOWN


class TTSError(Exception):
    """Ошибка синтеза речи, безопасная для показа пользователю."""

    def __init__(self, user_message: str = USER_ERROR_GENERIC) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class TTSQuotaError(TTSError):
    """Исчерпан лимит ElevenLabs."""

    def __init__(self) -> None:
        super().__init__(USER_ERROR_QUOTA)


class TTSService:
    """Синхронный клиент ElevenLabs для преобразования текста в MP3."""

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        client: Any | None = None,
    ) -> None:
        self._voice_id = voice_id
        self._client = client or ElevenLabs(api_key=api_key)

    @property
    def voice_id(self) -> str:
        return self._voice_id

    def generate_speech(
        self,
        text: str,
        output_path: Path,
        voice_id: str | None = None,
        speech_speed: float | None = None,
        previous_text: str | None = None,
        next_text: str | None = None,
    ) -> Path:
        """Сгенерировать MP3 из исходного текста и сохранить его в output_path."""
        if not text.strip():
            raise TTSError("Текст для озвучивания пустой.")

        used_voice_id = voice_id or self._voice_id
        used_speed = resolve_speech_speed(
            speech_speed if speech_speed is not None else DEFAULT_SPEECH_SPEED
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        bytes_written = 0

        convert_kwargs: dict[str, Any] = {
            "voice_id": used_voice_id,
            "text": text,
            "model_id": MODEL_ID,
            "output_format": OUTPUT_FORMAT,
            "voice_settings": VoiceSettings(speed=used_speed),
        }
        if previous_text:
            convert_kwargs["previous_text"] = previous_text[-200:]
        if next_text:
            convert_kwargs["next_text"] = next_text[:200]

        try:
            audio_stream = self._client.text_to_speech.convert(**convert_kwargs)
            with output_path.open("wb") as audio_file:
                for chunk in _iter_audio_chunks(audio_stream):
                    if chunk:
                        audio_file.write(chunk)
                        bytes_written += len(chunk)
        except TTSError:
            raise
        except ApiError as exc:
            log_elevenlabs_error(exc)
            raise _map_api_error(exc) from exc
        except httpx.TimeoutException as exc:
            log_elevenlabs_error(exc)
            raise TTSError(USER_ERROR_UNAVAILABLE) from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            log_elevenlabs_error(exc)
            raise TTSError(USER_ERROR_UNAVAILABLE) from exc
        except OSError as exc:
            log_elevenlabs_error(exc)
            raise TTSError(USER_ERROR_UNKNOWN) from exc
        except Exception as exc:
            log_elevenlabs_error(exc)
            raise TTSError(USER_ERROR_UNKNOWN) from exc

        if bytes_written == 0 or not output_path.exists() or output_path.stat().st_size == 0:
            raise TTSError(USER_ERROR_UNKNOWN)

        return output_path


def create_temp_mp3_path(temp_dir: Path | None = None) -> Path:
    """Создать уникальный путь temp/tts_<uuid>.mp3 без данных пользователя."""
    directory = temp_dir or TEMP_DIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"tts_{uuid.uuid4()}.mp3"


def delete_temp_file(path: Path | None, request_id: str | None = None) -> None:
    """Удалить только указанный временный файл. Ошибка удаления не пробрасывается."""
    if path is None:
        return
    filename = sanitize_log_value(path.name)
    try:
        if path.is_file():
            path.unlink()
            logger.info(
                format_log_event(
                    "temp_file_deleted",
                    request_id=request_id,
                    filename=filename,
                )
            )
    except OSError as exc:
        logger.warning(
            format_log_event(
                "temp_file_delete_failed",
                request_id=request_id,
                filename=filename,
                exception_class=type(exc).__name__,
            )
        )


def _iter_audio_chunks(audio_stream: Any) -> Iterator[bytes]:
    if audio_stream is None:
        return
    if isinstance(audio_stream, (bytes, bytearray)):
        yield bytes(audio_stream)
        return
    for chunk in audio_stream:
        if chunk:
            yield chunk


def extract_error_info(exc: BaseException) -> dict[str, Any]:
    """Безопасно извлечь код и сообщение ElevenLabs без секретов."""
    source = _find_api_error(exc) or exc
    info: dict[str, Any] = {
        "exception_class": type(source).__name__,
        "status_code": getattr(source, "status_code", None),
        "error_code": None,
        "error_message": None,
        "request_id": None,
    }

    body = getattr(source, "body", None)
    if body is None:
        response = getattr(source, "response", None)
        if response is not None:
            info["status_code"] = info["status_code"] or getattr(response, "status_code", None)
            body = getattr(response, "text", None)
            try:
                body = response.json()
            except Exception:
                pass

    detail = getattr(source, "detail", None)
    parsed_code, parsed_message = _parse_error_payload(body if body is not None else detail)
    info["error_code"] = parsed_code
    info["error_message"] = parsed_message

    headers = getattr(source, "headers", None)
    response = getattr(source, "response", None)
    if headers is None and response is not None:
        headers = getattr(response, "headers", None)
    info["request_id"] = _extract_request_id(headers)
    return info


def _find_api_error(exc: BaseException) -> BaseException | None:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ApiError) or hasattr(current, "status_code"):
            return current
        current = current.__cause__ or current.__context__
    return None


def log_elevenlabs_error(exc: BaseException) -> None:
    """Записать безопасную диагностику ошибки ElevenLabs."""
    details = describe_tts_failure(exc)
    logger.error(
        format_log_event(
            "tts_failed",
            exception_class=details["exception_class"],
            status=details["status_code"],
            error_code=details["error_code"],
            error_kind=details["error_kind"],
            error_message=details["error_message"],
        )
    )


def describe_tts_failure(exc: BaseException) -> dict[str, Any]:
    """Собрать безопасные поля ошибки для журнала."""
    info = extract_error_info(exc)
    error_code = info["error_code"]
    error_kind = _error_kind(exc, info)
    return {
        "exception_class": info["exception_class"],
        "status_code": info["status_code"],
        "error_code": error_code,
        "error_kind": error_kind,
        "error_message": sanitize_log_value(info["error_message"]),
    }


def _error_kind(exc: BaseException, info: dict[str, Any]) -> str:
    code = (info.get("error_code") or "").strip().lower()
    status = info.get("status_code")
    message = (info.get("error_message") or "").lower()

    if code == "quota_exceeded" or isinstance(exc, TTSQuotaError):
        return "quota_exceeded"
    if code in {"unusual_activity", "detected_unusual_activity"}:
        return "unusual_activity"
    if code == "payment_required" or _is_free_tier_restriction(code, message):
        return "payment_required"
    if code == "invalid_api_key" or status == 401:
        return "invalid_api_key"
    if code == "voice_not_found":
        return "voice_not_found"
    if code == "rate_limit_exceeded" or status == 429:
        return "rate_limit_exceeded"
    if status == 403 or _is_missing_access(code, message):
        return "missing_permissions"
    if _is_timeout(exc):
        return "timeout"
    if _is_network_error(exc):
        return "network_error"
    if code:
        return sanitize_log_value(code)
    return "unknown"


def _is_timeout(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.TimeoutException) or "timeout" in type(current).__name__.lower():
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_network_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (httpx.ConnectError, httpx.NetworkError)):
            return True
        current = current.__cause__ or current.__context__
    return False


def classify_elevenlabs_error(
    status_code: int | None,
    error_code: str | None,
    error_message: str | None,
) -> TTSError:
    """Сопоставить ответ ElevenLabs с пользовательским сообщением."""
    code = (error_code or "").strip().lower()
    message = (error_message or "").lower()

    if code == "quota_exceeded":
        return TTSQuotaError()
    if _is_free_tier_restriction(code, message):
        return TTSError(USER_ERROR_FREE_TIER)
    if code == "invalid_api_key" or status_code == 401:
        return TTSError(USER_ERROR_AUTH)
    if code == "voice_not_found":
        return TTSError(USER_ERROR_VOICE)
    if status_code == 403 or _is_missing_access(code, message):
        return TTSError(USER_ERROR_FORBIDDEN)
    if code == "rate_limit_exceeded" or status_code == 429:
        return TTSError(USER_ERROR_RATE_LIMIT)
    return TTSError(USER_ERROR_UNKNOWN)


def _map_api_error(exc: ApiError) -> TTSError:
    info = extract_error_info(exc)
    return classify_elevenlabs_error(
        info["status_code"],
        info["error_code"],
        info["error_message"],
    )


def _is_free_tier_restriction(error_code: str, message: str) -> bool:
    if error_code in {"unusual_activity", "detected_unusual_activity"}:
        return True
    free_tier_markers = (
        "free user",
        "free users",
        "free tier",
        "library voice",
        "library voices",
        "cannot use library",
    )
    return any(marker in message for marker in free_tier_markers)


def _is_missing_access(error_code: str, message: str) -> bool:
    return error_code in {"missing_permissions", "forbidden"} or (
        "does not have access" in message or "missing permissions" in message
    )


def _parse_error_payload(payload: Any) -> tuple[str | None, str | None]:
    if payload is None:
        return None, None
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            code = (
                detail.get("status")
                or detail.get("code")
                or detail.get("error")
                or payload.get("status")
                or payload.get("code")
            )
            message = detail.get("message") or detail.get("msg") or payload.get("message")
            return _as_optional_str(code), _as_optional_str(message)
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, dict):
                return _as_optional_str(first.get("type") or first.get("code")), _as_optional_str(
                    first.get("msg") or first.get("message")
                )
            return None, _as_optional_str(detail)
        return _as_optional_str(payload.get("status") or payload.get("code")), _as_optional_str(
            payload.get("message") or detail
        )
    return None, _as_optional_str(payload)


def _extract_request_id(headers: Any) -> str | None:
    if not headers:
        return None
    blocked = {"authorization", "xi-api-key", "x-api-key", "api-key", "cookie"}
    try:
        items = headers.items()
    except Exception:
        return None
    for key, value in items:
        key_text = str(key).lower()
        if key_text in blocked:
            continue
        if key_text in {"request-id", "x-request-id", "xi-request-id"}:
            return _as_optional_str(value)
    return None


def _as_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


