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

After release changes are merged into `main`, checks have passed, and the version is
approved, create a GitHub draft with generated release notes. Replace the example version
with the approved tag:

```sh
git switch main
git pull --ff-only
VERSION='v1.2.3' # replace if a different version is approved
git tag -a "$VERSION" -m "Release $VERSION"
git push origin "$VERSION"
gh release create "$VERSION" --draft --verify-tag --title "$VERSION" --generate-notes
```

`--verify-tag` requires the tag to have been pushed. Review the generated notes and draft
before publishing. Never attach `.env`, credentials, sessions, or Docker volumes.
