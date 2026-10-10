# Implementation tracking

This file tracks the MVP described in [`specifications.md`](specifications.md),
[issue #3 — MVP: OpenCode accessible via Telegram](https://github.com/boly38/bloc/issues/3),
and [issue #7 — Configurable launch mode](https://github.com/boly38/bloc/issues/7), which
formalizes the work on branch `bloc-v0`.

> **Checkboxes:** a checked item under “Already in the repository” means it is
> implemented, not necessarily validated in real-world use. Executed tests are listed
> separately. **[QA OK]** marks a user-reported manual test; its scope is stated to
> avoid implying unperformed checks. Unchecked items remain open; follow the checklist
> order to validate the MVP without skipping steps.

## Already in the repository

- [x] OpenCode runs as an HTTP server in the main Compose stack, with a health check and no host-published port.
- [x] The official Telegram bridge uses long polling and a chat allowlist.
- [x] The bridge keeps one OpenCode session per chat in a persistent volume.
- [x] `TELEGRAM_ENABLED` selects OpenCode-only or OpenCode-plus-Telegram mode; the Makefile delegates Compose profile selection and checks to `scripts/compose.sh`.
- [x] The Opper AI provider, its environment-based API key, and the default model `opper/gpt-5.6-luna#medium` are configured.
- [x] The main and repair Compose stacks are separate; the repair stack mounts the data/workspace volumes and read-only `opencode.json`.
- [x] Common operations are available in the root Makefile; the README documents local startup and Coolify caveats.
- [x] `.env` is ignored by Git and `.env.template` documents the expected variables.
- [x] The bridge injects an anti-hallucination instruction into OpenCode sessions.

## Checks already run

- [x] `make config` — passed with the local configuration; no secret values were displayed.
- [x] `python3 -m py_compile bot/telegram/bridge.py` — passed.
- [x] `git diff --check` — passed before publication and rerun after the latest tracking update.
- [x] Checked `.env.template` alone — Compose correctly rejects required variables that are empty; no local secrets were read or displayed.
- [x] **[QA OK]** User-reported local test: with a valid configuration, `make start` allowed OpenCode to be queried from Telegram.
- [x] **[QA OK]** User confirmed that `make repair` works.
- [x] **[QA OK]** User confirmed the `make check-env`, `make config`, `make build`, `make logs`, `make status`, `make health`, `make repair`, and `make clean` targets.
- [x] Compose configuration tested with temporary `.env` files: Telegram absent/false, true with variables, true rejected without token/allowlist, and invalid mode rejected.
- [x] `scripts/compose.sh` routing tested with a fake Docker executable: OpenCode-only mode stops Telegram then starts OpenCode; true mode activates the Telegram profile.
- [x] Before publication, verified that `.env` is ignored by Git and not tracked (`git check-ignore` / `git ls-files`).
- [x] **[QA OK]** User reports a quick successful retest of the configurable launch mode after the Makefile refactor.

## To qualify / test

Complete these items before considering the implementation fully accepted:

1. [ ] **Resolve first startup without a known Telegram chat ID.** The allowlist is mandatory and `scripts/compose.sh` rejects an empty value; define a bootstrap procedure without disabling the allowlist.
2. [x] **[QA OK]** With a correctly configured `.env`, `make config`, `make start`, and `make status` were tested; the Telegram test confirmed that the services started.
3. [x] **[QA OK]** `make health` confirmed that the OpenCode API is healthy.
4. [ ] Verify the complete Telegram flow: `/start`, a regular request from an authorized chat, and an Opper response.
5. [ ] Verify that an unauthorized chat receives no response and its ID is logged without exposing secrets.
6. [ ] Restart the services and verify that the Telegram session is restored and OpenCode/workspace volumes persist.
7. [ ] Complete the repair test with the main service stopped. **[QA OK]** User confirms that `make repair` opens and works.
8. [ ] Repeat local setup on a clean developer configuration using the README; the **[QA OK]** test above already confirms startup and Telegram interaction on the configuration tested.
9. [ ] Validate Docker Compose deployment on Coolify: Telegram profile activation, volume reuse on redeploy, volume-name alignment, and no publicly exposed OpenCode port.
10. [x] `.env` is ignored and not tracked by Git; verified again before publication.
11. [ ] Once these criteria are met, update and close [issue #3](https://github.com/boly38/bloc/issues/3) if all its acceptance criteria are satisfied.

## Remaining MVP work

- [ ] Resolve the allowlist bootstrap issue (item 1) and keep the README, Compose file, and `.env.template` aligned.
- [ ] Complete and record end-to-end tests for items 2–10. Local commands and the basic flow are reported **[QA OK]**, but the other Telegram scenarios, restart persistence, and Coolify remain to be validated.
- [ ] Fix any issues found; check items only after actual verification.
- [ ] Confirm that the MVP scope of [issue #3](https://github.com/boly38/bloc/issues/3) is met before considering the MVP complete.

## Later work — tracked in dedicated issues

- [ ] [#1 — Secure the OpenCode server](https://github.com/boly38/bloc/issues/1): authentication, threat model, unauthorized-access tests, and secret-handling procedures. `OPENCODE_SERVER_PASSWORD` is not currently passed through Compose. **Do not expose the OpenCode API to the Internet before this is addressed.**
- [ ] [#6 — Add TUI access to the OpenCode server](https://github.com/boly38/bloc/issues/6): documented administrator access without unnecessarily publishing the port.
- [ ] [#7 — Select OpenCode-only or Telegram mode through configuration](https://github.com/boly38/bloc/issues/7): implementation and checks are complete, with a quick user retest confirmed; [PR #4](https://github.com/boly38/bloc/pull/4) is open and will close the issue when merged.
- [ ] [#5 — Show the cost of an exchange or session on demand](https://github.com/boly38/bloc/issues/5): use cost data actually exposed by OpenCode/Opper.
- [ ] [#2 — Translate the project documentation and user-facing text into English](https://github.com/boly38/bloc/issues/2): translation is included in this PR; close after merge.

## References

- Repository specification: [`specifications.md`](specifications.md)
- User guide: [`README.md`](README.md)
- Contribution and release-draft guide: [`CONTRIBUTING.md`](CONTRIBUTING.md)
- MVP issue: [boly38/bloc#3](https://github.com/boly38/bloc/issues/3)
- Configurable launch issue: [boly38/bloc#7](https://github.com/boly38/bloc/issues/7)
- Deferred issues: [#1](https://github.com/boly38/bloc/issues/1), [#2](https://github.com/boly38/bloc/issues/2), [#5](https://github.com/boly38/bloc/issues/5), [#6](https://github.com/boly38/bloc/issues/6)
