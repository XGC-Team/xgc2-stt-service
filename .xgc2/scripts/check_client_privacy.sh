#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
mode="${1:-source}"

# Generic leak classes for this public repository. Loopback examples such as
# 127.0.0.1 remain allowed. Operator-specific hostnames, networks, ports, and
# registry instance IDs are not stored here.
forbidden_regex='(://|%3A%2F%2F)(10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(1[6-9]|2[0-9]|3[0-1])\.[0-9]{1,3}\.[0-9]{1,3})'

scan_text() {
  local target="$1"
  local status
  set +e
  if command -v rg >/dev/null 2>&1; then
    LC_ALL=C rg -a -q -i -- "${forbidden_regex}" "${target}"
  else
    # Distro build containers and GitHub-hosted runners may not ship ripgrep.
    LC_ALL=C grep -RaEq -i -- "${forbidden_regex}" "${target}"
  fi
  status=$?
  set -e
  case "${status}" in
    0)
      echo "Private or deployment-specific endpoint data found in ${target}." >&2
      return 1
      ;;
    1) return 0 ;;
    *)
      echo "Privacy scan failed for ${target} (rg exit ${status})." >&2
      return "${status}"
      ;;
  esac
}

scan_tracked_public_tree() {
  local rel path count=0
  git -C "${repo_root}" rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
    echo "source scan requires a git checkout." >&2
    return 2
  }
  while IFS= read -r -d '' rel; do
    path="${repo_root}/${rel}"
    [[ -f "${path}" ]] || continue
    count=$((count + 1))
    scan_text "${path}" || return $?
  done < <(
    git -C "${repo_root}" ls-files -z -- . \
      ':!*.png' ':!*.jpg' ':!*.jpeg' ':!*.gif' ':!*.webp' ':!*.ico' \
      ':!uv.lock' ':!**/package-lock.json' ':!*.pyc'
  )
  [[ "${count}" -gt 0 ]] || {
    echo "source scan found no tracked public files." >&2
    return 2
  }
}

case "${mode}" in
  source)
    scan_tracked_public_tree
    python3 - "${repo_root}/src/xgc2_stt/desktop_support.py" <<'PY'
import ast
import sys

tree = ast.parse(open(sys.argv[1], encoding="utf-8").read())
defaults = {}
for node in ast.walk(tree):
    if not isinstance(node, ast.ClassDef) or node.name != "DesktopSettings":
        continue
    for statement in node.body:
        if isinstance(statement, ast.AnnAssign):
            name = getattr(statement.target, "id", "")
            if name in {"endpoint", "api_key"}:
                defaults[name] = statement.value
for name in ("endpoint", "api_key"):
    value = defaults.get(name)
    if not isinstance(value, ast.Constant) or value.value != "":
        raise SystemExit(f"DesktopSettings.{name} must default to an empty string")
PY
    ;;
  deb)
    deb="${2:?usage: check_client_privacy.sh deb PACKAGE.deb}"
    extracted="$(mktemp -d)"
    trap 'rm -rf -- "${extracted}"' EXIT
    dpkg-deb --extract "${deb}" "${extracted}"
    install -d "${extracted}/DEBIAN"
    dpkg-deb --control "${deb}" "${extracted}/DEBIAN"
    scan_text "${extracted}"
    ;;
  *) echo "usage: check_client_privacy.sh [source|deb PACKAGE.deb]" >&2; exit 2 ;;
esac
