import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from pydantic import BaseModel, Field

APP_NAMESPACE = os.getenv("PLATFORM_NAMESPACE", "store-platform")
CRD_GROUP = os.getenv("STORE_CRD_GROUP", "stores.urumi.ai")
CRD_VERSION = os.getenv("STORE_CRD_VERSION", "v1alpha1")
CRD_PLURAL = os.getenv("STORE_CRD_PLURAL", "stores")

MAX_ACTIVE_STORES = int(os.getenv("MAX_ACTIVE_STORES", "5"))
MAX_STORES_PER_IP = int(os.getenv("MAX_STORES_PER_IP", "3"))
CREATE_RATE_LIMIT = int(os.getenv("CREATE_RATE_LIMIT", "5"))
RATE_WINDOW_SECONDS = int(os.getenv("RATE_WINDOW_SECONDS", "60"))
EVENTS_LIMIT = int(os.getenv("EVENTS_LIMIT", "20"))
CORS_ALLOW_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",")
    if origin.strip()
]

_rate_limit_lock = threading.Lock()
_ip_create_requests: Dict[str, Deque[float]] = {}


def _load_kube() -> None:
    # In-cluster first, fallback to local kubeconfig
    try:
        config.load_incluster_config()
    except Exception:
        config.load_kube_config()


_load_kube()
co_api = client.CustomObjectsApi()


class StoreCreateReq(BaseModel):
    engine: Literal["woocommerce", "medusa"] = "woocommerce"
    storeId: str = Field(..., min_length=3, max_length=32, pattern=r"^[a-z0-9-]+$")


class StoreEvent(BaseModel):
    type: str
    message: str
    timestamp: str


class StoreResp(BaseModel):
    storeId: str
    engine: str
    phase: str
    url: Optional[str] = None
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None
    lastError: Optional[str] = None
    namespace: Optional[str] = None
    releaseName: Optional[str] = None
    events: List[StoreEvent] = Field(default_factory=list)


class StoreEventsResp(BaseModel):
    storeId: str
    events: List[StoreEvent]


app = FastAPI(title="Store Platform API", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _check_create_rate_limit(ip: str) -> None:
    now = time.time()
    with _rate_limit_lock:
        bucket = _ip_create_requests.setdefault(ip, deque())
        while bucket and (now - bucket[0]) > RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= CREATE_RATE_LIMIT:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded for {ip}: {CREATE_RATE_LIMIT} creates/{RATE_WINDOW_SECONDS}s",
            )
        bucket.append(now)


def _list_store_objects() -> List[Dict[str, Any]]:
    try:
        res = co_api.list_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=APP_NAMESPACE,
            plural=CRD_PLURAL,
        )
        return res.get("items", [])
    except ApiException as e:
        raise HTTPException(status_code=500, detail=f"K8s error: {e.reason}") from e


def _to_store_resp(item: Dict[str, Any]) -> StoreResp:
    spec = item.get("spec", {})
    status = item.get("status", {}) or {}
    events = status.get("events", [])[:EVENTS_LIMIT]
    return StoreResp(
        storeId=spec.get("storeId") or item.get("metadata", {}).get("name", ""),
        engine=spec.get("engine", ""),
        phase=status.get("phase", "Provisioning"),
        url=status.get("url"),
        createdAt=status.get("createdAt") or item.get("metadata", {}).get("creationTimestamp"),
        updatedAt=status.get("updatedAt"),
        lastError=status.get("lastError"),
        namespace=status.get("namespace"),
        releaseName=status.get("releaseName"),
        events=events,
    )


def _get_store_or_none(store_id: str) -> Optional[Dict[str, Any]]:
    try:
        return co_api.get_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=APP_NAMESPACE,
            plural=CRD_PLURAL,
            name=store_id,
        )
    except ApiException as e:
        if e.status == 404:
            return None
        raise HTTPException(status_code=500, detail=f"K8s error: {e.reason}") from e


def _enforce_store_quotas(items: List[Dict[str, Any]], caller_ip: str) -> None:
    active = [it for it in items if not it.get("metadata", {}).get("deletionTimestamp")]
    if len(active) >= MAX_ACTIVE_STORES:
        raise HTTPException(
            status_code=409,
            detail=f"Global store quota reached ({MAX_ACTIVE_STORES})",
        )

    same_ip = 0
    for it in active:
        req_meta = (it.get("spec", {}) or {}).get("requestedBy", {}) or {}
        if req_meta.get("ip") == caller_ip:
            same_ip += 1
    if same_ip >= MAX_STORES_PER_IP:
        raise HTTPException(
            status_code=409,
            detail=f"Per-IP store quota reached ({MAX_STORES_PER_IP}) for {caller_ip}",
        )


@app.get("/healthz")
def healthz() -> Dict[str, bool]:
    return {"ok": True}


@app.post("/stores", response_model=StoreResp)
def create_store(req: StoreCreateReq, request: Request) -> StoreResp:
    caller_ip = _client_ip(request)
    _check_create_rate_limit(caller_ip)

    existing = _get_store_or_none(req.storeId)
    if existing:
        existing_engine = (existing.get("spec", {}) or {}).get("engine")
        if existing_engine != req.engine:
            raise HTTPException(
                status_code=409,
                detail=f"StoreId '{req.storeId}' already exists with engine '{existing_engine}'",
            )
        return _to_store_resp(existing)

    items = _list_store_objects()
    _enforce_store_quotas(items, caller_ip)

    body: Dict[str, Any] = {
        "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
        "kind": "Store",
        "metadata": {
            "name": req.storeId,
            "namespace": APP_NAMESPACE,
        },
        "spec": {
            "engine": req.engine,
            "storeId": req.storeId,
            "requestedBy": {
                "ip": caller_ip,
                "userAgent": request.headers.get("user-agent", "unknown"),
            },
        },
    }

    try:
        obj = co_api.create_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=APP_NAMESPACE,
            plural=CRD_PLURAL,
            body=body,
        )
        return _to_store_resp(obj)
    except ApiException as e:
        if e.status == 409:
            existing_after_race = _get_store_or_none(req.storeId)
            if existing_after_race:
                existing_engine = (existing_after_race.get("spec", {}) or {}).get("engine")
                if existing_engine != req.engine:
                    raise HTTPException(
                        status_code=409,
                        detail=f"StoreId '{req.storeId}' already exists with engine '{existing_engine}'",
                    ) from e
                return _to_store_resp(existing_after_race)
            raise HTTPException(status_code=409, detail="StoreId already exists") from e
        raise HTTPException(status_code=500, detail=f"K8s error: {e.reason}") from e


@app.get("/stores", response_model=List[StoreResp])
def list_stores() -> List[StoreResp]:
    items = _list_store_objects()
    stores = [_to_store_resp(it) for it in items]
    stores.sort(key=lambda s: s.createdAt or "", reverse=True)
    return stores


@app.get("/stores/{store_id}", response_model=StoreResp)
def get_store(store_id: str) -> StoreResp:
    obj = _get_store_or_none(store_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Not found")
    return _to_store_resp(obj)


@app.get("/stores/{store_id}/events", response_model=StoreEventsResp)
def get_store_events(store_id: str) -> StoreEventsResp:
    obj = _get_store_or_none(store_id)
    if not obj:
        raise HTTPException(status_code=404, detail="Not found")
    resp = _to_store_resp(obj)
    return StoreEventsResp(storeId=store_id, events=resp.events)


@app.delete("/stores/{store_id}")
def delete_store(store_id: str) -> Dict[str, Any]:
    try:
        co_api.delete_namespaced_custom_object(
            group=CRD_GROUP,
            version=CRD_VERSION,
            namespace=APP_NAMESPACE,
            plural=CRD_PLURAL,
            name=store_id,
            body=client.V1DeleteOptions(),
        )
        return {"deleted": True, "storeId": store_id}
    except ApiException as e:
        if e.status == 404:
            return {"deleted": False, "storeId": store_id, "reason": "NotFound"}
        raise HTTPException(status_code=500, detail=f"K8s error: {e.reason}") from e
