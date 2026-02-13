# Operations Guide

## Platform Install / Upgrade

Install:

```bash
helm upgrade --install store-platform charts/platform \
  --namespace store-platform --create-namespace \
  -f charts/platform/values-local.yaml
```

Upgrade with production values:

```bash
helm upgrade store-platform charts/platform \
  --namespace store-platform \
  -f charts/platform/values-prod.yaml
```

## Rollback

```bash
helm history store-platform -n store-platform
helm rollback store-platform <REVISION> -n store-platform
```

## Store Lifecycle Operations

Create:

```bash
curl -X POST http://api.127.0.0.1.nip.io/stores \
  -H 'Content-Type: application/json' \
  -d '{"engine":"woocommerce","storeId":"demo1"}'
```

Inspect status:

```bash
curl http://api.127.0.0.1.nip.io/stores/demo1 | jq
curl http://api.127.0.0.1.nip.io/stores/demo1/events | jq
```

Delete:

```bash
curl -X DELETE http://api.127.0.0.1.nip.io/stores/demo1
```

## Troubleshooting

### Store stuck in Provisioning

1. Check controller logs:

```bash
kubectl logs -n store-platform deploy/store-controller -f
```

2. Check Store CR status:

```bash
kubectl get stores.stores.urumi.ai -n store-platform <STORE_ID> -o yaml
```

3. Inspect namespace resources:

```bash
kubectl get all -n store-<STORE_ID>
kubectl get events -n store-<STORE_ID> --sort-by=.lastTimestamp
```

### Helm release failed

```bash
helm list -n store-<STORE_ID>
helm status woocommerce-<STORE_ID> -n store-<STORE_ID>
```

### Cleanup verification

After delete, confirm namespace removal:

```bash
kubectl get ns store-<STORE_ID>
```

If still terminating, inspect finalizers:

```bash
kubectl get ns store-<STORE_ID> -o yaml
```

## Demo Checklist (Video)

1. Show architecture and component responsibilities.
2. Create a store from dashboard.
3. Show namespace/resources appearing.
4. Show status transitions to Ready with URL.
5. Place/verify an order (manual or smoke script output).
6. Delete store and verify namespace cleanup.
7. Explain guardrails, security posture, and rollback story.
