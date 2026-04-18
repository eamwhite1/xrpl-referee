"""
AgentTrust — CrewAI Example
============================
Agent A (Payer) hires Agent B (Worker) to write a blog post.
Payment is locked in XRPL escrow and released automatically
when the AgentTrust oracle approves the work.

Install:
    pip install crewai requests

Run:
    python 03_crewai.py

Note: Uses XRPL Testnet wallets. Get free testnet XRP at:
    https://xrpl.org/xrp-testnet-faucet.html
"""

import os
import requests
from crewai import Agent, Task, Crew, Process

# ─────────────────────────────────────────────────────────────────────────────
# Config — swap in real testnet or mainnet wallets
# ─────────────────────────────────────────────────────────────────────────────

AGENTTRUST_API = "https://xrpl-referee.onrender.com"

PAYER_ADDRESS = os.getenv("PAYER_ADDRESS", "rPayerTestnetAddressHere")
PAYER_SECRET  = os.getenv("PAYER_SECRET",  "sPayerTestnetSecretHere")
WORKER_ADDRESS = os.getenv("WORKER_ADDRESS", "rWorkerTestnetAddressHere")
AMOUNT_XRP    = 1.0

# ─────────────────────────────────────────────────────────────────────────────
# AgentTrust helpers
# ─────────────────────────────────────────────────────────────────────────────

def create_escrow(job_spec: str) -> dict:
    """Lock XRP in escrow before the job begins."""
    resp = requests.post(f"{AGENTTRUST_API}/escrow/generate", json={
        "payerAddress":  PAYER_ADDRESS,
        "payerSecret":   PAYER_SECRET,
        "workerAddress": WORKER_ADDRESS,
        "amountXRP":     AMOUNT_XRP,
        "jobSpec":       job_spec,
    }, timeout=30)
    resp.raise_for_status()
    escrow = resp.json()
    print(f"[AgentTrust] Escrow created — sequence: {escrow['sequence']}")
    return escrow


def evaluate_and_release(escrow: dict, job_spec: str, deliverable: str) -> dict:
    """Submit work to oracle. XRP is released automatically on PASS."""
    resp = requests.post(f"{AGENTTRUST_API}/evaluate", json={
        "escrowSequence": escrow["sequence"],
        "payerAddress":   PAYER_ADDRESS,
        "workerAddress":  WORKER_ADDRESS,
        "condition":      escrow["condition"],
        "jobSpec":        job_spec,
        "deliverable":    deliverable,
    }, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    verdict = result.get("verdict", "UNKNOWN")
    score   = result.get("score", 0)
    print(f"[AgentTrust] Oracle verdict: {verdict} (score: {score}/100)")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CrewAI agents
# ─────────────────────────────────────────────────────────────────────────────

payer_agent = Agent(
    role="Client Agent (Payer)",
    goal="Commission high-quality written content and pay for it via trustless XRPL escrow",
    backstory=(
        "You are an AI agent that manages content production. "
        "You define job specifications clearly, lock payment in escrow before work begins, "
        "and rely on a neutral oracle to verify the work before funds are released. "
        "You never pay until the work meets the agreed specification."
    ),
    verbose=True,
    allow_delegation=False,
)

worker_agent = Agent(
    role="Content Agent (Worker)",
    goal="Produce excellent written content that fully satisfies the job specification",
    backstory=(
        "You are a specialist writing agent. You receive a clear job specification, "
        "produce the best possible deliverable, and submit it for oracle evaluation. "
        "Your payment is released automatically when the oracle approves your work."
    ),
    verbose=True,
    allow_delegation=False,
)

# ─────────────────────────────────────────────────────────────────────────────
# Job specification — defined by the payer
# ─────────────────────────────────────────────────────────────────────────────

JOB_SPEC = (
    "Write a blog post introduction (150–200 words) explaining what XRPL escrow is "
    "and why it is useful for AI agent payments. "
    "Must include: a clear definition of escrow, one concrete benefit for AI agents, "
    "and end with a call to action."
)

# ─────────────────────────────────────────────────────────────────────────────
# CrewAI tasks
# ─────────────────────────────────────────────────────────────────────────────

task_lock_escrow = Task(
    description=(
        f"You are about to commission a writing job. The job specification is:\n\n"
        f"{JOB_SPEC}\n\n"
        f"Confirm that the job spec is clear and complete. "
        f"Output the job specification exactly as it should be sent to the worker, "
        f"with no changes."
    ),
    expected_output="The finalised job specification, ready to send to the worker.",
    agent=payer_agent,
)

task_do_work = Task(
    description=(
        f"You have been hired to complete the following job:\n\n"
        f"{JOB_SPEC}\n\n"
        f"Write the deliverable now. Your payment of {AMOUNT_XRP} XRP is held in escrow "
        f"and will be released automatically when the AgentTrust oracle approves your work. "
        f"Produce your best work — the oracle evaluates against the specification precisely."
    ),
    expected_output=(
        "A complete blog post introduction of 150–200 words that fully satisfies the job spec."
    ),
    agent=worker_agent,
)

# ─────────────────────────────────────────────────────────────────────────────
# Crew
# ─────────────────────────────────────────────────────────────────────────────

crew = Crew(
    agents=[payer_agent, worker_agent],
    tasks=[task_lock_escrow, task_do_work],
    process=Process.sequential,
    verbose=True,
)

# ─────────────────────────────────────────────────────────────────────────────
# Run the crew, then settle payment via AgentTrust
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("AgentTrust + CrewAI: Trustless AI Agent Payment Demo")
    print("=" * 60)

    # Step 1: Lock payment in escrow before work begins
    print("\n[Step 1] Locking payment in XRPL escrow...")
    escrow = create_escrow(JOB_SPEC)

    # Step 2: Run the crew — payer confirms spec, worker produces deliverable
    print("\n[Step 2] Running crew...\n")
    result = crew.kickoff()

    # The last task output is the worker's deliverable
    deliverable = str(result)

    # Step 3: Submit work to AgentTrust oracle for evaluation
    print("\n[Step 3] Submitting work to AgentTrust oracle...")
    evaluation = evaluate_and_release(escrow, JOB_SPEC, deliverable)

    # Step 4: Report outcome
    print("\n" + "=" * 60)
    if evaluation.get("verdict") == "PASS":
        print(f"✅ PASS — {AMOUNT_XRP} XRP released to worker ({WORKER_ADDRESS})")
    else:
        print(f"❌ FAIL — Escrow not fulfilled. Score: {evaluation.get('score', 0)}/100")
        print(f"   Reason: {evaluation.get('summary', 'No summary provided')}")
    print("=" * 60)

    print("\n--- Worker deliverable ---")
    print(deliverable)
