import os
import warnings
import requests
warnings.filterwarnings("ignore")

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

# --- НАСТРОЙКА ПОДПИСКИ CURSOR PRO ---
# Берем официальный ключ подписки, который Cursor сам прокидывает в ваш терминал
cursor_key = os.getenv("CURSOR_API_KEY", "not-needed")

model = ChatOpenAI(
    model="gpt-4o", # С подпиской Pro вам доступна самая мощная gpt-4o или claude-3-5-sonnet
    openai_api_key=cursor_key
    # Никаких localhost и сторонних портов больше не пишем!
)

# --- НАШ НАДЕЖНЫЙ СКИЛ ПОИСКА ---
@tool
def search_the_web(query: str) -> str:
    """Используй этот инструмент, чтобы искать актуальную информацию в интернете, новости или факты."""
    try:
        url = f"https://duckduckgo.com{query}"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(response.text, 'html.parser')
            results = [snippet.get_text() for snippet in soup.find_all('a', class_='result__snippet')][:3]
            if results:
                return "\n\n".join(results)
        return "Поиск не дал результатов."
    except Exception as e:
        return f"Ошибка поиска: {str(e)}"

my_skills = [search_the_web]
agent = create_react_agent(model, my_skills)

def ask_agent(prompt: str):
    print(f"\n👤 Вы: {prompt}")
    inputs = {"messages": [("user", prompt)]}
    for chunk in agent.stream(inputs, stream_mode="values"):
        last_message = chunk["messages"][-1]
        if hasattr(last_message, 'tool_calls') and last_message.tool_calls:
            for call in last_message.tool_calls:
                print(f"🤖 Агент: Мне не хватает знаний. Активирую скил поиска '{call['name']}'...")
    print(f"\n🤖 Агент: {last_message.content}")

if __name__ == "__main__":
    ask_agent("Какие главные технологические новости произошли за последнюю неделю?")
