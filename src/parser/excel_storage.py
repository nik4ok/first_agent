import os
from pathlib import Path
from typing import List, Dict, Any, Set, Tuple
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from src.config import settings


class ExcelStorage:
    """Управление локальной базой вакансий в формате Excel (.xlsx)."""

    COLUMNS = [
        "id",
        "status",
        "match_score",
        "title",
        "employer",
        "city",
        "salary_str",
        "skills",
        "url",
        "published_at",
        "cover_letter",
        "notes",
        "description",
        "salary_from",
        "salary_to",
        "currency",
    ]

    COLUMN_NAMES_RU = {
        "id": "ID Вакансии",
        "status": "Статус",
        "match_score": "Score (%)",
        "title": "Название вакансии",
        "employer": "Компания",
        "city": "Город",
        "salary_str": "Зарплата",
        "skills": "Ключевые навыки",
        "url": "Ссылка",
        "published_at": "Дата публикации",
        "cover_letter": "Сопроводительное письмо",
        "notes": "Заметки / Резюме анализа",
        "description": "Полное описание",
        "salary_from": "ЗП от",
        "salary_to": "ЗП до",
        "currency": "Валюта",
    }

    def __init__(self, file_path: Path = settings.EXCEL_PATH):
        self.file_path = file_path

    def get_existing_ids(self) -> Set[str]:
        """Возвращает множество уже сохраненных ID вакансий, чтобы избежать дубликатов."""
        if not self.file_path.exists():
            return set()
        try:
            df = pd.read_excel(self.file_path, dtype={"id": str, "ID Вакансии": str})
            col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"
            if col in df.columns:
                return set(df[col].dropna().astype(str).tolist())
        except Exception:
            return set()
        return set()

    def load_all(self) -> pd.DataFrame:
        """Загрузка всех сохраненных вакансий в DataFrame."""
        if not self.file_path.exists():
            return pd.DataFrame(columns=list(self.COLUMN_NAMES_RU.values()))
        return pd.read_excel(self.file_path, dtype=str)

    def clear_all(self) -> bool:
        """Полная очистка локальной базы вакансий (создает чистый Excel-файл с заголовками)."""
        empty_df = pd.DataFrame(columns=list(self.COLUMN_NAMES_RU.values()))
        self._write_styled_excel(empty_df)
        return True

    def save_or_update_vacancies(self, vacancies: List[Dict[str, Any]]) -> Tuple[int, int]:
        """
        Сохраняет новые вакансии и обновляет данные (описание, скор, навыки) для уже существующих.
        Возвращает (added_count, updated_count).
        """
        if not vacancies:
            return 0, 0

        existing_df = self.load_all()
        id_col = "ID Вакансии" if "ID Вакансии" in existing_df.columns else "id"

        existing_ids = set()
        if not existing_df.empty and id_col in existing_df.columns:
            existing_ids = set(existing_df[id_col].dropna().astype(str).tolist())

        new_items = []
        updated_count = 0

        # Обновляем существующие записи в DataFrame
        for v in vacancies:
            v_id = str(v.get("id"))
            if v_id in existing_ids:
                mask = existing_df[id_col].astype(str) == v_id
                if mask.any():
                    # Обновляем поля, если в новых данных есть значения
                    for key, val in v.items():
                        ru_col = self.COLUMN_NAMES_RU.get(key, key)
                        if ru_col in existing_df.columns and val is not None and str(val).strip():
                            existing_df.loc[mask, ru_col] = str(val)
                    updated_count += 1
            else:
                new_items.append(v)

        if new_items:
            new_df = pd.DataFrame(new_items)
            new_df = new_df.rename(columns=self.COLUMN_NAMES_RU)
            if not existing_df.empty:
                combined_df = pd.concat([existing_df, new_df], ignore_index=True)
            else:
                combined_df = new_df
        else:
            combined_df = existing_df

        self._write_styled_excel(combined_df)
        return len(new_items), updated_count

    def save_new_vacancies(self, vacancies: List[Dict[str, Any]]) -> int:
        """
        Добавляет новые вакансии и обновляет существующие.
        Возвращает общее число добавленных + обновленных.
        """
        added, updated = self.save_or_update_vacancies(vacancies)
        return added if added > 0 else updated

    def update_status(
        self,
        vacancy_id: str,
        status: str,
        match_score: Any = None,
        cover_letter: str = "",
        notes: str = "",
    ) -> bool:
        """Обновление статуса, скора или сопроводительного письма для конкретной вакансии."""
        if not self.file_path.exists():
            return False

        df = pd.read_excel(self.file_path, dtype=str)
        id_col = "ID Вакансии" if "ID Вакансии" in df.columns else "id"

        mask = df[id_col].astype(str) == str(vacancy_id)
        if not mask.any():
            return False

        if "Статус" in df.columns:
            df.loc[mask, "Статус"] = status
        if match_score is not None and "Score (%)" in df.columns:
            df.loc[mask, "Score (%)"] = str(match_score)
        if cover_letter and "Сопроводительное письмо" in df.columns:
            df.loc[mask, "Сопроводительное письмо"] = cover_letter
        if notes and "Заметки / Резюме анализа" in df.columns:
            df.loc[mask, "Заметки / Резюме анализа"] = notes

        self._write_styled_excel(df)
        return True

    def _write_styled_excel(self, df: pd.DataFrame):
        """Красивое сохранение с авто-шириной столбцов и стилизацией шапки."""
        with pd.ExcelWriter(self.file_path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Вакансии")
            worksheet = writer.sheets["Вакансии"]

            # Цвета и стили
            header_fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
            header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
            border = Border(
                left=Side(style="thin", color="E0E0E0"),
                right=Side(style="thin", color="E0E0E0"),
                top=Side(style="thin", color="E0E0E0"),
                bottom=Side(style="thin", color="E0E0E0"),
            )

            # Форматирование шапки
            for col_idx, cell in enumerate(worksheet[1], 1):
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

            # Настройка ширины колонок и выравнивания ячеек
            for col in worksheet.columns:
                max_len = 0
                col_letter = get_column_letter(col[0].column)
                col_name = str(col[0].value)

                for cell in col:
                    cell.border = border
                    val_str = str(cell.value or "")
                    if cell.row > 1:
                        cell.font = Font(name="Arial", size=10)
                        # Выравнивание по левому краю для текста, по центру для статусов
                        if col_name in ["ID Вакансии", "Статус", "Score (%)", "Дата публикации", "Город"]:
                            cell.alignment = Alignment(horizontal="center", vertical="center")
                        else:
                            cell.alignment = Alignment(horizontal="left", vertical="center")

                    max_len = max(max_len, len(val_str.split("\n")[0]))

                # Задаем адаптивную ширину колонок
                if col_name in ["Название вакансии", "Компания"]:
                    worksheet.column_dimensions[col_letter].width = 30
                elif col_name in ["Зарплата", "Ключевые навыки"]:
                    worksheet.column_dimensions[col_letter].width = 25
                elif col_name in ["Сопроводительное письмо", "Заметки / Резюме анализа", "Полное описание"]:
                    worksheet.column_dimensions[col_letter].width = 40
                elif col_name == "Ссылка":
                    worksheet.column_dimensions[col_letter].width = 28
                else:
                    worksheet.column_dimensions[col_letter].width = max(max_len + 3, 14)
