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
- `telegram/request_presentation.py`: persistent-in-chat request card, status history, and
  permission action buttons.
- `telegram/format_test.py`: static formatting fixture; never calls the model.
- `opencode/api.py`: OpenCode API helpers and prompt/assistant correlation.
- `opencode/cost_reporting.py`: usage aggregation and sanitized diagnostics.
- `bridge_runtime.py`: daemon workers, shutdown context, and cooperative cancellation.
- `http_api.py`: shared HTTP transport, Telegram error classification, and polling lock.
- `opper_health.py`: isolated, optional, unauthenticated Opper reachability check for `/health` only.
- `bridge_config.py` and `session_state.py`: environment settings and persisted chat sessions.
- `request_store.py`: durable request metadata and Telegram-update deduplication.

For an overview, see [message flow](../../docs/diagrams/bloc_bridge-overview.puml).
For details, see [exchanges and shutdown](../../docs/diagrams/bloc_bridge-exchanges.puml).

## Behavior to preserve

- Check the chat allowlist before acknowledgements, session access, or model calls.
- Acknowledge before queuing and keep that message as the request card. Append the terminal
  summary there, send the model's final answer as a separate message, and send extra chunks only
  when needed. A failed send may already have reached Telegram: do not blindly retry an
  acknowledgement after a transport timeout.
- Keep one poller and advance offsets only after handling an update.
- Handle callback-query updates from the request card under the same chat allowlist and update
  deduplication as message commands. Keep the first acknowledgement as the active request card;
  append timestamped status entries, and remove active controls when the request becomes terminal.
- Bound admitted work (currently 32 requests, 4 workers), admit at most one active request
  per chat, and retain per-chat locking. `/status`, `/abort`, and `/cancel` must remain available while
  a request is active.
- Reuse the selected session per chat. `/new` changes the mapping without deleting history;
  persist mappings with atomic file replacement.
- Slash commands never submit a model prompt. `/new`, `/health`, and `/cost` can call
  OpenCode APIs; `/start`, `/help`, and unknown commands are handled locally.
- `/health` contacts Opper only when `TELEGRAM_HEALTH_OPPER_ENABLED=true`; this is a
  bridge-side reachability check, not proof of authentication or working inference.
- Correlate assistant responses to the generated prompt message ID. SSE is optional
  progress information, not proof of completion; status and history determine completion.
- Keep progress updates deterministic and sanitized: report analysis/tool/test stages and safe
  tool names, never raw reasoning text, tool inputs, or tool output. Include the request reference
  and remind users that `/status` and `/abort` target the active request by default.
- Show the age of the last correlated OpenCode activity separately from successful API polling;
  API reachability alone does not prove agent progress.
- Surface permission requests for the active request's session tree and require an explicit
  `/allow <letter>`, `/allowAll <letter>`, or `/deny <letter>` decision; approval is one-time by
  default. Never auto-approve. Interactive questions are not currently bridged to Telegram.
- At exchange completion, report a deduplicated list of tool names and tool-call count, including
  recognized CLI names such as `gh`; never include command arguments or tool output.
- Report only OpenCode usage already observed for the active exchange, marked as partial; do not
  estimate cost or count repeated history snapshots more than once.
- Persist request references, Telegram update IDs, acknowledgement IDs, and OpenCode
  correlation IDs without persisting prompt or response text. Resume submitted work by
  correlation; never replay an uncertain submission. Mark queued, unsubmitted work interrupted
  on restart.
- Retry transient status/history HTTP timeouts. The configurable overall request limit is
  5 days by default; request timeout or cancellation must not release a chat until
  OpenCode is confirmed idle.
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
  continue on the server; recover it on restart by session/prompt IDs, but do not claim it was
  cancelled or automatically replay it.
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
