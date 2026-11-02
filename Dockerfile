# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
#
# One image, two roles (compose picks the command): `collect` and `web`.
# Base image pinned by digest, Python dependencies pinned by hash (requirements.lock).

ARG PYTHON_IMAGE=python:3.15-slim-trixie@sha256:8c13a3f37a6a5b71498228e902c4b9f51c1f7900d49fabf71f9a650c6120e0d0

FROM ${PYTHON_IMAGE} AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN python -m venv /venv
COPY requirements.lock /tmp/requirements.lock
RUN /venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.lock \
    && /venv/bin/pip uninstall -y pip

FROM ${PYTHON_IMAGE} AS runtime
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.source="https://github.com/Pascal-Organisation/solar" \
      org.opencontainers.image.description="Local-first GoodWe inverter dashboard" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.revision="${GIT_SHA}"
# No pip or build tooling at runtime: only the prebuilt venv and the source.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin solar \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip* /usr/local/bin/pip* \
    && mkdir /data && chown 10001:10001 /data
COPY --from=build /venv /venv
COPY src /app/src
COPY web /app/web
# The deployer extracts this from the verified image, so compose changes ship with the code.
COPY deploy/docker-compose.yml /app/deploy/docker-compose.yml
ENV PATH=/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOLAR_WEB_DIR=/app/web \
    SOLAR_DB=/data/solar.db \
    TZ=Europe/Berlin
USER 10001:10001
WORKDIR /app
ENTRYPOINT ["python", "-m", "solar"]
CMD ["web"]
