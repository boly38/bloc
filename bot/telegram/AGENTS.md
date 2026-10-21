# Agent guide for the Telegram bridge

Applies to `bot/telegram/` in addition to the root `AGENTS.md`. Consult this guide for
bridge tests under `tests/` as well. Keep implementation details here rather than
duplicating them in the root guide.

## Where to change code

The package is `bloc_bridge`; use package-relative imports, not flat module imports.
Docker runs `python -m bloc_bridge`. Its Dockerfile copies the entire package.

- `bridge.py`: command routing, acknowledgement, admission, OpenCode exchange, and replies.
  Keep `main()` focused on startup, supervision, signals, and cleanup.
- `telegram/polling.py`: the single Telegram poller, update offsets, and retry backoff.
- `telegram/markdown_renderer.py`: Markdown-to-HTML conversion and bounded message chunks.
- `telegram/format_test.py`: static formatting fixture; never calls the model.
- `opencode/api.py`: OpenCode API helpers and prompt/assistant correlation.
- `opencode/cost_reporting.py`: usage aggregation and sanitized diagnostics.
- `bridge_runtime.py`: daemon workers, shutdown context, and cooperative cancellation.
- `http_api.py`: shared HTTP transport, Telegram error classification, and polling lock.
- `bridge_config.py` and `session_state.py`: environment settings and persisted chat sessions.

For an overview, see [message flow](../../docs/diagrams/bloc_bridge-overview.puml).
For details, see [exchanges and shutdown](../../docs/diagrams/bloc_bridge-exchanges.puml).

## Behavior to preserve

- Check the chat allowlist before acknowledgements, session access, or model calls.
- Acknowledge before queuing; update that message with progress/result and send extra
  chunks only when needed. A failed send may already have reached Telegram: do not
  blindly retry an acknowledgement after a transport timeout.
- Keep one poller and advance offsets only after handling an update.
- Bound admitted work (currently 32 requests, 4 workers) and retain per-chat locking.
  Do not assume that a lock guarantees FIFO execution of requests within a chat.
- Reuse the selected session per chat. `/new` changes the mapping without deleting history;
  persist mappings with atomic file replacement.
- Slash commands never submit a model prompt. `/new`, `/health`, and `/cost` can call
  OpenCode APIs; `/start`, `/help`, and unknown commands are handled locally.
- Correlate assistant responses to the generated prompt message ID. SSE is optional
  progress information, not proof of completion; status and history determine completion.
- Costs come from OpenCode usage, including descendant sessions, not price estimates.
- Render through the existing Markdown renderer. Fall back to plain text only for a
  classified Telegram formatting rejection, not for every delivery error.

## Shutdown and privacy

- Keep the main thread free of blocking network I/O. SIGTERM/SIGINT must remain responsive
  during polling, acknowledgement, active requests, and polling-lock contention.
- Do not replace daemon workers with an executor that joins blocked workers at interpreter
  exit, or add unbounded joins. Preserve cooperative checks around HTTP and request phases.
- `ShutdownRequested` intentionally inherits from `BaseException` so request error handlers
  do not swallow cancellation. Handle it only at lifecycle boundaries; preserve cleanup.
- Shutdown discards queued work and may lose replies. A submitted OpenCode prompt may
  continue on the server; do not claim it was cancelled or automatically replay it.
- Never log bot-token URLs, prompt/response bodies, provider error text, or session IDs.
  Use existing sanitized diagnostics and request references; surface failures explicitly.

## Validation

Run from the repository root. Imports read required environment variables; follow existing
tests that inject dummy values before importing the package, without loading real secrets.

```sh
python3 -m unittest discover -s tests -p 'test_telegram*.py'
python3 -m compileall -q bot/telegram/bloc_bridge
git diff --check
```

Use `make test` for the full offline suite. Tests require no Docker, Telegram, or model access.
`tests/test_telegram_shutdown.py` sends real SIGTERM to child processes and asserts clean
exit in under two seconds with blocked I/O. Preserve this measurable regression check;
an event-only mock is not a substitute.

Changes to package paths must also update Docker, `scripts/compose.sh`, tests, CI syntax
checks, and documentation. Live checks such as `make format-test` send real messages:
run them only when explicitly authorized.
