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
python3 -m py_compile bot/telegram/bridge.py
git diff --check
```

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
