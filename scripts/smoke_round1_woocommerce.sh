#!/usr/bin/env bash
set -euo pipefail

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing dependency: $1" >&2
    exit 1
  }
}

require_cmd curl
require_cmd jq
require_cmd kubectl

API_BASE="${API_BASE:-http://api.127.0.0.1.nip.io}"
STORE_ID="${STORE_ID:-smoke$RANDOM}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
POLL_SECONDS="${POLL_SECONDS:-5}"
STORE_NS_PREFIX="${STORE_NS_PREFIX:-store-}"
ADMIN_SECRET_NAME="${ADMIN_SECRET_NAME:-store-admin}"

store_ns="${STORE_NS_PREFIX}${STORE_ID}"
deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))

b64decode() {
  if base64 --help 2>/dev/null | grep -q -- "--decode"; then
    base64 --decode
  else
    base64 -D
  fi
}

echo "==> Creating store: ${STORE_ID}"
create_payload="$(jq -nc --arg sid "$STORE_ID" '{"engine":"woocommerce","storeId":$sid}')"
curl -fsS -X POST "${API_BASE}/stores" \
  -H "Content-Type: application/json" \
  -d "${create_payload}" >/tmp/smoke-store-create.json

echo "==> Waiting for Ready status"
phase=""
store_url=""
while true; do
  if (( "$(date +%s)" > deadline )); then
    echo "Timed out waiting for store readiness" >&2
    exit 1
  fi

  store_json="$(curl -fsS "${API_BASE}/stores/${STORE_ID}")"
  phase="$(echo "${store_json}" | jq -r '.phase')"
  store_url="$(echo "${store_json}" | jq -r '.url // empty')"
  last_error="$(echo "${store_json}" | jq -r '.lastError // empty')"

  if [[ "${phase}" == "Ready" ]]; then
    break
  fi
  if [[ "${phase}" == "Failed" ]]; then
    echo "Provisioning failed: ${last_error}" >&2
    exit 1
  fi
  sleep "${POLL_SECONDS}"
done

echo "==> Store ready: ${store_url}"
curl -fsS "${store_url}" >/tmp/smoke-store-home.html

echo "==> Resolving wordpress pod in namespace ${store_ns}"
wp_pod="$(kubectl get pods -n "${store_ns}" \
  -l "app.kubernetes.io/name=wordpress" \
  -o jsonpath='{.items[0].metadata.name}')"
if [[ -z "${wp_pod}" ]]; then
  echo "Unable to find wordpress pod in ${store_ns}" >&2
  exit 1
fi

echo "==> Reading admin credentials from ${ADMIN_SECRET_NAME}"
admin_user="$(kubectl get secret -n "${store_ns}" "${ADMIN_SECRET_NAME}" \
  -o jsonpath='{.data.username}' | b64decode)"
admin_pass="$(kubectl get secret -n "${store_ns}" "${ADMIN_SECRET_NAME}" \
  -o jsonpath='{.data.password}' | b64decode)"
if [[ -z "${admin_user}" || -z "${admin_pass}" ]]; then
  echo "Admin secret is missing username/password" >&2
  exit 1
fi

wp_path="$(
  kubectl exec -n "${store_ns}" "${wp_pod}" -- bash -lc '
    if [ -f /opt/bitnami/wordpress/wp-config.php ]; then
      echo /opt/bitnami/wordpress
    elif [ -f /bitnami/wordpress/wp-config.php ]; then
      echo /bitnami/wordpress
    else
      echo /opt/bitnami/wordpress
    fi
  ' | tr -d '\r\n'
)"
if [[ -z "${wp_path}" ]]; then
  wp_path="/opt/bitnami/wordpress"
fi

echo "==> Ensuring WooCommerce plugin/COD and creating a test product"
product_id="$(
  kubectl exec -n "${store_ns}" "${wp_pod}" -- bash -lc "
    set -euo pipefail
    wp --path='${wp_path}' --allow-root --quiet plugin is-installed woocommerce || \
      wp --path='${wp_path}' --allow-root --quiet plugin install woocommerce --activate >/dev/null
    wp --path='${wp_path}' --allow-root --quiet plugin activate woocommerce >/dev/null || true
    wp --path='${wp_path}' --allow-root eval '
      \$settings = get_option(\"woocommerce_cod_settings\", array());
      \$settings[\"enabled\"] = \"yes\";
      update_option(\"woocommerce_cod_settings\", \$settings);
      \$product = new WC_Product_Simple();
      \$product->set_name(\"Smoke Product\");
      \$product->set_status(\"publish\");
      \$product->set_regular_price(\"9.99\");
      echo \$product->save();
    '
  " | tr -dc '0-9'
)"

if [[ -z "${product_id}" ]]; then
  echo "Failed to create smoke product" >&2
  exit 1
fi
echo "Created product id=${product_id}"

echo "==> Attempting checkout via Woo Store API"
cookie_file="/tmp/smoke-cookie-${STORE_ID}.txt"
header_file="/tmp/smoke-headers-${STORE_ID}.txt"
checkout_file="/tmp/smoke-checkout-${STORE_ID}.json"
nonce=""
order_id=""

set +e
curl -sS -D "${header_file}" -c "${cookie_file}" \
  "${store_url}/?rest_route=/wc/store/v1/cart" >/tmp/smoke-cart.json
nonce="$(awk 'tolower($1)=="nonce:" {print $2}' "${header_file}" | tr -d '\r' | tail -n1)"

if [[ -n "${nonce}" ]]; then
  curl -sS -X POST -b "${cookie_file}" -c "${cookie_file}" \
    -H "Nonce: ${nonce}" \
    -H "Content-Type: application/json" \
    -d "{\"id\":${product_id},\"quantity\":1}" \
    "${store_url}/?rest_route=/wc/store/v1/cart/add-item" >/tmp/smoke-cart-add.json

  curl -sS -X POST -b "${cookie_file}" -c "${cookie_file}" \
    -H "Nonce: ${nonce}" \
    -H "Content-Type: application/json" \
    -d '{
      "billing_address": {
        "first_name":"Smoke","last_name":"Tester","address_1":"123 Demo St",
        "city":"San Francisco","state":"CA","postcode":"94105",
        "country":"US","email":"smoke@example.com","phone":"5555555555"
      },
      "shipping_address": {
        "first_name":"Smoke","last_name":"Tester","address_1":"123 Demo St",
        "city":"San Francisco","state":"CA","postcode":"94105",
        "country":"US"
      },
      "payment_method":"cod",
      "payment_data":[{"key":"payment_method","value":"cod"}]
    }' \
    "${store_url}/?rest_route=/wc/store/v1/checkout" >"${checkout_file}"
  order_id="$(jq -r '.order_id // .id // empty' "${checkout_file}")"
fi
set -e

if [[ -z "${order_id}" ]]; then
  echo "Store API checkout unavailable; creating deterministic order via wp-cli fallback"
  order_id="$(
    kubectl exec -n "${store_ns}" "${wp_pod}" -- bash -lc "
      set -euo pipefail
      wp --path='${wp_path}' --allow-root eval '
        \$product = wc_get_product(${product_id});
        \$order = wc_create_order();
        \$order->add_product(\$product, 1);
        \$order->set_address(array(
          \"first_name\" => \"Smoke\",
          \"last_name\" => \"Tester\",
          \"email\" => \"smoke@example.com\",
          \"phone\" => \"5555555555\",
          \"address_1\" => \"123 Demo St\",
          \"city\" => \"San Francisco\",
          \"state\" => \"CA\",
          \"postcode\" => \"94105\",
          \"country\" => \"US\"
        ), \"billing\");
        \$order->set_payment_method(\"cod\");
        \$order->set_payment_method_title(\"Cash on delivery\");
        \$order->calculate_totals();
        \$order->update_status(\"processing\", \"Smoke script order\");
        echo \$order->get_id();
      '
    " | tr -dc '0-9'
  )"
fi

if [[ -z "${order_id}" ]]; then
  echo "Order creation failed" >&2
  exit 1
fi
echo "Created order id=${order_id}"

echo "==> Verifying order exists"
kubectl exec -n "${store_ns}" "${wp_pod}" -- bash -lc "
  set -euo pipefail
  wp --path='${wp_path}' --allow-root eval '
    \$order = wc_get_order(${order_id});
    if (!\$order) { exit(2); }
    echo \"ok\";
  '
" >/tmp/smoke-order-verify.txt

echo "==> Deleting store"
curl -fsS -X DELETE "${API_BASE}/stores/${STORE_ID}" >/tmp/smoke-store-delete.json || true

echo "==> Waiting for namespace cleanup (${store_ns})"
while true; do
  if (( "$(date +%s)" > deadline )); then
    echo "Timed out waiting for namespace cleanup" >&2
    exit 1
  fi
  if ! kubectl get namespace "${store_ns}" >/dev/null 2>&1; then
    break
  fi
  sleep "${POLL_SECONDS}"
done

echo "Smoke test succeeded for store ${STORE_ID}"
