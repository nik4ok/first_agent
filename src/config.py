import os
import re
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")


class Settings:
    ROOT_DIR: Path = ROOT_DIR
    DATA_DIR: Path = ROOT_DIR / "data"
    EXCEL_PATH: Path = DATA_DIR / "vacancies.xlsx"
    RESUME_PATH: Path = DATA_DIR / "my_resume.txt"

    # HH API
    HH_API_URL: str = "https://api.hh.ru"
    HH_CLIENT_ID: str = os.getenv("HH_CLIENT_ID", "")
    HH_CLIENT_SECRET: str = os.getenv("HH_CLIENT_SECRET", "")
    HH_REDIRECT_URI: str = os.getenv("HH_REDIRECT_URI", "https://hh.ru")
    HH_ACCESS_TOKEN: str = os.getenv("HH_ACCESS_TOKEN", "")
    HH_REFRESH_TOKEN: str = os.getenv("HH_REFRESH_TOKEN", "")
    HH_RESUME_ID: str = os.getenv("HH_RESUME_ID", "")
    HH_USER_AGENT: str = os.getenv("HH_USER_AGENT", "JobAgent/1.0 (nikita.symnitelny@gmail.com)")

    # Search defaults (по умолчанию пустая или базовая, настраивается динамически)
    SEARCH_TEXT: str = os.getenv("SEARCH_TEXT", "")
    SEARCH_AREA: str = os.getenv("SEARCH_AREA", "113")  # 113 = Россия, 1 = Москва
    SEARCH_EXPERIENCE: str = os.getenv("SEARCH_EXPERIENCE", "between1And3")
    SEARCH_ONLY_WITH_SALARY: bool = os.getenv("SEARCH_ONLY_WITH_SALARY", "false").lower() == "true"
    SEARCH_PER_PAGE: int = int(os.getenv("SEARCH_PER_PAGE", "20"))

    # LLM (OpenAI / OpenRouter / DeepSeek / Ollama)
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL: str = os.getenv("OPENAI_BASE_URL", "")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "302327777")
    TELEGRAM_PROXY: str = os.getenv("TELEGRAM_PROXY", "")

    @classmethod
    def reload(cls):
        load_dotenv(cls.ROOT_DIR / ".env", override=True)
        cls.HH_CLIENT_ID = os.getenv("HH_CLIENT_ID", cls.HH_CLIENT_ID)
        cls.HH_CLIENT_SECRET = os.getenv("HH_CLIENT_SECRET", cls.HH_CLIENT_SECRET)
        cls.HH_ACCESS_TOKEN = os.getenv("HH_ACCESS_TOKEN", cls.HH_ACCESS_TOKEN)
        cls.HH_REFRESH_TOKEN = os.getenv("HH_REFRESH_TOKEN", cls.HH_REFRESH_TOKEN)
        cls.HH_RESUME_ID = os.getenv("HH_RESUME_ID", cls.HH_RESUME_ID)
        cls.SEARCH_TEXT = os.getenv("SEARCH_TEXT", cls.SEARCH_TEXT)
        cls.SEARCH_AREA = os.getenv("SEARCH_AREA", cls.SEARCH_AREA)
        cls.SEARCH_EXPERIENCE = os.getenv("SEARCH_EXPERIENCE", cls.SEARCH_EXPERIENCE)
        cls.OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", cls.OPENAI_API_KEY)
        cls.OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", cls.OPENAI_BASE_URL)
        cls.OPENAI_MODEL = os.getenv("OPENAI_MODEL", cls.OPENAI_MODEL)
        cls.TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", cls.TELEGRAM_BOT_TOKEN)
        cls.TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", cls.TELEGRAM_CHAT_ID)
        cls.TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", cls.TELEGRAM_PROXY)


def update_env_variable(key: str, value: str):
    """Обновляет или добавляет переменную в .env файл."""
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8")
        Settings.reload()
        return

    content = env_path.read_text(encoding="utf-8")
    if re.search(rf"^{key}=.*$", content, flags=re.MULTILINE):
        content = re.sub(rf"^{key}=.*$", f"{key}={value}", content, flags=re.MULTILINE)
    else:
        content += f"\n{key}={value}"

    env_path.write_text(content, encoding="utf-8")
    os.environ[key] = value
    Settings.reload()


settings = Settings()
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
