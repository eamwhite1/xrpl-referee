#!/usr/bin/env python3
"""
Testnet-only Privy policy test.
Run: python scripts/privy_policy_test.py
Skipped in CI when PRIVY_APP_ID is unset.
"""
import os, sys, asyncio

TESTNET_URL = "https://s.altnet.rippletest.net:51234"
PRIVY_APP_ID = os.getenv("PRIVY_APP_ID", "")
PRIVY_APP_SECRET = os.getenv("PRIVY_APP_SECRET", "")
PRIVY_POLICY_ID = os.getenv("PRIVY_POLICY_ID", "")

if not PRIVY_APP_ID:
    print("SKIP: PRIVY_APP_ID not set")
    sys.exit(0)


async def main():
    import httpx
    from xrpl.core.binarycodec import encode_for_signing

    policy_args = {}
    if PRIVY_POLICY_ID:
        policy_args["policy_ids"] = [PRIVY_POLICY_ID]

    async with httpx.AsyncClient(timeout=30) as hc:
        r = await hc.post(
            "https://api.privy.io/v1/wallets",
            auth=(PRIVY_APP_ID, PRIVY_APP_SECRET),
            headers={"privy-app-id": PRIVY_APP_ID},
            json={"chain_type": "xrpl", **policy_args},
        )
        r.raise_for_status()
        wallet_data = r.json()
        privy_wallet_id = wallet_data["id"]
        address = wallet_data["address"]
        pub_key = (wallet_data.get("public_key") or "").upper()
        print(f"Throwaway wallet: {address}")

        # Fund from testnet faucet
        faucet_r = await hc.post(
            "https://faucet.altnet.rippletest.net/accounts",
            json={"destination": address, "xrpAmount": "100"},
        )
        print(f"Faucet: {faucet_r.status_code}")
        await asyncio.sleep(5)

        # Build and sign EscrowCreate (hex path)
        escrow_tx = {
            "TransactionType": "EscrowCreate",
            "Account": address,
            "Destination": "rPT1Sjq2YGrBMTttX4GZHjKu9dyfzbpAYe",
            "Amount": "1000000",
            "Condition": "A0258020E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855810100",
            "CancelAfter": 999999999,
            "Fee": "12",
            "Sequence": 1,
            "LastLedgerSequence": 999999999,
            "SigningPubKey": pub_key,
        }
        hex_tx = encode_for_signing(escrow_tx)
        assert hex_tx.upper().startswith("53545800"), f"Bad prefix: {hex_tx[:8]}"

        r = await hc.post(
            f"https://api.privy.io/v1/wallets/{privy_wallet_id}/rpc",
            auth=(PRIVY_APP_ID, PRIVY_APP_SECRET),
            headers={"privy-app-id": PRIVY_APP_ID},
            json={"method": "xrpl_signTransaction", "params": {"transaction": hex_tx, "encoding": "hex"}},
        )
        if r.status_code == 200 and r.json().get("data", {}).get("signed_transaction"):
            print("PASS: EscrowCreate signed")
        else:
            print(f"FAIL: EscrowCreate signing returned {r.status_code}: {r.text[:200]}")
            sys.exit(1)

        # Build OfferCreate — should be DENIED if policy is attached
        offer_tx = {
            "TransactionType": "OfferCreate",
            "Account": address,
            "TakerGets": "1000000",
            "TakerPays": {"currency": "USD", "issuer": "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh", "value": "1"},
            "Fee": "12",
            "Sequence": 2,
            "LastLedgerSequence": 999999999,
            "SigningPubKey": pub_key,
        }
        offer_hex = encode_for_signing(offer_tx)
        r2 = await hc.post(
            f"https://api.privy.io/v1/wallets/{privy_wallet_id}/rpc",
            auth=(PRIVY_APP_ID, PRIVY_APP_SECRET),
            headers={"privy-app-id": PRIVY_APP_ID},
            json={"method": "xrpl_signTransaction", "params": {"transaction": offer_hex, "encoding": "hex"}},
        )
        if r2.status_code in (403, 400) or "denied" in r2.text.lower():
            print("PASS: OfferCreate DENIED by policy")
        elif not PRIVY_POLICY_ID:
            print("NOTE: No PRIVY_POLICY_ID set — OfferCreate was not denied (policy not attached). Attach the policy to enforce tx-type restrictions.")
        else:
            print(f"FAIL: OfferCreate should be DENIED but got {r2.status_code}: {r2.text[:200]}")
            sys.exit(1)

        print("ALL PASS")


asyncio.run(main())
