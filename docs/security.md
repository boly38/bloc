# Security

## Intended use

BLOC is intended to be deployed with runtime configuration and secrets supplied through
environment variables, not committed to the repository or baked into images. For Telegram,
only chats explicitly listed in `TELEGRAM_ALLOWED_CHAT_IDS` may exchange prompts and
responses with OpenCode. The local TUI is a separate administrative interface, available
only to trusted operators with Docker access.

## Trust boundaries

- Every ID in `TELEGRAM_ALLOWED_CHAT_IDS` is trusted to use OpenCode and the tools and
  credentials available to the agent. Only allow chats whose users you trust.
- The OpenCode API has no HTTP authentication. The Compose setup does not publish its
  port; keep it on the private Docker network and do not expose it to the Internet.
- Docker and Coolify administrators can access containers and environment variables.
  Restrict administrative access and verify that platform-level routing does not expose
  the API.
- The local TUI requires Docker access and is an administrative interface, not a separate
  security boundary.

## Secrets and data

Keep `.env`, provider keys, Telegram tokens, and optional `GH_TOKEN` out of Git and logs.
Grant GitHub tokens only the permissions needed, and leave `GH_TOKEN` unset when GitHub
access is unnecessary. Store deployment credentials in the platform's secret environment
configuration.

Persistent volumes and backups may contain session history, workspace files, credentials,
or other private state. Telegram request metadata includes chat, update, and OpenCode
correlation identifiers, but does not store prompt or response text. Restrict access,
encrypt backups before storing them off-host, and never commit them.

The anti-hallucination instruction is not a security control. Review sensitive actions
and outputs. If public API access is ever required, first define authentication,
authorization, TLS, and a threat model; these are not provided by this project.

For operational guidance, see the [Quickstart](quickstart.md) and
[Coolify deployment guide](coolify_deploy.md).
