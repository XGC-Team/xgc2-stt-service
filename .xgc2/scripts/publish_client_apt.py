#!/usr/bin/env python3
"""Optional trusted-artifact publish for desktop Debs when CI credentials exist."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PRODUCT = "xgc2-stt-client"
DISTRIBUTIONS = ("focal", "jammy", "noble")
ARCHITECTURES = ("amd64", "arm64")
SECRET_NAMES = (
    "APT_REPO_HOST",
    "APT_REPO_PORT",
    "APT_REPO_USER",
    "APT_REPO_SSH_KEY",
    "APT_REPO_KNOWN_HOSTS",
)


class PublishError(RuntimeError):
    pass


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(path for path in root.rglob("*") if path.is_file() and not path.is_symlink()):
        digest.update(f"{file_digest(path)}  {path.relative_to(root).as_posix()}\n".encode())
    return digest.hexdigest()


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def credential_state(env: dict[str, str] | None = None) -> str:
    source = os.environ if env is None else env
    present = sum(1 for name in SECRET_NAMES if source.get(name, "").strip())
    if present == 0:
        return "absent"
    if present == len(SECRET_NAMES):
        return "configured"
    return "partial"


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PublishError(f"{name} is required")
    return value


@contextmanager
def ssh_configuration() -> Iterator[list[str]]:
    host = required_env("APT_REPO_HOST")
    user = required_env("APT_REPO_USER")
    port = required_env("APT_REPO_PORT")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise PublishError("invalid publish port")
    key = required_env("APT_REPO_SSH_KEY")
    known_hosts = required_env("APT_REPO_KNOWN_HOSTS")
    with tempfile.TemporaryDirectory(prefix="xgc2-stt-publisher-") as directory:
        root = Path(directory)
        key_path = root / "identity"
        known_hosts_path = root / "known_hosts"
        key_path.write_text(key + ("" if key.endswith("\n") else "\n"), encoding="utf-8")
        key_path.chmod(0o600)
        known_hosts_path.write_text(
            known_hosts + ("" if known_hosts.endswith("\n") else "\n"), encoding="utf-8"
        )
        known_hosts_path.chmod(0o600)
        yield [
            "ssh",
            "-p",
            port,
            "-i",
            os.fspath(key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts_path}",
            "-o",
            "ConnectTimeout=20",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
            f"{user}@{host}",
        ]


def run_remote(command: list[str], stdin: Any = None) -> subprocess.CompletedProcess[bytes]:
    with ssh_configuration() as base:
        return subprocess.run(
            [*base, *command],
            stdin=stdin,
            capture_output=True,
            check=False,
            timeout=900,
        )


def finish(result: subprocess.CompletedProcess[bytes], context: str) -> dict[str, Any] | None:
    if result.stdout:
        sys.stdout.buffer.write(result.stdout)
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
    if result.returncode:
        raise PublishError(f"{context} failed ({result.returncode})")
    if not result.stdout.strip():
        return None
    try:
        value = json.loads(result.stdout.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def deb_metadata(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["dpkg-deb", "--show", "--showformat=${Package}\n${Version}\n${Architecture}\n", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    package, version, architecture = result.stdout.splitlines()
    return {
        "file": path.name,
        "package": package,
        "version": version,
        "architecture": architecture,
        "sha256": file_digest(path),
        "size": path.stat().st_size,
    }


def load_artifacts(
    artifact_dir: Path, version: str, source_sha: str,
) -> dict[tuple[str, str], tuple[Path, dict[str, Any], Path]]:
    selected: dict[tuple[str, str], tuple[Path, dict[str, Any], Path]] = {}
    for manifest_path in sorted(artifact_dir.rglob("*.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(manifest, dict) or manifest.get("schema") != "xgc2.build-artifact.v1":
            continue
        if manifest.get("product") != PRODUCT:
            continue
        distribution = str(manifest.get("distribution", ""))
        architecture = str(manifest.get("architecture", ""))
        if (distribution, architecture) not in {(dist, arch) for dist in DISTRIBUTIONS for arch in ARCHITECTURES}:
            raise PublishError(f"unexpected artifact identity: {manifest_path}")
        if manifest.get("version") != version or manifest.get("source_sha") != source_sha:
            raise PublishError(f"artifact identity mismatch: {manifest_path}")
        entries = manifest.get("debs")
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise PublishError(f"expected one Deb entry: {manifest_path}")
        filename = str(entries[0].get("file", ""))
        matches = [path for path in manifest_path.parent.rglob(filename) if path.is_file()]
        if len(matches) != 1:
            matches = [path for path in artifact_dir.rglob(filename) if path.is_file()]
        if len(matches) != 1:
            raise PublishError(f"missing Deb for {manifest_path}")
        actual = deb_metadata(matches[0])
        if actual != entries[0]:
            raise PublishError(f"Deb digest mismatch: {matches[0]}")
        key = (distribution, architecture)
        if key in selected:
            raise PublishError(f"duplicate artifact for {distribution}/{architecture}")
        selected[key] = (manifest_path, manifest, matches[0])
    missing = sorted(
        f"{dist}/{arch}"
        for dist in DISTRIBUTIONS
        for arch in ARCHITECTURES
        if (dist, arch) not in selected
    )
    if missing:
        raise PublishError("missing trusted artifacts: " + ", ".join(missing))
    return selected


def assemble_bundles(
    artifact_dir: Path,
    output_dir: Path,
    *,
    version: str,
    source_sha: str,
    release_id: str,
    lock_digest: str,
    run_id: str,
    workflow: str,
    workflow_ref: str,
) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    selected = load_artifacts(artifact_dir, version, source_sha)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    bundles: dict[str, Path] = {}
    products: list[dict[str, Any]] = []
    for distribution in DISTRIBUTIONS:
        bundle = output_dir / distribution
        bundle.mkdir()
        build_digests: list[str] = []
        debs: list[dict[str, str]] = []
        for architecture in ARCHITECTURES:
            manifest_path, manifest, deb_path = selected[(distribution, architecture)]
            shutil.copy2(deb_path, bundle / deb_path.name)
            manifest_digest = file_digest(manifest_path)
            included = bundle / "build-manifests" / f"{manifest_digest[:16]}-{manifest_path.name}"
            included.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(manifest_path, included)
            build_digests.append(manifest_digest)
            entry = deb_metadata(bundle / deb_path.name)
            debs.append({
                "package": str(entry["package"]),
                "version": str(entry["version"]),
                "architecture": str(entry["architecture"]),
                "sha256": str(entry["sha256"]),
            })
            release_manifest = {
                "schema": "xgc2.release-artifact.v1",
                "product": PRODUCT,
                "version": version,
                "source_sha": source_sha,
                "distribution": distribution,
                "architecture": architecture,
                "ci": {
                    "run_id": run_id,
                    "workflow": workflow,
                    "workflow_ref": workflow_ref,
                },
                "debs": [entry],
                "release_id": release_id,
                "release_lock_digest": lock_digest,
                "build_manifest": included.relative_to(bundle).as_posix(),
                "build_manifest_digest": manifest_digest,
                "published_at": timestamp,
            }
            write_json(
                bundle / "manifests" / PRODUCT / distribution / architecture
                / f"{entry['package']}_{entry['version']}.json",
                release_manifest,
            )
        debs.sort(key=lambda item: (item["package"], item["version"], item["architecture"], item["sha256"]))
        bundles[distribution] = bundle
        products.append({
            "product": PRODUCT,
            "distribution": distribution,
            "version": version,
            "source_sha": source_sha,
            "bundle_digest": tree_digest(bundle),
            "build_manifest_digests": sorted(set(build_digests)),
            "debs": debs,
        })
    return bundles, products


def publish(args: argparse.Namespace) -> int:
    state = credential_state()
    if state == "absent":
        print("optional package publish is not configured")
        return 0
    if state == "partial":
        raise PublishError("optional package publish is partially configured")

    identity = {
        "product": PRODUCT,
        "source_sha": args.source_sha,
        "version": args.version,
    }
    lock_digest = digest_bytes(canonical(identity))
    release_id = f"stt-desktop-{args.version}-{args.source_sha[:12]}"
    plan = {
        "schema": "xgc2.release-plan.v1",
        "release_id": release_id,
        "products": [identity],
    }
    plan_digest = digest_bytes(canonical(plan))
    artifact_dir = Path(args.artifact_dir).resolve(strict=True)
    work = Path(args.work_dir).resolve()
    bundles, products = assemble_bundles(
        artifact_dir,
        work / "bundles",
        version=args.version,
        source_sha=args.source_sha,
        release_id=release_id,
        lock_digest=lock_digest,
        run_id=args.run_id,
        workflow=args.workflow,
        workflow_ref=args.workflow_ref,
    )
    health = run_remote(["health"])
    finish(health, "verify")
    for distribution, bundle in bundles.items():
        with tempfile.TemporaryFile() as stream:
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for path in sorted(bundle.rglob("*")):
                    if path.is_symlink():
                        raise PublishError(f"bundle contains a symbolic link: {path}")
                    if path.is_file():
                        archive.add(path, arcname=path.relative_to(bundle).as_posix(), recursive=False)
            stream.seek(0)
            finish(
                run_remote(["stage", release_id, lock_digest, distribution], stdin=stream),
                f"stage {distribution}",
            )
    train = {
        "schema": "xgc2.release-train.v1",
        "release_id": release_id,
        "plan_digest": plan_digest,
        "release_lock_digest": lock_digest,
        "products": products,
    }
    train_path = work / "train.json"
    write_json(train_path, train)
    with train_path.open("rb") as stream:
        finish(run_remote(["promote", release_id, lock_digest], stdin=stream), "promote")
    print("optional package publish completed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--work-dir", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--workflow-ref", required=True)
    args = parser.parse_args()
    try:
        return publish(args)
    except (OSError, ValueError, PublishError, subprocess.TimeoutExpired) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
