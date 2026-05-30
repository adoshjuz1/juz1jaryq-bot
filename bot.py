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
from dotenv import load_dotenv
from aiohttp import web
import gspread
from google.oauth2.service_account import Credentials

# ====================== НАСТРОЙКИ ======================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# Ссылка на твою Google Таблицу
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/1srUrF8mUo_F_an-zDBVPnH_gC5qv2kcV0X9AXAO1hBQ/edit"
CREDS_FILE = "creds.json"

class SaleState(StatesGroup):
    waiting_items = State()
    waiting_source = State()

class UpdateStockState(StatesGroup):
    waiting_data = State()

class DRRState(StatesGroup):
    waiting_data = State()

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Подключение к Google Таблицам
def get_sheets_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
    return gspread.authorize(creds)

# Инициализация базовых строк, если листы вдруг пустые
def init_google_sheets():
    try:
        client = get_sheets_client()
        sheet = client.open_by_url(SPREADSHEET_URL)
        
        # Проверяем и заполняем лист "Остатки"
        wks_inventory = sheet.worksheet("Остатки")
        records = wks_inventory.get_all_records()
        if not records:
            initial_data = [
                ["Lightning-Type C adapter", 36, 1600, "шт"],
                ["Type C - Lightning adapter", 24, 1600, "шт"],
                ["USB Hub 8 in 1", 0, 3500, "шт"]
            ]
            wks_inventory.append_rows(initial_data, value_input_option="USER_ENTERED")
            logging.info("Начальные товары успешно загружены в Google Таблицу.")
    except Exception as e:
        logging.error(f"Ошибка инициализации Google Таблиц: {e}")

init_google_sheets()

# ====================== МИКРО-ВЕБ-СЕРВЕР ДЛЯ RENDER ======================
async def handle(request):
    return web.Response(text="Bot is alive and running online with Google Sheets!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logging.info(f"Фоновый веб-сервер успешно запущен на порту {port}")

# ====================== УТИЛИТЫ ======================
def parse_num(text: str) -> int:
    try:
        return int(float(str(text).strip().replace(" ", "").replace("\xa0", "").replace(",", ".")))
    except:
        return 0

def format_num(n):
    return f"{int(n):,}".replace(",", " ")

# ====================== КЛАВИАТУРЫ ======================
def main_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Товары")],
            [KeyboardButton(text="📊 Отчёты"), KeyboardButton(text="📈 ДРР")],
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
        "👋 Добро пожаловать в Складской ИИ-бот!\n\n"
        "Все данные автоматически синхронизируются с твоей Google Таблицей в реальном времени.\n\n"
        "Выберите раздел 👇",
        reply_markup=main_reply_keyboard(), parse_mode="HTML"
    )

@dp.message(F.text == "📦 Товары")
async def goods_section(message: types.Message):
    await message.answer("📦 Управление товарами\n\nВыберите действие:", reply_markup=goods_inline(), parse_mode="HTML")

@dp.callback_query(F.data == "stock_view")
async def stock_view(callback: types.CallbackQuery):
    await callback.message.edit_text("⏳ Читаю данные из Google Таблицы...")
    try:
        client = get_sheets_client()
        sheet = client.open_by_url(SPREADSHEET_URL).worksheet("Остатки")
        records = sheet.get_all_records()
        
        if not records:
            text = "📋 Список товаров в таблице пуст."
        else:
            text = "📋 <b>Актуальные остатки (Google Sheets):</b>\n\n"
            for row in records:
                stock = parse_num(row.get('Остаток', 0))
                icon = "⚠️" if stock < 5 else "•"
                text += f"{icon} <b>{row.get('Товар')}</b>: {stock} шт. (Цена: {format_num(row.get('Цена', 0))} ₸)\n"
                
        await callback.message.edit_text(text, reply_markup=goods_inline(), parse_mode="HTML")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка чтения Google Таблицы: {e}", reply_markup=goods_inline())

@dp.callback_query(F.data == "stock_update")
async def stock_update_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer(
        "📝 <b>Изменение остатков / Добавление товара</b>\n\n"
        "Введите данные в формате <code>Товар:Остаток:Цена</code>\n"
        "Каждый товар с новой строки. Если товара нет в таблице, он создастся автоматически.\n\n"
        "<i>Пример:</i>\n"
        "<code>USB Hub 8 in 1:50:3500</code>", 
        parse_mode="HTML"
    )
    await state.set_state(UpdateStockState.waiting_data)

@dp.message(UpdateStockState.waiting_data)
async def stock_update_finish(message: types.Message, state: FSMContext):
    lines = message.text.split("\n")
    await message.answer("⏳ Сохраняю изменения в Google Таблицу...")
    try:
        client = get_sheets_client()
        sheet = client.open_by_url(SPREADSHEET_URL).worksheet("Остатки")
        records = sheet.get_all_records()
        
        # Превращаем в словарь для быстрого поиска строк в gspread
        all_products = {row['Товар']: idx + 2 for idx, row in enumerate(records)} # +2 учитывает шапку и индексацию с 1
        updated_count = 0
        
        for line in lines:
            if ":" not in line: continue
            parts = line.split(":")
            name = parts[0].strip()
            stock = parse_num(parts[1])
            price = parse_num(parts[2]) if len(parts) > 2 else 0
            
            if name in all_products:
                row_num = all_products[name]
                sheet.update_cell(row_num, 2, stock) # Столбец B - Остаток
                if len(parts) > 2:
                    sheet.update_cell(row_num, 3, price) # Столбец C - Цена
            else:
                sheet.append_row([name, stock, price, "шт"], value_input_option="USER_ENTERED")
            updated_count += 1
            
        await message.answer(f"✅ Успешно обновлено в таблице товаров: {updated_count} шт.", reply_markup=main_reply_keyboard())
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка записи: {e}")

@dp.callback_query(F.data == "add_sale")
async def sale_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите данные о продаже (Товар:Кол-во:Цена):")
    await state.set_state(SaleState.waiting_items)

@dp.message(SaleState.waiting_items)
async def sale_items(message: types.Message, state: FSMContext):
    await state.update_data(items=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="FBO", callback_data="src_FBO"), 
         InlineKeyboardButton(text="FBS", callback_data="src_FBS")],
        [InlineKeyboardButton(text="Оффлайн", callback_data="src_Offline")]
    ])
    await message.answer("Выберите источник продажи:", reply_markup=kb)
    await state.set_state(SaleState.waiting_source)

@dp.callback_query(SaleState.waiting_source, F.data.startswith("src_"))
async def sale_finish(callback: types.CallbackQuery, state: FSMContext):
    src = callback.data.split("_")[1]
    user_data = await state.get_data()
    lines = user_data.get('items', "").split("\n")
    
    await callback.message.edit_text("⏳ Провожу продажи через Google Таблицу...")
    try:
        client = get_sheets_client()
        sheet_all = client.open_by_url(SPREADSHEET_URL)
        
        wks_sales = sheet_all.worksheet("Продажи")
        wks_inventory = sheet_all.worksheet("Остатки")
        
        inv_records = wks_inventory.get_all_records()
        all_products = {row['Товар']: idx + 2 for idx, row in enumerate(inv_records)}
        all_stocks = {row['Товар']: parse_num(row['Остаток']) for row in inv_records}
        
        count = 0
        sales_rows = []
        today_str = date.today().strftime("%d.%m.%Y")
        
        for line in lines:
            if ":" not in line: continue
            try:
                name, qty, pr = [x.strip() for x in line.split(":")]
                qty, pr = int(qty), int(pr)
                
                sales_rows.append([today_str, name, qty, pr, qty * pr, src])
                
                # Вычитаем из остатка онлайн
                if name in all_products:
                    row_num = all_products[name]
                    new_stock = all_stocks[name] - qty
                    wks_inventory.update_cell(row_num, 2, new_stock)
                count += 1
            except: continue
            
        if sales_rows:
            wks_sales.append_rows(sales_rows, value_input_option="USER_ENTERED")
            
        await callback.message.edit_text(f"✅ В таблицу успешно добавлено {count} продаж. Источник: {src}")
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка при проведении продажи: {e}")
        
    await state.clear()

@dp.message(F.text == "📊 Отчёты")
async def report(message: types.Message):
    today = date.today().strftime("%d.%m.%Y")
    await message.answer("⏳ Собираю отчет за сегодня...")
    try:
        client = get_sheets_client()
        sheet_all = client.open_by_url(SPREADSHEET_URL)
        
        sales_data = sheet_all.worksheet("Продажи").get_all_records()
        
        # Фильтруем продажи за сегодня
        fbo_q, fbo_s = 0, 0
        fbs_q, fbs_s = 0, 0
        off_q, off_s = 0, 0
        
        for row in sales_data:
            if str(row.get("Дата")) == today:
                qty = parse_num(row.get("Количество"))
                summ = parse_num(row.get("Сумма"))
                source = str(row.get("Источник")).strip()
                
                if source == "FBO": fbo_q += qty; fbo_s += summ
                elif source == "FBS": fbs_q += qty; fbs_s += summ
                elif source == "Offline": off_q += qty; off_s += summ

        total_q = fbo_q + fbs_q + off_q
        total_s = fbo_s + fbs_s + off_s

        res = (
            f"📦 <b>Отчёт от {today} (Google Sheets)</b>\n\n"
            f"Juz1jaryq\n"
            f"FBO: {fbo_q} шт; Сумма: {format_num(fbo_s)} ₸\n"
            f"FBS: {fbs_q} шт; Сумма: {format_num(fbs_s)} ₸\n"
            f"Оффлайн: {off_q} шт; Сумма: {format_num(off_s)} ₸\n"
            f"Возвраты: 0 шт;\n"
            f"Отмены: 0 шт;\n"
            f"--------------------------\n"
            f"Итого: {total_q} шт 🚚 на сумму {format_num(total_s)} ₸ 💰\n"
        )

        drr_data = sheet_all.worksheet("ДРР").get_all_records()
        for row in drr_data:
            if str(row.get("Дата")) == today:
                res += (
                    f"--------------------------\n"
                    f"📊 Детали DRR:\n"
                    f"{format_num(row.get('Оборот'))} - {format_num(row.get('Поиск'))} - {format_num(row.get('Поиск+Рек'))} - {format_num(row.get('ОЗ3'))} = {row.get('ДРР')}%"
                )
                break

        await message.answer(res, parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка генерации отчета: {e}")

@dp.message(F.text == "📈 ДРР")
async def ads_menu(message: types.Message):
    await message.answer(
        "📈 Реклама и DRR\n\nДанные сохраняются сразу во вкладку 'ДРР' вашей онлайн-таблицы.",
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
        
        await message.answer("⏳ Записываю DRR в таблицу...")
        client = get_sheets_client()
        wks_drr = client.open_by_url(SPREADSHEET_URL).worksheet("ДРР")
        
        today = date.today().strftime("%d.%m.%Y")
        drr_records = wks_drr.get_all_records()
        
        # Ищем, нет ли уже записи за сегодня, чтобы обновить
        row_idx = None
        for idx, row in enumerate(drr_records):
            if str(row.get("Дата")) == today:
                row_idx = idx + 2
                break
                
        new_row = [today, oborot, poisk, poisk_rek, oz3, obshiy, drr]
        
        if row_idx:
            wks_drr.update(f"A{row_idx}:G{row_idx}", [new_row], value_input_option="USER_ENTERED")
        else:
            wks_drr.append_row(new_row, value_input_option="USER_ENTERED")

        await message.answer(f"✅ Данные DRR успешно сохранены онлайн!\nДРР за сегодня: {drr}%", parse_mode="HTML")
        await state.clear()
    except Exception as e: 
        await message.answer(f"❌ Ошибка сохранения DRR: {e}")

async def scheduler():
    while True:
        now = datetime.now()
        # В 15:00 проверка низких остатков онлайн
        if now.hour == 15 and now.minute == 0 and ADMIN_ID:
            try:
                client = get_sheets_client()
                records = client.open_by_url(SPREADSHEET_URL).worksheet("Остатки").get_all_records()
                low_items = [r for r in records if parse_num(r.get('Остаток', 0)) < 5]
                if low_items:
                    msg = "⚠️ <b>Низкий остаток в Google Таблице:</b>\n" + "\n".join([f"• {r['Товар']}: {r['Остаток']} шт." for r in low_items])
                    await bot.send_message(ADMIN_ID, msg, parse_mode="HTML")
            except: pass
            await asyncio.sleep(61)
        # В 23:00 напоминалка о ДРР
        if now.hour == 23 and now.minute == 0 and ADMIN_ID:
            await bot.send_message(ADMIN_ID, f"🔔 Пора внести DRR через меню бота!", parse_mode="HTML")
            await asyncio.sleep(61)
        await asyncio.sleep(30)

async def main():
    asyncio.create_task(start_web_server())
    asyncio.create_task(scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())