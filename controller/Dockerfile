FROM python:3.11-slim

WORKDIR /app

# Helm is used to install per-store charts
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
  && rm -rf /var/lib/apt/lists/* \
  && curl -fsSL https://get.helm.sh/helm-v3.15.3-linux-amd64.tar.gz | tar -xz \
  && mv linux-amd64/helm /usr/local/bin/helm \
  && rm -rf linux-amd64

# Ensure UID 10001 exists for non-root runtime
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

COPY controller/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY controller/operator.py /app/operator.py
COPY charts /charts

ENV PLATFORM_NAMESPACE=store-platform
ENV BASE_DOMAIN=127.0.0.1.nip.io
ENV INGRESS_CLASS=nginx

USER 10001

CMD ["kopf", "run", "--standalone", "/app/operator.py"]
