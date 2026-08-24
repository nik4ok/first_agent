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

    def extract_skills_from_text(self, text: str, explicit_skills: Optional[str] = None) -> List[str]:
        """Извлечение известных технических, аналитических и профессиональных навыков из текста."""
        found: List[str] = []

        # 1. Если переданы явные теги навыков через запятую
        if explicit_skills and str(explicit_skills).lower() not in {"nan", "none", "null", ""}:
            for item in str(explicit_skills).split(","):
                clean = item.strip()
                if len(clean) >= 2 and clean.lower() not in {"nan", "none", "null"} and clean not in found:
                    found.append(clean)

        if not text or str(text).lower() in {"nan", "none", "null"}:
            return found

        text_lower = text.lower()

        common_tech_patterns = [
            (r"\bpython\b", "Python"),
            (r"\bsql\b", "SQL"),
            (r"\bpostgresql\b|\bpostgres\b", "PostgreSQL"),
            (r"\bmysql\b", "MySQL"),
            (r"\boracle\b", "Oracle"),
            (r"\bfastapi\b", "FastAPI"),
            (r"\bdjango\b", "Django"),
            (r"\bflask\b", "Flask"),
            (r"\basyncio\b", "Asyncio"),
            (r"\baiohttp\b", "Aiohttp"),
            (r"\bdocker\b", "Docker"),
            (r"\bkubernetes\b|\bk8s\b", "Kubernetes"),
            (r"\bgit\b", "Git"),
            (r"\bci/cd\b", "CI/CD"),
            (r"\bgitlab\b", "GitLab"),
            (r"\bpandas\b", "Pandas"),
            (r"\bnumpy\b", "NumPy"),
            (r"\bscikit-learn\b|\bsklearn\b", "Scikit-Learn"),
            (r"\bpytorch\b", "PyTorch"),
            (r"\btensorflow\b", "TensorFlow"),
            (r"\bclickhouse\b", "ClickHouse"),
            (r"\bredis\b", "Redis"),
            (r"\bkafka\b", "Kafka"),
            (r"\brabbitmq\b", "RabbitMQ"),
            (r"\bcelery\b", "Celery"),
            (r"\btableau\b", "Tableau"),
            (r"\bpower bi\b|\bpowerbi\b", "Power BI"),
            (r"\bmetabase\b", "Metabase"),
            (r"\bsuperset\b", "Apache Superset"),
            (r"\ba/b[-\s]?тест\w*\b", "A/B Тестирование"),
            (r"\bпродуктов\w+ аналити\w*\b", "Продуктовая аналитика"),
            (r"\bсистемн\w+ аналити\w*\b", "Системный анализ"),
            (r"\bunit[-\s]?экономик\w*\b", "Unit-экономика"),
            (r"\bcjm\b", "CJM"),
            (r"\bcustdev\b", "CustDev"),
            (r"\bjtbd\b", "JTBD"),
            (r"\bltv\b", "LTV"),
            (r"\bcac\b", "CAC"),
            (r"\broi\b|\bromi\b", "ROI / ROMI"),
            (r"\barpu\b|\barppu\b", "ARPU"),
            (r"\bcr\b|\bконверси\w*\b", "Конверсии (CR)"),
            (r"\bchurn\b|\botttok\b", "Churn Rate"),
            (r"\brest api\b|\brest\b", "REST API"),
            (r"\bgraphql\b", "GraphQL"),
            (r"\bgrpc\b", "gRPC"),
            (r"\bswagger\b", "Swagger"),
            (r"\bpostman\b", "Postman"),
            (r"\blinux\b", "Linux"),
            (r"\bbash\b", "Bash"),
            (r"\bexcel\b", "Excel"),
            (r"\bairflow\b", "Apache Airflow"),
            (r"\bdbt\b", "dbt"),
            (r"\bspark\b", "Apache Spark"),
            (r"\bhadoop\b", "Hadoop"),
            (r"\bscala\b", "Scala"),
            (r"\bgolang\b|\bgo\b", "Golang"),
            (r"\bjava\b", "Java"),
            (r"\bjavascript\b|\bjs\b", "JavaScript"),
            (r"\btypescript\b|\bts\b", "TypeScript"),
            (r"\breact\b", "React"),
            (r"\bvue\b", "Vue"),
            (r"\bnode\.?js\b", "Node.js"),
            (r"\bprompt engineering\b", "Prompt Engineering"),
            (r"\bllm\b", "LLM"),
            (r"\brag\b", "RAG"),
            (r"\blangchain\b", "LangChain"),
            (r"\blanggraph\b", "LangGraph"),
            # Отраслевые навыки (не-IT)
            (r"\bсанпин\b|\bсанитарн\w*\b", "Санитарные нормы (СанПиН)"),
            (r"\bтехкарт\w*\b|\bтехнологическ\w+ карт\w*\b", "Технологические карты"),
            (r"\bкассов\w+ операци\w*\b|\bкасс\w*\b", "Кассовые операции"),
            (r"\bвыкладк\w+ товар\w*\b", "Выкладка товаров"),
            (r"\bинвентаризаци\w*\b", "Инвентаризация"),
            (r"\bконтрол\w+ срок\w+ годност\w*\b", "Контроль сроков годности"),
        ]

        for pat, standard_name in common_tech_patterns:
            if re.search(pat, text_lower):
                if standard_name not in found:
                    found.append(standard_name)

        # Латинские термины от 3 символов
        latin_words = re.findall(r"\b[A-Za-z]{3,}\b", text)
        stop_latin = {"and", "the", "for", "with", "from", "you", "are", "have", "our", "all", "will", "this", "that", "team", "work", "job", "good", "plus", "must", "years", "senior", "middle", "junior", "lead", "developer", "engineer"}
        for w in latin_words:
            w_cap = w.capitalize()
            if w.lower() not in stop_latin and len(w) > 2 and w_cap not in found and len(found) < 25:
                found.append(w_cap)

        return found

    def get_resume_summary(self) -> Dict[str, Any]:
        """Краткая выжимка и профиль резюме пользователя."""
        text = self.load_resume_text()
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        # Ищем желаемую должность / специализацию
        title = ""
        for line in lines:
            if re.search(r"(должность|специализация|профиль|позиция):", line, re.I):
                clean_title = re.sub(r"[#\*]|\b(должность|специализация|профиль|позиция):\s*", "", line, flags=re.I).strip()
                if clean_title:
                    title = clean_title
                    break

        if not title:
            for line in lines:
                if not re.search(r"\bимя\s*:", line, re.I):
                    title = line.replace("#", "").replace("*", "").strip()
                    if title:
                        break

        if not title:
            title = lines[0] if lines else "Специалист"

        if len(title) > 70:
            title = title[:70] + "..."

        skills = self.extract_skills_from_text(text)
        preview_lines = [l.replace("#", "").strip() for l in lines if not re.match(r"^#?\s*имя\s*:", l, re.I)]
        preview = " • ".join([l for l in preview_lines[:3] if l])
        if len(preview) > 350:
            preview = preview[:350] + "..."

        return {
            "title": title,
            "skills": skills[:15],
            "skills_count": len(skills),
            "preview": preview or "Описание опыта не заполнено.",
            "full_text": text,
            "length": len(text),
        }

    def _dynamic_match(self, resume: str, title: str, desc: str, skills: str) -> Dict[str, Any]:
        """
        Интеллектуальный динамический скоринг соответствия резюме и вакансии:
        - Проверка совместимости сферы деятельности и должности
        - Оценка пересечения технологического стека и ключевых навыков
        - Выделение реальных сильных сторон (Match) и отсутствующих требований (Missing)
        """
        resume_summary = self.get_resume_summary()
        resume_title = resume_summary.get("title", "")
        resume_skills_list = self.extract_skills_from_text(resume)
        resume_skills_lower = set(k.lower() for k in resume_skills_list)

        title_lower = title.lower()
        vacancy_text = f"{title} {desc} {skills}"
        vacancy_text_lower = vacancy_text.lower()
        vacancy_skills_list = self.extract_skills_from_text(f"{title} {desc}", explicit_skills=skills)
        vacancy_skills_lower = set(k.lower() for k in vacancy_skills_list)

        # 1. Проверка несовпадающих сфер деятельности (непрофильные вакансии)
        UNRELATED_DOMAINS = {
            "food": {
                "keywords": ["повар", "кухн", "су-шеф", "бариста", "пекарь", "кондитер", "пиццамейкер", "сушист", "кулинар", "блюд", "официант", "бармен", "мойщик посуды", "мангальщик", "шаурмист"],
                "name": "Общепит и кулинария",
                "default_gaps": ["Опыт работы на кухне / поваром", "Приготовление блюд", "Санитарные нормы (СанПиН)", "Технологические карты"],
            },
            "retail": {
                "keywords": ["продавец", "кассир", "мерчендайзер", "товаровед", "продавец-консультант", "работник торгового зала", "выкладка товаров"],
                "name": "Розничная торговля и касса",
                "default_gaps": ["Работа с кассовым аппаратом", "Выкладка товаров", "Обслуживание покупателей", "Материальная ответственность"],
            },
            "logistics": {
                "keywords": ["водитель", "курьер", "кладовщик", "грузчик", "экспедитор", "комплектовщик", "такси", "карщик"],
                "name": "Транспорт и складская логистика",
                "default_gaps": ["Водительское удостоверение / стаж", "Складской учет / ТТН", "Погрузочно-разгрузочные работы"],
            },
            "medicine": {
                "keywords": ["врач", "медсестра", "медбрат", "стоматолог", "фармацевт", "провизор", "акушер", "ветеринар", "фельдшер"],
                "name": "Медицина и здравоохранение",
                "default_gaps": ["Медицинское образование", "Действующий сертификат / аккредитация", "Клиническая практика"],
            },
            "labor": {
                "keywords": ["монтажник", "сварщик", "электрик", "слесарь", "строитель", "разнорабочий", "уборщик", "клинер", "маляр", "штукатур", "плотник"],
                "name": "Рабочие специальности и строительство",
                "default_gaps": ["Профильные допуски / разряды", "Физический труд", "Работа со спецоборудованием"],
            },
        }

        # Проверяем, относится ли вакансия к непрофильной сфере, которой нет в резюме
        for _, domain_info in UNRELATED_DOMAINS.items():
            has_domain_in_vacancy = any(re.search(r"\b" + re.escape(kw), title_lower) for kw in domain_info["keywords"])
            has_domain_in_resume = any(re.search(r"\b" + re.escape(kw), resume.lower()) for kw in domain_info["keywords"])

            if has_domain_in_vacancy and not has_domain_in_resume:
                # Вакансия из совершенно другой сферы!
                missing = domain_info["default_gaps"]
                # Добавляем специфичные навыки из вакансии, если есть
                if skills:
                    for s in skills.split(",")[:3]:
                        s_clean = s.strip()
                        if s_clean and s_clean not in missing:
                            missing.append(s_clean)

                return {
                    "score": 5,
                    "match_level": "LOW",
                    "matching_skills": [],
                    "missing_skills": missing[:6],
                    "pros": f"Непрофильная сфера ({domain_info['name']})",
                    "cons": f"Специальность «{title}» не соответствует профилю резюме («{resume_title}»). Отсутствует профильный опыт и навыки в сфере {domain_info['name']}.",
                    "recommendation": "SKIP",
                }

        # 2. Сопоставление навыков для профильных/смежных вакансий
        matched = []
        missing = []
        seen_matched = set()
        seen_missing = set()

        # Навыки из списка тегов и описания
        for s in vacancy_skills_list:
            s_low = s.lower()
            if s_low in {"nan", "none", "null"}:
                continue
            if s_low in resume_skills_lower:
                if s_low not in seen_matched:
                    matched.append(s)
                    seen_matched.add(s_low)
            else:
                if s_low not in seen_missing and s_low not in seen_matched:
                    missing.append(s)
                    seen_missing.add(s_low)

        # 3. Проверка совпадения названия должности / роли
        title_words = [w for w in re.findall(r"[A-Za-zА-Яа-я]{4,}", title_lower) if w not in {"младший", "старший", "ведущий", "middle", "senior", "junior", "lead", "работа", "опыт"}]
        title_matches = [w for w in title_words if w in resume.lower()]
        has_title_match = len(title_matches) > 0

        # Расчет итогового скора
        if vacancy_skills_list:
            skill_ratio = len(matched) / len(vacancy_skills_list)
            base_score = int(skill_ratio * 70)
            if has_title_match:
                base_score += 25
            score = max(10, min(98, base_score))
        else:
            # Если явных тегов навыков нет, ищем пересечения по ключевым словам резюме
            resume_keywords = self.extract_resume_keywords()
            word_matches = [k for k in resume_keywords if k in vacancy_text_lower]
            matched = [w.capitalize() for w in word_matches[:6]]
            ratio = len(word_matches) / max(len(resume_keywords) * 0.3, 2)
            score = min(90, max(15, int(ratio * 70) + (25 if has_title_match else 0)))

        rec = "APPLY" if score >= 70 else ("MANUAL" if score >= 40 else "SKIP")
        match_level = "HIGH" if score >= 70 else ("MEDIUM" if score >= 40 else "LOW")

        pros_text = f"Совпадение по стеку ({len(matched)}): {', '.join(matched[:6])}" if matched else "Требуется дополнительное сопоставление стека"
        cons_text = f"Не указано в резюме ({len(missing)}): {', '.join(missing[:5])}" if missing else "Значительных пробелов по стеку не выявлено"

        return {
            "score": score,
            "match_level": match_level,
            "matching_skills": matched[:8],
            "missing_skills": missing[:6],
            "pros": pros_text,
            "cons": cons_text,
            "recommendation": rec,
        }
