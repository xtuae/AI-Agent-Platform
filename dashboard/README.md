# Dashboard

React 18 + Vite + TypeScript + Tailwind + shadcn/ui-style components (Radix) + TanStack Query.
`npm run build` writes `dist/`, which Caddy serves as static files (zero runtime CPU).

```bash
npm ci
npm run dev        # http://localhost:5173, proxies /api to API_ORIGIN (default http://127.0.0.1:8000)
npm run typecheck
npm test           # vitest: API client refresh logic, SSE parser
npm run build
```

- Auth: the access token lives in memory only; a reload restores the session from the HttpOnly
  refresh cookie (`/api/v1/auth/refresh`). The tenant comes from the token, never the URL.
- Live updates: `GET /api/v1/stream` (SSE) read with `fetch()` so the token stays in a header;
  queries fall back to 15 s polling while it is down.
- Only Today and Login are in the main bundle; other screens are lazy-loaded.
- Colours follow the dataviz reference palette (`src/index.css`); status colours are reserved for
  health and always come with a label.
