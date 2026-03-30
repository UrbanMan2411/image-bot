#!/usr/bin/env python3
"""
Image Bot v2 — Telegram бот-генератор картинок
С retry, историей, rate limiting, watermark.
"""

import asyncio
import base64
import json
import logging
import os
import re
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from io import BytesIO

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_URL = os.getenv("OPENROUTER_BASE_URL", "http://82.24.110.51:20128/v1")
MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/google/gemini-2.5-flash-image")
MAX_PER_DAY = 20

# State
user_history: dict[int, list[dict]] = defaultdict(list)
user_daily_count: dict[int, int] = defaultdict(int)
user_locks: dict[int, bool] = defaultdict(bool)
user_styles: dict[int, str] = {}
last_reset: datetime = datetime.now()

STYLES = {
    "realistic": "📸 Реализм",
    "anime": "🎌 Аниме",
    "watercolor": "🎨 Акварель",
    "cyberpunk": "🌆 Киберпанк",
    "minimal": "⬜ Минимализм",
    "oil": "🖼 Масло",
    "3d": "🧊 3D",
    "sketch": "✏️ Скетч",
}

STYLE_PROMPTS = {
    "realistic": "photorealistic, high detail, professional photography, 8k",
    "anime": "anime style, Japanese animation, vibrant colors, clean lines",
    "watercolor": "watercolor painting, soft edges, artistic, flowing colors",
    "cyberpunk": "cyberpunk, neon lights, futuristic, dark atmosphere",
    "minimal": "minimalist, clean, simple, geometric, modern design",
    "oil": "oil painting, rich textures, classical art, museum quality",
    "3d": "3D render, octane render, volumetric lighting, hyperrealistic",
    "sketch": "pencil sketch, hand-drawn, black and white, detailed lines",
}

SYSTEM_PROMPT = """You are an image generation AI. Generate images based on user descriptions.
Create stunning, detailed, visually appealing images that match the description."""


async def extract_frame_from_url(video_url: str) -> bytes | None:
    """Extract a frame from video URL using ffmpeg."""
    try:
        import subprocess
        tmp_video = f"/tmp/video-{uuid.uuid4().hex[:8]}.mp4"
        tmp_frame = f"/tmp/frame-{uuid.uuid4().hex[:8]}.jpg"

        # Download video
        async with aiohttp.ClientSession() as session:
            async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    return None
                with open(tmp_video, 'wb') as f:
                    f.write(await resp.read())

        # Extract frame at 2 seconds
        result = subprocess.run(
            ["ffmpeg", "-i", tmp_video, "-ss", "00:00:02", "-frames:v", "1", "-y", tmp_frame],
            capture_output=True, timeout=15,
        )

        if result.returncode == 0 and os.path.exists(tmp_frame):
            with open(tmp_frame, 'rb') as f:
                data = f.read()
            os.remove(tmp_video)
            os.remove(tmp_frame)
            return data

        # Cleanup
        for f in [tmp_video, tmp_frame]:
            if os.path.exists(f):
                os.remove(f)
        return None

    except Exception as e:
        logger.error(f"Frame extraction failed: {e}")
        return None


def check_rate(user_id: int) -> bool:
    global last_reset
    if (datetime.now() - last_reset) > timedelta(days=1):
        user_daily_count.clear()
        last_reset = datetime.now()
    return user_daily_count[user_id] < MAX_PER_DAY


async def generate_image(prompt: str, style: str = "") -> bytes | None:
    """Generate image via AI API with retry."""
    url = f"{API_URL}/chat/completions"
    headers = {"Content-Type": "application/json"}

    full_prompt = prompt
    if style and style in STYLE_PROMPTS:
        full_prompt = f"{prompt}. Style: {STYLE_PROMPTS[style]}"

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Generate an image: {full_prompt}"},
        ],
        "max_tokens": 4000,
    }

    last_error = None
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=120)) as resp:
                    if resp.status != 200:
                        error = await resp.text()
                        raise Exception(f"API error {resp.status}: {error}")

                    data = await resp.json()
                    choices = data.get("choices", [])
                    if not choices:
                        raise Exception("No choices in response")

                    message = choices[0].get("message", {})
                    content = message.get("content", "")

                    # Check for image in multimodal response
                    if isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict):
                                if part.get("type") == "image_url":
                                    img_data = part.get("image_url", {}).get("url", "")
                                    if img_data.startswith("data:image"):
                                        b64 = img_data.split(",", 1)[1]
                                        return base64.b64decode(b64)

                    # Check for base64 in text
                    if isinstance(content, str):
                        match = re.search(r'([A-Za-z0-9+/]{200,}={0,2})', content)
                        if match:
                            try:
                                return base64.b64decode(match.group(1))
                            except Exception:
                                pass
                        # Check for URL
                        if content.startswith("http"):
                            async with session.get(content) as img_resp:
                                if img_resp.status == 200:
                                    return await img_resp.read()

                    raise Exception("No image found in response")

        except Exception as e:
            last_error = e
            logger.warning(f"Attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                await asyncio.sleep(3)

    raise Exception(f"Failed after 3 attempts: {last_error}")


def add_watermark_to_image(image_data: bytes) -> bytes:
    """Add watermark to image using PIL."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(BytesIO(image_data))
        draw = ImageDraw.Draw(img)

        # Small semi-transparent text at bottom right
        text = "LandingAI"
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
        except Exception:
            font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x = img.width - w - 10
        y = img.height - h - 10

        # Semi-transparent background
        draw.rectangle([x - 4, y - 2, x + w + 4, y + h + 2], fill=(0, 0, 0, 128))
        draw.text((x, y), text, fill=(255, 255, 255, 200), font=font)

        output = BytesIO()
        img.save(output, format="PNG")
        return output.getvalue()
    except ImportError:
        return image_data  # PIL not available, return original
    except Exception as e:
        logger.warning(f"Watermark failed: {e}")
        return image_data


def get_style_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for sid, name in STYLES.items():
        row.append(InlineKeyboardButton(text=name, callback_data=f"style:{sid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎨 <b>Image Bot v2</b>\n\n"
        "Генерирую картинки по описанию.\n\n"
        "<b>Команды:</b>\n"
        "/styles — выбрать стиль\n"
        "/history — последние генерации\n"
        "/stats — статистика\n"
        "/help — помощь\n\n"
        "Просто опиши что нарисовать 👇",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "<b>Стили:</b>\n\n"
        "📸 Реализм • 🎌 Аниме • 🎨 Акварель\n"
        "🌆 Киберпанк • ⬜ Минимализм • 🖼 Масло\n"
        "🧊 3D • ✏️ Скетч\n\n"
        "Выбери через /styles или опиши картинку.",
        parse_mode="HTML",
    )


@router.message(Command("styles"))
async def cmd_styles(message: Message):
    await message.answer("Выбери стиль:", reply_markup=get_style_keyboard())


@router.message(Command("history"))
async def cmd_history(message: Message):
    history = user_history.get(message.from_user.id, [])
    if not history:
        await message.answer("История пуста. Сгенерируй первую картинку!")
        return
    text = "<b>📋 Последние генерации:</b>\n\n"
    for i, item in enumerate(reversed(history[-10:]), 1):
        t = item["time"].strftime("%H:%M")
        text += f"{i}. <i>{item['prompt'][:40]}...</i> • {item.get('style', '-')} • {t}\n"
    await message.answer(text, parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    uid = message.from_user.id
    total = len(user_history.get(uid, []))
    today = user_daily_count.get(uid, 0)
    await message.answer(
        f"<b>📊 Статистика</b>\n\n"
        f"Всего картинок: <b>{total}</b>\n"
        f"Сегодня: <b>{today}/{MAX_PER_DAY}</b>",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("style:"))
async def cb_style(callback: CallbackQuery):
    style = callback.data[6:]
    user_styles[callback.from_user.id] = style
    await callback.answer(f"Стиль: {STYLES.get(style, style)}")
    await callback.message.answer(
        f"Стиль: <b>{STYLES.get(style, style)}</b>\n\nОпиши что нарисовать 👇",
        parse_mode="HTML",
    )


@router.message(F.text & ~F.text.startswith("/"))
async def handle_request(message: Message):
    uid = message.from_user.id
    prompt = message.text.strip()
    if len(prompt) < 3:
        await message.answer("Опиши что нарисовать 🎨")
        return

    # Check for video URL — extract frame as reference
    import re as _re
    video_match = _re.search(r'https?://\S+\.(mp4|mov|avi|webm|gif)', prompt, _re.I)
    if video_match:
        if not check_rate(uid):
            await message.answer(f"⚠️ Дневной лимит ({MAX_PER_DAY}). Попробуй завтра.")
            return
        if user_locks[uid]:
            await message.answer("⏳ Подожди...")
            return
        user_locks[uid] = True
        status = await message.answer("🎬 Извлекаю кадр из видео...")
        try:
            frame = await extract_frame_from_url(video_match.group(0))
            if frame:
                user_daily_count[uid] += 1
                await status.delete()
                await message.answer_photo(
                    photo=BufferedInputFile(frame, filename="frame.jpg"),
                    caption="📸 <b>Кадр из видео</b>\n\nТеперь опиши что сгенерировать на основе этого референса 👇",
                    parse_mode="HTML",
                )
            else:
                await status.edit_text("❌ Не удалось извлечь кадр. Проверь ссылку.")
        except Exception as e:
            await status.edit_text(f"❌ Ошибка: {e}")
        finally:
            user_locks[uid] = False
        return

    # Regular image generation
    if not check_rate(uid):
        await message.answer(f"⚠️ Дневной лимит ({MAX_PER_DAY}). Попробуй завтра.")
        return

    if user_locks[uid]:
        await message.answer("⏳ Подожди, предыдущая картинка генерируется...")
        return

    user_locks[uid] = True
    style = user_styles.get(uid, "")
    status = await message.answer("🎨 Генерирую...")

    try:
        image_data = await generate_image(prompt, style)

        if image_data:
            # Add watermark
            image_data = add_watermark_to_image(image_data)

            # Save to history
            user_daily_count[uid] += 1
            user_history[uid].append({
                "prompt": prompt[:100],
                "style": style or "-",
                "time": datetime.now(),
            })

            await status.delete()
            filename = f"image-{uuid.uuid4().hex[:8]}.png"
            caption = f"🎨 <b>{prompt}</b>"
            if style:
                caption += f"\nСтиль: {STYLES.get(style, style)}"

            await message.answer_photo(
                photo=BufferedInputFile(image_data, filename=filename),
                caption=caption,
                parse_mode="HTML",
            )
        else:
            await status.edit_text("❌ Не удалось сгенерировать. Попробуй другой промпт.")

    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        try:
            await status.edit_text(f"❌ Ошибка: {e}\n\nПопробуй ещё раз.")
        except Exception:
            await message.answer(f"❌ Ошибка: {e}")

    finally:
        user_locks[uid] = False


async def main():
    if not BOT_TOKEN:
        print("❌ Missing BOT_TOKEN")
        return
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    print("🎨 Image Bot v2 started!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
