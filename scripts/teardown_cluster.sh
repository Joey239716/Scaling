#!/usr/bin/env bash
# Delete the local experiment cluster and everything in it (Prometheus data
# included). Experiment results in experiments/results/ are NOT touched.
#
#   ./scripts/teardown_cluster.sh
set -euo pipefail

PROFILE="${PROFILE:-scale}"   # same default as setup_cluster.sh

if minikube profile list -o json 2>/dev/null | grep -q "\"Name\":\"$PROFILE\""; then
  minikube delete -p "$PROFILE"
  echo "Deleted Minikube profile '$PROFILE'."
else
  echo "No Minikube profile '$PROFILE'; nothing to delete."
fi
