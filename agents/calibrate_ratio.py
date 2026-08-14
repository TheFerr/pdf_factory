#!/usr/bin/env python3
"""
calibrate_ratio.py — вспомогательный скрипт для подбора CODE_HEIGHT_RATIO.

Вырезает первую ячейку сетки эталонного PNG и сохраняет результат
crop с текущим значением ratio в отдельный файл для визуальной проверки —
код должен попасть в кадр полностью, а текст подписи не попасть.

Использование:
    python3 calibrate_ratio.py sample_page.png 0.78
"""

import sys
import cv2

sys.path.insert(0, "agents")
from agent2_ocr_to_csv import find_grid_lines, CELL_MARGIN

def main():
    png_path = sys.argv[1]
    ratio = float(sys.argv[2]) if len(sys.argv) > 2 else 0.78

    image = cv2.imread(png_path)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    v_lines, h_lines = find_grid_lines(gray)

    print(f"Вертикальные линии: {v_lines}")
    print(f"Горизонтальные линии: {h_lines}")

    # Берём первую ячейку (row=0, col=0)
    y1, y2 = h_lines[0] + CELL_MARGIN, h_lines[1] - CELL_MARGIN
    x1, x2 = v_lines[0] + CELL_MARGIN, v_lines[1] - CELL_MARGIN

    cell = image[y1:y2, x1:x2]
    cell_h, cell_w = cell.shape[:2]

    code_h = int(cell_h * ratio)
    code_side = min(cell_w, code_h)

    crop = cell[0:code_side, 0:code_side]

    cv2.imwrite("calibration_cell_full.png", cell)
    cv2.imwrite("calibration_code_crop.png", crop)

    print(f"Ячейка: {cell_w}x{cell_h}")
    print(f"Вычисленная зона кода: {code_side}x{code_side} (ratio={ratio})")
    print("Сохранено: calibration_cell_full.png, calibration_code_crop.png")
    print("Проверьте визуально: в calibration_code_crop.png должен быть виден")
    print("только сам DataMatrix, без текста подписи снизу.")


if __name__ == "__main__":
    main()