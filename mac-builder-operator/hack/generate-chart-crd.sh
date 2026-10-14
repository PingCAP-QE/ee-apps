#!/usr/bin/env bash
# Generate (or verify) the mac-builder-operator Helm chart CRD template from the
# controller-gen output.
#
# The chart ships its own copy of the MacBuild CRD, which MUST stay identical to
# `mac-builder-operator/config/crd/bases/tibuild.pingcap.net_macbuilds.yaml`
# (only the Helm wrapper and the labels include differ). Any field added to the
# CRD but missing here is silently pruned by the API server on real clusters.
#
# Usage:
#   ./hack/generate-chart-crd.sh          # rewrite the chart template
#   ./hack/generate-chart-crd.sh --check  # fail if the template is out of sync
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
base="$root/mac-builder-operator/config/crd/bases/tibuild.pingcap.net_macbuilds.yaml"
chart="$root/charts/mac-builder-operator/templates/crd.yaml"

if [[ ! -f "$base" ]]; then
  echo "missing generated CRD: $base (run 'make manifests' first)" >&2
  exit 1
fi

render() {
  printf '%s\n' '{{- if .Values.crd.create -}}'
  # Drop the leading '---' and inject the chart labels into the metadata block.
  tail -n +2 "$base" | awk '
    { print }
    /^  name: macbuilds\.tibuild\.pingcap\.net$/ {
      print "  labels:"
      print "    {{- include \"mac-builder-operator.labels\" . | nindent 4 }}"
    }'
  printf '%s\n' '{{- end }}'
}

if [[ "${1:-}" == "--check" ]]; then
  tmp="$(mktemp)"
  trap 'rm -f "$tmp"' EXIT
  render > "$tmp"
  if ! diff -u "$tmp" "$chart"; then
    echo "" >&2
    echo "chart CRD template is out of sync: $chart" >&2
    echo "run 'mac-builder-operator/hack/generate-chart-crd.sh' and commit the result" >&2
    exit 1
  fi
  echo "chart CRD template is in sync"
  exit 0
fi

render > "$chart"
echo "wrote $chart"
