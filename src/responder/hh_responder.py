import logging
from typing import Dict, Any, Optional
import requests

from src.config import settings
from src.auth.hh_oauth import HHOAuthManager

logger = logging.getLogger(__name__)


import asyncio
import logging
from typing import Dict, Any, Optional
import requests

from src.config import settings
from src.auth.hh_oauth import HHOAuthManager

logger = logging.getLogger(__name__)


class HHResponder:
    """
    Модуль отправки откликов на вакансии HeadHunter.
    Поддерживает:
    1. Официальный API (POST /negotiations)
    2. Автоматический браузерный Playwright-движок (при ограничении соискательского API 403 Forbidden)
    """

    def __init__(self, auth_manager: Optional[HHOAuthManager] = None):
        self.auth_manager = auth_manager or HHOAuthManager()
        self.base_url = settings.HH_API_URL
        self.browser_session_file = settings.DATA_DIR / "browser_state.json"

    def apply(self, vacancy_id: str, resume_id: Optional[str] = None, message: str = "") -> Dict[str, Any]:
        """
        Отправка отклика на вакансию.
        1. Сначала пробует официальный API.
        2. При ошибке 403 (соискательский API закрыт HH) переключается на браузерную сессию Playwright.
        """
        target_resume_id = resume_id or settings.HH_RESUME_ID

        # Проверяем наличие токена OAuth
        token = self.auth_manager.get_valid_access_token()
        if token:
            url = f"{self.base_url}/negotiations"
            headers = {
                "Authorization": f"Bearer {token}",
                "User-Agent": settings.HH_USER_AGENT,
                "HH-User-Agent": settings.HH_USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
            }
            data = {
                "vacancy_id": str(vacancy_id),
                "resume_id": str(target_resume_id or ""),
            }
            if message:
                data["message"] = message

            try:
                response = requests.post(url, headers=headers, data=data, timeout=12)

                if response.status_code == 401:
                    refreshed = self.auth_manager.refresh_token()
                    if refreshed:
                        headers["Authorization"] = f"Bearer {refreshed['access_token']}"
                        response = requests.post(url, headers=headers, data=data, timeout=12)

                if response.status_code in [200, 201]:
                    return {
                        "success": True,
                        "method": "api",
                        "message": "Отклик успешно отправлен через API HeadHunter!",
                        "data": response.headers.get("Location") or "Applied",
                    }
            except Exception as e:
                logger.warning(f"Ошибка вызова API negotiations: {e}")

        # Если API вернул 403 (или нет токена), пробуем автоматический Playwright движок
        if self.browser_session_file.exists():
            try:
                from src.responder.playwright_solver import PlaywrightFormSolver
                solver = PlaywrightFormSolver(headless=True)
                vacancy_url = f"https://hh.ru/vacancy/{vacancy_id}"
                
                # Запускаем playwright solver в текущем или новом event loop
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            result = pool.submit(
                                lambda: asyncio.run(solver.solve_and_apply(vacancy_url, cover_letter=message))
                            ).result()
                    else:
                        result = loop.run_until_complete(solver.solve_and_apply(vacancy_url, cover_letter=message))
                except Exception:
                    result = asyncio.run(solver.solve_and_apply(vacancy_url, cover_letter=message))

                if result.get("success"):
                    result["method"] = "browser"
                    return result
                elif result.get("error") != "no_browser_session":
                    return result
            except Exception as e:
                logger.error(f"Ошибка запуска браузерного отклика: {e}")

        # Если браузерная сессия еще не создана
        vacancy_url = f"https://hh.ru/vacancy/{vacancy_id}"
        return {
            "success": False,
            "error": "browser_auth_needed",
            "vacancy_url": vacancy_url,
            "message": (
                "HeadHunter закрыл метод откликов через публичный API (403 Forbidden). "
                "Для фоновых автооткликов выполните вход в браузере (кнопка «🔐 Вход в браузере» в шапке дашборда) "
                "или откройте вакансию на hh.ru."
            ),
        }
