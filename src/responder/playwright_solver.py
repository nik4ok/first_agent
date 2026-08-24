import asyncio
import logging
import os
from pathlib import Path
from typing import Dict, Any, List, Optional
from playwright.async_api import async_playwright, Page, BrowserContext, Browser

from src.config import settings
from src.analyzer.matcher import AIResumeAnalyzer

logger = logging.getLogger(__name__)


class PlaywrightFormSolver:
    """
    Модуль автоматического отклика на вакансии через браузер Playwright (Headless Chrome).
    Обеспечивает реальный клик «Откликнуться», прикрепление сопроводительного письма,
    автоматическое решение опросников/тестов и проверку фактического подтверждения отклика на hh.ru.
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.analyzer = AIResumeAnalyzer()
        self.session_file = settings.DATA_DIR / "browser_state.json"

    async def _launch_browser(self, p) -> Browser:
        """Надежный запуск браузера с приоритетом установленного Google Chrome на macOS."""
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ]

        # 1. Проверяем наличие системного Google Chrome на macOS
        chrome_macos_path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if chrome_macos_path.exists():
            try:
                return await p.chromium.launch(
                    executable_path=str(chrome_macos_path),
                    headless=self.headless,
                    args=launch_args,
                )
            except Exception as e_chrome:
                logger.info(f"Не удалось запустить системный Chrome ({e_chrome}), пробую bundled chromium")

        # 2. Пробуем стандартный bundled Chromium из Playwright
        try:
            return await p.chromium.launch(headless=self.headless, args=launch_args)
        except Exception as e_bundled:
            logger.info(f"Bundled Chromium не запустился: {e_bundled}. Пробую channel='chrome'")

        # 3. Fallback: channel="chrome"
        try:
            return await p.chromium.launch(channel="chrome", headless=self.headless, args=launch_args)
        except Exception as e_final:
            raise RuntimeError(
                f"Не удалось запустить браузер для отклика. "
                f"Убедитесь, что установлен Google Chrome или выполните './venv/bin/playwright install chromium'. Ошибка: {e_final}"
            )

    async def solve_and_apply(
        self,
        vacancy_url: str,
        cover_letter: str = "",
        user_cookies: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Открывает страницу вакансии в Playwright, нажимает «Откликнуться»,
        прикрепляет сопроводительное письмо, заполняет опросники и проверяет подтверждение отклика на HH.ru.
        """
        if not self.session_file.exists() and not user_cookies:
            return {
                "success": False,
                "error": "no_browser_session",
                "message": "Для автоматических откликов выполните вход в аккаунт на HH.ru в браузере (кнопка «🔐 Вход в браузере» в дашборде или команда 'python main.py browser-login').",
            }

        async with async_playwright() as p:
            browser = await self._launch_browser(p)
            context: BrowserContext = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                storage_state=str(self.session_file) if self.session_file.exists() else None,
            )

            if user_cookies:
                await context.add_cookies(user_cookies)

            page: Page = await context.new_page()
            page.set_default_timeout(25000)

            try:
                logger.info(f"🌐 [Playwright] Переход на вакансию: {vacancy_url}")
                await page.goto(vacancy_url, timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(2.0)

                # 1. Проверяем, не перенаправило ли сразу на страницу логина
                if "account/login" in page.url or "auth" in page.url:
                    return {
                        "success": False,
                        "error": "session_expired",
                        "message": "Сессия браузера истекла. Пожалуйста, выполните повторный вход через «🔐 Вход в браузере».",
                    }

                # 2. Проверяем, не был ли уже отправлен отклик ранее
                already_applied_loc = page.locator(
                    '[data-qa="vacancy-response-link-view"], button:has-text("Вы откликнулись"), a:has-text("Вы откликнулись"), span:has-text("Вы откликнулись")'
                )
                if await already_applied_loc.count() > 0 and await already_applied_loc.first.is_visible():
                    return {
                        "success": True,
                        "message": "Вы уже откликались на эту вакансию ранее.",
                        "already_applied": True,
                    }

                # 3. Ищем главную кнопку "Откликнуться" на странице вакансии
                apply_button = page.locator(
                    '[data-qa="vacancy-response-link-top"], [data-qa="vacancy-response-link-bottom"], '
                    '[data-qa="vacancy-response-link"], a[href*="vacancy_response"], '
                    'button[data-qa*="vacancy-response"], a:has-text("Откликнуться"), button:has-text("Откликнуться")'
                ).first

                if not await apply_button.count() or not await apply_button.is_visible():
                    # Проверяем еще раз статус отклика
                    if await already_applied_loc.count() > 0:
                        return {
                            "success": True,
                            "message": "Вы уже откликались на эту вакансию ранее.",
                            "already_applied": True,
                        }
                    return {
                        "success": False,
                        "error": "apply_button_not_found",
                        "message": "Кнопка 'Откликнуться' не найдена (возможно, вакансия закрыта или находится в архиве).",
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

                # 5. Проверяем: возможно, отклик отправился мгновенно (прямой отклик в 1 клик)
                if await already_applied_loc.count() > 0 and await already_applied_loc.first.is_visible():
                    logger.info("⚡ [Playwright] Прямой отклик в 1 клик зафиксирован")
                    if cover_letter:
                        logger.info("✉️ [Playwright] Отправка сопроводительного письма к прямому отклику...")
                        await self._send_cover_letter_in_chat(page, cover_letter)
                    try:
                        await context.storage_state(path=str(self.session_file))
                    except Exception:
                        pass
                    return {
                        "success": True,
                        "message": "Отклик успешно отправлен на HH.ru!",
                    }

                # 6. Если открылось модальное окно / форма отклика
                dialog = page.locator('[role="dialog"], [data-qa*="popup"], [data-qa*="vacancy-response-popup"], div[class*="popup"]').first
                questions_answered = []
                letter_filled = False

                if await dialog.count() > 0 and await dialog.is_visible():
                    logger.info("📄 [Playwright] Открылось модальное окно отклика на HH.ru")

                    # Проверяем, требуется ли выбрать резюме (только если видимы кликабельные элементы резюме)
                    resume_items = dialog.locator(
                        '[data-qa="resume-item"], [data-qa="applicant-resume-title"], '
                        'input[name="resumeId"], [data-qa="resume-select-item"]'
                    )
                    if await resume_items.count() > 0 and await resume_items.first.is_visible():
                        try:
                            if settings.HH_RESUME_ID:
                                target_resume = dialog.locator(f'[href*="{settings.HH_RESUME_ID}"], [data-qa*="{settings.HH_RESUME_ID}"]').first
                                if await target_resume.count() and await target_resume.is_visible():
                                    await target_resume.click(timeout=3000)
                                    await asyncio.sleep(0.5)
                                else:
                                    await resume_items.first.click(timeout=3000)
                                    await asyncio.sleep(0.5)
                            else:
                                await resume_items.first.click(timeout=3000)
                                await asyncio.sleep(0.5)
                        except Exception as e_res:
                            logger.warning(f"Выбор резюме в модалке: {e_res}")

                    # Раскрываем блок сопроводительного письма (если он скрыт под кнопку)
                    add_letter_btn = dialog.locator(
                        '[data-qa="vacancy-response-letter-toggle"], [data-qa*="letter-toggle"], '
                        'button:has-text("сопроводительное"), a:has-text("сопроводительное"), span:has-text("сопроводительное")'
                    ).first
                    if await add_letter_btn.count() and await add_letter_btn.is_visible():
                        try:
                            await add_letter_btn.click(timeout=3000)
                            await asyncio.sleep(0.5)
                        except Exception:
                            pass

                    # Вставляем сопроводительное письмо
                    letter_field = dialog.locator(
                        'textarea[data-qa="vacancy-response-popup-form-letter-input"], '
                        'textarea[data-qa="vacancy-response-letter-text"], '
                        'textarea[data-qa*="letter"], textarea[name="message"], textarea[name="text"], textarea'
                    ).first
                    if await letter_field.count() and await letter_field.is_visible() and cover_letter:
                        try:
                            await letter_field.fill(cover_letter)
                            await asyncio.sleep(0.5)
                            letter_filled = True
                            logger.info("📝 [Playwright] Сопроводительное письмо успешно вставлено в форму")
                        except Exception as e_letter:
                            logger.warning(f"Не удалось вставить сопроводительное письмо: {e_letter}")

                    # Обработка вопросов / тестов работодателя
                    try:
                        questions_answered = await self._handle_questions(page, dialog)
                    except Exception as e_q:
                        logger.warning(f"Ошибка заполнения опросника: {e_q}")

                    # Кликаем финальную кнопку отправки в модальном окне
                    submit_button = dialog.locator(
                        'button[data-qa="vacancy-response-submit-popup"], '
                        'button[data-qa*="response-submit"], button[data-qa*="submit"], '
                        'button:has-text("Отправить отклик"), button:has-text("Откликнуться"), '
                        '[type="submit"]'
                    ).first

                    if await submit_button.count() and await submit_button.is_visible():
                        logger.info("🚀 [Playwright] Нажатие кнопки подтверждения отклика в модальном окне...")
                        await submit_button.click()
                        await asyncio.sleep(3.0)
                    else:
                        logger.warning("Кнопка подтверждения отклика в модалке не найдена")

                # 7. Проверка фактического подтверждения отклика от HeadHunter
                # Проверяем, изменился ли статус на странице или закрылось ли окно без ошибок
                confirmed = False
                for _ in range(3):
                    if await already_applied_loc.count() > 0 and await already_applied_loc.first.is_visible():
                        confirmed = True
                        break
                    # Проверяем текст "Отклик отправлен"
                    success_msg = page.locator(':is(:text("Отклик отправлен"), :text("Ваш отклик отправлен"), :text("Вы откликнулись"))')
                    if await success_msg.count() > 0 and await success_msg.first.is_visible():
                        confirmed = True
                        break
                    # Если модалка закрылась и нет сообщений об ошибках
                    if await dialog.count() == 0 or not await dialog.is_visible():
                        confirmed = True
                        break
                    await asyncio.sleep(1.0)

                # Если отклик подтвержден, но письмо не попало в модалку, отправляем в чат
                if confirmed and cover_letter and not letter_filled:
                    await self._send_cover_letter_in_chat(page, cover_letter)

                # Проверяем, не отображается ли ошибка в модальном окне (например, видимость резюме, лимиты, капча)
                if await dialog.count() > 0 and await dialog.is_visible():
                    error_elements = dialog.locator('[data-qa*="error"], div[class*="error"], [role="alert"]')
                    if await error_elements.count() > 0 and await error_elements.first.is_visible():
                        try:
                            err_txt = await error_elements.first.inner_text()
                            if err_txt.strip():
                                return {
                                    "success": False,
                                    "error": "hh_restriction",
                                    "message": f"Ограничение HeadHunter: {err_txt.strip()}",
                                }
                        except Exception:
                            pass

                if confirmed:
                    # Сохраняем обновленную сессию
                    try:
                        await context.storage_state(path=str(self.session_file))
                    except Exception:
                        pass

                    return {
                        "success": True,
                        "message": "Отклик успешно отправлен на HH.ru!",
                        "questions_answered": questions_answered,
                    }
                else:
                    return {
                        "success": False,
                        "error": "unconfirmed",
                        "message": "Кнопка отклика была нажата, но HeadHunter не вернул подтверждение. Проверьте статус вакансии на сайте.",
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

    async def _send_cover_letter_in_chat(self, page: Page, cover_letter: str) -> bool:
        """
        Отправляет сопроводительное письмо в чат HeadHunter, если вакансия применилась в 1 клик
        или если блок письма был скрыт.
        """
        if not cover_letter or not cover_letter.strip():
            return False
        try:
            # 1. Проверяем кнопку "Добавить сопроводительное" на основной странице
            add_btn = page.locator(':is(button, a, span, div):has-text("Добавить сопроводительное"), :is(button, a, span, div):has-text("Написать сопроводительное")').first
            if await add_btn.count() and await add_btn.is_visible():
                try:
                    await add_btn.click(timeout=3000)
                    await asyncio.sleep(1.0)
                except Exception:
                    pass

            # 2. Проверяем textarea на основной странице (может появиться после клика)
            main_ta = page.locator('textarea[data-qa*="letter"], textarea[data-qa*="message"], textarea').first
            if await main_ta.count() and await main_ta.is_visible():
                await main_ta.fill(cover_letter)
                await asyncio.sleep(0.5)
                send_btn = page.locator('button[data-qa*="submit"], button[data-qa*="send"], button:has-text("Отправить")').first
                if await send_btn.count() and await send_btn.is_visible():
                    await send_btn.click(timeout=3000)
                    await asyncio.sleep(1.5)
                    logger.info("💌 [Playwright] Сопроводительное письмо отправлено через форму страницы")
                    return True
                else:
                    await main_ta.press("Enter")
                    await asyncio.sleep(1.5)
                    logger.info("💌 [Playwright] Сопроводительное письмо отправлено по нажатию Enter")
                    return True

            # 3. Открываем виджет чата HeadHunter (chatik.hh.ru), если он доступен
            chat_btn = page.locator('button:has-text("Чат"), a:has-text("Чат"), [data-qa*="chat"]').first
            if await chat_btn.count() and await chat_btn.is_visible():
                try:
                    await chat_btn.click(timeout=3000)
                    await asyncio.sleep(2.0)
                except Exception:
                    pass

            # 4. Проверяем фреймы страницы на наличие виджета чата
            for f in page.frames:
                if "chatik.hh.ru" in f.url:
                    # Кликаем на верхний чат в списке
                    chat_link = f.locator('[data-qa*="chatik-open-chat"]').first
                    if await chat_link.count() and await chat_link.is_visible():
                        await chat_link.click(timeout=3000)
                        await asyncio.sleep(1.0)

                    # Если в чате есть кнопка "Добавить сопроводительное"
                    add_action = f.locator('[data-qa="chatik-chat-message-applicant-action"], :is(button, a, span):has-text("Добавить сопроводительное")').first
                    if await add_action.count() and await add_action.is_visible():
                        await add_action.click(timeout=3000)
                        await asyncio.sleep(0.5)

                    # Поле ввода текста сообщения в чате
                    chat_ta = f.locator('textarea[data-qa="chatik-new-message-text"], textarea, [contenteditable="true"]').first
                    if await chat_ta.count() and await chat_ta.is_visible():
                        await chat_ta.fill(cover_letter)
                        await asyncio.sleep(0.5)
                        send_msg_btn = f.locator('button[data-qa="chatik-do-send-message"], button[aria-label*="Отправить"]').first
                        if await send_msg_btn.count() and await send_msg_btn.is_visible():
                            await send_msg_btn.click(timeout=3000)
                            await asyncio.sleep(1.5)
                            logger.info("💌 [Playwright] Сопроводительное письмо успешно отправлено работодателю в чат!")
                            return True
                        else:
                            await chat_ta.press("Enter")
                            await asyncio.sleep(1.5)
                            logger.info("💌 [Playwright] Сопроводительное письмо отправлено в чат по Enter!")
                            return True
        except Exception as e:
            logger.debug(f"Попытка отправки письма в чат: {e}")
        return False

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
