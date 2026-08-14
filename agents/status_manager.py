"""
Модуль управления файлом статуса обработки (status.json).
Обеспечивает потокобезопасную атомарную запись, читаемую PHP-скриптом
в реальном времени через polling.
"""

import json
import os
import tempfile
from datetime import datetime
from threading import Lock


class StatusManager:
    """
    Управляет чтением/записью JSON-файла статуса обработки.
    Запись выполняется атомарно (через временный файл + os.replace),
    чтобы избежать чтения частично записанного JSON внешним процессом.
    """

    def __init__(self, status_file_path: str):
        self.status_file_path = status_file_path
        self._lock = Lock()

        # Гарантируем существование директории
        os.makedirs(os.path.dirname(status_file_path), exist_ok=True)

        # Если файла ещё нет — создаём базовую структуру
        if not os.path.exists(status_file_path):
            self._write(self._default_state())

    @staticmethod
    def _default_state() -> dict:
        return {
            "stage": 1,
            "status": "processing",       # processing | done | error
            "percent": 0,
            "current_file": None,
            "processed_pages": 0,
            "total_pages": None,
            "log": [],
            "result_files": [],
            "error": None,
            "updated_at": datetime.utcnow().isoformat()
        }

    def read(self) -> dict:
        with self._lock:
            try:
                with open(self.status_file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                return self._default_state()

    def _write(self, data: dict) -> None:
        """Атомарная запись через временный файл в той же директории."""
        data["updated_at"] = datetime.utcnow().isoformat()
        dir_name = os.path.dirname(self.status_file_path)

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=dir_name,
            delete=False, suffix=".tmp"
        ) as tmp_file:
            json.dump(data, tmp_file, ensure_ascii=False)
            tmp_path = tmp_file.name

        os.replace(tmp_path, self.status_file_path)

    def update(self, **kwargs) -> dict:
        """
        Частичное обновление статуса. Поддерживает append-логику для log
        через отдельный метод add_log(), здесь — только прямая замена полей.
        """
        with self._lock:
            current = self._read_unlocked()
            current.update(kwargs)
            self._write(current)
            return current

    def _read_unlocked(self) -> dict:
        try:
            with open(self.status_file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            return self._default_state()

    def add_log(self, message: str, max_lines: int = 200) -> None:
        """
        Добавляет строку в лог с ограничением на максимальное количество
        строк (чтобы JSON не разрастался на 250-страничных PDF).
        """
        with self._lock:
            current = self._read_unlocked()
            log_list = current.get("log", [])
            log_list.append(message)

            # Ограничиваем размер лога, оставляя последние N строк
            if len(log_list) > max_lines:
                log_list = log_list[-max_lines:]

            current["log"] = log_list
            self._write(current)

    def set_error(self, message: str) -> None:
        with self._lock:
            current = self._read_unlocked()
            current["status"] = "error"
            current["error"] = message
            self._write(current)

    def set_progress(self, processed_pages: int, total_pages: int,
                      current_file: str = None) -> None:
        """Комплексное обновление прогресса с автоматическим расчётом percent."""
        with self._lock:
            current = self._read_unlocked()
            percent = 0
            if total_pages and total_pages > 0:
                percent = min(100, round((processed_pages / total_pages) * 100))

            current["processed_pages"] = processed_pages
            current["total_pages"] = total_pages
            current["percent"] = percent
            if current_file is not None:
                current["current_file"] = current_file

            self._write(current)