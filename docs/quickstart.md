# Quickstart

## Requirements

- Docker with Compose v2;
- access to the provider you choose (credentials depend on the provider); an
  [Opper AI](https://opper.ai/) API key is needed only for the default Opper setup
  (see [Provider](provider.md));
- a Telegram bot from [@BotFather](https://t.me/BotFather) if you want Telegram access.

Run all operations from the repository root through the `Makefile`.

## Configure and start

Create the local environment file. For the default Opper setup, set `OPPER_API_KEY`. To use
another provider, you can leave it empty and follow the [provider setup](provider.md) after
starting OpenCode:

```sh
cp .env.template .env
${EDITOR:-vi} .env
```

Set `OPENCODE_SERVER_PASSWORD` to a strong, unique value. OpenCode v2 requires authenticated
server access; the bridge and local TUI use this password. Generate one with `openssl rand -hex 32`.

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

## BLOC 1.x deployments

BLOC 2.x uses the separate Compose project `bloc-v2` and separate persistent volumes. It does
not migrate OpenCode sessions, provider credentials, workspace files, Telegram state, or backups
from BLOC 1.x. The v1 deployment and its resources remain untouched; continue using a BLOC 1.x
release to access them. Do not restore a v1 backup into BLOC 2.x.

When updating an existing checkout, update the local `.env` `OPENCODE_IMAGE` value to the v2
digest shown in `.env.template`, and set `OPENCODE_SERVER_PASSWORD`. The existing `.env` is not
overwritten by Git or by copying the template again.
