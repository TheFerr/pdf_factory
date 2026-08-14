#!/usr/bin/env python3
"""
Агент 2: Поиск и распознавание Data Matrix кодов на PNG страницах
через анализ проекций (projection profile) линий сетки.

Метод расчитан на синтезированные PDF с чёткими прямыми чёрными
линиями рамок на белом фоне (не сканы) — поиск сетки выполняется
через суммирование тёмных пикселей по строкам/столбцам, что даёт
высокую надёжность и скорость по сравнению с контурным анализом.

Алгоритм на странице:
    1. Бинаризация (тёмные элементы становятся "1").
    2. Вертикальная проекция -> находим X-координаты вертикальных линий сетки.
    3. Горизонтальная проекция -> находим Y-координаты горизонтальных линий сетки.
    4. Ячейки сетки = пересечения найденных линий (rows x cols, до 4 x 5).
    5. Для каждой ячейки: crop с отступом от линии, декодирование
       Data Matrix с каскадом fallback-стратегий (raw -> gray ->
       upscale -> Otsu threshold).

Каждые 5 обработанных PNG агент обновляет status.json.

По завершении распознавания всех страниц всех файлов, агент формирует
единый ASCII-файл (CRLF на конце каждой строки) и делит его на
result_count частей с примерно равным количеством строк.

Использование (обычно вызывается из run_pipeline.py):
    python3 agent2_ocr_to_csv.py \
        --png_dir /path/to/png \
        --csv_dir /path/to/csv \
        --status_file /path/to/status.json \
        --result_count 3
"""

import argparse
import os
import sys
import time
import traceback
from typing import List, Tuple, Optional

import cv2
import numpy as np
from pylibdmtx.pylibdmtx import decode as dmtx_decode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from status_manager import StatusManager


# ---------- Константы конфигурации алгоритма поиска сетки ----------

MAX_ROWS = 4
MAX_COLS = 5

# Порог бинаризации: пиксели темнее этого значения считаются
# частью линии сетки / кода. Для синтезированных чёрно-белых PDF
# со строгими границами 220 — надёжное значение (как в эталонном коде).
BINARY_THRESHOLD = 220

# Доля высоты/ширины страницы, которую должна перекрывать линия,
# чтобы считаться частью сетки (а не, например, штрихом внутри кода).
VERTICAL_LINE_RATIO = 0.60
HORIZONTAL_LINE_RATIO = 0.60

# Максимальный разрыв между соседними "тёмными" координатами
# при группировке в одну линию (учитывает антиалиасинг/толщину линии).
LINE_GROUPING_MAX_GAP = 5

# Отступ от найденной линии сетки внутрь ячейки перед crop —
# исключает саму линию рамки из вырезаемой области кода.
CELL_MARGIN = 3

# Через сколько обработанных PNG обновлять status.json.
# Декодирование Data Matrix — CPU-затратная операция (до 20 кодов
# на страницу с возможным каскадом из 4 попыток каждый),
# поэтому обновляем статус не на каждой странице, а пакетно.
STATUS_UPDATE_INTERVAL = 5

# Метка для нераспознанного кода — сохраняет позиционную
# целостность результата (ячейка физически была, но не считалась).
UNREADABLE_MARK = "ERROR"


def group_lines(points: np.ndarray, max_gap: int = LINE_GROUPING_MAX_GAP) -> List[int]:
    """
    Объединяет соседние координаты тёмных пикселей в единые линии сетки.

    Например, [206, 207, 208, 209, 210] (толщина линии в несколько
    пикселей из-за антиалиасинга) схлопывается в единую координату
    центра линии: 208.

    Возвращает список центров найденных линий.
    """
    if len(points) == 0:
        return []

    groups = []
    start = points[0]
    previous = points[0]

    for point in points[1:]:
        if point - previous <= max_gap:
            previous = point
        else:
            groups.append((start, previous))
            start = point
            previous = point

    groups.append((start, previous))

    return [(g_start + g_end) // 2 for g_start, g_end in groups]


def find_grid_lines(
    gray_image: np.ndarray
) -> Tuple[List[int], List[int]]:
    """
    Находит координаты вертикальных и горизонтальных линий сетки
    через анализ проекций тёмных пикселей.

    Принимает изображение в оттенках серого (не BGR) — конвертация
    из cv2.imread выполняется один раз при загрузке страницы,
    а не повторно на каждом вызове этой функции.

    Возвращает (vertical_line_positions, horizontal_line_positions).
    """
    _, binary = cv2.threshold(
        gray_image, BINARY_THRESHOLD, 255, cv2.THRESH_BINARY_INV
    )

    height, width = binary.shape

    vertical_projection = np.sum(binary > 0, axis=0)
    horizontal_projection = np.sum(binary > 0, axis=1)

    vertical_threshold = height * VERTICAL_LINE_RATIO
    horizontal_threshold = width * HORIZONTAL_LINE_RATIO

    vertical_candidates = np.where(vertical_projection > vertical_threshold)[0]
    horizontal_candidates = np.where(horizontal_projection > horizontal_threshold)[0]

    vertical_lines = group_lines(vertical_candidates)
    horizontal_lines = group_lines(horizontal_candidates)

    return vertical_lines, horizontal_lines


def decode_datamatrix(cell_bgr: np.ndarray) -> Optional[bytes]:
    """
    Распознаёт Data Matrix код из вырезанной ячейки с каскадом
    fallback-стратегий возрастающей "агрессивности" обработки.

    Порядок попыток (от самой быстрой к самой тяжёлой):
        1. Исходное BGR изображение как есть.
        2. Grayscale версия.
        3. Grayscale с увеличением в 2 раза (кубическая интерполяция) —
           помогает при мелких кодах, где размер модуля близок к 1 пикселю.
        4. Бинаризация по Otsu поверх увеличенного изображения —
           последний шанс для контрастных, но зашумлённых ячеек.

    Возвращает сырые байты декодированных данных либо None.
    """
    result = dmtx_decode(cell_bgr, max_count=1)
    if result:
        return result[0].data

    gray = cv2.cvtColor(cell_bgr, cv2.COLOR_BGR2GRAY)
    result = dmtx_decode(gray, max_count=1)
    if result:
        return result[0].data

    enlarged = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    result = dmtx_decode(enlarged, max_count=1)
    if result:
        return result[0].data

    _, binary = cv2.threshold(enlarged, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    result = dmtx_decode(binary, max_count=1)
    if result:
        return result[0].data

    return None


def process_single_page(png_path: str, status: StatusManager) -> List[str]:
    """
    Обрабатывает одну PNG страницу: находит сетку линий, вырезает
    каждую ячейку и распознаёт код внутри неё.

    Возвращает список распознанных строк в порядке чтения:
    сверху-вниз по строкам, слева-направо внутри строки —
    это естественный порядок, так как ячейки перебираются
    напрямую по индексам найденной сетки (rows x cols).
    """
    image = cv2.imread(png_path)  # BGR, как в эталонном алгоритме

    if image is None:
        status.add_log(f"[Stage2] ОШИБКА: не удалось открыть {os.path.basename(png_path)}")
        return []

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    vertical_lines, horizontal_lines = find_grid_lines(gray)

    cols = len(vertical_lines) - 1
    rows = len(horizontal_lines) - 1

    if rows <= 0 or cols <= 0:
        status.add_log(
            f"[Stage2] ПРЕДУПРЕЖДЕНИЕ: сетка не найдена на {os.path.basename(png_path)}"
        )
        return []

    # Защита от аномального результата проекции (шум дал лишние линии) —
    # обрезаем по заявленным максимумам сетки.
    rows = min(rows, MAX_ROWS)
    cols = min(cols, MAX_COLS)

    decoded_values = []

    for row in range(rows):
        y1 = horizontal_lines[row] + CELL_MARGIN
        y2 = horizontal_lines[row + 1] - CELL_MARGIN

        for col in range(cols):
            x1 = vertical_lines[col] + CELL_MARGIN
            x2 = vertical_lines[col + 1] - CELL_MARGIN

            if x2 <= x1 or y2 <= y1:
                decoded_values.append(UNREADABLE_MARK)
                continue

            cell = image[y1:y2, x1:x2]
            data = decode_datamatrix(cell)

            if data is None:
                decoded_values.append(UNREADABLE_MARK)
            else:
                decoded_values.append(data.decode("ascii", errors="replace"))

    return decoded_values


def collect_png_files(png_dir: str) -> List[str]:
    """
    Собирает полные пути всех PNG страниц из всех подпапок
    (каждая подпапка соответствует одному исходному PDF),
    в порядке: сортировка по имени папки, затем по имени файла.
    Нумерация страниц с ведущими нулями (из Агента 1) обеспечивает
    корректный порядок сортировки.
    """
    all_png_paths = []

    for subdir in sorted(os.listdir(png_dir)):
        subdir_path = os.path.join(png_dir, subdir)
        if not os.path.isdir(subdir_path):
            continue

        page_files = sorted(
            f for f in os.listdir(subdir_path) if f.lower().endswith(".png")
        )

        for page_file in page_files:
            all_png_paths.append(os.path.join(subdir_path, page_file))

    return all_png_paths


def write_ascii_lines(lines: List[str], output_path: str) -> None:
    """
    Записывает строки в ASCII файл с явным CRLF на конце каждой строки.

    Используется бинарный режим записи для гарантии одинакового
    результата независимо от ОС, на которой развёрнут сервер
    (текстовый режим на разных платформах по-разному транслирует \\n).
    """
    with open(output_path, "wb") as f:
        for line in lines:
            ascii_line = line.encode("ascii", errors="replace").decode("ascii")
            f.write(ascii_line.encode("ascii"))
            f.write(b"\r\n")


def split_into_result_files(
    all_lines: List[str], result_count: int, csv_dir: str
) -> List[str]:
    """
    Делит общий список строк на result_count файлов с примерно
    равным количеством строк на файл. Остаток распределяется
    по первым файлам (разница между файлами не более 1 строки).

    Имена результирующих файлов фиксированы по шаблону result_N.csv.
    """
    total_lines = len(all_lines)

    if total_lines == 0:
        return []

    effective_result_count = min(result_count, total_lines)

    base_lines_per_file = total_lines // effective_result_count
    remainder = total_lines % effective_result_count

    created_files = []
    current_index = 0

    for file_number in range(1, effective_result_count + 1):
        lines_in_this_file = base_lines_per_file + (1 if file_number <= remainder else 0)

        chunk = all_lines[current_index:current_index + lines_in_this_file]
        current_index += lines_in_this_file

        file_name = f"result_{file_number}.csv"
        output_path = os.path.join(csv_dir, file_name)

        write_ascii_lines(chunk, output_path)
        created_files.append(file_name)

    return created_files


def run(png_dir: str, csv_dir: str, status_file: str, result_count: int) -> List[str]:
    """
    Основная точка входа агента 2. Возвращает список имён
    созданных результирующих CSV файлов.
    """
    status = StatusManager(status_file)
    os.makedirs(csv_dir, exist_ok=True)

    status.add_log("[Stage2] Сбор списка PNG страниц для распознавания...")
    png_files = collect_png_files(png_dir)
    total_pages = len(png_files)

    if total_pages == 0:
        status.set_error("Не найдено PNG файлов для распознавания. Проверьте результат этапа 1.")
        raise ValueError("no PNG files found")

    status.update(
        stage=2,
        status="processing",
        total_pages=total_pages,
        processed_pages=0,
        percent=0
    )
    status.add_log(f"[Stage2] Найдено {total_pages} страниц для распознавания.")

    all_decoded_lines: List[str] = []
    unreadable_count = 0

    for index, png_path in enumerate(png_files, start=1):
        page_lines = process_single_page(png_path, status)
        all_decoded_lines.extend(page_lines)

        unreadable_count += sum(1 for line in page_lines if line == UNREADABLE_MARK)

        if index % STATUS_UPDATE_INTERVAL == 0 or index == total_pages:
            status.set_progress(
                processed_pages=index,
                total_pages=total_pages,
                current_file=os.path.basename(os.path.dirname(png_path))
            )
            status.add_log(
                f"[Stage2] Распознано страниц: {index}/{total_pages} "
                f"(найдено кодов: {len(all_decoded_lines)}, "
                f"нераспознано: {unreadable_count})"
            )

    status.add_log(
        f"[Stage2] Распознавание завершено. Всего кодов: {len(all_decoded_lines)}, "
        f"нераспознанных: {unreadable_count}."
    )

    status.add_log(f"[Stage2] Формирование {result_count} результирующих файлов...")
    result_files = split_into_result_files(all_decoded_lines, result_count, csv_dir)

    status.add_log(
        f"[Stage2] Готово: создано {len(result_files)} файлов "
        f"({', '.join(result_files)})."
    )

    status.update(
        stage=2,
        status="done",
        percent=100,
        processed_pages=total_pages,
        total_pages=total_pages,
        result_files=result_files
    )

    return result_files


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Data Matrix OCR agent (projection-based grid detection)")
    parser.add_argument("--png_dir", required=True, help="Директория с PNG страницами (из Агента 1)")
    parser.add_argument("--csv_dir", required=True, help="Директория для сохранения результирующих файлов")
    parser.add_argument("--status_file", required=True, help="Путь к status.json")
    parser.add_argument("--result_count", type=int, required=True,
                         help="Количество результирующих файлов")
    return parser.parse_args()


def main():
    args = parse_arguments()
    status = StatusManager(args.status_file)

    try:
        start_time = time.time()
        run(
            png_dir=args.png_dir,
            csv_dir=args.csv_dir,
            status_file=args.status_file,
            result_count=args.result_count
        )
        elapsed = round(time.time() - start_time, 1)
        status.add_log(f"[Stage2] Общее время распознавания: {elapsed} сек.")

    except Exception as exc:
        error_log_path = os.path.join(
            os.path.dirname(args.status_file), "agent2_error.log"
        )
        with open(error_log_path, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())

        status.set_error(f"Критическая ошибка агента распознавания: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()