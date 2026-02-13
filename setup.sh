#!/bin/bash
set -e

CLUSTER_NAME="store-platform"
REGISTRY_NAME="kind-registry"
REGISTRY_PORT="5001"

echo "Checking prerequisites..."
command -v kind >/dev/null 2>&1 || { echo >&2 "kind is not installed. Aborting."; exit 1; }
command -v kubectl >/dev/null 2>&1 || { echo >&2 "kubectl is not installed. Aborting."; exit 1; }
command -v helm >/dev/null 2>&1 || { echo >&2 "helm is not installed. Aborting."; exit 1; }
command -v docker >/dev/null 2>&1 || { echo >&2 "docker is not installed. Aborting."; exit 1; }

echo "Creating Kind cluster..."
if kind get clusters | grep -q "^$CLUSTER_NAME$"; then
    echo "Cluster $CLUSTER_NAME already exists."
else
    kind create cluster --name "$CLUSTER_NAME" --config kind-config.yaml
fi

echo "Building images..."
# Build API
echo "Building API image..."
docker build -f apps/api/Dockerfile -t urumi/platform-api:local .
# Build Controller
echo "Building Controller image..."
docker build -f controller/Dockerfile -t urumi/store-controller:local .
# Build Dashboard
echo "Building Dashboard image..."
docker build --build-arg VITE_API_BASE=http://api.127.0.0.1.nip.io -t urumi/dashboard:local apps/dashboard

echo "Loading images into Kind..."
kind load docker-image urumi/platform-api:local --name "$CLUSTER_NAME"
kind load docker-image urumi/store-controller:local --name "$CLUSTER_NAME"
kind load docker-image urumi/dashboard:local --name "$CLUSTER_NAME"

echo "Installing Ingress NGINX..."
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/main/deploy/static/provider/kind/deploy.yaml

echo "Waiting for Ingress NGINX to be ready..."
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=90s

echo "Installing Platform Chart..."
# Ensure dependencies (none for now, but good practice)
helm upgrade --install store-platform ./charts/platform \
  --namespace store-platform \
  --create-namespace \
  -f ./charts/platform/values-local.yaml \
  --wait

echo "Setup complete! Dashboard should be available at http://localhost:80 (via Ingress) or via port-forward if Ingress DNS isn't set up."
echo "For dashboard on localhost via Ingress, ensure '127.0.0.1 dashboard.127.0.0.1.nip.io' is resolvable (it should be automatically)."
