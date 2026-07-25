FROM node:22.19.0-bookworm-slim AS node-base

FROM python:3.12-bookworm AS python-base

FROM node-base AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build \
    && npm prune --omit=dev \
    && npm cache clean --force

FROM nginx:stable-bookworm AS runtime

COPY --from=python-base /usr/local /usr/local
COPY --from=node-base /usr/local/bin/node /usr/local/bin/node
COPY --from=node-base /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /app
ENV NODE_ENV=production \
    PORT=3002 \
    MONITOR_HOST=127.0.0.1 \
    MONITOR_PORT=3001 \
    PYTHONUNBUFFERED=1

COPY --from=build /app/package.json /app/package-lock.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/dist ./dist
COPY --from=build /app/app ./app
COPY --from=build /app/build ./build
COPY --from=build /app/worker ./worker
COPY --from=build /app/public ./public
COPY --from=build /app/public-video-crawler ./public-video-crawler
COPY --from=build /app/scripts ./scripts
COPY --from=build /app/config ./config
COPY --from=build /app/.openai ./.openai
COPY --from=build /app/vite.config.ts /app/next.config.ts /app/tsconfig.json /app/postcss.config.mjs ./
COPY docker/nginx.conf /etc/nginx/nginx.conf
RUN chmod +x /app/scripts/docker-entrypoint.sh \
    && mkdir -p /app/data/logs /app/data/partials /app/中转站 /var/cache/nginx /var/run

EXPOSE 3000

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
