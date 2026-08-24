import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, Optional, List
from openai import OpenAI

from src.config import settings

logger = logging.getLogger(__name__)


class AIResumeAnalyzer:
    """Анализ соответствия резюме требованиям вакансии и генерация сопроводительных писем."""

    def __init__(
        self,
        resume_path: Optional[Path] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.resume_path = resume_path or settings.RESUME_PATH
        self.api_key = api_key or settings.OPENAI_API_KEY
        self.base_url = base_url or settings.OPENAI_BASE_URL or None
        self.model = model or settings.OPENAI_MODEL

        if self.api_key:
            kwargs = {"api_key": self.api_key}
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self.client = OpenAI(**kwargs)
        else:
            self.client = None

    def load_resume_text(self) -> str:
        """Загрузка текста резюме из файла."""
        if self.resume_path.exists():
            text = self.resume_path.read_text(encoding="utf-8").strip()
            if text:
                return text
        return "Резюме не заполнено. Отредактируйте data/my_resume.txt."

    def extract_resume_keywords(self) -> List[str]:
        """Динамическое извлечение ключевых слов и технологий из текста резюме пользователя."""
        text = self.load_resume_text()
        # Ищем термины, написанные на латинице/кириллице (навыки, инструменты)
        words = re.findall(r"[A-Za-zА-Яа-я0-9\+\#\.\-]{2,}", text)
        stop_words = {"для", "или", "как", "это", "опыт", "лет", "года", "работа", "было", "что", "все", "при", "про", "разработка", "имя"}
        cleaned = [w.lower() for w in words if len(w) > 2 and w.lower() not in stop_words]
        return list(dict.fromkeys(cleaned))[:30]

    def analyze_match(self, vacancy_title: str, vacancy_desc: str, vacancy_skills: str = "") -> Dict[str, Any]:
        """
        Оценивает релевантность вакансии (0-100), возвращает рекомендации и решение.
        """
        resume_text = self.load_resume_text()

        if not self.client:
            return self._dynamic_match(resume_text, vacancy_title, vacancy_desc, vacancy_skills)

        system_prompt = (
            "Ты — профессиональный IT-рекрутер и карьерный консультант. "
            "Твоя задача — объективно оценить соответствие резюме кандидата описанию вакансии. "
            "Ответь строго в формате JSON со следующими полями:\n"
            "{\n"
            '  "score": <число от 0 до 100>,\n'
            '  "match_level": "HIGH" | "MEDIUM" | "LOW",\n'
            '  "matching_skills": ["навык1", "навык2"],\n'
            '  "missing_skills": ["навык1", "навык2"],\n'
            '  "pros": "краткие сильные стороны кандидата под эту роль",\n'
            '  "cons": "краткие риски или несоответствия",\n'
            '  "recommendation": "APPLY" | "MANUAL" | "SKIP"\n'
            "}"
        )

        user_prompt = (
            f"--- РЕЗЮМЕ КАНДИДАТА ---\n{resume_text}\n\n"
            f"--- ВАКАНСИЯ: {vacancy_title} ---\n"
            f"Ключевые навыки: {vacancy_skills}\n"
            f"Описание:\n{vacancy_desc[:2500]}"
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            result = json.loads(response.choices[0].message.content)
            return result
        except Exception as e:
            logger.error(f"Ошибка вызова LLM: {e}")
            return self._dynamic_match(resume_text, vacancy_title, vacancy_desc, vacancy_skills)

    def generate_cover_letter(
        self,
        vacancy_title: str,
        employer_name: str,
        vacancy_desc: str,
        match_info: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Генерирует лаконичное персонализированное сопроводительное письмо под конкретную вакансию.
        """
        resume_text = self.load_resume_text()

        if not self.client:
            return (
                f"Здравствуйте! Меня заинтересовала позиция {vacancy_title} в компании {employer_name}. "
                "Мой опыт и профессиональные навыки полностью соответствуют задачам и требованиям вашей роли. "
                "Буду рад познакомиться ближе и обсудить детали на интервью!"
            )

        prompt = (
            f"Напиши убедительное, живое и лаконичное сопроводительное письмо от кандидата для вакансии '{vacancy_title}' в компанию '{employer_name}'.\n\n"
            f"Требования к письму:\n"
            f"1. Без штампов и клише ('Я стрессоустойчивый коммуникабельный профессионал').\n"
            f"2. Персонализация: упомяни 2-3 ключевых навыка или задачи из описания вакансии, в которых у кандидата есть сильный опыт.\n"
            f"3. Длина: 2-3 коротких абзаца (до 700 символов).\n"
            f"4. Завершение с открытым призывом к диалогу/интервью.\n\n"
            f"Резюме кандидата:\n{resume_text}\n\n"
            f"Описание вакансии:\n{vacancy_desc[:2000]}"
        )

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": "Ты эксперт по написанию сопроводительных писем в IT."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.5,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            logger.error(f"Ошибка генерации письма: {e}")
            return (
                f"Здравствуйте! Заинтересовала вакансия {vacancy_title} в {employer_name}. "
                "Изучил стек и требования — мой практический опыт отлично подходит под задачи команды. Буду рад обсудить подробности на интервью!"
            )

    def extract_skills_from_text(self, text: str) -> List[str]:
        """Извлечение известных технических и профессиональных навыков из текста."""
        common_tech_patterns = [
            r"\bpython\b", r"\bsql\b", r"\bpostgresql\b", r"\bmysql\b", r"\boracle\b",
            r"\bfastapi\b", r"\bdjango\b", r"\bflask\b", r"\basyncio\b", r"\baiohttp\b",
            r"\bdocker\b", r"\bkubernetes\b", r"\bk8s\b", r"\bgit\b", r"\bci/cd\b", r"\bgitlab\b",
            r"\bpandas\b", r"\bnumpy\b", r"\bscikit-learn\b", r"\bpytorch\b", r"\btensorflow\b",
            r"\bclickhouse\b", r"\bredis\b", r"\bkafka\b", r"\brabbitmq\b", r"\bcelery\b",
            r"\btableau\b", r"\bpower bi\b", r"\bmetabase\b", r"\bsuperset\b",
            r"\ba/b[-\s]?тест\w*\b", r"\bпродуктов\w+ аналити\w*\b", r"\bсистемн\w+ аналити\w*\b",
            r"\bunit[-\s]?экономик\w*\b", r"\bcjм\b", r"\bcustdev\b", r"\bjtbd\b",
            r"\bltv\b", r"\bcac\b", r"\broi\b", r"\barpu\b", r"\bcr\b", r"\bchurn\b",
            r"\brest api\b", r"\bgraphql\b", r"\bgrpc\b", r"\bswagger\b", r"\bpostman\b",
            r"\blinux\b", r"\bbash\b", r"\bexcel\b", r"\bairflow\b", r"\bdbt\b",
            r"\bspark\b", r"\bhadoop\b", r"\bscala\b", r"\bgolang\b", r"\bgo\b", r"\bjava\b",
            r"\bjavascript\b", r"\btypescript\b", r"\breact\b", r"\bvue\b", r"\bnode\.?js\b",
            r"\bprompt engineering\b", r"\bllm\b", r"\brag\b", r"\blangchain\b", r"\blanggraph\b",
        ]
        found = []
        text_lower = text.lower()
        for pat in common_tech_patterns:
            matches = re.findall(pat, text_lower)
            if matches:
                # Очищаем и форматируем название навыка
                clean_name = matches[0].strip().replace("-", " ")
                clean_name = clean_name.title() if len(clean_name) > 3 else clean_name.upper()
                if clean_name not in found:
                    found.append(clean_name)

        # Также добавляем латинские термины от 3 символов
        latin_words = re.findall(r"\b[A-Za-z]{3,}\b", text)
        stop_latin = {"and", "the", "for", "with", "from", "you", "are", "have", "our", "all", "will", "this", "that", "team", "work", "job", "good", "plus"}
        for w in latin_words:
            w_cap = w.capitalize()
            if w.lower() not in stop_latin and len(w) > 2 and w_cap not in found and len(found) < 25:
                found.append(w_cap)

        return found

    def get_resume_summary(self) -> Dict[str, Any]:
        """Краткая выжимка и профиль резюме пользователя."""
        text = self.load_resume_text()
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        
        # Заголовок / Должность
        title = lines[0] if lines else "Специалист"
        if len(title) > 60:
            title = title[:60] + "..."

        skills = self.extract_skills_from_text(text)
        preview = " ".join(lines[:4])
        if len(preview) > 350:
            preview = preview[:350] + "..."

        return {
            "title": title,
            "skills": skills[:15],
            "skills_count": len(skills),
            "preview": preview,
            "full_text": text,
            "length": len(text),
        }

    def _dynamic_match(self, resume: str, title: str, desc: str, skills: str) -> Dict[str, Any]:
        """Динамический скоринг на основе навыков из резюме пользователя."""
        resume_skills = set(k.lower() for k in self.extract_skills_from_text(resume))
        vacancy_text = f"{title} {desc} {skills}"
        vacancy_skills = set(k.lower() for k in self.extract_skills_from_text(vacancy_text))

        # Совпадающие навыки (сильные стороны)
        matched = [s.title() for s in vacancy_skills if s in resume_skills]
        # Недостающие навыки (слабые стороны / пробелы)
        missing = [s.title() for s in vacancy_skills if s not in resume_skills]

        # Расчет скоринга
        if vacancy_skills:
            score = int((len(matched) / len(vacancy_skills)) * 100)
            score = max(20, min(95, score + (15 if len(matched) >= 3 else 0)))
        else:
            # Если в вакансии нет явных тегов, ищем пересечения по ключевым словам
            resume_keywords = self.extract_resume_keywords()
            text_lower = vacancy_text.lower()
            word_matches = [k for k in resume_keywords if k in text_lower]
            matched = [w.capitalize() for w in word_matches[:6]]
            ratio = len(word_matches) / max(len(resume_keywords) * 0.25, 2)
            score = min(90, max(25, int(ratio * 100)))

        rec = "APPLY" if score >= 70 else ("MANUAL" if score >= 40 else "SKIP")
        match_level = "HIGH" if score >= 70 else ("MEDIUM" if score >= 40 else "LOW")

        pros_text = f"Совпадение по стеку ({len(matched)}): {', '.join(matched[:6])}" if matched else "Базовое соответствие профилю"
        cons_text = f"Не указано в резюме: {', '.join(missing[:5])}" if missing else "Значительных пробелов по стеку не выявлено"

        return {
            "score": score,
            "match_level": match_level,
            "matching_skills": matched[:8],
            "missing_skills": missing[:6],
            "pros": pros_text,
            "cons": cons_text,
            "recommendation": rec,
        }
