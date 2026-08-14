# Shared by GitHub Actions image jobs. Sets EXTRA_ROOT when the four extra
# registry secrets are all present. Do not print secret values.
repo_lc="$(printf '%s' "${GITHUB_REPOSITORY}" | tr '[:upper:]' '[:lower:]')"
ns="$(printf '%s' "${EXTRA_NAMESPACE:-}" | sed 's#^/*##; s#/*$##')"
GHCR_ROOT="ghcr.io/${repo_lc}"
EXTRA_ROOT=""
if [[ -n "${EXTRA_REGISTRY:-}" && -n "${ns}" && -n "${EXTRA_USERNAME:-}" && -n "${EXTRA_PASSWORD:-}" ]]; then
  EXTRA_ROOT="${EXTRA_REGISTRY%/}/${ns}"
fi
