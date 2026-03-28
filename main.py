import logging
import asyncio
import os
import csv
import io
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler
)
from telegram.constants import ParseMode
import aiohttp
import aiosqlite

# Состояния для ConversationHandler
WAITING_FOR_USER_ID, WAITING_FOR_MESSAGE = range(2)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = [int(id) for id in os.environ.get('ADMIN_IDS', '').split(',') if id]
DATABASE_FILE = '/tmp/bot_database.db'  # Render использует /tmp для временных файлов

# Rate limiting
RATE_LIMIT = 3
RATE_LIMIT_PERIOD = 60
user_requests = defaultdict(list)

# Декоратор для проверки прав администратора
def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user = update.effective_user
        if user.id not in ADMIN_IDS:
            if update.message:
                await update.message.reply_text("⛔ Эта команда доступна только администраторам.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Доступ запрещен", show_alert=True)
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# Декоратор для rate limiting
def rate_limit(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        
        if user_id in ADMIN_IDS:
            return await func(update, context, *args, **kwargs)
        
        now = time.time()
        user_requests[user_id] = [t for t in user_requests[user_id] if now - t < RATE_LIMIT_PERIOD]
        
        if len(user_requests[user_id]) >= RATE_LIMIT:
            await update.message.reply_text("⏳ Слишком много запросов. Подождите немного.")
            return
        
        user_requests[user_id].append(now)
        return await func(update, context, *args, **kwargs)
    return wrapper

# Инициализация базы данных
async def init_database():
    """Создание всех необходимых таблиц"""
    async with aiosqlite.connect(DATABASE_FILE) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                phone TEXT,
                joined_date TIMESTAMP,
                last_activity TIMESTAMP,
                is_banned BOOLEAN DEFAULT 0,
                is_premium BOOLEAN DEFAULT 0,
                business_connected BOOLEAN DEFAULT 0
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS deleted_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                message_text TEXT,
                media_type TEXT,
                media_file_id TEXT,
                deleted_at TIMESTAMP,
                UNIQUE(user_id, chat_id, message_id)
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS edited_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                old_text TEXT,
                new_text TEXT,
                edited_at TIMESTAMP
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS self_destructing_media (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                chat_id INTEGER,
                media_type TEXT,
                file_id TEXT,
                caption TEXT,
                saved_at TIMESTAMP,
                expires_at TIMESTAMP
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                action TEXT,
                details TEXT,
                timestamp TIMESTAMP
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS admin_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER,
                user_id INTEGER,
                message_text TEXT,
                sent_at TIMESTAMP,
                status TEXT
            )
        ''')
        
        await db.commit()

class UserManager:
    @staticmethod
    async def add_user(user_id, username, first_name, last_name, phone=None):
        async with aiosqlite.connect(DATABASE_FILE) as db:
            now = datetime.now()
            await db.execute('''
                INSERT OR REPLACE INTO users 
                (user_id, username, first_name, last_name, phone, joined_date, last_activity)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, username, first_name, last_name, phone, now, now))
            await db.commit()
        
        await LogManager.add_log(user_id, 'start_command', 'Пользователь запустил бота')
    
    @staticmethod
    async def update_activity(user_id):
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute('UPDATE users SET last_activity = ? WHERE user_id = ?', 
                           (datetime.now(), user_id))
            await db.commit()
    
    @staticmethod
    async def is_banned(user_id):
        async with aiosqlite.connect(DATABASE_FILE) as db:
            async with db.execute('SELECT is_banned FROM users WHERE user_id = ?', (user_id,)) as cursor:
                result = await cursor.fetchone()
                return result and result[0] == 1
    
    @staticmethod
    async def ban_user(user_id, admin_id):
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute('UPDATE users SET is_banned = 1 WHERE user_id = ?', (user_id,))
            await db.commit()
        await LogManager.add_log(admin_id, 'ban_user', f'Забанен пользователь {user_id}')
    
    @staticmethod
    async def unban_user(user_id, admin_id):
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute('UPDATE users SET is_banned = 0 WHERE user_id = ?', (user_id,))
            await db.commit()
        await LogManager.add_log(admin_id, 'unban_user', f'Разбанен пользователь {user_id}')
    
    @staticmethod
    async def get_all_users():
        async with aiosqlite.connect(DATABASE_FILE) as db:
            async with db.execute('SELECT * FROM users ORDER BY joined_date DESC') as cursor:
                return await cursor.fetchall()
    
    @staticmethod
    async def get_user_by_id(user_id):
        async with aiosqlite.connect(DATABASE_FILE) as db:
            async with db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)) as cursor:
                return await cursor.fetchone()
    
    @staticmethod
    async def set_business_connected(user_id, status=True):
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute('UPDATE users SET business_connected = ? WHERE user_id = ?', 
                           (1 if status else 0, user_id))
            await db.commit()

class LogManager:
    @staticmethod
    async def add_log(user_id, action, details):
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute('INSERT INTO logs (user_id, action, details, timestamp) VALUES (?, ?, ?, ?)',
                           (user_id, action, details, datetime.now()))
            await db.commit()
    
    @staticmethod
    async def get_recent_logs(limit=100):
        async with aiosqlite.connect(DATABASE_FILE) as db:
            async with db.execute('SELECT * FROM logs ORDER BY timestamp DESC LIMIT ?', (limit,)) as cursor:
                return await cursor.fetchall()

class AdminMessageManager:
    @staticmethod
    async def save_message(admin_id, user_id, message_text, status='sent'):
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute('''
                INSERT INTO admin_messages (admin_id, user_id, message_text, sent_at, status)
                VALUES (?, ?, ?, ?, ?)
            ''', (admin_id, user_id, message_text, datetime.now(), status))
            await db.commit()
    
    @staticmethod
    async def get_user_messages(user_id, limit=50):
        async with aiosqlite.connect(DATABASE_FILE) as db:
            async with db.execute('''
                SELECT * FROM admin_messages 
                WHERE user_id = ? 
                ORDER BY sent_at DESC LIMIT ?
            ''', (user_id, limit)) as cursor:
                return await cursor.fetchall()

async def clean_old_records():
    cutoff_date = datetime.now() - timedelta(days=30)
    
    async with aiosqlite.connect(DATABASE_FILE) as db:
        await db.execute('DELETE FROM deleted_messages WHERE deleted_at < ?', (cutoff_date,))
        await db.execute('DELETE FROM edited_messages WHERE edited_at < ?', (cutoff_date,))
        await db.execute('DELETE FROM self_destructing_media WHERE saved_at < ?', (cutoff_date,))
        await db.execute('DELETE FROM logs WHERE timestamp < ?', (cutoff_date,))
        await db.commit()
    
    logger.info("Очистка старых записей выполнена")

# Обработчики команд
@rate_limit
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    await UserManager.add_user(
        user.id, 
        user.username, 
        user.first_name, 
        user.last_name
    )
    
    if user.id in ADMIN_IDS:
        keyboard = [
            [InlineKeyboardButton("👑 Админ-панель", callback_data="admin_panel")],
            [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton("📋 Логи", callback_data="admin_logs")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"👋 Здравствуйте, администратор {user.first_name}!\n\n"
            f"Я бот для отслеживания удаленных и измененных сообщений.\n"
            f"Ваши друзья с Telegram Premium могут подключить меня к своим аккаунтам "
            f"для сохранения:\n"
            f"• Удаленных сообщений\n"
            f"• Измененных сообщений\n"
            f"• Одноразовых фото/видео\n\n"
            f"Также я работаю в группах и каналах (нужны права администратора).\n\n"
            f"Используйте кнопки ниже для управления ботом.",
            reply_markup=reply_markup
        )
    else:
        keyboard = [
            [InlineKeyboardButton("🔗 Как подключить бота", callback_data="how_to_connect")],
            [InlineKeyboardButton("ℹ️ О боте", callback_data="about")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            f"👋 Привет, {user.first_name}!\n\n"
            f"Я бот для сохранения удаленных, измененных и одноразовых сообщений. "
            f"Чтобы я начал работать, вам нужно:\n\n"
            f"1️⃣ Иметь Telegram Premium (для личных чатов)\n"
            f"2️⃣ Для групп/каналов просто добавьте меня как администратора\n\n"
            f"Нажмите кнопку ниже для подробной инструкции.",
            reply_markup=reply_markup
        )
    
    if user.id not in ADMIN_IDS:
        await notify_admins_about_new_user(context, user)

async def notify_admins_about_new_user(context, user):
    message = (
        f"🆕 <b>Новый пользователь!</b>\n\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"👤 Имя: {user.first_name}\n"
        f"📝 Фамилия: {user.last_name if user.last_name else 'Не указана'}\n"
        f"🔗 Username: @{user.username if user.username else 'Нет'}\n"
        f"📅 Дата: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
    )
    
    try:
        photos = await user.get_profile_photos()
        if photos and photos.total_count > 0:
            for admin_id in ADMIN_IDS:
                await context.bot.send_photo(
                    chat_id=admin_id,
                    photo=photos.photos[0][-1].file_id,
                    caption=message,
                    parse_mode=ParseMode.HTML
                )
            return
    except:
        pass
    
    for admin_id in ADMIN_IDS:
        await context.bot.send_message(
            chat_id=admin_id,
            text=message,
            parse_mode=ParseMode.HTML
        )

@admin_only
async def export_users_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = await UserManager.get_all_users()
    
    if not users:
        await update.message.reply_text("📭 Нет пользователей для экспорта.")
        return
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    writer.writerow([
        'ID', 'Username', 'First Name', 'Last Name', 'Phone',
        'Joined Date', 'Last Activity', 'Is Banned', 'Is Premium', 
        'Business Connected'
    ])
    
    for user in users:
        writer.writerow([
            user[0], user[1], user[2], user[3], user[4],
            user[5], user[6], user[7], user[8], user[9]
        ])
    
    output.seek(0)
    filename = f"users_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    await update.message.reply_document(
        document=output.getvalue().encode('utf-8-sig'),
        filename=filename,
        caption=f"📊 Экспорт пользователей\nВсего: {len(users)}"
    )
    
    await LogManager.add_log(update.effective_user.id, 'export_users', f'Экспортировано {len(users)} пользователей')

# Обработчики событий (упрощенные для читаемости, но полные)
async def handle_business_connection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.business_connection:
        user_id = update.business_connection.user_id
        
        if await UserManager.is_banned(user_id):
            try:
                await context.bot.edit_business_connection(
                    business_connection_id=update.business_connection.id,
                    is_enabled=False
                )
            except:
                pass
            return
        
        await UserManager.set_business_connected(user_id, True)
        
        for admin_id in ADMIN_IDS:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"🔗 Пользователь {user_id} подключил Business API"
            )

async def handle_deleted_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post and update.channel_post.delete_date:
        msg = update.channel_post
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute('''
                INSERT INTO deleted_messages 
                (user_id, chat_id, message_id, message_text, deleted_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (msg.sender_chat.id if msg.sender_chat else 0, 
                  msg.chat_id, msg.message_id, msg.text or msg.caption, 
                  datetime.now()))
            await db.commit()
    
    elif update.message and update.message.delete_date:
        msg = update.message
        user_id = msg.from_user.id if msg.from_user else 0
        
        if await UserManager.is_banned(user_id):
            return
        
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute('''
                INSERT OR IGNORE INTO deleted_messages 
                (user_id, chat_id, message_id, message_text, deleted_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, msg.chat_id, msg.message_id, 
                  msg.text or msg.caption, datetime.now()))
            await db.commit()

async def handle_edited_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.edited_channel_post:
        msg = update.edited_channel_post
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute('''
                INSERT INTO edited_messages 
                (user_id, chat_id, message_id, old_text, new_text, edited_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (msg.sender_chat.id if msg.sender_chat else 0, 
                  msg.chat_id, msg.message_id, 
                  getattr(msg, 'old_text', ''), msg.text or msg.caption,
                  datetime.now()))
            await db.commit()
    
    elif update.edited_message:
        msg = update.edited_message
        user_id = msg.from_user.id if msg.from_user else 0
        
        if await UserManager.is_banned(user_id):
            return
        
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute('''
                INSERT INTO edited_messages 
                (user_id, chat_id, message_id, old_text, new_text, edited_at)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, msg.chat_id, msg.message_id, 
                  getattr(msg, 'old_text', ''), msg.text or msg.caption,
                  datetime.now()))
            await db.commit()

async def handle_self_destructing_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.channel_post
    if not msg:
        return
    
    is_self_destruct = False
    
    if msg.photo and hasattr(msg, 'has_media_spoiler'):
        is_self_destruct = True
        media_type = 'photo'
        file_id = msg.photo[-1].file_id
    elif msg.video and hasattr(msg, 'has_media_spoiler'):
        is_self_destruct = True
        media_type = 'video'
        file_id = msg.video.file_id
    elif msg.video_note:
        is_self_destruct = True
        media_type = 'video_note'
        file_id = msg.video_note.file_id
    elif msg.voice:
        is_self_destruct = True
        media_type = 'voice'
        file_id = msg.voice.file_id
    
    if not is_self_destruct:
        return
    
    user_id = msg.from_user.id if msg.from_user else (msg.sender_chat.id if msg.sender_chat else 0)
    
    if await UserManager.is_banned(user_id):
        return
    
    async with aiosqlite.connect(DATABASE_FILE) as db:
        await db.execute('''
            INSERT INTO self_destructing_media 
            (user_id, chat_id, media_type, file_id, caption, saved_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, msg.chat_id, media_type, file_id, msg.caption, datetime.now()))
        await db.commit()
    
    if user_id and user_id > 0:
        try:
            if media_type == 'photo':
                await context.bot.send_photo(chat_id=user_id, photo=file_id, caption=msg.caption)
            elif media_type == 'video':
                await context.bot.send_video(chat_id=user_id, video=file_id, caption=msg.caption)
            elif media_type == 'voice':
                await context.bot.send_voice(chat_id=user_id, voice=file_id, caption=msg.caption)
            elif media_type == 'video_note':
                await context.bot.send_video_note(chat_id=user_id, video_note=file_id)
        except Exception as e:
            logger.error(f"Ошибка отправки копии медиа: {e}")

# Админ-панель
async def show_admin_stats(query, context):
    async with aiosqlite.connect(DATABASE_FILE) as db:
        async with db.execute('SELECT COUNT(*) FROM users') as cursor:
            total_users = (await cursor.fetchone())[0]
        
        async with db.execute('SELECT COUNT(*) FROM users WHERE is_banned = 1') as cursor:
            banned_users = (await cursor.fetchone())[0]
        
        async with db.execute('SELECT COUNT(*) FROM users WHERE business_connected = 1') as cursor:
            connected_users = (await cursor.fetchone())[0]
        
        async with db.execute('SELECT COUNT(*) FROM deleted_messages') as cursor:
            deleted_msgs = (await cursor.fetchone())[0]
        
        async with db.execute('SELECT COUNT(*) FROM edited_messages') as cursor:
            edited_msgs = (await cursor.fetchone())[0]
        
        async with db.execute('SELECT COUNT(*) FROM self_destructing_media') as cursor:
            self_destruct_media = (await cursor.fetchone())[0]
        
        today = datetime.now().date()
        async with db.execute('SELECT COUNT(*) FROM users WHERE date(last_activity) = ?', (today,)) as cursor:
            active_today = (await cursor.fetchone())[0]
    
    stats_text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 <b>Пользователи:</b>\n"
        f"• Всего: {total_users}\n"
        f"• Забанено: {banned_users}\n"
        f"• Подключено к Business API: {connected_users}\n"
        f"• Активных сегодня: {active_today}\n\n"
        f"💬 <b>Сохраненные сообщения:</b>\n"
        f"• Удаленных: {deleted_msgs}\n"
        f"• Измененных: {edited_msgs}\n"
        f"• Одноразовых медиа: {self_destruct_media}"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        stats_text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )

async def show_users_list(query, context):
    users = await UserManager.get_all_users()
    
    if not users:
        await query.edit_message_text("Пользователей пока нет")
        return
    
    text = "👥 <b>Список пользователей:</b>\n\n"
    
    for user in users[:20]:
        status = "⛔" if user[7] else "✅"
        premium = "⭐" if user[8] else ""
        connected = "🔗" if user[9] else "❌"
        
        text += (
            f"{status} {premium} <b>{user[2]} {user[3]}</b>\n"
            f"🆔 ID: <code>{user[0]}</code>\n"
            f"👤 Username: @{user[1] if user[1] else 'нет'}\n"
            f"🔗 Подключен: {connected}\n"
            f"📅 Дата: {user[5][:19]}\n\n"
        )
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )

async def show_admin_logs(query, context):
    logs = await LogManager.get_recent_logs(20)
    
    if not logs:
        await query.edit_message_text("Логов пока нет")
        return
    
    text = "📋 <b>Последние логи:</b>\n\n"
    
    for log in logs:
        text += f"[{log[4][:19]}] User {log[1]}: {log[2]} - {log[3]}\n"
        if len(text) > 3500:
            text += "\n... и еще"
            break
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="admin_panel")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        text[:4000],
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )

async def show_connection_instructions(query, context):
    instructions = (
        "🔗 <b>Как подключить бота:</b>\n\n"
        "<b>Для личных чатов (нужен Premium):</b>\n"
        "1️⃣ Откройте <b>Настройки</b> Telegram\n"
        "2️⃣ Перейдите в раздел <b>Telegram Business</b>\n"
        "3️⃣ Выберите <b>Подключенные боты</b>\n"
        "4️⃣ Нажмите <b>Добавить бота</b>\n"
        "5️⃣ Введите <b>@имя_вашего_бота</b>\n\n"
        "<b>Для групп и каналов (Premium не нужен):</b>\n"
        "1️⃣ Добавьте бота в группу/канал\n"
        "2️⃣ Назначьте бота администратором\n"
        "3️⃣ Бот автоматически начнет отслеживать сообщения\n\n"
        "<b>Важно:</b>\n"
        "• Бот видит сообщения только после подключения\n"
        "• В группах бот не видит удаленные сообщения от других ботов\n"
        "• Отключить бота можно в любое время"
    )
    
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        instructions,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )

async def show_admin_panel(query, context):
    keyboard = [
        [InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 Список пользователей", callback_data="admin_users")],
        [InlineKeyboardButton("📋 Последние логи", callback_data="admin_logs")],
        [InlineKeyboardButton("📤 Экспорт пользователей", callback_data="export_users")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "👑 <b>Админ-панель</b>\n\n"
        "Выберите действие:",
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup
    )

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = update.effective_user
    
    if await UserManager.is_banned(user.id) and user.id not in ADMIN_IDS:
        await query.edit_message_text("⛔ Доступ заблокирован.")
        return
    
    if query.data == "admin_panel" and user.id in ADMIN_IDS:
        await show_admin_panel(query, context)
    
    elif query.data == "admin_stats" and user.id in ADMIN_IDS:
        await show_admin_stats(query, context)
    
    elif query.data == "admin_users" and user.id in ADMIN_IDS:
        await show_users_list(query, context)
    
    elif query.data == "admin_logs" and user.id in ADMIN_IDS:
        await show_admin_logs(query, context)
    
    elif query.data == "export_users" and user.id in ADMIN_IDS:
        await query.edit_message_text("⏳ Генерация CSV файла...")
        await export_users_csv(update, context)
        await show_admin_panel(query, context)
    
    elif query.data == "how_to_connect":
        await show_connection_instructions(query, context)
    
    elif query.data == "about":
        await query.edit_message_text(
            "ℹ️ <b>О боте</b>\n\n"
            "Этот бот создан для сохранения:\n"
            "• Удаленных сообщений\n"
            "• Измененных сообщений\n"
            "• Одноразовых фото и видео\n\n"
            "<b>Поддерживаемые чаты:</b>\n"
            "• Личные чаты (требуется Premium)\n"
            "• Группы (бота нужно добавить администратором)\n"
            "• Каналы (бота нужно добавить администратором)",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Назад", callback_data="back_to_main")
            ]])
        )
    
    elif query.data == "back_to_main":
        await start(update, context)

@admin_only
async def quick_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text(
            "Использование: /msg <user_id> <текст сообщения>\n"
            "Пример: /msg 123456789 Привет!"
        )
        return
    
    try:
        user_id = int(context.args[0])
        message_text = ' '.join(context.args[1:])
        
        user = await UserManager.get_user_by_id(user_id)
        if not user:
            await update.message.reply_text(f"❌ Пользователь {user_id} не найден в базе")
            return
        
        await context.bot.send_message(chat_id=user_id, text=message_text)
        
        await AdminMessageManager.save_message(update.effective_user.id, user_id, message_text, 'sent')
        
        await update.message.reply_text(f"✅ Сообщение отправлено пользователю {user_id}")
        await LogManager.add_log(update.effective_user.id, 'quick_send', f'Отправлено сообщение пользователю {user_id}')
        
    except ValueError:
        await update.message.reply_text("❌ Неверный формат ID пользователя")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")
    
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"❌ Ошибка бота:\n{context.error}"
            )
        except:
            pass

async def scheduled_cleanup(context: ContextTypes.DEFAULT_TYPE):
    await clean_old_records()

def main():
    if not TOKEN:
        logger.error("Токен бота не найден!")
        return
    
    if not ADMIN_IDS:
        logger.warning("ID администраторов не указаны!")
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("export_users", export_users_csv))
    application.add_handler(CommandHandler("msg", quick_send))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    application.add_handler(MessageHandler(
        filters.StatusUpdate.BUSINESS_CONNECTION, 
        handle_business_connection
    ))
    
    application.add_handler(MessageHandler(
        filters.StatusUpdate.DELETED_MESSAGES,
        handle_deleted_message
    ))
    
    application.add_handler(MessageHandler(
        filters.StatusUpdate.EDITED_MESSAGE,
        handle_edited_message
    ))
    
    application.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND,
        handle_self_destructing_media
    ))
    
    application.add_error_handler(error_handler)
    
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_daily(scheduled_cleanup, time=datetime.time(hour=3, minute=0))
    
    async def startup():
        await init_database()
        logger.info("База данных инициализирована")
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(startup())
    
    logger.info("Бот запущен...")
    application.run_polling()

if __name__ == '__main__':
    main()
