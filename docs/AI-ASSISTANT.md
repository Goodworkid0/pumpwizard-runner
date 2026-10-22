# Strategy Assistant

The browser trade header is a unified **AI Command Board**. On boot it checks the
local Runner, diagnostics, configured Solana RPC, authorized wallet, retained
market data, session records and assistant configuration, then keeps any missing
or failed service visible in the terminal feed. The RPC check verifies mainnet
identity and a current blockhash. A configured AI provider is reported as ready
without contacting it: saving a key or booting the board spends no provider
tokens.

After a provider key is available, the same board checks for local conversation
history. If it finds any, it asks the user to select and read a previous chat or
start fresh; it never silently attaches the latest conversation. Reading history
is local and does not contact the provider. The board then exposes New chat and a
message field. Submitting a message creates a frozen request preview; the provider
is contacted only after **Approve & send once**.
Provider requests for bounded local context pause for a second approval in the
same board. Session-monitor events remain visible beside diagnostics and chat.
Provider connection does not start a session, generate monitor evidence or
authorize execution. The browser uses the local assistant storage/API and keeps
the approval boundaries documented below.

In the native desktop app, the assistant stays available while you use Market, Strategies
or Settings. Its **Chat history** picker, New chat and Delete controls stay inside
the assistant. **Setup** opens one connection modal: select OpenAI API,
Anthropic Claude, DeepSeek API, Local Ollama or a compatible Chat API, choose a
model and enter an API key. Remote
endpoints require HTTPS; loopback HTTP works for a local compatible server.
Saving a connection sends no API request. The app does not supply an AI
subscription or key. Existing API-key environment-variable profiles continue to
work. A changed provider or model begins a separate local conversation, so older
messages are not attached automatically.

The OpenAI picker offers **GPT-5.6 Terra** for regular analysis, **GPT-5.6 Sol**
for harder review, **GPT-5.6 Luna** for cost-sensitive work, and **GPT-5.2** as a
Chat Completions fallback. Claude offers **Sonnet 5**, **Opus 5.5** and **Haiku
4.5** model IDs through Anthropic's native Messages API. DeepSeek offers
**DeepSeek Flash** and **DeepSeek V4 Pro** through its official Chat Completions
API. Local Ollama and compatible endpoints accept a model ID available to that
server. Selecting a model sends no API request; current model availability and
billing are determined by the user's provider account.

An entered key defaults to memory only, until the app closes. **Remember API key**
encrypts it with AES-GCM and a separate scrypt-derived AI-key password; it is unlocked
once per app session. This password is separate from the wallet password. Replacing
a saved key with a session-only key removes the previous saved copy. Removing a
profile removes its saved key. Keys are never included in model messages. Redirects
are rejected before a credential can be forwarded to another endpoint.

From **Market → Coin details → Ask Assistant**, the app stages up to 50 retained
samples for the exact selected mint. A data/token review opens before any API
request or saved-key unlock. Declining sends nothing. Unsent drafts and pending
requests must be finished first; they are never silently replaced. The provider
receives the frozen preview plus bounded conversation history only after approval.

## Conversation and cost controls

- **Send** authorizes one provider request with the recent conversation. No market
  or strategy records are attached automatically. Ctrl+Enter also sends.
- History is saved per profile in `assistant-history.sqlite` in the app data
  directory. It is local plaintext with owner-only file permissions on Unix, not
  encrypted by the AI-key password. Do not type secrets into chat. New chat and
  Delete manage conversations; deleting a profile retains its history on disk.
- The request includes at most 12 recent user/assistant messages, at most 8,000
  characters each and 24,000 UTF-8 bytes combined. Older messages stay local.
  The assistant does not have unlimited memory. Model answers may summarize
  previously approved evidence; raw tool results are not automatically resent.
- Requests estimated above 3,000 input tokens pause for review. The estimate is
  payload bytes divided by three, rounded up; it is a rough prompt-size estimate,
  not a tokenizer, price quote or guaranteed billing cap.
- Select an output cap (512, 1,200, 2,400 or 4,096 tokens). It is sent as the
  provider's token-limit parameter. Actual usage is shown when the provider
  returns it; missing usage is never presented as zero. No dollar pricing is inferred.
- Every proposed extra strategy/coin read pauses before another provider request.
  **Review request** shows the provider/model, estimated total input, output cap,
  reason, and exact additional data. The bounded conversation is also included.
  **Approve & send once** authorizes only that frozen request. **Decline** sends
  nothing further. Approval expires after five minutes and cannot be reused.
- Provider tool requests cannot approve themselves. Further reads require another
  review. There are no automatic retries, background chat loops or cumulative
  budget guarantees. A timed-out request may already have been billed.

## Available evidence

### Selected evidence explanations

The evidence assistant can also inspect one explicitly selected retained
campaign, campaign report, research report, decision, holding or receipt. The
selector is an exact record ID, plus the owning session/report ID where needed;
it is not a search over wallet history or files. A local read returns
`pumpwizard.evidence-assistant-context/v1` with the selected record's content
digest, retained engine/input identity, as-of time, evidence class, typed facts
and units, gaps, ownership, attribution, blockers and local source links. The
encoded context is capped at 48,000 bytes, 160 facts, 40 sources and 100 gaps or
blockers.

Deterministic local explanations cover recorded decision reasons, known cost
impact, retained cash/holding rankings, holding valuation reliability, receipt
status and missing evidence. They work without an AI provider and identify
their exact supporting fact and citation IDs. Missing values remain unknown. A
stale quote cannot produce a current holding value, a stale report cannot
establish a current leader, old replay engines require revalidation, and
external deposits/withdrawals are excluded from strategy profit.

Optional AI uses the existing conversation and one-use approval path. Its
preview includes the exact bounded history, context, local explanation,
provider/model, request digest, estimated input and output cap. Estimated and
actual dollar costs remain unknown unless a separate trusted pricing source is
implemented; provider-reported token usage is stored separately after the
request. The selected context is rebuilt immediately before sending. If its
material digest or the active profile/model changed, the approval is rejected
and a new preview is required.

Evidence AI requests expose no tools. The response must use the bounded
`pumpwizard.evidence-ai-candidate/v2` shape. A provider may select a retained
fact only by repeating its exact fact ID, subject, metric, unit, sign, as-of
time, horizon, evidence class, temporal scope and complete citation set. Runner
then renders the visible sentence from that retained fact; it never displays or
accepts provider-authored prose as a supported statement. Currency, subject,
metric, sign, time, evidence-class and citation swaps are withheld. Unsupported
rankings, advice, forecasts and profitability language are also withheld rather
than gaining support from a nearby citation. The reviewed response uses
`pumpwizard.evidence-ai-reviewed-response/v2`.

Holding contexts additionally expose
`pumpwizard.holding-valuation/v1`. Quantity, remaining basis, retained mark,
estimated current value, unrealized result, realized proceeds, fees and external
flows remain separate typed states. A mark is current only when its retained
observation is no more than 120 seconds old, or a slot-attributed public price is
no more than 5,000 slots behind the wallet read. Missing, stale and future quote
times keep current value unknown. A retained mark is always a display estimate,
never an executable liquidation quote; a fresh reviewed sell quote and its costs
are required for the latter. Missing entry FX never becomes a USD basis, exit
costs are not silently included and deposits/withdrawals are excluded from
strategy profit. Purchase attribution is separate from current management, so a
session-purchased position handed back to the user remains historically
session-purchased but is labeled user-managed.

Labels, notes and provider output remain untrusted data and cannot add wallet,
signer, order, shell, file or remote-fetch authority. Typed selection proves only
that the displayed statement matches a retained fact. It does not establish
market completeness, causation, a forecast, strategy edge or model accuracy.

In ordinary chat, the model can propose only `request_context`, with these local
reads (selected-evidence AI requests expose no tools at all):

| Read | Source and bound |
| --- | --- |
| Strategy search | Imported packages and bundled/cached catalog; up to 10 matches |
| Strategy details | Up to 3 exact packages, configuration identity, compatibility and available catalog simulation summary |
| Coin history | Up to 100 retained market-watch samples for a mint or symbol: observation time, price, market cap and liquidity |

All queries are local. Preview generation does not consume provider tokens or
fetch the website. Each returned dataset is limited to 24,000 UTF-8 bytes and each
complete API payload to 64,000 bytes. Oversized requests are rejected, with a
request to narrow the query. At most three proposed reads can be reviewed together.

Coin data is **recorded local snapshots**, not a complete historical candles/trades
feed. Coverage depends on previous local market scans; empty data means no matching
retained observations. Symbols can match multiple mints; use the mint for precision.
No fresh market-data API integration is added by this feature. Strategy snapshots
retain their timestamp and simulated/untested evidence status. Eligibility for
reviewed execution is not funded acceptance.

The assistant has no wallet, signing, order, settings-write, shell, arbitrary-file
or browsing tool. It cannot authorize execution. Imported/model text is untrusted;
local capability and approval checks enforce the available operations independently
of the model's instructions.

## Compatibility and validation boundary

OpenAI, DeepSeek, Ollama and custom endpoints use the Chat Completions
function/tool-message flow. OpenAI endpoints receive `max_completion_tokens`;
DeepSeek, Ollama and custom compatible endpoints receive `max_tokens`. DeepSeek
requests explicitly use non-thinking mode so a reviewed local-context tool result
does not need hidden provider reasoning replayed on the next request.

Claude uses Anthropic's native `POST /v1/messages` contract. Runner sends the
system prompt as the top-level `system` field, authenticates with `x-api-key` and
`anthropic-version`, converts `request_context` to an Anthropic `input_schema`,
and converts reviewed results to `tool_result` blocks. The official Anthropic
host is required for this protocol; a custom `/v1/messages` host is rejected so
a credential cannot be sent under an assumed provider format.

Provider/model support varies. Turn off **Request local data** for a compatible
chat-only endpoint that does not support tools. Models requiring another API or
request shape are not established compatible.

Automated tests and the desktop preview use a synthetic provider, throwaway local
data and blocked live sockets. They validate the request protocol, approval gates,
history and credential storage; they do not establish paid-provider/model quality,
real-world token billing, every local server's compatibility, or funded trading.
A live paid-provider smoke test remains user-initiated after adding a profile/key.

No strategy handoff/export schema changed. The website session needs no new shared
contract for this desktop feature. Its cached evidence remains a snapshot until
explicitly refreshed through the existing catalog workflow.
