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
