import logging
from typing import Dict, Any, Optional
import requests

from src.config import settings
from src.auth.hh_oauth import HHOAuthManager

logger = logging.getLogger(__name__)


class HHResponder:
    """Модуль отправки откликов на вакансии HeadHunter через официальный API /negotiations."""

    def __init__(self, auth_manager: Optional[HHOAuthManager] = None):
        self.auth_manager = auth_manager or HHOAuthManager()
        self.base_url = settings.HH_API_URL

    def apply(self, vacancy_id: str, resume_id: Optional[str] = None, message: str = "") -> Dict[str, Any]:
        """
        Отправка отклика на вакансию.
        POST https://api.hh.ru/negotiations
        Params:
          - vacancy_id
          - resume_id
          - message (сопроводительное письмо)
        """
        token = self.auth_manager.get_valid_access_token()
        if not token:
            return {
                "success": False,
                "error": "not_authorized",
                "message": "Требуется авторизация в HeadHunter (выполните /auth в боте).",
            }

        target_resume_id = resume_id or settings.HH_RESUME_ID
        if not target_resume_id:
            # Пытаемся автоматически подтянуть первое резюме
            try:
                resumes = self.auth_manager.get_my_resumes()
                if resumes:
                    target_resume_id = resumes[0].get("id")
            except Exception as e:
                logger.warning(f"Не удалось получить список резюме: {e}")

        if not target_resume_id:
            return {
                "success": False,
                "error": "no_resume_id",
                "message": "Не указан ID резюме (HH_RESUME_ID) для отклика.",
            }

        url = f"{self.base_url}/negotiations"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": settings.HH_USER_AGENT,
            "HH-User-Agent": settings.HH_USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        data = {
            "vacancy_id": str(vacancy_id),
            "resume_id": str(target_resume_id),
        }
        if message:
            data["message"] = message

        response = requests.post(url, headers=headers, data=data, timeout=15)

        # Обработка истекшего токена
        if response.status_code == 401:
            refreshed = self.auth_manager.refresh_token()
            if refreshed:
                headers["Authorization"] = f"Bearer {refreshed['access_token']}"
                response = requests.post(url, headers=headers, data=data, timeout=15)

        if response.status_code in [200, 201]:
            return {
                "success": True,
                "message": "Отклик успешно отправлен!",
                "data": response.headers.get("Location") or "Applied",
            }

        # Ошибки
        try:
            error_data = response.json()
        except Exception:
            error_data = {"error": response.text}

        error_description = error_data.get("description", str(error_data))
        return {
            "success": False,
            "error": f"http_{response.status_code}",
            "message": f"Ошибка отклика ({response.status_code}): {error_description}",
            "raw": error_data,
        }
