FROM python:3.12-slim

# APP_ENV=production: the strict checks apply (no AUTH_DISABLED, strong SECRET_KEY, secure cookies).
# For a local demo start the container with -e APP_ENV=dev.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore \
    PORT=8000 DATA_DIR=/data APP_ENV=production

WORKDIR /app
# jbig2dec: pypdf needs it to decode JBIG2 scans ("kompaktes PDF" of office copiers, optimised scans);
# without it such pages stay unread and are retried endlessly
RUN apt-get update \
 && apt-get install -y --no-install-recommends jbig2dec \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

COPY VERSION ./
COPY projektakte ./projektakte

# fixed, unprivileged user 10001: a bind mount for /data must belong to it
# (sudo chown -R 10001:10001 ./data); a named volume takes over the ownership of /data by itself
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 10001:10001 /data && chmod 0750 /data
USER 10001:10001

EXPOSE 8000
# /health/ready: database usable and indexer thread alive (python instead of curl, not in the image)
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-m", "projektakte.healthcheck"]
CMD ["python", "-m", "projektakte"]
