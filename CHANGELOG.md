# Changelog

All notable changes to the AgentTrust MCP server and REST API.

Protocol version header: `X-AgentTrust-Version: 0.1.0` on all responses since v2.0.0.

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
