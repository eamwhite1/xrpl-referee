# Changelog

All notable changes to the AgentTrust MCP server and REST API.

Protocol version header: `X-AgentTrust-Version: 0.1.0` on all responses since v2.0.0.

---

## [7.0.14] - 2026-11-21
Money-path fixes from the 21 Nov audit.
### Fixed
- A PASS marks the vault RELEASED before the EscrowFinish is sent. If the process restarted in that window nothing ever sent it: the stuck-release sweeper only handles RELEASING and the hash backfill only records a finish that already exists, so the worker was not paid and the funds returned to the buyer at the cancel time. The backfill now re-sends the finish for a RELEASED vault that has no EscrowFinish on the ledger once the vault is 10 minutes old (at most 5 release attempts). Repeating a finish is safe: a second one fails with tecNO_TARGET and is resolved from the ledger history. It does not retry when the finish exists, when the escrow was cancelled, or while the original finish may still be in flight (T48)
- One `/evaluate` at a time per vault. Parallel submissions with the same token all passed the attempt check together, so a worker could exceed `max_submissions` (extra attempts are paid for) and run AI calls in parallel. The second call now gets HTTP 409 `evaluation_in_progress` with `Retry-After: 15`. The marker is cleared on every exit and expires after 5 minutes if the process dies mid-evaluation (T49)
- New column `escrow_vault.evaluating_since` with its migration; every `EscrowVault` column is now checked against the migration list (T50)
### Behaviour change
- A second `/evaluate` for a vault that is already being evaluated returns 409 instead of running in parallel

## [7.0.13] - 2026-11-21
Security and robustness fixes from the 21 Nov audit.
### Security
- One guard for every outbound request to a URL a user can influence (evidence links and evidence-criteria URLs, vault / new-bid / award / `/evaluate` `callback_url` webhooks, domain `xrp-ledger.toml` lookups). It resolves the host, refuses unless every address is publicly routable (loopback, private, link-local/cloud metadata, 100.64/10, IPv4-mapped / 6to4 / NAT64 IPv6, decimal and hex IPv4 forms), connects to the checked address (so DNS cannot change between the check and the connection), re-checks every redirect hop and blocks ports such as 22, 25 and 6379. The bid, award and `/evaluate` callback webhooks previously had no check at all (T38, T39, T40)
- Rate limits and the MCP log use the last X-Forwarded-For hop (the one Render's proxy appends), not the client-controlled first hop, so the bootstrap and wallet-verification limits cannot be bypassed by rotating a header (T42)
- A wrong escrow id on `/evaluate` no longer loads the whole escrow table and writes every escrow id to the log (T41)
- Errors returned for failed XRPL/Base node calls name only the exception class; the node URL, which can embed an API key, is no longer returned or logged (T44)
- Work text is capped at 100,000 characters (HTTP 422 above that), bounding the per-call AI cost (T46)
### Fixed
- Best-effort database lookups on request sessions (KYC, fee check, wallet trust score signals, job escrow proof, seller ownership, issuer lookup, escrow-hash backfill) no longer leave a Postgres transaction aborted after a failed statement: the session is rolled back and the failure is logged. Before, a later signal or the commit could fail silently or return a 500 (T43)
- In-memory rate-limit tables are bounded and evict expired keys first (T45)
### Docs and discovery
- Added `/.well-known/security.txt`; the repo's `agent.json` and `openapi.json` now carry the app version (checked every release); removed the dead `smithery.yaml` (T47)
### Behaviour changes
- Webhooks to non-public addresses are no longer delivered; `/evaluate` rejects `work` over 100,000 characters

## [7.0.12] - 2026-11-18
### Fixed
- The AI referee now judges only the worker's raw deliverable. Before, the "NFT PROOF VERIFIED" notes were appended to the work it was given, so any proof gate plus AI on an exact-output task (e.g. "HELLO AGENT") could FAIL correct work as "including unrelated blockchain metadata". Proof gates are decided deterministically and their notes no longer enter the AI prompt, the evidence collection or the cache key (T34)
- The testnet AI result cache is keyed on the raw work, so the same work with different proof-note text is a cache hit (T35)
- `/status` and `/health` `free_ai.calls_today` reads 0 when the in-memory count was last written on an earlier UTC day, instead of showing yesterday's number; still no database access (T36)
- `models_used` on the vault now reflects the latest verdict. A FAIL followed by a PASS no longer leaves `["code-gate"]`: an AI PASS records the model, a deterministic PASS records `none (deterministic gates)` (T37)

## [7.0.11] - 2026-11-17
### Fixed
- Vaults with an NFT proof gate no longer return HTTP 500 on /evaluate. The registry lookups (`nft_issuer`) in `evaluate_work` ran on the request's own database session inside `except: pass`; on Postgres a failed statement aborts the transaction, so the later commit failed with `InFailedSqlTransaction`. Both lookups now use their own short read-only session and log `nft_issuer lookup failed: <ExceptionType>` (T29)
- Root cause of the failed lookup: the `NftIssuer` model has a `wallet_addresses` column that no migration ever created, so the query raised an undefined-column error on the live table. `run_migrations` now adds `nft_issuer.wallet_addresses`; every other model column was already migrated (T33)
- A vault that requires an NFT but names no issuer no longer fails with an unassigned-variable error (T27b)
- Code gates (`exact_match`, `sha256_of`, `regex`) check the worker's raw work. Previously the proof notes were appended first, so they could never pass once a proof gate had passed (T27, T28)
- NFT and domain proofs query the vault's own network: testnet vaults use `XRPL_TESTNET_URL`, mainnet vaults `XRPL_URL`. Bithomp, which indexes mainnet only, is skipped off mainnet (T30)
- `nft_wallet` must be the vault's worker or buyer wallet, otherwise HTTP 400 `nft_wallet must be the worker or buyer wallet` with no attempt used. Before, a worker could cite any wallet's NFT (T31)
- MCP `evaluate_escrow_work` accepts `nft_token_id`, `nft_wallet` and `vc_jwt` and forwards them, so MCP agents can satisfy NFT and VC proof gates (T32)

## [7.0.10] - 2026-11-14
### Changed
- `checks_run` uses stable names: `proof_nft` / `proof_domain` / `proof_vc`, `code_<gate>` (e.g. `code_exact_match`, once per gate type) and a single `ai_referee` when the AI judged the work (T2, T8, T26)
- `/testnet/demo` returns `ai`, `checks_run` and `criteria_results` on PASS/FAIL, not only on HELD (T26)

## [7.0.9] - 2026-11-14
### Fixed
- /try lock: wait (up to 20 s) for both faucet-funded test wallets to appear on a validated ledger before building the EscrowCreate. The faucet returns 200 when it submits the funding, not when it is validated, so a quick lock failed with a 502 (L1, L2)
- If the wallets still aren't funded, /try lock returns 503 `wallets_not_ready` with a retry message, submits nothing, and keeps the session usable (L3)

## [7.0.8] - 2026-11-14
### Changed
- evaluate_work: code gates always run first on a private copy of the criteria; a gate FAIL consumes the attempt and makes no AI call or free-AI DB write (T1, T22)
- AI is skipped only when every check is a deterministic proof/code gate and the AI box is off; PASS then reports `model_used "none (deterministic gates)"` (T8, T9)
- AI selected with code-only criteria: a synthetic `task` criterion with the verbatim server task is judged too, and every criterion must PASS (T2, T3, T10)
- Every evaluate_work result now has a string `verdict` (PASS/FAIL/HELD/INCONCLUSIVE), `verdict_detail` (the old dict), `checks_run`, `ai` and `criteria_results` with `type` (T2, T4, T9)
- Testnet: AI selected but capped/disabled/unavailable (incl. 500, timeout, bad JSON, MAX_TOKENS, missing criterion) returns HTTP 200 HELD; nothing released, attempt not consumed (T5, T6, T7)
- Mainnet: provider failure or a missing AI criterion returns 503 and never PASS (T10)
- Testnet free-tier call: TRY_AI_MODEL only (no Pro, even on INCONCLUSIVE/attempt 3), `maxOutputTokens` 512 (env TESTNET_AI_MAX_OUTPUT_TOKENS), `thinkingConfig.thinkingLevel "minimal"` (env TESTNET_AI_THINKING_LEVEL), 15 s timeout, one attempt (T14)
- `/try` submit: one AI call at most; response `verdict` PASS/FAIL/HELD/ERROR, `criteria` typed code/ai, `ai.status`; release fields only on PASS; the require_ai_audit UPDATE runs only when the box changes (T1, T2, T8, T22)
- Testnet evaluate path caches valid PASS/FAIL AI results (never errors or INCONCLUSIVE); a hit makes no provider call and no guard DB statement and reports `ai.status "cached"` (T18)
- Free-AI guard: reservations carry their own day/path; completion is one UPDATE, awaited in `finally` on every outcome, and records tokens/failures (T2, T7, T19)
- Per-client limits use the last X-Forwarded-For hop (5/hour, 20/day); public POST /evaluate passes it through (T17)
- Daily cap is atomic under concurrency and survives a process restart (T15, T16)
- Previous UTC day's summary is logged once on the first reservation of a new day, with no IP or hash (T19)
- `/testnet/demo` returns 200 HELD with no release when the free AI is capped (T12)
- Testnet challenge review: one free-tier call, no retry, at most one review per vault; capped gives REVIEW_INCONCLUSIVE (T13)
- `/stats/free-ai`: rate-limited, 503 when the DB is unavailable, zero-filled days, never returns `ip_` rows (T21)
- `/status` and `/health` stay DB-free; `calls_today` is an int (T20)
### Removed
- `_try_ai_judge`, `_free_ai_allow`, `_free_ai_record` (T25)
### Other
- Version 7.0.8 in referee.py and server.json (T24); domain normalisation regression (T23)

---

## [7.0.7] - 2026-11-09
### Changed
- Unified verification pipeline: proof gates → code criteria → AI; gates always run first
- HELD status when AI selected but unavailable/capped — no silent fallback
- Durable atomic free-AI: free_ai_usage DB table, _free_ai_reserve()
- Testnet AI: Flash-only, maxOutputTokens=768, 15s timeout, no retry
- /status calls_today is now an int + "commit" SHA field; GET /stats/free-ai endpoint
- _normalise_domain hardened (strips repeated schemes, validates chars)
### Fixed
- Double AI call on /try (was _try_ai_judge + evaluate_work; now evaluate_work only)
- Release before AI verdict on /try
- calls_today was returning the date string not an integer
- Code criteria skipped when proof gates pass + require_ai_audit=False (mainnet behaviour change)
- AI was no-op on code-only criteria with require_ai_audit=True (mainnet behaviour change)

Note: F3/F4 fixes, _try_ai_judge removal and evaluate-path caching listed above actually landed in 7.0.8.

---

## v7.0.5 — 2026-11-03

### Changed
- Release sweep is now deadline-driven so Neon can scale to zero; no user-visible timing change.
  - `_release_sweep_loop` wakes every 60 s but only queries the DB when a deadline is actually due.
  - `_compute_next_sweep_due` computes the earliest pending deadline in a single DB session.
  - `_note_sweep_due` is called at each state-change site to update the in-memory timer immediately.
  - Startup, DB recovery, and hourly window tick all refresh the timer from the DB as a backstop.
- `X-AgentTrust-Version` and `X-AgentTrust-Server` response headers now reflect `APP_VERSION` instead of a hardcoded string.
- Gemini Flash model updated to `gemini-3.5-flash-lite` (deprecation of `gemini-2.5-flash` Dec 1 2026).

---

## v3.0.1 — 2026-10-09

### Changed
- **XLS-56 live on XRPL mainnet**: `get_batch_dvp_payload()` docstring updated from "available from 9 October 2026" to "live on XRPL mainnet from 9 October 2026". The endpoint was already fully implemented in v2.9.0 — no code changes required.

---

## v3.0.0 — 2026-10-08

### Added
- **XLS-75 Permission Delegation (`delegate_address` on `POST /escrow/prepare` and `hire_and_pay`)**: buyers can let a hot agent key sign and submit EscrowCreate on their behalf without sharing their seed. Pass `delegate_address` to get a tx with a `Delegate` field — the agent signs it, `Account` stays as the buyer so ownership and cancellation rights remain with the buyer. The endpoint validates that a live XLS-75 delegate ledger object exists for (buyer → delegate) before building the tx.
- **`delegate_address` MCP parameter on `hire_and_pay()`**: first-class XLS-75 support with full docstring, `delegate_mode` flag in response, and clear "agent signs, not buyer" note.
- **DelegateSet guide in `get_wallet_setup_guide()`**: `optional_xls75_delegation` section covers setup code, revoke code, delegatable/non-delegatable tx types, spend-cap warning, and how to use with AgentTrust.
- **`telCAN_NOT_QUEUE` → HTTP 503**: clear "network busy, retry" message instead of opaque 400 in `POST /escrow/{id}/submit`.

### Changed
- `AccountObjects` added to xrpl-py imports for delegate ledger object validation.

---

## v2.9.1 — 2026-10-07

### Fixed
- **Batch DvP invalid signer (temBAD_SIGNER)**: inner `EscrowFinish` was using `referee_wallet.address` as `Account`, requiring a `BatchSigner` entry that was absent. Fixed by setting both inner tx `Account` fields to `vault.buyer_address` — anyone may submit `EscrowFinish` on a conditional escrow if they hold the fulfillment, so the Batch is single-account and needs no `BatchSigners`. `signing_pub_key=""` and `fee="0"` set explicitly on both inner txs per XLS-56 spec.
- **confirm_wallet_ownership (2a)**: now rejects transactions carrying a `Delegate` field (XLS-75 delegated txs could spoof ownership); requires `validated=true` and `meta.TransactionResult == tesSUCCESS`.
- **Enterprise linked-wallet verify (2b)**: now requires `tx.Account == wallet.xrpl_address`, no `Delegate` field, `validated=true`, and `tesSUCCESS` before accepting a challenge memo. Previously, any incoming tx with the challenge memo (including from a third party sending 1 drop) would pass.
- **confirm_escrow_tx (2c)**: now requires `validated=true`, `tesSUCCESS`, and `tx.Account == vault.buyer_address`. Prevents mismatched `Owner` in auto-finish when a delegated account creates the escrow.
- **fulfillment_note added to Batch payload response**: documents that the embedded fulfillment is safe to share — `EscrowFinish` can only release funds to the pre-set `Destination`.

### Changed
- `xrpl-py` pinned to `>=5.1.0` (first version with `Batch`, `DelegateSet`, `Transaction.delegate`).

---

## v2.9.0 — 2026-10-06

### Added
- **XLS-56 Atomic NFT DvP (`GET /escrow/{id}/batch-dvp-payload`)**: returns a pre-built `Batch` transaction (XLS-56 `TF_ALL_OR_NOTHING`) the buyer signs once to atomically accept the seller's NFT transfer and release escrow payment in a single ledger close. Requires vault to be in `PASS_AWAITING_NFT` state with an NFT sell offer registered. Both inner transactions (`NFTokenAcceptOffer` + `EscrowFinish`) succeed or both revert — eliminates the settlement gap in the legacy two-step DvP flow. Activates on XRPL mainnet 9 October 2026.
- **`get_batch_dvp_payload()` MCP tool**: exposes the XLS-56 Batch DvP endpoint as a first-class tool with buyer workflow instructions.
- **xrpl-py imports**: `NFTokenAcceptOffer`, `Batch`, `BatchSigner`, `BatchFlag`, `autofill` added to support XLS-56 Batch construction.

### Changed
- Legacy two-step NFT DvP flow (`/escrow/{id}/nft-offer` + auto-finish) remains fully operational for wallets not yet on XLS-56.

---

All notable changes to the AgentTrust MCP server and REST API.

Protocol version header: `X-AgentTrust-Version: 0.1.0` on all responses since v2.0.0.

---

## v2.8.0 — 2026-09-26

### Fixed
- **DEX schema drift (`get_dex_quote` MCP tool)**: MCP tool was sending `from_currency`, `to_currency`, `amount` to `POST /dex/quote` which expects `worker_address` and `xrp_amount`, causing HTTP 422 on every call. Updated tool parameters to match the REST endpoint.
- **Evaluate griefing (K1/A4)**: `POST /evaluate` accepted submissions from any caller who knew the `escrow_id`, allowing attempt-slot burning without being the awarded worker. Fixed by generating a one-time `evaluate_token` (URL-safe random, SHA-256 hashed) at `POST /escrow/generate` time. Hash stored on `EscrowVault.evaluate_token_hash`; plaintext returned to the buyer who shares it with the worker. `/evaluate` rejects requests with a missing or invalid token (HTTP 403) when the vault has a hash set. Existing vaults without the column value are unaffected (backwards compatible).
- **Debug endpoint RPC reliability**: removed `s1.ripple.com` and `s2.ripple.com` JSON-RPC sources from `GET /wallet/debug-age/{address}`. Production XRPL calls exclusively use `XRPL_URL` (default: `xrplcluster.com`).

### Added
- `evaluate_token` and `evaluate_token_hash` fields on `EscrowVault` (migration: `ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS evaluate_token_hash VARCHAR`).
- `evaluate_token` field on `AuditRequest` Pydantic model.
- `evaluate_token` parameter on MCP `evaluate_escrow_work` tool.

---

## v2.7.0 — 2026-09-26

### Added
- **Enterprise API Keys (`EnterpriseApiKey` model, `POST/GET/DELETE /enterprise/api-keys`)**: enterprise admins can issue named API keys from the dashboard. Agents authenticate as the enterprise account by passing the raw key in an `X-API-Key` request header. Keys are stored as SHA-256 hashes only — the raw value is shown once at creation. Revocation is immediate.
- **Escrow Templates (`EscrowTemplate` model, `POST/GET/PUT/DELETE /enterprise/templates`)**: enterprise accounts define job policy once — rubric, proof gates (NFT/domain/VC), amount floor and cap, deadline hours, AI audit flag, proof policy (ALL/ANY). Templates are referenced by `template_id` on `POST /escrow/generate`; the platform enforces all constraints and rejects out-of-range requests automatically.
- **`template_id` on `EscrowVault`**: vaults created from a template record the template ID. Added as a nullable column via migration (`ALTER TABLE escrow_vault ADD COLUMN IF NOT EXISTS template_id VARCHAR`).
- **`POST /enterprise/audit/{escrow_id}/abandon`**: authenticated enterprise users can mark a stuck LOCKED or PENDING vault as ABANDONED directly from the dashboard. Blocked for vaults with a live on-chain escrow transaction — those must use the cancel flow.
- **`_get_account_from_api_key` dependency**: resolves an enterprise account from the `X-API-Key` header (SHA-256 hash lookup). Updates `last_used_at` on each successful authentication. Used by agent-facing endpoints as an alternative to the Google OAuth session cookie.

### Fixed
- **Orphaned LOCKED vault poller**: LOCKED vaults with no on-chain transaction (`escrow_tx_hash` is NULL or empty string) are now unconditionally flipped to ABANDONED by `_poll_expired_escrows()`. Previous versions guarded on `escrow_sequence` (unreliable for pre-migration vaults) and time conditions (silently failed due to timezone/type mismatch). Fixed with `or_(EscrowVault.escrow_tx_hash == None, EscrowVault.escrow_tx_hash == "")` and no time condition.

### Changed
- **`_get_current_account` dependency placement**: the enterprise abandon endpoint is now defined after `_get_current_account` in `referee.py`, resolving a `NameError` on startup that caused deploy failures.

---

## v2.6.0 — 2026-09-20

### Added
- **`POST /wallet/scores` — batch trust scoring endpoint**: score up to 50 XRPL addresses in one call. Queries run in parallel; results returned ranked by score descending with a `rank` field on each entry. Fee: $0.10 (XRP or RLUSD on XRPL). Added to `/.well-known/x402` paid catalog. Use cases: candidate ranking, counterparty screening, agent directories, leaderboards.
- **`batch_wallet_trust_scores()` MCP tool**: exposes the batch endpoint as a first-class tool with full docstring and use-case guidance.
- **`callback_url` and `metadata` params on `create_escrow_vault` and `hire_and_pay`**: enables headless/white-label integrations where the integrator handles all user-facing surfaces. `callback_url` receives webhook POSTs on state changes (funded/PASS/FAIL/expired); `metadata` (JSON string) round-trips caller reference IDs (invoice_id, po_number, tenant_id) in every webhook payload.
- **`skill.md`**: distributable Claude Code `.claude/agents/` skill file covering the full AgentTrust workflow — wallet bootstrap, hiring flow, work submission, all four release condition modes, marketplace, trust/compliance, and key rules.
- **`/.well-known/agent.json` fully updated**: all 16 skills, proof gate capabilities, `callback_url`/`metadata` white-label params, fees table, x402 flag, correct provider org (Boxclever Media Ltd), and discovery links to MCP config, x402, marketplace, and OpenAPI.

### Changed
- **`GET /wallet/score/{address}` rate-limited**: 20 free requests per hour per IP (in-memory token bucket, 1-hour window). Requests exceeding the limit receive HTTP 429 with a message pointing at the batch endpoint.
- **Trust score reframed as open discovery infrastructure**: endpoint docstring, MCP tool description, and `wallet-score` page now position the API as "the open reputation layer for XRPL agents" rather than a pre-escrow tool. Aligns with Circle's call for open reputation indexes.
- **`get_wallet_trust_score()` MCP tool description updated**: notes rate limit, score bands, and pointer to `batch_wallet_trust_scores()` for multi-wallet queries.
- **`/.well-known/agent.json` inline fallback in `referee.py`**: fixed description "Trustless" → "Trust-minimized"; updated provider org and URL.
- **`serve_mcp_server_card` description**: "Trustless AI task verification" → "Trust-minimized AI task verification".

### Fixed
- `agent.json` root file was on schema v1.0/agentVersion 7.0 with only 3 skills — updated to v9.0/0.6 schema with all 16 skills and full capability flags.

---

## v2.5.0 — 2026-09-19

### Added
- **`purchase_extra_attempt()` MCP tool** — unlocks one additional work submission when the vault limit is reached ($0.05 fee). Previously only accessible as a bare REST endpoint; agents can now call it directly from `tools/list`.
- **Proof-gate params in `create_escrow_vault` and `hire_and_pay` MCP schemas** — `require_nft_proof`, `required_nft_issuer`, `nft_dvp`, `required_domain`, `required_vc_issuer_did`, `required_vc_type`, `proof_policy`, and `require_consensus` are now first-class tool parameters. Agents can discover and configure release conditions without reading the REST API docs.
- **`KYC_FEE_USD` / `kyc_fee_xrp()` constants** — KYC fee tracked at module level with live XRP pricing.

### Changed
- **x402 consistency across all paid endpoints:**
  - `/evaluate/purchase-attempt` (`$0.05`): `fee_hash` is now optional; omitting payment returns a proper `_raise_402` with `X-Payment-Required` (x402 v1) + `PAYMENT-REQUIRED` (x402 v2) headers and USDC/Base option. Previously returned 422 Unprocessable Entity.
  - `/kyc/start` (`$0.50`): replaced plain `HTTPException(402)` dict with `_raise_402` for full x402 v1+v2 envelope. Also wired up `verify_fee_payment` (supports x402 v2 presigned transactions) instead of the simpler internal `_validate_fee_hash`.
- **`/.well-known/x402` catalog expanded**: `paidEndpoints` now lists all six paid endpoints with per-entry `fee_usd` and description (was three entries, no descriptions). New `liveAmounts` block exposes current XRP equivalents for all fee tiers. Agents searching the x402 catalog now see the full AgentTrust paid surface including the premium consensus tier and extra-attempt path.
- **MCP system prompt**: `evaluate_escrow_work` description now states the 3-attempt default, `max_submissions` range (1–10), and the `purchase_extra_attempt` fallback. Onboarding guides (worker and buyer) now lead with `get_wallet_setup_guide()` as the recommended production path; `create_agent_wallet()` is clearly labelled dev/throwaway-only.
- **Coinbase framing**: system prompt, `fund_xrpl_wallet_via_coinbase` docstring, and `get_wallet_setup_guide` return value all now explicitly state Coinbase is an onramp only — all escrow settlement happens on XRPL. Removed a misleading "x402 autonomous payment" label from the wallet setup guide (the tool uses Coinbase HMAC API v2, not x402).

### Fixed
- `evaluate_escrow_work` docstring now documents the attempt limit and directs agents to `purchase_extra_attempt` on `submission_limit_reached` error.
- `fee_hash` description on `create_escrow_vault` no longer implies `create_agent_wallet` is the only path to the free tier.

---

## v2.4.0 — 2026-09-19

### Added
- **Premium consensus audit** (`require_consensus=true`): Gemini Flash evaluates first, then Gemini Pro independently reviews the same submission. Both must agree on PASS — disagreement returns FAIL with combined feedback from both models. Fee: $0.25 (vs $0.10 standard).
- `premium_audit_fee` field in `GET /fees` response with live XRP amount, RLUSD, and USDC payment options.
- Split-verdict details now include per-model feedback and a merged `criteria_failed` list so the seller knows exactly what to fix.

---

## v2.2.0 — 2026-09-15

### Added
- `criteria_met` and `criteria_failed` arrays now first-class fields in every `audit_task` and `evaluate_escrow_work` response. Each `criteria_failed` entry is specific and actionable — share with the worker before resubmitting.
- `05_mcp_tools.py` example: end-to-end MCP client flow with `get_fees()`, `assess_counterparty_and_job()`, and `audit_task()` verdict handling

---

## v2.1.0 — 2026-09-15

### Added
- `GET /fees` — machine-readable fee schedule, accepted assets, addresses, free-tier rules, and escrow caps. Single source of truth; agents should call this before any paid endpoint.
- `get_fees()` MCP tool — wraps `GET /fees`; listed first in system prompt
- `/fees` added to robots.txt Allow list

---

## v2.0.0 — 2026-09-15

### Added
- `X-AgentTrust-Version: 0.1.0` response header on all endpoints
- `protocol_version` field in `/status` and `/health` responses
- Issuer registry audit trail fields: `verified_at`, `verified_by`, `toml_url`, `accountset_tx_hash`
- `disputed` status for issuer registry entries
- `assess_counterparty_and_job` — single pre-flight tool aggregating trust, sanctions, KYC, issuer, escrow cap, and go/no-go
- `recommend_release_conditions(job_type)` — recommended escrow parameters by job type
- `explain_agenttrust_trust_model()` — full trust model as structured data
- `get_wallet_setup_guide()` — secure wallet setup steps (no seed returned)
- `/nft/issuers/feed` now returns `Cache-Control` and `ETag` headers; spec version bumped to 1.1.0
- Interop framing: x402 receipts accepted as deliverable evidence; ERC-8004 agent IDs accepted as trust score inputs
- Optional AI audit: `require_ai_audit=false` with proof gate emits synthetic PASS without LLM call

### Changed
- `lookup_nft_issuer` now returns `registered: false` with plain-language explanation on no-match (was silent empty or fuzzy result)
- `create_agent_wallet` demoted to development/quick-start; `get_wallet_setup_guide` is now the production default
- Issuer registry feed `?verified=` now accepts `disputed` and `revoked` in addition to `verified`, `public`, `pending`

---

## v1.9.0 — 2026-09-10

### Added
- `assess_counterparty_and_job` pre-flight tool (backfilled to v2.0.0 above)

---

## v1.8.0 — 2026-09-01

### Added
- `get_wallet_setup_guide`, `explain_agenttrust_trust_model`, `recommend_release_conditions`
- Optional AI audit (`require_ai_audit` flag on escrow vaults)
- Proof-gate-only mode: `require_ai_audit=false` + at least one proof gate skips Gemini

---

## Tool name reference

| Tool | Use when |
|---|---|
| `audit_task(task, work, fee_hash)` | Standalone AI verdict — **no escrow**. Pay $0.10, get PASS/FAIL. Use for one-off quality checks or when you handle payment separately. |
| `evaluate_escrow_work(escrow_id, work)` | Submit proof against an **existing escrow vault**. No separate fee — payment auto-releases on PASS. This is the normal worker payment flow. |
| `hire_and_pay(...)` | Buyer shortcut — registers vault AND returns a ready-to-sign `EscrowCreate` tx in one call. |
| `assess_counterparty_and_job(...)` | Pre-flight before locking any funds — aggregates trust, sanctions, KYC, issuer, escrow cap. Always call this first. |

`audit_task` and `evaluate_escrow_work` are **not interchangeable**. `audit_task` is pay-per-verdict with no escrow. `evaluate_escrow_work` is part of the escrow payment lifecycle and releases funds automatically on PASS.

---

## Cryptographic fulfillment key handling

Each escrow generates a cryptographically random 32-byte preimage via `secrets.token_bytes(32)`.  
Its SHA-256 hash becomes the on-chain XRPL crypto-condition (type `A0`, preimage-sha-256).  
The preimage (fulfillment) is stored AES-256-GCM encrypted in the database.  
It is decrypted in-memory only at the point of escrow release — never logged or transmitted in plaintext.

This is a **standard XRPL preimage-sha-256 crypto-condition** (RFC 3010 / draft-thomas-crypto-conditions). Any XRPL node can verify the condition independently. AgentTrust cannot fabricate a valid fulfillment for a condition it did not generate, and cannot move funds belonging to an escrow it does not hold the fulfillment for.

---

## MCP endpoint

Canonical: `https://mcp.cryptovault.co.uk/mcp`  
Status: `https://mcp.cryptovault.co.uk/status`  
Spec: `https://www.cryptovault.co.uk/docs/issuer-registry-spec.md`
