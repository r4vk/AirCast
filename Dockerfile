# syntax=docker/dockerfile:1

# ---- build: compile wheels (some deps have no prebuilt wheels on armv7) ----
FROM python:3.12-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libffi-dev \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY aircast ./aircast
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

# ---- runtime ----
FROM python:3.12-slim
ARG VERSION=dev
ARG BUILD_SHA=unknown
LABEL org.opencontainers.image.title="AirCast" \
      org.opencontainers.image.description="Expose AirPlay speakers as Chromecast and DLNA/UPnP renderers" \
      org.opencontainers.image.source="https://github.com/r4vk/AirCast" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${BUILD_SHA}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

RUN useradd --system --uid 1000 --home-dir /data aircast \
 && mkdir -p /data && chown aircast /data
USER aircast
VOLUME /data

ENV AIRCAST_STATE_DIR=/data \
    PYTHONUNBUFFERED=1

# Needs host networking (mDNS, SSDP multicast, AirPlay UDP back-channels).
# TCP 49152 status page + DLNA, TCP 8010+ Cast (one port per speaker), UDP 1900, UDP 5353.
EXPOSE 49152/tcp 8010/tcp 1900/udp 5353/udp

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('AIRCAST_HTTP_PORT','49152'), timeout=4)" || exit 1

ENTRYPOINT ["aircast"]
