# OpenClaw gateway - sandboxed container image.
#
# Contains only the immutable runtime: OS packages, Node, the global OpenClaw
# CLI, and Python. Durable OpenClaw state is mounted at /home/joseph/.openclaw.

FROM node:24-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    HOME=/home/joseph \
    OPENCLAW_HOME=/home/joseph/.openclaw \
    OPENCLAW_NO_AUTO_UPDATE=1 \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/home/joseph/.npm-global/bin

# Poppler renders CV pages and Tesseract recovers headings that embedded PDF
# fonts do not reliably expose to plain text extraction.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv python3-pip \
      poppler-utils tesseract-ocr \
      passwd sudo ca-certificates tini util-linux \
 && rm -rf /var/lib/apt/lists/*

# The node base image ships uid 1000 as `node`; use that uid for the gateway
# account so host-owned bind mounts retain the expected ownership.
RUN userdel -r node 2>/dev/null || true \
 && groupadd -g 1000 joseph \
 && useradd -m -u 1000 -g 1000 -s /bin/bash joseph

RUN npm install -g openclaw@2026.9.9 \
 && npm cache clean --force \
 && rm -rf /home/joseph/.npm

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

EXPOSE 18789

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
