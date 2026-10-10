## Requirements

- Make OpenCode accessible.
- Deploy to a VPS through Coolify using Docker Compose.
- Automatically start OpenCode after deployment and make it queryable through WhatsApp
  or Telegram, whichever is simpler.
- Provide access to a repair container if the main Compose stack fails to start.
- Minimize implementation code and, where possible, rely on popular or actively
  maintained open-source projects.

- Build an MVP.
- Make the MVP runnable locally so a developer can test it.
- Use `bloc` as the Compose project name wherever possible.
- Use `bloc-v0` as the MVP branch name.
- Prefer environment variables for configuration.
- Use an ignored `.env` file and a `.env.template` documenting expected configuration
  and secrets (API keys, webhook tokens, etc.).

- Keep the MVP minimal while meeting the requirements.
- The LLM must not intuit or guess answers. It must rely on either (1) a test using an
  official binary, (2) verified official online documentation, or (3) a clarification
  from the human, in which case processing pauses.
- If any part of this specification is unclear, the LLM must ask the human to clarify
  before proceeding.
- The project is documented in English, and the root README is the primary entry point.
- Minimize token usage throughout the project.
