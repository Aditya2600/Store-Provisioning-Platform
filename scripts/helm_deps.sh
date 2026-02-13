#!/usr/bin/env bash
set -euo pipefail
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update
(cd charts/woocommerce && helm dependency update)
echo "Helm dependencies updated."
