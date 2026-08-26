# Repository Guidelines

## Project Structure & Module Organization

`processing/` owns file discovery, validation, grouping, and PDF creation. `core/` wraps ImageMagick, Ghostscript, PDF, preview, and reporting operations. `services/` coordinates runs, order transitions, FTP/Sborka integration, and persistence; `server/` contains FastAPI settings, models, auth, and database setup. The Vue/Pinia dashboard is in `frontend/src/`. Python tests live in `tests/`; Alembic migrations are in `alembic/versions/`.

Keep source images untouched. Generated artifacts belong under an input folder's `PDF/`, `Previews/`, `Troubles/`, `Processed/`, or `output_report/` directories.

## Build, Test, and Development Commands

Create the Python environment and install dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Run the web app with `.venv/bin/python control_panel.py`. Apply migrations with `.venv/bin/alembic upgrade head`. Run the batch CLI with `python3 process_orders.py --input "/path/to/artwork"`.

### Production FastAPI Service

The primary production service runs on VM `10.20.2.104` as the systemd unit `fastapi-app`. When a production restart is requested, restart that unit on the VM and verify its new `MainPID`, `ExecMainStartTimestamp`, and `active` status. Never start `.venv/bin/python control_panel.py` or another Uvicorn process in parallel as a substitute for restarting production; this creates a second instance while the UI continues to use the original service. Historical check results remain stored after a restart, so validate changes with a newly created check run rather than an old report.

For the frontend, run `cd frontend && npm install`, then `npm test -- --run` and `npm run build`. Image processing tests require `magick` and `gs` on `PATH`.

## Coding Style & Naming Conventions

Use Python 4-space indentation, type hints for service boundaries, `snake_case` functions/modules, and `PascalCase` classes. Keep error messages actionable and in Russian where they are exposed to operators. Vue components use `PascalCase.vue`; Pinia stores and utility modules use lower camel-case APIs. Prefer small services over adding transport or filesystem logic to `control_panel.py`.

## Testing Guidelines

Add focused regression tests beside the relevant behavior: `tests/test_<feature>.py` for Python and `frontend/src/**/*.test.js` for UI state. Use fakes/mocks for FTP, Sborka, and shell tools; do not contact production services during tests. Run targeted tests first, then the full Python suite with `.venv/bin/python -m pytest -q` and frontend tests/build before review.

## Commit & Pull Request Guidelines

Use concise imperative commit subjects, e.g. `Support rework senders` or `Remove obsolete frontend build asset`. Keep commits scoped. PRs should explain operator-visible behavior, list test commands, link relevant issues, and include screenshots for UI changes.

## Security & Configuration

Never commit API keys, passwords, databases, previews, or generated PDFs. Local FTP credentials are stored in ignored `sborka_ftp_credentials.json`; configure production secrets outside tracked files. Avoid logging credentials or full sensitive HTTP responses.
