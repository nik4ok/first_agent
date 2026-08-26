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
                await asyncio.sleep(2.5)
                await self._confirm_foreign_country_warning(page)
                await asyncio.sleep(1.0)
                await self._confirm_foreign_country_warning(page)

                if "account/login" in page.url:
                    return {
                        "success": False,
                        "error": "session_expired",
                        "message": "Сессия браузера истекла. Пожалуйста, выполните повторный вход через «🔐 Вход в браузере».",
                    }

                blocker = await self._detect_blocker(page)
                if blocker:
                    await self._debug_screenshot(page, "apply_blocked.png")
                    return blocker

                questions_answered = []
                letter_filled = False
                dialog = page.locator(
                    '[data-qa="vacancy-response-popup"], [data-qa*="vacancy-response-popup"], '
                    '[role="dialog"], [data-qa*="modal"], [class*="magritte-modal"]'
                ).first
                dialog_visible = await dialog.count() > 0 and await dialog.is_visible()

                if dialog_visible:
                    logger.info("📄 [Playwright] Открылось модальное окно отклика на HH.ru")
                    letter_filled, questions_answered = await self._fill_response_dialog(
                        page, dialog, cover_letter
                    )

                if await self._is_applied(page):
                    if cover_letter and not letter_filled:
                        await self._send_cover_letter_in_chat(page, cover_letter)
                    try:
                        await context.storage_state(path=str(self.session_file))
                    except Exception:
                        pass
                    return {
                        "success": True,
                        "message": "Отклик успешно отправлен на HH.ru!",
                        "questions_answered": questions_answered,
                    }

                # Только перезагрузка страницы вакансии — надёжное подтверждение HH
                confirmed = await self._reload_and_confirm_applied(page, vacancy_url)
                if confirmed:
                    if cover_letter and not letter_filled:
                        await self._send_cover_letter_in_chat(page, cover_letter)
                    try:
                        await context.storage_state(path=str(self.session_file))
                    except Exception:
                        pass
                    return {
                        "success": True,
                        "message": "Отклик успешно отправлен на HH.ru!",
                        "questions_answered": questions_answered,
                    }

                await self._debug_screenshot(page, "apply_unconfirmed.png")
                blocker = await self._detect_blocker(page)
                if blocker:
                    return blocker
                return {
                    "success": False,
                    "error": "unconfirmed",
                    "message": (
                        "Кнопка «Откликнуться» нажата, но HH не показал статус «Вы откликнулись». "
                        "Частые причины: скрытое резюме, доп. анкета, капча. "
                        "Проверьте вакансию вручную и при необходимости заново сохраните сессию браузера."
                    ),
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

    async def _is_applied(self, page: Page) -> bool:
        markers = ("Вы откликнулись", "Отклик другим резюме", "Вы уже откликнулись")
        try:
            body = await page.inner_text("body")
            if any(marker in body for marker in markers):
                return True
        except Exception:
            pass
        loc = page.locator(
            '[data-qa="vacancy-response-link-view"], '
            '[data-qa="vacancy-response-subtitle-view"], '
            'button:has-text("Вы откликнулись"), a:has-text("Вы откликнулись"), '
            'span:has-text("Вы откликнулись"), '
            'button:has-text("Отклик другим резюме"), a:has-text("Отклик другим резюме")'
        )
        try:
            count = await loc.count()
            for i in range(min(count, 8)):
                item = loc.nth(i)
                if await item.is_visible():
                    return True
        except Exception:
            pass
        try:
            html = await page.content()
            return any(marker in html for marker in markers)
        except Exception:
            return False

    async def _reload_and_confirm_applied(self, page: Page, vacancy_url: str) -> bool:
        try:
            await page.goto(vacancy_url, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(3.5)
        except Exception as exc:
            logger.warning("Не удалось перезагрузить вакансию для проверки отклика: %s", exc)
            return False
        applied = await self._is_applied(page)
        if applied:
            logger.info("✅ [Playwright] HH подтвердил статус «Вы откликнулись» после перезагрузки")
        return applied

    async def _confirm_foreign_country_warning(self, page: Page) -> bool:
        """HH спрашивает подтверждение, если вакансия в другой стране, чем указано в резюме."""
        import re as _re

        clicked = False
        locators = [
            page.get_by_role("button", name=_re.compile(r"вс[её]\s+равно\s+откликнуться", _re.I)),
            page.locator(
                'button:has-text("Все равно откликнуться"), '
                'button:has-text("Всё равно откликнуться"), '
                '[role="button"]:has-text("равно откликнуться")'
            ),
        ]
        for loc in locators:
            try:
                btn = loc.first
                if await btn.count() and await btn.is_visible():
                    logger.info("🌍 [Playwright] Подтверждаю отклик на вакансию в другой стране")
                    await btn.click(timeout=5000)
                    await asyncio.sleep(2.0)
                    clicked = True
                    break
            except Exception as exc:
                logger.warning("Не удалось нажать «Все равно откликнуться»: %s", exc)
        return clicked

    async def _detect_blocker(self, page: Page) -> Optional[Dict[str, Any]]:
        hidden = page.locator('[data-qa="hidden-resume-warning"]')
        if await hidden.count() > 0 and await hidden.first.is_visible():
            continue_btn = page.locator(
                'button:has-text("Всё равно откликнуться"), button:has-text("Продолжить"), '
                'button:has-text("Сделать видимым")'
            ).first
            if await continue_btn.count() and await continue_btn.is_visible():
                try:
                    await continue_btn.click(timeout=4000)
                    await asyncio.sleep(1.5)
                except Exception:
                    pass
            if await hidden.count() > 0 and await hidden.first.is_visible():
                return {
                    "success": False,
                    "error": "hidden_resume",
                    "message": (
                        "HH не принял отклик: резюме скрыто от работодателя. "
                        "Откройте резюме на hh.ru и включите видимость (или «видно всем»)."
                    ),
                }

        extra_profile = page.locator(
            'button:has-text("Сохранить и продолжить"), '
            ':text("укажите категорию"), :text("Дополнительные сведения")'
        )
        if await extra_profile.count() > 0 and await extra_profile.first.is_visible():
            return {
                "success": False,
                "error": "extra_profile",
                "message": (
                    "HH открыл доп. анкету профиля (не форму отклика). "
                    "Автоотправка остановлена, чтобы не заполнять чужие поля."
                ),
            }

        captcha = page.locator('iframe[src*="captcha"], [data-qa*="captcha"], :text("Подтвердите, что вы не робот")')
        if await captcha.count() > 0 and await captcha.first.is_visible():
            return {
                "success": False,
                "error": "captcha",
                "message": "HH показал капчу. Нужен повторный вход через «Вход в браузере».",
            }
        return None

    async def _fill_response_dialog(self, page: Page, dialog, cover_letter: str) -> tuple:
        letter_filled = False
        questions_answered: List[Dict[str, str]] = []

        resume_items = dialog.locator(
            '[data-qa="resume-item"], [data-qa="applicant-resume-title"], '
            'input[name="resumeId"], [data-qa="resume-select-item"]'
        )
        if await resume_items.count() > 0 and await resume_items.first.is_visible():
            try:
                if settings.HH_RESUME_ID:
                    target_resume = dialog.locator(
                        f'[href*="{settings.HH_RESUME_ID}"], [data-qa*="{settings.HH_RESUME_ID}"]'
                    ).first
                    if await target_resume.count() and await target_resume.is_visible():
                        await target_resume.click(timeout=3000)
                    else:
                        await resume_items.first.click(timeout=3000)
                else:
                    await resume_items.first.click(timeout=3000)
                await asyncio.sleep(0.5)
            except Exception as e_res:
                logger.warning("Выбор резюме в модалке: %s", e_res)

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
                logger.warning("Не удалось вставить сопроводительное письмо: %s", e_letter)

        try:
            questions_answered = await self._handle_questions(page, dialog)
        except Exception as e_q:
            logger.warning("Ошибка заполнения опросника: %s", e_q)

        # Анкета не должна затирать уже вставленное письмо.
        if cover_letter and await letter_field.count() and await letter_field.is_visible():
            try:
                current = (await letter_field.input_value() or "").strip()
                if current != cover_letter.strip():
                    await letter_field.fill(cover_letter)
                    letter_filled = True
                    logger.info("📝 [Playwright] Письмо восстановлено после опросника")
            except Exception:
                pass

        await self._confirm_foreign_country_warning(page)
        submit_button = dialog.locator(
            'button[data-qa="vacancy-response-submit-popup"], '
            'button[data-qa*="response-submit"], button[data-qa="vacancy-response-submit"], '
            'button:has-text("Отправить отклик"), button:has-text("Откликнуться")'
        ).first
        if await submit_button.count() and await submit_button.is_visible():
            logger.info("🚀 [Playwright] Нажатие кнопки подтверждения отклика в модальном окне...")
            await submit_button.click()
            await asyncio.sleep(3.0)
            await self._confirm_foreign_country_warning(page)
        else:
            logger.warning("Кнопка «Отправить отклик» в модалке не найдена")
        return letter_filled, questions_answered

    async def send_letter_in_chat(self, vacancy_url: str, cover_letter: str) -> Dict[str, Any]:
        """Досылает готовое письмо в чат уже отправленного отклика. Без анкеты и без LLM."""
        if not cover_letter or not cover_letter.strip():
            return {"success": False, "error": "empty_letter"}
        if not self.session_file.exists():
            return {"success": False, "error": "no_browser_session"}

        async with async_playwright() as p:
            browser = await self._launch_browser(p)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                storage_state=str(self.session_file),
            )
            page = await context.new_page()
            page.set_default_timeout(25000)
            try:
                await page.goto(vacancy_url, timeout=30000, wait_until="domcontentloaded")
                await asyncio.sleep(2.0)
                sent = await self._send_cover_letter_in_chat(page, cover_letter.strip())
                try:
                    await context.storage_state(path=str(self.session_file))
                except Exception:
                    pass
                return {
                    "success": bool(sent),
                    "error": None if sent else "chat_not_found",
                    "message": "Письмо отправлено в чат" if sent else "Не удалось найти поле чата",
                }
            except Exception as exc:
                return {"success": False, "error": str(exc)}
            finally:
                await browser.close()

    async def _debug_screenshot(self, page: Page, filename: str) -> None:
        try:
            await page.screenshot(path=str(settings.DATA_DIR / filename), full_page=False)
        except Exception:
            pass

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

            # Только поле письма/сообщения, не любое textarea на странице.
            main_ta = page.locator(
                'textarea[data-qa*="letter"], textarea[data-qa="vacancy-response-letter-text"], '
                'textarea[data-qa="chatik-new-message-text"]'
            ).first
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

    async def _is_cover_letter_field(self, inp) -> bool:
        bits = " ".join(
            [
                (await inp.get_attribute("data-qa") or ""),
                (await inp.get_attribute("name") or ""),
                (await inp.get_attribute("id") or ""),
                (await inp.get_attribute("placeholder") or ""),
                (await inp.get_attribute("aria-label") or ""),
            ]
        ).lower()
        return any(token in bits for token in ("letter", "сопровод", "message", "cover"))

    async def _handle_questions(self, page: Page, modal) -> List[Dict[str, str]]:
        """Заполняет только явные вопросы работодателя. Поле письма не трогает и LLM не вызывает."""
        answered = []
        text_inputs = modal.locator('textarea, input[type="text"]')
        count = await text_inputs.count()

        for i in range(count):
            inp = text_inputs.nth(i)
            try:
                if await self._is_cover_letter_field(inp):
                    continue
                current = (await inp.input_value() or "").strip()
                if len(current) > 80:
                    continue
                qa = (await inp.get_attribute("data-qa") or "").lower()
                if "letter" in qa:
                    continue
                parent = inp.locator("xpath=..")
                q_text = (await parent.text_content() or "").strip()[:200]
                if len(q_text) < 8:
                    continue
                answer = (
                    "Да: 4 года в продуктовой аналитике (X5 Tech, Сбер), "
                    "A/B, SQL, Python, unit-экономика."
                )
                await inp.fill(answer)
                await asyncio.sleep(0.2)
                answered.append({"question": q_text, "answer": answer})
            except Exception:
                continue

        radio_groups = modal.locator('fieldset, div[class*="radio-group"]')
        radio_count = await radio_groups.count()
        for i in range(radio_count):
            group = radio_groups.nth(i)
            first_option = group.locator('input[type="radio"], label').first
            if await first_option.count():
                try:
                    await first_option.click()
                    await asyncio.sleep(0.2)
                except Exception:
                    pass

        return answered
