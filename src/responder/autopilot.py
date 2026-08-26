"""
Фоновый автопилот откликов.

Старт ставит очередь в data/autopilot_state.json и сразу возвращает управление.
Тикер (FastAPI lifespan или APScheduler) раз в ~15 секунд отправляет следующий
отклик, когда наступило next_at. Так 50 вакансий можно растянуть на 3 часа
без таймаута HTTP и без пачки запросов в HH.
"""
from __future__ import annotations

import fcntl
import json
import logging
import random
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pandas as pd

from src.config import settings
from src.parser.excel_storage import ExcelStorage
from src.analyzer.matcher import AIResumeAnalyzer
from src.responder.hh_responder import HHResponder

logger = logging.getLogger(__name__)

STATE_FILE = settings.DATA_DIR / "autopilot_state.json"
STATE_LOCK_FILE = settings.DATA_DIR / ".autopilot.lock"
FINISHED_STATUSES = {"APPLIED", "SKIPPED", "INVITED"}
BUSY_STATUSES = {"APPLIED", "SKIPPED", "INVITED", "QUEUED"}

_engine: Optional["AutopilotEngine"] = None
_engine_lock = threading.Lock()


def vacancy_matches_keywords(title: str, employer: str, description: str, keywords: str) -> bool:
    """Критерий отбора: через запятую — все фрагменты; иначе достаточно половины слов."""
    raw = (keywords or "").strip().lower()
    if not raw:
        return True
    hay = f"{title} {employer} {description}".lower()
    if "," in raw or ";" in raw:
        tokens = [t.strip() for t in re.split(r"[,;]+", raw) if t.strip()]
        return all(token in hay for token in tokens)
    if raw in hay:
        return True
    stop = {"для", "или", "как", "это", "and", "the", "with"}
    words = [w for w in re.findall(r"[a-zа-я0-9+#.]{3,}", raw, flags=re.I) if w.lower() not in stop]
    if not words:
        return True
    hits = sum(1 for w in words if w.lower() in hay)
    return hits >= max(1, (len(words) + 1) // 2)


def compute_apply_interval(count: int, duration_hours: float, delay_seconds: float = 90.0) -> float:
    """Секунды между откликами. duration_hours>0 — равномерно на окно, иначе delay_seconds."""
    if duration_hours and duration_hours > 0 and count > 0:
        return max(30.0, (float(duration_hours) * 3600.0) / max(count, 1))
    return max(8.0, float(delay_seconds or 90.0))


def parse_duration_arg(raw: str) -> float:
    """
    '3h' / '3ч' → 3.0 часа
    '90m' / '90мин' → 1.5
    '3' → 3.0 часа
    """
    text = (raw or "").strip().lower().replace(" ", "")
    if not text:
        return 3.0
    match = re.fullmatch(r"(\d+(?:[.,]\d+)?)(h|ч|hours?|час(?:а|ов)?|m|мин(?:ут)?|min)?", text)
    if not match:
        raise ValueError(f"Непонятный срок: {raw}")
    value = float(match.group(1).replace(",", "."))
    unit = match.group(2) or "h"
    if unit in {"m", "мин", "минут", "min"}:
        return max(0.0, value / 60.0)
    return max(0.0, value)


def parse_hh_resume_id(raw: str) -> str:
    """Достаёт ID резюме из ссылки hh.ru/resume/... или возвращает уже чистый id."""
    text = (raw or "").strip()
    match = re.search(r"/resume/([a-zA-Z0-9]+)", text)
    if match:
        return match.group(1)
    return text.split("?")[0].rstrip("/").split("/")[-1]


def get_autopilot() -> "AutopilotEngine":
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = AutopilotEngine()
        return _engine


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


class AutopilotEngine:
    def __init__(self):
        self.storage = ExcelStorage()
        self.analyzer = AIResumeAnalyzer()
        self.responder = HHResponder()
        self._io_lock = threading.Lock()

    def _state_file_lock(self):
        STATE_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        return open(STATE_LOCK_FILE, "a+", encoding="utf-8")

    def default_state(self) -> Dict[str, Any]:
        return {
            "running": False,
            "queue": [],
            "current_index": 0,
            "applied": [],
            "failed": [],
            "min_score": 70,
            "max_count": 50,
            "duration_hours": 3.0,
            "interval_seconds": 216.0,
            "next_at": None,
            "started_at": None,
            "finished_at": None,
            "last_error": None,
            "applying": False,
            "message": "",
            "current_title": "",
            "mode": "review",
            "keywords": "",
            "pending": None,
            "awaiting_review": False,
            "skipped": [],
            "reviewing": False,
        }

    def _load_state_unlocked(self) -> Dict[str, Any]:
        state = self.default_state()
        if STATE_FILE.exists():
            try:
                saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    state.update(saved)
            except Exception as exc:
                logger.warning("Не удалось прочитать состояние автопилота: %s", exc)
        return state

    def _save_state_unlocked(self, state: Dict[str, Any]) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_state(self) -> Dict[str, Any]:
        with self._io_lock:
            lock_file = self._state_file_lock()
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                return self._load_state_unlocked()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()

    def save_state(self, state: Dict[str, Any]) -> None:
        with self._io_lock:
            lock_file = self._state_file_lock()
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                self._save_state_unlocked(state)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()

    def status(self) -> Dict[str, Any]:
        state = self.load_state()
        total = len(state.get("queue") or [])
        index = int(state.get("current_index") or 0)
        remaining = max(0, total - index) if state.get("running") else 0
        next_at = _parse_iso(state.get("next_at"))
        eta_seconds = None
        if state.get("running") and remaining > 0 and (state.get("mode") or "review") == "auto":
            interval = float(state.get("interval_seconds") or 0)
            wait = 0.0
            if next_at:
                wait = max(0.0, (next_at - _now()).total_seconds())
            eta_seconds = int(wait + max(0, remaining - 1) * interval)
        return {
            **state,
            "queued_total": total,
            "processed": index,
            "remaining": remaining,
            "applied_count": len(state.get("applied") or []),
            "failed_count": len(state.get("failed") or []),
            "skipped_count": len(state.get("skipped") or []),
            "eta_seconds": eta_seconds,
            "mode": state.get("mode") or "review",
            "pending": state.get("pending"),
            "awaiting_review": bool(state.get("awaiting_review")),
        }

    def select_candidates(
        self,
        min_score: int,
        max_count: int,
        keywords: str = "",
    ) -> pd.DataFrame:
        df = self.storage.load_all()
        if df.empty:
            return df

        id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
        status_col = "Статус" if "Статус" in df.columns else "status"
        score_col = "Score (%)" if "Score (%)" in df.columns else "match_score"
        title_col = "Название вакансии" if "Название вакансии" in df.columns else "title"
        comp_col = "Компания" if "Компания" in df.columns else "employer"
        desc_col = "Полное описание" if "Полное описание" in df.columns else "description"

        busy = df[status_col].astype(str).isin(BUSY_STATUSES)
        free = df[~busy].copy()
        if free.empty:
            return free

        if keywords.strip():
            mask = free.apply(
                lambda rec: vacancy_matches_keywords(
                    str(rec.get(title_col, "") or ""),
                    str(rec.get(comp_col, "") or ""),
                    str(rec.get(desc_col, "") or "") if desc_col in rec.index else "",
                    keywords,
                ),
                axis=1,
            )
            filtered = free[mask]
            if not filtered.empty:
                free = filtered

        free["num_score"] = pd.to_numeric(free[score_col], errors="coerce").fillna(0)
        matched = free[free["num_score"] >= min_score].sort_values(by="num_score", ascending=False)
        if matched.empty:
            matched = free.sort_values(by="num_score", ascending=False)
        return matched.head(max_count)

    def start(
        self,
        min_score: int = 70,
        max_count: int = 50,
        duration_hours: float = 3.0,
        delay_seconds: float = 90.0,
        vacancy_ids: Optional[List[str]] = None,
        mode: str = "review",
        keywords: str = "",
    ) -> Dict[str, Any]:
        current = self.load_state()
        if current.get("running"):
            return {
                "status": "already_running",
                "message": "Очередь уже запущена. Остановите текущий сеанс, чтобы стартовать новый.",
                **self.status(),
            }

        mode = "auto" if str(mode).lower() in {"auto", "autopilot", "без подтверждения"} else "review"
        keywords = (keywords or "").strip()

        if vacancy_ids:
            queue_ids = [str(v) for v in vacancy_ids if str(v).strip()][: max(1, int(max_count))]
            if not queue_ids:
                return {
                    "status": "empty",
                    "message": "Список вакансий для очереди пуст.",
                    "queued": 0,
                }
        else:
            candidates = self.select_candidates(
                min_score=min_score, max_count=max_count, keywords=keywords
            )
            if candidates.empty:
                return {
                    "status": "empty",
                    "message": "Нет вакансий для отклика. Сначала соберите и проанализируйте базу.",
                    "queued": 0,
                }
            id_col = "ID Вакансии" if "ID Вакансии" in candidates.columns else "id"
            queue_ids = [str(v) for v in candidates[id_col].tolist()]
        interval = compute_apply_interval(len(queue_ids), duration_hours, delay_seconds)
        started = _now()

        self.storage.update_rows([{"id": vid, "status": "QUEUED"} for vid in queue_ids])

        state = self.default_state()
        state.update(
            {
                "running": True,
                "queue": queue_ids,
                "current_index": 0,
                "min_score": int(min_score),
                "max_count": int(max_count),
                "duration_hours": float(duration_hours),
                "interval_seconds": interval,
                "mode": mode,
                "keywords": keywords,
                "started_at": _iso(started),
            }
        )
        if mode == "auto":
            state["next_at"] = _iso(started)
            state["message"] = (
                f"В очереди {len(queue_ids)} откликов без подтверждения, "
                f"интервал ~{int(interval // 60)} мин {int(interval % 60)} сек."
            )
            self.save_state(state)
        else:
            state["next_at"] = None
            state["message"] = (
                f"На подтверждении {len(queue_ids)} вакансий. Смотрите карточку и жмите "
                "«Отправить», правьте письмо или пропускайте."
            )
            self.save_state(state)
            self._ensure_pending()

        logger.info(
            "Очередь запущена (%s): %s вакансий, критерии %r, min_score=%s",
            mode,
            len(queue_ids),
            keywords,
            min_score,
        )
        return {
            "status": "started",
            "queued": len(queue_ids),
            "interval_seconds": interval,
            "duration_hours": duration_hours,
            "mode": mode,
            "message": self.status().get("message") or state["message"],
            **self.status(),
        }

    def stop(self, reason: str = "Остановлен пользователем") -> Dict[str, Any]:
        state = self.load_state()
        start_from = int(state.get("current_index") or 0)
        if state.get("applying"):
            start_from += 1
        remaining_ids = (state.get("queue") or [])[start_from:]
        if remaining_ids:
            self.storage.update_rows(
                [{"id": vid, "status": "ANALYZED"} for vid in remaining_ids]
            )
        state["running"] = False
        state["applying"] = False
        state["reviewing"] = False
        state["awaiting_review"] = False
        state["pending"] = None
        state["finished_at"] = _iso(_now())
        state["message"] = reason
        state["next_at"] = None
        self.save_state(state)
        return {"status": "stopped", "message": reason, **self.status()}

    async def tick(self) -> Optional[Dict[str, Any]]:
        """Отправляет один отклик, если очередь активна и наступило next_at."""
        vacancy_id: Optional[str] = None
        lock_file = self._state_file_lock()
        try:
            with self._io_lock:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                state = self._load_state_unlocked()
                if not state.get("running") or state.get("applying"):
                    return None
                if (state.get("mode") or "review") != "auto":
                    return None
                queue: List[str] = list(state.get("queue") or [])
                index = int(state.get("current_index") or 0)
                if index >= len(queue):
                    state["running"] = False
                    state["finished_at"] = _iso(_now())
                    state["message"] = "Очередь откликов завершена."
                    self._save_state_unlocked(state)
                    return None
                next_at = _parse_iso(state.get("next_at"))
                if next_at and _now() < next_at:
                    return None
                vacancy_id = queue[index]
                state["applying"] = True
                self._save_state_unlocked(state)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

        if not vacancy_id:
            return None
        result = {"success": False, "id": vacancy_id, "error": "unknown", "title": ""}
        try:
            result = self._apply_one(vacancy_id)
        except Exception as exc:
            logger.exception("Необработанный сбой отклика %s: %s", vacancy_id, exc)
            result = {"success": False, "id": vacancy_id, "error": str(exc), "title": ""}

        lock_file = self._state_file_lock()
        try:
            with self._io_lock:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                state = self._load_state_unlocked()
                index = int(state.get("current_index") or 0)
                queue = list(state.get("queue") or [])
                interval = float(state.get("interval_seconds") or 90)
                jitter = interval * random.uniform(-0.1, 0.1)
                wait = max(8.0, interval + jitter)
                state["applying"] = False
                state["current_index"] = index + 1
                state["current_title"] = result.get("title") or ""
                state["last_error"] = None if result.get("success") else result.get("error")
                if result.get("success"):
                    state.setdefault("applied", []).append(result)
                else:
                    state.setdefault("failed", []).append(result)
                still_running = bool(state.get("running"))
                done = state["current_index"] >= len(queue)
                if not still_running:
                    state["next_at"] = None
                    state["message"] = (
                        f"Остановлен. Успешно: {len(state.get('applied') or [])}, "
                        f"ошибок: {len(state.get('failed') or [])}."
                    )
                elif done:
                    state["running"] = False
                    state["next_at"] = None
                    state["finished_at"] = _iso(_now())
                    state["message"] = (
                        f"Готово: отправлено {len(state.get('applied') or [])}, "
                        f"ошибок {len(state.get('failed') or [])}."
                    )
                else:
                    state["next_at"] = _iso(_now() + timedelta(seconds=wait))
                    state["message"] = (
                        f"Отправлено {len(state.get('applied') or [])} из {len(queue)}. "
                        f"Следующий отклик через ~{int(wait // 60)} мин."
                    )
                self._save_state_unlocked(state)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        return result

    def _apply_one(self, vacancy_id: str, letter: Optional[str] = None) -> Dict[str, Any]:
        df = self.storage.load_all()
        if df.empty:
            return {"success": False, "id": vacancy_id, "error": "База пуста"}

        id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
        title_col = "Название вакансии" if "Название вакансии" in df.columns else "title"
        comp_col = "Компания" if "Компания" in df.columns else "employer"
        desc_col = "Полное описание" if "Полное описание" in df.columns else "description"
        skills_col = "Ключевые навыки" if "Ключевые навыки" in df.columns else "skills"

        row = df[df[id_col].astype(str) == str(vacancy_id)]
        if row.empty:
            return {"success": False, "id": vacancy_id, "error": "Вакансия не найдена в Excel"}

        rec = row.iloc[0]
        title = str(rec.get(title_col, "") or "")
        employer = str(rec.get(comp_col, "") or "")
        desc = str(rec.get(desc_col, "") or "")
        skills = str(rec.get(skills_col, "") or "") if skills_col in rec.index else ""
        excel_letter = ""
        if "Сопроводительное письмо" in rec.index:
            excel_letter = str(rec.get("Сопроводительное письмо") or "")
        elif "cover_letter" in rec.index:
            excel_letter = str(rec.get("cover_letter") or "")

        text = (letter or excel_letter or "").strip()
        weak = (
            not text
            or text.lower() in {"nan", "none"}
            or len(text) < 80
            or "буду рад" in text.lower()
            or "опираюсь на" in text.lower()
        )
        if weak:
            match_info = self.analyzer.analyze_match(title, desc, skills)
            text = self.analyzer.generate_cover_letter(title, employer, desc, match_info)

        try:
            res = self.responder.apply(
                vacancy_id=vacancy_id,
                resume_id=settings.HH_RESUME_ID or None,
                message=text,
            )
        except Exception as exc:
            logger.exception("Сбой отклика %s: %s", vacancy_id, exc)
            res = {"success": False, "message": str(exc)}

        payload = {
            "id": vacancy_id,
            "title": title,
            "employer": employer,
            "success": bool(res.get("success")),
            "error": None if res.get("success") else (res.get("message") or res.get("error") or "unknown"),
            "method": res.get("method"),
        }
        if payload["success"]:
            self.storage.update_status(vacancy_id=vacancy_id, status="APPLIED", cover_letter=text)
        else:
            self.storage.update_status(vacancy_id=vacancy_id, status="ANALYZED", cover_letter=text)
        logger.info(
            "Автопилот [%s] %s — %s",
            vacancy_id,
            title,
            "OK" if payload["success"] else payload["error"],
        )
        return payload

    def _read_vacancy_row(self, vacancy_id: str) -> Optional[Dict[str, str]]:
        df = self.storage.load_all()
        if df.empty:
            return None
        id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
        row = df[df[id_col].astype(str) == str(vacancy_id)]
        if row.empty:
            return None
        rec = row.iloc[0]

        def _col(*names: str) -> str:
            for name in names:
                if name in rec.index:
                    val = rec.get(name)
                    if val is not None and str(val).lower() not in {"nan", "none"}:
                        return str(val)
            return ""

        return {
            "id": str(vacancy_id),
            "title": _col("Название вакансии", "title"),
            "employer": _col("Компания", "employer"),
            "city": _col("Город", "city"),
            "url": _col("Ссылка", "url") or f"https://hh.ru/vacancy/{vacancy_id}",
            "score": _col("Score (%)", "match_score"),
            "description": _col("Полное описание", "description"),
            "skills": _col("Ключевые навыки", "skills"),
        }

    def _build_pending(self, vacancy_id: str, *, regenerate: bool = False) -> Optional[Dict[str, Any]]:
        meta = self._read_vacancy_row(vacancy_id)
        if not meta:
            return None
        df = self.storage.load_all()
        letter = ""
        if not regenerate and not df.empty and "Сопроводительное письмо" in df.columns:
            id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
            row = df[df[id_col].astype(str) == str(vacancy_id)]
            if not row.empty:
                letter = str(row.iloc[0].get("Сопроводительное письмо") or "")
                if letter.lower() in {"nan", "none"}:
                    letter = ""
        if regenerate or len(letter) < 40:
            match_info = self.analyzer.analyze_match(meta["title"], meta["description"], meta["skills"])
            letter = self.analyzer.generate_cover_letter(
                meta["title"], meta["employer"], meta["description"], match_info
            )
            self.storage.update_status(vacancy_id=vacancy_id, cover_letter=letter)
        meta["letter"] = letter
        return meta

    def _ensure_pending(self) -> Optional[Dict[str, Any]]:
        state = self.load_state()
        if not state.get("running") or (state.get("mode") or "review") == "auto":
            return state.get("pending")
        queue: List[str] = list(state.get("queue") or [])
        index = int(state.get("current_index") or 0)
        if index >= len(queue):
            state["running"] = False
            state["awaiting_review"] = False
            state["pending"] = None
            state["finished_at"] = _iso(_now())
            state["message"] = (
                f"Готово: отправлено {len(state.get('applied') or [])}, "
                f"пропущено {len(state.get('skipped') or [])}, "
                f"ошибок {len(state.get('failed') or [])}."
            )
            self.save_state(state)
            return None
        current_id = str(queue[index])
        pending = state.get("pending") or {}
        if str(pending.get("id") or "") == current_id and pending.get("letter"):
            return pending
        card = self._build_pending(current_id)
        if not card:
            state.setdefault("failed", []).append(
                {"id": current_id, "success": False, "error": "Вакансия не найдена в Excel"}
            )
            state["current_index"] = index + 1
            state["pending"] = None
            self.save_state(state)
            return self._ensure_pending()
        card["index"] = index + 1
        card["total"] = len(queue)
        state["pending"] = card
        state["awaiting_review"] = True
        state["current_title"] = card.get("title") or ""
        state["message"] = (
            f"На подтверждении {index + 1} из {len(queue)}: {card.get('title')}"
        )
        self.save_state(state)
        return card

    def approve(self, letter: Optional[str] = None) -> Dict[str, Any]:
        state = self.load_state()
        if (state.get("mode") or "review") == "auto":
            return {"status": "error", "message": "Сейчас режим без подтверждения.", **self.status()}
        pending = state.get("pending") or {}
        vacancy_id = str(pending.get("id") or "")
        if not state.get("running") or not vacancy_id:
            return {"status": "idle", "message": "Нет вакансии на подтверждении.", **self.status()}
        if state.get("applying") or state.get("reviewing"):
            return {"status": "busy", "message": "Уже отправляю предыдущий отклик.", **self.status()}

        text = (letter if letter is not None else pending.get("letter") or "").strip()
        state["applying"] = True
        state["reviewing"] = True
        state["awaiting_review"] = False
        self.save_state(state)
        try:
            result = self._apply_one(vacancy_id, letter=text)
        except Exception as exc:
            logger.exception("Сбой подтверждённого отклика %s: %s", vacancy_id, exc)
            result = {"success": False, "id": vacancy_id, "error": str(exc), "title": pending.get("title") or ""}

        state = self.load_state()
        index = int(state.get("current_index") or 0)
        state["applying"] = False
        state["reviewing"] = False
        state["pending"] = None
        state["current_index"] = index + 1
        state["current_title"] = result.get("title") or ""
        state["last_error"] = None if result.get("success") else result.get("error")
        if result.get("success"):
            state.setdefault("applied", []).append(result)
        else:
            state.setdefault("failed", []).append(result)
        queue = list(state.get("queue") or [])
        done = state["current_index"] >= len(queue)
        if done:
            state["running"] = False
            state["finished_at"] = _iso(_now())
            state["message"] = (
                f"Готово: отправлено {len(state.get('applied') or [])}, "
                f"пропущено {len(state.get('skipped') or [])}, "
                f"ошибок {len(state.get('failed') or [])}."
            )
        else:
            state["message"] = (
                f"Отправлено {len(state.get('applied') or [])} из {len(queue)}. Готовлю следующую."
            )
        self.save_state(state)
        next_pending = self._ensure_pending() if state.get("running") else None
        return {
            "status": "sent" if result.get("success") else "failed",
            "result": result,
            "pending": next_pending,
            **self.status(),
        }

    def skip(self) -> Dict[str, Any]:
        state = self.load_state()
        pending = state.get("pending") or {}
        vacancy_id = str(pending.get("id") or "")
        if not state.get("running") or not vacancy_id:
            return {"status": "idle", "message": "Нет вакансии на подтверждении.", **self.status()}
        self.storage.update_status(vacancy_id=vacancy_id, status="SKIPPED")
        index = int(state.get("current_index") or 0)
        state.setdefault("skipped", []).append(
            {"id": vacancy_id, "title": pending.get("title"), "employer": pending.get("employer")}
        )
        state["current_index"] = index + 1
        state["pending"] = None
        state["awaiting_review"] = False
        queue = list(state.get("queue") or [])
        if state["current_index"] >= len(queue):
            state["running"] = False
            state["finished_at"] = _iso(_now())
            state["message"] = (
                f"Готово: отправлено {len(state.get('applied') or [])}, "
                f"пропущено {len(state.get('skipped') or [])}, "
                f"ошибок {len(state.get('failed') or [])}."
            )
        self.save_state(state)
        next_pending = self._ensure_pending() if state.get("running") else None
        return {"status": "skipped", "pending": next_pending, **self.status()}

    def regenerate_letter(self) -> Dict[str, Any]:
        state = self.load_state()
        pending = state.get("pending") or {}
        vacancy_id = str(pending.get("id") or "")
        if not vacancy_id:
            return {"status": "idle", "message": "Нет вакансии на подтверждении.", **self.status()}
        card = self._build_pending(vacancy_id, regenerate=True)
        if not card:
            return {"status": "error", "message": "Не удалось пересобрать письмо.", **self.status()}
        card["index"] = pending.get("index") or int(state.get("current_index") or 0) + 1
        card["total"] = pending.get("total") or len(state.get("queue") or [])
        state["pending"] = card
        self.save_state(state)
        return {"status": "ok", "pending": card, **self.status()}

    def update_pending_letter(self, letter: str) -> Dict[str, Any]:
        state = self.load_state()
        pending = dict(state.get("pending") or {})
        if not pending.get("id"):
            return {"status": "idle", "message": "Нет вакансии на подтверждении.", **self.status()}
        text = (letter or "").strip()
        if len(text) < 20:
            return {"status": "error", "message": "Слишком короткое письмо.", **self.status()}
        pending["letter"] = text
        state["pending"] = pending
        self.save_state(state)
        self.storage.update_status(vacancy_id=str(pending["id"]), cover_letter=text)
        return {"status": "ok", "pending": pending, **self.status()}
