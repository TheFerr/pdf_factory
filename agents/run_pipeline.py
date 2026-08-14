#!/usr/bin/env python3
"""
Оркестратор пайплайна обработки: последовательно запускает
Агент 1 (PDF -> PNG) и Агент 2 (PNG -> распознавание -> CSV).
"""

import argparse
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from status_manager import StatusManager
import agent1_pdf_to_png
import agent2_ocr_to_csv


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PDF processing pipeline orchestrator")
    parser.add_argument("--job_dir", required=True)
    parser.add_argument("--pdf_dir", required=True)
    parser.add_argument("--png_dir", required=True)
    parser.add_argument("--csv_dir", required=True)
    parser.add_argument("--status_file", required=True)
    parser.add_argument("--result_count", type=int, required=True)
    parser.add_argument("--files", required=True)
    parser.add_argument("--dpi", type=int, default=200)
    return parser.parse_args()


def main():
    args = parse_arguments()
    status = StatusManager(args.status_file)
    file_names = [name.strip() for name in args.files.split(",") if name.strip()]

    try:
        # ---------- Этап 1: конвертация PDF -> PNG ----------
        agent1_pdf_to_png.run(
            pdf_dir=args.pdf_dir,
            png_dir=args.png_dir,
            status_file=args.status_file,
            file_names=file_names,
            dpi=args.dpi
        )

        # ---------- Переключение на этап 2 ----------
        status.update(
            stage=2,
            status="processing",
            percent=0,
            processed_pages=0,
            total_pages=None,
            current_file=None
        )
        status.add_log("[Stage2] Инициализация распознавания кодов...")

        # ---------- Этап 2: распознавание -> CSV ----------
        agent2_ocr_to_csv.run(
            png_dir=args.png_dir,
            csv_dir=args.csv_dir,
            status_file=args.status_file,
            result_count=args.result_count
        )

    except Exception:
        error_log_path = os.path.join(args.job_dir, "pipeline_error.log")
        with open(error_log_path, "w", encoding="utf-8") as f:
            f.write(traceback.format_exc())

        status.set_error("Критическая ошибка в пайплайне обработки. См. pipeline_error.log")
        sys.exit(1)


if __name__ == "__main__":
    main()