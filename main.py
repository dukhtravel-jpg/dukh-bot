import logging
import os
from typing import Dict, Optional
import asyncio
import json
import re

# Telegram imports
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# Налаштування логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфігурація - отримуємо з environment variables
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
GOOGLE_CREDENTIALS_JSON = os.getenv('GOOGLE_CREDENTIALS_JSON')
GOOGLE_SHEET_URL = os.getenv('GOOGLE_SHEET_URL')

# Глобальні змінні
openai_client = None
user_states: Dict[int, str] = {}

# Тестові дані ресторанів як fallback
FALLBACK_RESTAURANTS = [
    {
        "name": "Пузата Хата",
        "address": "вул. Хрещатик, 15",
        "socials": "@puzatahata",
        "vibe": "Домашня атмосфера",
        "aim": "Для сім'ї",
        "cuisine": "Українська",
        "menu": "борщ, вареники, котлети",
        "menu_url": "",
        "photo": ""
    },
    {
        "name": "Pizza Celentano",
        "address": "вул. Саксаганського, 121",
        "socials": "@celentano_ua",
        "vibe": "Casual",
        "aim": "Для друзів",
        "cuisine": "Італійська",
        "menu": "піца, паста, салати",
        "menu_url": "",
        "photo": ""
    },
    {
        "name": "Канапа",
        "address": "вул. Городецького, 6",
        "socials": "@kanapa_kyiv",
        "vibe": "Інтимна атмосфера",
        "aim": "Для побачень",
        "cuisine": "Європейська",
        "menu": "стейк, риба, десерти",
        "menu_url": "",
        "photo": ""
    }
]

class RestaurantBot:
    def __init__(self):
        self.restaurants_data = []
        self.google_sheets_available = False
    
    def _convert_google_drive_url(self, url: str) -> str:
        """Перетворює Google Drive посилання в пряме посилання для зображення"""
        if not url or 'drive.google.com' not in url:
            return url
        
        match = re.search(r'/file/d/([a-zA-Z0-9-_]+)', url)
        if match:
            file_id = match.group(1)
            direct_url = f"https://drive.google.com/uc?export=view&id={file_id}"
            logger.info(f"Перетворено Google Drive посилання")
            return direct_url
        
        return url
        
    async def init_google_sheets(self):
        """Ініціалізація підключення до Google Sheets"""
        if not GOOGLE_CREDENTIALS_JSON or not GOOGLE_SHEET_URL:
            logger.warning("Google Sheets credentials не налаштовано, використовую тестові дані")
            self.restaurants_data = FALLBACK_RESTAURANTS
            return
            
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            
            scope = [
                "https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly"
            ]
            
            credentials_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
            creds = Credentials.from_service_account_info(credentials_dict, scopes=scope)
            
            gc = gspread.authorize(creds)
            google_sheet = gc.open_by_url(GOOGLE_SHEET_URL)
            worksheet = google_sheet.sheet1
            
            records = worksheet.get_all_records()
            
            if records:
                self.restaurants_data = records
                self.google_sheets_available = True
                logger.info(f"Завантажено {len(self.restaurants_data)} закладів з Google Sheets")
            else:
                logger.warning("Google Sheets порожній, використовую тестові дані")
                self.restaurants_data = FALLBACK_RESTAURANTS
                
        except Exception as e:
            logger.error(f"Детальна помилка Google Sheets: {type(e).__name__}: {str(e)}")
            logger.info("Використовую тестові дані ресторанів")
            self.restaurants_data = FALLBACK_RESTAURANTS
            
    async def get_recommendation(self, user_request: str) -> Optional[Dict]:
        """Отримання рекомендації"""
        try:
            if not self.restaurants_data:
                logger.error("Немає даних про ресторани")
                return None
            
            # Простий вибір без OpenAI для початку
            import random
            
            # Фільтруємо по ключових словах
            filtered_restaurants = self._filter_by_keywords(user_request, self.restaurants_data)
            
            # Вибираємо випадковий ресторан
            chosen_restaurant = random.choice(filtered_restaurants)
            
            photo_url = chosen_restaurant.get('photo', '')
            if photo_url:
                photo_url = self._convert_google_drive_url(photo_url)
            
            return {
                "name": chosen_restaurant.get('name', 'Ресторан'),
                "address": chosen_restaurant.get('address', 'Адреса не вказана'),
                "socials": chosen_restaurant.get('socials', 'Соц-мережі не вказані'),
                "vibe": chosen_restaurant.get('vibe', 'Приємна атмосфера'),
                "aim": chosen_restaurant.get('aim', 'Для будь-яких подій'),
                "cuisine": chosen_restaurant.get('cuisine', 'Смачна кухня'),
                "menu": chosen_restaurant.get('menu', ''),
                "menu_url": chosen_restaurant.get('menu_url', ''),
                "photo": photo_url
            }
            
        except Exception as e:
            logger.error(f"Помилка отримання рекомендації: {e}")
            return self._get_fallback_restaurant()

    def _filter_by_keywords(self, user_request: str, restaurant_list):
        """Простий фільтр по ключових словах"""
        user_lower = user_request.lower()
        
        # Ключові слова для фільтрування
        if any(word in user_lower for word in ['піц', 'pizza']):
            filtered = [r for r in restaurant_list if 'піц' in r.get('menu', '').lower() or 'pizza' in r.get('name', '').lower()]
            return filtered if filtered else restaurant_list
            
        if any(word in user_lower for word in ['сім', 'діт', 'родин']):
            filtered = [r for r in restaurant_list if 'сім' in r.get('aim', '').lower()]
            return filtered if filtered else restaurant_list
            
        if any(word in user_lower for word in ['романт', 'побач', 'двох']):
            filtered = [r for r in restaurant_list if 'побач' in r.get('aim', '').lower() or 'інтим' in r.get('vibe', '').lower()]
            return filtered if filtered else restaurant_list
            
        return restaurant_list

    def _get_fallback_restaurant(self):
        """Резервний ресторан"""
        return {
            "name": "Локальне кафе",
            "address": "Ваше місто",
            "socials": "Не вказано",
            "vibe": "Приємна атмосфера",
            "aim": "Для будь-яких подій",
            "cuisine": "Різноманітна кухня",
            "menu": "",
            "menu_url": "",
            "photo": ""
        }

restaurant_bot = RestaurantBot()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник команди /start"""
    user_id = update.effective_user.id
    user_states[user_id] = "waiting_request"
    
    message = (
        "Привіт! Я допоможу тобі знайти ідеальний ресторан!\n\n"
        "Розкажи мені про своє побажання. Наприклад:\n"
        "• 'Хочу місце для обіду з сім'єю'\n"
        "• 'Потрібен ресторан для побачення'\n"
        "• 'Шукаю піцу з друзями'\n\n"
        "Напиши, що ти шукаєш!"
    )
    
    await update.message.reply_text(message)
    logger.info(f"Користувач {user_id} почав діалог")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник текстових повідомлень"""
    user_id = update.effective_user.id
    
    if user_id not in user_states:
        await update.message.reply_text("Напишіть /start, щоб почати")
        return
    
    user_request = update.message.text
    logger.info(f"Користувач {user_id} написав: {user_request}")
    
    processing_message = await update.message.reply_text("Шукаю ідеальний ресторан для вас...")
    
    recommendation = await restaurant_bot.get_recommendation(user_request)
    
    try:
        await processing_message.delete()
    except:
        pass
    
    if recommendation:
        response_text = f"""<b>{recommendation['name']}</b>

📍 <b>Адреса:</b> {recommendation['address']}

📱 <b>Соц-мережі:</b> {recommendation['socials']}

✨ <b>Атмосфера:</b> {recommendation['vibe']}"""

        menu_url = recommendation.get('menu_url', '')
        if menu_url and menu_url.startswith('http'):
            response_text += f"\n\n📋 <a href='{menu_url}'>Переглянути меню</a>"

        photo_url = recommendation.get('photo', '')
        
        if photo_url and photo_url.startswith('http'):
            try:
                await update.message.reply_photo(
                    photo=photo_url,
                    caption=response_text,
                    parse_mode='HTML'
                )
            except Exception:
                await update.message.reply_text(response_text, parse_mode='HTML')
        else:
            await update.message.reply_text(response_text, parse_mode='HTML')
    else:
        await update.message.reply_text("Вибачте, сталася помилка. Спробуйте ще раз.")
    
    del user_states[user_id]
    await update.message.reply_text("Напишіть /start, щоб почати знову")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Обробник помилок"""
    logger.error(f"Помилка: {context.error}")

def main():
    """Основна функція запуску бота"""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN не встановлений!")
        return
    
    logger.info("Запускаю бота...")
    
    try:
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        logger.info("Telegram додаток створено успішно!")
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)
        
        logger.info("Підключаюся до Google Sheets...")
        
        # Ініціалізуємо Google Sheets синхронно
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(restaurant_bot.init_google_sheets())
        
        logger.info("Всі сервіси підключено! Бот готовий до роботи!")
        
        # Запускаємо polling
        loop.run_until_complete(application.run_polling(drop_pending_updates=True))
        
    except KeyboardInterrupt:
        logger.info("Бот зупинено користувачем")
    except Exception as e:
        logger.error(f"Критична помилка: {e}")
    finally:
        try:
            loop.close()
        except:
            pass

if __name__ == '__main__':
    main()
