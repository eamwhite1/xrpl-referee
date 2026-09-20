# Changelog

All notable changes to the AgentTrust MCP server and REST API.

Protocol version header: `X-AgentTrust-Version: 0.1.0` on all responses since v2.0.0.

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
