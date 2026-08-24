import asyncio
import io
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
import pandas as pd
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from src.config import settings, update_env_variable
from src.parser import HHClient, ExcelStorage
from src.analyzer import AIResumeAnalyzer
from src.responder import HHResponder
from src.auth import HHOAuthManager

logger = logging.getLogger(__name__)

app = FastAPI(title="AI Job Agent Web Dashboard")

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
    delay_seconds: float = 2.0


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

        # Пересчитываем скоринг вакансий
        df = storage.load_all()
        if not df.empty:
            id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
            title_col = "Название вакансии" if "Название вакансии" in df.columns else "title"
            desc_col = "Полное описание" if "Полное описание" in df.columns else "description"
            skills_col = "Ключевые навыки" if "Ключевые навыки" in df.columns else "skills"

            for _, row in df.iterrows():
                v_id = str(row[id_col])
                title = str(row.get(title_col, ""))
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

                storage.update_status(
                    vacancy_id=v_id,
                    status=str(row.get("Статус", "ANALYZED")),
                    match_score=score,
                    notes=notes_str,
                )

        summary = analyzer.get_resume_summary()
        return {
            "status": "ok",
            "message": f"Файл {file.filename} успешно обработан!",
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

    # Автоматически пересчитываем скоринг и заметки для всех существующих вакансий под новое резюме
    df = storage.load_all()
    if not df.empty:
        id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
        title_col = "Название вакансии" if "Название вакансии" in df.columns else "title"
        desc_col = "Полное описание" if "Полное описание" in df.columns else "description"
        skills_col = "Ключевые навыки" if "Ключевые навыки" in df.columns else "skills"

        for _, row in df.iterrows():
            v_id = str(row[id_col])
            title = str(row.get(title_col, ""))
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

            storage.update_status(
                vacancy_id=v_id,
                status=str(row.get("Статус", "ANALYZED")),
                match_score=score,
                notes=notes_str,
            )

    return {"status": "ok", "summary": analyzer.get_resume_summary()}


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
    if df.empty:
        return {
            "total": 0,
            "applied": 0,
            "skipped": 0,
            "analyzed": 0,
            "new": 0,
            "high_match": 0,
            "medium_match": 0,
            "low_match": 0,
            "avg_score": 0,
            "has_resume": settings.RESUME_PATH.exists() and len(settings.RESUME_PATH.read_text(encoding="utf-8").strip()) > 20,
        }

    status_col = "Статус" if "Статус" in df.columns else "status"
    score_col = "Score (%)" if "Score (%)" in df.columns else "match_score"

    scores = pd.to_numeric(df[score_col], errors="coerce").dropna()
    total = len(df)
    applied = len(df[df[status_col] == "APPLIED"])
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
        "skipped": skipped,
        "analyzed": analyzed,
        "new": new_items,
        "high_match": high_match,
        "medium_match": med_match,
        "low_match": low_match,
        "avg_score": avg_score,
        "has_resume": settings.RESUME_PATH.exists() and len(settings.RESUME_PATH.read_text(encoding="utf-8").strip()) > 20,
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
    """
    Автономный запуск автооткликов:
    1. Находит все подходящие вакансии (Score >= min_score)
    2. Сортирует по максимальной релевантности
    3. Отправляет персональные отклики с задержкой (анти-спам)
    4. Возвращает подробный отчет
    """
    df = storage.load_all()
    if df.empty:
        return {"status": "ok", "applied": 0, "failed": 0, "message": "База вакансий пуста."}

    id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
    title_col = "Название вакансии" if "Название вакансии" in df.columns else "title"
    comp_col = "Компания" if "Компания" in df.columns else "employer"
    status_col = "Статус" if "Статус" in df.columns else "status"
    score_col = "Score (%)" if "Score (%)" in df.columns else "match_score"
    letter_col = "Сопроводительное письмо" if "Сопроводительное письмо" in df.columns else "cover_letter"
    desc_col = "Полное описание" if "Полное описание" in df.columns else "description"

    # Фильтруем только еще не отправленные вакансии
    unapplied = df[~df[status_col].isin(["APPLIED", "SKIPPED", "INVITED"])].copy()
    if unapplied.empty:
        return {"status": "ok", "applied": 0, "failed": 0, "message": "Нет новых неотправленных вакансий в базе."}

    # Преобразуем скор в число для сортировки
    unapplied["num_score"] = pd.to_numeric(unapplied[score_col], errors="coerce").fillna(0)
    
    # Фильтруем по min_score
    candidates = unapplied[unapplied["num_score"] >= req.min_score].sort_values(by="num_score", ascending=False)
    
    # Если мало вакансий со скором >= min_score, берем лучший топ
    if candidates.empty:
        candidates = unapplied.sort_values(by="num_score", ascending=False).head(req.max_count)
    else:
        candidates = candidates.head(req.max_count)

    applied_results = []
    failed_results = []

    for _, row in candidates.iterrows():
        v_id = str(row[id_col])
        title = str(row.get(title_col, ""))
        employer = str(row.get(comp_col, ""))
        score = int(row.get("num_score", 0))
        letter = str(row.get(letter_col, "") or "")
        desc = str(row.get(desc_col, "") or "")

        # Если письмо не было сгенерировано, генерируем на лету
        if not letter or len(letter) < 20:
            match_info = analyzer.analyze_match(title, desc, "")
            letter = analyzer.generate_cover_letter(title, employer, desc, match_info)

        # Отправляем отклик через API
        res = responder.apply(vacancy_id=v_id, resume_id=settings.HH_RESUME_ID or None, message=letter)
        if res.get("success"):
            storage.update_status(vacancy_id=v_id, status="APPLIED", cover_letter=letter)
            applied_results.append({"id": v_id, "title": title, "employer": employer, "score": score})
            logger.info(f"🚀 Автопилот успешно откликнулся на: [{v_id}] {title} ({employer}) - {score}%")
        else:
            err_msg = res.get("message", "Неизвестная ошибка")
            failed_results.append({"id": v_id, "title": title, "employer": employer, "error": err_msg})
            logger.warning(f"⚠️ Ошибка отклика на [{v_id}] {title}: {err_msg}")

        # Безопасная пауза против блокировок анти-спама
        if req.delay_seconds > 0:
            await asyncio.sleep(req.delay_seconds)

    return {
        "status": "ok",
        "applied_count": len(applied_results),
        "failed_count": len(failed_results),
        "applied_items": applied_results,
        "failed_items": failed_results,
        "message": f"Автопилот завершил работу: успешно отправлено {len(applied_results)} откликов из {len(candidates)} отобранных.",
    }


@app.post("/api/apply/{vacancy_id}")
async def api_apply(vacancy_id: str):
    """Отправка отклика на вакансию."""
    df = storage.load_all()
    id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
    row = df[df[id_col].astype(str) == str(vacancy_id)]
    
    letter = ""
    if not row.empty and "Сопроводительное письмо" in row.columns:
        letter = str(row["Сопроводительное письмо"].values[0])

    res = responder.apply(vacancy_id=vacancy_id, resume_id=settings.HH_RESUME_ID or None, message=letter)
    if res.get("success"):
        storage.update_status(vacancy_id=vacancy_id, status="APPLIED")
    return res


@app.post("/api/skip/{vacancy_id}")
async def api_skip(vacancy_id: str):
    """Пометить вакансию как пропущенную."""
    success = storage.update_status(vacancy_id=vacancy_id, status="SKIPPED")
    return {"success": success}


@app.post("/api/letter/{vacancy_id}")
async def api_update_letter(vacancy_id: str, req: LetterUpdateRequest):
    """Обновление текста сопроводительного письма."""
    success = storage.update_status(vacancy_id=vacancy_id, status="ANALYZED", cover_letter=req.letter)
    return {"success": success}


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
