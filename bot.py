import asyncio
import json
import logging
import os
import random
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineQueryResultArticle, InputTextMessageContent, Message
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
INSTITUTION_ID = os.getenv("INSTITUTION_ID", "75")
MAX_INIT_DATA = os.getenv("MAX_INIT_DATA", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "maxscheduleetu_bot").lower()
API_BASE = "https://schedule.vk-apps.com/v1"
SESSION_FILE = Path(os.getenv("SCHEDULE_SESSION_FILE", "schedule_session.json"))
SESSION_REFRESH_INTERVAL = max(0, int(os.getenv("SESSION_REFRESH_INTERVAL_MINUTES", "0")))
SESSION_REFRESH_JITTER = min(
    0.9,
    max(0.0, float(os.getenv("SESSION_REFRESH_JITTER_PERCENT", "20")) / 100),
)
schedule_auth_token: str | None = None
schedule_refresh_token: str | None = None


def load_session() -> None:
    global schedule_auth_token, schedule_refresh_token
    if not SESSION_FILE.exists():
        schedule_auth_token = os.getenv("SCHEDULE_AUTH_TOKEN") or None
        return
    try:
        data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
        schedule_auth_token = data.get("auth_token")
        schedule_refresh_token = data.get("refresh_token")
    except (OSError, json.JSONDecodeError):
        logging.warning("Не удалось прочитать файл сессии %s", SESSION_FILE)


def save_session(auth_token: str, refresh_token: str | None = None) -> None:
    global schedule_auth_token, schedule_refresh_token
    schedule_auth_token = auth_token
    if refresh_token:
        schedule_refresh_token = refresh_token
    SESSION_FILE.write_text(
        json.dumps({
            "auth_token": schedule_auth_token,
            "refresh_token": schedule_refresh_token,
        }, ensure_ascii=False),
        encoding="utf-8",
    )

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в файле .env")

router = Router()


async def authenticate_schedule_api() -> str:
    if not MAX_INIT_DATA:
        raise RuntimeError(
            "Не задан MAX_INIT_DATA. Получите init_data из мини-приложения расписания "
            "и добавьте его в .env."
        )

    timeout = aiohttp.ClientTimeout(total=20)
    headers = {
        "Origin": "https://digital-uni-schedule.cdn-vk.ru",
        "Referer": "https://digital-uni-schedule.cdn-vk.ru/",
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    }
    payload = {"init_data": MAX_INIT_DATA, "timezone": "Europe/Moscow"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(f"{API_BASE}/auth", json=payload) as response:
            response.raise_for_status()
            data = await response.json()
            token = data.get("auth_token")
            if not token:
                raise RuntimeError("API не вернул auth_token")
            save_session(token, data.get("refresh_token"))
            return token


async def refresh_schedule_api() -> str:
    if not schedule_refresh_token:
        raise RuntimeError("Нет refresh_token")
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {
        "Origin": "https://digital-uni-schedule.cdn-vk.ru",
        "Referer": "https://digital-uni-schedule.cdn-vk.ru/",
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(
            f"{API_BASE}/auth/refresh",
            json={"refresh_token": schedule_refresh_token},
        ) as response:
            response.raise_for_status()
            data = await response.json()
            token = data.get("auth_token") or data.get("access_token")
            if not token:
                raise RuntimeError("API не вернул новый auth_token")
            save_session(token, data.get("refresh_token", schedule_refresh_token))
            return token


async def session_refresh_loop() -> None:
    if SESSION_REFRESH_INTERVAL == 0:
        logging.info("Периодический refresh отключён; токен обновляется только после 401")
        return
    while True:
        variation = random.uniform(-SESSION_REFRESH_JITTER, SESSION_REFRESH_JITTER)
        interval_seconds = max(60, SESSION_REFRESH_INTERVAL * 60 * (1 + variation))
        await asyncio.sleep(interval_seconds)
        if not schedule_refresh_token:
            continue
        try:
            await refresh_schedule_api()
            logging.info("Сессия API расписания автоматически обновлена; следующий интервал: %.1f мин.", interval_seconds / 60)
        except Exception:
            logging.warning("Refresh-токен отклонён, выполняем новую авторизацию")
            try:
                await authenticate_schedule_api()
                logging.info("Сессия API расписания восстановлена через MAX_INIT_DATA")
            except Exception:
                logging.exception(
                    "Не удалось восстановить сессию. Проверьте, что MAX_INIT_DATA в .env не истёк"
                )


async def api_get(path: str, params: dict[str, Any], retry_auth: bool = True) -> Any:
    global schedule_auth_token
    timeout = aiohttp.ClientTimeout(total=20)
    headers = {
        "Origin": "https://digital-uni-schedule.cdn-vk.ru",
        "Referer": "https://digital-uni-schedule.cdn-vk.ru/",
        "User-Agent": "Mozilla/5.0",
    }
    if schedule_auth_token:
        headers["Authorization"] = f"Bearer {schedule_auth_token}"
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(f"{API_BASE}{path}", params=params) as response:
            if response.status == 401 and retry_auth:
                try:
                    schedule_auth_token = await refresh_schedule_api()
                except Exception:
                    logging.info("Refresh-токен не сработал, выполняем новую авторизацию")
                    schedule_auth_token = await authenticate_schedule_api()
                return await api_get(path, params, retry_auth=False)
            response.raise_for_status()
            return await response.json()


async def find_teachers(query: str = "") -> list[dict[str, Any]]:
    params = {"limit": 20, "offset": 0}
    if query:
        params["name"] = query
    data = await api_get(f"/institutions/{INSTITUTION_ID}/teachers", params)
    return data if isinstance(data, list) else data.get("items", data.get("teachers", []))


async def get_schedule(teacher_id: str, selected_date: date) -> list[dict[str, Any]]:
    data = await api_get("/schedules", {
        "entity_type": "teacher", "entity_id": teacher_id,
        "date_from": selected_date.isoformat(), "date_to": selected_date.isoformat(),
    })
    return data if isinstance(data, list) else data.get("items", [])


def format_schedule(teacher_id: str, selected_date: date, data: list[dict[str, Any]]) -> str:
    slots = data[0].get("slots", []) if data else []
    lines = [f"Расписание преподавателя ID {teacher_id}", selected_date.strftime("%d.%m.%Y"), ""]
    if not slots:
        return "\n".join(lines + ["Занятий нет."])

    for slot in slots:
        time_info = slot.get("time", {})
        for event in slot.get("events", []):
            lines.append(f"{slot.get('slot_number', '?')}. {time_info.get('start', '?')}–{time_info.get('end', '?')}")
            lines.append(f"   {event.get('name', 'Без названия')}")
            event_type = event.get("type", {}).get("name")
            if event_type:
                lines.append(f"   Тип: {event_type}")
            groups = ", ".join(g.get("name", "") for g in event.get("groups", []))
            if groups:
                lines.append(f"   Группы: {groups}")
            rooms = []
            for room in event.get("rooms", []):
                building = room.get("building", {}).get("name")
                rooms.append(f"{building}, ауд. {room.get('name')}" if building else str(room.get("name")))
            if rooms:
                lines.append(f"   Место: {'; '.join(rooms)}")
            lines.append("")
    return "\n".join(lines).strip()


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Привет!\n\nКоманды:\n"
        "/teachers — список преподавателей\n"
        "/teachers фамилия — поиск\n"
        "/schedule ID — расписание на сегодня\n"
        "/schedule ID 2026-09-18 — расписание на дату"
    )


async def send_teachers(message: Message, query: str = "") -> None:
    try:
        result = await find_teachers(query)
    except RuntimeError as error:
        logging.error(str(error))
        await message.answer(
            "Нужна авторизация API расписания. Добавьте MAX_INIT_DATA в .env "
            "и перезапустите бота."
        )
        return
    except Exception:
        logging.exception("Ошибка поиска преподавателей")
        await message.answer("Не удалось получить список преподавателей.")
        return
    if not result:
        await message.answer("Преподаватели не найдены.")
        return
    lines = ["Найденные преподаватели:", ""]
    for item in result[:20]:
        lines.append(f"{item.get('name', item.get('full_name', 'Без имени'))} — ID {item.get('id', '?')}")
    await message.answer("\n".join(lines))


@router.message(Command("teachers"))
async def teachers(message: Message) -> None:
    query = message.text.partition(" ")[2].strip() if message.text else ""
    await send_teachers(message, query)


async def build_schedule_response(args: list[str]) -> str:
    if len(args) < 2:
        return (
            f"Использование: @{BOT_USERNAME} schedule ID [дата]\n"
            "Например: @maxscheduleetu_bot schedule 18269 2026-09-18"
        )
    teacher_id = args[1]
    moscow_timezone = timezone(timedelta(hours=3))
    selected_date = datetime.now(moscow_timezone).date()
    if len(args) >= 3:
        try:
            selected_date = datetime.strptime(args[2], "%Y-%m-%d").date()
        except ValueError:
            return "Дата должна быть в формате YYYY-MM-DD."
    return format_schedule(teacher_id, selected_date, await get_schedule(teacher_id, selected_date))


async def send_schedule(message: Message, args: list[str]) -> None:
    try:
        text = await build_schedule_response(args)
    except RuntimeError as error:
        logging.error(str(error))
        await message.answer("Нужна авторизация API расписания. Добавьте MAX_INIT_DATA в .env и перезапустите бота.")
        return
    except (aiohttp.ClientError, asyncio.TimeoutError):
        logging.exception("Ошибка API расписания")
        await message.answer("Сервис расписания временно недоступен.")
        return
    except Exception:
        logging.exception("Неожиданная ошибка")
        await message.answer("Не удалось обработать расписание.")
        return
    await message.answer(text)


@router.message(Command("schedule"))
async def schedule(message: Message) -> None:
    await send_schedule(message, (message.text or "").split())


@router.message(F.text)
async def mentioned_command(message: Message) -> None:
    text = (message.text or "").strip()
    match = re.match(rf"^@{re.escape(BOT_USERNAME)}\s+schedule(?:\s+(.*))?$", text, re.IGNORECASE)
    if match:
        args = ["schedule"] + (match.group(1).split() if match.group(1) else [])
        await send_schedule(message, args)
        return

    teachers_match = re.match(
        rf"^@{re.escape(BOT_USERNAME)}\s+teachers(?:\s+(.*))?$",
        text,
        re.IGNORECASE,
    )
    if teachers_match:
        query = teachers_match.group(1).strip() if teachers_match.group(1) else ""
        await send_teachers(message, query)


@router.guest_message()
async def guest_message(message: Message) -> None:
    """Обрабатывает @maxscheduleetu_bot ... без добавления бота в чат."""
    text = (message.text or "").strip()
    text = re.sub(rf"^@{re.escape(BOT_USERNAME)}\s*", "", text, flags=re.IGNORECASE)
    args = text.split()
    if not args:
        answer = f"Напишите: @{BOT_USERNAME} schedule ID [дата]"
    elif args[0].lower() == "teachers":
        try:
            result = await find_teachers(" ".join(args[1:]))
            if not result:
                answer = "Преподаватели не найдены."
            else:
                lines = ["Найденные преподаватели:", ""]
                for item in result[:20]:
                    lines.append(
                        f"{item.get('name', item.get('full_name', 'Без имени'))} — ID {item.get('id', '?')}"
                    )
                answer = "\n".join(lines)
        except Exception:
            logging.exception("Ошибка guest-поиска преподавателей")
            answer = "Не удалось получить список преподавателей."
    elif args[0].lower() == "schedule":
        try:
            answer = await build_schedule_response(args)
        except RuntimeError:
            answer = "Сервис расписания не авторизован. Владелец бота должен обновить MAX_INIT_DATA."
        except (aiohttp.ClientError, asyncio.TimeoutError):
            answer = "Сервис расписания временно недоступен."
        except Exception:
            logging.exception("Ошибка guest-запроса")
            answer = "Не удалось обработать расписание."
    else:
        answer = f"Доступно: @{BOT_USERNAME} schedule ID [дата] или @{BOT_USERNAME} teachers [фамилия]"

    result = InlineQueryResultArticle(
        id=str(message.guest_query_id or message.message_id),
        title="Расписание",
        input_message_content=InputTextMessageContent(message_text=answer),
    )
    await message.answer_guest_query(result=result)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    load_session()
    bot = Bot(BOT_TOKEN)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    refresh_task = asyncio.create_task(session_refresh_loop())
    try:
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        refresh_task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
