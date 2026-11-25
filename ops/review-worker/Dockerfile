# Stage B1 containment probe only. No ERP files or authentication are copied.
# The mutable base tag is acceptable for a disposable probe, NOT live review.
FROM node:22-bookworm-slim
RUN npm install --global --no-audit --no-fund @openai/codex@0.172.0 \
    && codex --version \
    && npm cache clean --force
WORKDIR /opt/review-worker
COPY probe.js /opt/review-worker/probe.js
ENV HOME=/tmp/reviewer CODEX_HOME=/tmp/reviewer/.codex
USER 10001:10001
ENTRYPOINT ["node", "/opt/review-worker/probe.js"]
