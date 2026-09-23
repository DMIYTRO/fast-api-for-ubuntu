# Repository Guide

This repository is Image Magic, a prepress order checking and preparation system. Read the nearest scoped `AGENTS.md` before changing code:

- `processing/AGENTS.md` and `core/AGENTS.md`: artwork inspection, policy, image/PDF tools, previews and reports.
- `services/AGENTS.md` and `server/AGENTS.md`: run orchestration, order lifecycle, API, persistence and integrations.
- `frontend/AGENTS.md`: Vue/Pinia dashboard.
- `tests/AGENTS.md`: regression test conventions.

## Main areas

- `processing/`: filename parsing, file/order models, profile rules, DPI/size decisions, batch inspection and PDF creation.
- `core/`: ImageMagick/Ghostscript/Callas/PyMuPDF wrappers, PDF inspection/export, previews and HTML reports.
- `services/`: background runs, domain transitions, file lifecycle, SQL repositories, PitStop, Sborka and FTP workflows.
- `server/`: FastAPI settings, authentication, database models/schemas, error handling and logging.
- `control_panel.py`: FastAPI composition/root wiring and HTTP routes. Prefer implementing behavior in services over adding processing/filesystem logic here.
- `frontend/src/`: Vue 3, Pinia, router and API client. `frontend/dist/` is generated output.
- `alembic/versions/`: ordered database migrations. Do not edit an applied migration; add a new migration.
- `tests/`: Python regression coverage; frontend state/view tests live beside frontend code.

## Important invariants

- Preserve original artwork. Put generated files only in designated `PDF/`, `Previews/`, `Troubles/`, `Processed/`, `output_report/` or configured output directories.
- Respect the configured allowed input roots and path containment checks. Never expose source files, PDFs, previews or reports through unauthenticated routes.
- Current prepress baseline: minimum effective resolution is 270 DPI; RGB/sRGB may pass with a warning and must not be silently converted to CMYK. Preserve PDF page geometry, color information and ICC metadata where applicable.
- Corrections must remain proportional; never stretch width and height independently. Orders requiring operator confirmation must remain pending until the explicit decision is recorded.
- Use injected/fake external transports and shell tools in tests. Never contact production Sborka, FTP or PitStop endpoints from tests.
- Do not commit credentials, databases, generated PDFs/previews, logs, local tool caches or temporary test artifacts. Check `git status --short` before and after work; the working tree may contain user data.

## Development and verification

- Python environment: `.venv/bin/python`; install from `requirements.txt` when needed.
- Run locally with `.venv/bin/python control_panel.py` (default local port 8006), or use the batch CLI `python3 process_orders.py --input "/path/to/artwork"`.
- Migrations: `.venv/bin/alembic upgrade head`.
- Python tests: `.venv/bin/python -m pytest -q`; image-tool tests need `magick` and `gs` on `PATH`.
- Frontend: `cd frontend && npm ci && npm test -- --run && npm run build`.
- Run focused checks first. Do not run tests/build unless requested or needed to substantiate a code change.

## Production constraint

Production is VM `10.20.2.104`, systemd unit `fastapi-app`. If a production restart is explicitly requested, restart that unit and verify its new `MainPID`, `ExecMainStartTimestamp`, and `active` state. Never start another Uvicorn/control-panel process as a substitute. Validate behavior with a newly created check run; old report history persists across restarts.

## Style and operator-facing behavior

Use Python type hints at service boundaries, 4-space indentation, `snake_case` and `PascalCase` classes. Keep Vue component filenames in `PascalCase.vue`; use lower camel-case store/util APIs. Error messages shown to operators should be actionable and in Russian. Keep commits scoped with concise imperative subjects.

## Recent design context

Latest commit `5398b9d` (`Lower minimum DPI to 270 and preserve PDF color metadata`) set the profile and resampling threshold to 270 DPI, added ICC-profile fields to file results/DTOs, and added TIFF PDF export that removes the embedded ICC profile without assigning a replacement. It also uses Callas for PDF preview rendering when enabled and falls back to Ghostscript. Preserve these behaviors when editing adjacent paths; update regression coverage when changing them.
