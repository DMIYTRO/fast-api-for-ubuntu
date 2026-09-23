# Processing Instructions

`processing/` owns deterministic file discovery, filename parsing, grouping, prepress policy and order-level PDF production. Keep these rules at this layer rather than in FastAPI routes or Vue code.

- `filename_parser.py` parses customer/order/side/dimensions/direction from filenames; keep parsing errors clear and test supported naming variations.
- `profile_rules.py`, `resample_policy.py` and `config/profiles.py` are policy sources. Use the selected `PrePressProfile` rather than duplicating thresholds. Current minimum effective DPI is 270.
- `batch_processor.py` coordinates inspection, order validation, corrections and PDF assembly. Preserve deterministic sorting, incremental order iteration, operator confirmation semantics and original inputs.
- `models.py` represents inspection outcomes. When adding fields, follow the path through `services/dto.py` and persistence/API serialization as needed.
- A passing RGB/sRGB file is warning-level and must keep its original colors; do not introduce implicit CMYK conversion. Keep ICC presence/profile metadata intact in results.
- Corrections are proportional (`cover` + centered crop where policy allows); never distort dimensions. PDFs must be validated for page count, order and physical size before being accepted.
- TIFF structure checks (single page, flattened, no alpha) are intentional. Keep clear operator-facing Russian errors.
- Put focused regression tests in `tests/test_<feature>.py`; use temporary directories and fake external commands. Image-tool tests require ImageMagick and Ghostscript.
