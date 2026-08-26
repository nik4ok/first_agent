"""Журнал реально отправленных откликов — источник правды для статистики.

vacancies.xlsx может врать (APPLIED без подтверждения HH). Сюда пишем только
после success Playwright/API или already_applied. JSON — канон, Excel — для просмотра.
"""
from __future__ import annotations

import fcntl
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from src.config import settings

logger = logging.getLogger(__name__)

_THREAD_LOCK = threading.Lock()


class SentApplicationsLog:
    COLUMNS = [
        "sent_at",
        "vacancy_id",
        "title",
        "employer",
        "city",
        "url",
        "match_score",
        "method",
        "already_applied",
        "cover_letter",
    ]

    COLUMN_NAMES_RU = {
        "sent_at": "Дата отправки",
        "vacancy_id": "ID Вакансии",
        "title": "Название вакансии",
        "employer": "Компания",
        "city": "Город",
        "url": "Ссылка",
        "match_score": "Score (%)",
        "method": "Канал",
        "already_applied": "Уже был на HH",
        "cover_letter": "Сопроводительное письмо",
    }

    def __init__(
        self,
        json_path: Optional[Path] = None,
        xlsx_path: Optional[Path] = None,
    ):
        self.json_path = json_path or settings.SENT_APPLICATIONS_JSON
        self.xlsx_path = xlsx_path or settings.SENT_APPLICATIONS_XLSX
        self.lock_path = self.json_path.parent / ".sent_applications.lock"

    def list_all(self) -> List[Dict[str, Any]]:
        with _THREAD_LOCK:
            with open(self.lock_path, "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    return self._load_unlocked()
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def count(self) -> int:
        return len(self.list_all())

    def record(self, payload: Dict[str, Any]) -> bool:
        """Добавляет отклик, если этого vacancy_id ещё нет. True = новая запись."""
        vacancy_id = str(payload.get("vacancy_id") or payload.get("id") or "").strip()
        if not vacancy_id:
            return False
        row = {
            "sent_at": payload.get("sent_at") or datetime.now(timezone.utc).isoformat(),
            "vacancy_id": vacancy_id,
            "title": str(payload.get("title") or ""),
            "employer": str(payload.get("employer") or ""),
            "city": str(payload.get("city") or ""),
            "url": str(payload.get("url") or f"https://hh.ru/vacancy/{vacancy_id}"),
            "match_score": payload.get("match_score"),
            "method": str(payload.get("method") or "browser"),
            "already_applied": bool(payload.get("already_applied")),
            "cover_letter": str(payload.get("cover_letter") or payload.get("letter") or ""),
        }
        with _THREAD_LOCK:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    items = self._load_unlocked()
                    if any(str(x.get("vacancy_id")) == vacancy_id for x in items):
                        return False
                    items.append(row)
                    self._save_unlocked(items)
                    logger.info("Журнал откликов: +%s %s", vacancy_id, row["title"])
                    return True
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def record_from_excel_row(
        self,
        vacancy_id: str,
        *,
        method: str = "browser",
        already_applied: bool = False,
        cover_letter: str = "",
    ) -> bool:
        from src.parser.excel_storage import ExcelStorage

        df = ExcelStorage().load_all()
        if df.empty:
            return self.record(
                {
                    "vacancy_id": vacancy_id,
                    "method": method,
                    "already_applied": already_applied,
                    "cover_letter": cover_letter,
                }
            )
        id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
        row = df[df[id_col].astype(str) == str(vacancy_id)]
        meta: Dict[str, Any] = {"vacancy_id": vacancy_id, "method": method, "already_applied": already_applied}
        if not row.empty:
            rec = row.iloc[0]
            meta.update(
                {
                    "title": rec.get("Название вакансии") or rec.get("title") or "",
                    "employer": rec.get("Компания") or rec.get("employer") or "",
                    "city": rec.get("Город") or rec.get("city") or "",
                    "url": rec.get("Ссылка") or rec.get("url") or "",
                    "match_score": rec.get("Score (%)") or rec.get("match_score"),
                    "cover_letter": cover_letter
                    or rec.get("Сопроводительное письмо")
                    or rec.get("cover_letter")
                    or "",
                }
            )
        else:
            meta["cover_letter"] = cover_letter
        return self.record(meta)

    def backfill_from_vacancies_excel(self) -> int:
        """Подтягивает уже помеченные APPLIED из vacancies.xlsx в журнал."""
        from src.parser.excel_storage import ExcelStorage

        df = ExcelStorage().load_all()
        if df.empty:
            return 0
        status_col = "Статус" if "Статус" in df.columns else "status"
        id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
        added = 0
        for _, rec in df[df[status_col] == "APPLIED"].iterrows():
            if self.record_from_excel_row(str(rec[id_col]), method="excel_backfill"):
                added += 1
        return added

    def backfill_from_autopilot_state(self) -> int:
        """Успехи прошлого автопилота, даже если Excel ещё не обновился."""
        state_path = settings.DATA_DIR / "autopilot_state.json"
        if not state_path.exists():
            return 0
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Не удалось прочитать autopilot_state.json: %s", exc)
            return 0
        added = 0
        for item in data.get("applied") or []:
            if not isinstance(item, dict):
                continue
            vacancy_id = str(item.get("id") or item.get("vacancy_id") or "").strip()
            if vacancy_id and self.record_from_excel_row(vacancy_id, method="autopilot_backfill"):
                added += 1
        return added

    def seed_from_known_sources(self) -> int:
        """Одноразово поднимает историю в журнал при старте дашборда."""
        return self.backfill_from_vacancies_excel() + self.backfill_from_autopilot_state()

    def ensure_xlsx(self) -> Path:
        items = self.list_all()
        with _THREAD_LOCK:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a+", encoding="utf-8") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    self._save_unlocked(items)
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return self.xlsx_path

    def _load_unlocked(self) -> List[Dict[str, Any]]:
        if not self.json_path.exists():
            return []
        try:
            data = json.loads(self.json_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as exc:
            logger.warning("Не удалось прочитать журнал откликов: %s", exc)
        return []

    def _save_unlocked(self, items: List[Dict[str, Any]]) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        self.json_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        df = pd.DataFrame(items)
        if df.empty:
            df = pd.DataFrame(columns=self.COLUMNS)
        df = df.rename(columns=self.COLUMN_NAMES_RU)
        df.to_excel(self.xlsx_path, index=False)


_log: Optional[SentApplicationsLog] = None


def get_sent_log() -> SentApplicationsLog:
    global _log
    if _log is None:
        _log = SentApplicationsLog()
    return _log
