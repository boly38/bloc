# bloc Project Context

## Goal

`bloc` is a minimal MVP that runs OpenCode in Docker and allows users to query it from
Telegram through a dependency-free Python bridge.

## Constraints

- The repository is documented in English.
- The main Compose file is `bot/docker-compose.yml`.
- The independent repair Compose file is `bot-repair/docker-compose.yml`.
- Operational commands go through the root `Makefile`.
- The Opper AI key comes only from `OPPER_API_KEY` and must never be written to the
  repository.
- `.env` is local and ignored by Git; `.env.template` documents the expected variables.
- The default model is `opper/gpt-5.6-luna#medium` via Opper AI.
- Telegram uses long polling and requires the `TELEGRAM_ALLOWED_CHAT_IDS` allowlist.
- Do not expose the OpenCode API to the Internet without addressing authentication,
  authorization, and TLS encryption.
- Do not invent APIs or behavior: verify them in official documentation or with a
  reproducible test.

## Validation Commands

```sh
make config
make start
make status
make health
python3 -m py_compile bot/telegram/bridge.py
git diff --check
```

Docker commands may require variables from `.env`. Never display or commit secrets.
