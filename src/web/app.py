import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Request, HTTPException
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


class SearchRequest(BaseModel):
    query: str
    count: int = 10
    area: str = "113"
    experience: Optional[str] = "between1And3"
    only_with_salary: bool = False


class LetterUpdateRequest(BaseModel):
    letter: str


class ResumeSelectRequest(BaseModel):
    resume_id: str


class ResumeTextUpdateRequest(BaseModel):
    text: str


@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    """Главная страница дашборда."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/config")
async def get_config():
    """Получение текущих настроек."""
    return {
        "search_text": settings.SEARCH_TEXT,
        "search_area": settings.SEARCH_AREA,
        "search_experience": settings.SEARCH_EXPERIENCE,
        "search_only_with_salary": settings.SEARCH_ONLY_WITH_SALARY,
        "resume_id": settings.HH_RESUME_ID,
        "is_authorized": bool(auth_mgr.get_valid_access_token()),
    }


@app.get("/api/resume-summary")
async def get_resume_summary():
    """Получение краткой сводки локального резюме пользователя."""
    summary = analyzer.get_resume_summary()
    return {
        "status": "ok",
        "active_hh_resume_id": settings.HH_RESUME_ID,
        "summary": summary,
    }


@app.post("/api/update-resume-text")
async def update_resume_text(req: ResumeTextUpdateRequest):
    """Обновление локального текста резюме."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Текст резюме не может быть пустым.")
    settings.RESUME_PATH.write_text(req.text.strip(), encoding="utf-8")
    return {"status": "ok", "summary": analyzer.get_resume_summary()}


@app.get("/api/my-resumes")
async def get_my_resumes():
    """Получение списка резюме пользователя с HeadHunter."""
    try:
        resumes = auth_mgr.get_my_resumes()
        return {"status": "ok", "items": resumes, "active_resume_id": settings.HH_RESUME_ID}
    except Exception as e:
        return {"status": "error", "message": str(e), "items": []}


@app.post("/api/select-resume")
async def select_resume(req: ResumeSelectRequest):
    """Выбор активного резюме для откликов."""
    update_env_variable("HH_RESUME_ID", req.resume_id)
    settings.HH_RESUME_ID = req.resume_id
    return {"status": "ok", "selected_id": req.resume_id}


@app.get("/api/vacancies")
async def get_vacancies():
    """Получение списка вакансий из Excel."""
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

    return result


@app.post("/api/search")
async def api_search(req: SearchRequest):
    """Сбор свежих вакансий по любому запросу с HeadHunter в Excel."""
    client = HHClient()
    query = req.query.strip() or settings.SEARCH_TEXT or "IT"
    vacancies = client.fetch_and_normalize_vacancies(
        text=query,
        area=req.area,
        experience=req.experience,
        only_with_salary=req.only_with_salary,
        max_vacancies=req.count,
    )
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


def run_web_dashboard(host: str = "127.0.0.1", port: int = 8000):
    """Запуск Uvicorn веб-сервера."""
    import uvicorn
    print(f"\n🌐 Запуск Веб-Дашборда: http://{host}:{port}")
    uvicorn.run("src.web.app:app", host=host, port=port, reload=False)
