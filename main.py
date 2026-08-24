import argparse
import asyncio
import sys
import threading
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from src.config import settings
from src.parser import HHClient, ExcelStorage
from src.analyzer import AIResumeAnalyzer
from src.auth import HHOAuthManager
from src.bot import start_telegram_bot, create_bot_app
from src.bot.telegram_bot import setup_bot_commands
from src.scheduler import JobHuntingScheduler
from src.web import run_web_dashboard


def handle_browser_login():
    """Вход в HeadHunter через браузер Playwright для сохранения постоянной сессии автооткликов."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright не установлен. Выполните: pip install playwright && playwright install chromium")
        return

    print("\n🌐 === ВХОД В HEADHUNTER ЧЕРЕЗ БРАУЗЕР (PLAYWRIGHT) ===")
    print("Открываю браузер Chromium...")
    print("1. В открывшемся окне выполните вход в ваш аккаунт HeadHunter.")
    print("2. После успешного входа вернитесь сюда и нажмите Enter для сохранения сессии.\n")

    session_file = settings.DATA_DIR / "browser_state.json"
    with sync_playwright() as p:
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            browser = p.chromium.launch(headless=False, args=launch_args)
        except Exception as e_bundled:
            try:
                browser = p.chromium.launch(channel="chrome", headless=False, args=launch_args)
            except Exception:
                print(f"❌ Ошибка запуска браузера: {e_bundled}")
                print("💡 Выполните установку браузера: ./venv/bin/playwright install chromium")
                return

        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            storage_state=str(session_file) if session_file.exists() else None,
        )
        page = context.new_page()
        page.goto("https://hh.ru/account/login")
        input("\n👉 Нажмите [ENTER] здесь после того, как вошли в свой профиль на hh.ru: ")
        context.storage_state(path=str(session_file))
        print(f"\n🎉 Сессия успешно сохранена в {session_file.name}! Теперь фоновые автоотклики через Playwright активны.")
        browser.close()


def handle_auth():
    """Консольная авторизация в HeadHunter по OAuth."""
    mgr = HHOAuthManager()
    url = mgr.get_authorization_url()
    print("\n🔐 === АВТОРИЗАЦИЯ В HEADHUNTER ===")
    print("1. Откройте в браузере ссылку:")
    print(f"\n   👉 {url}\n")
    print("2. Нажмите «Разрешить».")
    print("3. Скопируйте значение 'code' из адресной строки (https://hh.ru/?code=ВАШ_КОД).")

    code = input("\nВставьте код сюда: ").strip()
    if not code:
        print("Код не введен. Отмена.")
        return

    try:
        tokens = mgr.exchange_code(code)
        print("🎉 Авторизация успешна! Токены сохранены в .env и data/hh_tokens.json")
        try:
            resumes = mgr.get_my_resumes()
            if resumes:
                print("\n📄 Ваши резюме на HeadHunter:")
                for r in resumes:
                    print(f"  • {r.get('title')} (ID: {r.get('id')})")
        except Exception:
            pass
    except Exception as e:
        print(f"❌ Ошибка авторизации: {e}")


def handle_parse(query: str, count: int, area: str, experience: str, only_with_salary: bool):
    """Сбор вакансий и сохранение в Excel."""
    print(f"\n🔍 Запуск сбора вакансий по запросу: '{query}'")
    client = HHClient()
    storage = ExcelStorage()

    vacancies = client.fetch_and_normalize_vacancies(
        text=query,
        area=area,
        experience=experience,
        only_with_salary=only_with_salary,
        max_vacancies=count,
        fetch_full_description=True,
    )

    added = storage.save_new_vacancies(vacancies)
    total_df = storage.load_all()

    print(f"✅ Найдено вакансий: {len(vacancies)}")
    print(f"📥 Новых сохранено в Excel: {added}")
    print(f"📊 Всего записей в базе ({settings.EXCEL_PATH.name}): {len(total_df)}")


def handle_analyze():
    """AI анализ всех новых вакансий в Excel."""
    storage = ExcelStorage()
    analyzer = AIResumeAnalyzer()

    df = storage.load_all()
    if df.empty:
        print("База вакансий пуста. Сначала выполните сбор: python main.py parse")
        return

    status_col = "Статус" if "Статус" in df.columns else "status"
    id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
    title_col = "Название вакансии" if "Название вакансии" in df.columns else "title"
    comp_col = "Компания" if "Компания" in df.columns else "employer"
    desc_col = "Полное описание" if "Полное описание" in df.columns else "description"
    skills_col = "Ключевые навыки" if "Ключевые навыки" in df.columns else "skills"

    new_rows = df[df[status_col] == "NEW"]
    print(f"\n🤖 Найдено новых вакансий для анализа: {len(new_rows)}")

    for _, row in new_rows.iterrows():
        v_id = str(row[id_col])
        title = str(row.get(title_col, ""))
        employer = str(row.get(comp_col, ""))
        desc = str(row.get(desc_col, ""))
        skills = str(row.get(skills_col, ""))

        print(f"\n🔎 Анализирую: [{v_id}] {title} ({employer})...")
        match_info = analyzer.analyze_match(title, desc, skills)
        score = match_info.get("score", 50)
        pros = match_info.get("pros", "")
        cons = match_info.get("cons", "")

        cover_letter = analyzer.generate_cover_letter(title, employer, desc, match_info)

        storage.update_status(
            vacancy_id=v_id,
            status="ANALYZED",
            match_score=score,
            cover_letter=cover_letter,
            notes=f"Плюсы: {pros} | Минусы: {cons}",
        )
        print(f"  ⭐ Match Score: {score}% | Рекомендация: {match_info.get('recommendation', 'MANUAL')}")

    print("\n✅ Анализ завершен! Все результаты и сгенерированные письма сохранены в Excel.")


async def run_bot_and_scheduler():
    """Запуск Telegram-бота вместе с фоновым планировщиком."""
    app = create_bot_app()
    if not app:
        print("⚠️ Telegram Bot токен не задан. Запуск в фоновом режиме планировщика...")
        sched = JobHuntingScheduler()
        sched.start(scan_interval_minutes=30, tracking_interval_minutes=10)
        while True:
            await asyncio.sleep(3600)
        return

    dp, bot = app
    sched = JobHuntingScheduler(bot=bot, chat_id=settings.TELEGRAM_CHAT_ID)
    sched.start(scan_interval_minutes=30, tracking_interval_minutes=10)

    print("🚀 Telegram Bot и планировщик запускаются...")
    try:
        await setup_bot_commands(bot)
        await dp.start_polling(bot)
    except Exception as e:
        print(f"\n⚠️ Ошибка подключения к Telegram API: {e}")
        print("💡 Подсказка: Включите VPN или укажите TELEGRAM_PROXY в .env файле.")
        print("🌐 Веб-дашборд и планировщик продолжают работать в фоновом режиме...\n")
        # Не даем процессу завершиться, чтобы веб-сервер и планировщик продолжали работать
        while True:
            await asyncio.sleep(3600)


def main():
    parser = argparse.ArgumentParser(description="AI Job Agent for HeadHunter")
    subparsers = parser.add_subparsers(dest="command", help="Команда для выполнения")

    # parse
    p_parse = subparsers.add_parser("parse", help="Собрать новые вакансии с HH.ru в Excel")
    p_parse.add_argument("--query", "-q", default=settings.SEARCH_TEXT, help="Поисковый запрос")
    p_parse.add_argument("--count", "-c", type=int, default=10, help="Количество вакансий")
    p_parse.add_argument("--area", "-a", default=settings.SEARCH_AREA, help="Регион (113 - РФ)")
    p_parse.add_argument("--experience", "-e", default=settings.SEARCH_EXPERIENCE, help="Опыт")

    # analyze
    subparsers.add_parser("analyze", help="Запустить AI-анализ и генерацию писем для новых вакансий")

    # auth
    subparsers.add_parser("auth", help="Авторизоваться в HeadHunter по OAuth")

    # browser-login
    subparsers.add_parser("browser-login", help="Вход в HeadHunter через браузер Playwright (для автоматических откликов)")

    # bot
    subparsers.add_parser("bot", help="Запустить Telegram бота и планировщик")

    # web / dashboard
    p_web = subparsers.add_parser("web", help="Запустить Web Dashboard в браузере")
    p_web.add_argument("--port", "-p", type=int, default=8000, help="Порт сервера (по умолчанию 8000)")

    # all (web + bot + scheduler)
    subparsers.add_parser("all", help="Запустить всё сразу: Веб-Дашборд + Telegram Bot + Планировщик")

    args = parser.parse_args()

    if args.command == "auth":
        handle_auth()
    elif args.command == "browser-login":
        handle_browser_login()
    elif args.command == "parse":
        handle_parse(
            query=args.query,
            count=args.count,
            area=args.area,
            experience=args.experience,
            only_with_salary=settings.SEARCH_ONLY_WITH_SALARY,
        )
    elif args.command == "analyze":
        handle_analyze()
    elif args.command == "web":
        run_web_dashboard(port=args.port)
    elif args.command == "bot":
        asyncio.run(run_bot_and_scheduler())
    elif args.command == "all":
        # Запускаем Web-сервер в отдельном потоке, а бота в главном
        t = threading.Thread(target=run_web_dashboard, kwargs={"port": 8000}, daemon=True)
        t.start()
        asyncio.run(run_bot_and_scheduler())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
