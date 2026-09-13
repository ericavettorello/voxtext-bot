<p align="center">
  <img src="voxtext-cover.png" alt="VoxText cover" width="100%">
</p>

# VoxText

VoxText is a multilingual Telegram text-to-speech bot powered by ElevenLabs. It converts text and documents into natural-sounding MP3 audio.

## Demo

Watch VoxText convert text and documents into speech:

https://github.com/user-attachments/assets/c2edaadb-0837-481b-a50e-4f86d19e9785

## Features

* Converts short and long texts into MP3 audio
* Supports Russian, English, and Italian
* Accepts TXT, DOCX, and PDF documents
* Provides multiple voice options
* Allows users to adjust speech speed
* Splits and processes long texts automatically
* Tracks users, requests, characters, and API usage
* Stores project data in SQLite
* Provides administrator statistics through Telegram
* Exports database tables to CSV
* Provides database backups and application logs

## Supported formats

| Input                 | Output |
| --------------------- | ------ |
| Plain text            | MP3    |
| TXT                   | MP3    |
| DOCX                  | MP3    |
| PDF with a text layer | MP3    |

Scanned PDF documents without a text layer are not currently supported.

## Supported languages

* Russian
* English
* Italian

The multilingual ElevenLabs model automatically processes text in the language in which it was written.

## Technology stack

* Python 3.12
* aiogram
* ElevenLabs API
* SQLite
* FFmpeg
* python-docx
* PDF text extraction

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

Create a `.env` file and add the required configuration:

```env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ELEVENLABS_API_KEY=your_elevenlabs_api_key
ELEVENLABS_VOICE_ID=your_default_voice_id
ADMIN_IDS=your_telegram_user_id
```

Never publish the `.env` file or expose API keys in the repository.

Run the bot:

```bash
python main.py
```

## Administration

Authorized administrators can receive directly through Telegram:

* general usage statistics;
* user and request reports in CSV;
* complete database export;
* SQLite database backup;
* current and archived application logs.

## Security

Secret keys and private configuration are stored in environment variables. Temporary files, logs, databases, generated audio files, and the virtual environment should not be committed to GitHub.

## Project status

The core functionality is implemented and operational. Future development may include additional languages, OCR support for scanned PDFs, personal usage limits, and deployment for continuous operation.

