import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from playwright.async_api import async_playwright, Page, BrowserContext

from src.config import settings
from src.analyzer.matcher import AIResumeAnalyzer

logger = logging.getLogger(__name__)


class PlaywrightFormSolver:
    """
    Модуль автоматического прохождения тестов и опросников работодателя через Playwright + AI.
    Используется, когда обычный API-отклик блокируется обязательным опросником/тестом.
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.analyzer = AIResumeAnalyzer()
        self.session_file = settings.DATA_DIR / "browser_state.json"

    async def solve_and_apply(
        self,
        vacancy_url: str,
        cover_letter: str = "",
        user_cookies: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Открывает страницу вакансии, нажимает «Откликнуться», находит опросники/тесты,
        отвечает на них с помощью LLM и отправляет форму.
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=self.headless,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )

            context: BrowserContext = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
            )

            # Если есть сохраненное состояние кук
            if self.session_file.exists():
                try:
                    await context.storage_state(path=str(self.session_file))
                except Exception:
                    pass

            if user_cookies:
                await context.add_cookies(user_cookies)

            page: Page = await context.new_page()

            try:
                logger.info(f"🌐 [Playwright] Переход на вакансию: {vacancy_url}")
                await page.goto(vacancy_url, timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(1.5)

                # Ищем кнопку "Откликнуться"
                apply_button = page.locator(
                    '[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"], a:has-text("Откликнуться"), button:has-text("Откликнуться")'
                ).first

                if not await apply_button.count():
                    return {
                        "success": False,
                        "error": "apply_button_not_found",
                        "message": "Кнопка 'Откликнуться' не найдена (возможно, уже откликнулись или вакансия в архиве).",
                    }

                await apply_button.click()
                await asyncio.sleep(2)

                # Проверяем, открылся ли опросник / модалка
                modal = page.locator('[data-qa*="response-popup"], div[class*="popup"], div[role="dialog"]').first

                questions_answered = []
                if await modal.count():
                    logger.info("📋 [Playwright] Обнаружен опросник работодателя, запускаю AI-заполнение...")
                    questions_answered = await self._handle_questions(page, modal)

                # Ищем поле для сопроводительного письма (если открылось)
                letter_field = page.locator(
                    'textarea[data-qa="vacancy-response-letter-text"], textarea[name="text"], textarea[placeholder*="сопроводительное"]'
                ).first
                if await letter_field.count() and cover_letter:
                    await letter_field.fill(cover_letter)
                    await asyncio.sleep(0.5)

                # Нажимаем финальную кнопку отправки
                submit_button = page.locator(
                    '[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-submit"], button:has-text("Отправить отклик"), button:has-text("Откликнуться")'
                ).first

                if await submit_button.count():
                    await submit_button.click()
                    await asyncio.sleep(2)

                # Сохраняем сессию
                await context.storage_state(path=str(self.session_file))

                return {
                    "success": True,
                    "message": "Отклик успешно подан через браузер!",
                    "questions_answered": questions_answered,
                }

            except Exception as e:
                logger.error(f"❌ [Playwright] Ошибка при автоотклике: {e}")
                # Скриншот для отладки
                screenshot_path = settings.DATA_DIR / "error_playwright.png"
                await page.screenshot(path=str(screenshot_path))
                return {
                    "success": False,
                    "error": str(e),
                    "message": f"Ошибка браузера: {e}. Скриншот сохранен в {screenshot_path.name}",
                }
            finally:
                await browser.close()

    async def _handle_questions(self, page: Page, modal) -> List[Dict[str, str]]:
        """Поиск вопросов в модальном окне и их решение через AI."""
        answered = []
        resume_text = self.analyzer.load_resume_text()

        # Ищем все текстовые поля внутри опросника
        text_inputs = modal.locator('textarea, input[type="text"]')
        count = await text_inputs.count()

        for i in range(count):
            inp = text_inputs.nth(i)
            # Ищем текст вопроса рядом с полем
            parent = inp.locator("xpath=..")
            q_text = await parent.text_content() or f"Вопрос #{i+1}"
            q_text = q_text.strip()[:200]

            # Генерируем ответ через LLM
            answer = self._generate_answer(question=q_text, resume_text=resume_text)
            await inp.fill(answer)
            await asyncio.sleep(0.3)
            answered.append({"question": q_text, "answer": answer})

        # Ищем радиокнопки или чекбоксы
        radio_groups = modal.locator('fieldset, div[class*="radio-group"]')
        radio_count = await radio_groups.count()
        for i in range(radio_count):
            group = radio_groups.nth(i)
            first_option = group.locator('input[type="radio"], label').first
            if await first_option.count():
                await first_option.click()
                await asyncio.sleep(0.2)

        return answered

    def _generate_answer(self, question: str, resume_text: str) -> str:
        """Генерация краткого ответа на конкретный вопрос работодателя."""
        if not self.analyzer.client:
            return "Да, имею соответствующий практический опыт."

        prompt = (
            f"Работодатель в анкете отклика задает вопрос: '{question}'.\n"
            f"Дай четкий, уверенный и краткий ответ (1-2 предложения) от лица кандидата на основе резюме:\n"
            f"{resume_text}"
        )

        try:
            resp = self.analyzer.client.chat.completions.create(
                model=self.analyzer.model,
                messages=[
                    {"role": "system", "content": "Ты кандидат, отвечающий на вопросы работодателя в анкете."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=150,
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return "Да, есть опыт реализации аналогичных задач."
