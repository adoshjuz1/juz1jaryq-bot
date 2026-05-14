import asyncio
import logging
import os
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import aiohttp
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from faster_whisper import WhisperModel
from dotenv import load_dotenv

# ====================== НАСТРОЙКИ ======================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID")
OZON_API_KEY = os.getenv("OZON_API_KEY")

EXCEL_INVENTORY = "inventory.xlsx"
EXCEL_SALES = "sales.xlsx"
DR_FILE = "drr_history.xlsx"

class SaleState(StatesGroup):
    waiting_items = State()
    waiting_source = State()

class UpdateStockState(StatesGroup):
    waiting = State()

class DRRState(StatesGroup):
    waiting_data = State()

logging.basicConfig(level=logging.INFO)
model = WhisperModel("tiny", device="cpu", compute_type="int8")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def init_files():
    if not Path(EXCEL_INVENTORY).exists():
        pd.DataFrame(columns=["Товар", "Остаток", "Цена", "Единица"]).to_excel(EXCEL_INVENTORY, index=False)
    if not Path(EXCEL_SALES).exists():
        pd.DataFrame(columns=["Дата", "Товар", "Количество", "Цена_за_шт", "Сумма", "Источник"]).to_excel(EXCEL_SALES, index=False)
    if not Path(DR_FILE).exists():
        pd.DataFrame(columns=["Дата", "Оборот", "Поиск", "Поиск+Рек", "ОЗ3", "Общий_Расход", "ДРР"]).to_excel(DR_FILE, index=False)

init_files()

# ====================== УТИЛИТЫ ======================
def parse_num(text: str) -> int:
    try:
        return int(float(str(text).strip().replace(" ", "").replace("\xa0", "").replace(",", ".")))
    except:
        return 0

def format_num(n):
    return f"{int(n):,}".replace(",", " ")

async def import_ozon_stocks():
    if not OZON_CLIENT_ID or not OZON_API_KEY: return -1
    headers = {"Client-Id": OZON_CLIENT_ID, "Api-Key": OZON_API_KEY, "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post("https://api-seller.ozon.ru/v3/product/list", 
                                    json={"filter": {"visibility": "ALL"}, "limit": 1000}, headers=headers) as resp:
                data = await resp.json()
                offer_ids = [item["offer_id"] for item in data.get("result", {}).get("items", [])]
            
            async with session.post("https://api-seller.ozon.ru/v3/product/info/stocks",
                                    json={"filter": {"offer_id": offer_ids, "visibility": "ALL"}, "limit": 1000}, headers=headers) as resp:
                stock_data = await resp.json()
                stocks = stock_data.get("result", {}).get("items", [])

            df = pd.read_excel(EXCEL_INVENTORY)
            for s_item in stocks:
                name = s_item.get("offer_id")
                total_stock = sum(wh.get("present", 0) for wh in s_item.get("stocks", []))
                if name in df["Товар"].values:
                    df.loc[df["Товар"] == name, "Остаток"] = total_stock
                else:
                    new_row = pd.DataFrame([{"Товар": name, "Остаток": total_stock, "Цена": 0, "Единица": "шт"}])
                    df = pd.concat([df, new_row], ignore_index=True)
            df.to_excel(EXCEL_INVENTORY, index=False)
            return len(stocks)
    except: return -1

# ====================== КЛАВИАТУРЫ ======================
def main_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Товары"), KeyboardButton(text="🎤 Аудио")],
            [KeyboardButton(text="📊 Отчёты"), KeyboardButton(text="📈 ДРР")],
            [KeyboardButton(text="🔄 Импорт из Ozon")],
        ], resize_keyboard=True
    )

def goods_inline():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Посмотреть остатки", callback_data="stock_view")],
        [InlineKeyboardButton(text="🔄 Изменить остаток", callback_data="stock_update")],
        [InlineKeyboardButton(text="➕ Добавить продажу", callback_data="add_sale")],
    ])

# ====================== ХЕНДЛЕРЫ ======================

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer(
        "👋 Добро пожаловать в Складской бот!\n\n"
        "Я помогаю вести учёт товаров, продаж и рекламы на Ozon.\n\n"
        "Выберите раздел внизу 👇",
        reply_markup=main_reply_keyboard(), parse_mode="HTML"
    )

@dp.message(F.text == "📦 Товары")
async def goods_section(message: types.Message):
    await message.answer("📦 Управление товарами\n\nВыберите действие:", reply_markup=goods_inline(), parse_mode="HTML")

@dp.message(F.text == "🎤 Аудио")
async def audio_help(message: types.Message):
    await message.answer("🎤 Отправьте голосовое сообщение, и я расшифрую его в текст.")

@dp.callback_query(F.data == "stock_view")
async def stock_view(callback: types.CallbackQuery):
    await callback.message.edit_text("⏳ Обновляю остатки из Ozon...")
    await import_ozon_stocks()
    df = pd.read_excel(EXCEL_INVENTORY)
    text = "📋 Актуальные остатки:\n\n"
    for _, row in df.iterrows():
        icon = "⚠️" if row['Остаток'] < 5 else "•"
        text += f"{icon} {row['Товар']}: {row['Остаток']} шт.\n"
    await callback.message.edit_text(text, reply_markup=goods_inline(), parse_mode="HTML")

@dp.callback_query(F.data == "add_sale")
async def sale_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите данные в формате (Товар:Кол-во:Цена):")
    await state.set_state(SaleState.waiting_items)

@dp.message(SaleState.waiting_items)
async def sale_items(message: types.Message, state: FSMContext):
    await state.update_data(items=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="FBO", callback_data="src_FBO"), 
         InlineKeyboardButton(text="FBS", callback_data="src_FBS")],
        [InlineKeyboardButton(text="Оффлайн", callback_data="src_Offline")]
    ])
    await message.answer("Выберите источник:", reply_markup=kb)
    await state.set_state(SaleState.waiting_source)

@dp.callback_query(SaleState.waiting_source, F.data.startswith("src_"))
async def sale_finish(callback: types.CallbackQuery, state: FSMContext):
    src = callback.data.split("_")[1]
    user_data = await state.get_data()
    lines = user_data.get('items', "").split("\n")
    
    try:
        s_df = pd.read_excel(EXCEL_SALES)
        i_df = pd.read_excel(EXCEL_INVENTORY)
        
        count = 0
        for line in lines:
            if ":" not in line: continue
            try:
                name, qty, pr = [x.strip() for x in line.split(":")]
                qty, pr = int(qty), int(pr)
                new_s = {"Дата": date.today().strftime("%d.%m.%Y"), "Товар": name, "Количество": qty, "Цена_за_шт": pr, "Сумма": qty*pr, "Источник": src}
                s_df = pd.concat([s_df, pd.DataFrame([new_s])], ignore_index=True)
                if name in i_df["Товар"].values:
                    i_df.loc[i_df["Товар"] == name, "Остаток"] -= qty
                count += 1
            except: continue
        
        s_df.to_excel(EXCEL_SALES, index=False)
        i_df.to_excel(EXCEL_INVENTORY, index=False)
        await callback.message.edit_text(f"✅ Успешно добавлено {count} поз. (Источник: {src})")
    except PermissionError:
        await callback.message.answer("❌ Закройте файлы Excel!")
    
    await state.clear()

@dp.message(F.text == "📊 Отчёты")
async def report(message: types.Message):
    today = date.today().strftime("%d.%m.%Y")
    try:
        # Продажи
        s_df = pd.read_excel(EXCEL_SALES)
        today_s = s_df[s_df["Дата"] == today]
        
        def get_stats(source):
            df_src = today_s[today_s["Источник"] == source]
            return int(df_src['Количество'].sum()), int(df_src['Сумма'].sum())

        fbo_q, fbo_s = get_stats("FBO")
        fbs_q, fbs_s = get_stats("FBS")
        off_q, off_s = get_stats("Offline")
        
        total_q = fbo_q + fbs_q + off_q
        total_s = fbo_s + fbs_s + off_s

        res = (
            f"📦 Отчёт от {today}\n\n"
            f"Juz1jaryq\n"
            f"FBO: {fbo_q} шт; Сумма: {format_num(fbo_s)} ₸\n"
            f"FBS: {fbs_q} шт; Сумма: {format_num(fbs_s)} ₸\n"
            f"Оффлайн: {off_q} шт; Сумма: {format_num(off_s)} ₸\n"
            f"Возвраты: 0 шт;\n"
            f"Отмены: 0 шт;\n"
            f"--------------------------\n"
            f"Итого: {total_q} шт 🚚 на сумму {format_num(total_s)} ₸ 💰\n"
        )

        # ДОБАВЛЯЕМ ДРР В КОНЕЦ
        if Path(DR_FILE).exists():
            dr_df = pd.read_excel(DR_FILE)
            today_dr = dr_df[dr_df["Дата"] == today]
            if not today_dr.empty:
                row = today_dr.iloc[0]
                res += (
                    f"--------------------------\n"
                    f"📊 Детали DRR:\n"
                    f"{format_num(row['Оборот'])} - {format_num(row['Поиск'])} - {format_num(row['Поиск+Рек'])} - {format_num(row['ОЗ3'])} = {row['ДРР']}%"
                )

        await message.answer(res, parse_mode="HTML")
    except PermissionError:
        await message.answer("❌ Ошибка доступа: Закройте файл Excel!")

@dp.message(F.text == "📈 ДРР")
async def ads_menu(message: types.Message):
    await message.answer(
        "📈 Реклама и DRR\n\nЗдесь вы можете вносить данные по рекламе и смотреть статистику.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Ввести DRR за сегодня", callback_data="drr_input")]
        ]), parse_mode="HTML"
    )

@dp.callback_query(F.data == "drr_input")
async def drr_input_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите данные (Оборот:Поиск:Поиск+Рек:ОЗ3):")
    await state.set_state(DRRState.waiting_data)

@dp.message(DRRState.waiting_data)
async def drr_input_finish(message: types.Message, state: FSMContext):
    try:
        parts = message.text.split(":")
        oborot, poisk, poisk_rek, oz3 = [parse_num(p) for p in parts]
        obshiy = poisk + poisk_rek + oz3
        drr = round((obshiy / oborot * 100), 2) if oborot > 0 else 0
        
        df = pd.read_excel(DR_FILE)
        today = date.today().strftime("%d.%m.%Y")
        row = {"Дата": today, "Оборот": oborot, "Поиск": poisk, "Поиск+Рек": poisk_rek, "ОЗ3": oz3, "Общий_Расход": obshiy, "ДРР": drr}
        
        if today in df["Дата"].values:
            df.loc[df["Дата"] == today, list(row.keys())] = list(row.values())
        else:
            df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        df.to_excel(DR_FILE, index=False)

        await message.answer(f"✅ Данные DRR сохранены!\nДРР за сегодня: {drr}%", parse_mode="HTML")
        await state.clear()
    except: await message.answer("❌ Ошибка в формате!")

@dp.message(F.voice)
async def voice_handler(message: types.Message):
    msg = await message.answer("⏳ Расшифровываю голос...")
    file = await bot.get_file(message.voice.file_id)
    await bot.download_file(file.file_path, "v.ogg")
    segments, _ = model.transcribe("v.ogg")
    text = "".join([s.text for s in segments])
    await msg.edit_text(f"🎤 Текст:\n{text}", parse_mode="HTML")

@dp.message(F.text == "🔄 Импорт из Ozon")
async def manual_imp(message: types.Message):
    await message.answer("⏳ Синхронизация...")
    res = await import_ozon_stocks()
    await message.answer(f"✅ Готово. Обновлено: {res}" if res != -1 else "❌ Ошибка API")

async def scheduler():
    while True:
        now = datetime.now()
        if now.hour == 15 and now.minute == 0 and ADMIN_ID:
            try:
                df = pd.read_excel(EXCEL_INVENTORY)
                low = df[df["Остаток"] < 5]
                if not low.empty:
                    msg = "⚠️ Низкий остаток:\n" + "\n".join([f"• {r['Товар']}: {int(r['Остаток'])} шт." for _, r in low.iterrows()])
                    await bot.send_message(ADMIN_ID, msg, parse_mode="HTML")
            except: pass
            await asyncio.sleep(61)
        if now.hour == 23 and now.minute == 0 and ADMIN_ID:
            await bot.send_message(ADMIN_ID, f"🔔 Пора внести DRR!", parse_mode="HTML")
            await asyncio.sleep(61)
        await asyncio.sleep(30)

async def main():
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())