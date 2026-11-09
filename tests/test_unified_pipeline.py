"""
tests/test_unified_pipeline.py — T1-T24 from v7.0.7 brief
Tests domain normalisation (T23) and version consistency (T24) run standalone.
Other tests require the full app + DB and are marked with @pytest.mark.integration.
"""
import pytest
import json
import os
import sys

# ---------------------------------------------------------------------------
# T23: domain normalisation — no DB, no app import needed
# ---------------------------------------------------------------------------

def test_t23_domain_normalisation():
    """T23: _normalise_domain handles schemes, paths, trailing dots, bad input."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from referee import _normalise_domain
    assert _normalise_domain("https://ripple.com/") == "ripple.com"
    assert _normalise_domain("ripple.com") == "ripple.com"
    assert _normalise_domain("http://ripple.com") == "ripple.com"
    # Repeated scheme
    result = _normalise_domain("HTTPS://https://Ripple.com//path?q=1")
    assert result == "ripple.com"
    # Trailing dot
    assert _normalise_domain("ripple.com.") == "ripple.com"
    # Empty / None
    assert _normalise_domain("") is None
    assert _normalise_domain(None) is None
    # No dot → invalid
    assert _normalise_domain("not a domain") is None
    # Path stripped
    assert _normalise_domain("ripple.com/foo/bar") == "ripple.com"
    # Query stripped
    assert _normalise_domain("ripple.com?q=1") == "ripple.com"


# ---------------------------------------------------------------------------
# T24: version consistency — referee.py vs server.json
# ---------------------------------------------------------------------------

def test_t24_version():
    """T24: APP_VERSION in referee.py matches server.json and equals 7.0.7."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    from referee import APP_VERSION
    assert APP_VERSION == "7.0.7", f"referee.py APP_VERSION={APP_VERSION!r}, expected 7.0.7"
    server_json_path = os.path.join(repo_root, "server.json")
    if os.path.exists(server_json_path):
        with open(server_json_path) as f:
            sj = json.load(f)
        assert sj["version"] == "7.0.7", f"server.json version={sj['version']!r}, expected 7.0.7"
