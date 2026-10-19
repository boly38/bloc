# Usage

## Telegram

Send a message in a chat listed in `TELEGRAM_ALLOWED_CHAT_IDS`. The bridge keeps one
OpenCode session per chat, so later messages continue that chat's conversation. OpenCode
stores sessions centrally; selecting a Telegram session in the TUI shares its history.
Use a separate session for independent work.

The bot supports `/health` for a sanitized service and session check, and `/cost` for the
current chat's session total and current fixed UTC-hour total. Neither command sends a
prompt to the model. Costs are based on usage reported by OpenCode, not an estimate from
provider prices, and are not account-wide totals. Replies include a cost line when
OpenCode provides usage data.

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

`make stop` and `make down` preserve OpenCode data, workspace files, and Telegram session
mappings. Use `make backup` and `make restore` for volume backups and recovery. Backups go
under the ignored `backups/` directory by default, in a timestamped folder containing one
`.tar.gz` archive per Docker volume.

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
