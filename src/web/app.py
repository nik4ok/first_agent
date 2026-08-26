import asyncio
import io
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Dict, Any, List
import pandas as pd
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.config import settings, update_env_variable
from src.parser import HHClient, ExcelStorage, get_sent_log
from src.analyzer import AIResumeAnalyzer
from src.responder import HHResponder
from src.responder.autopilot import get_autopilot
from src.auth import HHOAuthManager
from src.auth.hh_oauth import parse_hh_resume_id

logger = logging.getLogger(__name__)

templates_dir = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

storage = ExcelStorage()
auth_mgr = HHOAuthManager()
analyzer = AIResumeAnalyzer()
responder = HHResponder(auth_mgr)

browser_login_state: Dict[str, Any] = {
    "playwright": None,
    "browser": None,
    "context": None,
}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Тикер автопилота живёт и при `python main.py web`, без Telegram-планировщика."""
    stop = asyncio.Event()

    async def _loop():
        while not stop.is_set():
            try:
                await get_autopilot().tick()
            except Exception:
                logger.exception("Сбой тика автопилота")
            try:
                await asyncio.wait_for(stop.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(_loop())
    try:
        seeded = get_sent_log().seed_from_known_sources()
        if seeded:
            logger.info("Журнал откликов: подтянуто %s записей из Excel/автопилота", seeded)
    except Exception:
        logger.exception("Не удалось сидировать журнал откликов")
    yield
    stop.set()
    task.cancel()


app = FastAPI(title="AI Job Agent Web Dashboard", lifespan=lifespan)


class SearchRequest(BaseModel):
    query: str
    count: int = 20
    area: str = "113"
    experience: Optional[str] = "all"
    period: Optional[str] = "30"
    order_by: str = "publication_time"
    only_with_salary: bool = False
    save_as_default: bool = False


class LetterUpdateRequest(BaseModel):
    letter: str


class ResumeSelectRequest(BaseModel):
    resume_id: str


class ResumeTextUpdateRequest(BaseModel):
    text: str


class AuthCodeRequest(BaseModel):
    code: str


class AutopilotRequest(BaseModel):
    min_score: int = 70
    max_count: int = 50
    duration_hours: float = 3.0
    delay_seconds: float = 90.0
    vacancy_ids: Optional[List[str]] = None
    mode: str = "review"
    keywords: str = ""


class ReviewLetterRequest(BaseModel):
    letter: Optional[str] = None


@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    """Главная страница дашборда."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/auth/url")
async def get_auth_url():
    """Получение ссылки для прохождения OAuth 2.0 в HeadHunter."""
    url = auth_mgr.get_authorization_url()
    return {"status": "ok", "url": url}


@app.post("/api/auth/submit-code")
async def submit_auth_code(req: AuthCodeRequest):
    """Обмен кода авторизации на токены доступа."""
    code = req.code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="Код авторизации не может быть пустым.")
    try:
        tokens = auth_mgr.exchange_code(code)
        resumes = []
        try:
            resumes = auth_mgr.get_my_resumes()
            if resumes and not settings.HH_RESUME_ID:
                update_env_variable("HH_RESUME_ID", resumes[0].get("id"))
                settings.HH_RESUME_ID = resumes[0].get("id")
        except Exception:
            pass
        return {"status": "ok", "message": "Авторизация успешно завершена!", "resumes": resumes}
    except Exception as e:
        logger.error(f"Ошибка обмена кода OAuth: {e}")
        raise HTTPException(status_code=400, detail=f"Ошибка авторизации: {e}")


@app.get("/api/browser-status")
async def get_browser_status():
    """Проверка наличия сохраненной браузерной сессии Playwright."""
    session_file = settings.DATA_DIR / "browser_state.json"
    is_active = session_file.exists()
    return {
        "is_logged_in": is_active,
        "session_file": str(session_file.name),
    }


@app.post("/api/browser-login-open")
async def browser_login_open():
    """Запуск браузера Chromium для входа в HeadHunter."""
    global browser_login_state
    session_file = settings.DATA_DIR / "browser_state.json"

    # Закрываем предыдущие экземпляры при наличии
    if browser_login_state["browser"]:
        try:
            await browser_login_state["browser"].close()
        except Exception:
            pass
    if browser_login_state["playwright"]:
        try:
            await browser_login_state["playwright"].stop()
        except Exception:
            pass

    try:
        from playwright.async_api import async_playwright
        p = await async_playwright().start()
        browser_login_state["playwright"] = p

        launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        browser = None
        chrome_macos = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if chrome_macos.exists():
            try:
                browser = await p.chromium.launch(executable_path=str(chrome_macos), headless=False, args=launch_args)
            except Exception as e_ch:
                logger.info(f"Не удалось открыть системный Chrome ({e_ch}), пробую стандартный chromium")

        if not browser:
            try:
                browser = await p.chromium.launch(headless=False, args=launch_args)
            except Exception as e_bundled:
                logger.info(f"Пробую запустить установленный системный Google Chrome: {e_bundled}")
                browser = await p.chromium.launch(channel="chrome", headless=False, args=launch_args)

        browser_login_state["browser"] = browser

        storage_state = str(session_file) if session_file.exists() else None
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            storage_state=storage_state,
        )
        browser_login_state["context"] = context
        page = await context.new_page()
        await page.goto("https://hh.ru/account/login", timeout=45000)
        return {
            "status": "ok",
            "message": "Окно браузера открыто на экране! Пожалуйста, выполните вход в свой аккаунт HeadHunter.",
        }
    except Exception as e:
        logger.error(f"Ошибка открытия браузера: {e}")
        raise HTTPException(status_code=500, detail=f"Не удалось открыть браузер: {e}")


@app.post("/api/browser-login-save")
async def browser_login_save():
    """Сохранение cookies и сессии после авторизации на hh.ru."""
    global browser_login_state
    session_file = settings.DATA_DIR / "browser_state.json"
    context = browser_login_state["context"]
    browser = browser_login_state["browser"]
    p = browser_login_state["playwright"]

    if not context:
        raise HTTPException(status_code=400, detail="Окно браузера не было открыто.")

    try:
        await context.storage_state(path=str(session_file))
        await browser.close()
        await p.stop()
        browser_login_state["context"] = None
        browser_login_state["browser"] = None
        browser_login_state["playwright"] = None
        return {
            "status": "ok",
            "message": "🎉 Сессия браузера успешно сохранена! Автоматические отклики через Playwright активны.",
        }
    except Exception as e:
        logger.error(f"Ошибка сохранения сессии браузера: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка сохранения сессии: {e}")


@app.get("/api/config")
async def get_config():
    """Получение текущих настроек и статуса профиля HH."""
    user_info = auth_mgr.get_user_info()
    return {
        "search_text": settings.SEARCH_TEXT,
        "search_area": settings.SEARCH_AREA,
        "search_experience": settings.SEARCH_EXPERIENCE,
        "search_only_with_salary": settings.SEARCH_ONLY_WITH_SALARY,
        "resume_id": settings.HH_RESUME_ID,
        "is_authorized": bool(auth_mgr.get_valid_access_token()),
        "user_info": user_info,
    }


@app.get("/api/resume-summary")
async def get_resume_summary():
    """Получение краткой сводки локального резюме пользователя."""
    summary = analyzer.get_resume_summary()
    user_info = auth_mgr.get_user_info()
    return {
        "status": "ok",
        "active_hh_resume_id": settings.HH_RESUME_ID,
        "summary": summary,
        "user_info": user_info,
    }


@app.post("/api/upload-resume-file")
async def upload_resume_file(file: UploadFile = File(...)):
    """Загрузка резюме из файла (PDF, DOCX, TXT) и мгновенный пересчет скоринга."""
    filename = file.filename.lower()
    content = await file.read()
    extracted_text = ""

    try:
        if filename.endswith(".pdf"):
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            pages_text = [page.extract_text() for page in reader.pages if page.extract_text()]
            extracted_text = "\n\n".join(pages_text).strip()
        elif filename.endswith(".docx"):
            import docx
            doc = docx.Document(io.BytesIO(content))
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            extracted_text = "\n\n".join(paragraphs).strip()
        elif filename.endswith(".txt") or filename.endswith(".md"):
            extracted_text = content.decode("utf-8", errors="ignore").strip()
        else:
            raise HTTPException(status_code=400, detail="Поддерживаются только форматы .pdf, .docx, .txt")

        if not extracted_text:
            raise HTTPException(status_code=400, detail="Не удалось извлечь текст из загруженного файла.")

        # Сохраняем в data/my_resume.txt
        settings.RESUME_PATH.write_text(extracted_text, encoding="utf-8")
        rescored = _rescore_vacancies()
        summary = analyzer.get_resume_summary()
        return {
            "status": "ok",
            "message": f"Файл {file.filename} успешно обработан! Пересчитано вакансий: {rescored}.",
            "summary": summary,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка парсинга файла резюме: {e}")
        raise HTTPException(status_code=500, detail=f"Ошибка обработки файла: {e}")


@app.post("/api/update-resume-text")
async def update_resume_text(req: ResumeTextUpdateRequest):
    """Обновление локального текста резюме и мгновенный пересчет скоринга всех вакансий."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Текст резюме не может быть пустым.")
    settings.RESUME_PATH.write_text(req.text.strip(), encoding="utf-8")
    _rescore_vacancies()
    return {"status": "ok", "summary": analyzer.get_resume_summary()}


def _match_notes(match_info: Dict[str, Any]) -> str:
    pros = match_info.get("pros", "")
    cons = match_info.get("cons", "")
    matching_skills = ", ".join(match_info.get("matching_skills", []))
    missing_skills = ", ".join(match_info.get("missing_skills", []))
    notes_str = f"Плюсы: {pros} | Минусы: {cons}"
    if matching_skills:
        notes_str += f" | Match Skills: {matching_skills}"
    if missing_skills:
        notes_str += f" | Missing Skills: {missing_skills}"
    return notes_str


def _rescore_vacancies() -> int:
    """Пересчёт Match Score по текущему резюме одним проходом записи в Excel."""
    df = storage.load_all()
    if df.empty:
        return 0

    id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
    title_col = "Название вакансии" if "Название вакансии" in df.columns else "title"
    desc_col = "Полное описание" if "Полное описание" in df.columns else "description"
    skills_col = "Ключевые навыки" if "Ключевые навыки" in df.columns else "skills"
    status_col = "Статус" if "Статус" in df.columns else "status"
    keep_status = {"APPLIED", "INVITED", "SKIPPED", "QUEUED"}

    updates = []
    for _, row in df.iterrows():
        v_id = str(row[id_col])
        title = str(row.get(title_col, ""))
        desc = str(row.get(desc_col, ""))
        skills = str(row.get(skills_col, ""))
        current_status = str(row.get(status_col, "ANALYZED") or "ANALYZED")

        match_info = analyzer.analyze_match(title, desc, skills)
        updates.append(
            {
                "id": v_id,
                "status": current_status if current_status in keep_status else "ANALYZED",
                "match_score": match_info.get("score", 50),
                "notes": _match_notes(match_info),
            }
        )
    return storage.update_rows(updates)


@app.post("/api/sync-hh-resume")
async def sync_hh_resume(req: Optional[ResumeSelectRequest] = None):
    """Синхронизация резюме с HeadHunter."""
    resume_id = (req.resume_id if req and req.resume_id else None) or settings.HH_RESUME_ID
    if not resume_id:
        resumes = auth_mgr.get_my_resumes()
        if resumes:
            resume_id = resumes[0].get("id")
            update_env_variable("HH_RESUME_ID", resume_id)
            settings.HH_RESUME_ID = resume_id

    if not resume_id:
        return {
            "status": "manual_prompt",
            "message": "Вставьте текст резюме в редактор",
            "summary": analyzer.get_resume_summary(),
        }

    try:
        formatted_text = auth_mgr.download_and_format_resume(resume_id)
        if formatted_text:
            settings.RESUME_PATH.write_text(formatted_text, encoding="utf-8")
        summary = analyzer.get_resume_summary()
        return {"status": "ok", "resume_id": resume_id, "summary": summary}
    except Exception as e:
        logger.info(f"HH API не отдало текст резюме напрямую ({e}), открываем редактор текста.")
        return {
            "status": "manual_prompt",
            "message": "HeadHunter защищает текст резюме от прямого скачивания. Вставьте текст в открывшемся окне.",
            "summary": analyzer.get_resume_summary(),
        }


@app.get("/api/my-resumes")
async def api_my_resumes():
    """Список резюме на HH для селектора в шапке."""
    items = []
    try:
        if auth_mgr.get_valid_access_token():
            items = auth_mgr.get_my_resumes()
    except Exception as e:
        logger.warning("Не удалось получить список резюме HH: %s", e)
    return {
        "status": "ok",
        "items": items,
        "active_resume_id": settings.HH_RESUME_ID,
    }


@app.post("/api/select-resume")
async def api_select_resume(req: ResumeSelectRequest):
    """Выбор активного резюме HH по ID или ссылке."""
    resume_id = parse_hh_resume_id(req.resume_id)
    if not resume_id or len(resume_id) < 8:
        raise HTTPException(status_code=400, detail="Некорректный ID или ссылка на резюме.")
    update_env_variable("HH_RESUME_ID", resume_id)
    settings.HH_RESUME_ID = resume_id
    try:
        formatted_text = auth_mgr.download_and_format_resume(resume_id)
        if formatted_text:
            settings.RESUME_PATH.write_text(formatted_text, encoding="utf-8")
            _rescore_vacancies()
    except Exception as e:
        logger.info("Текст резюме HH недоступен (%s), ID сохранён.", e)
    return {"status": "ok", "selected_id": resume_id, "summary": analyzer.get_resume_summary()}


@app.get("/api/vacancies")
async def get_vacancies():
    """Получение списка вакансий из Excel (свежие сверху)."""
    df = storage.load_all()
    if df.empty:
        return []

    mapping = {
        "ID Вакансии": "id",
        "Название вакансии": "title",
        "Компания": "employer",
        "Город": "city",
        "Зарплата": "salary_str",
        "Ключевые навыки": "skills",
        "Ссылка": "url",
        "Дата публикации": "published_at",
        "Полное описание": "description",
        "Статус": "status",
        "Score (%)": "match_score",
        "Сопроводительное письмо": "cover_letter",
        "Заметки / Резюме анализа": "notes",
    }
    
    df_clean = df.fillna("")
    result = []
    for _, row in df_clean.iterrows():
        item = {}
        for col_ru, col_en in mapping.items():
            if col_ru in row:
                item[col_en] = str(row[col_ru]) if row[col_ru] != "" else None
            elif col_en in row:
                item[col_en] = str(row[col_en]) if row[col_en] != "" else None
        result.append(item)

    # Самые свежие вакансии всегда в начале списка
    result.reverse()
    return result


@app.get("/api/stats")
async def get_stats():
    """Сводная статистика базы вакансий и откликов для дашборда."""
    df = storage.load_all()
    sent_count = get_sent_log().count()
    has_resume = settings.RESUME_PATH.exists() and len(settings.RESUME_PATH.read_text(encoding="utf-8").strip()) > 20
    if df.empty:
        return {
            "total": 0,
            "applied": sent_count,
            "applied_excel": 0,
            "skipped": 0,
            "analyzed": 0,
            "new": 0,
            "queued": 0,
            "invited": 0,
            "high_match": 0,
            "medium_match": 0,
            "low_match": 0,
            "avg_score": 0,
            "has_resume": has_resume,
        }

    status_col = "Статус" if "Статус" in df.columns else "status"
    score_col = "Score (%)" if "Score (%)" in df.columns else "match_score"

    scores = pd.to_numeric(df[score_col], errors="coerce").dropna()
    total = len(df)
    applied = sent_count
    skipped = len(df[df[status_col] == "SKIPPED"])
    analyzed = len(df[df[status_col] == "ANALYZED"])
    new_items = len(df[df[status_col] == "NEW"])

    high_match = len(scores[scores >= 70])
    med_match = len(scores[(scores >= 40) & (scores < 70)])
    low_match = len(scores[scores < 40])
    avg_score = round(float(scores.mean()), 1) if not scores.empty else 0

    return {
        "total": total,
        "applied": applied,
        "applied_excel": len(df[df[status_col] == "APPLIED"]) if not df.empty else 0,
        "skipped": skipped,
        "analyzed": analyzed,
        "new": new_items,
        "queued": len(df[df[status_col] == "QUEUED"]) if status_col in df.columns else 0,
        "invited": len(df[df[status_col] == "INVITED"]) if status_col in df.columns else 0,
        "high_match": high_match,
        "medium_match": med_match,
        "low_match": low_match,
        "avg_score": avg_score,
        "has_resume": has_resume,
    }


@app.get("/api/market-audit")
async def api_market_audit():
    """Комплексный аудит резюме против всей текущей базы спарсенных вакансий."""
    return analyzer.audit_market_competency()


@app.post("/api/search")
async def api_search(req: SearchRequest):
    """Сбор свежих вакансий по любому запросу с HeadHunter в Excel и мгновенный AI-скоринг."""
    client = HHClient()
    query = req.query.strip() or settings.SEARCH_TEXT or "IT"

    if req.save_as_default:
        update_env_variable("SEARCH_TEXT", query)
        if req.area:
            update_env_variable("SEARCH_AREA", req.area)
        if req.experience:
            update_env_variable("SEARCH_EXPERIENCE", req.experience)
        if req.period:
            update_env_variable("SEARCH_PERIOD", req.period)

    vacancies = client.fetch_and_normalize_vacancies(
        text=query,
        area=req.area,
        experience=req.experience,
        search_period=req.period,
        only_with_salary=req.only_with_salary,
        order_by=req.order_by,
        max_vacancies=req.count,
    )

    # Автоматически анализируем и генерируем письма для найденных вакансий
    for v in vacancies:
        match_info = analyzer.analyze_match(v["title"], v.get("description", ""), v.get("skills", ""))
        v["match_score"] = match_info.get("score", 50)
        matching_skills = ", ".join(match_info.get("matching_skills", []))
        missing_skills = ", ".join(match_info.get("missing_skills", []))
        pros = match_info.get("pros", "")
        cons = match_info.get("cons", "")

        notes_str = f"Плюсы: {pros} | Минусы: {cons}"
        if matching_skills:
            notes_str += f" | Match Skills: {matching_skills}"
        if missing_skills:
            notes_str += f" | Missing Skills: {missing_skills}"

        v["notes"] = notes_str
        v["cover_letter"] = analyzer.generate_cover_letter(v["title"], v["employer"], v.get("description", ""), match_info)
        v["status"] = "ANALYZED"

    added = storage.save_new_vacancies(vacancies)
    return {"status": "ok", "found": len(vacancies), "added": added}


@app.post("/api/analyze")
async def api_analyze():
    """Запуск AI анализа для всех вакансий в статусе NEW."""
    df = storage.load_all()
    if df.empty:
        return {"analyzed": 0}

    status_col = "Статус" if "Статус" in df.columns else "status"
    id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
    title_col = "Название вакансии" if "Название вакансии" in df.columns else "title"
    comp_col = "Компания" if "Компания" in df.columns else "employer"
    desc_col = "Полное описание" if "Полное описание" in df.columns else "description"
    skills_col = "Ключевые навыки" if "Ключевые навыки" in df.columns else "skills"

    new_rows = df[df[status_col] == "NEW"]
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
        matching_skills = ", ".join(match_info.get("matching_skills", []))
        missing_skills = ", ".join(match_info.get("missing_skills", []))
        
        notes_str = f"Плюсы: {pros} | Минусы: {cons}"
        if matching_skills:
            notes_str += f" | Match Skills: {matching_skills}"
        if missing_skills:
            notes_str += f" | Missing Skills: {missing_skills}"

        cover_letter = analyzer.generate_cover_letter(title, employer, desc, match_info)

        storage.update_status(
            vacancy_id=v_id,
            status="ANALYZED",
            match_score=score,
            cover_letter=cover_letter,
            notes=notes_str,
        )
        analyzed_count += 1

    return {"status": "ok", "analyzed": analyzed_count}


@app.post("/api/autopilot")
async def api_autopilot(req: AutopilotRequest):
    """Старт фоновой очереди откликов. HTTP сразу возвращается, тикер шлёт по расписанию."""
    session_file = settings.DATA_DIR / "browser_state.json"
    if not session_file.exists() and not auth_mgr.get_valid_access_token():
        raise HTTPException(
            status_code=400,
            detail="Сначала сохраните сессию браузера (кнопка «Вход в браузере») или пройдите OAuth.",
        )
    result = get_autopilot().start(
        min_score=req.min_score,
        max_count=req.max_count,
        duration_hours=req.duration_hours,
        delay_seconds=req.delay_seconds,
        vacancy_ids=req.vacancy_ids,
        mode=req.mode,
        keywords=req.keywords,
    )
    return result


@app.get("/api/autopilot/status")
async def api_autopilot_status():
    return get_autopilot().status()


@app.post("/api/autopilot/stop")
async def api_autopilot_stop():
    return get_autopilot().stop()


@app.post("/api/review/approve")
async def api_review_approve(req: ReviewLetterRequest):
    return await asyncio.to_thread(get_autopilot().approve, req.letter)


@app.post("/api/review/skip")
async def api_review_skip():
    return get_autopilot().skip()


@app.post("/api/review/regenerate")
async def api_review_regenerate():
    return await asyncio.to_thread(get_autopilot().regenerate_letter)


@app.post("/api/review/letter")
async def api_review_letter(req: ReviewLetterRequest):
    if not req.letter:
        raise HTTPException(status_code=400, detail="Пустое письмо")
    return get_autopilot().update_pending_letter(req.letter)


@app.get("/api/autopilot/status")
async def api_autopilot_status():
    return get_autopilot().status()


@app.post("/api/autopilot/stop")
async def api_autopilot_stop():
    return get_autopilot().stop()


@app.post("/api/apply/{vacancy_id}")
async def api_apply(vacancy_id: str):
    """Отправка отклика на вакансию."""
    df = storage.load_all()
    id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
    row = df[df[id_col].astype(str) == str(vacancy_id)]

    letter = ""
    title = ""
    employer = ""
    desc = ""
    skills = ""
    if not row.empty:
        rec = row.iloc[0]
        if "Сопроводительное письмо" in row.columns:
            letter = str(rec.get("Сопроводительное письмо") or "")
        title = str(rec.get("Название вакансии") or rec.get("title") or "")
        employer = str(rec.get("Компания") or rec.get("employer") or "")
        desc = str(rec.get("Полное описание") or rec.get("description") or "")
        skills = str(rec.get("Ключевые навыки") or rec.get("skills") or "")

    weak = (
        not letter
        or letter.lower() in {"nan", "none"}
        or len(letter) < 80
        or "буду рад" in letter.lower()
        or "опираюсь на" in letter.lower()
    )
    if weak and title:
        match_info = analyzer.analyze_match(title, desc, skills)
        letter = analyzer.generate_cover_letter(title, employer, desc, match_info)

    res = responder.apply(vacancy_id=vacancy_id, resume_id=settings.HH_RESUME_ID or None, message=letter)
    if res.get("success"):
        storage.update_status(vacancy_id=vacancy_id, status="APPLIED", cover_letter=letter)
    return res


@app.post("/api/skip/{vacancy_id}")
async def api_skip(vacancy_id: str):
    """Пометить вакансию как пропущенную."""
    success = storage.update_status(vacancy_id=vacancy_id, status="SKIPPED")
    return {"success": success}


@app.post("/api/letter/{vacancy_id}")
async def api_update_letter(vacancy_id: str, req: LetterUpdateRequest):
    """Обновление текста сопроводительного письма."""
    success = storage.update_status(vacancy_id=vacancy_id, cover_letter=req.letter)
    return {"success": success}


@app.get("/api/sent-applications")
async def api_sent_applications():
    """Журнал реально отправленных откликов (не статус APPLIED в vacancies.xlsx)."""
    items = get_sent_log().list_all()
    return {"count": len(items), "items": items}


@app.get("/api/download-excel")
async def download_excel():
    """Скачивание файла Excel."""
    if not settings.EXCEL_PATH.exists():
        raise HTTPException(status_code=404, detail="Файл Excel еще не создан.")
    return FileResponse(
        path=str(settings.EXCEL_PATH),
        filename="vacancies.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/download-sent")
async def download_sent():
    """Скачивание журнала реальных откликов."""
    path = get_sent_log().ensure_xlsx()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Журнал откликов ещё пуст.")
    return FileResponse(
        path=str(path),
        filename="sent_applications.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/clear-database")
async def api_clear_database():
    """Полная очистка локальной базы вакансий в Excel."""
    try:
        storage.clear_all()
        return {"status": "ok", "message": "База вакансий успешно очищена!"}
    except Exception as e:
        logger.error(f"Ошибка при очистке базы: {e}")
        raise HTTPException(status_code=500, detail=f"Не удалось очистить базу: {e}")


def run_web_dashboard(host: str = "127.0.0.1", port: int = 8000):
    """Запуск Uvicorn веб-сервера."""
    import uvicorn
    print(f"\n🌐 Запуск Веб-Дашборда: http://{host}:{port}")
    uvicorn.run("src.web.app:app", host=host, port=port, reload=False)
