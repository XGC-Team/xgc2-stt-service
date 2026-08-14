#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
mode="${1:-source}"

# Block deployment values from this workspace. Generic examples shipped in
# third-party license/metadata files (for example localhost:8765) aren't a
# configured endpoint and are intentionally outside this denylist.
forbidden_regex='xiaokang\.ink|10\.10\.10\.[0-9]+|(:|%3A)(34896|34897)([^0-9]|$)|crpi-[[:alnum:]]+\.'
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

case "${mode}" in
  source)
    scan_text "${repo_root}/src/xgc2_stt/desktop.py"
    scan_text "${repo_root}/src/xgc2_stt/desktop_audio.py"
    scan_text "${repo_root}/src/xgc2_stt/desktop_cli.py"
    scan_text "${repo_root}/src/xgc2_stt/desktop_support.py"
    scan_text "${repo_root}/README.md"
    scan_text "${repo_root}/THIRD_PARTY_NOTICES.md"
    scan_text "${repo_root}/.xgc2/desktop"
    scan_text "${repo_root}/.xgc2/product.yml"
    scan_text "${repo_root}/.github/workflows/client-deb.yml"
    scan_text "${repo_root}/.github/workflows/client-deb-ci.yml"
    for source_file in "${repo_root}"/.xgc2/scripts/*; do
      [[ "${source_file}" == "${repo_root}/.xgc2/scripts/check_client_privacy.sh" ]] || \
        scan_text "${source_file}"
    done
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
