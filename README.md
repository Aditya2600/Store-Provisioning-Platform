# Urumi Store Provisioning Platform (Round 1)

A Kubernetes-native platform for provisioning isolated e-commerce stores (WooCommerce) on command.

## 📦 Deliverables

### Source Code
- **Dashboard**: [`apps/dashboard`](apps/dashboard) (React + Vite + Tailwind/Lucide)
- **Backend API**: [`apps/api`](apps/api) (FastAPI)
- **Orchestration**: [`controller/operator.py`](controller/operator.py) (Kopf/Python)
- **Helm Charts**: [`charts/`](charts/) (Platform & Store charts)

---

## 🚀 Setup Instructions

### 1. Local Setup (Kind)
The easiest way to run the platform locally.

**Prerequisites:** Docker, Kubectl, Helm, Kind.

**Steps:**
1. Run the setup script:
   ```bash
   ./setup.sh
   ```
   *This handles cluster creation, image building, ingress installation, and platform deployment.*

2. Access the platform:
   - **Dashboard**: [http://dashboard.127.0.0.1.nip.io](http://dashboard.127.0.0.1.nip.io)
   - **API**: [http://api.127.0.0.1.nip.io](http://api.127.0.0.1.nip.io/docs)

### 2. VPS / Production-like Setup (k3s)
To deploy on a remote VPS (e.g., DigitalOcean, AWS EC2) using `k3s`.

**Steps:**
1. **Install k3s**:
   ```bash
   curl -sfL https://get.k3s.io | sh -
   ```
2. **Build & Push Images**:
   Build the images locally and push them to a container registry (Docker Hub, GHCR, ECR).
   ```bash
   docker build -t your-repo/platform-api:latest -f apps/api/Dockerfile .
   docker build -t your-repo/store-controller:latest -f controller/Dockerfile .
   docker build --build-arg VITE_API_BASE=http://api.your-domain.com -t your-repo/dashboard:latest apps/dashboard
   
   docker push your-repo/platform-api:latest
   docker push your-repo/store-controller:latest
   docker push your-repo/dashboard:latest
   ```
3. **Configure Values**:
   Create a `values-prod.yaml` for the platform chart:
   ```yaml
   baseDomain: "your-domain.com"
   images:
     api: "your-repo/platform-api:latest"
     controller: "your-repo/store-controller:latest"
     dashboard: "your-repo/dashboard:latest"
   ingressClassName: "traefik" # k3s uses traefik by default
   ```
4. **Deploy**:
   ```bash
   helm upgrade --install store-platform charts/platform \
     -n store-platform --create-namespace \
     -f values-prod.yaml
   ```

---

## 🛒 How to Use

### Create a Store
1. Open the **Dashboard**.
2. Enter a unique **Store ID** (e.g., `fashion-boutique`).
3. Select **WooCommerce** as the engine.
4. Click **Launch Store**.
5. Watch the status move from `Provisioning` → `Ready`.

### Place an Order (Manual Verification)
1. Once `Ready`, click **Visit Store** in the dashboard.
2. (Optional) Log in to `/wp-admin` using the `admin` credentials stored in the store's namespace Secret (`store-fashion-boutique/store-admin`).
3. Browse the shop, add a product to the cart.
4. Proceed to checkout and place an order (Cash on Delivery is enabled by default in our automation, or enable it manually).

---

## ⚙️ Helm Charts & Configuration

### Platform Chart (`charts/platform`)
Deploys the core infrastructure: Dashboard, API, and Controller.

- **Values Files**:
  - `values.yaml`: Default settings.
  - `values-local.yaml`: Overrides for local `kind` dev (local images, `nip.io` domain).
  - *(Create your own `values-prod.yaml` for VPS deployment)*.

### Store Charts (`charts/woocommerce`, `charts/medusa`)
Templated deployments for individual stores.

- **WooCommerce**: Wrapper around `bitnami/wordpress`.
- **Medusa**: Stubbed for Round 1.

---

## 🏗️ System Design & Tradeoffs

### Architecture Choice
We chose a **Kubernetes Operator pattern** (using Kopf) over a simple API-driven Terraform/Script approach.
- **Why?** It makes "Store" a first-class Kubernetes citizen. The operator allows for active reconciliation (self-healing), easy status reporting, and native integration with K8s events.
- **Tradeoff**: Higher complexity than a simple script, but significantly more robust for day-2 operations.

### Idempotency & Failure Handling
- **Idempotency**: The operator checks for existing resources before creating them. Updating a `Store` CR updates the underlying Helm release.
- **Failure Handling**: If a provisioning step fails (e.g., Helm chart error), the operator retries with exponential backoff. The error is captured and displayed in the Dashboard.
- **Cleanup**: We use **Finalizers**. When you delete a Store, the operator intercepts the deletion, uninstalls the Helm release, deletes the Namespace, and only then allows the CR to be removed. This prevents "orphaned" resources.

### Production Considerations
To move this from `kind` to Production, change the following:
1. **DNS**: Replace `nip.io` with a real DNS provider (Cloudflare, AWS Route53) and ExternalDNS.
2. **Ingress**: Use a production Ingress Class (e.g., Nginx, ALB) with **Cert-Manager** for automatic Let's Encrypt SSL.
3. **Storage**: Switch from `standard` (hostPath) to a managed CSI driver (e.g., EBS gp3, Longhorn).
4. **Secrets**: Integrate with Vault or AWS Secrets Manager instead of native K8s Secrets for sensitive credentials.
5. **Observability**: Add Prometheus/Grafana for monitoring controller health and store metrics.
