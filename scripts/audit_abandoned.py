#!/usr/bin/env python3
"""
Print XRPL on-chain status for escrows marked ABANDONED since 2026-10-23 18:00 UTC.
Read-only — does not change any DB records.
Usage: DATABASE_URL=... python3 scripts/audit_abandoned.py
"""
import sys, os, asyncio, httpx
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

XRPL_URL = "https://xrplcluster.com"
CUTOFF = datetime(2026, 10, 23, 18, 0, 0, tzinfo=timezone.utc)
TARGET_IDS = {"AT-R4-47FE", "AT-R4-3DFE", "AT-EXZN-YH98", "AT-FF-3B14", "AT-FF-EB86"}


async def check_onchain(buyer: str, amount_xrp: float) -> dict:
    amount_drops = str(int(amount_xrp * 1_000_000))
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(XRPL_URL, json={
            "method": "account_objects",
            "params": [{"account": buyer, "type": "escrow", "ledger_index": "validated"}],
        })
        objects = r.json().get("result", {}).get("account_objects", [])
        matches = [o for o in objects if o.get("Amount") == amount_drops]
        return {"escrow_objects_found": len(matches), "matches_amount": len(matches) > 0}


async def main():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("ERROR: DATABASE_URL env var required")
        sys.exit(1)

    from referee import EscrowVault
    engine = create_engine(database_url)
    Sess = sessionmaker(bind=engine)
    db = Sess()

    vaults = db.query(EscrowVault).filter(
        EscrowVault.status == "ABANDONED",
        EscrowVault.updated_at >= CUTOFF,
    ).all()

    print(f"Found {len(vaults)} ABANDONED escrows since {CUTOFF.isoformat()}")
    print()

    for v in vaults:
        flag = " *** TARGET ***" if v.escrow_id in TARGET_IDS else ""
        print(f"Escrow: {v.escrow_id}{flag}")
        print(f"  Buyer: {v.buyer_address}  Amount: {v.amount_xrp} XRP")
        print(f"  Abandoned at: {v.updated_at}")
        try:
            result = await check_onchain(v.buyer_address, v.amount_xrp or 0)
            if result["matches_amount"]:
                print(f"  ON-CHAIN: ESCROW OBJECT FOUND — may have been abandoned incorrectly")
            else:
                print(f"  ON-CHAIN: escrow object not found (likely correctly abandoned)")
        except Exception as e:
            print(f"  ON-CHAIN: check failed — {e}")
        print()

    db.close()


if __name__ == "__main__":
    asyncio.run(main())
