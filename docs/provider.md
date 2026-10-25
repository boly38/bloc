# Providers

BLOC uses Opper AI by default, but Opper is not required. BLOC can use providers supported by
the qualified OpenCode release. Connect a provider in the trusted OpenCode TUI, choose one of
its available models, then set that model as `OPENCODE_MODEL` so Telegram uses it too. Provider
availability, authentication methods, model access, and billing are controlled by each provider.
See the official [OpenCode provider directory](https://opencode.ai/docs/providers/) for details.

## Default provider: Opper AI

The Opper endpoint is configured in [`bot/opencode.json`](../bot/opencode.json). Its API key
is supplied through `OPPER_API_KEY`; do not put it in the config file or commit it. The defaults
are:

```dotenv
OPPER_MODEL=gpt-5.6-luna
OPPER_VARIANT=medium
```

When `OPENCODE_MODEL` is empty, BLOC constructs
`opper/${OPPER_MODEL}#${OPPER_VARIANT}`, which defaults to `opper/gpt-5.6-luna#medium`. Use a
model available to your Opper project and omit the `openai/` prefix from `OPPER_MODEL`.

## Use another provider

These steps apply to local BLOC. For Coolify or another remote deployment, see
[Remote deployment](#remote-deployment) first.

1. Create `.env` from `.env.template`. `OPPER_API_KEY` is only needed if you plan to use Opper.
2. Start the server and open its trusted TUI:

   ```sh
   make config
   make start
   make tui
   ```

3. In the TUI, run `/connect`, choose the provider, and complete the authentication it requests.
   Some providers use browser sign-in; others ask for a provider API key or need extra settings.
4. Run `/models` and choose a model that your provider account can use. The CLI command
   `opencode models` lists full IDs in `provider/model` form. Do not infer an ID from its display
   name.
5. In `.env`, set `OPENCODE_MODEL` to that exact ID. Add `#variant` only if you selected a
   variant; for example:

   ```dotenv
   OPENCODE_MODEL=github-copilot/gpt-6-luna#medium
   ```

6. Apply the setting:

   ```sh
   make config
   make restart
   ```

Telegram requests use `OPENCODE_MODEL`, even if you select a different model in the TUI. Each
Telegram request card shows the configured provider, model, and variant; an omitted variant is
shown as `default`.

Provider credentials added through `/connect` are kept in the persistent `opencode-data` volume,
not in `.env`. A restart keeps them; `make clean` deletes the volume and requires connecting
again.

## Remote deployment

For Coolify, set `OPENCODE_MODEL` in the resource environment variables, not in the repository.
The local `.env` and local OpenCode credentials are not copied to Coolify. After deploying:

1. Run `make remote-tui HOST=<ssh-alias>` to find the remote OpenCode container ID.
2. Run `make remote-tui HOST=<ssh-alias> CONTAINER_ID=<container-id>` to open its TUI.
3. In that remote TUI, run `/connect` and `/models`.

The remote credentials are stored in that deployment's persistent `opencode-data` volume. See
the [Coolify remote TUI instructions](coolify_deploy.md#remote-tui) for the full procedure and
security requirements.

Never publish the OpenCode API port to the Internet or commit provider credentials.

## Optional reachability check

Set `TELEGRAM_HEALTH_OPPER_ENABLED=true` in the deployment environment to include an
Opper check in Telegram's `/health`. It is disabled by default and can be removed from
the command by setting the variable to `false` and recreating the bridge.

The isolated `bloc_bridge/opper_health.py` module sends an unauthenticated
`GET https://api.opper.ai/v3/compat/models` from the bridge, with a five-second socket
timeout, no redirects, no retries, and no response-body processing. DNS resolution can
depend on the system resolver's timeout. No key is passed to the bridge and no model is
invoked, so there is no inference charge. A `401` is an expected reachability result;
`200` is also accepted. Other HTTP statuses and transport failures are reported explicitly.
The endpoint is documented in Opper's [List Models API](https://docs.opper.ai/v3-api-reference/compatibility/list-models).

This checks only the bridge's network path to the Opper API, not OpenCode's network
path, its credentials, available credits, or the ability of a model to generate a response.
It is not an end-to-end LLM health check. It is never run automatically before a prompt,
and `make health` continues to check only the OpenCode server.
