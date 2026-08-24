import asyncio
import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import (
    BotCommand,
    BotCommandScopeDefault,
    FSInputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

from src.config import settings, update_env_variable
from src.auth.hh_oauth import HHOAuthManager
from src.parser import HHClient, ExcelStorage
from src.analyzer import AIResumeAnalyzer
from src.responder import HHResponder

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class BotStates(StatesGroup):
    waiting_for_search_query = State()
    waiting_for_default_search = State()
    waiting_for_auth_code = State()
    waiting_for_resume_id = State()


def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Создает удобное постоянное меню-клавиатуру внизу экрана."""
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🔍 Поиск вакансий"),
        KeyboardButton(text="🤖 AI-анализ базы"),
    )
    builder.row(
        KeyboardButton(text="📊 Статистика"),
        KeyboardButton(text="📥 Скачать Excel"),
    )
    builder.row(
        KeyboardButton(text="📄 Мои резюме"),
        KeyboardButton(text="⚙️ Настройки"),
    )
    builder.row(
        KeyboardButton(text="🔐 HH Авторизация"),
        KeyboardButton(text="❓ Помощь"),
    )
    return builder.as_markup(resize_keyboard=True, persistent=True)


def get_main_inline_menu() -> InlineKeyboardMarkup:
    """Создает интерактивную панель управления."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Найти вакансии", callback_data="menu_search")
    builder.button(text="🤖 Запустить AI-анализ", callback_data="menu_analyze")
    builder.button(text="📊 Статистика", callback_data="menu_stats")
    builder.button(text="📥 Скачать Excel", callback_data="menu_excel")
    builder.button(text="📄 Выбрать резюме", callback_data="menu_resumes")
    builder.button(text="⚙️ Настройки поиска", callback_data="menu_settings")
    builder.button(text="🔐 Авторизация в HH", callback_data="menu_auth")
    builder.button(text="📝 Текст резюме", callback_data="menu_resume_text")
    builder.adjust(2, 2, 2, 2)
    return builder.as_markup()


def get_settings_inline_menu() -> InlineKeyboardMarkup:
    """Инлайн-меню настроек."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Изменить поисковый запрос", callback_data="set_query_prompt")
    builder.button(text="🌍 Регион (РФ / Москва / СПб)", callback_data="toggle_area")
    builder.button(text="⏳ Опыт работы", callback_data="toggle_exp")
    builder.button(text="💰 Только с зарплатой (Вкл/Выкл)", callback_data="toggle_salary")
    builder.button(text="🔙 Назад в меню", callback_data="menu_back")
    builder.adjust(1, 1, 1, 1, 1)
    return builder.as_markup()


async def setup_bot_commands(bot: Bot):
    """Регистрирует подсказки команд в интерфейсе Telegram (кнопка Меню /)."""
    commands = [
        BotCommand(command="start", description="🏠 Главное меню и статус"),
        BotCommand(command="menu", description="📱 Открыть панель управления"),
        BotCommand(command="search", description="🔍 Поиск вакансий (<запрос>)"),
        BotCommand(command="analyze", description="🤖 Запустить AI-анализ базы"),
        BotCommand(command="stats", description="📊 Статистика базы вакансий"),
        BotCommand(command="excel", description="📥 Получить Excel файл"),
        BotCommand(command="resumes", description="📄 Список и выбор резюме"),
        BotCommand(command="settings", description="⚙️ Настройки и фильтры"),
        BotCommand(command="auth", description="🔐 Авторизация в HeadHunter"),
        BotCommand(command="help", description="❓ Справка по командам"),
    ]
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    except Exception as e:
        logger.warning(f"Не удалось установить команды бота: {e}")


def create_bot_app() -> Optional[tuple[Dispatcher, Bot]]:
    token = settings.TELEGRAM_BOT_TOKEN
    if not token or token == "your_tg_bot_token":
        logger.warning("TELEGRAM_BOT_TOKEN не задан в .env! Запуск бота в Telegram отложен.")
        return None

    session = None
    if settings.TELEGRAM_PROXY:
        logger.info(f"Используется прокси для Telegram: {settings.TELEGRAM_PROXY}")
        session = AiohttpSession(proxy=settings.TELEGRAM_PROXY)

    bot = Bot(token=token, session=session)
    dp = Dispatcher()

    auth_mgr = HHOAuthManager()
    storage = ExcelStorage()
    analyzer = AIResumeAnalyzer()
    responder = HHResponder(auth_mgr)

    async def get_status_text() -> str:
        token_present = bool(auth_mgr.get_valid_access_token())
        status_hh = "🟢 Авторизован в HH.ru" if token_present else "🔴 Требуется авторизация в HH.ru (/auth)"
        current_resume = settings.HH_RESUME_ID or "⚠️ Не выбрано"
        current_search = settings.SEARCH_TEXT or "🔍 Любой запрос"
        
        area_names = {"113": "Вся Россия", "1": "Москва", "2": "Санкт-Петербург"}
        area_str = area_names.get(str(settings.SEARCH_AREA), f"Код {settings.SEARCH_AREA}")

        return (
            "🤖 **AI Job Agent — Интеллектуальный помощник HH.ru**\n\n"
            f"**Статус интеграции:**\n{status_hh}\n\n"
            f"📄 **Активное резюме:** `{current_resume}`\n"
            f"🎯 **Текущий запрос:** *{current_search}*\n"
            f"📍 **Регион поиска:** {area_str}\n"
            f"💼 **Опыт:** {settings.SEARCH_EXPERIENCE}\n\n"
            "Выберите действие в меню ниже или отправьте поисковый запрос текстом:"
        )

    # ==================== КОМАНДЫ ====================

    @dp.message(Command("start"))
    async def cmd_start(message: types.Message, state: FSMContext):
        await state.clear()
        text = await get_status_text()
        await message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=get_main_reply_keyboard(),
        )
        await message.answer(
            "⚡ **Быстрые действия:**",
            parse_mode="Markdown",
            reply_markup=get_main_inline_menu(),
        )

    @dp.message(Command("menu"))
    @dp.message(F.text == "📱 Меню")
    async def cmd_menu(message: types.Message):
        text = await get_status_text()
        await message.answer(
            text,
            parse_mode="Markdown",
            reply_markup=get_main_inline_menu(),
        )

    @dp.message(Command("help"))
    @dp.message(F.text == "❓ Помощь")
    async def cmd_help(message: types.Message):
        help_text = (
            "📖 **Руководство по использованию AI Job Agent:**\n\n"
            "🔍 **Поиск вакансий:**\n"
            "• Нажмите «🔍 Поиск вакансий» или напишите в чат: `/search <профессия>` (например: `/search Python Lead`).\n"
            "• Либо просто отправьте специальность в чат сообщением (например, *«Data Analyst»*).\n\n"
            "🤖 **AI-анализ и письма:**\n"
            "• Нажмите «🤖 AI-анализ базы» — агент рассчитает Match Score % для всех новых вакансий и сгенерирует сопроводительные письма.\n\n"
            "🚀 **Отклики:**\n"
            "• Под каждой карточкой вакансии есть кнопки: **«🚀 Откликнуться»** (отправка отклика с письмом) и **«❌ Пропустить»**.\n\n"
            "📊 **Excel и статистика:**\n"
            "• Нажмите «📥 Скачать Excel» — бот пришлет готовый файл со всеми статусами и текстами писем.\n\n"
            "🔐 **Авторизация:**\n"
            "• Нажмите «🔐 HH Авторизация» для подключения вашего профиля HeadHunter."
        )
        await message.answer(help_text, parse_mode="Markdown")

    @dp.message(Command("stats"))
    @dp.message(F.text == "📊 Статистика")
    async def cmd_stats(message: types.Message):
        df = storage.load_all()
        total = len(df)
        if total == 0:
            await message.answer("📊 База вакансий в Excel пока пуста. Запустите поиск через «🔍 Поиск вакансий».")
            return

        status_col = "Статус" if "Статус" in df.columns else "status"
        status_counts = df[status_col].value_counts().to_dict() if status_col in df.columns else {}

        text = (
            f"📊 **Статистика базы данных ({settings.EXCEL_PATH.name}):**\n\n"
            f"• Всего сохранено вакансий: **{total}**\n"
            f"• 🆕 Новых (NEW): **{status_counts.get('NEW', 0)}**\n"
            f"• 🧠 Проанализировано (ANALYZED): **{status_counts.get('ANALYZED', 0)}**\n"
            f"• 🚀 Отправлено откликов (APPLIED): **{status_counts.get('APPLIED', 0)}**\n"
            f"• 🎉 Приглашений от HR (INVITED): **{status_counts.get('INVITED', 0)}**\n"
            f"• 🚫 Пропущено (SKIPPED): **{status_counts.get('SKIPPED', 0)}**\n"
        )
        builder = InlineKeyboardBuilder()
        builder.button(text="📥 Скачать Excel файл", callback_data="menu_excel")
        builder.button(text="🤖 Запустить AI-анализ", callback_data="menu_analyze")
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    @dp.message(Command("excel"))
    @dp.message(F.text == "📥 Скачать Excel")
    async def cmd_excel(message: types.Message):
        excel_path = settings.EXCEL_PATH
        if not excel_path.exists():
            await message.answer("⚠️ Файл Excel еще не создан. Сначала выполните поиск вакансий.")
            return

        doc = FSInputFile(str(excel_path), filename=f"HH_Vacancies_{excel_path.name}")
        await message.answer_document(doc, caption="📊 **Актуальная база вакансий в Excel**", parse_mode="Markdown")

    @dp.message(Command("analyze"))
    @dp.message(F.text == "🤖 AI-анализ базы")
    async def cmd_analyze(message: types.Message):
        df = storage.load_all()
        if df.empty:
            await message.answer("⚠️ База вакансий пуста. Сначала выполните «🔍 Поиск вакансий».")
            return

        status_col = "Статус" if "Статус" in df.columns else "status"
        id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
        title_col = "Название вакансии" if "Название вакансии" in df.columns else "title"
        comp_col = "Компания" if "Компания" in df.columns else "employer"
        desc_col = "Полное описание" if "Полное описание" in df.columns else "description"
        skills_col = "Ключевые навыки" if "Ключевые навыки" in df.columns else "skills"

        new_rows = df[df[status_col] == "NEW"]
        if new_rows.empty:
            await message.answer("✅ Все вакансии в базе уже проанализированы!")
            return

        msg_wait = await message.answer(f"🤖 Начинаю AI-анализ {len(new_rows)} новых вакансий...")

        analyzed_count = 0
        for _, row in new_rows.iterrows():
            v_id = str(row[id_col])
            title = str(row.get(title_col, ""))
            employer = str(row.get(comp_col, ""))
            desc = str(row.get(desc_col, ""))
            skills = str(row.get(skills_col, ""))

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
            analyzed_count += 1

        await msg_wait.edit_text(
            f"🎉 **AI-анализ успешно завершен!**\nПроанализировано вакансий: **{analyzed_count}**.\n\n"
            "Результаты и персонализированные письма сохранены в Excel.",
            parse_mode="Markdown",
        )

    @dp.message(Command("resumes"))
    @dp.message(F.text == "📄 Мои резюме")
    async def cmd_resumes(message: types.Message):
        try:
            resumes = auth_mgr.get_my_resumes()
            if not resumes:
                builder = InlineKeyboardBuilder()
                builder.button(text="🔐 Авторизоваться в HH", callback_data="menu_auth")
                await message.answer(
                    "⚠️ Резюме не найдены или аккаунт еще не авторизован.",
                    reply_markup=builder.as_markup(),
                )
                return

            await message.answer(
                "📄 **Ваши резюме на HeadHunter:**\nВыберите резюме, которое бот будет использовать для автооткликов:",
                parse_mode="Markdown",
            )

            for r in resumes:
                r_id = r.get("id")
                r_title = r.get("title", "Без названия")
                status = r.get("status", {}).get("name", "Не указан")
                is_active = (r_id == settings.HH_RESUME_ID)

                builder = InlineKeyboardBuilder()
                if is_active:
                    builder.button(text="✅ Выбрано для откликов", callback_data="none")
                else:
                    builder.button(text="🎯 Использовать это резюме", callback_data=f"select_resume:{r_id}")

                text = f"📋 **{r_title}**\n🆔 ID: `{r_id}`\nСтатус: {status}"
                await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

        except Exception as e:
            await message.answer(f"❌ Ошибка получения резюме: {e}\nУбедитесь, что вы прошли авторизацию (/auth).")

    @dp.message(Command("settings"))
    @dp.message(F.text == "⚙️ Настройки")
    async def cmd_settings(message: types.Message):
        area_names = {"113": "Вся Россия (113)", "1": "Москва (1)", "2": "Санкт-Петербург (2)"}
        area_str = area_names.get(str(settings.SEARCH_AREA), f"Код {settings.SEARCH_AREA}")
        
        salary_str = "Да (только с з/п)" if settings.SEARCH_ONLY_WITH_SALARY else "Все вакансии"

        text = (
            "⚙️ **Текущие настройки поиска:**\n\n"
            f"🔍 **Поисковый запрос:** *{settings.SEARCH_TEXT or 'Любой'}*\n"
            f"🌍 **Регион:** {area_str}\n"
            f"💼 **Опыт работы:** `{settings.SEARCH_EXPERIENCE}`\n"
            f"💰 **Зарплатный фильтр:** {salary_str}\n"
            f"📄 **ID резюме:** `{settings.HH_RESUME_ID or 'Не выбрано'}`\n\n"
            "Нажмите на нужный пункт для изменения:"
        )
        await message.answer(text, reply_markup=get_settings_inline_menu(), parse_mode="Markdown")

    @dp.message(Command("auth"))
    @dp.message(F.text == "🔐 HH Авторизация")
    async def cmd_auth(message: types.Message):
        auth_url = auth_mgr.get_authorization_url()
        builder = InlineKeyboardBuilder()
        builder.button(text="👉 Открыть страницу авторизации", url=auth_url)
        builder.button(text="⌨️ Ввести полученный код", callback_data="prompt_code")
        builder.adjust(1, 1)

        text = (
            "🔐 **Авторизация в HeadHunter:**\n\n"
            "1. Нажмите кнопку ниже и подтвердите доступ (кнопка **«Разрешить»**).\n"
            "2. Вас перенаправит на `https://hh.ru/?code=XXXXXX`\n"
            "3. Скопируйте код из адресной строки и отправьте его боту командой:\n"
            "`/code ВАШ_КОД`\n"
            "_или нажмите кнопку «Ввести полученный код»._"
        )
        await message.answer(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    @dp.message(Command("resume_text"))
    @dp.message(F.text == "📝 Текст резюме")
    async def cmd_resume_text(message: types.Message):
        text = analyzer.load_resume_text()
        preview = text[:1500]
        await message.answer(
            f"📄 **Текущий текст резюме для AI-анализа (`data/my_resume.txt`):**\n\n```\n{preview}\n```\n\n"
            f"_Вы можете отредактировать файл data/my_resume.txt в любой момент на компьютере._",
            parse_mode="Markdown",
        )

    # ==================== ДИАЛОГИ И ПОИСК ====================

    @dp.message(F.text == "🔍 Поиск вакансий")
    async def prompt_search_query(message: types.Message, state: FSMContext):
        builder = InlineKeyboardBuilder()
        if settings.SEARCH_TEXT:
            builder.button(text=f"🎯 Искать «{settings.SEARCH_TEXT}»", callback_data=f"quick_search:{settings.SEARCH_TEXT}")
        builder.button(text="🐍 Python Developer", callback_data="quick_search:Python Developer")
        builder.button(text="📊 Data Analyst", callback_data="quick_search:Data Analyst")
        builder.button(text="📈 Product Manager", callback_data="quick_search:Product Manager")
        builder.button(text="🔍 Системный аналитик", callback_data="quick_search:Системный аналитик")
        builder.adjust(1, 2, 2)

        await state.set_state(BotStates.waiting_for_search_query)
        await message.answer(
            "🔎 **Введите название должности или ключевые слова для поиска:**\n\n"
            "_(Например: `Product Lead`, `FastAPI Developer`, `Руководитель проекта` или выберите быстрый вариант ниже)_",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown",
        )

    async def execute_search(query: str, target_message: types.Message, max_count: int = 5):
        msg_wait = await target_message.answer(f"🔍 Ищу вакансии по запросу: *«{query}»*...", parse_mode="Markdown")
        client = HHClient()
        vacancies = client.fetch_and_normalize_vacancies(
            text=query,
            area=settings.SEARCH_AREA,
            experience=settings.SEARCH_EXPERIENCE,
            only_with_salary=settings.SEARCH_ONLY_WITH_SALARY,
            max_vacancies=max_count,
            fetch_full_description=True,
        )

        if not vacancies:
            await msg_wait.edit_text(f"По запросу *«{query}»* ничего не найдено.", parse_mode="Markdown")
            return

        storage.save_new_vacancies(vacancies)
        await msg_wait.edit_text(f"✅ Найдено **{len(vacancies)}** вакансий. Запускаю AI-скоринг и генерацию писем...", parse_mode="Markdown")

        for v in vacancies:
            v_id = v["id"]
            title = v["title"]
            employer = v["employer"]
            desc = v["description"]

            # AI Анализ соответствия
            match_res = analyzer.analyze_match(title, desc, v.get("skills", ""))
            score = match_res.get("score", 50)
            pros = match_res.get("pros", "")
            cons = match_res.get("cons", "")

            # Генерация сопроводительного письма
            cover_letter = analyzer.generate_cover_letter(title, employer, desc, match_res)

            # Обновление в Excel
            storage.update_status(
                vacancy_id=v_id,
                status="ANALYZED",
                match_score=score,
                cover_letter=cover_letter,
                notes=f"Плюсы: {pros} | Минусы: {cons}",
            )

            # Кнопки
            builder = InlineKeyboardBuilder()
            builder.button(text="🚀 Откликнуться", callback_data=f"apply:{v_id}")
            builder.button(text="❌ Пропустить", callback_data=f"skip:{v_id}")
            builder.button(text="🔗 На сайт HH", url=v["url"])
            builder.adjust(2, 1)

            card_text = (
                f"🎯 **{title}**\n"
                f"🏢 Компания: **{employer}** ({v.get('city', '')})\n"
                f"💰 Зарплата: **{v.get('salary_str', 'Не указана')}**\n"
                f"📊 Match Score: **{score}%**\n\n"
                f"💡 **AI Вердикт:** {pros}\n"
                f"⚠️ **Нюансы:** {cons}\n\n"
                f"✉️ **Черновик письма:**\n_{cover_letter}_\n"
            )
            await target_message.answer(card_text, reply_markup=builder.as_markup(), parse_mode="Markdown")

    @dp.message(Command("search"))
    async def cmd_search(message: types.Message, state: FSMContext):
        await state.clear()
        args = message.text.split(maxsplit=1)
        query = args[1].strip() if len(args) > 1 else (settings.SEARCH_TEXT or "IT специалист")
        await execute_search(query, message)

    @dp.message(StateFilter(BotStates.waiting_for_search_query))
    async def handle_search_query_state(message: types.Message, state: FSMContext):
        query = message.text.strip()
        await state.clear()
        await execute_search(query, message)

    @dp.message(StateFilter(BotStates.waiting_for_default_search))
    async def handle_default_search_state(message: types.Message, state: FSMContext):
        query = message.text.strip()
        await state.clear()
        update_env_variable("SEARCH_TEXT", query)
        settings.SEARCH_TEXT = query
        await message.answer(
            f"✅ **Основной поисковый запрос обновлен на:** *«{query}»*",
            reply_markup=get_main_inline_menu(),
            parse_mode="Markdown",
        )

    @dp.message(StateFilter(BotStates.waiting_for_auth_code))
    async def handle_auth_code_state(message: types.Message, state: FSMContext):
        code = message.text.strip()
        await state.clear()
        await process_auth_code(code, message)

    @dp.message(Command("code"))
    async def cmd_code(message: types.Message, state: FSMContext):
        await state.clear()
        args = message.text.split(maxsplit=1)
        if len(args) < 2:
            await message.answer("⚠️ Укажите код. Пример:\n`/code 12345ABCDE`", parse_mode="Markdown")
            return
        await process_auth_code(args[1].strip(), message)

    async def process_auth_code(code: str, message: types.Message):
        msg_wait = await message.answer("⏳ Обмениваю код на токен авторизации...")
        try:
            tokens = auth_mgr.exchange_code(code)
            resumes = auth_mgr.get_my_resumes()
            resume_info = ""
            if resumes:
                resume_info = "\n\n📄 **Ваши резюме:**\n" + "\n".join(
                    [f"• {r.get('title')} (`{r.get('id')}`)" for r in resumes[:3]]
                )
            await msg_wait.edit_text(
                f"🎉 **Авторизация успешна!** Токены сохранены.{resume_info}\n\n"
                "Нажмите «📄 Выбрать резюме» чтобы установить активное резюме для откликов.",
                reply_markup=get_main_inline_menu(),
                parse_mode="Markdown",
            )
        except Exception as e:
            await msg_wait.edit_text(f"❌ Ошибка авторизации: {e}")

    # Обработка произвольного текстового сообщения (если нет открытого стейта)
    @dp.message(F.text & ~F.text.startswith("/"))
    async def handle_free_text(message: types.Message, state: FSMContext):
        current_state = await state.get_state()
        if current_state:
            return
        query = message.text.strip()
        builder = InlineKeyboardBuilder()
        builder.button(text=f"🔍 Искать «{query}»", callback_data=f"quick_search:{query}")
        builder.button(text="⚙️ Сохранить как запрос по умолчанию", callback_data=f"save_default_query:{query}")
        builder.adjust(1, 1)
        await message.answer(
            f"Вы ввели: *«{query}»*.\nЧто сделать с этим запросом?",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown",
        )

    # ==================== CALLBACK QUERIES ====================

    @dp.callback_query(F.data == "menu_search")
    async def cb_menu_search(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await prompt_search_query(callback.message, state)

    @dp.callback_query(F.data.startswith("quick_search:"))
    async def cb_quick_search(callback: types.CallbackQuery, state: FSMContext):
        await state.clear()
        query = callback.data.split(":", 1)[1]
        await callback.answer(f"Ищу {query}...")
        await execute_search(query, callback.message)

    @dp.callback_query(F.data.startswith("save_default_query:"))
    async def cb_save_default_query(callback: types.CallbackQuery):
        query = callback.data.split(":", 1)[1]
        update_env_variable("SEARCH_TEXT", query)
        settings.SEARCH_TEXT = query
        await callback.answer("Запрос сохранен!")
        await callback.message.edit_text(
            f"✅ Поисковый запрос по умолчанию обновлен на *«{query}»*.",
            reply_markup=get_main_inline_menu(),
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data == "menu_analyze")
    async def cb_menu_analyze(callback: types.CallbackQuery):
        await callback.answer()
        await cmd_analyze(callback.message)

    @dp.callback_query(F.data == "menu_stats")
    async def cb_menu_stats(callback: types.CallbackQuery):
        await callback.answer()
        await cmd_stats(callback.message)

    @dp.callback_query(F.data == "menu_excel")
    async def cb_menu_excel(callback: types.CallbackQuery):
        await callback.answer()
        await cmd_excel(callback.message)

    @dp.callback_query(F.data == "menu_resumes")
    async def cb_menu_resumes(callback: types.CallbackQuery):
        await callback.answer()
        await cmd_resumes(callback.message)

    @dp.callback_query(F.data == "menu_settings")
    async def cb_menu_settings(callback: types.CallbackQuery):
        await callback.answer()
        await cmd_settings(callback.message)

    @dp.callback_query(F.data == "menu_auth")
    async def cb_menu_auth(callback: types.CallbackQuery):
        await callback.answer()
        await cmd_auth(callback.message)

    @dp.callback_query(F.data == "menu_resume_text")
    async def cb_menu_resume_text(callback: types.CallbackQuery):
        await callback.answer()
        await cmd_resume_text(callback.message)

    @dp.callback_query(F.data == "menu_back")
    async def cb_menu_back(callback: types.CallbackQuery):
        await callback.answer()
        text = await get_status_text()
        await callback.message.edit_text(
            text,
            reply_markup=get_main_inline_menu(),
            parse_mode="Markdown",
        )

    @dp.callback_query(F.data == "prompt_code")
    async def cb_prompt_code(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.set_state(BotStates.waiting_for_auth_code)
        await callback.message.answer("✏️ **Отправьте полученный код из адресной строки в ответном сообщении:**", parse_mode="Markdown")

    @dp.callback_query(F.data == "set_query_prompt")
    async def cb_set_query_prompt(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.set_state(BotStates.waiting_for_default_search)
        await callback.message.answer("✏️ **Введите новый поисковый запрос по умолчанию:**\n_(Например: Системный аналитик или Python Developer)_", parse_mode="Markdown")

    @dp.callback_query(F.data == "toggle_area")
    async def cb_toggle_area(callback: types.CallbackQuery):
        areas = ["113", "1", "2"]
        current = str(settings.SEARCH_AREA)
        next_idx = (areas.index(current) + 1) % len(areas) if current in areas else 0
        new_area = areas[next_idx]
        update_env_variable("SEARCH_AREA", new_area)
        settings.SEARCH_AREA = new_area
        await callback.answer("Регион изменен!")
        await cmd_settings(callback.message)

    @dp.callback_query(F.data == "toggle_exp")
    async def cb_toggle_exp(callback: types.CallbackQuery):
        exps = ["noExperience", "between1And3", "between3And6", "moreThan6"]
        current = settings.SEARCH_EXPERIENCE
        next_idx = (exps.index(current) + 1) % len(exps) if current in exps else 0
        new_exp = exps[next_idx]
        update_env_variable("SEARCH_EXPERIENCE", new_exp)
        settings.SEARCH_EXPERIENCE = new_exp
        await callback.answer("Опыт изменен!")
        await cmd_settings(callback.message)

    @dp.callback_query(F.data == "toggle_salary")
    async def cb_toggle_salary(callback: types.CallbackQuery):
        new_val = not settings.SEARCH_ONLY_WITH_SALARY
        update_env_variable("SEARCH_ONLY_WITH_SALARY", str(new_val).lower())
        settings.SEARCH_ONLY_WITH_SALARY = new_val
        await callback.answer("Фильтр зарплаты изменен!")
        await cmd_settings(callback.message)

    @dp.callback_query(F.data.startswith("select_resume:"))
    async def cb_select_resume(callback: types.CallbackQuery):
        r_id = callback.data.split(":")[1]
        update_env_variable("HH_RESUME_ID", r_id)
        settings.HH_RESUME_ID = r_id
        await callback.answer(f"Резюме {r_id} выбрано!")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(f"✅ **Резюме `{r_id}` сохранено как основное для откликов!**", parse_mode="Markdown")

    @dp.callback_query(F.data.startswith("apply:"))
    async def cb_apply(callback: types.CallbackQuery):
        v_id = callback.data.split(":")[1]
        await callback.answer("⏳ Отправка отклика...")

        df = storage.load_all()
        id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
        row = df[df[id_col].astype(str) == str(v_id)]
        letter = ""
        if not row.empty and "Сопроводительное письмо" in row.columns:
            letter = str(row["Сопроводительное письмо"].values[0])

        result = responder.apply(vacancy_id=v_id, resume_id=settings.HH_RESUME_ID or None, message=letter)
        if result.get("success"):
            storage.update_status(vacancy_id=v_id, status="APPLIED")
            await callback.message.edit_reply_markup(reply_markup=None)
            await callback.message.reply(f"✅ **Отклик успешно отправлен на HH.ru!** (ID {v_id})", parse_mode="Markdown")
        else:
            await callback.message.reply(f"❌ Не удалось откликнуться: {result.get('message')}")

    @dp.callback_query(F.data.startswith("skip:"))
    async def cb_skip(callback: types.CallbackQuery):
        v_id = callback.data.split(":")[1]
        storage.update_status(vacancy_id=v_id, status="SKIPPED")
        await callback.answer("Вакансия пропущена")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.reply(f"🚫 Вакансия {v_id} помечена как пропущенная в Excel.")

    return dp, bot


async def start_telegram_bot():
    app = create_bot_app()
    if not app:
        print("❌ Telegram Bot не может быть запущен: отсутствует TELEGRAM_BOT_TOKEN в .env")
        return
    dp, bot = app
    await setup_bot_commands(bot)
    print("🚀 Telegram Bot запущен и готов к работе...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(start_telegram_bot())

