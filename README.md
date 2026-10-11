# BLOC

**BLOC** stands for **BLOC Layer for OpenCode**—a deliberately recursive, GNU-style
name for a minimal Docker Compose setup that runs OpenCode alone or connects it to
Telegram. In Telegram mode, the bridge keeps one OpenCode session per chat in a Docker
volume.

## Requirements

- Docker with Compose v2;
- an Opper AI API key and a model available in your Opper project;
- a Telegram bot created with [@BotFather](https://t.me/BotFather), only if Telegram is enabled.

## Local startup

```sh
cp .env.template .env
# set OPPER_API_KEY; see “Launch modes” for Telegram setup
make start
make logs
```

Operational commands are centralized in the root `Makefile`. Run `make help` for the
full list:

```text
make start       # start OpenCode alone or with Telegram, based on TELEGRAM_ENABLED
make stop        # stop without removing containers
make down        # remove containers, keep volumes
make status      # show service status
make logs        # follow logs
make health      # check the OpenCode API
make repair      # open the repair container
```

Contribution and release-draft instructions are in [`CONTRIBUTING.md`](CONTRIBUTING.md).

## Launch modes

Set the mode in `.env` with `TELEGRAM_ENABLED`:

- `TELEGRAM_ENABLED=false` or unset: `make start` starts OpenCode alone. Telegram
  variables are not required. This starts only the server; interactive TUI access is
  tracked separately in [issue #6](https://github.com/boly38/bloc/issues/6).
- `TELEGRAM_ENABLED=true`: `make start` starts OpenCode and the Telegram bridge. Set
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_IDS` as well; the Makefile refuses to
  start if either value is missing.

When switching back to `false`, `make start` also stops Telegram before starting
OpenCode alone. `make status` shows both services, and `make stop`, `make down`, and
`make clean` account for both in either mode. The OpenCode API remains on the Docker
network; no host port is published.

The main Compose file is in `bot/`. The repair Compose file is intentionally separate
in `bot-repair/` so it can be used independently if the main deployment has a problem.

## Opper AI provider

The configured provider is `opper`, using Opper's official OpenAI-compatible API at
`https://api.opper.ai/v3/compat`. The key is supplied only through `OPPER_API_KEY` and
is never written to `opencode.json`.

The default model is `gpt-5.6-luna` via Opper AI, with the OpenCode `medium` variant.
The full model name is constructed as `opper/${OPPER_MODEL}#${OPPER_VARIANT}`, which
defaults to `opper/gpt-5.6-luna#medium`.

To select another model available in Opper, change `OPPER_MODEL` and, if needed,
`OPPER_VARIANT` in `.env`. Use model identifiers provided by Opper. Do not add the
`openai/` prefix to `OPPER_MODEL`: OpenCode already uses the `opper` provider and
expects `gpt-5.6-luna` as the model identifier.

The bot uses Telegram **long polling**, so no public domain or port is required. A
message from an unauthorized chat is rejected and its `chat_id` is written to the
logs; add it to `TELEGRAM_ALLOWED_CHAT_IDS` and restart the service. The allowlist must
be populated before the first Telegram startup; the bootstrap procedure for an
unknown initial chat ID is still to be defined.

Only one process can poll a bot token at a time. Bridge instances sharing the
`telegram-data` volume coordinate with a local lock; a poller running on another host
still needs to be stopped manually. Telegram 409 conflicts are logged without exposing
the bot token and retried with backoff.

## Telegram cost reporting

Every authorized OpenCode answer ends with a compact USD cost line; agent subtotals
are included when OpenCode provides them. Send `/cost` to see the current chat's
session total and its total for the current fixed UTC hour, along with available token
counts. These are not Opper account-wide or cross-chat totals. The bridge aggregates
all assistant messages across the session and its child sessions, so tool-using
exchanges are counted without estimating from model prices. `/cost` reads OpenCode
history and does not send an LLM prompt.

```text
Cost: $0.001971

/cost
Session: $0.008800
UTC hour (18:00–19:00): $0.002100
```

For a temporary cost/usage investigation, set `TELEGRAM_COST_DIAGNOSTICS=true` in
`.env` and restart with `make start`. Retrieve the sanitized metadata with
`make diagnostics`. The file is stored in the Telegram data volume; it includes
message and step cost/token fields, but excludes message text and chat/session IDs.
Turn diagnostics off again after the test and do not publish the generated file
without reviewing it.

## First startup

1. Create an API key in your Opper AI project and set `OPPER_API_KEY`.
2. Initialize the configuration and choose OpenCode-only mode by default:

   ```sh
   cp .env.template .env
   ${EDITOR:-vi} .env
   ```

3. Start and check OpenCode alone (`TELEGRAM_ENABLED=false`):

   ```sh
   make start
   make status
   make health
   ```

4. To enable Telegram, create a bot with [@BotFather](https://t.me/BotFather), set
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_IDS`, then set
   `TELEGRAM_ENABLED=true`. The allowlist must contain at least one ID before startup;
   do not leave it empty.
5. Start both services and check their status:

   ```sh
   make start
   make status
   make health
   ```

6. Send a message from an authorized chat. If there is an error,
   `make logs SERVICE=telegram` shows the HTTP details returned by OpenCode.

If Telegram replies `Invalid Opper API key` or the logs mention `401`, regenerate the
key in your Opper AI project and replace only the
`OPPER_API_KEY` value in `.env`. Then recreate the service:

```sh
make down
make start
```

## Coolify deployment

The Docker Compose deployment has been tested with Coolify, including redeploys. Use
these settings:

- **Base Directory:** `/bot`
- **Docker Compose Location:** `/docker-compose.yml` (relative to the base directory)
- Add the variables from [`.env.coolify.template`](.env.coolify.template) to the
  Coolify resource environment, replacing the example credentials and chat ID. The
  `COMPOSE_PROFILES=telegram` setting enables the Telegram service. `GH_TOKEN` is
  optional; set it only if the agent needs GitHub CLI access.

## GitHub CLI access (optional)

The OpenCode image includes GitHub CLI (`gh`), so the executable remains available
after a container rebuild or redeploy. To authenticate it, set `GH_TOKEN` in `.env` or
as a Coolify resource environment variable. GitHub CLI reads this variable directly;
there is no need to run an interactive `gh auth login`. The token is passed only to the
OpenCode service, not to the Telegram bridge. Leave it unset to run without GitHub
authentication.

Treat this as a powerful credential: the agent can use the permissions granted to the
token and may expose it through commands or content it processes. Use a dedicated
GitHub account or fine-grained token with access limited to the repositories and
permissions it needs, keep the deployment restricted to trusted users, and never put a
real token in the image, repository, or logs. For Coolify, store it as a secret
environment variable. See GitHub CLI's [environment variable reference](https://cli.github.com/manual/gh_help_environment).

## Repair

If the main Compose deployment does not start, open an independent shell:

```sh
make repair
```

The main deployment's volumes must already exist for their contents to be accessible.
This container does not start OpenCode automatically; it is only for diagnosing or
repairing the workspace. The repository's `bot/opencode.json` is mounted read-only at
`/workspace/opencode.json`, as in the main service. To change it, edit it on the host
and recreate the service if needed. The shell is minimal: run `make` commands on the
host, not inside this container.

## MVP choices and limitations

- Telegram was chosen because its official API supports long polling without a webhook
  or public infrastructure.
- The bridge uses no external Python dependencies.
- The OpenCode server is not exposed to the Internet; only the bridge can access it on
  the Docker network. HTTP authentication is therefore not enabled in Compose because
  the API is not published on the host.
- Services explicitly clear proxy variables (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`
  and their lowercase variants) so they do not inherit a proxy configured on the host.
- The anti-hallucination instruction is injected into every session. It does not
  replace human review for sensitive actions.

Official references: [OpenCode Server](https://opencode.ai/docs/server),
[OpenCode Docker](https://opencode.ai/docs/), and
[Telegram Bot API](https://core.telegram.org/bots/api).
