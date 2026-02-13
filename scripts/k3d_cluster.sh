#!/usr/bin/env bash
set -euo pipefail
k3d cluster create urumi --servers 1 --agents 2 -p "80:80@loadbalancer" -p "443:443@loadbalancer"
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
helm repo update
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx --namespace ingress-nginx --create-namespace
echo "Cluster ready."
