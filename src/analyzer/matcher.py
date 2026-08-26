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
            return self._local_cover_letter(vacancy_title, employer_name, vacancy_desc, match_info)

        prompt = (
            f"Напиши сопроводительное письмо от кандидата на вакансию '{vacancy_title}' в '{employer_name}'.\n\n"
            "Правила:\n"
            "1. Без клише (стрессоустойчивый, коммуникабельный, командный игрок).\n"
            "2. Первое предложение: почему именно эта роль и компания — из должности и опыта резюме.\n"
            "3. Дальше 2 факта с цифрами или проектами СТРОГО из текста резюме, которые бьются с вакансией.\n"
            "4. Не выдумывай компании, стеки, цифры и должности, которых нет в резюме.\n"
            "5. 3 коротких абзаца, 700–1100 символов, на русском, обращение «вы» с маленькой буквы.\n"
            "6. Финал — конкретный созвон, без «буду рад сотрудничеству».\n\n"
            f"Резюме:\n{resume_text}\n\n"
            f"Вакансия:\n{vacancy_desc[:2000]}"
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
            return self._local_cover_letter(vacancy_title, employer_name, vacancy_desc, match_info)

    def parse_resume_profile(self) -> Dict[str, Any]:
        """Должность, компания и факты с цифрами — только из текущего файла резюме."""
        summary = self.get_resume_summary()
        text = summary.get("full_text") or self.load_resume_text()
        role = self._extract_desired_role(text) or str(summary.get("title") or "").strip()
        if role.lower() in {"специалист", "резюме не заполнено. отредактируйте data/my_resume.txt."}:
            role = ""
        return {
            "role": role[:120],
            "company": self._extract_last_company(text),
            "skills": list(summary.get("skills") or [])[:12],
            "achievements": self._extract_metric_bullets(text),
        }

    def _local_cover_letter(
        self,
        vacancy_title: str,
        employer_name: str,
        vacancy_desc: str,
        match_info: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Письмо из фактов текущего резюме, без зашитого профиля конкретного человека."""
        match_info = match_info or {}
        profile = self.parse_resume_profile()
        matched = [str(s) for s in (match_info.get("matching_skills") or []) if s]
        if not matched:
            matched = [str(s) for s in (profile.get("skills") or [])[:5]]
        skill_str = ", ".join(matched[:5]) if matched else "релевантный стек из резюме"

        role = profile.get("role") or "специалист"
        company = profile.get("company") or ""
        who = f"я {role}" + (f" в {company}" if company else "")
        bullets = self._select_resume_bullets(vacancy_title, vacancy_desc, profile)
        letter = (
            f"Здравствуйте!\n\n"
            f"Откликаюсь на {vacancy_title} в {employer_name}: {who}, "
            f"в работе опираюсь на {skill_str}.\n\n"
            f"{bullets}\n\n"
            f"Могу за 20 минут разобрать, как этот опыт закрывает задачи роли — удобно созвониться на этой неделе."
        )
        return letter[:1400]

    def _select_resume_bullets(
        self,
        vacancy_title: str,
        vacancy_desc: str,
        profile: Optional[Dict[str, Any]] = None,
    ) -> str:
        profile = profile or self.parse_resume_profile()
        hay = f"{vacancy_title} {vacancy_desc}".lower()
        achievements = list(profile.get("achievements") or [])
        scored = sorted(achievements, key=lambda bullet: self._score_bullet(bullet, hay), reverse=True)
        picked: List[str] = []
        for bullet in scored:
            if bullet not in picked:
                picked.append(bullet)
            if len(picked) == 2:
                break
        if len(picked) < 2:
            for skill in profile.get("skills") or []:
                extra = f"В резюме есть практический опыт с {skill}, это напрямую стыкуется с задачами роли."
                if extra not in picked:
                    picked.append(extra)
                if len(picked) == 2:
                    break
        if not picked:
            picked = ["Готов разобрать ваш контур по резюме и показать, какие задачи закрывал на похожих ролях."]
        return "• " + "\n• ".join(picked[:2])

    def _extract_desired_role(self, text: str) -> str:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        for idx, line in enumerate(lines):
            if re.search(r"желаемая должность", line, re.I):
                after = re.sub(r".*должность[^:]*:\s*", "", line, flags=re.I).strip()
                after = re.sub(r"\s+и зарплата.*$", "", after, flags=re.I).strip(" .:-")
                if after and not re.search(r"желаемая должность", after, re.I):
                    return after[:120]
                if idx + 1 < len(lines):
                    nxt = re.sub(r"^[\-—·•*]+\s*", "", lines[idx + 1]).strip()
                    if nxt and not re.search(r"специализац|зарплат|тип занятости", nxt, re.I):
                        return nxt[:120]
        role_re = re.compile(
            r"^(senior|lead|middle|junior|staff|principal)?\s*"
            r"(product |data |системный |бизнес.?|маркетинг.?|python |backend |frontend |fullstack )?"
            r"(analyst|аналитик|developer|разработчик|engineer|инженер|manager|менеджер|designer|дизайнер)\b",
            re.I,
        )
        for line in lines:
            clean = re.sub(r"^[\-—·•*]+\s*", "", line).strip()
            if role_re.search(clean) and len(clean) <= 80:
                return clean[:120]
        return ""

    def _extract_last_company(self, text: str) -> str:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        skip = re.compile(
            r"опыт работы|настоящее время|месяц|город|москва|информационн|розничн|специализац|"
            r"желаемая|прожива|гражданств|мужчин|женщин|телефон|зарплат|занятост",
            re.I,
        )
        start = 0
        for idx, line in enumerate(lines):
            if re.search(r"опыт работы", line, re.I):
                start = idx + 1
                break
        for line in lines[start : start + 12]:
            clean = re.sub(r"^[\-—·•*]+\s*", "", line).strip()
            if skip.search(clean) or re.search(r"\d{4}", clean) or len(clean) < 2 or len(clean) > 60:
                continue
            if re.search(r"analyst|аналитик|developer|engineer|менеджер", clean, re.I):
                continue
            return clean[:80]
        return ""

    def _extract_metric_bullets(self, text: str) -> List[str]:
        skip = re.compile(
            r"@|\+7|родил|прожива|гражданств|резюме обновл|мужчина|женщина|предпочитаем|"
            r"разрешение на работу|лет, родился|настоящее время",
            re.I,
        )
        metric = re.compile(
            r"(\d+[.,]?\d*\s*%|\d+\s*\+|млн|миллиард|млрд|тыс\.|ebitda|п\.п|руб|p99|latency|nps|gmv|ltv)",
            re.I,
        )
        out: List[str] = []
        seen = set()
        buffer = ""
        for raw in text.splitlines():
            stripped = raw.strip()
            is_new = bool(re.match(r"^[·•\-—*]\s+", stripped))
            line = re.sub(r"^[\s·•\-\—*]+", "", stripped).strip()
            if not line:
                self._push_metric_bullet(buffer, skip, metric, out, seen)
                buffer = ""
                continue
            if buffer and line[:1].islower() and not is_new:
                buffer = f"{buffer} {line}"
                continue
            self._push_metric_bullet(buffer, skip, metric, out, seen)
            buffer = line
        self._push_metric_bullet(buffer, skip, metric, out, seen)
        return out[:12]

    @staticmethod
    def _push_metric_bullet(
        line: str,
        skip: re.Pattern,
        metric: re.Pattern,
        out: List[str],
        seen: set,
    ) -> None:
        text = (line or "").strip()
        if len(text) < 40 or skip.search(text) or not metric.search(text):
            return
        key = text[:90].lower()
        if key in seen:
            return
        seen.add(key)
        out.append(text[:320])

    @staticmethod
    def _score_bullet(bullet: str, hay: str) -> int:
        tokens = [
            "a/b", "аб", "sql", "python", "retention", "марж", "ebitda", "кэшбек",
            "подписк", "ml", "дашборд", "qlik", "финмодел", "тариф", "clickhouse",
            "api", "backend", "latency", "gmv", "nps", "ltv", "эксперимент",
        ]
        b = bullet.lower()
        return sum(2 for t in tokens if t in hay and t in b) + sum(1 for t in tokens if t in b)

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
        stop_latin = {
            "and", "the", "for", "with", "from", "you", "are", "have", "our", "all", "will", "this", "that",
            "team", "work", "job", "good", "plus", "must", "years", "senior", "middle", "junior", "lead",
            "developer", "engineer", "data", "product", "company", "project", "user", "business", "service",
            "system", "requirements", "experience", "skills", "about", "role", "looking", "responsibilities",
            "salary", "bonus", "time", "full", "part", "hybrid", "remote", "office", "moscow", "russia",
        }
        for w in latin_words:
            w_cap = w.capitalize()
            if w.lower() not in stop_latin and len(w) > 2 and w_cap not in found and len(found) < 25:
                found.append(w_cap)

        return found

    def get_resume_summary(self) -> Dict[str, Any]:
        """Краткая выжимка и профиль резюме пользователя."""
        text = self.load_resume_text()
        lines = [line.strip() for line in text.split("\n") if line.strip()]

        # Ищем желаемую должность / специализацию (в т.ч. на следующей строке, как в выгрузке HH)
        title = self._extract_desired_role(text)

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

    def audit_market_competency(self, vacancies: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """
        Комплексный аудит резюме против всего среза рынка:
        1. Анализирует частотность всех навыков и требований среди всех спарсенных вакансий.
        2. Сопоставляет стек кандидата со срезом рынка.
        3. Рассчитывает индекс конкурентоспособности резюме.
        4. Формирует конкретные рекомендации по доработке резюме под требования работодателей.
        """
        from collections import Counter

        # Если вакансии не переданы, пробуем загрузить из Excel
        if not vacancies:
            from src.parser.excel_storage import ExcelStorage
            storage = ExcelStorage()
            df = storage.load_all()
            if df.empty:
                return {
                    "status": "empty",
                    "message": "База вакансий пуста. Сначала выполните поиск вакансий.",
                }
            df_clean = df.fillna("")
            vacancies = []
            title_col = "Название вакансии" if "Название вакансии" in df.columns else "title"
            desc_col = "Полное описание" if "Полное описание" in df.columns else "description"
            skills_col = "Ключевые навыки" if "Ключевые навыки" in df.columns else "skills"
            for _, r in df_clean.iterrows():
                vacancies.append({
                    "title": str(r.get(title_col, "")),
                    "description": str(r.get(desc_col, "")),
                    "skills": str(r.get(skills_col, "")),
                })

        total_vacancies = len(vacancies)
        if total_vacancies == 0:
            return {"status": "empty", "message": "Нет данных по вакансиям для анализа."}

        # 1. Извлекаем все навыки из всех вакансий и считаем частотность
        all_skills_counter = Counter()
        canonical_map = {
            "sql": "SQL",
            "python": "Python",
            "excel": "Excel",
            "power bi": "Power BI",
            "powerbi": "Power BI",
            "tableau": "Tableau",
            "clickhouse": "ClickHouse",
            "postgresql": "PostgreSQL",
            "postgres": "PostgreSQL",
            "spark": "PySpark / Spark",
            "pyspark": "PySpark / Spark",
            "airflow": "Airflow",
            "pandas": "Pandas",
            "numpy": "NumPy",
            "git": "Git",
            "docker": "Docker",
            "dbt": "dbt",
            "superset": "Apache Superset",
            "hadoop": "Hadoop",
            "fastapi": "FastAPI",
            "django": "Django",
            "a/b": "A/B Тестирование",
            "ab": "A/B Тестирование",
            "unit economy": "Unit-экономика",
            "causal inference": "Causal Inference",
            "ml": "Machine Learning (ML)",
            "stat": "Мат. статистика",
        }

        for v in vacancies:
            v_text = f"{v.get('title', '')} {v.get('description', '')}"
            skills_in_v = self.extract_skills_from_text(v_text, explicit_skills=v.get("skills", ""))
            seen_in_v = set()
            for s in skills_in_v:
                s_clean = s.strip()
                if not s_clean or s_clean.lower() in {"nan", "none", "null"}:
                    continue
                s_lower = s_clean.lower()
                s_canon = canonical_map.get(s_lower, s_clean.capitalize() if s_clean.islower() else s_clean)
                if s_canon.lower() not in seen_in_v:
                    seen_in_v.add(s_canon.lower())
                    all_skills_counter[s_canon] += 1

        # 2. Извлекаем навыки кандидата
        resume_summary = self.get_resume_summary()
        resume_text = self.load_resume_text()
        resume_skills = set(k.lower() for k in self.extract_skills_from_text(resume_text))

        # 3. Формируем топ рынка (навыки, встречающиеся чаще всего)
        top_skills_raw = all_skills_counter.most_common(25)
        top_market_skills = []
        covered_count = 0
        strong_skills = []
        missing_critical = []
        missing_secondary = []

        for skill_name, count in top_skills_raw:
            pct = round((count / total_vacancies) * 100, 1)
            is_present = skill_name.lower() in resume_skills

            skill_data = {
                "skill": skill_name,
                "count": count,
                "percentage": pct,
                "present_in_resume": is_present,
            }
            top_market_skills.append(skill_data)

            if is_present:
                covered_count += 1
                strong_skills.append(f"{skill_name} ({pct}% рынка)")
            else:
                if pct >= 20.0 or len(missing_critical) < 5:
                    missing_critical.append(f"{skill_name} ({pct}% рынка)")
                else:
                    missing_secondary.append(f"{skill_name} ({pct}% рынка)")

        # 4. Расчет индекса конкурентоспособности (0-100)
        # Взвешиваем покрытие топ-15 навыков рынка
        top_15 = top_market_skills[:15]
        if top_15:
            total_weight = sum(item["percentage"] for item in top_15)
            covered_weight = sum(item["percentage"] for item in top_15 if item["present_in_resume"])
            competency_score = int((covered_weight / max(total_weight, 1)) * 100)
        else:
            competency_score = 50

        competency_score = max(10, min(99, competency_score))

        # Уровень соответствия
        if competency_score >= 80:
            market_tier = "TOP TIER (Высокая конкурентоспособность)"
            tier_color = "emerald"
        elif competency_score >= 55:
            market_tier = "SOLID MATCH (Средне-высокая конкурентоспособность)"
            tier_color = "blue"
        else:
            market_tier = "GAPS DETECTED (Требуется доработка)"
            tier_color = "amber"

        # 5. Генерация рекомендаций по улучшению
        recommendations = []

        if missing_critical:
            clean_critical = [s.split(" (")[0] for s in missing_critical[:5]]
            recommendations.append(
                f"🎯 **Добавьте ключевые слова в раздел навыков и опыт:** Рынок в этой сфере требует: {', '.join(clean_critical)}. "
                "Если у вас есть опыт с этими технологиями, явно упомяните их в тексте для прохождения ATS-фильтров."
            )

        if "A/B" in str(strong_skills) or "A/B Тестирование" in str(strong_skills):
            recommendations.append(
                "📈 **Выделите продуктовые эксперименты:** Навык A/B-тестирования очень востребован. "
                "Укажите размер выборки, бизнес-эффект (в деньгах или %) и методологию (Causal Inference, CUPED)."
            )

        if "SQL" in str(strong_skills) or "Python" in str(strong_skills):
            recommendations.append(
                "💡 **Оцифруйте результаты работы:** В блоках опыта добавьте глаголы действия и метрики: "
                "«увеличил маржинальность на X%», «сократил время расчетов с N дней до 1 часа», «автоматизировал пайплайн»."
            )

        if len(resume_text) < 500:
            recommendations.append(
                "⚠️ **Резюме слишком краткое:** Опишите подробнее ваши ключевые проекты, задачи, стек и достигнутые результаты."
            )

        return {
            "status": "ok",
            "vacancies_analyzed": total_vacancies,
            "resume_title": resume_summary.get("title", "Специалист"),
            "competency_score": competency_score,
            "market_tier": market_tier,
            "tier_color": tier_color,
            "top_market_skills": top_market_skills[:15],
            "strong_skills": strong_skills[:8],
            "missing_critical": missing_critical[:6],
            "missing_secondary": missing_secondary[:6],
            "recommendations": recommendations,
        }

