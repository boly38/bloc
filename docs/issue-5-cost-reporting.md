# Issue #5: Telegram cost reporting

**Issue:** [Show the cost of an exchange or session on demand](https://github.com/boly38/bloc/issues/5)
**Status:** Runtime cost fields have been compared against the Opper portal for simple
and tool-using exchanges. Cost reporting is implemented from OpenCode session message
history, including child sessions.

## Analysis

The OpenCode server returns cost and token usage per assistant message. A tool-using
exchange can generate multiple assistant messages, so the bridge compares message IDs
before and after a request and sums every new assistant message, including messages in
child sessions. This was compared with user-observed Opper portal increments: a
Swedish greeting reported `$0.00044896` against a rounded `$0.0005` increase; a search
exchange with two assistant messages summed to `$0.00197112` against `$0.0021`.
`step-finish.cost` duplicates `info.cost` in observed responses and is not added again.

### User behavior

- Append a short cost line in USD to each authorized normal answer.
- `/cost` reports the current chat's OpenCode session total and its total for the
  current fixed UTC hour. The report is not a project-wide Opper total.
- Show cost subtotals by OpenCode agent when the API identifies them; otherwise show
  only the total.
- Include token totals in `/cost` when available.
- If cost or timestamp data is incomplete, report it as unavailable rather than
  estimating or displaying a partial total.

## Implementation steps

1. **Add opt-in local diagnostics.** Done. Set `TELEGRAM_COST_DIAGNOSTICS=true` in
   `.env`, restart the bridge, send one authorized test request, then read the
   sanitized output with `make diagnostics`. Disable the flag after testing. The
   diagnostics append to `/data/cost-diagnostics.jsonl`, include cost/token metrics
   for each new assistant message in the exchange, and omit prompts, response text,
   Telegram chat IDs, OpenCode session IDs, and credentials. The bridge reads session
   message history before/after exchanges for cost calculation whether diagnostics are
   enabled or not; this does not send additional LLM prompts.
2. **Verify the cost source.** Done with authorized simple and tool-using exchanges,
   comparing the sum of assistant message costs to rounded Opper portal increments.
3. **Aggregate OpenCode history.** Done. Read the current session and child-session
   message histories before/after each exchange; sum only new assistant-message
   `info.cost` values. Use the same history for session and current-UTC-hour reports.
4. **Implement Telegram reporting.** Done. Append the exchange total and available agent
   subtotals to normal responses. Handle `/cost` before forwarding to OpenCode; report
   session and current UTC hour totals per chat, plus tokens when available.
5. **Test and document.** Done locally with dependency-free tests for multi-message
   aggregation, child sessions, missing cost/time, agent subtotals, UTC-hour boundaries,
   chat isolation, and `/cost` not invoking the LLM. Document the command in `README.md`.
6. **Run integration checks.** Run local validation and ask for Telegram/Opper checks
   after deployment. Do not claim exact portal parity beyond its displayed precision.

## Risks and safeguards

- OpenCode message costs were observed to track Opper portal increments to the
  portal's displayed precision; the bridge reports OpenCode's values without estimates.
- Session/hour totals cover the current Telegram chat's OpenCode session tree, not all
  sessions or usage on an Opper project/API key.
- Never include prompt/response content or chat/session IDs in diagnostics, replies,
  or error logs.
- Commands must remain behind the existing chat allowlist.

## Verification references

- [OpenCode Server API](https://opencode.ai/docs/server) — session messages and child sessions.
- [Opper Chat Completions API](https://docs.opper.ai/v3-api-reference/compatibility/chat-completions) — compatible response cost and usage.
- [Issue #5](https://github.com/boly38/bloc/issues/5) — acceptance criteria and intended `/cost` behavior.
