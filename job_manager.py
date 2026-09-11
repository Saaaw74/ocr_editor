# -*- coding: utf-8 -*-
"""
job_manager.py
Единый потокобезопасный менеджер задач с поддержкой TTL-кэша и вытеснения на диск.
Лимит по умолчанию: 5 файлов и 1 час (3600 сек) хранения.
"""

import os
import uuid
import logging
import threading
import tempfile
import time
from typing import Optional, Dict, Any

try:
    from cachetools import TTLCache
except ImportError:
    TTLCache = None

logger = logging.getLogger("job_manager")

# Каталог для временных PDF-файлов активных сессий
TEMP_JOBS_DIR = os.path.join(tempfile.gettempdir(), "bentopdf_jobs")
os.makedirs(TEMP_JOBS_DIR, exist_ok=True)

JOB_MAX_COUNT = int(os.getenv("JOB_MAX_COUNT", "5"))
JOB_TTL_SEC = int(os.getenv("JOB_TTL_SEC", "3600"))


class Job:
    """
    Класс сессии документа.
    Сохраняет PDF на диск во временный файл и предоставляет интерфейс словаря
    для полной обратной совместимости с существующим кодом.
    """

    def __init__(self, job_id: str, filename: str, pdf_bytes: bytes, page_count: int, mode: str = "native"):
        self.job_id = job_id
        self.filename = filename
        self.page_count = page_count
        self.mode = mode
        self.created_at = time.time()
        self.progress: Dict[str, Any] = {"percent": 0, "step": "uploaded"}
        self.pages: Dict[int, Any] = {}

        self.temp_pdf_path = os.path.join(TEMP_JOBS_DIR, f"{self.job_id}.pdf")
        with open(self.temp_pdf_path, "wb") as f:
            f.write(pdf_bytes)

    @property
    def pdf_bytes(self) -> bytes:
        """Ленивое чтение файла с диска при необходимости."""
        if os.path.exists(self.temp_pdf_path):
            with open(self.temp_pdf_path, "rb") as f:
                return f.read()
        return b""

    def set_progress(self, percent: int, step: str) -> None:
        self.progress = {"percent": percent, "step": step}

    def cleanup(self) -> None:
        """Удаляет временный файл сессии с диска."""
        if os.path.exists(self.temp_pdf_path):
            try:
                os.remove(self.temp_pdf_path)
                logger.info(f"[JOB_CLEANUP] Удален временный файл задачи: {self.job_id}")
            except OSError as e:
                logger.warning(f"[JOB_CLEANUP] Ошибка удаления {self.temp_pdf_path}: {e}")

    # --- Интерфейс совместимости с dict ---
    def __getitem__(self, key: str):
        if key == "pdf_bytes":
            return self.pdf_bytes
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any):
        setattr(self, key, value)

    def get(self, key: str, default=None):
        if key == "pdf_bytes":
            return self.pdf_bytes
        return getattr(self, key, default)

    def __contains__(self, key: str):
        return hasattr(self, key)


class JobManager:
    """Синглтон-менеджер задач с защитой от гонок потоков и авто-очисткой."""

    def __init__(self, maxsize: int = JOB_MAX_COUNT, ttl: int = JOB_TTL_SEC):
        self._lock = threading.Lock()
        self._maxsize = maxsize
        self._ttl = ttl

        if TTLCache is not None:
            class CleanableTTLCache(TTLCache):
                def popitem(self):
                    key, job = super().popitem()
                    if isinstance(job, Job):
                        job.cleanup()
                    return key, job

            self._cache = CleanableTTLCache(maxsize=maxsize, ttl=ttl)
        else:
            logger.warning("[JOB_MANAGER] cachetools не установлен, используется простой словарь")
            self._cache = {}

    def _purge_expired_fallback(self) -> None:
        """Очистка по таймауту для режима без cachetools."""
        now = time.time()
        expired = [jid for jid, j in self._cache.items() if (now - j.created_at) > self._ttl]
        for jid in expired:
            job = self._cache.pop(jid, None)
            if job:
                job.cleanup()

    def create_job(self, filename: str, pdf_bytes: bytes, page_count: int, mode: str = "native") -> Job:
        job_id = uuid.uuid4().hex[:12]
        job = Job(job_id=job_id, filename=filename, pdf_bytes=pdf_bytes, page_count=page_count, mode=mode)
        with self._lock:
            if TTLCache is None:
                self._purge_expired_fallback()
                if len(self._cache) >= self._maxsize:
                    oldest_id, oldest_job = next(iter(self._cache.items()))
                    oldest_job.cleanup()
                    del self._cache[oldest_id]

            self._cache[job_id] = job
        logger.info(f"[JOB_MANAGER] Создана задача {job_id} ({mode}, {page_count} стр.). Всего в памяти: {len(self._cache)}/5")
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            if TTLCache is None:
                self._purge_expired_fallback()
            try:
                return self._cache.get(job_id)
            except KeyError:
                return None

    def delete_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._cache.pop(job_id, None)
            if job:
                job.cleanup()
                logger.info(f"[JOB_MANAGER] Вручную удалена задача {job_id}")
                return True
        return False


# Глобальный экземпляр для всех маршрутов
job_manager = JobManager()