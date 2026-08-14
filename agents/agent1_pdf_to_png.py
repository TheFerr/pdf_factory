#!/usr/bin/env python3
"""
Агент 1: Постраничная конвертация PDF файлов в PNG изображения.

Использует PyMuPDF (fitz) для быстрого рендеринга страниц без
зависимости от внешних бинарников (poppler и т.д.).

Каждая страница сохраняется как отдельный PNG в формате:
    {png_dir}/{pdf_basename}/page_{NNNN}.png

По завершении обработки всех файлов записывает в status.json
финальный список страниц для передачи агенту 2, и переключает
stage на 2 (это сигнал для оркестратора запустить следующий этап).

Использование:
    python3 agent1_pdf_to_png.py \
        --pdf_dir /path/to/pdf \
        --png_dir /path/to/png \
        --status_file /path/to/status.json \
        --files file1.pdf,file2.pdf \
        --dpi 200
"""

import argparse
import os
import sys
import time
import traceback

import fitz  # PyMuPDF

# Позволяет запускать скрипт как отдельно, так и из run_pipeline.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from status_manager import StatusManager


# Точки на дюйм для рендеринга. 150 DPI — компромисс между
# качеством распознавания кодов агентом 2 и скоростью/размером PNG.
DEFAULT_DPI = 150


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PDF -> PNG converter agent")
    parser.add_argument("--pdf_dir", required=True, help="Директория с исходными PDF")
    parser.add_argument("--png_dir", required=True, help="Директория для сохранения PNG")
    parser.add_argument("--status_file", required=True, help="Путь к status.json")
    parser.add_argument("--files", required=True,
                         help="Список имён PDF файлов через запятую")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI,
                         help="Разрешение рендеринга страниц (по умолчанию 200)")
    return parser.parse_args()


def count_total_pages(pdf_paths: list, status: StatusManager) -> dict:
    """
    Предварительный проход по всем PDF для подсчёта общего количества
    страниц. Это необходимо для корректного расчёта общего percent
    прогресса, так как файлы могут содержать от 1 до 250 страниц каждый.

    Возвращает словарь {pdf_path: page_count}.
    """
    page_counts = {}

    for pdf_path in pdf_paths:
        try:
            with fitz.open(pdf_path) as doc:
                page_counts[pdf_path] = doc.page_count
        except Exception as exc:
            status.add_log(
                f"[Stage1] ОШИБКА: не удалось открыть {os.path.basename(pdf_path)}: {exc}"
            )
            page_counts[pdf_path] = 0

    return page_counts


def convert_pdf_to_png(pdf_path: str, output_dir: str, dpi: int,
                        status: StatusManager, processed_so_far: int,
                        total_pages: int) -> tuple:
    """
    Конвертирует один PDF файл постранично в PNG.

    Возвращает кортеж:
        (список путей к созданным PNG, обновлённый processed_so_far)
    """
    pdf_basename = os.path.splitext(os.path.basename(pdf_path))[0]
    file_output_dir = os.path.join(output_dir, pdf_basename)
    os.makedirs(file_output_dir, exist_ok=True)

    png_paths = []

    # Матрица масштабирования: PyMuPDF по умолчанию рендерит в 72 DPI,
    # поэтому пересчитываем множитель для заданного DPI.
    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf_path) as doc:
        page_count = doc.page_count

        status.add_log(
            f"[Stage1] Начата обработка {os.path.basename(pdf_path)} ({page_count} стр.)"
        )

        for page_index in range(page_count):
            page = doc.load_page(page_index)
            pixmap = page.get_pixmap(matrix=matrix)

            # Нумерация страниц с ведущими нулями для корректной сортировки
            page_filename = f"page_{page_index + 1:04d}.png"
            output_path = os.path.join(file_output_dir, page_filename)

            pixmap.save(output_path)
            png_paths.append(output_path)

            processed_so_far += 1

            # Обновляем прогресс на каждой странице.
            # При 250 стр. в файле это даёт плавный progress-bar на фронтенде.
            status.set_progress(
                processed_pages=processed_so_far,
                total_pages=total_pages,
                current_file=os.path.basename(pdf_path)
            )

        status.add_log(
            f"[Stage1] Завершена обработка {os.path.basename(pdf_path)}: "
            f"создано {len(png_paths)} PNG файлов"
        )

    return png_paths, processed_so_far


def run(pdf_dir: str, png_dir: str, status_file: str,
        file_names: list, dpi: int) -> dict:
    """
    Основная точка входа агента. Возвращает словарь с результатами
    обработки, используемый оркестратором (run_pipeline.py) для
    передачи данных агенту 2.
    """
    status = StatusManager(status_file)
    os.makedirs(png_dir, exist_ok=True)

    pdf_paths = [os.path.join(pdf_dir, name) for name in file_names]

    # Проверяем, что все файлы физически существуют
    missing_files = [p for p in pdf_paths if not os.path.isfile(p)]
    if missing_files:
        missing_names = ", ".join(os.path.basename(p) for p in missing_files)
        status.set_error(f"Файлы не найдены на диске: {missing_names}")
        raise FileNotFoundError(missing_names)

    status.add_log(f"[Stage1] Подсчёт общего количества страниц ({len(pdf_paths)} файлов)...")

    page_counts = count_total_pages(pdf_paths, status)
    total_pages = sum(page_counts.values())

    if total_pages == 0:
        status.set_error("Ни один PDF файл не содержит страниц или все файлы повреждены.")
        raise ValueError("total_pages is zero")

    status.update(
        stage=1,
        status="processing",
        total_pages=total_pages,
        processed_pages=0
    )
    status.add_log(f"[Stage1] Всего страниц к обработке: {total_pages}")

    # Карта: pdf_path -> [список путей к PNG] — понадобится агенту 2
    result_map = {}
    processed_so_far = 0

    for pdf_path in pdf_paths:
        try:
            png_paths, processed_so_far = convert_pdf_to_png(
                pdf_path=pdf_path,
                output_dir=png_dir,
                dpi=dpi,
                status=status,
                processed_so_far=processed_so_far,
                total_pages=total_pages
            )
            result_map[pdf_path] = png_paths

        except Exception as exc:
            error_message = f"Ошибка при обработке {os.path.basename(pdf_path)}: {exc}"
            status.add_log(f"[Stage1] {error_message}")
            status.set_error(error_message)
            raise

    status.add_log(
        f"[Stage1] Конвертация завершена. Всего создано "
        f"{sum(len(v) for v in result_map.values())} PNG файлов."
    )

    # Финализируем этап 1. stage остаётся равным 1 со status="done" —
    # это сигнал для JS на фронтенде подсветить карточку stage2 как активную.
    # Оркестратор (run_pipeline.py) сразу после этого переключит stage=2.
    status.update(
        stage=1,
        status="done",
        percent=100,
        processed_pages=total_pages,
        total_pages=total_pages
    )

    return result_map


def main():
    args = parse_arguments()
    file_names = [name.strip() for name in args.files.split(",") if name.strip()]

    status = StatusManager(args.status_file)

    try:
        start_time = time.time()
        run(
            pdf_dir=args.pdf_dir,
            png_dir=args.png_dir,
            status_file=args.status_file,
            file_names=file_names,
            dpi=args.dpi
        )
        elapsed = round(time.time() - start_time, 1)
        status.add_log(f"[Stage1] Общее время конвертации: {elapsed} сек.")

    except Exception as exc:
        # Логируем полный traceback в файл рядом со status.json для отладки,
        # но во внешний JSON выводим только краткое сообщение.
        error_log_path = os.path.join(
            os.path.dirname(args.status_file), "agent1_error.log"
        )
        with open(error_log_path, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())

        status.set_error(f"Критическая ошибка агента конвертации: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()