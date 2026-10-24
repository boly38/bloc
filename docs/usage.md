# Usage

## Telegram

Send a message in a chat listed in `TELEGRAM_ALLOWED_CHAT_IDS`. The bridge keeps one
OpenCode session per chat, so later messages continue that chat's conversation. OpenCode
stores sessions centrally; selecting a Telegram session in the TUI shares its history.
Use a separate session for independent work.

The Telegram commands are:

- `/start` displays a welcome message and points to `/help`; it does not reset the session.
- `/help` lists the available commands without calling the model.
- `/new` starts a new OpenCode session for the chat. The previous session and its history
  are kept and remain available in OpenCode/TUI. `/cost` then reports usage for the new
  session, starting at zero.
- `/health` reports a sanitized bridge, OpenCode API, and current-session status.
  Set `TELEGRAM_HEALTH_OPPER_ENABLED=true` to also check Opper API reachability without
  credentials or inference; it is disabled by default. See [Provider](provider.md#optional-reachability-check).
- `/cost` reports the current session's total and its total for the current fixed UTC hour.
  Costs are based on usage reported by OpenCode, not an estimate from provider prices, and
  are not account-wide totals. Replies include a cost line when OpenCode provides usage
  data.
- `/status [reference]` shows the active request, or the state of a request with the given
  reference. A reference can only be inspected from the chat that owns it.
- `/abort` asks OpenCode to stop the active request; it is an alias for `/cancel`.
- `/cancel [reference]` asks OpenCode to abort the active request, or the identified request.
  The command reports whether the stop was confirmed; use `/status` to check the final state.
- `/allow [letter]` approves the matching permission once. `/allowAll <letter>` remembers an
  eligible rule, and `/deny [letter]` rejects it. The active request card shows the available
  letter and clickable buttons; no permission is approved automatically.

An unknown slash command returns an error directing you to `/help`; it is not sent to the
model. Normal messages are still sent to OpenCode. `/health` and `/cost` do not send a
prompt to the model.

While a normal request is running, the acknowledgement is updated with deterministic
analysis/tool/test stages, or a waiting message approximately every 30 seconds when no
progress is available. It also shows time since the last correlated OpenCode activity
separately from the last successful API heartbeat; a responsive API does not guarantee that
the agent or its current tool is progressing. Network calls can delay updates. Temporary
status/history HTTP timeouts are retried; they do not end the request. The default maximum request duration is
5 days, configurable with `OPENCODE_REQUEST_MAX_DURATION_SECONDS`. At that limit, the
bridge asks OpenCode to abort and keeps the chat busy until it confirms the session has
stopped. If the stop cannot be confirmed, `/status` reports that the request may still be
running; check before retrying. Failures include a reference and, when OpenCode supplies a
structured error, distinguish provider authentication/access, rate/quota limits, and
provider service errors.

The initial Telegram acknowledgement remains as a request card. It has status and abort
buttons while the request is active. `/status` appends a timestamped entry to that card, and
permission decisions appear there with letter aliases and action buttons. The card shows
deterministic analysis/tool/test stages, observed partial cost, and time since OpenCode's last
activity separately from the API heartbeat.

When the request ends, the card is replaced with a terminal summary containing status, cost,
duration, request reference, distinct tool count, and total tool-call count. The model's final answer is sent as a
separate Telegram message and lists tool names only, not arguments or output.

OpenCode permission requests for the active request's session tree are forwarded to the
request card. When several are pending, use the displayed letter in `/allow A`,
`/allowAll A`, or `/deny A`. Interactive questions are not yet forwarded to Telegram; answer
them in the remote TUI or use `/abort` to stop the active request.

Only one request at a time is admitted per chat. A new prompt while one is active is refused
with the active request's reference. Use the card buttons or `/status` and `/abort` to manage it. References do
not expose OpenCode session IDs or prompt content.
The final reply retains its reference so the request can still be inspected afterward.

Telegram replies support this Markdown subset:

- headings: `# Heading`;
- emphasis: `**bold**`, `*italic*`, and `~~strikethrough~~` (not nested);
- simple lists: `- item` or `1. item`;
- quotes: `> quoted text`;
- inline and fenced code, with an optional language;
- links: `[label](https://example.com)`.

The bridge converts supported formatting to Telegram HTML and escapes text. Unsupported
constructs are sent as readable text; if Telegram rejects formatted text, it is retried
without formatting. To check rendering without an OpenCode request, use
`make format-test CHAT_ID=<allowed-chat-id>` while the Telegram service is running.

## Local OpenCode TUI

On the Docker host, run `make tui` after starting the deployment. It connects to the
existing server and uses the same workspace, credentials, and sessions. Docker access is
required; the command does not publish an API port. Closing the TUI does not stop the
services. See [Security](security.md).

## Persistent data

`make stop` sends SIGTERM to the bridge. Its main thread stays responsive while a single
background poller handles Telegram I/O; blocked HTTP calls do not delay process exit.
Request metadata is persisted in `/data/requests.sqlite3` in the Telegram volume, but prompts
and generated responses are not copied into that database. On restart, submitted requests
are correlated with OpenCode history and monitored without resubmission. Requests that had
not reached OpenCode are marked interrupted rather than replayed. Replies may be delayed or
their delivery may be uncertain if shutdown occurs while Telegram is updating the
acknowledgement. Use `/status <reference>` before retrying. The Compose 30-second grace period
is a safety limit, not a mandatory wait.

`make restart`, `make stop`, and `make down` preserve OpenCode data, workspace files, and
Telegram session mappings. Restarting the services therefore continues the current session;
use `/new` to start a fresh one without deleting the old history. Use `make backup` and
`make restore` for volume backups and recovery. Backups go under the ignored `backups/`
directory by default, in a timestamped folder containing one `.tar.gz` archive per Docker
volume.

```sh
make stop
make backup
tar -tzf backups/bloc-<timestamp>/opencode-data.tar.gz
make restore                         # selects the latest complete backup under backups/
make restore BACKUP_DIR=backups/bloc-<timestamp>  # select a specific backup
```

Restore displays the selected folder and requires typing `RESTORE` before replacing any
volume contents. Backups may contain private state and conversation history: protect them
as secrets and never commit them. Run backup as your normal user, not with `sudo`, so the
resulting files remain accessible to you. On WSL filesystems that do not support POSIX
permissions, access is governed by the Windows ACL; keep backups in a suitably private
location. `make clean` deletes the volumes.

See the [Quickstart](quickstart.md) for startup and operational commands.
