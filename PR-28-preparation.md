# PR #28 Preparation

## Goal

Document and validate an alternative OpenCode provider, preferably GitHub Copilot,
while keeping Opper AI as the default BLOC provider.

## Working decisions

- Verify the official OpenCode authentication and provider flow before documenting it.
- Treat provider credentials as secrets and avoid committing or printing them.
- Verify whether the existing Compose environment forces Opper-specific settings.
- Keep the change minimal unless a tested configuration requires Compose or config changes.

## Status

- Tracking file created.
- Repository and issue #28 inspected.
- Official OpenCode documentation confirms GitHub Copilot support through `/connect`,
  device authentication, and `/models`.
- The implementation will keep Opper as the default while making the model reference
  provider-neutral for the OpenCode and Telegram services.
- Copilot credentials will remain in the persistent OpenCode data volume; no Copilot
  secret will be added to `.env` or the repository.

## Validation log

- `git diff --check`: passed.
- JSON syntax was not executable because `python3` is unavailable in the workspace.
- `make test` and `make config` were not executable because `make` is unavailable in
  the workspace; Docker/Compose is also unavailable.
- No credentials were added to the repository.

## Remaining verification

- Run `make config` with Docker Compose v2 and a local `.env` based on `.env.template`.
- Run the project test suite and the documented health check in an environment with
  `make`, Python, and Docker installed.

## Pull request

- Branch: `docs/provider-alternatives-28`
- Commit: `10a0712 docs: document GitHub Copilot provider setup`
- Pull request: https://github.com/boly38/bloc/pull/37
- Issue linkage: `Closes #28`
