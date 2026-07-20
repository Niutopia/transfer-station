FROM python:3.12-bookworm AS node-base

ARG NODE_VERSION=22.19.0
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && case "$TARGETARCH" in \
         amd64) node_arch="x64" ;; \
         arm64) node_arch="arm64" ;; \
         *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.xz" \
       | tar -xJ -C /usr/local --strip-components=1 \
    && node --version \
    && npm --version \
    && rm -rf /var/lib/apt/lists/*

FROM node-base AS build

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node-base AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV NODE_ENV=production \
    PORT=3002 \
    MONITOR_HOST=127.0.0.1 \
    MONITOR_PORT=3001 \
    PYTHONUNBUFFERED=1

COPY --from=build /app /app
COPY docker/nginx.conf /etc/nginx/nginx.conf
RUN chmod +x /app/scripts/docker-entrypoint.sh \
    && mkdir -p /app/data/logs /app/data/partials /app/中转站 /var/cache/nginx /var/run

EXPOSE 3000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/scripts/docker-entrypoint.sh"]
