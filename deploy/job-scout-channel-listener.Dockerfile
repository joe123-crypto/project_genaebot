FROM node:24-bookworm-slim

WORKDIR /app
COPY workspace/job-scout/channel-listener/package.json workspace/job-scout/channel-listener/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
  && npm cache clean --force
COPY workspace/job-scout/channel-listener/*.mjs ./

ENV NODE_ENV=production \
    JOB_SCOUT_CHANNEL_DATA_DIR=/data
USER node
CMD ["node", "listener.mjs"]
