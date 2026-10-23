# Provider

Opper AI is the default provider configured in BLOC. Its OpenAI-compatible endpoint and
provider settings are defined in [`bot/opencode.json`](../bot/opencode.json). The API key
comes from `OPPER_API_KEY`; it is passed to the OpenCode container and is not stored in
that configuration file.

OpenCode supports many providers and local models. BLOC enables both Opper AI and GitHub
Copilot in its provider configuration, while keeping Opper as the default. See the
official [OpenCode provider documentation](https://opencode.ai/docs/providers/) for the
complete provider directory.

## Default: Opper AI

Set the key in the ignored local `.env` file. The defaults are:

```dotenv
OPPER_MODEL=gpt-5.6-luna
OPPER_VARIANT=medium
```

The resulting OpenCode model identifier is
`opper/${OPPER_MODEL}#${OPPER_VARIANT}`, by default
`opper/gpt-5.6-luna#medium`. Use a model identifier available in your Opper project and
do not add the `openai/` prefix to `OPPER_MODEL`.

## Alternative: GitHub Copilot

GitHub Copilot authentication is interactive and is not configured with an API key in
`.env`:

1. Start BLOC with `make config` and `make start`.
2. Open the trusted OpenCode TUI with `make tui`.
3. Run `/connect`, select **GitHub Copilot**, then open
   [github.com/login/device](https://github.com/login/device) and enter the displayed code.
4. Run `/models` and select a model available with your Copilot subscription. Some models
   may require a Copilot Pro+ subscription.
5. Set the selected model's complete identifier in `.env` as `OPENCODE_MODEL`. The value
   must use the provider/model form shown by OpenCode, for example
   `github-copilot/<model-id>`; do not guess the model ID.
6. Run `make config` and `make restart`.

The authentication is stored in the persistent `opencode-data` volume. Do not run
`make clean` when switching between restarts if you want to keep the Copilot login.
Telegram uses the same `OPENCODE_MODEL` value through the OpenCode server.

Copilot and Opper have different model catalogs, subscription limits, and billing. The
Copilot example is therefore intentionally model-agnostic: select a currently available
model with `/models` rather than copying an outdated model name.

After changing provider settings, validate and restart:

```sh
make config
make start
make health
```

For Coolify, configure these values as resource environment variables; see the
[Coolify deployment guide](coolify_deploy.md). Never commit or share a real API key.
