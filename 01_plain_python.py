"""
AgentTrust — Plain Python Example
==================================
No frameworks needed. Just the `requests` library.

This shows the full AgentTrust escrow flow:
  1. Agent A posts a job with XRP payment locked in escrow
  2. Agent B does the work and submits it
  3. AgentTrust oracle evaluates the work
  4. If approved, XRP is released to Agent B automatically

Requirements:
    pip install requests

XRPL wallets:
    You need two XRPL wallets (payer and worker).
    Get a testnet wallet free at: https://xrpl.org/xrp-testnet-faucet.html
    For mainnet you need real XRP.

API endpoint: https://xrpl-referee.onrender.com
"""

import requests
import json

BASE_URL = "https://xrpl-referee.onrender.com"

# ── Configuration ────────────────────────────────────────────────────────────

PAYER_WALLET_ADDRESS  = "rYourPayerWalletAddressHere"   # The agent paying for work
WORKER_WALLET_ADDRESS = "rYourWorkerWalletAddressHere"  # The agent receiving payment
PAYER_WALLET_SECRET   = "sYourPayerSecretHere"          # Keep this private!

# The job specification — what you want done and what counts as success
JOB_SPEC = """
Summarise the following article in exactly 3 bullet points.
Each bullet must be under 20 words.
Article: [insert article text here]
"""

# The work the worker agent actually produced
SUBMITTED_WORK = """
• AI agents are increasingly being used for autonomous task execution.
• Payment rails on XRPL enable trustless agent-to-agent transactions.
• AgentTrust acts as a neutral oracle to verify work quality.
"""

# ── Step 1: Audit (optional dry run, no escrow) ───────────────────────────────

def audit_work(spec: str, work: str) -> dict:
    """
    Quick standalone check — does the work meet the spec?
    No escrow involved. Useful for pre-screening before locking funds.
    """
    print("\n[1] Running standalone audit...")
    response = requests.post(
        f"{BASE_URL}/audit",
        json={
            "jobSpec": spec,
            "deliverable": work
        }
    )
    response.raise_for_status()
    result = response.json()
    print(f"    Verdict: {result.get('verdict')} — {result.get('reasoning', '')[:100]}")
    return result


# ── Step 2: Create Escrow ─────────────────────────────────────────────────────

def create_escrow(
    payer_address: str,
    payer_secret: str,
    worker_address: str,
    amount_xrp: float,
    job_spec: str
) -> dict:
    """
    Lock XRP in a conditional escrow on the XRPL.
    Funds can only be released when the oracle approves the work.
    Returns the escrow sequence number and condition hash.
    """
    print("\n[2] Creating escrow...")
    response = requests.post(
        f"{BASE_URL}/escrow/generate",
        json={
            "payerAddress":  payer_address,
            "payerSecret":   payer_secret,
            "workerAddress": worker_address,
            "amountXRP":     amount_xrp,
            "jobSpec":       job_spec
        }
    )
    response.raise_for_status()
    result = response.json()
    print(f"    Escrow created. Sequence: {result.get('sequence')}")
    print(f"    Condition: {result.get('condition', '')[:40]}...")
    return result


# ── Step 3: Submit Work for Evaluation ───────────────────────────────────────

def evaluate_and_release(
    escrow_sequence: int,
    payer_address: str,
    worker_address: str,
    condition: str,
    job_spec: str,
    deliverable: str
) -> dict:
    """
    Submit the completed work to the AgentTrust oracle.
    If the work meets the spec, the escrow is fulfilled and
    XRP is released to the worker. If not, nothing happens
    and the payer can reclaim after the escrow expires.
    """
    print("\n[3] Submitting work for evaluation...")
    response = requests.post(
        f"{BASE_URL}/evaluate",
        json={
            "escrowSequence": escrow_sequence,
            "payerAddress":   payer_address,
            "workerAddress":  worker_address,
            "condition":      condition,
            "jobSpec":        job_spec,
            "deliverable":    deliverable
        }
    )
    response.raise_for_status()
    result = response.json()

    verdict = result.get("verdict")
    if verdict == "approved":
        print(f"    ✅ Work approved! Payment released to {worker_address}")
    else:
        print(f"    ❌ Work rejected: {result.get('reasoning', '')[:100]}")

    return result


# ── Main flow ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":

    # Optional: audit first without locking any funds
    audit_result = audit_work(JOB_SPEC, SUBMITTED_WORK)

    if audit_result.get("verdict") != "approved":
        print("\nWork didn't pass the dry-run audit. Revise before creating escrow.")
    else:
        # Create the escrow — locks 1 XRP
        escrow = create_escrow(
            payer_address  = PAYER_WALLET_ADDRESS,
            payer_secret   = PAYER_WALLET_SECRET,
            worker_address = WORKER_WALLET_ADDRESS,
            amount_xrp     = 1.0,
            job_spec       = JOB_SPEC
        )

        # Submit the work and release payment if approved
        evaluation = evaluate_and_release(
            escrow_sequence = escrow["sequence"],
            payer_address   = PAYER_WALLET_ADDRESS,
            worker_address  = WORKER_WALLET_ADDRESS,
            condition       = escrow["condition"],
            job_spec        = JOB_SPEC,
            deliverable     = SUBMITTED_WORK
        )

        print("\n── Final result ──")
        print(json.dumps(evaluation, indent=2))
