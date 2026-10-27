"""Test that FAIL verdict does not raise UnboundLocalError (prev_status fix)."""
import pytest
import xrpl.core.keypairs as kp


def _throwaway_address():
    seed = kp.generate_seed()
    pub, _ = kp.derive_keypair(seed)
    return kp.derive_classic_address(pub)


def test_prev_status_set_before_is_approved_branch():
    """Verify _prev_status is captured before branching on PASS/FAIL."""
    import ast, textwrap
    with open("referee.py") as f:
        src = f.read()

    tree = ast.parse(src)
    lines = src.splitlines()

    # Find the line with 'is_approved = verdict_dict.get("verdict") == "PASS"'
    # and confirm _prev_status = vault.status appears before 'if is_approved:'
    is_approved_line = None
    prev_status_line = None
    if_is_approved_line = None

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_prev_status":
                    if prev_status_line is None or node.lineno < prev_status_line:
                        prev_status_line = node.lineno
                if isinstance(t, ast.Name) and t.id == "is_approved":
                    is_approved_line = node.lineno
        if isinstance(node, ast.If):
            if isinstance(node.test, ast.Name) and node.test.id == "is_approved":
                if if_is_approved_line is None or node.lineno < if_is_approved_line:
                    if_is_approved_line = node.lineno

    assert prev_status_line is not None, "_prev_status not assigned anywhere"
    assert is_approved_line is not None, "is_approved not assigned"
    assert if_is_approved_line is not None, "if is_approved: not found"

    # _prev_status must be assigned AFTER is_approved but BEFORE the first if is_approved:
    assert is_approved_line < prev_status_line < if_is_approved_line, (
        f"_prev_status (L{prev_status_line}) must appear between "
        f"is_approved assignment (L{is_approved_line}) and if is_approved: (L{if_is_approved_line})"
    )
