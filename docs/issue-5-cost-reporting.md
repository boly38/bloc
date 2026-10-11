# Issue #5: Telegram cost reporting

**Issue:** [Show the cost of an exchange or session on demand](https://github.com/boly38/bloc/issues/5)
**Status:** Analysis and implementation plan; no bridge changes have been made.

## Analysis

The Telegram bridge sends prompts to OpenCode with `POST /session/{id}/message`,
but currently extracts and returns only the response text. The OpenCode server API
returns a message object with `cost` and token usage fields. Opper's compatible chat
API also documents cost and usage information. The bridge can therefore report costs
without estimating them, provided the Opper cost survives the OpenCode provider/API
path used by this project.

That compatibility is the main technical risk. It must be tested against a real
exchange before showing a monetary value. If OpenCode returns no cost or a value that
does not match Opper's reported charge, do not derive an estimate from a static model
price table; stop and choose a verified source for the actual cost.

### Proposed user behavior

- By default, append the latest exchange's cost in USD (`$`) to every normal LLM
  answer. Do not include daily or session totals in normal answers.
- `/cost` reports both the current day's cumulative cost and the current OpenCode
  session's cumulative cost. Include token counts when OpenCode exposes them.
- `/cost-show yes|no` enables or disables the per-answer exchange-cost display. The
  default is `yes`; this setting is per Telegram chat.
- Daily totals include only requests handled by this bridge, are isolated per
  allowlisted Telegram chat, and roll over at 00:00 UTC.
- If verified cost data is absent, say it is unavailable rather than estimating.

This behavior combines the existing issue scope (`/cost`, session and exchange costs)
with the requested daily total and per-answer display.

## Implementation steps

1. **Add opt-in local diagnostics.** Allow a developer to enable diagnostics while
   running the bridge locally, so a controlled Telegram exchange can reveal which
   cost and token fields are actually available. Gate this with an environment
   variable such as `TELEGRAM_COST_DIAGNOSTICS`, defaulting to `false`. When enabled,
   append sanitized metadata to a shareable file under `/data` (for example,
   `/data/cost-diagnostics.jsonl`); when disabled, write no diagnostic file. Record
   only the OpenCode/Opper response structure and relevant cost, token, model and
   provider fields—never API keys, request/response text, Telegram chat IDs, OpenCode
   session IDs, or other sensitive identifiers. The developer can then share this
   diagnostic file for analysis.
2. **Verify the cost source.** Use the local diagnostics for a controlled authorized
   request and compare the reported cost and currency with the corresponding Opper
   usage record. Verify how a multi-step OpenCode answer is represented. Do not log
   secrets or conversation contents.
3. **Retain usage in the bridge.** Refactor response handling so it returns the text
   plus validated cost/token metrics from the OpenCode message. Treat missing,
   malformed, or unverified monetary data as unavailable; never estimate it.
4. **Persist cost state.** Store per-chat, per-UTC-date totals and the per-chat
   `/cost-show` preference in the existing Telegram `/data` volume, using an atomic
   file update like the session mapping. Count a successful exchange once, including
   protection against replayed Telegram update IDs after a bridge restart.
5. **Implement Telegram reporting.** Add `/cost` and `/cost-show yes|no` handling
   before forwarding prompts to OpenCode. Append only the exchange cost to normal
   answers when that chat's setting is enabled. Keep daily, session, and global Opper
   project totals distinct.
6. **Test and document.** Add dependency-free unit tests for diagnostics being disabled
   by default, sanitized diagnostic output when enabled, cost extraction, missing
   cost, the default-on display, both `/cost-show` settings, per-chat isolation, daily
   rollover, persistence, duplicate updates, and both commands not invoking the LLM.
   Document the commands and diagnostic environment variable in `README.md`.
7. **Run integration checks.** Run the Python compile check and `git diff --check`,
   then test through the deployed Opper/OpenCode stack and compare a real response
   against Opper's usage record before considering the feature validated.

## Risks and safeguards

- OpenCode may calculate its own model cost or omit Opper's reported cost; runtime
  compatibility is not assumed from field names alone.
- A daily total is per Telegram chat and covers only bridge traffic, not all usage on
  an Opper project/API key. UTC is the proposed day boundary.
- Persist totals and deduplication state without storing prompt/response content or
  exposing chat/session identifiers in Telegram replies or logs.
- Commands must remain behind the existing chat allowlist.

## Verification references

- [OpenCode Server API](https://opencode.ai/docs/server) — session message endpoints.
- [OpenCode generated message types](https://github.com/anomalyco/opencode/blob/dev/packages/sdk/js/src/gen/types.gen.ts) — message cost and token fields.
- [Opper Chat Completions API](https://docs.opper.ai/v3-api-reference/compatibility/chat-completions) — compatible response cost and usage.
- [Issue #5](https://github.com/boly38/bloc/issues/5) — acceptance criteria and intended `/cost` behavior.
