# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

## Commands

No Python linter or formatter is configured, and pytest runs on defaults (no `pytest.ini`/`pyproject.toml`).

```bash
# Single Python test
.venv/bin/python -m pytest tests/test_order_workflow.py -q
.venv/bin/python -m pytest tests/test_order_workflow.py::test_name -q

# Single frontend test (run from frontend/)
npx vitest run src/path/to/file.test.js

# Local server: http://127.0.0.1:8006/login (needs a hash from `manage.py set-password`)
IMAGE_MAGIC_PASSWORD_HASH='...' .venv/bin/python control_panel.py

# Frontend dev server (Vite, proxies the API; run from frontend/)
npm run dev
```

FastAPI serves the built UI from `frontend/dist`, so run `npm run build` to see frontend changes through the backend.

## Architecture

Image Magic is a prepress checker: it validates raster artwork for print orders, groups `face`/`back` files by order, safely corrects small size deviations, builds PDFs and previews, then hands orders off to print or rework.

**Request flow.** `control_panel.py` holds `create_app()`, which wires the routes and all dependencies. `app = create_app()` runs at module level and, unless `initialize_schema=True`, applies Alembic migrations. Importing `control_panel` therefore touches the configured database; tests should call `create_app` with their own settings, repository, and `adapter_factory`.

**Check runs.** `RunCoordinator` (`services/coordinator.py`) runs one check at a time on a worker thread owned by the app lifespan, not by an HTTP request; later runs queue. Orders within a run are processed in parallel by a thread pool. A correction needing confirmation pauses only that order, and the run enters `waiting_confirmation` after the others finish. The UI follows progress over SSE (`GET /api/checks/{id}/events`). Run and order states are defined in `services/domain.py`.

**Layering.** The coordinator depends on the `ProcessorAdapter` protocol. `BatchProcessorAdapter` (`services/batch_adapter.py`) implements it over `processing/batch_processor.py`, which calls the `core/` wrappers. When PitStop is enabled, `_pitstop_adapter_factory` in `control_panel.py` supplies the adapter. Tests can inject fake adapters through this seam.

**Persistence.** `RunRepository` has in-memory (`services/repository.py`) and SQL (`services/sql_repository.py`) implementations. Production data is in SQLite (`image_magic.db`). The CLI records runs through `services/use_cases.py`, using the same run aggregate as the web app. After a restart, unfinished runs are marked interrupted and finished history is kept.

**Post-check actions.** `OrderWorkflowService` (`services/order_workflow.py`) handles accepting orders for print and returning them for rework, using `services/file_lifecycle.py` for file moves and `services/sborka_integration.py` for Sborka. Layered TIFF print assembly is in `services/layered_tiff_print.py`.

**PitStop.** `services/pitstop/` runs report-only preflight over SSH and an SMB share, and is off by default. The order for each built PDF is PitStop, save reports, compute final status, then generate the production preview of that exact PDF revision. An order is not ready for print until PitStop finishes. Diagnose problem PDFs with `scripts/diagnose_pitstop.py`.

**Rules.** Per-direction rules (`digital`, `offset`) are in `config/profiles.py`; add a direction as a new `PROFILES` entry rather than duplicating the engine. Accept/correct/confirm/reject decisions are made in `processing/resample_policy.py`.

## Processing invariants

- Never stretch width and height independently, and never use ImageMagick's forced `!` resize. Corrections scale proportionally to cover the target, then center-crop.
- Never convert color spaces automatically. RGB/sRGB produces a warning, not a failure.
- `50×90` and `90×50` are equally valid: name order does not define the top of the artwork, and one-sided orders are not auto-rotated.
- PDFs are merged with PyMuPDF `insert_pdf()` (direct page copy, no rasterization). A final PDF is published only after a Ghostscript control render.
- Reviewed layered TIFF exports do not replace the production PDF. A print assembly built from them gets its own PitStop result, shown separately from the original PDF's report.

## Configuration

Settings come from `IMAGE_MAGIC_*` environment variables, read by `Settings.from_env()` in `server/settings.py`. User paths are confined to `IMAGE_MAGIC_ALLOWED_ROOTS`. Logs are written to `logs/image-magic.log`.
