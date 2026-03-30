#!/usr/bin/env python3
"""
Image Bot — Telegram бот-генератор картинок
Генерирует изображения по текстовому описанию.
"""

import asyncio
import base64
import json
import logging
import os
import uuid

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_URL = os.getenv("OPENROUTER_BASE_URL", "http://82.24.110.51:20128/v1")
MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/google/gemini-2.5-flash-image")

# Style presets
STYLES = {
    "realistic": "photorealistic, high detail, professional photography",
    "anime": "anime style, Japanese animation, vibrant colors, clean lines",
    "watercolor": "watercolor painting, soft edges, artistic, flowing colors",
    "cyberpunk": "cyberpunk, neon lights, futuristic, dark atmosphere",
    "minimal": "minimalist, clean, simple, geometric, modern",
    "oil": "oil painting, rich textures, classical art style",
    "3d": "3D render, octane render, volumetric lighting, hyperrealistic",
    "sketch": "pencil sketch, hand-drawn, black and white, detailed lines",
}

SYSTEM_PROMPT = """You are an image generation AI. Generate images based on user descriptions.
Return the image as base64-encoded data in your response.
Make images visually stunning, detailed, and matching the user's description."""


async def generate_image(prompt: str, style: str = "") -> bytes | None:
    """Generate image via AI API."""
    url = f"{API_URL}/chat/completions"
    headers = {"Content-Type": "application/json"}

    full_prompt = prompt
    if style and style in STYLES:
        full_prompt = f"{prompt}. Style: {STYLES[style]}"

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Generate an image: {full_prompt}"},
        ],
        "max_tokens": 4000,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=120)) as resp:
                if resp.status != 200:
                    error = await resp.text()
                    logger.error(f"API error: {resp.status} - {error}")
                    return None

                data = await resp.json()

                # Check for image in response
                choices = data.get("choices", [])
                if choices:
                    message = choices[0].get("message", {})
                    content = message.get("content", "")

                    # Check if content contains base64 image
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict):
                                if part.get("type") == "image_url":
                                    img_data = part.get("image_url", {}).get("url", "")
                                    if img_data.startswith("data:image"):
                                        b64 = img_data.split(",", 1)[1]
                                        return base64.b64decode(b64)

                    # Check for base64 in text
                    if "base64" in str(content):
                        import re
                        match = re.search(r'([A-Za-z0-9+/]{100,}={0,2})', str(content))
                        if match:
                            try:
                                return base64.b64decode(match.group(1))
                            except Exception:
                                pass

                    # Check for image URL
                    if isinstance(content, str) and content.startswith("http"):
                        async with session.get(content) as img_resp:
                            if img_resp.status == 200:
                                return await img_resp.read()

                return None

    except Exception as e:
        logger.error(f"Image generation error: {e}")
        return None


def get_style_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    names = {
        "realistic": "📸 Реализм",
        "anime": "🎌 Аниме",
        "watercolor": "🎨 Акварель",
        "cyberpunk": "🌆 Киберпанк",
        "minimal": "⬜ Минимализм",
        "oil": "🖼 Масло",
        "3d": "🧊 3D",
        "sketch": "✏️ Скетч",
    }
    for sid, name in names.items():
        row.append(InlineKeyboardButton(text=name, callback_data=f"style:{sid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


router = Router()
user_styles: dict[int, str] = {}


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎨 <b>Image Bot</b>\n\n"
        "Генерирую картинки по описанию.\n\n"
        "<b>Как пользоваться:</b>\n"
        "1️⃣ Опиши что нарисовать\n"
        "2️⃣ (Опционально) Выбери стиль\n"
        "3️⃣ Получи картинку\n\n"
        "<b>Примеры:</b>\n"
        "• Кот в космосе\n"
        "• Закат над морем, акварель\n"
        "• Футуристический город, киберпанк\n\n"
        "/styles — выбрать стиль\n"
        "/help — помощь",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Стили:</b>\n\n"
        "📸 Реализм — фотореализм\n"
        "🎌 Аниме — японская анимация\n"
        "🎨 Акварель — мягкие краски\n"
        "🌆 Киберпанк — неон и футуризм\n"
        "⬜ Минимализм — чистый дизайн\n"
        "🖼 Масло — масляная живопись\n"
        "🧊 3D — трёхмерный рендер\n"
        "✏️ Скетч — карандашный набросок\n\n"
        "Напиши /styles чтобы выбрать, или просто опиши картинку.",
        parse_mode="HTML",
    )


@router.message(Command("styles"))
async def cmd_styles(message: Message):
    await message.answer("Выбери стиль:", reply_markup=get_style_keyboard())


@router.callback_query(F.data.startswith("style:"))
async def cb_style(callback: CallbackQuery):
    style = callback.data[6:]
    user_styles[callback.from_user.id] = style
    names = {
        "realistic": "📸 Реализм", "anime": "🎌 Аниме", "watercolor": "🎨 Акварель",
        "cyberpunk": "🌆 Киберпанк", "minimal": "⬜ Минимализм", "oil": "🖼 Масло",
        "3d": "🧊 3D", "sketch": "✏️ Скетч",
    }
    await callback.answer(f"Стиль: {names.get(style, style)}")
    await callback.message.answer(
        f"Стиль: <b>{names.get(style, style)}</b>\n\nТеперь опиши что нарисовать 👇",
        parse_mode="HTML",
    )


@router.message(F.text & ~F.text.startswith("/"))
async def handle_request(message: Message):
    prompt = message.text.strip()
    if len(prompt) < 3:
        await message.answer("Опиши что нарисовать 🎨")
        return

    style = user_styles.get(message.from_user.id, "")
    status = await message.answer("🎨 Генерирую картинку...")

    try:
        image_data = await generate_image(prompt, style)

        if image_data:
            await status.delete()
            filename = f"image-{uuid.uuid4().hex[:8]}.png"
            await message.answer_photo(
                photo=BufferedInputFile(image_data, filename=filename),
                caption=f"🎨 <b>{prompt}</b>" + (f"\nСтиль: {style}" if style else ""),
                parse_mode="HTML",
            )
        else:
            await status.edit_text(
                "❌ Не удалось сгенерировать.\n\n"
                "Попробуй:\n"
                "• Другой промпт\n"
                "• Короче описание\n"
                "• Выбери стиль через /styles"
            )

    except Exception as e:
        logger.error(f"Error: {e}")
        await status.edit_text(f"❌ Ошибка: {e}")


async def main():
    if not BOT_TOKEN:
        print("❌ Missing BOT_TOKEN")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    print("🎨 Image Bot started!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
