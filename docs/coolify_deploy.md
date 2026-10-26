# Coolify deployment

Configure the BLOC 2.x Coolify resource as follows.

- **Base Directory:** `/bot`
- **Docker Compose Location:** `/docker-compose.yml`
- Add the values from [`.env.coolify.template`](../.env.coolify.template) as resource
  environment variables. Keep `COMPOSE_PROFILES=telegram` to enable the bot.
- Set a strong, unique `OPENCODE_SERVER_PASSWORD`; the Telegram bridge authenticates to the
  OpenCode v2 API with it.
- Set `OPENCODE_MODEL` to the chosen `provider/model#variant` when using a provider other than
  the default Opper model. Leave the variant suffix off when using the provider default.
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

Provider authentication is deployment-specific. Connecting on the local machine does not
authenticate the Coolify instance. After deployment, use the remote TUI below to run `/connect`
and `/models`; credentials are stored in the remote OpenCode data volume.

## Remote TUI

From a trusted operator workstation with SSH and Docker access to the VPS, list running
containers and find the OpenCode container ID in the first column:

```sh
make remote-tui HOST=coolify
```

Then pass that ID to attach the TUI:

```sh
make remote-tui HOST=coolify CONTAINER_ID=0123456789ab
```

Replace `coolify` with your SSH host or alias and the example ID with the value shown by
`docker ps`. Docker access on the VPS is equivalent to operator-level access; use this only
from trusted administrator accounts. This workflow does not publish the OpenCode API.

## Moving from BLOC 1.x to BLOC 2.x

Deploy BLOC 2.x as a separate Coolify resource. It uses a separate Compose project and volumes;
do not configure it to reuse the BLOC 1.x resource or volumes. No v1 data or backup migration is
supported:

- OpenCode sessions and provider credentials;
- workspace files;
- Telegram bridge state;
- backups created by BLOC 1.x.

Keep the BLOC 1.x resource and its data if you still need access to them. BLOC 1.x backups cannot
be restored into BLOC 2.x.
