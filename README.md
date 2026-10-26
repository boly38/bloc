# BLOC

**Use your OpenCode agent from Telegram—even when you are away from your desk.**

BLOC (**BLOC for Long-running OpenCode Conversations**) is a self-hosted Docker Compose setup that
connects [OpenCode](https://opencode.ai/) to an allowlisted Telegram chat. Deploy it with Docker
Compose or Coolify, keep OpenCode's API private, and manage long-running conversations from your
phone.

## Features

- Keep one persistent OpenCode session per Telegram chat for ongoing conversations.
- Follow long-running work with progress updates, `/status`, and `/abort`.
- Check per-request and per-session costs reported by OpenCode; BLOC does not estimate provider
  billing.
- Review tool permission requests in Telegram and explicitly allow or deny them; permissions are
  never approved automatically.
- Use Opper AI by default or connect another provider supported by OpenCode through the TUI.
- Show the configured provider, model, and variant on each Telegram request card.
- Deploy with Docker Compose or Coolify, and access the same server through the local TUI without
  publishing the OpenCode API port.

## Compatible versions

| BLOC version | OpenCode version | Status |
| --- | --- | --- |
| **1.0.2** | **1.19.3** | Legacy pairing; no longer maintained. |
| **2.x** | **2.1.4** | OpenCode v2 qualification target. |

BLOC 1.x and 2.x use separate Compose projects and persistent resources. There is no migration from
BLOC 1.x to 2.x: sessions, credentials, workspace files, Telegram state, and backups remain with
their original major version. BLOC 2.x uses the `bloc-v2` Compose project and rejects BLOC 1.x
backups.

## Get started

Follow the [Quickstart](docs/quickstart.md) to configure and launch BLOC. Use `make help` to see the
available operational commands.

## Documentation

- [Quickstart](docs/quickstart.md): configure, start, and stop BLOC.
- [Usage](docs/usage.md): Telegram formatting, sessions, and cost reporting.
- [Provider](docs/provider.md): connect a provider, choose a model, and configure Telegram to use it.
- [Security](docs/security.md): trust boundaries and safe deployment.
- [Coolify deployment](docs/coolify_deploy.md): deploy with Coolify Compose.
- [Contributing](CONTRIBUTING.md): tests and release process.
