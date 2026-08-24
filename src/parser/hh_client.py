import re
import logging
from typing import List, Dict, Any, Optional
import requests
from bs4 import BeautifulSoup

from src.config import settings

logger = logging.getLogger(__name__)


class HHClient:
    """
    Клиент для HeadHunter с поддержкой официального API и Web-парсинга:
    - Официальный API (api.hh.ru) при наличии HH_ACCESS_TOKEN / авторизации
    - Автоматический Web-fallback при отсутствии токена или блокировках edge-антибота
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
        """Парсинг строки зарплаты из HTML (например 'от 120 000 до 180 000 руб.')."""
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

    def fetch_via_web(
        self,
        text: str = "Python",
        area: Optional[str] = "113",
        experience: Optional[str] = None,
        only_with_salary: bool = False,
        max_vacancies: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Поиск и сбор вакансий через Web-интерфейс hh.ru (не требует API ключей).
        """
        results: List[Dict[str, Any]] = []
        page = 0
        seen_ids = set()

        exp_map = {
            "noExperience": "noExperience",
            "between1And3": "between1And3",
            "between3And6": "between3And6",
            "moreThan6": "moreThan6",
        }

        while len(results) < max_vacancies:
            params: Dict[str, Any] = {
                "text": text,
                "page": page,
                "items_on_page": min(max_vacancies - len(results) + 5, 50),
                "order_by": "publication_time",
            }
            if area:
                params["area"] = area
            if experience and experience in exp_map:
                params["experience"] = exp_map[experience]
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

                    # Ищем карточку вакансии (поднимаемся по дереву DOM)
                    card = (
                        link.find_parent(attrs={"data-qa": re.compile(r"vacancy-serp__vacancy", re.I)})
                        or link.find_parent("div", class_=re.compile(r"vacancy-card|serp-item", re.I))
                        or link.find_parent("li")
                        or link.find_parent("div")
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

                    # Требования и сниппет
                    snippet_parts = []
                    if card:
                        for snip in card.find_all(attrs={"data-qa": re.compile(r"snippet|requirement|responsibility", re.I)}):
                            txt = snip.get_text(" ", strip=True)
                            if txt and txt not in snippet_parts:
                                snippet_parts.append(txt)

                        if not snippet_parts:
                            for snip in card.find_all(["div", "p", "span"], class_=re.compile(r"snippet|requirement|responsibility", re.I)):
                                txt = snip.get_text(" ", strip=True)
                                if txt and txt not in snippet_parts:
                                    snippet_parts.append(txt)

                    description = " | ".join(snippet_parts) if snippet_parts else f"Позиция {title} в компании {employer}"

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
                        "published_at": "",
                        "description": description,
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

        return results

    def fetch_and_normalize_vacancies(
        self,
        text: str = "Python",
        area: Optional[str] = "113",
        experience: Optional[str] = None,
        only_with_salary: bool = False,
        max_vacancies: int = 20,
        fetch_full_description: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Основной метод сбора: сначала пробует официальный API,
        при 403 (отсутствии ключа) мягко переключается на веб-сбор.
        """
        try:
            url = f"{self.base_url}/vacancies"
            params = {
                "text": text,
                "area": area or "113",
                "per_page": min(max_vacancies, 50),
                "page": 0,
                "order_by": "publication_time",
            }
            if experience:
                params["experience"] = experience
            if only_with_salary:
                params["only_with_salary"] = "true"

            resp = requests.get(url, headers=self.api_headers, params=params, timeout=10)
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                results = []
                for item in items[:max_vacancies]:
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
                if results:
                    return results
        except Exception as e:
            logger.warning(f"Официальный API недоступен, переключаемся на Web-режим: {e}")

        # Fallback на Web-сбор
        return self.fetch_via_web(
            text=text,
            area=area,
            experience=experience,
            only_with_salary=only_with_salary,
            max_vacancies=max_vacancies,
        )
