from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".xgc2/scripts/xgc2_artifact_manifest.py"
SPEC = importlib.util.spec_from_file_location("xgc2_artifact_manifest", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
manifest_tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manifest_tool)


def deb_metadata(path: Path) -> dict[str, object]:
    return {
        "file": path.name,
        "package": "xgc2-stt-client",
        "version": "0.2.1-3",
        "architecture": "amd64",
        "sha256": "a" * 64,
        "size": path.stat().st_size,
    }


def arguments(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        product="xgc2-stt-client",
        product_version="0.2.1-3",
        distribution="jammy",
        architecture="amd64",
        source_sha="b" * 40,
        deb_dir=str(root / "debs"),
        output_dir=str(root / "manifests"),
        ci_run_id="123",
        ci_workflow="Desktop client Deb CI",
        ci_workflow_ref="XGC-Team/xgc2-stt-service/.github/workflows/client-deb-ci.yml@refs/heads/main",
        artifact_dir=str(root),
        deb_output_dir=str(root / "verified-debs"),
        manifest_output_dir=str(root / "verified-manifests"),
    )


def test_v1_manifest_has_exact_fields_and_debian_version(tmp_path: Path, monkeypatch) -> None:
    deb_dir = tmp_path / "debs"
    deb_dir.mkdir()
    (deb_dir / "xgc2-stt-client.deb").write_bytes(b"deb")
    monkeypatch.setattr(manifest_tool, "deb_entry", deb_metadata)

    args = arguments(tmp_path)
    manifest_tool.build(args)
    manifest_path = next((tmp_path / "manifests").glob("*.build.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["schema"] == "xgc2.build-artifact.v1"
    assert set(manifest) == manifest_tool.BUILD_FIELDS
    assert set(manifest["ci"]) == manifest_tool.CI_FIELDS
    assert set(manifest["debs"][0]) == manifest_tool.DEB_FIELDS
    assert manifest["version"] == "0.2.1-3"
    assert manifest["debs"][0]["version"] == "0.2.1-3"
    created_at = datetime.fromisoformat(manifest["created_at"].replace("Z", "+00:00"))
    assert manifest["created_at"].endswith("Z")
    assert created_at.tzinfo == timezone.utc
    manifest_tool.verify(args)


def test_verifier_rejects_non_exact_v1_fields(tmp_path: Path, monkeypatch) -> None:
    deb_dir = tmp_path / "debs"
    deb_dir.mkdir()
    (deb_dir / "xgc2-stt-client.deb").write_bytes(b"deb")
    monkeypatch.setattr(manifest_tool, "deb_entry", deb_metadata)

    args = arguments(tmp_path)
    manifest_tool.build(args)
    manifest_path = next((tmp_path / "manifests").glob("*.build.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencyMode"] = "staging-apt"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="build manifest fields are not exact"):
        manifest_tool.verify(args)
