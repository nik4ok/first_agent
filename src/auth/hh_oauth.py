import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, Optional, List
import requests

from src.config import settings

logger = logging.getLogger(__name__)


class HHOAuthManager:
    """Менеджер OAuth 2.0 авторизации для HH.ru."""

    TOKEN_FILE = settings.DATA_DIR / "hh_tokens.json"

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        redirect_uri: Optional[str] = None,
    ):
        self.client_id = client_id or settings.HH_CLIENT_ID
        self.client_secret = client_secret or settings.HH_CLIENT_SECRET
        self.redirect_uri = redirect_uri or getattr(settings, "HH_REDIRECT_URI", "https://hh.ru")
        self.base_auth_url = "https://hh.ru/oauth"
        self.base_api_url = settings.HH_API_URL

    def get_authorization_url(self) -> str:
        """
        Генерирует ссылку, по которой пользователю нужно перейти в браузере,
        нажать 'Разрешить' и скопировать code из адресной строки.
        """
        return f"{self.base_auth_url}/authorize?response_type=code&client_id={self.client_id}&redirect_uri={self.redirect_uri}"

    def exchange_code(self, code: str) -> Dict[str, Any]:
        """
        Обменивает полученный код авторизации на access_token и refresh_token.
        """
        url = f"{self.base_auth_url}/token"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": settings.HH_USER_AGENT,
        }
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "code": code.strip(),
        }

        response = requests.post(url, headers=headers, data=data, timeout=15)
        if response.status_code != 200:
            error_data = response.json() if response.headers.get("content-type") == "application/json" else response.text
            raise RuntimeError(f"Ошибка получения токена от HH ({response.status_code}): {error_data}")

        token_data = response.json()
        self.save_tokens(token_data)
        return token_data

    def refresh_token(self) -> Optional[Dict[str, Any]]:
        """
        Обновляет access_token с помощью refresh_token.
        """
        tokens = self.load_tokens()
        refresh_token = tokens.get("refresh_token") or os.getenv("HH_REFRESH_TOKEN")
        if not refresh_token:
            logger.warning("Нет refresh_token для обновления токена.")
            return None

        url = f"{self.base_auth_url}/token"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": settings.HH_USER_AGENT,
        }
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }

        response = requests.post(url, headers=headers, data=data, timeout=15)
        if response.status_code != 200:
            logger.error(f"Не удалось обновить токен: {response.text}")
            return None

        token_data = response.json()
        self.save_tokens(token_data)
        return token_data

    def save_tokens(self, token_data: Dict[str, Any]):
        """Сохранение токенов в JSON-файл и обновление .env."""
        with open(self.TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(token_data, f, indent=2)

        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")

        # Обновляем .env файл
        env_path = settings.ROOT_DIR / ".env"
        if env_path.exists():
            content = env_path.read_text(encoding="utf-8")
            if "HH_ACCESS_TOKEN=" in content:
                content = re_sub_env(content, "HH_ACCESS_TOKEN", access_token)
            else:
                content += f"\nHH_ACCESS_TOKEN={access_token}"

            if "HH_REFRESH_TOKEN=" in content:
                content = re_sub_env(content, "HH_REFRESH_TOKEN", refresh_token)
            else:
                content += f"\nHH_REFRESH_TOKEN={refresh_token}"

            env_path.write_text(content, encoding="utf-8")

    def load_tokens(self) -> Dict[str, Any]:
        """Загрузка токенов из файла или .env."""
        if self.TOKEN_FILE.exists():
            try:
                with open(self.TOKEN_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "access_token": os.getenv("HH_ACCESS_TOKEN", ""),
            "refresh_token": os.getenv("HH_REFRESH_TOKEN", ""),
        }

    def get_valid_access_token(self) -> Optional[str]:
        """Возвращает актуальный access_token."""
        tokens = self.load_tokens()
        return tokens.get("access_token") or None

    def get_user_info(self) -> Optional[Dict[str, Any]]:
        """Получение информации об авторизованном пользователе через /me."""
        token = self.get_valid_access_token()
        if not token:
            return None

        url = f"{self.base_api_url}/me"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": settings.HH_USER_AGENT,
            "HH-User-Agent": settings.HH_USER_AGENT,
        }
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 401:
                new_tokens = self.refresh_token()
                if new_tokens:
                    headers["Authorization"] = f"Bearer {new_tokens['access_token']}"
                    response = requests.get(url, headers=headers, timeout=10)

            if response.status_code == 200:
                data = response.json()
                first_name = data.get("first_name", "")
                last_name = data.get("last_name", "")
                full_name = f"{first_name} {last_name}".strip() or "Пользователь HH"
                counters = data.get("counters", {})
                return {
                    "id": data.get("id"),
                    "full_name": full_name,
                    "first_name": first_name,
                    "last_name": last_name,
                    "email": data.get("email"),
                    "phone": data.get("phone"),
                    "resumes_count": counters.get("resumes_count", 0),
                    "is_applicant": data.get("is_applicant", True),
                }
        except Exception as e:
            logger.warning(f"Не удалось получить профиль /me: {e}")
        return None

    def get_my_resumes(self) -> List[Dict[str, Any]]:
        """Получение списка резюме пользователя из HeadHunter."""
        token = self.get_valid_access_token()
        if not token:
            raise RuntimeError("Требуется авторизация: access_token не найден.")

        url = f"{self.base_api_url}/resumes/mine"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": settings.HH_USER_AGENT,
            "HH-User-Agent": settings.HH_USER_AGENT,
        }
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 401:
            # Пробуем обновить токен
            new_tokens = self.refresh_token()
            if new_tokens:
                headers["Authorization"] = f"Bearer {new_tokens['access_token']}"
                response = requests.get(url, headers=headers, timeout=15)

        response.raise_for_status()
        data = response.json()
        return data.get("items", [])

    def get_resume_details(self, resume_id: str) -> Dict[str, Any]:
        """Получение подробных данных конкретного резюме по ID с HeadHunter."""
        token = self.get_valid_access_token()
        if not token:
            raise RuntimeError("Требуется авторизация: access_token не найден.")

        url = f"{self.base_api_url}/resumes/{resume_id}"
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": settings.HH_USER_AGENT,
            "HH-User-Agent": settings.HH_USER_AGENT,
        }
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 401:
            new_tokens = self.refresh_token()
            if new_tokens:
                headers["Authorization"] = f"Bearer {new_tokens['access_token']}"
                response = requests.get(url, headers=headers, timeout=15)

        response.raise_for_status()
        return response.json()

    def download_and_format_resume(self, resume_id: str) -> str:
        """Скачивает резюме с HeadHunter и форматирует в читаемый текст для AI-анализа."""
        data = self.get_resume_details(resume_id)
        
        parts = []
        name = f"{data.get('last_name', '')} {data.get('first_name', '')} {data.get('middle_name', '')}".strip()
        if name:
            parts.append(f"# Имя: {name}")

        title = data.get("title", "")
        if title:
            parts.append(f"# Желаемая должность: {title}")

        skills = [s.get("name") for s in data.get("skill_set", []) if s.get("name")]
        if skills:
            parts.append(f"## Ключевые навыки:\n{', '.join(skills)}")

        skills_text = data.get("skills", "")
        if skills_text:
            parts.append(f"## Дополнительные навыки и о себе:\n{skills_text}")

        experience = data.get("experience", [])
        if experience:
            exp_parts = ["## Опыт работы:"]
            for exp in experience:
                comp = exp.get("company", "Компания")
                pos = exp.get("position", "Должность")
                start = exp.get("start", "")
                end = exp.get("end", "по настоящее время")
                desc = exp.get("description", "")
                exp_parts.append(f"### {pos} в {comp} ({start} — {end})\n{desc}\n")
            parts.append("\n".join(exp_parts))

        return "\n\n".join(parts)


def re_sub_env(content: str, key: str, value: str) -> str:
    import re
    return re.sub(rf"^{key}=.*$", f"{key}={value}", content, flags=re.MULTILINE)
