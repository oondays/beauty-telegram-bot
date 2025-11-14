# main.py
import json
import os
from google.oauth2 import service_account
from googleapiclient.discovery import build
import pytz
from datetime import datetime, timedelta
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

SCOPES = ['https://www.googleapis.com/auth/calendar']

def get_calendar_service():
    # Читаем ключ из переменной окружения
    service_account_json_str = os.getenv('GOOGLE_SERVICE_ACCOUNT')
    if not service_account_json_str:
        raise ValueError("Переменная окружения GOOGLE_SERVICE_ACCOUNT не установлена.")
    try:
        service_account_json = json.loads(service_account_json_str)
    except json.JSONDecodeError:
        raise ValueError("Переменная окружения GOOGLE_SERVICE_ACCOUNT содержит некорректный JSON.")

    creds = service_account.Credentials.from_service_account_info(
        service_account_json,
        scopes=SCOPES
    )
    return build('calendar', 'v3', credentials=creds)

# --- Глобальный список слотов ---
SLOTS = []

# --- Изменённая структура для хранения записей ---
# Теперь USER_BOOKINGS[user_id] = [{'slot': '...', 'event_id': '...'}, {...}, ...]
USER_BOOKINGS = {}

# --- Функция генерации слотов (пример) ---
def generate_slots():
    slots = []
    tz = pytz.timezone('Asia/Omsk') # Убедитесь, что часовой пояс правильный
    now = datetime.now(tz)
    for day_offset in range(5):  # 5 дней вперёд
        date = (now + timedelta(days=day_offset)).date()
        for hour in range(10, 18):  # 10:00 - 18:00
            time_str = f"{date} {hour:02d}:00"
            dt = datetime.strptime(time_str, '%Y-%m-%d %H:%M')
            localized_dt = tz.localize(dt)
            if localized_dt > now:
                formatted_time = localized_dt.strftime('%d.%m.%Y %H:%M')
                slots.append(formatted_time)
    return slots

SLOTS = generate_slots()

# --- Функция главного меню ---
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("Записаться на процедуру", callback_data='booking')],
        [InlineKeyboardButton("Посмотреть мою запись", callback_data='mybooking')],
        [InlineKeyboardButton("Отменить запись", callback_data='mybooking')], # Теперь ведёт в то же меню
        [InlineKeyboardButton("Информация о мастере", callback_data='info')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    query = update.callback_query
    if query:
        await query.edit_message_text('Привет! Выберите действие:', reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text('Привет! Выберите действие:', reply_markup=reply_markup)

# --- Функции для кнопок (CallbackQuery) ---
async def booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not SLOTS:
        await query.edit_message_text(text="Нет доступных слотов.")
        return
    keyboard = [
        [InlineKeyboardButton(slot, callback_data=f"select_{slot}")] for slot in SLOTS
    ]
    keyboard.append([InlineKeyboardButton("Обновить список", callback_data='refresh')])
    keyboard.append([InlineKeyboardButton("Назад", callback_data='start')])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text="Выберите слот:", reply_markup=reply_markup)

async def select_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slot = query.data.replace('select_', '')
    user = update.effective_user
    context.user_data['selected_slot'] = slot
    keyboard = [
        [InlineKeyboardButton("✅ Да, записаться", callback_data='confirm_booking')],
        [InlineKeyboardButton("❌ Нет, выбрать другое время", callback_data='booking')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=f"Вы уверены, что хотите записаться на {slot}?", reply_markup=reply_markup)

async def confirm_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slot = context.user_data.get('selected_slot')
    user = update.effective_user
    # Записываем в календарь
    service = get_calendar_service()
    start_time = datetime.strptime(slot, '%d.%m.%Y %H:%M')
    end_time = start_time + timedelta(minutes=60)
    timezone = pytz.timezone('Asia/Omsk')
    start_iso = timezone.localize(start_time).isoformat()
    end_iso = timezone.localize(end_time).isoformat()
    event = {
        'summary': f'Запись к мастеру: {user.first_name}',
        'start': {'dateTime': start_iso, 'timeZone': 'Asia/Omsk'},
        'end': {'dateTime': end_iso, 'timeZone': 'Asia/Omsk'},
    }
    created_event = service.events().insert(
        calendarId='26b49de33120ca2fe5852f246a5d89541bcebed5b90928856fbd5cb0d084f5eb@group.calendar.google.com', # ЗАМЕНИТЕ НА СВОЙ ID КАЛЕНДАРЯ
        body=event
    ).execute()
    event_id = created_event.get('id')
    # Убираем слот из списка
    global SLOTS
    if slot in SLOTS:
        SLOTS.remove(slot)
    else:
        print(f"Предупреждение: Слот {slot} не найден в SLOTS при добавлении записи для пользователя {user.id}.")
    # --- Изменение: Добавляем запись в список для пользователя ---
    if user.id not in USER_BOOKINGS:
        USER_BOOKINGS[user.id] = []
    USER_BOOKINGS[user.id].append({'slot': slot, 'event_id': event_id})

    # --- Отправка данных в n8n (не забудьте настроить переменную N8N_WEBHOOK_URL) ---
    import requests
    n8n_webhook_url = os.getenv('N8N_WEBHOOK_URL')
    if n8n_webhook_url:
        try:
            webhook_data = {
                "user": user.first_name,
                "slot": slot,
                "user_id": user.id,
                "event_id": event_id
            }
            response = requests.post(n8n_webhook_url, json=webhook_data)
            if response.status_code != 200:
                print(f"Ошибка при отправке в n8n: {response.status_code}, {response.text}")
        except Exception as e:
            print(f"Ошибка при отправке запроса в n8n: {e}")
    else:
        print("N8N_WEBHOOK_URL не установлена. Уведомление не отправлено.")

    await query.edit_message_text(text=f"✅ Вы записаны на {slot}! Спасибо.")

# --- Изменение: Функция для просмотра всех записей ---
async def mybooking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    user_bookings = USER_BOOKINGS.get(user.id, [])
    
    if not user_bookings:
        await query.edit_message_text(text="У вас нет активной записи.")
        return

    # Сортировка записей по времени (от ближайшего)
    sorted_bookings = sorted(user_bookings, key=lambda x: datetime.strptime(x['slot'], '%d.%m.%Y %H:%M'))

    keyboard = []
    for booking in sorted_bookings:
        slot = booking['slot']
        # Создаём кнопку для отмены конкретного слота
        keyboard.append([InlineKeyboardButton(f"❌ Отменить {slot}", callback_data=f"cancel_specific_{slot}")])
    
    keyboard.append([InlineKeyboardButton("Назад", callback_data='start')]) # Кнопка "Назад"
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text="Ваши записи:", reply_markup=reply_markup)

# --- Новая функция для подтверждения отмены конкретного слота ---
async def confirm_cancel_specific(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slot_to_cancel = query.data.replace('cancel_specific_', '')
    user = update.effective_user
    context.user_data['slot_to_cancel'] = slot_to_cancel # Сохраняем слот для отмены

    keyboard = [
        [InlineKeyboardButton("✅ Да, отменить", callback_data='execute_cancel')],
        [InlineKeyboardButton("❌ Нет, вернуться", callback_data='mybooking')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=f"Вы уверены, что хотите отменить запись на {slot_to_cancel}?", reply_markup=reply_markup)

# --- Новая функция для выполнения отмены ---
async def execute_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    slot_to_cancel = context.user_data.get('slot_to_cancel')
    user = update.effective_user

    if not slot_to_cancel:
        await query.edit_message_text(text="Произошла ошибка при отмене записи.")
        return

    user_bookings = USER_BOOKINGS.get(user.id, [])
    booking_to_cancel = None
    for booking in user_bookings:
        if booking['slot'] == slot_to_cancel:
            booking_to_cancel = booking
            break

    if not booking_to_cancel:
        await query.edit_message_text(text="Запись больше не найдена.")
        return

    event_id = booking_to_cancel['event_id']

    # Удаляем событие из календаря
    service = get_calendar_service()
    try:
        service.events().delete(
            calendarId='26b49de33120ca2fe5852f246a5d89541bcebed5b90928856fbd5cb0d084f5eb@group.calendar.google.com', # ЗАМЕНИТЕ НА СВОЙ ID КАЛЕНДАРЯ
            eventId=event_id
        ).execute()
    except Exception as e:
        print(f"Ошибка при удалении события: {e}")
        await query.edit_message_text(text=f"❌ Ошибка при отмене записи: {e}")
        return

    # Возвращаем слот в список
    global SLOTS
    SLOTS.append(slot_to_cancel)
    # --- Изменение: Удаляем конкретную запись из списка пользователя ---
    user_bookings.remove(booking_to_cancel)
    # Удаляем ключ пользователя, если список записей пуст
    if not user_bookings:
        del USER_BOOKINGS[user.id]

    await query.edit_message_text(text=f"❌ Запись на {slot_to_cancel} отменена и удалена из календаря.")

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = """
🌸 Мастер красоты
📍 Омск
📞 +7 (999) 999-99-99
🕒 Рабочие часы: 10:00 - 18:00
🎁 Акции и скидки — в группе
    """
    keyboard = [[InlineKeyboardButton("Назад", callback_data='start')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=text, reply_markup=reply_markup)

async def refresh_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    global SLOTS
    SLOTS = generate_slots()
    await booking(update, context)

# --- Функции для команд ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)

async def cmd_mybooking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    # --- Добавлено для отладки ---
    print(f"DEBUG: USER_BOOKINGS = {USER_BOOKINGS}")
    print(f"DEBUG: Текущий user_id = {user.id}")
    # ----------------------------
    booking_info = USER_BOOKINGS.get(user.id)
    if not booking_info:
        await update.effective_message.reply_text("У вас нет активной записи.")
        return
    slot = booking_info['slot']
    await update.effective_message.reply_text(f"Вы записаны на: {slot}")

# --- Изменение: Команда /mybooking теперь ведёт в то же меню ---
async def cmd_mybooking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context)
    # Или, если вы хотите сразу показать записи:
    # user = update.effective_user
    # user_bookings = USER_BOOKINGS.get(user.id, [])
    # if not user_bookings:
    #     await update.effective_message.reply_text("У вас нет активной записи.")
    #     return
    # sorted_bookings = sorted(user_bookings, key=lambda x: datetime.strptime(x['slot'], '%d.%m.%Y %H:%M'))
    # message = "Ваши записи:\n" + "\n".join([f"- {booking['slot']}" for booking in sorted_bookings])
    # await update.effective_message.reply_text(message)

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_main_menu(update, context) # Перенаправляем в меню просмотра записей

async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
🌸 Мастер красоты
📍 Омск
📞 +7 (999) 999-99-99
🕒 Рабочие часы: 10:00 - 18:00
🎁 Акции и скидки — в группе
    """
    await update.effective_message.reply_text(text)

def main():
    TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    if not TOKEN:
        raise ValueError("Переменная окружения TELEGRAM_BOT_TOKEN не установлена.")
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("booking", cmd_booking))
    application.add_handler(CommandHandler("mybooking", cmd_mybooking))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("info", cmd_info))
    application.add_handler(CallbackQueryHandler(show_main_menu, pattern='start'))
    application.add_handler(CallbackQueryHandler(booking, pattern='booking'))
    application.add_handler(CallbackQueryHandler(select_slot, pattern=r'^select_'))
    application.add_handler(CallbackQueryHandler(confirm_booking, pattern='confirm_booking'))
    application.add_handler(CallbackQueryHandler(mybooking, pattern='mybooking'))
    # --- Новые обработчики ---
    application.add_handler(CallbackQueryHandler(confirm_cancel_specific, pattern=r'^cancel_specific_'))
    application.add_handler(CallbackQueryHandler(execute_cancel, pattern='execute_cancel'))
    application.add_handler(CallbackQueryHandler(info, pattern='info'))
    application.add_handler(CallbackQueryHandler(refresh_slots, pattern='refresh'))
    application.run_polling()

if __name__ == '__main__':
    main()

