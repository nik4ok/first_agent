import sys
from pathlib import Path

# Добавляем корень проекта в sys.path
ROOT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT_DIR))

from src.parser import HHClient, ExcelStorage
from src.config import settings


def run_test():
    print("🚀 === ТЕСТ МОДУЛЯ 1: СБОР ВАКАНСИЙ И СОХРАНЕНИЕ В EXCEL ===")
    print(f"📁 Путь к данным: {settings.DATA_DIR}")
    print(f"📊 Файл Excel: {settings.EXCEL_PATH}")

    client = HHClient()
    storage = ExcelStorage()

    search_query = "Python"
    max_count = 5

    print(f"\n🔍 Запрос к HeadHunter API:")
    print(f"   • Поисковая фраза: '{search_query}'")
    print(f"   • Регион: {settings.SEARCH_AREA} (Россия)")
    print(f"   • Опыт: {settings.SEARCH_EXPERIENCE}")
    print(f"   • Количество для сбора: {max_count}")

    print("\n⏳ Получение вакансий...")
    vacancies = client.fetch_and_normalize_vacancies(
        text=search_query,
        area=settings.SEARCH_AREA,
        experience=settings.SEARCH_EXPERIENCE,
        max_vacancies=max_count,
        fetch_full_description=True,
    )

    print(f"✅ Найдено и получено: {len(vacancies)} вакансий.")

    if not vacancies:
        print("⚠️ Вакансий по запросу не найдено.")
        return

    print("\n📋 Примеры собранных вакансий:")
    for i, v in enumerate(vacancies, 1):
        skills_str = v['skills'] if v['skills'] else 'не указаны'
        print(f"  {i}. [{v['id']}] {v['title']} в '{v['employer']}' ({v['city']})")
        print(f"     Зарплата: {v['salary_str']} | Навыки: {skills_str[:60]}...")
        print(f"     Ссылка: {v['url']}")

    print("\n💾 Сохранение в Excel...")
    added_count = storage.save_new_vacancies(vacancies)
    print(f"🎉 Успешно сохранено новых записей: {added_count} в {settings.EXCEL_PATH.name}")

    total_df = storage.load_all()
    print(f"📈 Всего записей в локальной базе Excel: {len(total_df)}")
    print("\n✅ Тест успешно завершен!")


if __name__ == "__main__":
    run_test()
