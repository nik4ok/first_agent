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
        Открывает страницу вакансии в Playwright Chromium, нажимает «Откликнуться»,
        вставляет сопроводительное письмо, заполняет опросники через AI и подтверждает отклик.
        """
        if not self.session_file.exists() and not user_cookies:
            return {
                "success": False,
                "error": "no_browser_session",
                "message": "Для автоматических откликов выполните вход в аккаунт на HH.ru в браузере (кнопка «🔐 Вход в браузере» в дашборде или команда 'python main.py browser-login').",
            }

        async with async_playwright() as p:
            launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
            try:
                browser = await p.chromium.launch(headless=self.headless, args=launch_args)
            except Exception as e_bundled:
                logger.info(f"Пробую системный Google Chrome: {e_bundled}")
                browser = await p.chromium.launch(channel="chrome", headless=self.headless, args=launch_args)

            context: BrowserContext = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                storage_state=str(self.session_file) if self.session_file.exists() else None,
            )

            if user_cookies:
                await context.add_cookies(user_cookies)

            page: Page = await context.new_page()

            try:
                logger.info(f"🌐 [Playwright] Переход на вакансию: {vacancy_url}")
                await page.goto(vacancy_url, timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(2.0)

                # 1. Проверяем, не перенаправило ли сразу на страницу логина
                if "account/login" in page.url:
                    return {
                        "success": False,
                        "error": "session_expired",
                        "message": "Сессия браузера истекла. Пожалуйста, выполните повторный вход через «🔐 Вход в браузере».",
                    }

                page_content = await page.content()

                # 2. Проверяем, не был ли уже отправлен отклик ранее
                already_applied = (
                    "Вы откликнулись" in page_content
                    or await page.locator(
                        '[data-qa="vacancy-response-link-view"], button:has-text("Вы откликнулись"), a:has-text("Вы откликнулись")'
                    ).count() > 0
                )
                if already_applied:
                    return {
                        "success": True,
                        "message": "Вы уже откликались на эту вакансию ранее.",
                        "already_applied": True,
                    }

                # 3. Ищем кнопку "Откликнуться"
                apply_button = page.locator(
                    '[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"], '
                    'a[href*="vacancy_response"], button[data-qa*="vacancy-response"], '
                    'a:has-text("Откликнуться"), button:has-text("Откликнуться")'
                ).first

                if not await apply_button.count():
                    # Проверяем еще раз, возможно отклик уже есть
                    if "Вы откликнулись" in await page.content():
                        return {
                            "success": True,
                            "message": "Вы уже откликались на эту вакансию ранее.",
                            "already_applied": True,
                        }
                    return {
                        "success": False,
                        "error": "apply_button_not_found",
                        "message": "Кнопка 'Откликнуться' не найдена (возможно, вакансия закрыта или в архиве).",
                    }

                await apply_button.click()
                await asyncio.sleep(2.0)

                # 4. Проверяем, не перекинуло ли на логин после клика
                if "account/login" in page.url:
                    return {
                        "success": False,
                        "error": "session_expired",
                        "message": "Сессия браузера истекла. Пожалуйста, выполните повторный вход через «🔐 Вход в браузере».",
                    }

                # 5. Если открылся выбор резюме (если у пользователя несколько резюме)
                resume_items = page.locator('[data-qa*="resume-item"], [data-qa*="applicant-resume"], div[data-qa*="resume"]')
                if await resume_items.count() > 0:
                    try:
                        # Если задан ID резюме, пробуем кликнуть по нему
                        if settings.HH_RESUME_ID:
                            target_resume = page.locator(f'[href*="{settings.HH_RESUME_ID}"], [data-qa*="{settings.HH_RESUME_ID}"]').first
                            if await target_resume.count():
                                await target_resume.click()
                                await asyncio.sleep(0.5)
                            else:
                                await resume_items.first.click()
                                await asyncio.sleep(0.5)
                        else:
                            await resume_items.first.click()
                            await asyncio.sleep(0.5)
                    except Exception:
                        pass

                # 6. Проверяем, появилась ли кнопка раскрытия сопроводительного письма
                letter_toggle = page.locator(
                    '[data-qa="vacancy-response-letter-toggle"], [data-qa*="letter-toggle"], '
                    'button:has-text("Написать сопроводительное"), a:has-text("Написать сопроводительное"), '
                    'span:has-text("Написать сопроводительное")'
                ).first
                if await letter_toggle.count():
                    try:
                        await letter_toggle.click()
                        await asyncio.sleep(0.5)
                    except Exception:
                        pass

                # 7. Ищем поле для ввода сопроводительного письма
                letter_field = page.locator(
                    'textarea[data-qa="vacancy-response-popup-form-letter-input"], '
                    'textarea[data-qa="vacancy-response-letter-text"], '
                    'textarea[data-qa*="letter"], textarea[name="message"], textarea[name="text"], textarea'
                ).first
                if await letter_field.count() and cover_letter:
                    try:
                        await letter_field.fill(cover_letter)
                        await asyncio.sleep(0.5)
                    except Exception as e_letter:
                        logger.warning(f"Не удалось вставить письмо: {e_letter}")

                # 8. Проверяем наличие модальных опросников / тестов
                modal = page.locator('[data-qa*="response-popup"], div[class*="popup"], div[role="dialog"]').first
                questions_answered = []
                if await modal.count():
                    try:
                        questions_answered = await self._handle_questions(page, modal)
                    except Exception as e_q:
                        logger.warning(f"Ошибка заполнения опросника: {e_q}")

                # 9. Ищем финальную кнопку отправки отклика
                submit_button = page.locator(
                    '[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-submit"], '
                    'button[data-qa*="response-submit"], button:has-text("Отправить отклик"), '
                    'button:has-text("Откликнуться")'
                ).first

                if await submit_button.count():
                    await submit_button.click()
                    await asyncio.sleep(2.5)

                # 10. Сохраняем обновленные cookies
                try:
                    await context.storage_state(path=str(self.session_file))
                except Exception:
                    pass

                return {
                    "success": True,
                    "message": "Отклик успешно подан через браузер!",
                    "questions_answered": questions_answered,
                }

            except Exception as e:
                logger.error(f"❌ [Playwright] Ошибка при автоотклике: {e}")
                screenshot_path = settings.DATA_DIR / "error_playwright.png"
                try:
                    await page.screenshot(path=str(screenshot_path))
                except Exception:
                    pass
                return {
                    "success": False,
                    "error": str(e),
                    "message": f"Ошибка браузера при отклике: {e}",
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
