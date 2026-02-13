FROM python:3.11-slim

WORKDIR /app

COPY apps/api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/api/main.py /app/main.py

# Ensure UID 10001 exists for non-root runtime
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser

ENV PLATFORM_NAMESPACE=store-platform
EXPOSE 8000

USER 10001

CMD ["uvicorn", "main:app", "--host=0.0.0.0", "--port=8000"]
