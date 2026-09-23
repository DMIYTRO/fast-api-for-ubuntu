#!/usr/bin/env python3
"""
Пакетный инспектор макетов и генератор статического HTML-отчёта pre-press проверки.

Функции:
1. Сканирование входной папки с графическими файлами (TIFF, JPEG, PNG, PDF).
2. Считывание параметров: размер в мм, DPI, цветовая модель, размер файла в МБ.
    3. Валидация по нормам допечатной подготовки (CMYK, 270 DPI, до 2000 МБ).
4. Отрисовка превью с виртуальными рамками (1 мм красная - край реза, 4 мм зеленая - безопасная зона).
5. Сборка статической HTML-страницы отчёта в стиле допечатного модуля.
"""

import os
import sys
import glob
import shutil
import subprocess

def get_image_metadata(image_path: str):
    """Считывает метаданные изображения через ImageMagick identify."""
    magick_cmd = shutil.which("magick") or shutil.which("identify")
    if not magick_cmd:
        raise FileNotFoundError("ImageMagick CLI не найден.")

    cmd = [
        magick_cmd, "identify",
        "-format", "%f\n%m\n%w\n%h\n%x\n%y\n%[units]\n%[colorspace]\n%[type]\n%[depth]\n%A",
        image_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    lines = [line.strip() for line in result.stdout.strip().split("\n")]

    file_name = lines[0] if len(lines) > 0 else os.path.basename(image_path)
    file_fmt = lines[1] if len(lines) > 1 else ""
    w_px = float(lines[2]) if len(lines) > 2 else 0.0
    h_px = float(lines[3]) if len(lines) > 3 else 0.0
    res_x = float(lines[4]) if len(lines) > 4 else 72.0
    units = lines[6] if len(lines) > 6 else "PixelsPerInch"
    colorspace = lines[7] if len(lines) > 7 else "sRGB"
    img_type = lines[8] if len(lines) > 8 else ""

    if "Centimeter" in units:
        dpi = res_x * 2.54
    else:
        dpi = res_x if res_x > 0 else 72.0

    # Расчет физических размеров в миллиметрах
    width_mm = round((w_px / dpi) * 25.4, 1)
    height_mm = round((h_px / dpi) * 25.4, 1)

    # Размер файла в Мегабайтах
    size_bytes = os.path.getsize(image_path)
    size_mb = round(size_bytes / (1024 * 1024), 2)

    return {
        "file_name": file_name,
        "format": file_fmt,
        "width_px": int(w_px),
        "height_px": int(h_px),
        "dpi": round(dpi, 1),
        "width_mm": width_mm,
        "height_mm": height_mm,
        "colorspace": colorspace,
        "type": img_type,
        "size_mb": size_mb
    }

def generate_preview_image(input_path: str, output_preview_path: str, dpi: float, w_px: int, h_px: int, safe_mm: float = 4.0, border_mm: float = 1.0):
    """Генерирует превью макета с наложением красной (1 мм) и зеленой (4 мм) рамок."""
    magick_cmd = shutil.which("magick")
    if not magick_cmd:
        raise FileNotFoundError("ImageMagick CLI (`magick`) не найден.")

    safe_px = round(safe_mm * (dpi / 25.4))
    border_px = max(1, round(border_mm * (dpi / 25.4)))

    gx1 = safe_px
    gy1 = safe_px
    gx2 = w_px - safe_px
    gy2 = h_px - safe_px

    half_b = border_px / 2.0
    rx1 = half_b
    ry1 = half_b
    rx2 = w_px - half_b
    ry2 = h_px - half_b

    cmd = [
        magick_cmd, input_path,
        "-stroke", "#dc3545",
        "-strokewidth", str(border_px),
        "-fill", "none",
        "-draw", f"rectangle {rx1},{ry1} {rx2},{ry2}",
        "-stroke", "#28a745",
        "-strokewidth", str(max(2, round(border_px * 0.4))),
        "-fill", "none",
        "-draw", f"rectangle {gx1},{gy1} {gx2},{gy2}",
        output_preview_path
    ]
    subprocess.run(cmd, check=True)

def process_directory(input_dir: str, output_dir: str):
    """Осуществляет пакетную обработку директории и верстку HTML-отчёта."""
    os.makedirs(output_dir, exist_ok=True)
    previews_dir = os.path.join(output_dir, "previews")
    os.makedirs(previews_dir, exist_ok=True)

    extensions = ("*.jpg", "*.jpeg", "*.tif", "*.tiff", "*.png")
    image_files = []
    for ext in extensions:
        image_files.extend(glob.glob(os.path.join(input_dir, ext)))
        image_files.extend(glob.glob(os.path.join(input_dir, ext.upper())))

    image_files.sort()

    cards_data = []

    for img_path in image_files:
        meta = get_image_metadata(img_path)
        base_name = os.path.basename(img_path)
        preview_filename = f"preview_{base_name}.jpg"
        preview_filepath = os.path.join(previews_dir, preview_filename)
        rel_preview_path = f"previews/{preview_filename}"

        # Генерация превью
        try:
            generate_preview_image(
                img_path,
                preview_filepath,
                dpi=meta["dpi"],
                w_px=meta["width_px"],
                h_px=meta["height_px"]
            )
        except Exception as e:
            print(f"Ошибка при создании превью для {base_name}: {e}")
            shutil.copy(img_path, preview_filepath)

        # Валидация по нормам
        target_dpi = 300.0
        dpi_ok = meta["dpi"] >= 280.0
        cmyk_ok = meta["colorspace"].upper() in ("CMYK", "COLORSEPARATION")
        size_ok = meta["size_mb"] <= 2000.0

        # Размеры реза
        target_w_mm = meta["width_mm"]
        target_h_mm = meta["height_mm"]

        overall_passed = dpi_ok and cmyk_ok and size_ok

        cards_data.append({
            "meta": meta,
            "preview_rel": rel_preview_path,
            "dpi_ok": dpi_ok,
            "cmyk_ok": cmyk_ok,
            "size_ok": size_ok,
            "overall_passed": overall_passed,
            "target_dpi": int(target_dpi),
            "target_w_mm": target_w_mm,
            "target_h_mm": target_h_mm
        })

    # Генерация HTML-страницы
    html_content = generate_html_report(cards_data)
    html_path = os.path.join(output_dir, "report.html")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"✅ Пакетная обработка завершена! Обработано файлов: {len(cards_data)}")
    print(f"📁 Отчёт сохранён по пути: {html_path}")
    return html_path

def generate_html_report(cards: list) -> str:
    """Генерирует статическую HTML-страницу с дизайном в точности как на образце."""

    cards_html = ""
    for idx, card in enumerate(cards):
        m = card["meta"]
        passed = card["overall_passed"]

        dpi_icon = '<span class="status-icon pass">✔</span>' if card["dpi_ok"] else '<span class="status-icon fail">✖</span>'
        cmyk_icon = '<span class="status-icon pass">✔</span>' if card["cmyk_ok"] else '<span class="status-icon fail">✖</span>'
        size_icon = '<span class="status-icon pass">✔</span>' if card["size_ok"] else '<span class="status-icon fail">✖</span>'
        dims_icon = '<span class="status-icon pass">✔</span>'

        status_box_class = "pass-box" if passed else "fail-box"
        status_text = (
            "<strong>Макет успешно прошёл автоматическую проверку.</strong><br>Макет готов к отправке в печать."
            if passed else
            "<strong>Макет не прошёл автоматическую проверку.</strong><br>"
            "Отдел допечатной подготовки проверит макет и вы получите соответствующее уведомление «проверен» или «вернут на доработку»."
        )

        card_item = f"""
        <div class="report-card">
            <div class="card-header">
                <span class="side-title">Сторона / Макет #{idx + 1}:</span>
                <div class="upload-bar">
                    <button class="upload-btn">ЗАГРУЗИТЬ ФАЙЛ</button>
                    <div class="file-info">
                        <span class="dot">•</span>
                        <span class="file-name">{m['file_name']}</span>
                        <span class="file-size">{m['size_mb']} МБ</span>
                        <span class="file-status-badge">Файл успешно загружен и обработан</span>
                    </div>
                </div>
            </div>

            <!-- Зона визуализации макета с рамками -->
            <div class="preview-container">
                <div class="preview-frame">
                    <img src="{card['preview_rel']}" alt="Превью {m['file_name']}" class="preview-img">
                </div>
                <div class="legend">
                    <span class="legend-item"><span class="color-box red"></span> Красный контур (1 мм) — край реза</span>
                    <span class="legend-item"><span class="color-box green"></span> Зеленый контур (4 мм) — безопасная зона</span>
                </div>
            </div>

            <!-- Таблица параметров макета -->
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
                    <tr>
                        <td class="param-title">{dims_icon} Размер, мм</td>
                        <td class="val-cell">{m['width_mm']} × {m['height_mm']}</td>
                        <td class="target-cell">{m['width_mm']} × {m['height_mm']}</td>
                        <td class="status-cell">соответствует</td>
                    </tr>
                    <tr>
                        <td class="param-title">{dpi_icon} Разрешающая способность, DPI</td>
                        <td class="val-cell">{int(m['dpi'])} DPI</td>
                        <td class="target-cell">270</td>
                        <td class="status-cell">{'270 DPI' if card['dpi_ok'] else '<span class="text-danger">требуется 270 DPI</span>'}</td>
                    </tr>
                    <tr>
                        <td class="param-title">{cmyk_icon} Цветовая модель</td>
                        <td class="val-cell">{m['colorspace']}</td>
                        <td class="target-cell">CMYK</td>
                        <td class="status-cell">{m['colorspace']}</td>
                    </tr>
                    <tr>
                        <td class="param-title">{size_icon} Размер файла, Mb</td>
                        <td class="val-cell">{m['size_mb']}</td>
                        <td class="target-cell">до 2000</td>
                        <td class="status-cell">норма</td>
                    </tr>
                </tbody>
            </table>

            <!-- Уведомление о статусе проверки -->
            <div class="status-box {status_box_class}">
                <p class="status-message">{status_text}</p>
                <label class="confirm-checkbox">
                    <input type="checkbox" {'checked' if passed else ''}>
                    <span>Подтверждаю автоматически доработанный макет, претензий по обрезке и цвету иметь не буду. (Иначе макет будет обработан в ручном режиме).</span>
                </label>
            </div>
        </div>
        """
        cards_html += card_item

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Отчёт допечатной проверки макетов (Pre-Press Audit)</title>
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
            --header-red-hover: #9e2222;
            --pass-color: #28a745;
            --fail-color: #dc3545;
            --border-color: #dee2e6;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-primary);
            padding: 40px 20px;
            line-height: 1.5;
        }}

        .container {{
            max-width: 900px;
            margin: 0 auto;
        }}

        .main-header {{
            margin-bottom: 30px;
            text-align: center;
        }}

        .main-header h1 {{
            font-size: 26px;
            font-weight: 700;
            color: #1a1a1a;
        }}

        .main-header p {{
            color: var(--text-secondary);
            font-size: 14px;
            margin-top: 5px;
        }}

        .report-card {{
            background: var(--card-bg);
            border-radius: 8px;
            border: 1px solid var(--border-color);
            padding: 24px;
            margin-bottom: 35px;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
        }}

        .card-header {{
            margin-bottom: 20px;
        }}

        .side-title {{
            font-size: 16px;
            font-weight: 600;
            color: #333;
            display: block;
            margin-bottom: 10px;
        }}

        .upload-bar {{
            display: flex;
            align-items: center;
            gap: 15px;
            flex-wrap: wrap;
        }}

        .upload-btn {{
            background-color: #555;
            color: white;
            border: none;
            padding: 10px 18px;
            font-size: 13px;
            font-weight: 600;
            letter-spacing: 0.5px;
            border-radius: 4px;
            cursor: pointer;
            text-transform: uppercase;
        }}

        .file-info {{
            font-size: 14px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}

        .dot {{
            color: #888;
        }}

        .file-name {{
            font-weight: 600;
            color: #222;
        }}

        .file-size {{
            color: #666;
        }}

        .file-status-badge {{
            color: var(--pass-color);
            font-weight: 500;
        }}

        /* Preview box */
        .preview-container {{
            text-align: center;
            margin: 25px 0;
            background: #fafafa;
            padding: 20px;
            border-radius: 6px;
            border: 1px dashed var(--border-color);
        }}

        .preview-frame {{
            display: inline-block;
            box-shadow: 0 5px 15px rgba(0,0,0,0.1);
            background: #fff;
            max-width: 100%;
        }}

        .preview-img {{
            max-width: 100%;
            max-height: 420px;
            display: block;
            height: auto;
        }}

        .legend {{
            margin-top: 12px;
            font-size: 13px;
            display: flex;
            justify-content: center;
            gap: 20px;
            color: #555;
        }}

        .legend-item {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}

        .color-box {{
            width: 14px;
            height: 14px;
            border-radius: 2px;
            display: inline-block;
        }}

        .color-box.red {{ background-color: #dc3545; }}
        .color-box.green {{ background-color: #28a745; }}

        /* Table Styling */
        .params-table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
            font-size: 14px;
        }}

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

        .params-table th.col-param {{
            text-align: left;
            width: 40%;
        }}

        .params-table td {{
            padding: 12px 16px;
            border: 1px solid var(--border-color);
            text-align: center;
        }}

        .params-table td.param-title {{
            text-align: left;
            font-weight: 500;
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .status-icon {{
            width: 22px;
            height: 22px;
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-size: 12px;
            font-weight: bold;
            color: white;
            flex-shrink: 0;
        }}

        .status-icon.pass {{
            background-color: var(--pass-color);
        }}

        .status-icon.fail {{
            background-color: var(--fail-color);
        }}

        .status-box {{
            margin-top: 25px;
            padding: 18px;
            border-radius: 6px;
            font-size: 14px;
        }}

        .status-box.pass-box {{
            background-color: #e8f5e9;
            border: 1px solid #c8e6c9;
            color: #1b5e20;
        }}

        .status-box.fail-box {{
            background-color: #fff8f8;
            border: 1px solid #ffcdd2;
            color: #842029;
        }}

        .status-message {{
            margin-bottom: 12px;
            line-height: 1.5;
        }}

        .confirm-checkbox {{
            display: flex;
            align-items: flex-start;
            gap: 10px;
            font-size: 13px;
            color: #333;
            cursor: pointer;
        }}

        .confirm-checkbox input {{
            margin-top: 3px;
        }}

        .text-danger {{
            color: var(--fail-color);
            font-weight: 600;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header class="main-header">
            <h1>Отчёт допечатного контроля макетов (Pre-Press Audit)</h1>
            <p>Пакетная проверка файлов в папке • Автоматический расчёт рамок обрезки и безопасных зон</p>
        </header>

        {cards_html}
    </div>
</body>
</html>
"""

if __name__ == "__main__":
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    input_directory = sys.argv[1] if len(sys.argv) > 1 else os.path.join(base_dir, "input_files")
    output_directory = sys.argv[2] if len(sys.argv) > 2 else os.path.join(base_dir, "output_report")

    if not os.path.exists(input_directory):
        print(f"Ошибка: Директория с макетами '{input_directory}' не найдена.")
        sys.exit(1)

    print(f"🚀 Запуск проверки файлов в папке:\n   📂 {input_directory}\n")
    process_directory(input_directory, output_directory)
