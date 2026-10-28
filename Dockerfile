FROM python:3.13-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --system --uid 10001 --no-create-home rembo
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts
USER rembo
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"
# One worker on purpose: the background sweeper (hold expiry, payment reconciliation, daily summary) lives in the app process.
# The database locks make several workers safe; scale out later by adding --workers or more containers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--proxy-headers", "--forwarded-allow-ips", "*"]
