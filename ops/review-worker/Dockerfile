# Stage B1 containment probe only. No ERP files or authentication are copied.
# Pin the base digest observed in the first CI build. Live review needs additional provenance verification.
FROM node:22-bookworm-slim@sha256:be979e5523c76fa4d5ca4ebd3099078f220aa47ec39c48ad5b5df9314debd6eb
RUN npm install --global --no-audit --no-fund @openai/codex@0.172.0 \
    && codex --version \
    && npm cache clean --force
WORKDIR /opt/review-worker
COPY --chmod=0444 probe.js /opt/review-worker/probe.js
ENV HOME=/tmp/reviewer CODEX_HOME=/tmp/reviewer/.codex
USER 10001:10001
ENTRYPOINT ["node", "/opt/review-worker/probe.js"]
