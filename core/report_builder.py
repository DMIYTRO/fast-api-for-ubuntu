"""
Ядро сборки отчётов (HTML & JSON).
"""

import os
import json
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Union, Optional
from core.inspector import ImageMetadata
from validators.rules import ValidationResult

def build_reports(results: List[Tuple[ImageMetadata, ValidationResult, str]], output_dir: str):
    """Служит для сборки HTML и JSON отчётов."""
    os.makedirs(output_dir, exist_ok=True)

    # 1. Генерация JSON
    json_path = os.path.join(output_dir, "report.json")
    json_data = []
    for meta, val, rel_preview in results:
        json_data.append({
            "file_name": meta.file_name,
            "format": meta.format,
            "overall_passed": val.overall_passed,
            "preview_path": rel_preview,
            "dimensions_px": {"width": meta.width_px, "height": meta.height_px},
            "dimensions_mm": {"width": meta.width_mm, "height": meta.height_mm},
            "dpi": meta.dpi,
            "colorspace": meta.colorspace,
            "icc_profile": meta.icc_profile,
            "size_mb": meta.size_mb,
            "validation_items": [
                {
                    "name": item.name,
                    "actual": item.actual_value,
                    "target": item.target_value,
                    "passed": item.passed,
                    "message": item.message
                }
                for item in val.items
            ]
        })
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    # 2. Генерация HTML
    html_path = os.path.join(output_dir, "report.html")
    cards_html = ""

    for idx, (meta, val, rel_preview) in enumerate(results):
        passed = val.overall_passed

        rows_html = ""
        for item in val.items:
            icon_class = "pass" if item.passed else "fail"
            icon_symbol = "✔" if item.passed else "✖"
            status_text = item.actual_value if item.passed else f'<span class="text-danger">{item.actual_value}</span>'
            
            rows_html += f"""
            <tr>
                <td class="param-title"><span class="status-icon {icon_class}">{icon_symbol}</span> {item.name}</td>
                <td class="val-cell">{item.actual_value}</td>
                <td class="target-cell">{item.target_value}</td>
                <td class="status-cell">{status_text}</td>
            </tr>
            """

        status_box_class = "pass-box" if passed else "fail-box"
        status_msg = (
            "<strong>Макет успешно прошёл автоматическую проверку.</strong><br>Файл равен стандарту допечатной подготовки."
            if passed else
            "<strong>Макет не прошёл автоматическую проверку.</strong><br>"
            "Отдел допечатной подготовки проверит макет и вы получите соответствующее уведомление."
        )

        cards_html += f"""
        <div class="report-card">
            <div class="card-header">
                <span class="side-title">Макет #{idx + 1}:</span>
                <div class="upload-bar">
                    <button class="upload-btn">ФАЙЛ ОБРАБОТАН</button>
                    <div class="file-info">
                        <span class="dot">•</span>
                        <span class="file-name">{meta.file_name}</span>
                        <span class="file-size">{meta.size_mb} МБ</span>
                    </div>
                </div>
            </div>

            <div class="preview-container">
                <div class="preview-frame">
                    <img src="{rel_preview}" alt="Превью {meta.file_name}" class="preview-img">
                </div>
                <div class="legend">
                    <span class="legend-item"><span class="color-box red"></span> Красный контур (1 мм) — край реза</span>
                    <span class="legend-item"><span class="color-box green"></span> Зеленый контур (4 мм) — безопасная зона</span>
                </div>
            </div>

            <table class="params-table">
                <thead>
                    <tr>
                        <th class="col-param">ПАРАМЕТР</th>
                        <th class="col-val">ВАШ ФАЙЛ</th>
                        <th class="col-target">ПОТРЕБНО / НОРМА</th>
                        <th class="col-proc">ОБРАБОТАННЫЙ / СТАТУС</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>

            <div class="status-box {status_box_class}">
                <p class="status-message">{status_msg}</p>
                <label class="confirm-checkbox">
                    <input type="checkbox" {'checked' if passed else ''}>
                    <span>Подтверждаю автоматически доработанный макет, претензий по обрезке и цвету иметь не буду.</span>
                </label>
            </div>
        </div>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Отчёт допечатного контроля макетов (Image-Magic Audit)</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #f4f6f8;
            --card-bg: #ffffff;
            --text-primary: #212529;
            --text-secondary: #6c757d;
            --header-red: #c02b2b;
            --pass-color: #28a745;
            --fail-color: #dc3545;
            --border-color: #dee2e6;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-primary);
            padding: 40px 20px;
            line-height: 1.5;
        }}
        .container {{ max-width: 900px; margin: 0 auto; }}
        .main-header {{ margin-bottom: 30px; text-align: center; }}
        .main-header h1 {{ font-size: 26px; font-weight: 700; color: #1a1a1a; }}
        .main-header p {{ color: var(--text-secondary); font-size: 14px; margin-top: 5px; }}

        .report-card {{
            background: var(--card-bg);
            border-radius: 8px;
            border: 1px solid var(--border-color);
            padding: 24px;
            margin-bottom: 35px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
        }}
        .card-header {{ margin-bottom: 20px; }}
        .side-title {{ font-size: 16px; font-weight: 600; color: #333; display: block; margin-bottom: 10px; }}
        .upload-bar {{ display: flex; align-items: center; gap: 15px; flex-wrap: wrap; }}
        .upload-btn {{
            background-color: #555;
            color: white;
            border: none;
            padding: 10px 18px;
            font-size: 13px;
            font-weight: 600;
            letter-spacing: 0.5px;
            border-radius: 4px;
            text-transform: uppercase;
        }}
        .file-info {{ font-size: 14px; display: flex; align-items: center; gap: 8px; }}
        .file-name {{ font-weight: 600; color: #222; }}
        .file-size {{ color: #666; }}

        .preview-container {{
            text-align: center;
            margin: 25px 0;
            background: #fafafa;
            padding: 20px;
            border-radius: 6px;
            border: 1px dashed var(--border-color);
        }}
        .preview-frame {{ display: inline-block; box-shadow: 0 5px 15px rgba(0,0,0,0.1); background: #fff; }}
        .preview-img {{ max-width: 100%; max-height: 420px; display: block; height: auto; }}
        .legend {{ margin-top: 12px; font-size: 13px; display: flex; justify-content: center; gap: 20px; color: #555; }}
        .legend-item {{ display: flex; align-items: center; gap: 6px; }}
        .color-box {{ width: 14px; height: 14px; border-radius: 2px; display: inline-block; }}
        .color-box.red {{ background-color: #dc3545; }}
        .color-box.green {{ background-color: #28a745; }}

        .params-table {{ width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 14px; }}
        .params-table th {{
            background-color: var(--header-red);
            color: #ffffff;
            font-weight: 600;
            text-transform: uppercase;
            font-size: 12px;
            letter-spacing: 0.5px;
            padding: 12px 16px;
            text-align: center;
            border: 1px solid var(--header-red);
        }}
        .params-table th.col-param {{ text-align: left; width: 40%; }}
        .params-table td {{ padding: 12px 16px; border: 1px solid var(--border-color); text-align: center; }}
        .params-table td.param-title {{ text-align: left; font-weight: 500; display: flex; align-items: center; gap: 10px; }}

        .status-icon {{
            width: 22px; height: 22px; border-radius: 50%; display: inline-flex;
            align-items: center; justify-content: center; font-size: 12px; font-weight: bold; color: white;
        }}
        .status-icon.pass {{ background-color: var(--pass-color); }}
        .status-icon.fail {{ background-color: var(--fail-color); }}

        .status-box {{ margin-top: 25px; padding: 18px; border-radius: 6px; font-size: 14px; }}
        .status-box.pass-box {{ background-color: #e8f5e9; border: 1px solid #c8e6c9; color: #1b5e20; }}
        .status-box.fail-box {{ background-color: #fff8f8; border: 1px solid #ffcdd2; color: #842029; }}
        .status-message {{ margin-bottom: 12px; line-height: 1.5; }}
        .confirm-checkbox {{ display: flex; align-items: flex-start; gap: 10px; font-size: 13px; color: #333; cursor: pointer; }}
        .confirm-checkbox input {{ margin-top: 3px; }}
        .text-danger {{ color: var(--fail-color); font-weight: 600; }}
    </style>
</head>
<body>
    <div class="container">
        <header class="main-header">
            <h1>Отчёт допечатного контроля макетов (Pre-Press Audit)</h1>
            <p>Модульная пакетная проверка файлов • Вылеты 1 мм (красный) • Безопасная зона 4 мм (зеленый)</p>
        </header>
        {cards_html}
    </div>
</body>
</html>
"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    # 3. Генерация PDF
    try:
        from core.pdf_builder import build_pdf_report
        pdf_path = build_pdf_report(results, output_dir)
    except Exception as e:
        print(f"⚠️ Ошибка при создании PDF отчёта: {e}")
        pdf_path = ""

    return html_path, json_path, pdf_path


def build_orders_html_report(
    orders: list,
    output_html_path: Union[str, Path],
    preview_dir: Optional[Path] = None,
) -> str:
    """Генерирует современный интерактивный HTML-отчёт для списка заказов."""
    out_path = Path(output_html_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    order_cards_html = ""
    pass_count = 0
    warn_count = 0
    fail_count = 0

    for idx, order in enumerate(orders, start=1):
        has_warnings = bool(order.warnings or any(f.warnings for f in order.files))
        if not order.passed:
            status_code = "fail"
            status_badge = '<span class="status-badge badge-fail">❌ ОШИБКА</span>'
            fail_count += 1
        elif has_warnings:
            status_code = "warning"
            status_badge = '<span class="status-badge badge-warning">⚠️ ПРЕДУПРЕЖДЕНИЕ</span>'
            warn_count += 1
        else:
            status_code = "pass"
            status_badge = '<span class="status-badge badge-pass">✅ ПРОЙДЕН</span>'
            pass_count += 1

        first_parsed = order.files[0].parsed if order.files and order.files[0].parsed else None
        customer_id = order.customer_id
        color_mode = (
            f"{first_parsed.front_colors}-{first_parsed.back_colors}"
            if first_parsed
            else "4-4"
        )
        expected_w = (first_parsed.width_mm + 4.0) if first_parsed else 0.0
        expected_h = (first_parsed.height_mm + 4.0) if first_parsed else 0.0

        face_file = next((f for f in order.files if f.parsed and f.parsed.side == "face"), None)
        back_file = next((f for f in order.files if f.parsed and f.parsed.side == "back"), None)
        if not face_file and order.files:
            face_file = order.files[0]

        # Превью для лица и оборота
        previews_html = ""
        sides_to_check = [("Лицо (Face)", face_file)]
        if back_file:
            sides_to_check.append(("Оборот (Back)", back_file))

        for label, f_item in sides_to_check:
            if not f_item:
                continue
            img_src = ""
            if preview_dir:
                p_candidate = preview_dir / f"{f_item.path.stem}_preview.png"
                if p_candidate.is_file():
                    try:
                        img_src = os.path.relpath(p_candidate, out_path.parent)
                    except ValueError:
                        img_src = str(p_candidate)

            if img_src:
                preview_box = f"""
                <div class="preview-thumb-card" onclick="openModal('{img_src}', 'Заказ #{order.order_id} — {label}')">
                    <div class="side-label">{label}</div>
                    <div class="img-wrapper">
                        <img src="{img_src}" alt="{label}" class="preview-image" loading="lazy">
                        <div class="zoom-overlay">🔍 Нажмите для увеличения</div>
                    </div>
                    <div class="thumb-filename" title="{f_item.path.name}">{f_item.path.name}</div>
                </div>
                """
            else:
                preview_box = f"""
                <div class="preview-thumb-card no-img">
                    <div class="side-label">{label}</div>
                    <div class="img-wrapper" style="height: 200px; display: flex; align-items: center; justify-content: center; background: #f1f5f9; color: #94a3b8; font-size: 13px; font-weight: 600;">🖼 Превью отсутствует</div>
                    <div class="thumb-filename" title="{f_item.path.name}">{f_item.path.name}</div>
                </div>
                """
            previews_html += preview_box

        # Формирование таблицы параметров
        has_back = bool(back_file)
        table_headers = """
            <tr>
                <th style="width: 35%;">Параметр</th>
                <th style="width: 25%;">Лицо (Face)</th>
        """
        if has_back:
            table_headers += '<th style="width: 25%;">Оборот (Back)</th>'
        table_headers += '<th style="width: 15%;">Норма</th></tr>'

        def render_param_row(name, face_val, back_val, target_val, is_ok=True):
            status_icon = "✔" if is_ok else "✖"
            icon_cls = "pass" if is_ok else "fail"
            row = f'<tr><td class="param-title"><span class="status-icon {icon_cls}">{status_icon}</span> {name}</td>'
            row += f'<td>{face_val}</td>'
            if has_back:
                row += f'<td>{back_val if back_val is not None else "—"}</td>'
            row += f'<td>{target_val}</td></tr>'
            return row

        rows_code = ""
        # 1. Размер
        face_sz = f"{face_file.actual_width_mm:.1f} × {face_file.actual_height_mm:.1f} мм" if face_file and face_file.actual_width_mm else "не определён"
        back_sz = f"{back_file.actual_width_mm:.1f} × {back_file.actual_height_mm:.1f} мм" if back_file and back_file.actual_width_mm else "—"
        rows_code += render_param_row("Размер с вылетами", face_sz, back_sz, f"{expected_w:.1f} × {expected_h:.1f} мм", is_ok=order.passed)

        # 2. Разрешение DPI
        face_dpi = f"{face_file.dpi_x:.0f}×{face_file.dpi_y:.0f} DPI" if face_file and face_file.dpi_x else "—"
        back_dpi = f"{back_file.dpi_x:.0f}×{back_file.dpi_y:.0f} DPI" if back_file and back_file.dpi_x else "—"
        rows_code += render_param_row("Разрешение (DPI)", face_dpi, back_dpi, "≥ 300 DPI")

        # 3. Цветность
        face_col = face_file.colorspace if face_file else "—"
        back_col = back_file.colorspace if back_file else "—"
        rows_code += render_param_row("Цветовая модель", face_col, back_col, "CMYK")

        # 4. Формат
        face_fmt = face_file.actual_format if face_file else "—"
        back_fmt = back_file.actual_format if back_file else "—"
        rows_code += render_param_row("Формат файла", face_fmt, back_fmt, "TIFF/JPEG/PDF")

        # Сообщения статуса/ошибок/попереджень
        messages_html = ""
        if order.errors or any(f.errors for f in order.files):
            err_list = order.errors + [err for f in order.files for err in f.errors]
            err_items = "".join(f"<li>{e}</li>" for e in err_list)
            messages_html += f'<div class="msg-box box-fail"><strong>❌ Ошибки проверки:</strong><ul>{err_items}</ul></div>'

        all_warnings = order.warnings + [w for f in order.files for w in f.warnings]
        if all_warnings:
            warn_items = "".join(f"<li>{w}</li>" for w in all_warnings)
            messages_html += f'<div class="msg-box box-warning"><strong>⚠️ Попередження / Авто-ресемплинг:</strong><ul>{warn_items}</ul></div>'

        rotated_files = [
            item for item in order.files if item.rotation_degrees in {90, 180, 270}
        ]
        if rotated_files:
            rotation_items = "".join(
                f"<li>{item.parsed.side if item.parsed else item.path.name}: "
                f"{item.rotation_degrees}°</li>"
                for item in rotated_files
            )
            messages_html += (
                '<div class="msg-box box-warning orientation-review">'
                "<strong>⚠️ Требуется визуальная проверка</strong>"
                "<p>Автоматический поворот:</p>"
                f"<ul>{rotation_items}</ul>"
                '<label><input type="checkbox" class="orientation-confirmation"> '
                "Совмещение проверено пользователем</label></div>"
            )

        if order.passed and not all_warnings:
            messages_html += '<div class="msg-box box-pass"><strong>✅ Заказ полностью соответствует стандартам допечатной подготовки.</strong></div>'

        order_cards_html += f"""
        <article class="order-card" data-status="{status_code}">
            <div class="card-header-bar">
                <div class="order-checkbox-group">
                    <input type="checkbox" class="order-checkbox" onchange="updateSelectedCount()">
                    <span class="order-num">Заказ #{order.order_id}</span>
                </div>
                <div class="order-meta-info">
                    <span class="meta-item">Клиент ID: <strong>{customer_id}</strong></span>
                    <span class="meta-item">Красочность: <strong>{color_mode}</strong></span>
                    <span class="meta-item">Размер макета: <strong>{first_parsed.width_mm:.0f}×{first_parsed.height_mm:.0f} мм</strong></span>
                </div>
                <div>
                    {status_badge}
                </div>
            </div>

            <div class="card-body-grid">
                <!-- Левая колонка: Превью -->
                <div class="previews-column">
                    <span class="previews-box-title">Превью макетов</span>
                    <div class="previews-row {'single' if not back_file else 'double'}">
                        {previews_html}
                    </div>
                    <div class="frame-legend">
                        <span><span class="legend-dot green"></span> Зелёный контур (1 мм) — Край реза</span>
                        <span><span class="legend-dot red"></span> Красный контур (4 мм) — Безопасная зона</span>
                    </div>
                </div>

                <!-- Правая колонка: Статус и таблица -->
                <div class="details-column">
                    <table class="specs-table">
                        <thead>
                            {table_headers}
                        </thead>
                        <tbody>
                            {rows_code}
                        </tbody>
                    </table>

                    {messages_html}
                </div>
            </div>
        </article>
        """

    total_orders = len(orders)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html_template = f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Отчёт допечатного контроля макетов (Image-Magic Audit)</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #f1f5f9;
            --card-bg: #ffffff;
            --text-primary: #0f172a;
            --text-secondary: #64748b;
            --border-color: #e2e8f0;
            --pass-color: #10b981;
            --pass-bg: #ecfdf5;
            --pass-border: #a7f3d0;
            --warning-color: #f59e0b;
            --warning-bg: #fffbeb;
            --warning-border: #fde68a;
            --fail-color: #ef4444;
            --fail-bg: #fef2f2;
            --fail-border: #fecaca;
            --brand-primary: #2563eb;
            --brand-hover: #1d4ed8;
        }}

        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-primary);
            padding: 30px 20px 100px 20px;
            line-height: 1.5;
        }}

        .container {{
            max-width: 1680px;
            margin: 0 auto;
        }}

        /* Main Header */
        .main-header {{
            background: var(--card-bg);
            border-radius: 14px;
            border: 1px solid var(--border-color);
            padding: 24px 30px;
            margin-bottom: 24px;
            box-shadow: 0 4px 20px -2px rgba(0, 0, 0, 0.05);
        }}

        .header-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 20px;
            margin-bottom: 20px;
        }}

        .header-title h1 {{
            font-size: 24px;
            font-weight: 800;
            color: var(--text-primary);
            letter-spacing: -0.5px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .header-title p {{
            color: var(--text-secondary);
            font-size: 14px;
            margin-top: 4px;
        }}

        /* Stats Row */
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 12px;
            margin-bottom: 20px;
        }}

        .stat-card {{
            background: #f8fafc;
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 12px 18px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}

        .stat-label {{ font-size: 13px; font-weight: 600; color: var(--text-secondary); }}
        .stat-value {{ font-size: 20px; font-weight: 800; }}
        .stat-value.pass {{ color: var(--pass-color); }}
        .stat-value.warn {{ color: var(--warning-color); }}
        .stat-value.fail {{ color: var(--fail-color); }}

        /* Filter Controls & Select All */
        .controls-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
            padding-top: 16px;
            border-top: 1px solid var(--border-color);
        }}

        .select-all-label {{
            display: inline-flex;
            align-items: center;
            gap: 10px;
            font-size: 14px;
            font-weight: 700;
            color: var(--text-primary);
            cursor: pointer;
            user-select: none;
        }}

        .select-all-label input[type="checkbox"] {{
            width: 18px;
            height: 18px;
            accent-color: var(--brand-primary);
            cursor: pointer;
        }}

        .filter-pills {{
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
        }}

        .filter-btn {{
            background: #f1f5f9;
            border: 1px solid var(--border-color);
            padding: 7px 16px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-secondary);
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .filter-btn:hover, .filter-btn.active {{
            background: var(--brand-primary);
            color: #ffffff;
            border-color: var(--brand-primary);
        }}

        .count-tag {{
            background: rgba(255, 255, 255, 0.25);
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 11px;
        }}

        /* Order Cards Layout */
        .orders-list {{
            display: flex;
            flex-direction: column;
            gap: 20px;
        }}

        .order-card {{
            background: var(--card-bg);
            border-radius: 14px;
            border: 1px solid var(--border-color);
            overflow: hidden;
            box-shadow: 0 4px 14px rgba(0, 0, 0, 0.03);
            transition: border-color 0.2s ease, box-shadow 0.2s ease;
        }}

        .order-card.selected {{
            border-color: var(--brand-primary);
            box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.15);
        }}

        /* Card Header */
        .card-header-bar {{
            background: #f8fafc;
            border-bottom: 1px solid var(--border-color);
            padding: 14px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
        }}

        .order-checkbox-group {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}

        .order-checkbox {{
            width: 19px;
            height: 19px;
            accent-color: var(--brand-primary);
            cursor: pointer;
        }}

        .order-num {{
            font-size: 16px;
            font-weight: 800;
            color: var(--text-primary);
        }}

        .order-meta-info {{
            display: flex;
            gap: 16px;
            font-size: 13px;
            color: var(--text-secondary);
        }}

        .meta-item strong {{
            color: var(--text-primary);
        }}

        /* Status Badge */
        .status-badge {{
            padding: 5px 14px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 700;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .badge-pass {{ background: #d1fae5; color: #065f46; }}
        .badge-warning {{ background: #fef3c7; color: #92400e; }}
        .badge-fail {{ background: #fee2e2; color: #991b1b; }}

        /* Card Main Grid: Previews Left, Table Right */
        .card-body-grid {{
            display: grid;
            grid-template-columns: minmax(520px, 720px) 1fr;
            gap: 28px;
            padding: 20px 24px;
        }}

        @media (max-width: 1100px) {{
            .card-body-grid {{
                grid-template-columns: 1fr;
            }}
        }}

        /* Previews Column */
        .previews-column {{
            display: flex;
            flex-direction: column;
            gap: 10px;
        }}

        .previews-box-title {{
            font-size: 12px;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-secondary);
        }}

        .previews-row {{
            display: grid;
            gap: 14px;
            background: #f8fafc;
            padding: 14px;
            border-radius: 10px;
            border: 1px dashed var(--border-color);
        }}

        .previews-row.double {{ grid-template-columns: 1fr 1fr; }}
        .previews-row.single {{ grid-template-columns: 1fr; }}

        .preview-thumb-card {{
            background: #ffffff;
            border-radius: 8px;
            border: 1px solid var(--border-color);
            padding: 8px;
            text-align: center;
            cursor: pointer;
            transition: all 0.2s ease;
            position: relative;
        }}

        .preview-thumb-card:hover {{
            border-color: var(--brand-primary);
            box-shadow: 0 4px 12px rgba(37, 99, 235, 0.12);
            transform: translateY(-2px);
        }}

        .side-label {{
            font-size: 11px;
            font-weight: 700;
            color: var(--text-secondary);
            text-transform: uppercase;
            margin-bottom: 6px;
        }}

        .img-wrapper {{
            position: relative;
            background: #e2e8f0;
            border-radius: 4px;
            overflow: hidden;
        }}

        .preview-image {{
            width: 100%;
            height: 280px;
            object-fit: contain;
            display: block;
            background: #ffffff;
        }}

        .zoom-overlay {{
            position: absolute;
            inset: 0;
            background: rgba(15, 23, 42, 0.6);
            color: #ffffff;
            font-size: 11px;
            font-weight: 600;
            display: flex;
            align-items: center;
            justify-content: center;
            opacity: 0;
            transition: opacity 0.2s ease;
        }}

        .preview-thumb-card:hover .zoom-overlay {{
            opacity: 1;
        }}

        .thumb-filename {{
            font-size: 10px;
            color: var(--text-secondary);
            margin-top: 6px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}

        /* Frame Legend */
        .frame-legend {{
            display: flex;
            justify-content: center;
            gap: 16px;
            font-size: 11px;
            color: var(--text-secondary);
            margin-top: 4px;
        }}

        .legend-dot {{ width: 9px; height: 9px; border-radius: 50%; display: inline-block; margin-right: 4px; }}
        .legend-dot.green {{ background-color: var(--pass-color); }}
        .legend-dot.red {{ background-color: var(--fail-color); }}

        /* Inspection Details Column */
        .details-column {{
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }}

        .specs-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
            margin-bottom: 12px;
        }}

        .specs-table th {{
            background: #f1f5f9;
            color: var(--text-secondary);
            font-weight: 700;
            text-transform: uppercase;
            font-size: 11px;
            letter-spacing: 0.5px;
            padding: 8px 12px;
            text-align: left;
            border-bottom: 2px solid var(--border-color);
        }}

        .specs-table td {{
            padding: 8px 12px;
            border-bottom: 1px solid var(--border-color);
            color: var(--text-primary);
        }}

        .param-cell {{
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .icon-check {{
            width: 16px;
            height: 16px;
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 10px;
            font-weight: 800;
            color: white;
        }}
        .icon-check.pass {{ background: var(--pass-color); }}
        .icon-check.fail {{ background: var(--fail-color); }}

        /* Status & Alert Boxes */
        .alert-box {{
            padding: 12px 16px;
            border-radius: 8px;
            font-size: 13px;
            margin-top: 10px;
        }}
        .alert-box.pass {{ background: var(--pass-bg); border: 1px solid var(--pass-border); color: #065f46; }}
        .alert-box.warning {{ background: var(--warning-bg); border: 1px solid var(--warning-border); color: #92400e; }}
        .alert-box.fail {{ background: var(--fail-bg); border: 1px solid var(--fail-border); color: #991b1b; }}
        .alert-box ul {{ margin-left: 18px; margin-top: 4px; }}

        /* Sticky Action Bar at Bottom */
        .bottom-action-bar {{
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            background: #ffffff;
            border-top: 1px solid var(--border-color);
            padding: 14px 30px;
            box-shadow: 0 -4px 20px rgba(0, 0, 0, 0.08);
            z-index: 1000;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
        }}

        .action-info {{
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 14px;
            font-weight: 700;
            color: var(--text-primary);
        }}

        .selected-count-badge {{
            background: var(--brand-primary);
            color: #ffffff;
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 12px;
        }}

        .action-buttons {{
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
        }}

        .btn-action {{
            padding: 10px 22px;
            border-radius: 8px;
            font-size: 14px;
            font-weight: 700;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            transition: all 0.2s ease;
            border: 1px solid transparent;
        }}

        .btn-print {{
            background-color: var(--pass-color);
            color: #ffffff;
        }}

        .btn-print:hover {{
            background-color: #059669;
            box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);
        }}

        .btn-reject {{
            background-color: #ffffff;
            color: var(--fail-color);
            border-color: var(--fail-color);
        }}

        .btn-reject:hover {{
            background-color: var(--fail-bg);
            box-shadow: 0 4px 12px rgba(239, 68, 68, 0.15);
        }}

        /* Lightbox Modal */
        .lightbox-modal {{
            display: none;
            position: fixed;
            inset: 0;
            z-index: 9999;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(4px);
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}

        .lightbox-modal.active {{ display: flex; }}

        .lightbox-box {{
            max-width: 90vw;
            max-height: 90vh;
            display: flex;
            flex-direction: column;
            align-items: center;
            position: relative;
        }}

        .lightbox-img {{
            max-width: 100%;
            max-height: 80vh;
            border-radius: 8px;
            background: #ffffff;
            object-fit: contain;
            box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5);
        }}

        .lightbox-title {{
            color: #ffffff;
            margin-top: 14px;
            font-size: 15px;
            font-weight: 600;
        }}

        .lightbox-close-btn {{
            position: absolute;
            top: -42px;
            right: 0;
            color: #ffffff;
            font-size: 30px;
            font-weight: 700;
            cursor: pointer;
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Main Header -->
        <header class="main-header">
            <div class="header-top">
                <div class="header-title">
                    <h1>✨ Image-Magic Audit • Отчёт допечатного контроля</h1>
                    <p>Пакетный анализ и автоматическая подготовка печатных файлов к производству • Дата: {now_str}</p>
                </div>
            </div>

            <!-- Stats Bar -->
            <div class="stats-grid">
                <div class="stat-card">
                    <span class="stat-label">Всего заказов</span>
                    <span class="stat-value">{total_orders}</span>
                </div>
                <div class="stat-card">
                    <span class="stat-label">Прошли проверку</span>
                    <span class="stat-value pass">{pass_count}</span>
                </div>
                <div class="stat-card">
                    <span class="stat-label">Предупреждения</span>
                    <span class="stat-value warn">{warn_count}</span>
                </div>
                <div class="stat-card">
                    <span class="stat-label">Ошибки / Браковано</span>
                    <span class="stat-value fail">{fail_count}</span>
                </div>
            </div>

            <!-- Control Bar (Select All + Filters) -->
            <div class="controls-row">
                <label class="select-all-label">
                    <input type="checkbox" id="selectAllCheckbox" onchange="toggleSelectAll(this)">
                    <span>Выбрать все заказы на странице</span>
                </label>

                <div class="filter-pills">
                    <button class="filter-btn active" onclick="applyFilter('all', this)">Все <span class="count-tag">{total_orders}</span></button>
                    <button class="filter-btn" onclick="applyFilter('pass', this)">Пройденные <span class="count-tag">{pass_count}</span></button>
                    <button class="filter-btn" onclick="applyFilter('warning', this)">Предупреждения <span class="count-tag">{warn_count}</span></button>
                    <button class="filter-btn" onclick="applyFilter('fail', this)">Ошибки <span class="count-tag">{fail_count}</span></button>
                </div>
            </div>
        </header>

        <!-- Orders Cards Container -->
        <main class="orders-list">
            {order_cards_html}
        </main>
    </div>

    <!-- Sticky Action Bar at Bottom -->
    <div class="bottom-action-bar">
        <div class="action-info">
            <span>Выбрано заказов:</span>
            <span class="selected-count-badge" id="selectedCounter">0</span>
        </div>

        <div class="action-buttons">
            <button class="btn-action btn-reject" onclick="handleAction('reject')">
                ↩️ Вернуть заказы на доработку
            </button>
            <button class="btn-action btn-print" onclick="handleAction('print')">
                🖨️ Провести заказы на печать
            </button>
        </div>
    </div>

    <!-- Lightbox Modal -->
    <div id="lightboxModal" class="lightbox-modal" onclick="closeModal(event)">
        <div class="lightbox-box">
            <span class="lightbox-close-btn" onclick="closeModal(event)">✕</span>
            <img id="modalImg" class="lightbox-img" src="" alt="Full preview">
            <div id="modalTitle" class="lightbox-title"></div>
        </div>
    </div>

    <script>
        // Modal Lightbox functions
        function openModal(src, title) {{
            const modal = document.getElementById('lightboxModal');
            document.getElementById('modalImg').src = src;
            document.getElementById('modalTitle').innerText = title;
            modal.classList.add('active');
        }}

        function closeModal(e) {{
            if (e.target.id === 'lightboxModal' || e.target.classList.contains('lightbox-close-btn')) {{
                document.getElementById('lightboxModal').classList.remove('active');
            }}
        }}

        document.addEventListener('keydown', (e) => {{
            if (e.key === 'Escape') {{
                document.getElementById('lightboxModal').classList.remove('active');
            }}
        }});

        // Selection & Action functions
        function toggleSelectAll(masterCheckbox) {{
            const checkboxes = document.querySelectorAll('.order-checkbox');
            checkboxes.forEach(cb => {{
                const card = cb.closest('.order-card');
                if (card.style.display !== 'none') {{
                    cb.checked = masterCheckbox.checked;
                    toggleCardHighlight(cb);
                }}
            }});
            updateSelectedCount();
        }}

        function toggleCardHighlight(cb) {{
            const card = cb.closest('.order-card');
            if (cb.checked) {{
                card.classList.add('selected');
            }} else {{
                card.classList.remove('selected');
            }}
        }}

        function updateSelectedCount() {{
            const checkboxes = document.querySelectorAll('.order-checkbox');
            let count = 0;
            checkboxes.forEach(cb => {{
                toggleCardHighlight(cb);
                if (cb.checked) count++;
            }});
            document.getElementById('selectedCounter').innerText = count;
        }}

        // Filtering
        function applyFilter(status, btn) {{
            const buttons = document.querySelectorAll('.filter-btn');
            buttons.forEach(b => b.classList.remove('active'));
            btn.classList.add('active');

            const cards = document.querySelectorAll('.order-card');
            cards.forEach(card => {{
                if (status === 'all' || card.dataset.status === status) {{
                    card.style.display = 'block';
                }} else {{
                    card.style.display = 'none';
                }}
            }});

            document.getElementById('selectAllCheckbox').checked = false;
            updateSelectedCount();
        }}

        // Action handlers
        function handleAction(type) {{
            const selected = [];
            document.querySelectorAll('.order-checkbox:checked').forEach(cb => {{
                const num = cb.closest('.order-card').querySelector('.order-num').innerText;
                selected.push(num);
            }});

            if (selected.length === 0) {{
                alert('Пожалуйста, выберите хотя бы один заказ с помощью чекбокса.');
                return;
            }}

            if (type === 'print') {{
                alert(`✅ ${{selected.length}} зак.(а/ов) отправлено в печать!\\n\\nСписок:\\n` + selected.join('\\n'));
            }} else if (type === 'reject') {{
                alert(`↩️ ${{selected.length}} зак.(а/ов) возвращено на доработку менеджеру!\\n\\nСписок:\\n` + selected.join('\\n'));
            }}
        }}
    </script>
</body>
</html>
"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_template)

    return str(out_path)
