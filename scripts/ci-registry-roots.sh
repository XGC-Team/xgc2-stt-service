# Shared by GitHub Actions image jobs. Sets EXTRA_ROOT when the three extra
# registry secrets and EXTRA_NAMESPACE (an Actions variable) are all present.
# Do not print secret values.
repo_lc="$(printf '%s' "${GITHUB_REPOSITORY}" | tr '[:upper:]' '[:lower:]')"
ns="$(printf '%s' "${EXTRA_NAMESPACE:-}" | sed 's#^/*##; s#/*$##')"
GHCR_ROOT="ghcr.io/${repo_lc}"
EXTRA_ROOT=""
extra_secrets=0
for key in EXTRA_REGISTRY EXTRA_USERNAME EXTRA_PASSWORD; do
  [[ -n "${!key:-}" ]] && extra_secrets=$((extra_secrets + 1))
done
if [[ "${extra_secrets}" -eq 3 && -n "${ns}" ]]; then
  EXTRA_ROOT="${EXTRA_REGISTRY%/}/${ns}"
elif [[ "${extra_secrets}" -ne 0 ]]; then
  echo "extra registry is partially configured" >&2
  exit 1
fi
