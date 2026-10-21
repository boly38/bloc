# Agent guide for bloc

## Purpose and navigation

`bloc` runs OpenCode in Docker and allows users to query it from Telegram through a
dependency-free Python bridge. Trusted operators can connect to the same server through
the local OpenCode TUI without publishing its API port.

- `Makefile` and `scripts/compose.sh`: operational commands and launch modes.
- `bot/docker-compose.yml`: main deployment; `bot-repair/docker-compose.yml`: independent
  repair deployment.
- `bot/telegram/bloc_bridge/`: Python bridge. Before changing anything under
  `bot/telegram/`, read [its scoped agent guide](bot/telegram/AGENTS.md).
  Also read that guide when editing bridge tests under `tests/`.
- `.env.template`: supported configuration; `docs/`: user and operational documentation.
- `CONTRIBUTING.md`: development checks and source layout.

## Invariants

- Keep documentation, code comments, and user-facing bridge messages in English.
- Keep the bridge dependency-free (Python standard library only).
- Run deployment operations through the root `Makefile`; do not bypass its environment
  handling with ad hoc Compose commands.
- The Opper AI key comes only from `OPPER_API_KEY` and must never be written to the
  repository.
- `.env` is local and ignored by Git. Never print secrets, inspect credentials unnecessarily,
  or commit environment files, session data, or volume contents.
- The default model is `opper/gpt-5.6-luna#medium` via Opper AI.
- Telegram uses long polling and requires the `TELEGRAM_ALLOWED_CHAT_IDS` allowlist.
- OpenCode TUI access is for trusted Docker administrators and must not publish the API port.
- Do not expose the OpenCode API to the Internet without addressing authentication,
  authorization, and TLS encryption.
- Do not invent APIs or behavior: verify them in official documentation or with a
  reproducible test.

## Change discipline

- Preserve unrelated local changes and keep edits focused on the requested behavior.
- Update `.env.template` and directly related documentation when changing configuration.
- Keep the two Compose deployments independent; check both when changing shared assumptions.
- Do not start/stop deployments, send Telegram messages, or make paid model calls merely
  to validate documentation or a local refactor.

## Validation

From the repository root, use offline tests for code changes:

```sh
make test
python3 -m compileall -q bot/telegram/bloc_bridge
git diff --check
```

For deployment/configuration changes, run `make config` using the intended environment
and launch mode. It may require local `.env` variables; never display their values.
See `.github/workflows/tests.yml` for checks in both launch modes.

Only when an end-to-end deployment check is requested, use `make start`, `make status`,
`make health`, and `make stop`. Report what was actually verified; offline tests do not
prove live Telegram, provider, or container behavior. Documentation-only changes do not
require runtime tests.
