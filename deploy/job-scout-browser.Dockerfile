FROM mcr.microsoft.com/playwright:v1.61.0-noble

WORKDIR /app
COPY workspace/job-scout/package.json workspace/job-scout/package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts \
  && npm cache clean --force
COPY workspace/job-scout/web_apply.mjs workspace/job-scout/web_apply_server.mjs ./

ENV NODE_ENV=production \
    JOB_SCOUT_BROWSER_PORT=8787
USER pwuser
EXPOSE 8787
CMD ["node", "web_apply_server.mjs"]
