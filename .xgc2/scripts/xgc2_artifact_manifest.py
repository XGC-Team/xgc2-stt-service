#!/usr/bin/env python3
"""Create or verify an xgc2.build-artifact.v2 manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "xgc2.build-artifact.v2"
SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def deb_entry(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["dpkg-deb", "--show", "--showformat=${Package}\n${Version}\n${Architecture}\n", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    if len(result) != 3 or not all(result):
        raise ValueError(f"cannot read Deb identity from {path}")
    return {
        "file": path.name,
        "package": result[0],
        "version": result[1],
        "architecture": result[2],
        "sha256": digest(path),
        "size": path.stat().st_size,
    }


def identity(args: argparse.Namespace) -> None:
    if args.product != "xgc2-stt-client":
        raise ValueError("unexpected product")
    if args.distribution not in {"focal", "jammy", "noble"} or args.architecture not in {"amd64", "arm64"}:
        raise ValueError("unsupported distribution or architecture")
    if not SHA.fullmatch(args.source_sha):
        raise ValueError("source_sha must contain 40 or 64 lowercase hex characters")


def build(args: argparse.Namespace) -> None:
    identity(args)
    debs = sorted(Path(args.deb_dir).glob("*.deb"))
    if len(debs) != 1:
        raise ValueError("expected exactly one Deb")
    entry = deb_entry(debs[0])
    expected_version = args.product_version
    if entry["package"] != args.product or entry["version"] != expected_version:
        raise ValueError("Deb product or version does not match requested identity")
    if entry["architecture"] != args.architecture:
        raise ValueError("Deb architecture does not match requested identity")
    payload = {
        "schema": SCHEMA,
        "product": args.product,
        "version": args.product_version,
        "distribution": args.distribution,
        "architecture": args.architecture,
        "source_sha": args.source_sha,
        "ci": {
            "run_id": str(args.ci_run_id),
            "workflow": args.ci_workflow,
            "workflow_ref": args.ci_workflow_ref,
        },
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "debs": [entry],
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{args.product}_{args.distribution}_{args.architecture}.build.json"
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify(args: argparse.Namespace) -> None:
    identity(args)
    root = Path(args.artifact_dir).resolve(strict=True)
    if root.is_symlink() or any(path.is_symlink() for path in root.rglob("*")):
        raise ValueError("artifact input must not contain symbolic links")
    version = args.product_version
    matches: list[tuple[Path, Path]] = []
    for manifest_path in root.rglob("*.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        expected = {
            "schema": SCHEMA,
            "product": args.product,
            "version": version,
            "distribution": args.distribution,
            "architecture": args.architecture,
            "source_sha": args.source_sha,
        }
        if not all(manifest.get(key) == value for key, value in expected.items()):
            continue
        entries = manifest.get("debs")
        if not isinstance(entries, list) or len(entries) != 1:
            raise ValueError(f"{manifest_path}: expected one Deb entry")
        candidate = list(root.rglob(str(entries[0].get("file", ""))))
        if len(candidate) != 1 or deb_entry(candidate[0]) != entries[0]:
            raise ValueError(f"{manifest_path}: Deb identity mismatch")
        matches.append((manifest_path, candidate[0]))
    if len(matches) != 1:
        raise ValueError("expected exactly one matching trusted build manifest")
    deb_out = Path(args.deb_output_dir)
    manifest_out = Path(args.manifest_output_dir)
    deb_out.mkdir(parents=True, exist_ok=True)
    manifest_out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(matches[0][1], deb_out / matches[0][1].name)
    shutil.copy2(matches[0][0], manifest_out / matches[0][0].name)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    commands = result.add_subparsers(required=True)
    common = argparse.ArgumentParser(add_help=False)
    for name in ("product", "product_version", "distribution", "architecture", "source_sha"):
        common.add_argument(f"--{name.replace('_', '-')}", dest=name, required=True)
    create = commands.add_parser("build", parents=[common])
    create.add_argument("--deb-dir", required=True)
    create.add_argument("--output-dir", required=True)
    create.add_argument("--ci-run-id", required=True)
    create.add_argument("--ci-workflow", required=True)
    create.add_argument("--ci-workflow-ref", required=True)
    create.set_defaults(function=build)
    check = commands.add_parser("verify-build", parents=[common])
    check.add_argument("--artifact-dir", required=True)
    check.add_argument("--deb-output-dir", required=True)
    check.add_argument("--manifest-output-dir", required=True)
    check.set_defaults(function=verify)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        args.function(args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
