"""Test that GET /escrow/{id} response includes challenge fields."""
import ast


def test_get_escrow_returns_challenge_fields():
    """Verify challenge_outcome, challenge_fee_state, challenge_fee_tx_hash are in GET /escrow/{id}."""
    with open("referee.py") as f:
        src = f.read()

    # Find the dict returned by get_escrow_by_id — look for challenge fields
    assert '"challenge_outcome"' in src, "challenge_outcome missing from GET /escrow response"
    assert '"challenge_fee_state"' in src, "challenge_fee_state missing from GET /escrow response"
    assert '"challenge_fee_tx_hash"' in src, "challenge_fee_tx_hash missing from GET /escrow response"

    # Verify fix_tx_hash was removed from explorer_links
    assert "finish_tx_hash" not in src or "release_tx_hash" in src, (
        "finish_tx_hash still referenced — should be release_tx_hash"
    )
