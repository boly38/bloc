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

An unknown slash command returns an error directing you to `/help`; it is not sent to the
model. Normal messages are still sent to OpenCode. `/health` and `/cost` do not send a
prompt to the model.

While a normal request is running, the acknowledgement is updated with sanitized
reasoning/tool progress or a waiting message approximately every 30 seconds when no
progress is available. Network calls can delay updates. These updates do not reset the
300-second OpenCode deadline. Failures include a reference for the operator and, when
OpenCode supplies a structured error, distinguish provider authentication/access,
rate/quota limits, and provider service errors. Do not automatically retry: an interrupted
request may still be running in OpenCode.

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
Queued requests are discarded on shutdown, and in-flight replies are not guaranteed to
be delivered. Work already submitted to OpenCode may continue there: inspect the session
before retrying a prompt. The Compose 30-second grace period is a safety limit, not a
mandatory wait.

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
