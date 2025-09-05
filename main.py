import logging
import os
from typing import Dict, Optional
import asyncio
import json
import re

import gspread
from google.oauth2.service_account import Credentials
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
            logger.info(f"Перетворено Google Drive посилання: {url} → {direct_url}")
            return direct_url
        
        logger.warning(f"Не вдалося витягнути ID з Google Drive посилання: {url}")
        return url
        
    async def init_google_sheets(self):
        """Ініціалізація підключення до Google Sheets"""
        if not GOOGLE_CREDENTIALS_JSON or not GOOGLE_SHEET_URL:
            logger.error("Google Sheets credentials не налаштовано")
            return
            
        try:
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
                logger.info(f"✅ Завантажено {len(self.restaurants_data)} закладів з Google Sheets")
            else:
                logger.warning("Google Sheets порожній")
                
        except Exception as e:
            logger.error(f"Детальна помилка Google Sheets: {type(e).__name__}: {str(e)}")
            
    async def get_recommendation(self, user_request: str) -> Optional[Dict]:
        """Отримання рекомендації через OpenAI з урахуванням меню"""
        try:
            # Ініціалізуємо OpenAI клієнт
            global openai_client
            if openai_client is None:
                import openai
                openai.api_key = OPENAI_API_KEY
                openai_client = openai
                logger.info("✅ OpenAI клієнт ініціалізовано")
            
            if not self.restaurants_data:
                logger.error("❌ Немає даних про ресторани")
                return None
            
            # Рандомізуємо порядок ресторанів для різноманітності
            import random
            shuffled_restaurants = self.restaurants_data.copy()
            random.shuffle(shuffled_restaurants)
            
            logger.info(f"🎲 Перемішав порядок ресторанів для різноманітності")
            
            # Фільтруємо по меню (якщо користувач шукає конкретну страву)
            filtered_restaurants = self._filter_by_menu(user_request, shuffled_restaurants)
            
            # Готуємо детальний промпт для OpenAI
            restaurants_details = []
            for i, r in enumerate(filtered_restaurants):
                detail = f"""Варіант {i+1}:
- Назва: {r.get('name', 'Без назви')}
- Кухня: {r.get('cuisine', 'Не вказана')}
- Атмосфера: {r.get('vibe', 'Не описана')}
- Підходить для: {r.get('aim', 'Не вказано')}"""
                restaurants_details.append(detail)
            
            restaurants_text = "\n\n".join(restaurants_details)
            
            # Додаємо випадкові приклади для різноманітності
            examples = [
                "Якщо запит про романтику → обирай інтимну атмосферу",
                "Якщо згадані діти/сім'я → обирай сімейні заклади", 
                "Якщо швидкий перекус → обирай casual формат",
                "Якщо особлива кухня → враховуй тип кухні",
                "Якщо святкування → обирай просторні заклади"
            ]
            random.shuffle(examples)
            selected_examples = examples[:2]
            
            prompt = f"""ЗАПИТ КОРИСТУВАЧА: "{user_request}"

ВАЖЛИВО: Всі заклади нижче УЖЕ ВІДФІЛЬТРОВАНІ і підходять під запит користувача.

ВАРІАНТИ ЗАКЛАДІВ:
{restaurants_text}

ІНСТРУКЦІЇ:
- Обери ТІЛЬКИ номер варіанту (число від 1 до {len(filtered_restaurants)})
- НЕ пояснюй свій вибір
- НЕ додавай коментарі про кухню чи атмосферу
- Просто поверни номер: наприклад "3"

Номер обраного варіанту:"""

            logger.info(f"🤖 Надсилаю запит до OpenAI з {len(filtered_restaurants)} варіантами...")
            logger.info(f"🔍 Перші 3 варіанти: {[r.get('name') for r in filtered_restaurants[:3]]}")
            
            def make_openai_request():
                return openai_client.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "Ти експерт-ресторатор. Обирай варіанти різноманітно, не зациклюй на одному закладі."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=200,
                    temperature=0.4,
                    top_p=0.9
                )
            
            # Виконуємо запит з timeout
            response = await asyncio.wait_for(
                asyncio.to_thread(make_openai_request),
                timeout=20
            )
            
            choice_text = response.choices[0].message.content.strip()
            logger.info(f"🤖 OpenAI повна відповідь: '{choice_text}'")
            
            # Покращений парсинг - шукаємо перше число в відповіді
            numbers = re.findall(r'\d+', choice_text)
            
            if numbers:
                choice_num = int(numbers[0]) - 1
                logger.info(f"🔍 Знайдено число в відповіді: {numbers[0]} → індекс {choice_num}")
                
                if 0 <= choice_num < len(filtered_restaurants):
                    chosen_restaurant = filtered_restaurants[choice_num]
                    logger.info(f"✅ OpenAI обрав: {chosen_restaurant.get('name', '')} (варіант {choice_num + 1} з {len(filtered_restaurants)})")
                else:
                    logger.warning(f"⚠️ Число {choice_num + 1} поза межами, використовую резервний алгоритм")
                    chosen_restaurant = self._smart_fallback_selection(user_request, filtered_restaurants)
            else:
                logger.warning("⚠️ Не знайдено чисел в відповіді, використовую резервний алгоритм")
                chosen_restaurant = self._smart_fallback_selection(user_request, filtered_restaurants)
            
            # Перетворюємо Google Drive посилання на фото
            photo_url = chosen_restaurant.get('photo', '')
            if photo_url:
                photo_url = self._convert_google_drive_url(photo_url)
            
            # Повертаємо результат
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
            
        except asyncio.TimeoutError:
            logger.error("⏰ Timeout при запиті до OpenAI, використовую резервний алгоритм")
            return self._fallback_selection_dict(user_request)
        except Exception as e:
            logger.error(f"❌ Помилка отримання рекомендації: {e}")
            return self._fallback_selection_dict(user_request)

    def _filter_by_menu(self, user_request: str, restaurant_list):
        """Фільтрує ресторани по меню (якщо користувач шукає конкретну страву)"""
        user_lower = user_request.lower()
        
        # Ключові слова для конкретних страв
        food_keywords = {
            'піца': [' піц', 'pizza', 'піца'],
            'паста': [' паст', 'спагеті', 'pasta'],
            'бургер': ['бургер', 'burger', 'гамбургер'],
            'суші': [' суші', 'sushi', ' рол', 'ролл', 'сашімі'],
            'салат': [' салат', 'salad'],
            'хумус': ['хумус', 'hummus'],
            'фалафель': ['фалафель', 'falafel'],
            'шаурма': ['шаурм', 'shawarma'],
            'стейк': ['стейк', 'steak', ' мясо'],
            'риба': [' риб', 'fish', 'лосось'],
            'курка': [' курк', 'курич', 'chicken'],
            'десерт': ['десерт', 'торт', 'тірамісу', 'морозиво']
        }
        
        # Перевіряємо чи користувач шукає конкретну страву
        requested_dishes = []
        for dish, keywords in food_keywords.items():
            if any(keyword in user_lower for keyword in keywords):
                requested_dishes.append(dish)
        
        if requested_dishes:
            # Фільтруємо ресторани де є потрібні страви
            filtered_restaurants = []
            logger.info(f"🍽 Користувач шукає конкретні страви: {requested_dishes}")
            
            for restaurant in restaurant_list:
                menu_text = restaurant.get('menu', '').lower()
                has_requested_dish = False
                
                for dish in requested_dishes:
                    dish_keywords = food_keywords[dish]
                    if any(keyword in menu_text for keyword in dish_keywords):
                        has_requested_dish = True
                        logger.info(f"   ✅ {restaurant.get('name', '')} має {dish}")
                        break
                
                if has_requested_dish:
                    filtered_restaurants.append(restaurant)
                else:
                    logger.info(f"   ❌ {restaurant.get('name', '')} немає потрібних страв")
            
            if filtered_restaurants:
                logger.info(f"📋 Відфільтровано до {len(filtered_restaurants)} закладів з потрібними стравами")
                return filtered_restaurants
            else:
                logger.warning("⚠️ Жоден заклад не має потрібних страв, показую всі")
                return restaurant_list
        else:
            # Якщо не шукає конкретну страву, повертаємо всі ресторани
            logger.info("🔍 Загальний запит, аналізую всі ресторани")
            return restaurant_list

    def _smart_fallback_selection(self, user_request: str, restaurant_list):
        """Резервний алгоритм з рандомізацією"""
        import random
        
        user_lower = user_request.lower()
        
        # Ключові слова для різних категорій
        keywords_map = {
            'romantic': (['романт', 'побачен', 'двох', 'інтимн', 'затишн'], ['інтимн', 'романт', 'для пар', 'затишн']),
            'family': (['сім', 'діт', 'родин', 'батьк'], ['сімейн', 'діт', 'родин']),
            'business': (['діл', 'зустріч', 'перегов', 'бізнес'], ['діл', 'зустріч', 'бізнес']),
            'friends': (['друз', 'компан', 'гуртом', 'весел'], ['компан', 'друз', 'молодіжн']),
            'quick': (['швидк', 'перекус', 'фаст', 'поспіша'], ['швидк', 'casual', 'фаст']),
            'celebration': (['святкув', 'день народж', 'ювіле', 'свято'], ['святков', 'простор', 'груп'])
        }
        
        # Підраховуємо очки
        scored_restaurants = []
        for restaurant in restaurant_list:
            score = 0
            restaurant_text = f"{restaurant.get('vibe', '')} {restaurant.get('aim', '')} {restaurant.get('cuisine', '')}".lower()
            
            # Аналізуємо відповідність
            for category, (user_keywords, restaurant_keywords) in keywords_map.items():
                user_match = any(keyword in user_lower for keyword in user_keywords)
                if user_match:
                    restaurant_match = any(keyword in restaurant_text for keyword in restaurant_keywords)
                    if restaurant_match:
                        score += 5
                    
            # Додаємо випадковий бонус для різноманітності
            score += random.uniform(0, 2)
            
            scored_restaurants.append((score, restaurant))
        
        # Сортуємо, але беремо з ТОП-3 випадково
        scored_restaurants.sort(key=lambda x: x[0], reverse=True)
        
        if scored_restaurants[0][0] > 0:
            # Якщо є хороші варіанти, беремо один з топ-3 випадково
            top_candidates = scored_restaurants[:min(3, len(scored_restaurants))]
            chosen = random.choice(top_candidates)[1]
            logger.info(f"🎯 Резервний алгоритм обрав: {chosen.get('name', '')} (випадково з ТОП-3)")
            return chosen
        else:
            # Якщо немає явних збігів, беремо випадковий
            chosen = random.choice(restaurant_list)
            logger.info(f"🎲 Резервний алгоритм: випадковий вибір - {chosen.get('name', '')}")
            return chosen

    def _fallback_selection_dict(self, user_request: str):
        """Резервний алгоритм що повертає словник"""
        if not self.restaurants_data:
            logger.error("❌ Немає даних про ресторани для fallback")
            return {
                "name": "Ресторан недоступний",
                "address": "Спробуйте пізніше",
                "socials": "",
                "vibe": "",
                "aim": "",
                "cuisine": "",
                "menu": "",
                "menu_url": "",
                "photo": ""
            }
            
        chosen = self._smart_fallback_selection(user_request, self.restaurants_data)
        
        # Перетворюємо Google Drive посилання на фото
        photo_url = chosen.get('photo', '')
        if photo_url:
            photo_url = self._convert_google_drive_url(photo_url)
        
        return {
            "name": chosen.get('name', 'Ресторан'),
            "address": chosen.get('address', 'Адреса не вказана'),
            "socials": chosen.get('socials', 'Соц-мережі не вказані'),
            "vibe": chosen.get('vibe', 'Приємна атмосфера'),
            "aim": chosen.get('aim', 'Для будь-яких подій'),
            "cuisine": chosen.get('cuisine', 'Смачна кухня'),
            "menu": chosen.get('menu', ''),
            "menu_url": chosen.get('menu_url', ''),
            "photo": photo_url
        }

restaurant_bot = RestaurantBot()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник команди /start"""
    user_id = update.effective_user.id
    user_states[user_id] = "waiting_request"
    
    message = (
        "🍽 Привіт! Я допоможу тобі знайти ідеальний ресторан!\n\n"
        "Розкажи мені про своє побажання. Наприклад:\n"
        "• 'Хочу місце для обіду з сім'єю'\n"
        "• 'Потрібен ресторан для побачення'\n"
        "• 'Шукаю піцу з друзями'\n\n"
        "Напиши, що ти шукаєш! 😊"
    )
    
    await update.message.reply_text(message)
    logger.info(f"✅ Користувач {user_id} почав діалог")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник текстових повідомлень"""
    user_id = update.effective_user.id
    
    # Якщо користувач не використав /start, пропонуємо це зробити
    if user_id not in user_states:
        await update.message.reply_text("Напишіть /start, щоб почати")
        return
    
    user_request = update.message.text
    logger.info(f"🔍 Користувач {user_id} написав: {user_request}")
    
    # Показуємо, що шукаємо
    processing_message = await update.message.reply_text("🔍 Шукаю ідеальний ресторан для вас...")
    
    # Отримуємо рекомендацію
    recommendation = await restaurant_bot.get_recommendation(user_request)
    
    # Видаляємо повідомлення "шукаю"
    try:
        await processing_message.delete()
    except:
        pass
    
    if recommendation:
        # Готуємо основну інформацію
        response_text = f"""🏠 <b>{recommendation['name']}</b>

📍 <b>Адреса:</b> {recommendation['address']}

📱 <b>Соц-мережі:</b> {recommendation['socials']}

✨ <b>Атмосфера:</b> {recommendation['vibe']}"""

        # Додаємо ТІЛЬКИ посилання на меню (без тексту меню)
        menu_url = recommendation.get('menu_url', '')
        if menu_url and menu_url.startswith('http'):
            response_text += f"\n\n📋 <a href='{menu_url}'>Переглянути меню</a>"

        # Перевіряємо чи є фото
        photo_url = recommendation.get('photo', '')
        
        if photo_url and photo_url.startswith('http'):
            # Надсилаємо фото як медіафайл з підписом
            try:
                logger.info(f"📸 Спроба надіслати фото: {photo_url}")
                await update.message.reply_photo(
                    photo=photo_url,
                    caption=response_text,
                    parse_mode='HTML'
                )
                logger.info(f"✅ Надіслано рекомендацію з фото: {recommendation['name']}")
            except Exception as photo_error:
                logger.warning(f"⚠️ Не вдалося надіслати фото: {photo_error}")
                logger.warning(f"📸 Посилання на фото: {photo_url}")
                # Якщо фото не завантажується, надсилаємо текст без фото
                response_text += f"\n\n📸 <a href='{photo_url}'>Переглянути фото ресторану</a>"
                await update.message.reply_text(response_text, parse_mode='HTML')
                logger.info(f"✅ Надіслано рекомендацію з посиланням на фото: {recommendation['name']}")
        else:
            # Надсилаємо тільки текст якщо фото немає
            await update.message.reply_text(response_text, parse_mode='HTML')
            logger.info(f"✅ Надіслано текстову рекомендацію: {recommendation['name']}")
    else:
        await update.message.reply_text("Вибачте, не знайшов закладів з потрібними стравами. Спробуйте змінити запит або вказати конкретну страву.")
        logger.warning(f"⚠️ Не знайдено рекомендацій для користувача {user_id}")
    
    # Прибираємо стан користувача і пропонуємо почати заново
    del user_states[user_id]
    await update.message.reply_text("Напишіть /start, щоб почати знову")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Обробник помилок"""
    logger.error(f"❌ Помилка: {context.error}")

def main():
    """Основна функція запуску бота"""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN не встановлений!")
        return
        
    if not OPENAI_API_KEY:
        logger.error("❌ OPENAI_API_KEY не встановлений!")
        return
        
    if not GOOGLE_SHEET_URL:
        logger.error("❌ GOOGLE_SHEET_URL не встановлений!")
        return
    
    logger.info("🚀 Запускаю оновлений бота...")
    
    try:
        # Створюємо новий event loop для кожного запуску
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        logger.info("✅ Telegram додаток створено успішно!")
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)
        
        logger.info("🔗 Підключаюся до Google Sheets...")
        loop.run_until_complete(restaurant_bot.init_google_sheets())
        
        logger.info("✅ Всі сервіси підключено! Бот готовий до роботи!")
        
        # Запускаємо polling
        loop.run_until_complete(application.run_polling(drop_pending_updates=True))
        
    except KeyboardInterrupt:
        logger.info("🛑 Бот зупинено користувачем")
    except Exception as e:
        logger.error(f"❌ Критична помилка: {e}")
    finally:
        try:
            loop.close()
        except:
            pass

if __name__ == '__main__':
    main()
