# Provider

Opper AI is the example provider configured by default in BLOC. Its OpenAI-compatible
endpoint and provider settings are defined in [`bot/opencode.json`](../bot/opencode.json).
The API key comes from `OPPER_API_KEY`; it is passed to the OpenCode container and is not
stored in that configuration file.

OpenCode supports many providers and local models, and also lets you configure a custom
provider. You can replace the example provider or add your own in `bot/opencode.json`.
See the official [OpenCode provider documentation](https://opencode.ai/docs/providers/)
for supported providers and configuration options.

Set the key in the ignored local `.env` file. The defaults are:

```dotenv
OPPER_MODEL=gpt-5.6-luna
OPPER_VARIANT=medium
```

The resulting OpenCode model identifier is
`opper/${OPPER_MODEL}#${OPPER_VARIANT}`, by default
`opper/gpt-5.6-luna#medium`. Use a model identifier available in your Opper project and
do not add the `openai/` prefix to `OPPER_MODEL`.

After changing provider settings, validate and restart:

```sh
make config
make start
make health
```

For Coolify, configure these values as resource environment variables; see the
[Coolify deployment guide](coolify_deploy.md). Never commit or share a real API key.

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
