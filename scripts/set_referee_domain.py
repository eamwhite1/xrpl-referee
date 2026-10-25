#!/usr/bin/env python3
"""
Print an unsigned AccountSet transaction to set Domain=cryptovault.co.uk
on the referee wallet. Review and sign with Xaman — never auto-submit.
"""
import json

REFEREE_ADDRESS = "rmcSrkpZ2i2kuvtCPeTVetee9SixP4djR"
DOMAIN_HEX = "cryptovault.co.uk".encode().hex().upper()

tx = {
    "TransactionType": "AccountSet",
    "Account": REFEREE_ADDRESS,
    "Domain": DOMAIN_HEX,
    "Fee": "12",
    "Flags": 0,
}

print("Unsigned AccountSet transaction:")
print(json.dumps(tx, indent=2))
print()
print("Sign this with Xaman or your hardware wallet.")
print("Never auto-submit — review the Domain field before signing.")
print(f"Domain (hex): {DOMAIN_HEX}  →  cryptovault.co.uk")
