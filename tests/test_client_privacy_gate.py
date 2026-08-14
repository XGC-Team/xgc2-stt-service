from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".xgc2" / "scripts" / "check_client_privacy.sh"


def test_privacy_gate_fails_closed_when_scanner_errors(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_rg = fake_bin / "rg"
    fake_rg.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    fake_rg.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"

    result = subprocess.run(
        [str(SCRIPT), "source"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "Privacy scan failed" in result.stderr
    assert "xiaokang" not in result.stdout + result.stderr


def test_privacy_gate_scans_deb_control_metadata(tmp_path: Path) -> None:
    package_root = tmp_path / "package"
    control_root = package_root / "DEBIAN"
    control_root.mkdir(parents=True)
    (control_root / "control").write_text(
        "\n".join(
            [
                "Package: privacy-probe",
                "Version: 1.0-1",
                "Architecture: all",
                "Maintainer: Test <test@example.invalid>",
                "Description: xiaokang.ink must never enter control metadata",
                "",
            ]
        ),
        encoding="utf-8",
    )
    deb = tmp_path / "privacy-probe.deb"
    subprocess.run(["dpkg-deb", "--build", str(package_root), str(deb)], check=True, capture_output=True)

    result = subprocess.run(
        [str(SCRIPT), "deb", str(deb)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Private or deployment-specific endpoint data" in result.stderr
    assert "xiaokang" not in result.stdout + result.stderr
