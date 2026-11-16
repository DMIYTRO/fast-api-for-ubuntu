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

**Check runs.** `RunCoordinator` (`services/coordinator.py`) runs one check at a time on a worker thread owned by the app lifespan, not by an HTTP request; later runs queue. Orders within a run are processed in parallel by a thread pool. The per-order timeout counts from the moment a worker starts the order, not from queueing, and a timed-out order does not cancel queued ones. A correction needing confirmation pauses only that order, and the run enters `waiting_confirmation` after the others finish. The UI follows progress over SSE (`GET /api/checks/{id}/events`). Run and order states are defined in `services/domain.py`.

**Layering.** The coordinator depends on the `ProcessorAdapter` protocol. `BatchProcessorAdapter` (`services/batch_adapter.py`) implements it over `processing/batch_processor.py`, which calls the `core/` wrappers. When PitStop is enabled, `_pitstop_adapter_factory` in `control_panel.py` supplies the adapter. Tests can inject fake adapters through this seam.

**Persistence.** `RunRepository` has in-memory (`services/repository.py`) and SQL (`services/sql_repository.py`) implementations. Production data is in SQLite (`image_magic.db`). The CLI records runs through `services/use_cases.py`, using the same run aggregate as the web app. While a run is active the coordinator keeps the aggregate in memory and persists one order per event (`save_order_with_event`); the whole-run `options_json` envelope is rewritten only at stage boundaries and at most every `IMAGE_MAGIC_ENVELOPE_CHECKPOINT_SECONDS` (default 5), so per-event cost must not grow with run size (`tests/test_run_persistence_scaling.py`). After a restart, unfinished runs are marked interrupted and finished history is kept.

**Post-check actions.** `OrderWorkflowService` (`services/order_workflow.py`) handles accepting orders for print and returning them for rework, using `services/file_lifecycle.py` for file moves and `services/sborka_integration.py` for Sborka. Layered TIFF print assembly is in `services/layered_tiff_print.py`.

**PitStop.** `services/pitstop/` runs report-only preflight over SSH and an SMB share, and is off by default. The order for each built PDF is PitStop, save reports, compute final status, then generate the production preview of that exact PDF revision. An order is not ready for print until PitStop finishes. Up to `IMAGE_MAGIC_PITSTOP_MAX_CONCURRENT` (default 4, `1` = strictly serial) checks run at once, measured on the live server on 2026-11-15; a transient failure is retried once with no other PitStop check in flight, and repeated transient failures switch the process to serial mode for a cool-down (`services/pitstop/concurrency.py`). Diagnose problem PDFs with `scripts/diagnose_pitstop.py`.

**Rules.** Per-direction rules (`digital`, `offset`) are in `config/profiles.py`; add a direction as a new `PROFILES` entry rather than duplicating the engine. Accept/correct/confirm/reject decisions are made in `processing/resample_policy.py`.

## Processing invariants

- Never stretch width and height independently (sole exception: the bounded PDF `stretch` method below), and never use ImageMagick's forced `!` resize. Corrections scale proportionally to cover the target, then center-crop.
- Never convert color spaces automatically, with one exception: when `IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_ENABLED` is on, a final PDF with RGB content is converted to CMYK by a callas pdfToolbox fixup (`IMAGE_MAGIC_CALLAS_RGB_TO_CMYK_PROFILE`, default `Image Magic/NEW_Convert color to 4c for digital printing`, which replaced the ISO Coated v2 profile; RGB presence is decided by `find_rgb_pages` before/after, and ICC/Lab color found beforehand is reported as `icc_removed` / the "ICC off" badge), and the result replaces the PDF only if no RGB remains and the page count is unchanged. On any failure the RGB PDF is kept with a warning. Source images are never recolored; other color models (Gray, Lab, spot) are left alone.
- PDF page sizes are corrected only when `IMAGE_MAGIC_CALLAS_PDF_RESIZE_ENABLED` is on (`processing/pdf_resize_policy.py`). Per page the least destructive method wins: `reveal` (page smaller than the format and a background object already extends past the edge, per `PdfPageInfo.hidden_margins_mm` → `enlarge_pages`, no scaling), `fill` (same `analyze_resample` rules as rasters → `scale_pages fit_from_outside_cut_page`), or `stretch` (`stretch_to_fill`). Stretch is the only allowed exception to "never stretch independently": PDF only, only when fill is not automatic, and only within the profile's `pdf_stretch_auto_pct` / `pdf_stretch_confirm_pct` (default 1% / 3%), so on small formats it almost never applies. Reveal growth and fill crop share the `auto_crop_mm` / `confirm_crop_mm` limits. Afterwards CropBox = BleedBox = MediaBox and TrimBox is inset by the bleed. The JSON goes through the wrapper profile `Image Magic/Apply QuickFix config` (built by `scripts/build_quickfix_profile.py`) in the `quickfix_config` variable (max 1024 chars; a two-page worst case is ~920). A bad QuickFix silently changes nothing, so `services/callas/page_resize.py` accepts the result only after checking page count, MediaBox, TrimBox and BleedBox. Pages with `/Rotate` are not resized.
- `50×90` and `90×50` are equally valid: name order does not define the top of the artwork, and one-sided orders are not auto-rotated.
- All PDF operations (merge, page split, info, render) go through callas pdfToolbox, with no PyMuPDF/Ghostscript fallback; raster TIFF/JPEG/PNG → PDF conversion stays on ImageMagick. A final PDF is published only after a callas pdfToolbox control render; PDF rendering never falls back to Ghostscript or PyMuPDF, and fails with an actionable error when callas is not configured.
- Reviewed layered TIFF exports do not replace the production PDF. A print assembly built from them gets its own PitStop result, shown separately from the original PDF's report.

## Configuration

Settings come from `IMAGE_MAGIC_*` environment variables, read by `Settings.from_env()` in `server/settings.py`. User paths are confined to `IMAGE_MAGIC_ALLOWED_ROOTS`. Logs are written to `logs/image-magic.log`.
