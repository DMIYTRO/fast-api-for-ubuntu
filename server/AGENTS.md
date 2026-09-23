# Server Instructions

`server/` contains FastAPI configuration support: settings, auth/session handling, SQLAlchemy models/database setup, request schemas, errors and logging. `control_panel.py` composes these pieces and declares routes.

- Require an authenticated session for API routes that expose source artwork, PDFs, previews, reports or order actions. Keep allowed-root path containment checks intact.
- Keep credentials outside source control and environment/config output. Never log passwords, tokens or full sensitive external responses.
- Keep API validation and error payloads stable and actionable; operator-facing messages should be Russian.
- Model changes that alter persisted schema require a new Alembic revision under `alembic/versions/`; do not rewrite migrations already applied in production.
- Use the app lifespan for worker/database setup and shutdown. Do not create a second production worker/process through route-level startup.
- Test routes with isolated temporary databases, fake transports and auth fixtures. Never hit production services from tests.
