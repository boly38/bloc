# Coolify deployment

The Compose deployment has been tested with Coolify. Configure the resource as follows:

- **Base Directory:** `/bot`
- **Docker Compose Location:** `/docker-compose.yml`
- Add the values from [`.env.coolify.template`](../.env.coolify.template) as resource
  environment variables. Keep `COMPOSE_PROFILES=telegram` to enable the bot.
- Set real credentials and allowed chat IDs; discover the first ID locally with
  [`make telegram-chat-id`](quickstart.md#configure-and-start) before enabling Telegram.
- Keep the pinned `OPENCODE_IMAGE` digest unless a replacement has been qualified.

Store credentials in Coolify's environment configuration, not in the repository. The
Compose file does not publish the OpenCode port or configure a public route. Check the
resource's domains, port mappings, and proxy settings as well: platform-level routing is
outside this repository's control and must not expose the API.

For provider settings, see [Provider](provider.md); for trust boundaries, see
[Security](security.md). The local TUI requires an operator shell with Docker access to
the deployment.
