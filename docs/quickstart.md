# Quickstart

## Requirements

- Docker with Compose v2;
- an [Opper AI](https://opper.ai/) API key (see [Provider](provider.md));
- a Telegram bot from [@BotFather](https://t.me/BotFather) if you want Telegram access.

Run all operations from the repository root through the `Makefile`.

## Configure and start

Create the local environment file and set `OPPER_API_KEY`:

```sh
cp .env.template .env
${EDITOR:-vi} .env
```

Start OpenCode alone, then check it:

```sh
make config
make start
make status
make health
```

To enable Telegram, first put the bot token in `.env` and discover your chat ID:

```sh
make telegram-chat-id
```

In a private chat, send the exact one-time challenge shown in the terminal. Put the
resulting ID in `TELEGRAM_ALLOWED_CHAT_IDS` and set `TELEGRAM_ENABLED=true` in `.env`.
Then start and verify the services:

```sh
make config
make start
make status
make health
```

See [Usage](usage.md) for interacting with the bot and [Security](security.md) before
allowing other people to use it.

## Stop and inspect

```sh
make logs                 # follow all service logs
make logs SERVICE=telegram
make stop                 # stop containers, keep them and their data
make down                 # remove containers, keep persistent volumes
```

Use `make help` for the complete command list. `make clean` also deletes persistent
volumes; it is not a routine stop or update command.
