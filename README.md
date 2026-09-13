<p align="center">
  <img src="VoxText%20Cover.png" alt="VoxText cover" width="100%">
</p>

# VoxText

VoxText is a multilingual Telegram text-to-speech bot powered by ElevenLabs. It converts text and documents into natural-sounding MP3 audio.

The bot speaks the source language and does not translate. ElevenLabs detects Russian, English, and Italian automatically.

## Demo

Watch VoxText convert text and documents into speech:

https://github.com/user-attachments/assets/c2edaadb-0837-481b-a50e-4f86d19e9785

## Features

* Converts short and long texts into MP3 audio
* Supports Russian, English, and Italian
* Accepts TXT, DOCX, and PDF documents
* Lets users choose a voice and remember the selection
* Lets users set speech speed (`0.85×`, `1.0×`, `1.15×`)
* Splits long texts, generates each fragment in order, and merges the MP3 files
* Applies daily usage limits and exposes the `/limit` command
* Tracks users, requests, characters, and estimated API usage in SQLite
* Provides administrator statistics, CSV exports, database backups, and logs through Telegram

## Supported input formats

| Input | Output |
| --------------------- | ------ |
| Plain text | MP3 |
| TXT | MP3 |
| DOCX | MP3 |
| PDF with a text layer | MP3 |

Scanned PDF documents and text inside images are not supported. Only PDFs with an extractable text layer are accepted. Password-protected PDFs are rejected.

## Supported languages

* Russian
* English
* Italian

The multilingual ElevenLabs model processes text in the language in which it was written.

## Daily usage limits

Regular users have two simultaneous daily limits:

* 5 speech requests per user per day
* 20,000 characters per user per day

The day is calculated in UTC and resets at `00:00 UTC`.

One user action counts as one request: sending short text, confirming a long-text draft, or confirming a TXT, DOCX, or PDF. If the text is split into several ElevenLabs fragments, it still counts as a single request. Characters are counted on the full prepared text before splitting.

Use `/limit` to see how many requests and characters have been used today and how many remain.

Administrators listed in `ADMIN_IDS` are not limited. For them, `/limit` reports that daily limits do not apply.

## Admin capabilities

Telegram IDs in `ADMIN_IDS` can open `/admin` and `/stats`. These commands are registered only for admin chats and do not appear in the public bot menu.

Administrators can receive:

* overview and period statistics, including users who reached today's limits
* user and request CSV reports (`utf-8-sig`, semicolon delimiter, Excel formula protection)
* a ZIP of every database table as a separate CSV for Excel
* a SQLite snapshot created with the Backup API and checked with `PRAGMA integrity_check`
* a redacted copy of the current log and a ZIP of rotated logs

The SQLite ZIP is a full backup for restore. The CSV ZIP is for reading data in Excel, not for restoring the database. Temporary export files live under `temp/admin_exports/<job_id>/` and are deleted after send or failure. Telegram file size is limited to 49 MB; larger exports are split into regular ZIP archives.

If `ADMIN_IDS` is empty, the bot still starts, but the admin panel stays disabled.

## Technology stack

* Python 3.12
* aiogram
* ElevenLabs API
* SQLite via aiosqlite
* FFmpeg through `imageio-ffmpeg` (no separate Windows install required)
* python-docx and charset-normalizer for DOCX and TXT
* pypdf for PDF text-layer extraction
* pydub for MP3 merging

## Installation

Clone the repository:

```bash
git clone https://github.com/ericavettorello/voxtext-bot.git
cd voxtext-bot
```

Create and activate a virtual environment on Windows:

```bash
python -m venv venv
venv\Scripts\activate
```

Install the dependencies:

```bash
pip install -r requirements.txt
```

## Environment configuration

Copy `.env.example` to `.env` and fill in your values. Do not commit `.env`.

```env
TELEGRAM_BOT_TOKEN=
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
ADMIN_IDS=
ELEVENLABS_PLAN_PRICE_USD=
ELEVENLABS_PLAN_CREDITS=
DAILY_REQUEST_LIMIT=5
DAILY_CHARACTER_LIMIT=20000
DAILY_LIMIT_TIMEZONE=UTC
```

Required: `TELEGRAM_BOT_TOKEN`, `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID`.

Optional voices: set `ELEVENLABS_FEMALE_VOICE_ID` and `ELEVENLABS_MALE_VOICE_ID` to show extra voice buttons. Empty IDs hide those options.

Optional plan fields `ELEVENLABS_PLAN_PRICE_USD` and `ELEVENLABS_PLAN_CREDITS` enable an estimated cost share. This is not the actual ElevenLabs invoice.

Other optional settings and defaults:

* `DATABASE_PATH=database/voxtext.db`
* `TTS_CHUNK_SIZE=4500`
* `MAX_LONG_TEXT_CHARS=30000`
* `TTS_CHUNK_PAUSE_MS=150`
* `MAX_UPLOAD_FILE_MB=10`
* `MAX_DOCX_UNCOMPRESSED_MB=50`
* `MAX_PDF_PAGES=100`
* `MAX_PDF_CONTENT_STREAM_MB=25`

Invalid daily-limit values stop startup. After changing `.env`, restart the bot completely.

On the ElevenLabs free plan, Voice Library voices cannot be used through the API. Use a voice from your own account or a paid plan.

## Running the bot

```bash
python main.py
```

If a required variable is missing, the process exits with a configuration error and does not start Telegram polling.

Run automated tests without calling Telegram or ElevenLabs:

```bash
python -m unittest discover -s tests
```

The tests use a temporary SQLite database and mock the APIs. They do not spend credits or touch the working database.

## Security

Never publish `.env`, API keys, Telegram tokens, or real administrator IDs.

Logs omit user text, document contents, tokens, and API keys. Voice buttons send short keys such as `default`, not the full Voice ID.

Temporary audio and uploads are stored under `temp/<job_id>/` and removed after success, cancel, or error. Database files, logs, generated audio, and the virtual environment must stay out of Git.

## Project status

The core product is implemented: short and long text, TXT/DOCX/PDF with a text layer, voices, speed, daily limits, SQLite accounting, and the admin panel.

Future work may include OCR for scanned PDFs and image text, extra languages, and a deployment setup for continuous operation.
