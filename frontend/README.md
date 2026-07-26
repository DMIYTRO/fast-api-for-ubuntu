# Image Magic frontend

Vue 3 / Vite interface for the local Image Magic service.

## Development

```bash
npm install
npm run dev
```

Vite proxies `/api` and `/runs` to `http://127.0.0.1:8006`.

## Verification and production build

```bash
npm test
npm run build
```

The production files are written to `frontend/dist`. FastAPI should serve that
directory and return `dist/index.html` for the `/` and `/login` client routes.

## API contract

The interface uses the routes documented in `IMPLEMENTATION_PLAN.md`. Collection
responses may be either arrays or objects containing `items`/`orders`. Preview,
source and PDF URLs may be supplied by the API; otherwise the documented
`/api/files/{id}/preview`, `/api/files/{id}/source`, and
`/api/orders/{order_id}/pdf` routes are used.
