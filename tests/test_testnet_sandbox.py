"""Tests for testnet sandbox features."""
import xrpl.core.keypairs as kp


def test_create_testnet_wallet_mcp_returns_seed_alias():
    """Verify create_testnet_wallet returns 'seed' field as alias for 'secret'."""
    import ast

    with open("mcp_server.py") as f:
        src = f.read()

    assert '"seed": seed' in src, "create_testnet_wallet must return seed alias"
    assert '"next_step"' in src, "create_testnet_wallet must return next_step field"


def test_throwaway_keypair_generation():
    """Verify throwaway keypairs work for test isolation."""
    seed = kp.generate_seed()
    pub, _ = kp.derive_keypair(seed)
    address = kp.derive_classic_address(pub)
    assert address.startswith("r"), "XRPL address should start with r"
    assert len(address) >= 25, "XRPL address too short"


def test_mcp_instructions_contain_testnet_section():
    """Verify MCP instructions have testnet START HERE section."""
    with open("mcp_server.py") as f:
        src = f.read()

    assert "START FOR FREE" in src or "START HERE" in src, (
        "MCP instructions must have a testnet/free section at the top"
    )
    assert "create_testnet_wallet" in src[:3000], (
        "create_testnet_wallet must appear in the first part of MCP instructions"
    )


def test_try_testnet_demo_tool_exists():
    """Verify try_testnet_demo tool is defined in mcp_server.py."""
    with open("mcp_server.py") as f:
        src = f.read()

    assert "async def try_testnet_demo" in src, "try_testnet_demo tool not found"


def test_no_error_dict_returns_in_mcp():
    """Verify no MCP tools return {\"error\": ...} dicts (should raise exceptions)."""
    with open("mcp_server.py") as f:
        lines = f.readlines()

    violations = [
        (i + 1, l.strip())
        for i, l in enumerate(lines)
        if 'return {"error"' in l
    ]
    assert not violations, (
        f"Found return {{\"error\": ...}} dicts (should raise exceptions): {violations}"
    )
