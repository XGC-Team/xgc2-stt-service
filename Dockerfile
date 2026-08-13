ARG VLLM_IMAGE=vllm/vllm-openai:v0.27.1-cu129@sha256:07913e94a58a4e61322c88f8d4647411b4dd394838b53abacaa32448d9d8de0a
ARG QWEN_VLLM_IMAGE=vllm/vllm-openai:v0.14.0@sha256:1d6866b87630d94f5e0cdae55ab5abb4ce0b03fcb84d9d10612f9d518d19d4fd

FROM node:22-bookworm-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436 AS web-builder

WORKDIR /build/web
COPY web/package.json web/package-lock.json web/.npmrc ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ${VLLM_IMAGE} AS base

ARG DEBIAN_FRONTEND=noninteractive
ARG APP_VERSION=0.1.0

LABEL org.opencontainers.image.title="XGC2 STT Base" \
      org.opencontainers.image.description="Native realtime GPU speech-to-text runtime for XGC2" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.licenses="MIT AND Apache-2.0" \
      org.opencontainers.image.source="https://github.com/lxk36/xgc2-stt-service" \
      io.xgc2.stt.variant="base" \
      io.xgc2.stt.weights="external"

USER root
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl tini; \
    rm -rf /var/lib/apt/lists/*
RUN getent passwd 2000 >/dev/null || \
    useradd --uid 2000 --gid 0 --no-create-home --home-dir /var/lib/xgc2-stt --shell /usr/sbin/nologin xgc2-stt

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/xgc2-stt \
    HF_HOME=/var/lib/xgc2-stt/huggingface \
    XDG_CACHE_HOME=/var/lib/xgc2-stt/cache \
    VLLM_CACHE_ROOT=/var/lib/xgc2-stt/vllm \
    TRITON_CACHE_DIR=/var/lib/xgc2-stt/triton \
    STT_WEB_DIST=/opt/xgc2-stt/web \
    STT_HOST=0.0.0.0 \
    STT_PORT=8000 \
    STT_INTERNAL_HOST=127.0.0.1 \
    STT_INTERNAL_PORT=8001

WORKDIR /opt/xgc2-stt/app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --system --no-cache-dir .

RUN install -d -o 2000 -g 0 -m 0770 \
      /var/lib/xgc2-stt \
      /opt/xgc2-stt/models \
      /opt/xgc2-stt/web; \
    install -d /opt/xgc2-stt/licenses; \
    cp /usr/share/common-licenses/Apache-2.0 /opt/xgc2-stt/licenses/APACHE-2.0.txt

COPY --from=web-builder --chown=2000:0 /build/web/dist/ /opt/xgc2-stt/web/

USER 2000:0
VOLUME ["/var/lib/xgc2-stt"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "xgc2_stt.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM ${QWEN_VLLM_IMAGE} AS qwen-base

ARG DEBIAN_FRONTEND=noninteractive
ARG APP_VERSION=0.1.0

LABEL org.opencontainers.image.title="XGC2 STT Qwen Base" \
      org.opencontainers.image.description="Official revision-capable Qwen streaming runtime for XGC2" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.licenses="MIT AND Apache-2.0" \
      org.opencontainers.image.source="https://github.com/lxk36/xgc2-stt-service" \
      io.xgc2.stt.variant="qwen-base" \
      io.xgc2.stt.weights="external"

USER root
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl tini sox libsox-fmt-all; \
    rm -rf /var/lib/apt/lists/*
RUN getent passwd 2000 >/dev/null || \
    useradd --uid 2000 --gid 0 --no-create-home --home-dir /var/lib/xgc2-stt --shell /usr/sbin/nologin xgc2-stt

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/var/lib/xgc2-stt \
    HF_HOME=/var/lib/xgc2-stt/huggingface \
    XDG_CACHE_HOME=/var/lib/xgc2-stt/cache \
    VLLM_CACHE_ROOT=/var/lib/xgc2-stt/vllm \
    TRITON_CACHE_DIR=/var/lib/xgc2-stt/triton \
    STT_WEB_DIST=/opt/xgc2-stt/web \
    STT_HOST=0.0.0.0 \
    STT_PORT=8000 \
    STT_INTERNAL_HOST=127.0.0.1 \
    STT_INTERNAL_PORT=8001

WORKDIR /opt/xgc2-stt/app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN uv pip install --system --no-cache-dir . "qwen-asr[vllm]==0.0.6"

RUN install -d -o 2000 -g 0 -m 0770 \
      /var/lib/xgc2-stt \
      /opt/xgc2-stt/models \
      /opt/xgc2-stt/web; \
    install -d /opt/xgc2-stt/licenses; \
    cp /usr/share/common-licenses/Apache-2.0 /opt/xgc2-stt/licenses/APACHE-2.0.txt

COPY --from=web-builder --chown=2000:0 /build/web/dist/ /opt/xgc2-stt/web/

USER 2000:0
VOLUME ["/var/lib/xgc2-stt"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz >/dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "xgc2_stt.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM ${QWEN_VLLM_IMAGE} AS qwen-weights
ARG QWEN_MODEL=Qwen/Qwen3-ASR-1.7B
ARG QWEN_REVISION=7278e1e70fe206f11671096ffdd38061171dd6e5

USER root
RUN --mount=type=cache,target=/root/.cache/huggingface \
    HF_HOME=/root/.cache/huggingface python3 -c \
      "from huggingface_hub import snapshot_download; snapshot_download('${QWEN_MODEL}', revision='${QWEN_REVISION}', local_dir='/opt/xgc2-stt/models/qwen')"

FROM qwen-base AS qwen

ARG QWEN_MODEL=Qwen/Qwen3-ASR-1.7B
ARG QWEN_REVISION=7278e1e70fe206f11671096ffdd38061171dd6e5

USER root
COPY --from=qwen-weights --chown=2000:0 /opt/xgc2-stt/models/qwen/ /opt/xgc2-stt/models/qwen/

ENV STT_ENGINE_VARIANT=qwen \
    STT_MODEL_ID=/opt/xgc2-stt/models/qwen \
    STT_MODEL_NAME=Qwen/Qwen3-ASR-1.7B \
    STT_TRANSCRIPTION_DELAY_MS=1000

LABEL org.opencontainers.image.title="XGC2 STT Qwen" \
      org.opencontainers.image.description="One-step Qwen3-ASR-1.7B revision-capable streaming STT image" \
      io.xgc2.stt.variant="qwen" \
      io.xgc2.stt.model="Qwen/Qwen3-ASR-1.7B" \
      io.xgc2.stt.model.revision="${QWEN_REVISION}" \
      io.xgc2.stt.weights="embedded"

USER 2000:0

FROM ${VLLM_IMAGE} AS voxtral-weights
ARG VOXTRAL_MODEL=mistralai/Voxtral-Mini-4B-Realtime-2602
ARG VOXTRAL_REVISION=2769294da9567371363522aac9bbcfdd19447add

USER root
RUN --mount=type=cache,target=/root/.cache/huggingface \
    HF_HOME=/root/.cache/huggingface python3 -c \
      "from huggingface_hub import snapshot_download; snapshot_download('${VOXTRAL_MODEL}', revision='${VOXTRAL_REVISION}', local_dir='/opt/xgc2-stt/models/voxtral', ignore_patterns=['model.safetensors', '.gitattributes'])"

FROM base AS voxtral

ARG VOXTRAL_MODEL=mistralai/Voxtral-Mini-4B-Realtime-2602
ARG VOXTRAL_REVISION=2769294da9567371363522aac9bbcfdd19447add

USER root
COPY --from=voxtral-weights --chown=2000:0 /opt/xgc2-stt/models/voxtral/ /opt/xgc2-stt/models/voxtral/

ENV STT_ENGINE_VARIANT=voxtral \
    STT_MODEL_ID=/opt/xgc2-stt/models/voxtral \
    STT_MODEL_NAME=mistralai/Voxtral-Mini-4B-Realtime-2602 \
    STT_TRANSCRIPTION_DELAY_MS=480

LABEL org.opencontainers.image.title="XGC2 STT Voxtral" \
      org.opencontainers.image.description="One-step Voxtral Mini 4B Realtime STT image" \
      io.xgc2.stt.variant="voxtral" \
      io.xgc2.stt.model="mistralai/Voxtral-Mini-4B-Realtime-2602" \
      io.xgc2.stt.model.revision="${VOXTRAL_REVISION}" \
      io.xgc2.stt.weights="embedded"

USER 2000:0
