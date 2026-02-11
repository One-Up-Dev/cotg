# COTG — Claude On The Go

Access [Claude Code](https://docs.anthropic.com/en/docs/claude-code) from anywhere, 24/7, through Telegram.

COTG is a lightweight bridge between Telegram and the Claude CLI (`claude -p`). Send a message on Telegram, get a Claude Code response — from your phone, tablet, or any device.

## Features

- **Claude Code via Telegram** — full CLI capabilities from your phone
- **Markdown formatting** — responses are converted to Telegram-compatible MarkdownV2
- **Conversation history** — messages are persisted in SQLite across sessions
- **Typing indicator** — visual feedback while Claude is thinking
- **Single-user security** — restricted to one authorized chat ID
- **Systemd service** — runs as a background daemon with auto-restart

## Architecture

```
Telegram → bot.py → claude_runner.py → claude -p → response → formatting.py → Telegram
```

| File | Role |
|------|------|
| `bot.py` | Telegram bot entry point (polling, handlers, typing indicator) |
| `claude_runner.py` | Async subprocess wrapper for `claude -p` |
| `config.py` | Configuration from environment variables |
| `formatting.py` | Markdown → Telegram format conversion |
| `.claude/hooks/` | Auto-save/load conversation history |

## Prerequisites

- Python 3.13+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Installation

```bash
git clone https://github.com/oneup/cotg.git
cd cotg

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy the example env file and fill in your values:

```bash
cp .env.example .env
```

```env
TELEGRAM_TOKEN=your-bot-token-from-botfather
TELEGRAM_CHAT_ID=your-chat-id
```

To get your chat ID, send a message to your bot and check:
```bash
curl https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```

## Usage

```bash
source .venv/bin/activate
python bot.py
```

### Run as a systemd service

Edit `telegram-bot.service` to match your paths, then:

```bash
sudo cp telegram-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-bot.service
```

Check status:
```bash
sudo systemctl status telegram-bot.service
journalctl -u telegram-bot.service -f
```

## License

MIT
