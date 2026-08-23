import vinext from "vinext";
import { defineConfig, type ViteDevServer } from "vite";
import fs from "fs";
import path from "path";
import type { IncomingMessage, ServerResponse } from "node:http";
import hostingConfig from "./.openai/hosting.json";
import { sites } from "./build/sites-vite-plugin";

const SITE_CREATOR_PLACEHOLDER_DATABASE_ID =
  "00000000-0000-4000-8000-000000000000";

const { d1, r2 } = hostingConfig;

// macOS Seatbelt blocks FSEvents, so Codex previews need polling for HMR.
const isCodexSeatbeltSandbox = process.env.CODEX_SANDBOX === "seatbelt";

const localBindingConfig = {
  main: "./worker/index.ts",
  compatibility_flags: ["nodejs_compat"],
  d1_databases: d1
    ? [
        {
          binding: d1,
          database_name: "site-creator-d1",
          database_id: SITE_CREATOR_PLACEHOLDER_DATABASE_ID,
        },
      ]
    : [],
  r2_buckets: r2
    ? [
        {
          binding: r2,
          bucket_name: "site-creator-r2",
        },
      ]
    : [],
};

export default defineConfig(async () => {
  // Keep Wrangler and Miniflare state project-local. These are non-secret tool
  // settings; application environment belongs in ignored `.env*` files.
  process.env.WRANGLER_WRITE_LOGS ??= "false";
  process.env.WRANGLER_LOG_PATH ??= ".wrangler/logs";
  process.env.MINIFLARE_REGISTRY_PATH ??= ".wrangler/registry";

  // Wrangler snapshots its log path while the Cloudflare plugin is imported.
  const { cloudflare } = await import("@cloudflare/vite-plugin");

  return {
    server: isCodexSeatbeltSandbox
      ? { watch: { useFsEvents: false, usePolling: true } }
      : undefined,
    plugins: [
      vinext(),
      {
        name: 'bypass-status-cache',
        configureServer(server: ViteDevServer) {
          // Proxy local task API requests to the Python service.
          server.middlewares.use(async (req: IncomingMessage, res: ServerResponse, next: () => void) => {
            if (!req.url?.startsWith('/api/')) {
              next();
              return;
            }
            try {
              const controller = new AbortController();
              res.on('close', () => controller.abort());
              const method = req.method || 'GET';
              let requestBody: string | undefined;
              if (!['GET', 'HEAD'].includes(method)) {
                const chunks: Uint8Array[] = [];
                let total = 0;
                for await (const chunk of req) {
                  const bytes = typeof chunk === 'string' ? Buffer.from(chunk) : chunk;
                  total += bytes.length;
                  if (total > 64 * 1024) throw new Error('API request body too large');
                  chunks.push(bytes);
                }
                requestBody = Buffer.concat(chunks).toString('utf8');
              }
              const proxyHeaders = new Headers({ 'content-type': req.headers['content-type'] || 'application/json' });
              if (req.headers.origin) proxyHeaders.set('origin', req.headers.origin);
              // Preserve the browser-facing origin so the local API can enforce
              // an exact same-origin boundary in development as it does in nginx.
              if (req.headers.host) proxyHeaders.set('x-forwarded-host', req.headers.host);
              proxyHeaders.set('x-forwarded-proto', 'http');
              const proxy = await fetch(`http://127.0.0.1:3001${req.url}`, {
                method,
                headers: proxyHeaders,
                body: requestBody,
                signal: controller.signal,
              });
              const responseHeaders: Record<string, string> = {
                'Content-Type': proxy.headers.get('content-type') || 'application/json',
                'Cache-Control': proxy.headers.get('cache-control') || 'no-cache',
                'X-Accel-Buffering': proxy.headers.get('x-accel-buffering') || 'no',
              };
              const allowedOrigin = proxy.headers.get('access-control-allow-origin');
              if (allowedOrigin) responseHeaders['Access-Control-Allow-Origin'] = allowedOrigin;
              res.writeHead(proxy.status, responseHeaders);
              if (!proxy.body) {
                res.end();
                return;
              }
              const reader = proxy.body.getReader();
              while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                res.write(value);
              }
              res.end();
            } catch {
              if (res.headersSent) {
                res.end();
                return;
              }
              res.writeHead(503, { 'Content-Type': 'application/json' });
              res.end(JSON.stringify({ error: 'Task service unavailable' }));
            }
          });
          server.middlewares.use('/status.json', (_req: IncomingMessage, res: ServerResponse, next: () => void) => {
            try {
              const content = fs.readFileSync(path.resolve('public/status.json'), 'utf-8');
              res.setHeader('Content-Type', 'application/json');
              res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate');
              res.end(content);
            } catch {
              next();
            }
          });
        }
      },
      sites(),
      cloudflare({
        viteEnvironment: { name: "rsc", childEnvironments: ["ssr"] },
        config: localBindingConfig,
      }),
    ],
  };
});
