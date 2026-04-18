"""
AgentTrust — OpenAI Agents SDK Example
========================================
Shows how to give an OpenAI agent the ability to create and
manage escrow payments via AgentTrust.

The agent can:
  - Audit work before committing funds
  - Create an XRPL escrow to lock payment
  - Evaluate submitted work and release payment on approval

Requirements:
    pip install openai requests

Usage:
    export OPENAI_API_KEY="sk-..."
    python 02_openai_agents_sdk.py

XRPL wallets:
    Get a free testnet wallet at: https://xrpl.org/xrp-testnet-faucet.html
"""

import os
import json
import requests
from openai import OpenAI

BASE_URL = "https://xrpl-referee.onrender.com"

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


# ── AgentTrust tool definitions ───────────────────────────────────────────────
# These are passed to the OpenAI API as function tools.
# The model decides when to call them based on the conversation.

AGENTTRUST_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "agenttrust_audit",
            "description": (
                "Audit a piece of work against a job specification using the AgentTrust oracle. "
                "Returns a verdict (approved/rejected) and reasoning. "
                "Use this to check work quality before locking any funds in escrow."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "job_spec": {
                        "type": "string",
                        "description": "The specification the work must meet."
                    },
                    "deliverable": {
                        "type": "string",
                        "description": "The completed work to evaluate."
                    }
                },
                "required": ["job_spec", "deliverable"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agenttrust_create_escrow",
            "description": (
                "Create a trustless XRPL conditional escrow that locks XRP payment. "
                "Funds are only released when the oracle approves the deliverable. "
                "Returns the escrow sequence number and condition hash needed for evaluation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payer_address":  {"type": "string", "description": "XRPL address of the payer."},
                    "payer_secret":   {"type": "string", "description": "XRPL wallet secret of the payer."},
                    "worker_address": {"type": "string", "description": "XRPL address of the worker receiving payment."},
                    "amount_xrp":     {"type": "number", "description": "Amount of XRP to lock in escrow."},
                    "job_spec":       {"type": "string", "description": "The job specification the worker must fulfil."}
                },
                "required": ["payer_address", "payer_secret", "worker_address", "amount_xrp", "job_spec"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "agenttrust_evaluate",
            "description": (
                "Submit completed work to the AgentTrust oracle for evaluation. "
                "If the work meets the spec, the escrow is fulfilled and XRP is "
                "released to the worker automatically on the XRPL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "escrow_sequence": {"type": "integer", "description": "Sequence number from create_escrow."},
                    "payer_address":   {"type": "string"},
                    "worker_address":  {"type": "string"},
                    "condition":       {"type": "string", "description": "Condition hash from create_escrow."},
                    "job_spec":        {"type": "string"},
                    "deliverable":     {"type": "string", "description": "The work to evaluate."}
                },
                "required": ["escrow_sequence", "payer_address", "worker_address",
                             "condition", "job_spec", "deliverable"]
            }
        }
    }
]


# ── Tool execution ─────────────────────────────────────────────────────────────

def execute_tool(tool_name: str, tool_args: dict) -> str:
    """Route tool calls from the model to the AgentTrust API."""

    if tool_name == "agenttrust_audit":
        resp = requests.post(f"{BASE_URL}/audit", json={
            "jobSpec":     tool_args["job_spec"],
            "deliverable": tool_args["deliverable"]
        })
        return json.dumps(resp.json())

    elif tool_name == "agenttrust_create_escrow":
        resp = requests.post(f"{BASE_URL}/escrow/generate", json={
            "payerAddress":  tool_args["payer_address"],
            "payerSecret":   tool_args["payer_secret"],
            "workerAddress": tool_args["worker_address"],
            "amountXRP":     tool_args["amount_xrp"],
            "jobSpec":       tool_args["job_spec"]
        })
        return json.dumps(resp.json())

    elif tool_name == "agenttrust_evaluate":
        resp = requests.post(f"{BASE_URL}/evaluate", json={
            "escrowSequence": tool_args["escrow_sequence"],
            "payerAddress":   tool_args["payer_address"],
            "workerAddress":  tool_args["worker_address"],
            "condition":      tool_args["condition"],
            "jobSpec":        tool_args["job_spec"],
            "deliverable":    tool_args["deliverable"]
        })
        return json.dumps(resp.json())

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


# ── Agent loop ─────────────────────────────────────────────────────────────────

def run_agent(user_message: str) -> str:
    """
    Run the OpenAI agent with AgentTrust tools available.
    The agent will decide when to call escrow/audit/evaluate
    based on the conversation.
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are a payment-enabled AI agent. You can create trustless escrow payments "
                "on the XRP Ledger using AgentTrust. When asked to pay for work, use the "
                "agenttrust tools to: first audit the work, then create escrow, then evaluate "
                "and release payment. Always confirm the verdict before declaring success."
            )
        },
        {"role": "user", "content": user_message}
    ]

    # Agentic loop — keep going until the model stops calling tools
    while True:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            tools=AGENTTRUST_TOOLS,
            tool_choice="auto"
        )

        message = response.choices[0].message

        # No tool calls — we have a final answer
        if not message.tool_calls:
            return message.content

        # Process each tool call
        messages.append(message)  # Add assistant message with tool_calls

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            tool_args = json.loads(tool_call.function.arguments)

            print(f"\n  → Calling {tool_name}({list(tool_args.keys())})")
            result = execute_tool(tool_name, tool_args)
            print(f"  ← {result[:120]}...")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result
            })


# ── Example usage ─────────────────────────────────────────────────────────────

if __name__ == "__main__":

    task = """
    I need you to:
    1. Audit this piece of work against the spec below
    2. If it passes, create a 1 XRP escrow from my wallet to the worker
    3. Then evaluate the work and release payment if approved

    Job spec: Summarise the article in exactly 3 bullet points, each under 20 words.

    Submitted work:
    • AI agents are increasingly used for autonomous task execution.
    • Payment rails on XRPL enable trustless agent-to-agent transactions.
    • AgentTrust acts as a neutral oracle to verify work quality.

    My wallet:  rPayerAddressHere  (secret: sPayerSecretHere)
    Worker:     rWorkerAddressHere
    """

    print("Running AgentTrust-enabled OpenAI agent...\n")
    result = run_agent(task)
    print(f"\nAgent response:\n{result}")
