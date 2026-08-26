import asyncio
import concurrent.futures
import logging
from typing import Dict, Any, Optional
import requests

from src.config import settings
from src.auth.hh_oauth import HHOAuthManager

logger = logging.getLogger(__name__)


class HHResponder:
    """
    Отправка откликов на вакансии HeadHunter.
    Публичный API соискателя почти всегда отвечает 403 — поэтому при живой
    браузерной сессии сразу идём в Playwright.
    """

    def __init__(self, auth_manager: Optional[HHOAuthManager] = None):
        self.auth_manager = auth_manager or HHOAuthManager()
        self.base_url = settings.HH_API_URL
        self.browser_session_file = settings.DATA_DIR / "browser_state.json"

    def apply(self, vacancy_id: str, resume_id: Optional[str] = None, message: str = "") -> Dict[str, Any]:
        result = self._dispatch_apply(vacancy_id, resume_id=resume_id, message=message)
        if result.get("success"):
            try:
                from src.parser.applications_log import get_sent_log

                get_sent_log().record_from_excel_row(
                    vacancy_id,
                    method=str(result.get("method") or "browser"),
                    already_applied=bool(result.get("already_applied")),
                    cover_letter=message,
                )
            except Exception as exc:
                logger.warning("Не удалось записать журнал откликов: %s", exc)
        return result

    def _dispatch_apply(
        self, vacancy_id: str, resume_id: Optional[str] = None, message: str = ""
    ) -> Dict[str, Any]:
        """
        Отправка отклика на вакансию.
        1. Если есть сохранённая сессия браузера — Playwright.
        2. Иначе официальный API (часто 403).
        3. Fallback снова на Playwright, если сессия появилась.
        """
        target_resume_id = resume_id or settings.HH_RESUME_ID

        if self.browser_session_file.exists():
            browser_result = self._apply_via_browser(vacancy_id, message)
            if browser_result.get("success") or browser_result.get("error") != "no_browser_session":
                return browser_result

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
                logger.info("HH API negotiations: %s %s", response.status_code, response.text[:200])
            except Exception as e:
                logger.warning("Ошибка вызова API negotiations: %s", e)

        if self.browser_session_file.exists():
            return self._apply_via_browser(vacancy_id, message)

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

    def _apply_via_browser(self, vacancy_id: str, message: str) -> Dict[str, Any]:
        try:
            from src.responder.playwright_solver import PlaywrightFormSolver

            solver = PlaywrightFormSolver(headless=True)
            vacancy_url = f"https://hh.ru/vacancy/{vacancy_id}"
            result = self._run_playwright_apply(solver, vacancy_url, message)
            if result.get("success"):
                result["method"] = "browser"
            return result
        except Exception as e:
            logger.error("Ошибка запуска браузерного отклика: %s", e)
            return {
                "success": False,
                "error": "browser_error",
                "message": f"Ошибка браузерного отклика: {e}",
            }

    @staticmethod
    def _run_playwright_apply(solver, vacancy_url: str, message: str) -> Dict[str, Any]:
        """Playwright async API нельзя крутить в уже запущенном event loop — выносим в поток."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(
                    lambda: asyncio.run(solver.solve_and_apply(vacancy_url, cover_letter=message))
                ).result()
        return asyncio.run(solver.solve_and_apply(vacancy_url, cover_letter=message))
