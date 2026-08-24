import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
import requests

from src.config import settings
from src.auth.hh_oauth import HHOAuthManager
from src.parser.excel_storage import ExcelStorage

logger = logging.getLogger(__name__)


class NegotiationTracker:
    """
    Трекер откликов, статусов приглашений и сообщений от рекрутеров через API HeadHunter.
    """

    STATE_FILE = settings.DATA_DIR / "negotiations_state.json"

    def __init__(self, auth_manager: Optional[HHOAuthManager] = None):
        self.auth_manager = auth_manager or HHOAuthManager()
        self.base_url = settings.HH_API_URL
        self.storage = ExcelStorage()

    def load_cached_state(self) -> Dict[str, Any]:
        """Загрузка предыдущего снимка состояний откликов."""
        if self.STATE_FILE.exists():
            try:
                with open(self.STATE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def save_cached_state(self, state: Dict[str, Any]):
        """Сохранение текущего снимка состояний."""
        with open(self.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    def fetch_negotiations(self) -> List[Dict[str, Any]]:
        """Получение списка всех активных откликов и их статусов."""
        token = self.auth_manager.get_valid_access_token()
        if not token:
            return []

        url = f"{self.base_url}/negotiations"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": settings.HH_USER_AGENT,
            "HH-User-Agent": settings.HH_USER_AGENT,
        }

        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.status_code == 401:
                refreshed = self.auth_manager.refresh_token()
                if refreshed:
                    headers["Authorization"] = f"Bearer {refreshed['access_token']}"
                    response = requests.get(url, headers=headers, timeout=15)

            if response.status_code == 200:
                return response.json().get("items", [])
            else:
                logger.warning(f"Ошибка получения negotiations ({response.status_code}): {response.text}")
                return []
        except Exception as e:
            logger.error(f"Ошибка запроса к negotiations: {e}")
            return []

    def check_updates(self) -> List[Dict[str, Any]]:
        """
        Проверяет обновления в откликах по сравнению с прошлым разом.
        Возвращает список новых событий (приглашения, отказы, новые сообщения).
        """
        items = self.fetch_negotiations()
        if not items:
            return []

        prev_state = self.load_cached_state()
        new_state = {}
        events = []

        for item in items:
            neg_id = str(item.get("id"))
            vacancy = item.get("vacancy", {})
            v_id = str(vacancy.get("id", ""))
            v_title = vacancy.get("name", "Вакансия")
            employer = vacancy.get("employer", {}).get("name", "Работодатель")
            state_obj = item.get("state", {})
            current_status = state_obj.get("id", "")  # e.g., 'invitation', 'discard', 'response'
            status_name = state_obj.get("name", current_status)
            has_updates = item.get("has_updates", False)

            new_state[neg_id] = {
                "status": current_status,
                "status_name": status_name,
                "has_updates": has_updates,
                "title": v_title,
                "employer": employer,
            }

            # Сравниваем со старым состоянием
            if neg_id in prev_state:
                old_status = prev_state[neg_id].get("status")
                if current_status != old_status:
                    event_type = "STATUS_CHANGED"
                    if current_status in ["invitation", "invite"]:
                        event_type = "INVITATION"
                    elif current_status in ["discard", "reject"]:
                        event_type = "DISCARD"

                    events.append({
                        "type": event_type,
                        "negotiation_id": neg_id,
                        "vacancy_id": v_id,
                        "vacancy_title": v_title,
                        "employer": employer,
                        "old_status": old_status,
                        "new_status": status_name,
                        "url": vacancy.get("alternate_url", f"https://hh.ru/vacancy/{v_id}"),
                    })

                    # Обновляем статус в Excel
                    if v_id:
                        excel_status = "INVITED" if event_type == "INVITATION" else ("REJECTED" if event_type == "DISCARD" else "UPDATED")
                        self.storage.update_status(vacancy_id=v_id, status=excel_status, notes=f"Статус HH: {status_name}")
            else:
                # Новый отклик
                if current_status in ["invitation", "invite"]:
                    events.append({
                        "type": "INVITATION",
                        "negotiation_id": neg_id,
                        "vacancy_id": v_id,
                        "vacancy_title": v_title,
                        "employer": employer,
                        "new_status": status_name,
                        "url": vacancy.get("alternate_url", f"https://hh.ru/vacancy/{v_id}"),
                    })

        self.save_cached_state(new_state)
        return events
