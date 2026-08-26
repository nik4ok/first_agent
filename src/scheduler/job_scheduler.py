import asyncio
import logging
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.config import settings
from src.parser import HHClient, ExcelStorage
from src.analyzer import AIResumeAnalyzer
from src.responder import NegotiationTracker
from src.responder.autopilot import get_autopilot

logger = logging.getLogger(__name__)


class JobHuntingScheduler:
    """
    Фоновый планировщик задач:
    1. Периодический автопоиск и скоринг новых вакансий.
    2. Трекинг статусов откликов (приглашения/отказы) и уведомления в Telegram.
    """

    def __init__(self, bot: Optional[Bot] = None, chat_id: Optional[str] = None):
        self.scheduler = AsyncIOScheduler()
        self.bot = bot
        self.chat_id = chat_id or settings.TELEGRAM_CHAT_ID
        self.client = HHClient()
        self.storage = ExcelStorage()
        self.analyzer = AIResumeAnalyzer()
        self.tracker = NegotiationTracker()

    async def scan_and_analyze_job(self):
        """Задача: периодический сбор и анализ свежих вакансий."""
        logger.info("⏰ [Планировщик] Запуск периодического поиска вакансий...")
        try:
            vacancies = self.client.fetch_and_normalize_vacancies(
                text=settings.SEARCH_TEXT,
                area=settings.SEARCH_AREA,
                experience=settings.SEARCH_EXPERIENCE,
                search_period=settings.SEARCH_PERIOD,
                only_with_salary=settings.SEARCH_ONLY_WITH_SALARY,
                max_vacancies=20,
                fetch_full_description=True,
            )

            if not vacancies:
                logger.info("ℹ️ [Планировщик] Новых вакансий не обнаружено.")
                return

            added = self.storage.save_new_vacancies(vacancies)
            logger.info(f"📥 [Планировщик] Сохранено {added} новых вакансий.")

            # Анализируем новые
            for v in vacancies:
                v_id = v["id"]
                title = v["title"]
                employer = v["employer"]
                desc = v["description"]

                match_info = self.analyzer.analyze_match(title, desc, v.get("skills", ""))
                score = match_info.get("score", 50)
                pros = match_info.get("pros", "")
                cons = match_info.get("cons", "")
                cover_letter = self.analyzer.generate_cover_letter(title, employer, desc, match_info)

                self.storage.update_status(
                    vacancy_id=v_id,
                    status="ANALYZED",
                    match_score=score,
                    cover_letter=cover_letter,
                    notes=f"Плюсы: {pros} | Минусы: {cons}",
                )

                # Если есть Telegram Bot и подходящий score, отправляем алерт
                if self.bot and self.chat_id and score >= 60:
                    builder = InlineKeyboardBuilder()
                    builder.button(text="🚀 Откликнуться", callback_data=f"apply:{v_id}")
                    builder.button(text="❌ Пропустить", callback_data=f"skip:{v_id}")
                    builder.button(text="🔗 На сайт HH", url=v["url"])
                    builder.adjust(2, 1)

                    text = (
                        f"🔔 **Новая подходящая вакансия!**\n\n"
                        f"🎯 **{title}**\n"
                        f"🏢 {employer} ({v.get('city', '')})\n"
                        f"💰 Зарплата: {v.get('salary_str', 'Не указана')}\n"
                        f"📊 Match Score: **{score}%**\n\n"
                        f"💡 **AI Вердикт:** {pros}\n"
                        f"⚠️ **Нюансы:** {cons}\n\n"
                        f"✉️ **Черновик письма:**\n_{cover_letter[:400]}..._\n"
                    )
                    try:
                        await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=text,
                            reply_markup=builder.as_markup(),
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось отправить уведомление в TG: {e}")

        except Exception as e:
            logger.error(f"❌ [Планировщик] Ошибка в scan_and_analyze_job: {e}")

    async def track_negotiations_job(self):
        """Задача: периодический трекинг статусов откликов."""
        logger.info("🔍 [Планировщик] Проверка обновлений по откликам на HH.ru...")
        try:
            events = self.tracker.check_updates()
            if not events:
                return

            for ev in events:
                logger.info(f"🎉 Событие по отклику: {ev}")
                if self.bot and self.chat_id:
                    if ev["type"] == "INVITATION":
                        msg = (
                            f"🎉 **УРА! ВЫ ПОЛУЧИЛИ ПРИГЛАШЕНИЕ!** 🎉\n\n"
                            f"🏢 Компания: **{ev['employer']}**\n"
                            f"🎯 Вакансия: **{ev['vacancy_title']}**\n"
                            f"📊 Статус: `{ev['new_status']}`\n\n"
                            f"[👉 Перейти на HeadHunter к вакансии]({ev['url']})"
                        )
                    elif ev["type"] == "DISCARD":
                        msg = (
                            f"ℹ️ **Отказ по вакансии:**\n"
                            f"🏢 {ev['employer']} — *{ev['vacancy_title']}*\n"
                            f"Статус: `{ev['new_status']}`"
                        )
                    else:
                        msg = (
                            f"📝 **Обновление по отклику:**\n"
                            f"🏢 {ev['employer']} — *{ev['vacancy_title']}*\n"
                            f"Статус изменен на: `{ev['new_status']}`"
                        )

                    try:
                        await self.bot.send_message(
                            chat_id=self.chat_id,
                            text=msg,
                            parse_mode="Markdown",
                        )
                    except Exception as e:
                        logger.warning(f"Ошибка отправки TG алерта: {e}")

        except Exception as e:
            logger.error(f"❌ [Планировщик] Ошибка в track_negotiations_job: {e}")

    async def autopilot_tick_job(self):
        """Тик фоновой очереди автооткликов (50 вакансий за N часов и т.п.)."""
        try:
            await get_autopilot().tick()
        except Exception as e:
            logger.error("❌ [Планировщик] Ошибка тика автопилота: %s", e)

    def start(self, scan_interval_minutes: int = 30, tracking_interval_minutes: int = 10):
        """Запуск фонового расписания."""
        self.scheduler.add_job(self.scan_and_analyze_job, "interval", minutes=scan_interval_minutes, id="job_scanner")
        self.scheduler.add_job(self.track_negotiations_job, "interval", minutes=tracking_interval_minutes, id="job_tracker")
        self.scheduler.add_job(self.autopilot_tick_job, "interval", seconds=15, id="autopilot_tick")
        self.scheduler.start()
        logger.info(
            "🚀 Планировщик запущен (поиск: каждые %s мин, трекинг: каждые %s мин, автопилот: каждые 15 сек).",
            scan_interval_minutes,
            tracking_interval_minutes,
        )

    def stop(self):
        self.scheduler.shutdown()
