from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / ".xgc2/scripts/publish_client_apt.py"
SPEC = importlib.util.spec_from_file_location("publish_client_apt", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)


def test_optional_publish_credentials_are_all_or_nothing() -> None:
    assert publisher.credential_state({}) == "absent"
    assert publisher.credential_state({"APT_REPO_HOST": "example"}) == "partial"
    configured = {name: "x" for name in publisher.SECRET_NAMES}
    configured["APT_REPO_PORT"] = "2222"
    assert publisher.credential_state(configured) == "configured"


def test_canonical_digest_is_order_independent() -> None:
    first = publisher.digest_bytes(publisher.canonical({"b": 1, "a": 2}))
    second = publisher.digest_bytes(publisher.canonical({"a": 2, "b": 1}))
    assert first == second
    assert len(first) == 64
