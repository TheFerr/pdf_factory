#!/usr/bin/env python3
"""
Агент 2: Поиск и распознавание Data Matrix кодов на PNG страницах,
экспорт результатов в текстовые файлы с последующим равномерным
разбиением на заданное количество результирующих файлов.

Алгоритм поиска кодов на странице:
    1. Бинаризация изображения (коды чёрные на белом фоне).
    2. Поиск контуров через cv2.findContours.
    3. Фильтрация контуров по геометрии (площадь, аспект-ratio ~ квадрат) —
       так отсекаются случайные артефакты и текстовые блоки, если такие есть.
    4. Кластеризация найденных прямоугольников в сетку строка/столбец
       (до 4 строк, до 5 столбцов) на основании коллинеарности границ.
    5. Сортировка кодов по порядку чтения: сверху-вниз, слева-направо.
    6. Crop каждого кода с небольшим отступом (padding) и распознавание
       через pylibdmtx.

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


# ---------- Константы конфигурации алгоритма поиска ----------

MAX_ROWS = 4
MAX_COLS = 5

# Ожидаемая доля площади одного кода относительно площади страницы.
# Используется как первичный фильтр для отсечения слишком мелких
# (шум/артефакты) или слишком крупных (случайно слипшиеся) контуров.
MIN_CODE_AREA_RATIO = 0.001
MAX_CODE_AREA_RATIO = 0.15

# Допустимое отклонение от квадратной формы рамки кода (аспект-ratio).
# Data Matrix почти всегда квадратный, но небольшая погрешность
# сканирования/скана допустима.
ASPECT_RATIO_TOLERANCE = 0.25

# Отступ (в пикселях) добавляемый при crop кода — небольшой запас
# помогает libdmtx корректнее находить finder pattern по краям.
CROP_PADDING = 6

# Порог группировки центров кодов в одну строку (по Y),
# рассчитывается динамически как доля от среднего размера кода,
# но имеет минимальное абсолютное значение на случай мелких кодов.
ROW_GROUPING_MIN_PX = 15

# Через сколько обработанных PNG обновлять status.json
STATUS_UPDATE_INTERVAL = 5


class DetectedCode:
    """Представляет один найденный на странице код с его геометрией."""

    __slots__ = ["x", "y", "w", "h", "center_x", "center_y", "image"]

    def __init__(self, x: int, y: int, w: int, h: int, image: np.ndarray):
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.center_x = x + w / 2
        self.center_y = y + h / 2
        self.image = image


def find_code_contours(gray_image: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """
    Находит прямоугольные контуры-кандидаты на роль рамок Data Matrix кодов.

    Возвращает список bounding box'ов в формате (x, y, w, h).
    """
    page_area = gray_image.shape[0] * gray_image.shape[1]
    min_area = page_area * MIN_CODE_AREA_RATIO
    max_area = page_area * MAX_CODE_AREA_RATIO

    # Бинаризация: коды чёрные на белом фоне, поэтому инвертируем,
    # чтобы рамки стали белыми объектами на чёрном фоне для findContours.
    _, binary = cv2.threshold(
        gray_image, 0, 255,
        cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )

    # Небольшая морфологическая операция закрытия — помогает соединить
    # разрывы в тонкой рамке кода, если скан не идеально чистый.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)

    contours, hierarchy = cv2.findContours(
        binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
    )

    candidates = []

    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area or area > max_area:
            continue

        x, y, w, h = cv2.boundingRect(contour)

        if h == 0:
            continue

        aspect_ratio = w / h
        if abs(aspect_ratio - 1.0) > ASPECT_RATIO_TOLERANCE:
            continue

        # Дополнительная проверка: контур должен быть достаточно
        # "прямоугольным" (площадь контура близка к площади bounding box),
        # это отсекает неровные пятна/шум.
        bbox_area = w * h
        if bbox_area == 0:
            continue

        fill_ratio = area / bbox_area
        if fill_ratio < 0.5:
            continue

        candidates.append((x, y, w, h))

    return candidates


def deduplicate_nested_boxes(
    boxes: List[Tuple[int, int, int, int]]
) -> List[Tuple[int, int, int, int]]:
    """
    Рамка кода на изображении часто даёт два вложенных контура
    (внешний и внутренний край линии рамки). Оставляем только
    внешний (больший) прямоугольник среди сильно перекрывающихся.
    """
    if not boxes:
        return []

    # Сортируем по площади по убыванию — сначала крупные (внешние) рамки
    boxes_sorted = sorted(boxes, key=lambda b: b[2] * b[3], reverse=True)
    kept = []

    def iou(box_a, box_b) -> float:
        ax, ay, aw, ah = box_a
        bx, by, bw, bh = box_b

        inter_x1 = max(ax, bx)
        inter_y1 = max(ay, by)
        inter_x2 = min(ax + aw, bx + bw)
        inter_y2 = min(ay + ah, by + bh)

        inter_w = max(0, inter_x2 - inter_x1)
        inter_h = max(0, inter_y2 - inter_y1)
        inter_area = inter_w * inter_h

        area_a = aw * ah
        area_b = bw * bh
        union_area = area_a + area_b - inter_area

        return inter_area / union_area if union_area > 0 else 0

    for box in boxes_sorted:
        is_duplicate = any(iou(box, kept_box) > 0.6 for kept_box in kept)
        if not is_duplicate:
            kept.append(box)

    return kept


def arrange_codes_in_grid(
    boxes: List[Tuple[int, int, int, int]]
) -> List[Tuple[int, int, int, int]]:
    """
    Кластеризует найденные прямоугольники в логическую сетку
    (строки/столбцы) и возвращает их в порядке чтения:
    сверху-вниз по строкам, слева-направо внутри строки.

    Опирается на коллинеарность рамок по вертикали/горизонтали,
    заявленную в исходных данных страницы.
    """
    if not boxes:
        return []

    # Средняя высота кода используется для расчёта допуска группировки в строки
    avg_height = sum(b[3] for b in boxes) / len(boxes)
    row_threshold = max(ROW_GROUPING_MIN_PX, avg_height * 0.5)

    # Сортируем все боксы по Y для последовательной кластеризации в строки
    boxes_by_y = sorted(boxes, key=lambda b: b[1])

    rows: List[List[Tuple[int, int, int, int]]] = []
    current_row = [boxes_by_y[0]]
    current_row_y = boxes_by_y[0][1]

    for box in boxes_by_y[1:]:
        if abs(box[1] - current_row_y) <= row_threshold:
            current_row.append(box)
        else:
            rows.append(current_row)
            current_row = [box]
            current_row_y = box[1]

    rows.append(current_row)

    # Ограничиваем количество строк заявленным максимумом (защита от шума)
    rows = rows[:MAX_ROWS]

    # Внутри каждой строки сортируем по X (порядок столбцов слева-направо)
    ordered_boxes = []
    for row in rows:
        row_sorted = sorted(row, key=lambda b: b[0])[:MAX_COLS]
        ordered_boxes.extend(row_sorted)

    return ordered_boxes


def crop_code_image(
    gray_image: np.ndarray, box: Tuple[int, int, int, int]
) -> np.ndarray:
    """Вырезает область кода с небольшим padding для надёжности декодирования."""
    x, y, w, h = box
    img_h, img_w = gray_image.shape[:2]

    x1 = max(0, x - CROP_PADDING)
    y1 = max(0, y - CROP_PADDING)
    x2 = min(img_w, x + w + CROP_PADDING)
    y2 = min(img_h, y + h + CROP_PADDING)

    return gray_image[y1:y2, x1:x2]


def decode_data_matrix(cropped_image: np.ndarray) -> Optional[str]:
    """
    Распознаёт Data Matrix код из вырезанного изображения через pylibdmtx.

    Возвращает распознанную строку либо None, если код не распознан.
    """
    try:
        # timeout защищает от редких случаев зависания libdmtx
        # на сильно зашумлённых/повреждённых изображениях
        results = dmtx_decode(cropped_image, timeout=2000, max_count=1)

        if results:
            return results[0].data.decode("utf-8", errors="replace").strip()

    except Exception:
        # Ошибка декодирования конкретного кода не должна прерывать
        # обработку всей страницы — просто фиксируем как нераспознанный
        pass

    return None


def process_single_page(
    png_path: str, status: StatusManager
) -> List[str]:
    """
    Обрабатывает одну PNG страницу: находит все коды, распознаёт их,
    возвращает список распознанных строк в порядке чтения (сетка).

    Нераспознанные коды помечаются как "UNREADABLE" — это позволяет
    сохранить позиционную структуру результата и не терять информацию
    о том, что на этом месте код присутствовал, но не считался.
    """
    image = cv2.imread(png_path, cv2.IMREAD_GRAYSCALE)

    if image is None:
        status.add_log(f"[Stage2] ОШИБКА: не удалось открыть {os.path.basename(png_path)}")
        return []

    raw_boxes = find_code_contours(image)
    deduped_boxes = deduplicate_nested_boxes(raw_boxes)
    ordered_boxes = arrange_codes_in_grid(deduped_boxes)

    decoded_values = []

    for box in ordered_boxes:
        cropped = crop_code_image(image, box)
        decoded_text = decode_data_matrix(cropped)

        if decoded_text:
            decoded_values.append(decoded_text)
        else:
            decoded_values.append("UNREADABLE")

    return decoded_values


def collect_png_files(png_dir: str) -> List[str]:
    """
    Собирает полные пути всех PNG страниц из всех подпапок
    (каждая подпапка соответствует одному исходному PDF),
    в порядке: сортировка по имени папки, затем по имени файла
    (нумерация страниц с ведущими нулями обеспечивает верный порядок).
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

    Открываем файл в бинарном режиме ('wb'), чтобы избежать автоматической
    трансляции переводов строк операционной системой (например, Windows
    иначе продублировал бы \\r при записи текстового '\\n' в текстовом режиме).
    """
    with open(output_path, "wb") as f:
        for line in lines:
            # Гарантируем чистый ASCII: заменяем не-ASCII символы на '?'
            ascii_line = line.encode("ascii", errors="replace").decode("ascii")
            f.write(ascii_line.encode("ascii"))
            f.write(b"\r\n")


def split_into_result_files(
    all_lines: List[str], result_count: int, csv_dir: str
) -> List[str]:
    """
    Делит общий список строк на result_count файлов с примерно
    равным количеством строк на файл.

    Имена результирующих файлов фиксированы по шаблону result_N.csv,
    как того требует контракт с фронтендом (PHP формирует ссылки
    на скачивание по этому же шаблону имён).

    Возвращает список имён созданных файлов (без пути).
    """
    total_lines = len(all_lines)

    if total_lines == 0:
        return []

    # Не создаём больше файлов, чем есть строк
    effective_result_count = min(result_count, total_lines)

    base_lines_per_file = total_lines // effective_result_count
    remainder = total_lines % effective_result_count

    created_files = []
    current_index = 0

    for file_number in range(1, effective_result_count + 1):
        # Распределяем остаток по первым файлам, чтобы разница
        # в количестве строк между файлами была не более 1 строки
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

        unreadable_count += sum(1 for line in page_lines if line == "UNREADABLE")

        # Обновление status.json каждые N файлов, а не на каждой странице —
        # снижает нагрузку на диск при 250-страничных PDF с большим количеством
        # исходных файлов (в отличие от Агента 1, где мы обновляли на каждой
        # странице ради плавности бара — здесь операция дороже по CPU,
        # и обновление каждые 5 файлов даёт достаточную детализацию
        # без избыточной нагрузки на I/O).
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
    parser = argparse.ArgumentParser(description="Data Matrix OCR agent")
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