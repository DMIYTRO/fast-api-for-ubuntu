#!/usr/bin/env python3
import argparse
from pathlib import Path

from processing import BatchProcessor
from config.profiles import DEFAULT_DIRECTION, PROFILES, get_profile


# Быстро изменяемые пути для тестового запуска.
INPUT_DIR = Path("/Users/admin/Desktop/sempels")
PDF_DIR_NAME = "PDF"
TROUBLES_DIR_NAME = "Troubles"
PREVIEWS_DIR_NAME = "Previews"


def print_report(processor: BatchProcessor, orders) -> None:
    print("\n" + "=" * 78)
    print("ПРОВЕРКА ЗАКАЗОВ")
    print("=" * 78)

    for order in orders:
        first = order.files[0].parsed
        expected_w = first.width_mm + processor.size_extra_mm
        expected_h = first.height_mm + processor.size_extra_mm
        mode = f"{first.front_colors}-{first.back_colors}"
        print(f"\nЗаказ {order.order_id} | клиент {order.customer_id} | цветность {mode}")
        print(f"  Ожидаемый размер: {expected_w:.1f}x{expected_h:.1f} мм, допуск ±{processor.tolerance_mm:.1f} мм")

        for item in sorted(order.files, key=lambda value: 0 if value.parsed.side == "face" else 1):
            size = (
                f"{item.actual_width_mm:.1f}x{item.actual_height_mm:.1f} мм"
                if item.actual_width_mm is not None else "не определён"
            )
            status = "PASS" if item.passed else "FAIL"
            orientation = (
                processor._orientation(item.actual_width_mm, item.actual_height_mm)
                if item.actual_width_mm is not None and item.actual_height_mm is not None
                else "не определена"
            )
            print(
                f"  [{status}] {item.parsed.side.upper():4} | размер {size} | "
                f"ориентация {orientation} | "
                f"DPI {item.dpi_x if item.dpi_x is not None else '?'}x{item.dpi_y if item.dpi_y is not None else '?'} | "
                f"цвет {item.colorspace or 'не определён'}"
            )
            print(f"         {item.path.name}")
            for warning in item.warnings:
                print(f"         Предупреждение: {warning}")
            for error in item.errors:
                print(f"         Причина: {error}")

        for error in order.errors:
            print(f"  Причина заказа: {error}")
        for warning in order.warnings:
            print(f"  Предупреждение: {warning}")
        found_sides = {item.parsed.side for item in order.files}
        required_sides = {"face", "back"} if first.back_colors > 0 else {"face"}
        print(f"  Группа: {'НАЙДЕНА' if required_sides.issubset(found_sides) else 'НЕПОЛНАЯ'}")
        print(f"  ИТОГ: {'ПОДХОДИТ ДЛЯ PDF' if order.passed else 'НЕ ПОДХОДИТ'}")

    if processor.unparsed:
        print("\nФАЙЛЫ С НЕРАСПОЗНАННЫМИ ИМЕНАМИ")
        for item in processor.unparsed:
            print(f"  [FAIL] {item.path.name}")
            for error in item.errors:
                print(f"         Причина: {error}")

    if processor.unsupported:
        print("\nФАЙЛЫ ИСКЛЮЧЁННЫХ ФОРМАТОВ")
        for item in processor.unsupported:
            print(f"  [SKIP] {item.path.name}")
            for error in item.errors:
                print(f"         Причина: {error}")

    passed = sum(order.passed for order in orders)
    failed = len(orders) - passed
    print("\n" + "-" * 78)
    print(
        f"Заказов: {len(orders)} | подходят: {passed} | не подходят: {failed} | "
        f"не распознано файлов: {len(processor.unparsed)} | исключено форматов: {len(processor.unsupported)}"
    )


def generate_and_print_previews(
    processor: BatchProcessor,
    pdf_paths: list[Path],
    preview_dir: Path,
    pdf_page_names_map: Optional[dict[Path, list[str]]] = None,
) -> None:
    if not pdf_paths:
        return
    print(f"\n" + "=" * 78)
    print(f"ГЕНЕРАЦИЯ ПРЕВЬЮ С РАМКАМИ ({len(pdf_paths)} PDF)")
    print(f"Папка превью: {preview_dir}")
    print("=" * 78)

    results = processor.generate_previews_for_all(
        pdf_paths, preview_dir, pdf_page_names_map=pdf_page_names_map
    )
    success_count = 0
    for pdf_path, previews, error in results:
        if error:
            print(f"  [FAIL] {pdf_path.name}: {error}")
        else:
            success_count += 1
            names = ", ".join(p.name for p in previews)
            print(f"  [OK] {pdf_path.name} → {names}")

    print(f"\nУспешно сгенерировано превью для {success_count}/{len(pdf_paths)} PDF файлов.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка заказов, создание PDF и генерация превью с рамками")
    parser.add_argument("--input", type=Path, default=INPUT_DIR, help="Папка с исходными файлами")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Папка для готовых PDF (по умолчанию: подпапка PDF во входной папке)",
    )
    parser.add_argument(
        "--previews-dir",
        type=Path,
        default=None,
        help="Папка для сгенерированных превью (по умолчанию: подпапка Previews во входной папке)",
    )
    parser.add_argument(
        "--previews-only",
        action="store_true",
        help="Сгенерировать превью для всех существующих PDF в папки PDF без проверки входных исходников",
    )
    parser.add_argument(
        "--direction",
        choices=tuple(PROFILES),
        default=DEFAULT_DIRECTION,
        help=f"Профиль направления (по умолчанию: {DEFAULT_DIRECTION})",
    )
    args = parser.parse_args()

    output_dir = args.output if args.output is not None else args.input / PDF_DIR_NAME
    preview_dir = args.previews_dir if args.previews_dir is not None else args.input / PREVIEWS_DIR_NAME
    processor = BatchProcessor(args.input, output_dir, profile=get_profile(args.direction))
    print(f"Профиль направления: {processor.profile.name} ({processor.profile.direction})")

    if args.previews_only:
        existing_pdfs = sorted(output_dir.glob("*.pdf")) if output_dir.is_dir() else []
        if not existing_pdfs:
            print(f"В папке {output_dir} не найдено PDF файлов.")
            return
        generate_and_print_previews(processor, existing_pdfs, preview_dir)
        return

    try:
        orders = processor.inspect_orders()
    except Exception as exc:
        raise SystemExit(f"Ошибка: {exc}") from exc

    pending = [
        item for order in orders for item in order.files
        if item.resample_decision == "ask_confirmation"
    ]
    for item in pending:
        crop_x, crop_y = item.resample_crop_mm
        rotation = f", поворот {item.rotation_degrees}°" if item.rotation_degrees else ""
        answer = input(
            f"\nФайл {item.path.name}: требуется коррекция до "
            f"{item.resample_target_mm[0]:.1f}x{item.resample_target_mm[1]:.1f} мм "
            f"(центральная обрезка {crop_x:.2f}x{crop_y:.2f} мм{rotation}). "
            "Выполнить? [y/N]: "
        ).strip().lower()
        processor.confirm_resample(item, answer in {"y", "yes", "д", "да"})

    print_report(processor, orders)
    troubles_dir = args.input / TROUBLES_DIR_NAME
    trouble_results = processor.copy_failed_to_troubles(orders, troubles_dir)
    if trouble_results:
        print("\nФАЙЛЫ, НЕ ПОПАВШИЕ В ОБРАБОТКУ")
        for source, target, error in trouble_results:
            if error:
                print(f"  [FAIL] {source.name}: не удалось скопировать — {error}")
            else:
                print(f"  [COPIED] {source.name} → {target}")
        print(f"  Папка: {troubles_dir}")

    suitable = [order for order in orders if order.passed]
    created_pdf_paths: list[Path] = []
    created_pdf_map: dict[str, Path] = {}

    if suitable:
        answer = input(f"\nСоздать PDF для подходящих заказов ({len(suitable)})? [y/N]: ").strip().lower()
        if answer in {"y", "yes", "д", "да"}:
            results = processor.create_pdfs(suitable)
            print("\nРЕЗУЛЬТАТ СОЗДАНИЯ PDF")
            for order, path, error in results:
                if error:
                    print(f"  [FAIL] Заказ {order.order_id}: {error}")
                    routed = processor.copy_pdf_failure_to_troubles(order, troubles_dir, error)
                    copied = sum(copy_error is None for _, _, copy_error in routed)
                    print(f"         Скопировано в Troubles: {copied}/{len(routed)} файлов")
                    for source, _, copy_error in routed:
                        if copy_error:
                            print(f"         Не удалось скопировать {source.name}: {copy_error}")
                else:
                    print(f"  [PASS] Заказ {order.order_id}: {path}")
                    created_pdf_paths.append(path)
                    created_pdf_map[order.order_id] = path
        else:
            print("Создание PDF отменено. Исходные файлы не изменены.")

    # Построение карты имен оригинальных макетов для превью (face / back)
    pdf_page_names_map: dict[Path, list[str]] = {}
    for order in orders:
        ordered_files = sorted(order.files, key=lambda item: 0 if item.parsed.side == "face" else 1)
        face_file = next((item for item in ordered_files if item.parsed.side == "face"), None)
        if face_file:
            pdf_p = output_dir / f"{face_file.path.stem}.pdf"
            pdf_page_names_map[pdf_p] = [item.path.stem for item in ordered_files]

    existing_pdfs = sorted(output_dir.glob("*.pdf")) if output_dir.is_dir() else []
    target_pdfs = created_pdf_paths if created_pdf_paths else existing_pdfs

    previews_generated_count = 0
    if target_pdfs:
        answer_preview = input(
            f"\nСгенерировать превью с рамками (зелёная 1 мм обрез / красная 4 мм безопасная зона) для {len(target_pdfs)} PDF? [Y/n]: "
        ).strip().lower()
        if answer_preview in {"", "y", "yes", "д", "да"}:
            generate_and_print_previews(
                processor, target_pdfs, preview_dir, pdf_page_names_map=pdf_page_names_map
            )
            previews_generated_count = len(target_pdfs)

    # CLI и web используют одну модель истории запусков.
    try:
        from server.database import Database, upgrade_database
        from server.settings import Settings
        from services.sql_repository import SqlRunRepository
        from services.use_cases import record_completed_cli_run

        history_settings = Settings.from_env()
        upgrade_database(history_settings)
        history_database = Database(history_settings.database_url)
        try:
            record_completed_cli_run(
                SqlRunRepository(
                    history_database.session_factory,
                    recover_interrupted=False,
                ),
                input_path=args.input,
                direction=args.direction,
                orders=orders,
                pdf_paths=created_pdf_map,
            )
        finally:
            history_database.dispose()
        print(f"\n[DB] История проверки {len(orders)} заказов сохранена в общей базе запусков.")
    except Exception as db_exc:
        print(f"\n[DB] Не удалось сохранить историю аудита в БД: {db_exc}")

    # Генерация интерактивного HTML отчёта
    try:
        from core.report_builder import build_orders_html_report
        report_html_path = args.input / "output_report" / "report.html"
        generated_html = build_orders_html_report(
            orders=orders,
            output_html_path=report_html_path,
            preview_dir=preview_dir,
            profile=processor.profile,
        )
        print(f"[HTML] Интерактивный HTML-отчёт успешно создан: {generated_html}")
    except Exception as html_exc:
        print(f"[HTML] Не удалось сгенерировать HTML-отчёт: {html_exc}")


if __name__ == "__main__":
    main()
