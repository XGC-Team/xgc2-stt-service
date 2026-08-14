from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "docker-image.yml"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _jobs(workflow: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^  ([a-z][a-z0-9-]*):\n", workflow.split("\njobs:\n", 1)[1]))
    body = workflow.split("\njobs:\n", 1)[1]
    return {
        match.group(1): body[match.end() : matches[index + 1].start() if index + 1 < len(matches) else None]
        for index, match in enumerate(matches)
    }


def test_main_pushes_run_checks_without_entering_publish_jobs() -> None:
    workflow = _workflow()
    trigger = workflow.split("\npermissions:\n", 1)[0]
    jobs = _jobs(workflow)

    assert "  push:\n    branches: [main]\n  workflow_dispatch:" in trigger
    assert "paths-ignore:" not in trigger
    assert "tags:" not in trigger
    assert "expected_version:" in trigger
    assert "expected_source_sha:" in trigger
    assert "env -u DISPLAY -u WAYLAND_DISPLAY uv run pytest" in jobs["checks"]
    assert "if:" not in jobs["checks"]
    assert "github.event_name == 'workflow_dispatch'" in jobs["release-guard"]
    assert "github.event_name == 'workflow_dispatch'" in jobs["build"]
    assert "needs: checks" in jobs["release-guard"]
    assert "needs: release-guard" in jobs["build"]


def test_publishing_uses_one_immutable_tag_and_least_privilege() -> None:
    workflow = _workflow()
    jobs = _jobs(workflow)
    top_level = workflow.split("\njobs:\n", 1)[0]

    assert "packages:" not in top_level
    assert workflow.count("packages: write") == 1
    assert "packages: write" in jobs["build"]
    assert "packages: write" not in jobs["release-guard"]
    assert "packages: write" not in jobs["mirror"]
    assert workflow.count("push: true") == 1
    assert "tag=qwen-${version}" in jobs["release-guard"]
    assert "tags: ${{ env.IMAGE }}:${{ needs.release-guard.outputs.tag }}" in jobs["build"]
    assert "matrix:" not in workflow
    assert "${{ env.IMAGE }}:base" not in workflow
    assert "${IMAGE}:base" not in workflow
    assert "${IMAGE}:qwen" not in workflow
    assert ":latest" not in workflow
    assert "-sha-${" not in workflow


def test_release_identity_and_registry_checks_fail_closed() -> None:
    workflow = _workflow()
    jobs = _jobs(workflow)
    guard = jobs["release-guard"]
    mirror = jobs["mirror"]

    assert '"${GITHUB_REF}" == refs/heads/main' in guard
    assert '"${EXPECTED_SOURCE_SHA}" =~ ^[0-9a-f]{40}$' in guard
    assert '"${source_sha}" == "${GITHUB_SHA}"' in guard
    assert '"${source_sha}" == "${EXPECTED_SOURCE_SHA}"' in guard
    assert "git -c credential.helper= ls-remote --exit-code" in guard
    assert "refs/heads/main" in guard
    assert '"${remote_main}" == "${source_sha}"' in guard
    assert '"${version}" == "${EXPECTED_VERSION}"' in guard
    assert guard.count("skopeo list-tags") == 2
    assert "EXTRA_NAMESPACE: ${{ vars.EXTRA_NAMESPACE }}" in guard
    assert "values=(EXTRA_REGISTRY EXTRA_USERNAME EXTRA_PASSWORD)" in guard
    assert '"${present}" -eq 3 && -n "${EXTRA_NAMESPACE:-}"' in guard
    assert '"${present}" -eq 0 && -z "${EXTRA_NAMESPACE:-}"' in guard
    assert "type == \"array\"" in guard
    assert "mirror_enabled=false" in guard
    assert "mirror_enabled=true" in guard
    assert mirror.count("skopeo login") == 2
    assert "skopeo list-tags" in mirror
    assert 'source="docker://${IMAGE}@${BUILD_DIGEST}"' in mirror
    assert 'destination="docker://${target}:${RELEASE_TAG}"' in mirror
    assert '"${destination_digest}" == "${source_digest}"' in mirror


def test_all_actions_are_pinned_to_full_commit_shas() -> None:
    action_refs = re.findall(r"(?m)^\s*- uses: ([^\s#]+)", _workflow())

    assert action_refs
    assert all(re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", ref) for ref in action_refs)


def test_service_version_and_runtime_image_references_stay_aligned() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    with (ROOT / "uv.lock").open("rb") as handle:
        locked_packages = tomllib.load(handle)["package"]

    assert f'__version__ = "{version}"' in (ROOT / "src/xgc2_stt/__init__.py").read_text(encoding="utf-8")
    assert next(package["version"] for package in locked_packages if package["name"] == "xgc2-stt-service") == version
    assert f'ARG APP_VERSION={version}' in (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"APP_VERSION: ${{STT_VERSION:-{version}}}" in (ROOT / "docker-compose.build.yml").read_text(
        encoding="utf-8"
    )

    immutable_ref = f"ghcr.io/xgc-team/xgc2-stt-service:qwen-{version}"
    assert immutable_ref in (ROOT / ".env.example").read_text(encoding="utf-8")
    assert (ROOT / "docker-compose.yml").read_text(encoding="utf-8").count(immutable_ref) == 2
    assert f'${{registry}}:qwen-{version}' in (ROOT / "scripts/deploy-local.sh").read_text(encoding="utf-8")
