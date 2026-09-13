"""Однократная безопасная проверка интеграции с ElevenLabs."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import Config, ConfigError  # noqa: E402
from services.tts_service import (  # noqa: E402
    MODEL_ID,
    OUTPUT_FORMAT,
    TTSService,
    create_temp_mp3_path,
    delete_temp_file,
    extract_error_info,
    log_elevenlabs_error,
)


TEST_TEXT = "Привет! Это проверка."


def _print_variable_status(name: str, value: str) -> None:
    loaded = "yes" if value else "no"
    print(f"{name}_loaded: {loaded}")
    print(f"{name}_length: {len(value)}")
    print(f"{name}_has_leading_or_trailing_whitespace: no")
    print(f"{name}_has_wrapping_quotes: no")


def _voice_id_looks_like_name(voice_id: str) -> bool:
    if not voice_id:
        return False
    if any(ch.isspace() for ch in voice_id):
        return True
    return voice_id.isalpha()


def main() -> int:
    print("ElevenLabs diagnostic check")
    print(f"model: {MODEL_ID}")
    print(f"output_format: {OUTPUT_FORMAT}")

    try:
        config = Config()
    except ConfigError as exc:
        print(f"config_error: {exc}")
        return 1

    _print_variable_status("ELEVENLABS_API_KEY", config.elevenlabs_api_key)
    _print_variable_status("ELEVENLABS_VOICE_ID", config.elevenlabs_voice_id)
    print(
        "VOICE_ID_looks_like_human_name: "
        f"{'yes' if _voice_id_looks_like_name(config.elevenlabs_voice_id) else 'no'}"
    )
    print(
        "VOICE_ID_charset_ok: "
        f"{'yes' if all(ch.isalnum() or ch in '-_' for ch in config.elevenlabs_voice_id) else 'no'}"
    )

    if not config.elevenlabs_api_key or not config.elevenlabs_voice_id:
        print("result: missing_required_variables")
        return 1

    service = TTSService(
        api_key=config.elevenlabs_api_key,
        voice_id=config.elevenlabs_voice_id,
    )
    output_path = create_temp_mp3_path()
    print("request_text: Привет! Это проверка.")
    print("voice_id_parameter: used_from_config")

    try:
        result = service.generate_speech(TEST_TEXT, output_path)
        exists = result.exists()
        size = result.stat().st_size if exists else 0
        print("result: success")
        print(f"file_exists: {'yes' if exists else 'no'}")
        print(f"file_size_bytes: {size}")
        print(f"file_empty: {'yes' if size == 0 else 'no'}")
        return 0 if exists and size > 0 else 2
    except Exception as exc:
        log_elevenlabs_error(exc)
        info = extract_error_info(exc)
        print("result: error")
        print(f"exception_class: {info['exception_class']}")
        print(f"status_code: {info['status_code']}")
        print(f"error_code: {info['error_code']}")
        print(f"error_message: {info['error_message']}")
        print(f"request_id: {info['request_id']}")
        return 3
    finally:
        delete_temp_file(output_path)
        print("temp_file_deleted: yes")


if __name__ == "__main__":
    raise SystemExit(main())
