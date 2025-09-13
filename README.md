# Open Source Discord Chatbot

A conversational Discord chatbot that uses Ollama (or an OpenAI-compatible LLM server) for inference and Redis for per-channel context.

## What this project does

- Listens in guild channels and responds when mentioned.
- Keeps per-channel message history in Redis so the bot can use prior messages as context.
- Sends long replies in chunks to stay under Discord's 2000-character limit.
- Provides a slash command `/reset` to clear a channel's context; the command is admin-only if `ADMIN_ID` is set, otherwise available to anyone.
- Supports two backends selectable via the `TYPE` setting:
  - `ollama` — uses the Ollama AsyncClient.
  - `llamacpp` / `openai` — uses an OpenAI-compatible endpoint via the openai package (pointed at `LLAMA_SERVER_URL`).
- Optional streaming support for Ollama, and a `PREDICT` option to control prediction count sent to the model.

## Quickstart

Prerequisites
- Ollama installed and running (if using the Ollama backend) or an OpenAI-compatible LLM server reachable at `LLAMA_SERVER_URL`.
- Redis server.
- Python 3.11+ (or run via Docker).

1. Clone the repository.
2. Create a `.env` (or `bot.env`) file with the required variables (see Configuration).
3. Option A — Docker:
   - Place your `.env` next to `docker-compose.yml` and run:
     - `docker-compose up -d`
4. Option B — Local:
   - Install dependencies: `pip install -r requirements.txt`
   - Run: `python discord_bot.py` (ensure `DISCORD_TOKEN` and other variables are set in the environment)

When the bot starts it will log readiness and, if available, an invite URL you can use to add the bot to your server.


## Configuration (environment variables)

- DISCORD_TOKEN (required): Discord bot token.
- ADMIN_ID: Discord user ID allowed to reset chat. Leave empty to allow anyone to use `/reset`.
- CHAT_CHANNEL_ID: If set, the bot only responds in this channel.
- BOT_NAME: Name used to replace mention text internally (default: assistant).
- TYPE: backend type; `ollama` (default) or `llamacpp`/`openai`.
- OLLAMA_*: host/port/scheme/model for Ollama backend.
- LLAMA_SERVER_URL, LLAMA_API_KEY: for OpenAI-compatible servers (used when TYPE != 'ollama').
- PREDICT / --predict: controls model prediction count (sent as `num_predict` to Ollama or `n_predict` to OpenAI-compatible servers).
- STREAM: when set, attempt to stream responses from Ollama (fallback to non-stream if streaming unsupported).
- CTX: context management parameter used to decide how much history to send (default: 2048).
- CHAT_MAX_LENGTH: max messages stored per channel (default: 500).
- MSG_MAX_CHARS: max characters saved per message (default: 1000).
- BUFFER_SIZE: internal send delay in milliseconds to reduce rate-limit risk (default: 500).

## Commands & Interaction

- Slash command: `/reset` — Clears stored chat context for the current channel. If `ADMIN_ID` is set only that user may use it; if empty, anyone with the command can use it. Response is ephemeral.
- The bot only returns LLM output in guild channels; when mentioned in DMs it replies with a short "can't respond in private messages" message.

## Backends and PREDICT

- Ollama backend: uses the Ollama AsyncClient. When `PREDICT` is set, it sends `num_predict` in options to Ollama.
- OpenAI-compatible backend: sets `openai.api_base` to `LLAMA_SERVER_URL` and calls `ChatCompletion.create`. `PREDICT` maps to `n_predict` for that server.

## Troubleshooting & Tuning

- If context payloads are too large, reduce `CHAT_MAX_LENGTH` or `MSG_MAX_CHARS` or lower `CTX`.
- If invites or invite URL do not appear in logs, the application id may not be available at startup; wait until the client populates it.
- If using streaming and experiencing issues, disable `STREAM` to use the non-streaming fallback.
- If the chosen backend is unreachable, check `OLLAMA_HOST` / `LLAMA_SERVER_URL`, network settings, and Docker host mappings.

## Example model (Ollama)

FROM llama3
PARAMETER temperature 0.7
SYSTEM """
You are a chatter in a Discord channel. Your goal is to respond like a human participant in the chat.
You can see the messages in the format: "**<message id> at <time> <author name>(<author id>) said in <channel>**: <message>".
Do not reproduce that metadata format in your replies; use it only to personalize and situate your responses.
Your name is Assistant.
"""

Replace "Assistant" with the bot's configured name for best results.

## Credits

Adapted from mxyng/discollama and extended with async Redis, backend selection, a `/reset` slash command, message size limits, and optional streaming.

## License

MIT. See the LICENSE file for details.