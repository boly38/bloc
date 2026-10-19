# BLOC

**BLOC** (BLOC Layer for OpenCode) is a minimal Docker Compose setup for running
[OpenCode](https://opencode.ai/) on its own or connecting it to Telegram.

This BLOC release is qualified with **OpenCode v1.19.3**. OpenCode v2 is not yet qualified.

## Features

- Run OpenCode with a configurable AI provider in Docker.
- Optionally query it from Telegram through a dependency-free Python bridge.
- Keep one persistent OpenCode session per Telegram chat.
- Access the same server through the local OpenCode TUI without publishing its API port.

## Commands

Run `make help` for the available commands.

## Documentation

- [Quickstart](docs/quickstart.md): configure, start, and stop BLOC.
- [Usage](docs/usage.md): Telegram formatting, sessions, and cost reporting.
- [Provider](docs/provider.md): configure Opper AI and select a model.
- [Security](docs/security.md): trust boundaries and safe deployment.
- [Coolify deployment](docs/coolify_deploy.md): deploy with Coolify Compose.
- [Contributing](CONTRIBUTING.md): tests and release process.
