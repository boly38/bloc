# BLOC

**BLOC** (**BLOC for Long-running OpenCode Conversations**) is a minimal Docker Compose setup for running
[OpenCode](https://opencode.ai/) on its own or connecting it to Telegram.

This BLOC release is qualified with **OpenCode v1.19.3**. OpenCode v2 is not yet qualified.

## Features

- Use Opper AI by default or connect another provider supported by OpenCode through the TUI.
- Optionally query it from Telegram through a dependency-free Python bridge.
- Show the configured provider, model, and variant on each Telegram request card.
- Keep one persistent OpenCode session per Telegram chat.
- Access the same server through the local OpenCode TUI without publishing its API port.

## Commands

Run `make help` for the available commands.

## Documentation

- [Quickstart](docs/quickstart.md): configure, start, and stop BLOC.
- [Usage](docs/usage.md): Telegram formatting, sessions, and cost reporting.
- [Provider](docs/provider.md): connect a provider, choose a model, and configure Telegram to use it.
- [Security](docs/security.md): trust boundaries and safe deployment.
- [Coolify deployment](docs/coolify_deploy.md): deploy with Coolify Compose.
- [Contributing](CONTRIBUTING.md): tests and release process.
