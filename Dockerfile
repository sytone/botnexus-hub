FROM python:3.12-slim-bookworm
ARG HUB_COMMIT=unknown
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HUB_DB_PATH=/data/hub.sqlite HUB_COMMIT=${HUB_COMMIT}
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home hub && mkdir /data && chown hub:hub /data
COPY VERSION ./
COPY hub/ ./hub/
USER hub
HEALTHCHECK --interval=15s --timeout=4s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8785/health',timeout=3)" || exit 1
CMD ["python", "-m", "uvicorn", "hub.app:app", "--host", "127.0.0.1", "--port", "8785", "--no-proxy-headers", "--ws-max-size", "1048576", "--no-access-log"]
