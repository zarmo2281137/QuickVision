import base64
import html
import io
import os
import re
import time
import keyboard
import mss
from PIL import Image
import requests
from dotenv import load_dotenv
from groq import Groq

# Завантажуємо секретні ключі з локального файлу .env
load_dotenv()

# ================= НАЛАШТУВАННЯ =================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Ключі Gemini передаються через кому в .env: GEMINI_API_KEYS=key1,key2,key3
raw_gemini_keys = os.getenv("GEMINI_API_KEYS", "")
GEMINI_API_KEYS = [k.strip() for k in raw_gemini_keys.split(",") if k.strip()]

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

conversation_history_gemini = []
conversation_history_groq = []
screenshot_batch = []
# ================================================


def validate_config():
    """Перевіряє наявність обов'язкових змінних середовища перед запуском."""
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not GEMINI_API_KEYS and not GROQ_API_KEY:
        missing.append("GEMINI_API_KEYS або GROQ_API_KEY")

    if missing:
        raise ValueError(
            f"Відсутні змінні середовища у файлі .env: {', '.join(missing)}"
        )


def format_code_for_telegram(text):
    """Форматує блоки коду Markdown під HTML для Telegram."""
    parts = re.split(r"(```[\s\S]*?```)", text)
    formatted_result = []

    for part in parts:
        if part.startswith("```") and part.endswith("```"):
            lines = part[3:-3].strip().split("\n")
            lang = (
                lines[0].strip()
                if lines and not lines[0].startswith(" ") and len(lines[0]) < 15
                else ""
            )
            code_content = "\n".join(lines[1:]) if lang else "\n".join(lines)
            escaped_code = html.escape(code_content)
            formatted_result.append(f"<pre><code>{escaped_code}</code></pre>")
        else:
            formatted_result.append(html.escape(part))

    return "".join(formatted_result)


def send_telegram_message(text, is_raw_html=False):
    """Надсилає повідомлення у Telegram із розбиттям на частини до 4000 символів."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    max_length = 4000

    chunks = (
        [text[i : i + max_length] for i in range(0, len(text), max_length)]
        if len(text) > max_length
        else [text]
    )

    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk if is_raw_html else html.escape(chunk),
            "parse_mode": "HTML",
        }
        try:
            res = requests.post(url, json=payload, timeout=10)
            if not res.ok:
                payload.pop("parse_mode", None)
                requests.post(url, json=payload, timeout=10)
        except Exception as e:
            print(f"❌ Помилка відправки у Telegram: {e}")
        time.sleep(0.3)


def capture_screen_base64():
    """Захоплює екран та стискає зображення у JPEG (Base64) для зменшення трафіку."""
    with mss.mss() as sct:
        monitor = sct.monitors[1]
        sct_img = sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        buffered = io.BytesIO()
        img.save(buffered, format="JPEG", quality=85, optimize=True)
        return base64.b64encode(buffered.getvalue()).decode("utf-8")


def add_screenshot_to_batch():
    """Додає скриншот у чергу."""
    try:
        img_b64 = capture_screen_base64()
        screenshot_batch.append(img_b64)
        count = len(screenshot_batch)
        print(f"\n📸 Додано скриншот #{count} у чергу.")
        send_telegram_message(
            f"📸 <b>Скриншот #{count} додано.</b>\n"
            f"• <code>Ctrl + ~</code> — Аналіз коду / дебагінг\n"
            f"• <code>Alt + ~</code> — Аналіз теорії / графіків",
            is_raw_html=True,
        )
    except Exception as e:
        print(f"❌ Помилка зчитування екрана: {e}")


def ask_gemini(images_base64_list, prompt):
    """Запит до Gemini API з ротацією ключів та лімітом токенів 6144."""
    if not GEMINI_API_KEYS:
        raise Exception("Список GEMINI_API_KEYS порожній.")

    last_exception = None
    user_parts = [{"text": prompt}]
    for img_b64 in images_base64_list:
        user_parts.append(
            {"inline_data": {"mime_type": "image/jpeg", "data": img_b64}}
        )

    for idx, api_key in enumerate(GEMINI_API_KEYS, start=1):
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-3.5-flash:generateContent?key={api_key}"
        )
        contents_payload = conversation_history_gemini + [
            {"role": "user", "parts": user_parts}
        ]

        payload = {
            "contents": contents_payload,
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 6144,
            },
        }

        try:
            print(f"[2/3] Спроба через Gemini (Ключ #{idx})...")
            response = requests.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            result = response.json()

            if "error" in result:
                raise Exception(
                    result["error"].get("message", "Помилка Gemini API")
                )

            answer = result["candidates"][0]["content"]["parts"][0]["text"]

            conversation_history_gemini.append(
                {
                    "role": "user",
                    "parts": [{"text": f"Серія з {len(images_base64_list)} скриншотів"}],
                }
            )
            conversation_history_gemini.append(
                {"role": "model", "parts": [{"text": answer}]}
            )
            if len(conversation_history_gemini) > 6:
                conversation_history_gemini[:] = conversation_history_gemini[-6:]

            return answer, f"Gemini (Ключ #{idx})"

        except Exception as e:
            print(f"⚠️ Gemini (Ключ #{idx}) не відповів: {e}")
            last_exception = e

    raise Exception(
        f"Усі ключі Gemini не працюють. Остання помилка: {last_exception}"
    )


def ask_groq(images_base64_list, prompt):
    """Запит до Groq (резервний канал Failover)."""
    if not groq_client:
        raise Exception("GROQ_API_KEY не вказано.")

    content_list = [{"type": "text", "text": prompt}]
    for img_b64 in images_base64_list:
        content_list.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
            }
        )

    messages = conversation_history_groq + [
        {"role": "user", "content": content_list}
    ]

    completion = groq_client.chat.completions.create(
        model="qwen/qwen3.8-27b",
        messages=messages,
        temperature=0.1,
        max_tokens=2048,
    )
    answer = completion.choices[0].message.content

    conversation_history_groq.append(
        {"role": "user", "content": f"Серія з {len(images_base64_list)} скриншотів"}
    )
    conversation_history_groq.append({"role": "assistant", "content": answer})
    if len(conversation_history_groq) > 6:
        conversation_history_groq[:] = conversation_history_groq[-6:]

    return answer


def process_and_send(mode="coding"):
    """Обробляє чергу скриншотів та надсилає результат у Telegram."""
    global screenshot_batch
    if not screenshot_batch:
        print("\n⚠️ Буфер скриншотів порожній! Спочатку натисніть ~.")
        send_telegram_message("⚠️ Буфер скриншотів порожній.")
        return

    count = len(screenshot_batch)
    try:
        mode_title = (
            "💻 АНАЛІЗ КОДУ" if mode == "coding" else "📊 ЗАГАЛЬНИЙ АНАЛІЗ"
        )
        print(f"\n[1/3] Обробка [{mode_title}] для {count} скриншот(ів)...")
        send_telegram_message(
            f"⏳ <b>Аналізую скриншоти ({mode_title})...</b>", is_raw_html=True
        )

        if mode == "coding":
            prompt = (
                f"Тобі надано {count} скриншот(ів) із кодом або алгоритмічною задачею.\n"
                "Напиши ПОВНИЙ, ЕФЕКТИВНИЙ ТА РОБОЧИЙ КОД НА PYTHON у блоці коду (```python ... ```).\n"
                "ВИМОГИ:\n"
                "1. Алгоритм має бути оптимальним за часом виконання.\n"
                "2. Використовуй швидке зчитування даних через sys.stdin.read().split(), якщо це задача з вводом.\n"
                "3. Напиши код повністю без скорочень.\n"
                "4. Після коду додай 1–2 короткі речення з поясненням ключової ідеї або виправленої помилки.\n"
                "5. Мова відповіді — українська."
            )
        else:
            prompt = (
                f"Тобі надано {count} скриншот(ів).\n"
                "Проаналізуй візуальний вміст (графік, схему, текст або питання).\n"
                "Дай чітку, структуровану та стислу відповідь українською мовою."
            )

        try:
            answer, source = ask_gemini(screenshot_batch, prompt)
        except Exception as e_gemini:
            print(f"⚠️ Gemini недоступний, переходжу на Groq: {e_gemini}")
            try:
                answer = ask_groq(screenshot_batch, prompt)
                source = "Groq (Qwen 3.8)"
            except Exception as e_groq:
                raise Exception(f"Gemini: {e_gemini} | Groq: {e_groq}")

        print(f"[3/3] Отримано відповідь від {source}:\n{answer}\n")
        formatted_message = (
            f"💡 <b>Відповідь [{mode_title}] ({source}):</b>\n\n"
            + format_code_for_telegram(answer)
        )
        send_telegram_message(formatted_message, is_raw_html=True)

    except Exception as e:
        print(f"\n❌ ПОМИЛКА: {e}\n")
        send_telegram_message(f"⚠️ Помилка: {e}")
    finally:
        screenshot_batch = []


def clear_all():
    """Очищає буфер скриншотів та історію діалогу."""
    global conversation_history_gemini, conversation_history_groq, screenshot_batch
    conversation_history_gemini = []
    conversation_history_groq = []
    screenshot_batch = []
    print("\n🗑️ Буфер скриншотів та історію очищено.")
    send_telegram_message("🗑️ Буфер скриншотів та історію очищено.")


def main():
    validate_config()
    print("🚀 QuickVision AI запущено!")
    print("👉 '~' — Додати скриншот у чергу")
    print("👉 'Ctrl + ~' — Аналіз коду / дебагінг")
    print("👉 'Alt + ~' — Загальний аналіз (схеми, теорія)")
    print("👉 'Shift + ~' — Очистити буфер та історію")

    while True:
        try:
            if keyboard.is_pressed("`") or keyboard.is_pressed("~"):
                if keyboard.is_pressed("shift"):
                    clear_all()
                elif keyboard.is_pressed("ctrl"):
                    process_and_send(mode="coding")
                elif keyboard.is_pressed("alt"):
                    process_and_send(mode="general")
                else:
                    add_screenshot_to_batch()

                while keyboard.is_pressed("`") or keyboard.is_pressed("~"):
                    time.sleep(0.05)
        except Exception:
            pass

        time.sleep(0.05)


if __name__ == "__main__":
    main()
