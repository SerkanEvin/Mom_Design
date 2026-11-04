# Mom_Design studio: FastAPI app + job workers + headless Blender (CPU).
FROM python:3.12-slim

# Blender runtime libraries (headless, no GPU) + tools to fetch it
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates xz-utils libgl1 libegl1 libglib2.0-0 libgomp1 libx11-6 libxext6 libxi6 \
      libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 libsm6 libice6 \
    && rm -rf /var/lib/apt/lists/*

# Blender LTS tarball; first URL that exists wins (exact patch versions change over time).
ARG BLENDER_CANDIDATES="5.2/blender-5.2.2 5.2/blender-5.2.1 5.2/blender-5.2.0 4.5/blender-4.5.4 4.5/blender-4.5.3 4.5/blender-4.5.0"
RUN set -e; for c in $BLENDER_CANDIDATES; do \
      series=${c%%/*}; name=${c#*/}; url="https://download.blender.org/release/Blender${series}/${name}-linux-x64.tar.xz"; \
      if curl -fsSLI "$url" >/dev/null 2>&1; then echo "Blender: $url"; \
        curl -fsSL "$url" | tar -xJ -C /opt && ln -s "/opt/${name}-linux-x64" /opt/blender && break; fi; \
    done; test -x /opt/blender/blender

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY studio ./studio

ENV STUDIO_DATA=/data BLENDER_BIN=/opt/blender/blender PYTHONUNBUFFERED=1 OMP_NUM_THREADS=4
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
CMD ["uvicorn", "studio.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
