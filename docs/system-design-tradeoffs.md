# System Design and Tradeoffs

## Architecture Choice

This platform uses Kubernetes-native reconciliation with a custom `Store` CRD:

1. Dashboard and API are stateless control-plane services.
2. API writes desired state (`Store` CR).
3. Operator reconciles desired state into concrete infra:
   - namespace-per-store
   - guardrails/policies
   - per-store Helm release

Reasoning:
- Simple control-plane boundary.
- Native idempotency/recovery via reconcile loops.
- Local and production environments share the same charts.

## Idempotency Strategy

- `POST /stores` is idempotent for same `storeId+engine`.
- Operator uses `helm upgrade --install` so retries do not duplicate releases.
- `on.resume` reconciliation re-attempts non-terminal stores after operator restart.
- Namespace and policy reconcilers are upsert-style.

## Failure Handling

- Provisioning status transitions are explicit: `Provisioning -> Ready | Failed`.
- Bounded event timeline in `status.events` captures step-level progress.
- `status.lastError` stores the latest failure reason.
- Concurrency-limited provisioning (`MAX_CONCURRENT_PROVISIONS`) reduces blast radius.
- Provision timeout (`MAX_PROVISION_SECONDS`) avoids infinite hangs.

## Cleanup Guarantees

- Each `Store` CR gets a finalizer.
- Delete flow:
  1. `helm uninstall` in store namespace
  2. delete owned namespace
  3. remove finalizer
- Delete is resilient if resources are already absent.

## Security Decisions

- No plaintext admin credentials in Store status.
- Credentials are generated and stored in per-store Kubernetes Secrets.
- API and dashboard only expose platform endpoints publicly.
- Controller uses ClusterRole scoped to required resources.
- Pod hardening is enabled for API/controller (non-root + dropped caps + no privilege escalation).

## Multi-tenant Isolation and Guardrails

- Namespace-per-store isolation.
- Per-namespace:
  - ResourceQuota
  - LimitRange
  - default-deny NetworkPolicy plus explicit required allows
- API-side guardrails:
  - global store cap
  - per-IP store cap
  - create rate limit

## Production Differences via Helm Values

No chart fork is required. Differences are values-only:

- `baseDomain`
- image registry/tag
- `storageClass`
- ingress annotations / optional TLS
- replica counts and resources

## Round 2 Readiness (Gen-AI Extension)

The operator now has explicit engine handlers and status event surfaces.
This allows adding AI-assisted provisioning workflows in Round 2 without replacing the core reconciliation model.
