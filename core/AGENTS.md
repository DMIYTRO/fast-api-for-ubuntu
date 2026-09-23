# Core Tooling Instructions

`core/` wraps external tools and document primitives. Keep subprocess details and file-format mechanics here; inject or mock wrappers from higher layers.

- `tool_runner.py` is the shared subprocess boundary. Do not bypass it for production shell calls; avoid logging secrets or full sensitive tool output.
- `inspector.py` reads image metadata and TIFF structure. Preserve dimensions, per-axis DPI, colorspace and ICC profile metadata in returned models.
- `pdf_exporter.py` converts images and merges pages. TIFF-to-PDF export removes an embedded ICC profile without assigning a replacement; PDF page merge should avoid rasterization, rescaling and color conversion.
- `pdf_inspector.py` and `pdf_builder.py` inspect/validate PDF content and geometry. Maintain page count, page order and physical-size validation.
- `resampler.py` performs proportional resizing/cropping only; current threshold is 270 DPI. Do not independently stretch axes.
- `callas_toolbox.py` and `preview_generator.py` provide optional Callas support. Keep Ghostscript fallback behavior when Callas is disabled or fails; do not make the optional tool a hard dependency.
- `report_builder.py` and `return_reasons.py` produce operator-facing content. Keep messages actionable and in Russian where shown to operators.
- Tests should mock `run_command` or the tool wrapper. Never invoke licensed/production services during ordinary tests. Generated artifacts belong in temporary or designated output folders.
