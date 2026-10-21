# Contributing

Use the root `Makefile` for project operations. Never commit `.env`, credentials, OpenCode
sessions, or Docker volume contents.

## Automated checks

Run the dependency-free unit tests with:

```sh
make test
```

The tests use Python's standard library and need no credentials, Docker, Telegram, or LLM
access. Before submitting changes, also run:

```sh
make config
python3 -m compileall -q bot/telegram/bloc_bridge
git diff --check
```

## Python source layout

The dependency-free bridge lives in the `bloc_bridge` package under `bot/telegram`:

- `__main__.py` is the `python -m bloc_bridge` entry point used by Docker.
- `bridge.py` orchestrates commands, sessions, and replies.
- `telegram/` contains polling, Markdown rendering, and the formatting smoke test.
- `opencode/` contains API helpers and cost reporting.
- `bridge_config.py`, `bridge_runtime.py`, `http_api.py`, and `session_state.py` provide
  shared configuration, worker lifecycle, HTTP transport, and persistence.

Use package-relative imports inside the bridge. Docker copies the complete package;
`make format-test` runs `python -m bloc_bridge.telegram.format_test` in the container.

Start with the PlantUML [bridge overview](docs/diagrams/bloc_bridge-overview.puml) for the
high-level message flow. The [message lifecycle and shutdown](docs/diagrams/bloc_bridge-exchanges.puml)
diagram provides the technical details of the asynchronous path.
Open the `.puml` files with a local PlantUML viewer to render them.

## Manual checks

For changes to configuration or deployment, validate the Compose configuration in the
intended mode. `make config` uses the local `.env`; check both OpenCode-only mode and
Telegram mode when applicable. Do not share the output of environment files or secrets.

For Telegram formatting changes, start the bridge and send the static fixture to an
already allowed chat:

```sh
make format-test CHAT_ID=<allowed-chat-id>
```

For an end-to-end check, start the services, send a message from an allowed chat, then
check `make status`, `make health`, and `make logs SERVICE=telegram`. The `/health` and
`/cost` commands check status and usage without making an LLM call.

## Draft a release

After release changes are merged into `main`, open **Actions > Draft patch release >
Run workflow**, select `main`, and run the workflow. It runs the offline checks, creates
the next patch version tag (for example, `v1.0.0` becomes `v1.0.1`), and prepares a GitHub
release draft with generated notes. Review the notes and publish the draft manually.

Only patch releases are currently supported. Never attach `.env`, credentials, sessions,
or Docker volumes.
