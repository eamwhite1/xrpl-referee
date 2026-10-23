#!/usr/bin/env python3
"""
Print XRPL on-chain status for escrows marked ABANDONED (all time) and specific target IDs.
Read-only — does not change any DB records.

Note: The original 18:00 UTC cutoff missed the 17:18 UTC poller run, so we query all
ABANDONED escrows plus the known target IDs without a date filter.

Usage: DATABASE_URL=... python3 scripts/audit_abandoned.py
"""
import sys, os, asyncio, httpx
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from sqlalchemy import create_engine, or_
from sqlalchemy.orm import sessionmaker

XRPL_URL = "https://xrplcluster.com"
TARGET_IDS = {"AT-R4-47FE", "AT-R4-3DFE", "AT-EXZN-YH98", "AT-FF-3B14", "AT-FF-EB86", "AT-H6LB-RXZH"}

DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL env var required")
    sys.exit(1)


async def check_onchain(buyer: str, amount_xrp: float) -> dict:
    amount_drops = str(int(amount_xrp * 1_000_000))
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(XRPL_URL, json={
            "method": "account_objects",
            "params": [{"account": buyer, "type": "escrow", "ledger_index": "validated"}]
        })
        objects = r.json().get("result", {}).get("account_objects", [])
        matches_xrp = [o for o in objects if o.get("Amount") == amount_drops]
        iou_objects = [o for o in objects if isinstance(o.get("Amount"), dict)]
        return {
            "escrow_objects_total": len(objects),
            "matches_xrp_amount": len(matches_xrp) > 0,
            "iou_escrows_found": len(iou_objects),
        }


async def main():
    from referee import EscrowVault

    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    db = Session()

    # Fetch all ABANDONED + specific target IDs (union)
    vaults = db.query(EscrowVault).filter(
        or_(
            EscrowVault.status == "ABANDONED",
            EscrowVault.escrow_id.in_(TARGET_IDS),
        )
    ).all()

    print(f"Found {len(vaults)} escrows (ABANDONED or target IDs)")
    print()

    for v in vaults:
        flag = " *** TARGET ***" if v.escrow_id in TARGET_IDS else ""
        print(f"Escrow: {v.escrow_id}{flag}  status={v.status}")
        print(f"  Buyer: {v.buyer_address}  Amount: {v.amount_xrp} XRP")
        print(f"  Created: {v.created_at}  cancel_after: {v.cancel_after_ts}")
        print(f"  escrow_tx_hash: {v.escrow_tx_hash or '—'}")
        try:
            result = await check_onchain(v.buyer_address, v.amount_xrp or 0)
            if result["matches_xrp_amount"]:
                print(f"  ON-CHAIN: ✅ ESCROW OBJECT FOUND — may have been abandoned incorrectly!")
            elif result["iou_escrows_found"]:
                print(f"  ON-CHAIN: IOU escrow objects found ({result['iou_escrows_found']}) — different asset, not this escrow")
            else:
                print(f"  ON-CHAIN: no matching escrow object found (total escrow objects: {result['escrow_objects_total']})")
        except Exception as e:
            print(f"  ON-CHAIN: check failed — {e}")
        print()

    db.close()


if __name__ == "__main__":
    asyncio.run(main())
