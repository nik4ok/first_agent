import concurrent.futures
from datetime import datetime
import logging
import re
from typing import List, Dict, Any, Optional, Tuple, Union
import requests
from bs4 import BeautifulSoup

from src.config import settings

logger = logging.getLogger(__name__)

# Специальные значения SEARCH_AREA (не числовые id hh.ru)
WORLDWIDE_AREA_KEYS = {"all", "world", "worldwide"}
EXCEPT_RU_AREA_KEYS = {"ex_ru", "world_except_ru", "except_ru", "no_ru"}
RUSSIA_AREA_ID = "113"

# Fallback, если /areas недоступен: страны HH + «Другие регионы» (ЕС, Кипр, remote)
FALLBACK_COUNTRY_IDS = ["113", "5", "40", "9", "16", "28", "1001", "48", "97"]

AREA_LABELS = {
    "all": "🌐 Весь мир",
    "ex_ru": "🌍 Весь мир кроме России",
    "113": "🇷🇺 Вся Россия",
    "1": "🏙️ Москва",
    "2": "🏛️ Санкт-Петербург",
    "3": "🏢 Екатеринбург",
    "88": "🕌 Казань",
    "4": "🌲 Новосибирск",
    "66": "🏭 Нижний Новгород",
    "53": "🌴 Краснодар",
}

# Цикл переключения региона в Telegram
AREA_TOGGLE_CYCLE = ["all", "ex_ru", "113", "1", "2"]


class HHClient:
    """
    Клиент для HeadHunter с поддержкой официального API и глубокого Web-парсинга:
    - Поиск вакансий через официальный API или web-выдачу hh.ru
    - Параллельный сбор полных описаний (требования, обязанности, условия) и ключевых навыков
    """

    WEB_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }

    def __init__(self, access_token: Optional[str] = None, user_agent: Optional[str] = None):
        self.base_url = settings.HH_API_URL
        self.access_token = access_token or settings.HH_ACCESS_TOKEN
        self.user_agent = user_agent or settings.HH_USER_AGENT
        self._country_ids: Optional[List[str]] = None

    @property
    def api_headers(self) -> Dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "HH-User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    @staticmethod
    def clean_html(raw_html: Optional[str]) -> str:
        """Очистка HTML-тегов из описания вакансии."""
        if not raw_html:
            return ""
        soup = BeautifulSoup(raw_html, "html.parser")
        text = soup.get_text(separator="\n")
        text = re.sub(r"\n\s*\n", "\n\n", text)
        return text.strip()

    def get_country_ids(self, exclude_russia: bool = False) -> List[str]:
        """Страны верхнего уровня hh.ru (Россия, СНГ, «Другие регионы» = 1001)."""
        if self._country_ids is None:
            try:
                resp = requests.get(f"{self.base_url}/areas", headers=self.api_headers, timeout=10)
                if resp.status_code == 200:
                    ids = [str(c.get("id")) for c in resp.json() if c.get("id") is not None]
                    if ids:
                        self._country_ids = ids
            except Exception as e:
                logger.warning(f"Не удалось загрузить справочник стран HH: {e}")
            if not self._country_ids:
                self._country_ids = list(FALLBACK_COUNTRY_IDS)

        ids = list(self._country_ids)
        if exclude_russia:
            return [i for i in ids if i != RUSSIA_AREA_ID]
        return ids

    def resolve_area_ids(self, area: Optional[str]) -> List[str]:
        """
        Преобразует значение фильтра региона в список area id для HH.
        all / world  → все страны HH (включая Россию и «Другие регионы»)
        ex_ru        → все страны кроме России (СНГ + ЕС/Кипр/remote через 1001)
        113, 1, 2…   → один регион как раньше
        """
        raw = str(area or "").strip()
        key = raw.lower()
        if key in WORLDWIDE_AREA_KEYS:
            return self.get_country_ids(exclude_russia=False)
        if key in EXCEPT_RU_AREA_KEYS:
            return self.get_country_ids(exclude_russia=True)
        if not raw or key == "0":
            return [RUSSIA_AREA_ID]
        return [raw]

    def _apply_area_params(self, params: Dict[str, Any], area: Optional[str]) -> Dict[str, Any]:
        area_ids = self.resolve_area_ids(area)
        if not area_ids:
            return params
        params["area"] = area_ids[0] if len(area_ids) == 1 else area_ids
        if len(area_ids) > 1:
            logger.info("Поиск HH по регионам %s → %s стран/зон", area, ",".join(area_ids))
        return params

    @staticmethod
    def parse_salary_dict(salary_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Парсинг структуры зарплаты из API."""
        if not salary_data:
            return {
                "salary_from": None,
                "salary_to": None,
                "currency": None,
                "gross": None,
                "salary_str": "Не указана",
            }

        s_from = salary_data.get("from")
        s_to = salary_data.get("to")
        curr = salary_data.get("currency")
        gross = salary_data.get("gross")

        parts = []
        if s_from:
            parts.append(f"от {s_from:,}".replace(",", " "))
        if s_to:
            parts.append(f"до {s_to:,}".replace(",", " "))
        if curr:
            parts.append(curr)

        return {
            "salary_from": s_from,
            "salary_to": s_to,
            "currency": curr,
            "gross": gross,
            "salary_str": " ".join(parts) if parts else "Не указана",
        }

    @staticmethod
    def parse_salary_text(raw_text: str) -> Dict[str, Any]:
        """Парсинг строки зарплаты из HTML."""
        if not raw_text or "не указана" in raw_text.lower():
            return {
                "salary_from": None,
                "salary_to": None,
                "currency": None,
                "gross": None,
                "salary_str": "Не указана",
            }

        cleaned = raw_text.replace("\u202f", "").replace("\xa0", " ").strip()
        from_match = re.search(r"от\s*([\d\s]+)", cleaned)
        to_match = re.search(r"до\s*([\d\s]+)", cleaned)

        s_from = int(from_match.group(1).replace(" ", "")) if from_match else None
        s_to = int(to_match.group(1).replace(" ", "")) if to_match else None

        currency = "RUR"
        if "usd" in cleaned.lower() or "$" in cleaned:
            currency = "USD"
        elif "eur" in cleaned.lower() or "€" in cleaned:
            currency = "EUR"
        elif "kzt" in cleaned.lower() or "₸" in cleaned:
            currency = "KZT"

        return {
            "salary_from": s_from,
            "salary_to": s_to,
            "currency": currency,
            "gross": None,
            "salary_str": cleaned,
        }

    def fetch_vacancy_full_details(self, vacancy_id: str) -> Tuple[str, List[str]]:
        """
        Загрузка полного описания вакансии и списка навыков со страницы hh.ru/vacancy/{id}.
        """
        url = f"https://hh.ru/vacancy/{vacancy_id}"
        try:
            resp = requests.get(url, headers=self.WEB_HEADERS, timeout=7)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                desc_el = (
                    soup.find(attrs={"data-qa": re.compile(r"vacancy-description|vacancy_description", re.I)})
                    or soup.find("div", class_=re.compile(r"vacancy-description|g-user-content", re.I))
                )
                skills_els = soup.find_all(attrs={"data-qa": re.compile(r"skills-element|skill", re.I)})
                skills = [s.get_text(strip=True) for s in skills_els if s.get_text(strip=True)]

                desc_text = desc_el.get_text(separator=" ", strip=True) if desc_el else ""
                # Очищаем множественные пробелы
                desc_text = re.sub(r"\s+", " ", desc_text).strip()
                return desc_text, skills
        except Exception as e:
            logger.debug(f"Не удалось загрузить детальное описание для {vacancy_id}: {e}")
        return "", []

    def fetch_via_web(
        self,
        text: str = "Python",
        area: Optional[str] = "113",
        experience: Optional[str] = None,
        search_period: Optional[Union[str, int]] = "30",
        only_with_salary: bool = False,
        order_by: str = "publication_time",
        max_vacancies: int = 20,
        fetch_full_description: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Поиск вакансий через Web-выдачу hh.ru с параллельным сбором полных описаний.
        order_by: 'publication_time' (по дате/свежие) или 'relevance' (по соответствию)
        search_period: 1 (день), 3 (дня), 7 (неделя), 14 (2 недели), 30 (месяц), 60 (2 мес), 90 (3 мес), 365 (год), 'all' (все время)
        experience: 'noExperience', 'between1And3', 'between3And6', 'moreThan6', или 'all'/None (все)
        max_vacancies: любое желаемое количество вакансий
        """
        results: List[Dict[str, Any]] = []
        page = 0
        seen_ids = set()
        max_pages = min(50, max(2, (max_vacancies // 20) + 4))

        exp_map = {
            "noExperience": "noExperience",
            "between1And3": "between1And3",
            "between3And6": "between3And6",
            "moreThan6": "moreThan6",
        }

        order_val = "publication_time" if order_by == "publication_time" else "relevance"

        while len(results) < max_vacancies and page < max_pages:
            params: Dict[str, Any] = {
                "text": text,
                "page": page,
                "items_on_page": min(max_vacancies - len(results) + 5, 50),
                "order_by": order_val,
            }
            self._apply_area_params(params, area)
            if experience and experience in exp_map and experience != "all":
                params["experience"] = exp_map[experience]
            if search_period and str(search_period).strip() not in {"all", "0", ""}:
                params["search_period"] = str(search_period).strip()
            if only_with_salary:
                params["only_with_salary"] = "true"

            try:
                response = requests.get(
                    "https://hh.ru/search/vacancy",
                    headers=self.WEB_HEADERS,
                    params=params,
                    timeout=15,
                )
                if response.status_code != 200:
                    break

                soup = BeautifulSoup(response.text, "html.parser")
                links = soup.find_all("a", href=re.compile(r"/vacancy/\d+"))

                found_on_page = 0
                for link in links:
                    href = link.get("href", "")
                    if "adsrv" in href:
                        continue

                    match = re.search(r"/vacancy/(\d+)", href)
                    if not match:
                        continue
                    v_id = match.group(1)

                    if v_id in seen_ids:
                        continue
                    seen_ids.add(v_id)

                    title = link.get_text(strip=True)
                    if not title or len(title) < 3:
                        continue

                    # Ищем карточку вакансии (article / div)
                    card = (
                        link.find_parent("article")
                        or link.find_parent(attrs={"data-qa": re.compile(r"vacancy-serp__vacancy", re.I)})
                        or link.find_parent("div", class_=re.compile(r"vacancy-card|serp-item", re.I))
                        or link.find_parent("li")
                    )

                    # Компания
                    comp_el = (
                        card.find("span", attrs={"data-qa": re.compile(r"company|employer", re.I)})
                        or card.find("a", attrs={"data-qa": re.compile(r"company|employer", re.I)})
                        or card.find(class_=re.compile(r"company|employer", re.I))
                        if card
                        else None
                    )
                    employer = comp_el.get_text(strip=True) if comp_el else "Не указан"

                    # Зарплата
                    sal_el = (
                        card.find(attrs={"data-qa": re.compile(r"salary|compensation", re.I)})
                        or card.find("span", class_=re.compile(r"compensation|salary", re.I))
                        if card
                        else None
                    )
                    sal_text = sal_el.get_text(strip=True) if sal_el else ""
                    salary_info = self.parse_salary_text(sal_text)

                    # Город
                    city_el = card.find(attrs={"data-qa": re.compile(r"address|location", re.I)}) if card else None
                    city = city_el.get_text(strip=True) if city_el else ""

                    # Дата публикации / обнаружения
                    date_el = (
                        card.find("time")
                        or card.find(attrs={"data-qa": re.compile(r"vacancy-serp__vacancy-date|publication-date|vacancy-date", re.I)})
                        or card.find("span", class_=re.compile(r"date|publication", re.I))
                        if card
                        else None
                    )
                    pub_date = date_el.get("datetime") or date_el.get_text(strip=True) if date_el else ""
                    if not pub_date:
                        pub_date = datetime.now().strftime("%Y-%m-%d %H:%M")

                    record = {
                        "id": v_id,
                        "title": title,
                        "employer": employer,
                        "city": city,
                        "salary_str": salary_info["salary_str"],
                        "salary_from": salary_info["salary_from"],
                        "salary_to": salary_info["salary_to"],
                        "currency": salary_info["currency"],
                        "skills": "",
                        "url": f"https://hh.ru/vacancy/{v_id}",
                        "published_at": pub_date,
                        "description": f"Позиция {title} в компании {employer}",
                        "status": "NEW",
                        "match_score": None,
                        "cover_letter": "",
                        "notes": "",
                    }
                    results.append(record)
                    found_on_page += 1

                    if len(results) >= max_vacancies:
                        break

                if found_on_page == 0:
                    break

                page += 1

            except Exception as e:
                logger.error(f"Ошибка при веб-поиске: {e}")
                break

        # Параллельная дозагрузка полных описаний и навыков для найденных вакансий
        if fetch_full_description and results:
            logger.info(f"Загрузка полных описаний для {len(results)} вакансий...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                future_to_id = {executor.submit(self.fetch_vacancy_full_details, r["id"]): r for r in results}
                for future in concurrent.futures.as_completed(future_to_id):
                    rec = future_to_id[future]
                    try:
                        desc, skills = future.result()
                        if desc:
                            rec["description"] = desc
                        if skills:
                            rec["skills"] = ", ".join(skills)
                    except Exception as err:
                        logger.debug(f"Ошибка загрузки деталей для {rec['id']}: {err}")

        return results

    def fetch_and_normalize_vacancies(
        self,
        text: str = "Python",
        area: Optional[str] = "113",
        experience: Optional[str] = None,
        search_period: Optional[Union[str, int]] = "30",
        only_with_salary: bool = False,
        order_by: str = "publication_time",
        max_vacancies: int = 20,
        fetch_full_description: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Основной метод сбора вакансий.
        Поддерживает сбор любого количества вакансий (max_vacancies) и фильтрацию по свежести (search_period).
        """
        # Сначала пробуем официальный API
        try:
            url = f"{self.base_url}/vacancies"
            results = []
            api_page = 0
            max_api_pages = min(20, max(1, (max_vacancies + 49) // 50))

            while len(results) < max_vacancies and api_page < max_api_pages:
                per_page_count = min(max_vacancies - len(results), 50)
                params: Dict[str, Any] = {
                    "text": text,
                    "per_page": max(per_page_count, 10),
                    "page": api_page,
                    "order_by": "publication_time" if order_by == "publication_time" else "relevance",
                }
                self._apply_area_params(params, area)
                if experience and experience not in {"all", ""}:
                    params["experience"] = experience
                if search_period and str(search_period).strip() not in {"all", "0", ""}:
                    params["period"] = int(search_period)
                if only_with_salary:
                    params["only_with_salary"] = "true"

                resp = requests.get(url, headers=self.api_headers, params=params, timeout=10)
                if resp.status_code != 200:
                    break

                items = resp.json().get("items", [])
                if not items:
                    break

                for item in items:
                    v_id = str(item.get("id"))
                    sal_info = self.parse_salary_dict(item.get("salary"))
                    req_txt = self.clean_html(item.get("snippet", {}).get("requirement", "") or "")
                    resp_txt = self.clean_html(item.get("snippet", {}).get("responsibility", "") or "")
                    desc_parts = [p for p in [req_txt, resp_txt] if p]
                    desc_full = " | ".join(desc_parts) if desc_parts else f"Позиция {item.get('name', '')} в компании {item.get('employer', {}).get('name', '')}"

                    results.append({
                        "id": v_id,
                        "title": item.get("name", ""),
                        "employer": item.get("employer", {}).get("name", "Не указан"),
                        "city": item.get("area", {}).get("name", ""),
                        "salary_str": sal_info["salary_str"],
                        "salary_from": sal_info["salary_from"],
                        "salary_to": sal_info["salary_to"],
                        "currency": sal_info["currency"],
                        "skills": "",
                        "url": item.get("alternate_url", f"https://hh.ru/vacancy/{v_id}"),
                        "published_at": item.get("published_at", "")[:10],
                        "description": desc_full,
                        "status": "NEW",
                        "match_score": None,
                        "cover_letter": "",
                        "notes": "",
                    })
                    if len(results) >= max_vacancies:
                        break

                api_page += 1

            if results:
                # Если требуется полное описание и навыки со страницы вакансии
                if fetch_full_description:
                    logger.info(f"Загрузка полных описаний и навыков для {len(results)} вакансий API...")
                    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                        future_to_id = {executor.submit(self.fetch_vacancy_full_details, r["id"]): r for r in results}
                        for future in concurrent.futures.as_completed(future_to_id):
                            rec = future_to_id[future]
                            try:
                                desc, skills = future.result()
                                if desc:
                                    rec["description"] = desc
                                if skills:
                                    rec["skills"] = ", ".join(skills)
                            except Exception:
                                pass
                return results
        except Exception as e:
            logger.warning(f"Официальный API недоступен, переключаемся на Web-режим: {e}")

        # Fallback на Web-сбор с загрузкой полного описания
        return self.fetch_via_web(
            text=text,
            area=area,
            experience=experience,
            search_period=search_period,
            only_with_salary=only_with_salary,
            order_by=order_by,
            max_vacancies=max_vacancies,
            fetch_full_description=fetch_full_description,
        )
