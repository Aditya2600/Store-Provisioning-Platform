import base64
import os
import secrets
import string
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import kopf
from kubernetes import client, config
from kubernetes.client.rest import ApiException

PLATFORM_NAMESPACE = os.getenv("PLATFORM_NAMESPACE", "store-platform")
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "127.0.0.1.nip.io")
URL_SCHEME = os.getenv("URL_SCHEME", "http")
INGRESS_CLASS = os.getenv("INGRESS_CLASS", "nginx")
STORE_NS_PREFIX = os.getenv("STORE_NS_PREFIX", "store-")
STORAGE_CLASS = os.getenv("STORAGE_CLASS", "")

MAX_PROVISION_SECONDS = int(os.getenv("MAX_PROVISION_SECONDS", "900"))
MAX_CONCURRENT_PROVISIONS = int(os.getenv("MAX_CONCURRENT_PROVISIONS", "2"))
MAX_STATUS_EVENTS = int(os.getenv("MAX_STATUS_EVENTS", "20"))
OPERATOR_WORKERS = int(os.getenv("OPERATOR_WORKERS", "4"))

HELM_BIN = os.getenv("HELM_BIN", "helm")
CHART_WOOCOMMERCE = os.getenv("CHART_WOOCOMMERCE", "/charts/woocommerce")
CHART_MEDUSA = os.getenv("CHART_MEDUSA", "/charts/medusa")

CRD_GROUP = os.getenv("STORE_CRD_GROUP", "stores.urumi.ai")
CRD_VERSION = os.getenv("STORE_CRD_VERSION", "v1alpha1")
CRD_PLURAL = os.getenv("STORE_CRD_PLURAL", "stores")

FINALIZER = "stores.urumi.ai/finalizer"
STORE_MANAGED_LABEL = "urumi.ai/managed-store"
STORE_ID_LABEL = "urumi.ai/storeId"
ADMIN_SECRET_NAME = os.getenv("STORE_ADMIN_SECRET_NAME", "store-admin")

_provision_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_PROVISIONS)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_kube() -> None:
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()


_load_kube()
core = client.CoreV1Api()
co = client.CustomObjectsApi()
net = client.NetworkingV1Api()


@dataclass(frozen=True)
class EngineHandler:
    name: str
    chart_path: str

    def build_release_name(self, store_id: str) -> str:
        return f"{self.name}-{store_id}"

    def build_helm_args(
        self,
        store_id: str,
        namespace: str,
        host: str,
        admin_user: str,
        admin_password: str,
    ) -> List[str]:
        timeout = f"--timeout={MAX_PROVISION_SECONDS}s"
        if self.name == "woocommerce":
            args = [
                "upgrade",
                "--install",
                self.build_release_name(store_id),
                self.chart_path,
                "-n",
                namespace,
                "--wait",
                timeout,
                # Bitnami WordPress chart values
                "--set",
                "wordpress.ingress.enabled=true",
                "--set-string",
                f"wordpress.ingress.ingressClassName={INGRESS_CLASS}",
                "--set-string",
                f"wordpress.ingress.hostname={host}",
                "--set-string",
                "wordpress.service.type=ClusterIP",
                "--set-string",
                f"wordpress.wordpressUsername={admin_user}",
                "--set-string",
                f"wordpress.wordpressPassword={admin_password}",
                "--set-string",
                f"wordpress.wordpressEmail=admin@{host}",
                "--set-string",
                f"wordpress.wordpressBlogName={store_id}",
                "--set-string",
                "wordpress.wordpressPlugins=woocommerce",
            ]
            if STORAGE_CLASS:
                args.extend(
                    [
                        "--set-string",
                        f"wordpress.persistence.storageClass={STORAGE_CLASS}",
                        "--set-string",
                        f"wordpress.mariadb.primary.persistence.storageClass={STORAGE_CLASS}",
                    ]
                )
            return args

        # Medusa stub path (Round 1)
        return [
            "upgrade",
            "--install",
            self.build_release_name(store_id),
            self.chart_path,
            "-n",
            namespace,
            "--wait",
            timeout,
            "--set-string",
            f"ingress.className={INGRESS_CLASS}",
            "--set-string",
            f"ingress.hostname={host}",
        ]

    def post_ready_checks(self, store_id: str, namespace: str) -> None:
        # Placeholder for future engine-specific post checks.
        _ = (store_id, namespace)


ENGINE_HANDLERS: Dict[str, EngineHandler] = {
    "woocommerce": EngineHandler(name="woocommerce", chart_path=CHART_WOOCOMMERCE),
    "medusa": EngineHandler(name="medusa", chart_path=CHART_MEDUSA),
}


def run_helm(args: List[str], timeout: Optional[int] = None) -> str:
    cmd = [HELM_BIN] + args
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout or (MAX_PROVISION_SECONDS + 60),
    )
    if proc.returncode != 0:
        details = proc.stderr.strip() or proc.stdout.strip() or "unknown error"
        raise RuntimeError(f"helm failed: {details}")
    return proc.stdout.strip()


def store_namespace(store_id: str) -> str:
    return f"{STORE_NS_PREFIX}{store_id}"


def store_host(store_id: str) -> str:
    return f"{store_id}.{BASE_DOMAIN}"


def store_url(store_id: str) -> str:
    return f"{URL_SCHEME}://{store_host(store_id)}"


def rand_password(n: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def get_store(name: str) -> Dict:
    return co.get_namespaced_custom_object(
        group=CRD_GROUP,
        version=CRD_VERSION,
        namespace=PLATFORM_NAMESPACE,
        plural=CRD_PLURAL,
        name=name,
    )


def _safe_patch_store_status(name: str, status: Dict) -> None:
    try:
        co.patch_namespaced_custom_object_status(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=PLATFORM_NAMESPACE,
            plural=CRD_PLURAL,
            name=name,
            body={"status": status},
        )
    except ApiException as e:
        if e.status == 404:
            return
        raise


def patch_store_status(
    name: str,
    *,
    phase: Optional[str] = None,
    event_type: Optional[str] = None,
    event_message: Optional[str] = None,
    set_fields: Optional[Dict] = None,
    drop_fields: Optional[List[str]] = None,
) -> None:
    obj = get_store(name)
    current = obj.get("status", {}) or {}

    if phase:
        current["phase"] = phase

    if set_fields:
        for key, value in set_fields.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value

    if drop_fields:
        for field_name in drop_fields:
            current.pop(field_name, None)

    if "createdAt" not in current:
        current["createdAt"] = now_iso()
    current["updatedAt"] = now_iso()

    events = current.get("events", []) or []
    if event_type:
        events.append(
            {
                "type": event_type,
                "message": event_message or "",
                "timestamp": now_iso(),
            }
        )
        events = events[-MAX_STATUS_EVENTS:]
    current["events"] = events

    _safe_patch_store_status(name, current)


def add_finalizer(name: str) -> None:
    obj = get_store(name)
    finalizers = obj.get("metadata", {}).get("finalizers", []) or []
    if FINALIZER in finalizers:
        return
    finalizers.append(FINALIZER)
    co.patch_namespaced_custom_object(
        CRD_GROUP,
        CRD_VERSION,
        PLATFORM_NAMESPACE,
        CRD_PLURAL,
        name,
        {"metadata": {"finalizers": finalizers}},
    )


def remove_finalizer(name: str) -> None:
    try:
        obj = get_store(name)
    except ApiException as e:
        if e.status == 404:
            return
        raise
    finalizers = obj.get("metadata", {}).get("finalizers", []) or []
    if FINALIZER not in finalizers:
        return
    finalizers = [f for f in finalizers if f != FINALIZER]
    co.patch_namespaced_custom_object(
        CRD_GROUP,
        CRD_VERSION,
        PLATFORM_NAMESPACE,
        CRD_PLURAL,
        name,
        {"metadata": {"finalizers": finalizers}},
    )


def ensure_namespace(ns: str, store_id: str) -> None:
    labels = {STORE_MANAGED_LABEL: "true", STORE_ID_LABEL: store_id}
    try:
        existing = core.read_namespace(ns)
        current_labels = existing.metadata.labels or {}
        if any(current_labels.get(k) != v for k, v in labels.items()):
            current_labels.update(labels)
            body = {"metadata": {"labels": current_labels}}
            core.patch_namespace(ns, body)
        return
    except ApiException as e:
        if e.status != 404:
            raise

    body = client.V1Namespace(metadata=client.V1ObjectMeta(name=ns, labels=labels))
    core.create_namespace(body)


def apply_resourcequota(ns: str) -> None:
    rq = client.V1ResourceQuota(
        metadata=client.V1ObjectMeta(name="store-quota", namespace=ns),
        spec=client.V1ResourceQuotaSpec(
            hard={
                "pods": "10",
                "requests.cpu": "2",
                "requests.memory": "2Gi",
                "limits.cpu": "4",
                "limits.memory": "4Gi",
                "persistentvolumeclaims": "5",
                "requests.storage": "20Gi",
            }
        ),
    )
    try:
        core.create_namespaced_resource_quota(ns, rq)
    except ApiException as e:
        if e.status != 409:
            raise
        core.patch_namespaced_resource_quota(
            name="store-quota",
            namespace=ns,
            body={"spec": {"hard": rq.spec.hard}},
        )


def apply_limitrange(ns: str) -> None:
    lr = client.V1LimitRange(
        metadata=client.V1ObjectMeta(name="store-limits", namespace=ns),
        spec=client.V1LimitRangeSpec(
            limits=[
                client.V1LimitRangeItem(
                    type="Container",
                    default={"cpu": "500m", "memory": "512Mi"},
                    default_request={"cpu": "200m", "memory": "256Mi"},
                )
            ]
        ),
    )
    try:
        core.create_namespaced_limit_range(ns, lr)
    except ApiException as e:
        if e.status != 409:
            raise
        core.patch_namespaced_limit_range(
            name="store-limits",
            namespace=ns,
            body={"spec": {"limits": [lr.spec.limits[0].to_dict()]}},
        )


def apply_networkpolicy_default_deny(ns: str) -> None:
    policy = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name="default-deny", namespace=ns),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(match_labels={}),
            policy_types=["Ingress", "Egress"],
        ),
    )
    try:
        net.create_namespaced_network_policy(ns, policy)
    except ApiException as e:
        if e.status != 409:
            raise
        net.patch_namespaced_network_policy(
            name="default-deny",
            namespace=ns,
            body=policy,
        )


def apply_networkpolicy_allow_required(ns: str) -> None:
    policy = client.V1NetworkPolicy(
        metadata=client.V1ObjectMeta(name="allow-required", namespace=ns),
        spec=client.V1NetworkPolicySpec(
            pod_selector=client.V1LabelSelector(match_labels={}),
            policy_types=["Ingress", "Egress"],
            ingress=[
                client.V1NetworkPolicyIngressRule(
                    _from=[
                        client.V1NetworkPolicyPeer(
                            namespace_selector=client.V1LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "ingress-nginx"}
                            )
                        ),
                        client.V1NetworkPolicyPeer(
                            pod_selector=client.V1LabelSelector(match_labels={})
                        ),
                    ]
                )
            ],
            egress=[
                # intra-namespace app/db traffic
                client.V1NetworkPolicyEgressRule(
                    to=[client.V1NetworkPolicyPeer(pod_selector=client.V1LabelSelector(match_labels={}))],
                ),
                # dns
                client.V1NetworkPolicyEgressRule(
                    to=[
                        client.V1NetworkPolicyPeer(
                            namespace_selector=client.V1LabelSelector(
                                match_labels={"kubernetes.io/metadata.name": "kube-system"}
                            )
                        )
                    ],
                    ports=[
                        client.V1NetworkPolicyPort(protocol="UDP", port=53),
                        client.V1NetworkPolicyPort(protocol="TCP", port=53),
                    ],
                ),
                # external https/http for package/plugin installs and upstream calls
                client.V1NetworkPolicyEgressRule(
                    to=[
                        client.V1NetworkPolicyPeer(
                            ip_block=client.V1IPBlock(cidr="0.0.0.0/0")
                        )
                    ],
                    ports=[
                        client.V1NetworkPolicyPort(protocol="TCP", port=443),
                        client.V1NetworkPolicyPort(protocol="TCP", port=80),
                    ],
                ),
            ],
        ),
    )
    try:
        net.create_namespaced_network_policy(ns, policy)
    except ApiException as e:
        if e.status != 409:
            raise
        net.patch_namespaced_network_policy(
            name="allow-required",
            namespace=ns,
            body=policy,
        )


def ensure_namespace_resources(ns: str) -> None:
    apply_resourcequota(ns)
    apply_limitrange(ns)
    # If CNI does not support NetworkPolicy, reconcile should continue.
    try:
        apply_networkpolicy_default_deny(ns)
        apply_networkpolicy_allow_required(ns)
    except Exception:
        pass


def ensure_admin_secret(ns: str, store_id: str) -> Tuple[str, str]:
    try:
        sec = core.read_namespaced_secret(ADMIN_SECRET_NAME, ns)
        data = sec.data or {}
        if "username" in data and "password" in data:
            username = base64.b64decode(data["username"]).decode("utf-8")
            password = base64.b64decode(data["password"]).decode("utf-8")
            if username and password:
                return username, password
    except ApiException as e:
        if e.status != 404:
            raise

    username = "admin"
    password = rand_password(20)
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name=ADMIN_SECRET_NAME, namespace=ns),
        type="Opaque",
        string_data={
            "username": username,
            "password": password,
            "storeId": store_id,
        },
    )
    try:
        core.create_namespaced_secret(ns, secret)
    except ApiException as e:
        if e.status != 409:
            raise
        core.patch_namespaced_secret(
            name=ADMIN_SECRET_NAME,
            namespace=ns,
            body={"stringData": secret.string_data, "type": "Opaque"},
        )
    return username, password


def _namespace_is_owned(ns: str, store_id: str) -> bool:
    if not ns.startswith(STORE_NS_PREFIX):
        return False
    try:
        namespace_obj = core.read_namespace(ns)
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    labels = namespace_obj.metadata.labels or {}
    return labels.get(STORE_MANAGED_LABEL) == "true" and labels.get(STORE_ID_LABEL) == store_id


def reconcile_store(
    *,
    name: str,
    namespace: str,
    spec: Dict,
    meta: Dict,
    logger,
) -> Dict:
    if namespace != PLATFORM_NAMESPACE:
        logger.info("Ignoring Store in non-platform namespace")
        return {}

    engine = spec.get("engine", "woocommerce")
    handler = ENGINE_HANDLERS.get(engine)
    if not handler:
        patch_store_status(
            name,
            phase="Failed",
            event_type="Failed",
            event_message=f"Unsupported engine '{engine}'",
            set_fields={"lastError": f"Unsupported engine '{engine}'"},
        )
        raise kopf.PermanentError(f"Unsupported engine '{engine}'")

    store_id = spec.get("storeId", name)
    store_ns = store_namespace(store_id)
    host = store_host(store_id)
    release = handler.build_release_name(store_id)
    generation = meta.get("generation", 0)

    add_finalizer(name)
    patch_store_status(
        name,
        phase="Provisioning",
        event_type="ProvisioningStarted",
        event_message=f"Starting reconcile for {engine}",
        set_fields={
            "url": store_url(store_id),
            "namespace": store_ns,
            "releaseName": release,
            "observedGeneration": generation,
            "lastError": None,
        },
        drop_fields=["adminPassword", "adminUser"],
    )

    acquired = _provision_semaphore.acquire(timeout=MAX_PROVISION_SECONDS)
    if not acquired:
        patch_store_status(
            name,
            phase="Failed",
            event_type="Failed",
            event_message="Provisioning lock timeout",
            set_fields={"lastError": "Provisioning lock timeout"},
        )
        raise kopf.TemporaryError("Provisioning lock timeout", delay=15)

    try:
        patch_store_status(
            name,
            event_type="NamespaceReady",
            event_message=f"Ensuring namespace {store_ns}",
            set_fields={"namespace": store_ns},
        )
        ensure_namespace(store_ns, store_id)
        ensure_namespace_resources(store_ns)

        admin_user, admin_password = ensure_admin_secret(store_ns, store_id)
        patch_store_status(
            name,
            event_type="HelmInstallStarted",
            event_message=f"Installing/upgrading release {release}",
        )

        helm_args = handler.build_helm_args(
            store_id=store_id,
            namespace=store_ns,
            host=host,
            admin_user=admin_user,
            admin_password=admin_password,
        )
        run_helm(helm_args, timeout=MAX_PROVISION_SECONDS + 60)
        handler.post_ready_checks(store_id=store_id, namespace=store_ns)

        patch_store_status(
            name,
            phase="Ready",
            event_type="Ready",
            event_message=f"Store ready at {store_url(store_id)}",
            set_fields={
                "url": store_url(store_id),
                "readyAt": now_iso(),
                "releaseName": release,
                "namespace": store_ns,
                "observedGeneration": generation,
                "lastError": None,
            },
            drop_fields=["adminPassword", "adminUser"],
        )
        return {"namespace": store_ns, "host": host, "releaseName": release}
    except Exception as e:
        patch_store_status(
            name,
            phase="Failed",
            event_type="Failed",
            event_message=str(e),
            set_fields={
                "lastError": str(e),
                "releaseName": release,
                "namespace": store_ns,
                "observedGeneration": generation,
            },
            drop_fields=["adminPassword", "adminUser"],
        )
        raise
    finally:
        _provision_semaphore.release()


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_) -> None:
    settings.posting.enabled = True
    settings.execution.max_workers = OPERATOR_WORKERS


@kopf.on.create(CRD_GROUP, CRD_VERSION, CRD_PLURAL)
def on_create(spec, name, namespace, meta, logger, **_):
    return reconcile_store(name=name, namespace=namespace, spec=spec, meta=meta, logger=logger)


@kopf.on.resume(CRD_GROUP, CRD_VERSION, CRD_PLURAL)
def on_resume(spec, status, name, namespace, meta, logger, **_):
    if namespace != PLATFORM_NAMESPACE:
        return
    if meta.get("deletionTimestamp"):
        return
    current_status = status or {}
    if current_status.get("phase") == "Ready" and current_status.get("observedGeneration") == meta.get(
        "generation", 0
    ):
        return
    return reconcile_store(name=name, namespace=namespace, spec=spec, meta=meta, logger=logger)


@kopf.on.delete(CRD_GROUP, CRD_VERSION, CRD_PLURAL)
def on_delete(spec, name, namespace, logger, **_):
    if namespace != PLATFORM_NAMESPACE:
        return

    engine = spec.get("engine", "woocommerce")
    store_id = spec.get("storeId", name)
    handler = ENGINE_HANDLERS.get(engine, ENGINE_HANDLERS["woocommerce"])
    store_ns = store_namespace(store_id)
    release = handler.build_release_name(store_id)

    try:
        patch_store_status(
            name,
            phase="Deleting",
            event_type="Deleting",
            event_message=f"Deleting {release}",
            set_fields={"namespace": store_ns, "releaseName": release},
        )
        try:
            run_helm(["uninstall", release, "-n", store_ns], timeout=300)
        except Exception:
            pass

        if _namespace_is_owned(store_ns, store_id):
            try:
                core.delete_namespace(store_ns)
            except ApiException as e:
                if e.status != 404:
                    raise

        patch_store_status(
            name,
            phase="Deleted",
            event_type="Deleted",
            event_message=f"Deleted resources for {store_id}",
        )
    finally:
        try:
            remove_finalizer(name)
        except Exception as e:
            logger.warning(f"Finalizer removal warning: {e}")
