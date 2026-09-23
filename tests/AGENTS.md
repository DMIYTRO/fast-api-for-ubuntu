# Test Instructions

Python regression tests live in this directory; frontend tests live beside frontend source. Prefer targeted tests for the behavior changed.

- Use temporary directories and isolated SQLite databases; do not read/write tracked sample outputs or a live application database.
- Mock ImageMagick, Ghostscript, Callas, FTP, Sborka and PitStop at their wrapper/transport boundaries. Tests must not contact production endpoints or require credentials/licenses.
- Cover failure paths and operator-visible status/message changes as well as success. For workflows, assert transitions, idempotency, and original-file preservation.
- Preserve focused naming: `test_<feature>.py` for Python; `*.test.js` for frontend.
- Useful commands: `.venv/bin/python -m pytest tests/test_<feature>.py -q`; frontend `cd frontend && npm test -- --run`.
- Tests requiring `magick`/`gs` are environment-dependent; report missing binaries distinctly instead of interpreting them as product failures.
