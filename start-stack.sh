#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
minikube status >/dev/null
python3 dashboard.py
kubectl --context minikube apply -f - <<'YAML'
apiVersion: v1
kind: Namespace
metadata:
  name: cache-e2e
YAML
kubectl --context minikube -n cache-e2e create configmap cache-dashboard \
  --from-file=dashboard.json --from-file=cache-operations.json --dry-run=client -o yaml |
  kubectl --context minikube apply -f -
kubectl --context minikube apply -f stack.yaml
for deployment in mock-backend valkey prometheus grafana loki tempo; do
  kubectl --context minikube -n cache-e2e rollout status "deployment/$deployment" --timeout=120s
done
exec python3 lifecycle.py forwards
