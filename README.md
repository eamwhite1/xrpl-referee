# AgentTrust Referee

**Trustless AI verdict engine for the agent economy.**

Pay 0.1 XRP. POST a task spec and proof of work. Receive a structured PASS/FAIL verdict from Gemini. Optionally lock funds in XRPL crypto-condition escrow that releases automatically on approval.

No humans. No disputes. No middlemen.

🔗 **Live API:** https://xrpl-referee.onrender.com  
🧪 **Playground:** https://xrpl-referee.onrender.com/playground  
📖 **OpenAPI Docs:** https://xrpl-referee.onrender.com/docs  
🌐 **App (with escrow):** https://www.cryptovault.co.uk

---

## Quickstart — Standalone Verdict (no escrow)

```python
import httpx
from xrpl.transaction import submit_and_wait
from xrpl.models.transactions import Payment
from xrpl.utils import xrp_to_drops
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.wallet import Wallet

# 1. Pay the 0.1 XRP protocol fee
client = AsyncJsonRpcClient("https://xrplcluster.com")
wallet = Wallet.from_seed("your_seed_here")

fee_tx = submit_and_wait(Payment(
    account=wallet.address,
    destination="rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR",
    amount=xrp_to_drops(0.1),
), client, wallet)

fee_hash = fee_tx.result["hash"]

# 2. Submit task + work for AI verdict
verdict = httpx.post("https://xrpl-referee.onrender.com/audit", json={
    "fee_hash":      fee_hash,
    "task":          "Write a 300-word summary of how XRPL escrow works. Must cover: what escrow is, how crypto-conditions work, and one real use case.",
    "work":          "... completed work here ...",
    "task_category": "creative",   # creative | code | data | bug_bounty | legal | supply_chain
}).json()

print(verdict["verdict"])   # "PASS" or "FAIL"
print(verdict["score"])     # 0-100
print(verdict["summary"])   # one-sentence conclusion
print(verdict["details"])   # full reasoning
```

Each fee hash can only be used once (anti-replay protection).

---

## Quickstart — Full Escrow Protocol

Lock XRPL funds. Release on AI approval. Worker gets paid automatically.

```python
import httpx, secrets
from xrpl.transaction import submit_and_wait
from xrpl.models.transactions import Payment, EscrowCreate, EscrowFinish
from xrpl.utils import xrp_to_drops
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.wallet import Wallet

REFEREE = "https://xrpl-referee.onrender.com"
PROTOCOL_WALLET = "rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR"

client = AsyncJsonRpcClient("https://xrplcluster.com")
buyer_wallet  = Wallet.from_seed("buyer_seed")
worker_wallet = Wallet.from_seed("worker_seed")

# ── BUYER ────────────────────────────────────────────────────────
escrow_id = f"AT-{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"

# Step 1 — pay protocol fee
fee_tx   = submit_and_wait(Payment(account=buyer_wallet.address, destination=PROTOCOL_WALLET, amount=xrp_to_drops(0.1)), client, buyer_wallet)
fee_hash = fee_tx.result["hash"]

# Step 2 — create vault, get crypto-condition
r = httpx.post(f"{REFEREE}/escrow/generate", json={
    "escrow_id":        escrow_id,
    "fee_hash":         fee_hash,
    "buyer_name":       "Acme Agent",
    "buyer_address":    buyer_wallet.address,
    "task_description": "Write a 300-word XRPL escrow summary.",
    "worker_address":   worker_wallet.address,
    "amount_xrp":       10.0,
    "cancel_after_hrs": 168,
})
condition = r.json()["condition"]

# Step 3 — lock funds on-chain
escrow_tx = submit_and_wait(EscrowCreate(
    account=buyer_wallet.address,
    destination=worker_wallet.address,
    amount=xrp_to_drops(10),
    condition=condition,
), client, buyer_wallet)

# Step 4 — confirm with referee (auto-stores sequence number)
httpx.post(f"{REFEREE}/escrow/{escrow_id}/confirm",
           json={"tx_hash": escrow_tx.result["hash"]})

# ── WORKER ───────────────────────────────────────────────────────
# Step 5 — submit work for AI audit
verdict = httpx.post(f"{REFEREE}/evaluate", json={
    "escrow_id": escrow_id,
    "work":      "... completed article here ...",
}).json()

if verdict["status"] == "approved":
    # Step 6 — claim funds via EscrowFinish
    submit_and_wait(EscrowFinish(
        account=worker_wallet.address,
        owner=verdict["buyer_address"],
        offer_sequence=verdict["escrow_sequence"],
        fulfillment=verdict["fulfillment"],
        condition=verdict["condition"],
        fee="5000",  # ~0.005 XRP — required for crypto-condition finish
    ), client, worker_wallet)
    print("💸 Paid!")
else:
    print("Verdict:", verdict["verdict"])  # detailed feedback for revision
```

---

## XRPL NFT Issuer Registry

An open, machine-readable registry mapping real-world organisations to their XRPL NFT-issuing wallet addresses. Verification is bidirectional: the wallet's on-chain `Domain` field must point to the organisation's domain, and `xrp-ledger.toml` at that domain must list the wallet (XLS-26 compatible).

**Spec:** https://www.cryptovault.co.uk/docs/issuer-registry-spec.md  
**Discovery:** `GET https://xrpl-referee.onrender.com/.well-known/xrpl-issuer-registry`

### Registry endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/nft/issuers` | List verified and public issuers. Filter by `?category=` |
| `GET`  | `/nft/issuers/feed` | Paginated versioned feed for wallets, explorers, DEXs. Supports `page`, `per_page`, `since` (incremental sync), `category`, `verified` |
| `POST` | `/nft/issuers` | Register your organisation as an NFT issuer |
| `POST` | `/domain/verify` | Verify wallet ↔ domain ownership via `xrp-ledger.toml` |
| `GET`  | `/gleif/xrpl-lookup?q=` | Look up an organisation's verified XRPL wallet by name |

### MCP tools for agents

| Tool | Description |
|------|-------------|
| `list_trusted_issuers` | Query the registry by name or category |
| `company_xrpl_lookup` | Find a verified XRPL wallet address by organisation name |
| `verify_domain_ownership` | Confirm wallet ↔ domain link via `xrp-ledger.toml` |
| `verify_nft_proof` | Verify an NFT exists in a wallet, was minted by a required issuer, and contains required metadata |
| `register_as_issuer` | Submit a new issuer registration |

### Incremental sync example

```
GET https://xrpl-referee.onrender.com/nft/issuers/feed?since=2026-06-01T00:00:00Z&verified=verified
```

Returns a versioned envelope with `spec_version`, `generated_at`, and `pagination.next` so any wallet, DEX, or explorer can cache and incrementally update the registry.

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/audit` | Standalone AI verdict — no escrow required |
| `POST` | `/escrow/generate` | Create AI-gated escrow vault |
| `POST` | `/escrow/{id}/confirm` | Store EscrowCreate tx hash (auto-looks up sequence) |
| `GET`  | `/escrow/{id}` | Get vault metadata |
| `POST` | `/evaluate` | Submit work for escrow-linked AI audit |
| `GET`  | `/dex/quote` | XRP → RLUSD conversion quote |
| `GET`  | `/status` | Health check |

Full schema at [`/docs`](https://xrpl-referee.onrender.com/docs) (Swagger UI).

---

## Task Categories

Pass `task_category` to get domain-specific AI evaluation:

| Category | Use case |
|----------|----------|
| `default` | General purpose |
| `creative` | Writing, design, content |
| `code` | Software development |
| `data` | Research, datasets, scraping |
| `bug_bounty` | Security vulnerability PoC |
| `legal` | Contracts, settlements, compliance |
| `supply_chain` | Logistics documents, bills of lading |

For high-stakes jobs, set `require_consensus: true` — two AI models must independently agree before a PASS is returned.

---

## Agent Discovery

| Platform | Link |
|----------|------|
| OpenAPI / Swagger | [`/docs`](https://xrpl-referee.onrender.com/docs) |
| agent.json (A2A) | [`/.well-known/agent.json`](https://xrpl-referee.onrender.com/.well-known/agent.json) |
| ai-plugin.json | [`/.well-known/ai-plugin.json`](https://xrpl-referee.onrender.com/.well-known/ai-plugin.json) |
| Smithery | [smithery.ai/servers/xrpl/referee-pro](https://smithery.ai/servers/xrpl/referee-pro) |
| HuggingFace | [spaces/eamwhite1/xrpl-referee-tool](https://huggingface.co/spaces/eamwhite1/xrpl-referee-tool) |

---

## Protocol Fee

Every audit costs **0.1 XRP** — paid to `rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR` on XRPL Mainnet before calling `/audit` or `/escrow/generate`. Each transaction hash is single-use.

For the escrow flow, the XRPL network also charges ~**0.005 XRP** when the worker submits the EscrowFinish transaction (standard crypto-condition fee).

---

## Architecture

```
Buyer pays 0.1 XRP fee
        ↓
POST /escrow/generate → referee stores vault, returns crypto-condition
        ↓
Buyer submits EscrowCreate on-chain (funds locked)
        ↓
Worker submits proof → POST /evaluate
        ↓
Gemini audits work against task spec
        ↓
PASS → fulfillment key returned → worker submits EscrowFinish → paid
FAIL → detailed feedback returned → worker can revise and resubmit
```

The Referee never holds funds. It only issues or withholds the cryptographic key that unlocks the on-chain escrow.

---

## Stack

- **Backend:** FastAPI + Python
- **AI:** Google Gemini 2.5 Pro (with fallback chain)
- **Blockchain:** XRPL Mainnet via xrpl-py
- **Signing (human flow):** Xaman (formerly XUMM)
- **Database:** PostgreSQL (Render)
- **Hosting:** Render

---

Built by [@eamwhite1](https://github.com/eamwhite1)
