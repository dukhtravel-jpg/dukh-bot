import logging
import os
from typing import Dict, Optional
import asyncio
import json
import re
from datetime import datetime

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
ANALYTICS_SHEET_URL = os.getenv('ANALYTICS_SHEET_URL', GOOGLE_SHEET_URL)

# Глобальні змінні
openai_client = None
user_states: Dict[int, str] = {}
user_last_recommendation: Dict[int, str] = {}
user_rating_data: Dict[int, Dict] = {}

class RestaurantBot:
    def __init__(self):
        self.restaurants_data = []
        self.google_sheets_available = False
        self.analytics_sheet = None
        self.gc = None
    
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
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive.readonly"
            ]
            
            credentials_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
            creds = Credentials.from_service_account_info(credentials_dict, scopes=scope)
            
            self.gc = gspread.authorize(creds)
            
            # Завантажуємо дані ресторанів
            google_sheet = self.gc.open_by_url(GOOGLE_SHEET_URL)
            worksheet = google_sheet.sheet1
            
            records = worksheet.get_all_records()
            
            if records:
                self.restaurants_data = records
                self.google_sheets_available = True
                logger.info(f"✅ Завантажено {len(self.restaurants_data)} закладів з Google Sheets")
            else:
                logger.warning("Google Sheets порожній")
            
            # Ініціалізуємо аналітичну таблицю
            await self.init_analytics_sheet()
                
        except Exception as e:
            logger.error(f"Детальна помилка Google Sheets: {type(e).__name__}: {str(e)}")
    
    async def init_analytics_sheet(self):
        """Ініціалізація аналітичної таблиці"""
        try:
            analytics_sheet = self.gc.open_by_url(ANALYTICS_SHEET_URL)
            logger.info(f"📊 Відкрито таблицю для analytics: {ANALYTICS_SHEET_URL}")
            
            existing_sheets = [worksheet.title for worksheet in analytics_sheet.worksheets()]
            logger.info(f"📋 Існуючі аркуші: {existing_sheets}")
            
            try:
                self.analytics_sheet = analytics_sheet.worksheet("Analytics")
                logger.info("✅ Знайдено існуючий лист Analytics")
                
                try:
                    headers = self.analytics_sheet.row_values(1)
                    if "Rating Explanation" not in headers:
                        logger.info("🔧 Додаю колонку Rating Explanation до існуючого аркуша")
                        if "Rating" in headers:
                            rating_index = headers.index("Rating") + 1
                            self.analytics_sheet.insert_cols([[]], col=rating_index + 2)
                            self.analytics_sheet.update_cell(1, rating_index + 2, "Rating Explanation")
                        else:
                            next_col = len(headers) + 1
                            self.analytics_sheet.update_cell(1, next_col, "Rating Explanation")
                except Exception as header_error:
                    logger.warning(f"⚠️ Помилка перевірки заголовків: {header_error}")
                    
            except gspread.WorksheetNotFound:
                logger.info("📝 Аркуш Analytics не знайдено, створюю новий...")
                
                self.analytics_sheet = analytics_sheet.add_worksheet(title="Analytics", rows="1000", cols="12")
                logger.info("✅ Створено новий лист Analytics")
                
                headers = [
                    "Timestamp", "User ID", "User Request", "Restaurant Name", 
                    "Rating", "Rating Explanation", "Date", "Time"
                ]
                self.analytics_sheet.append_row(headers)
                logger.info("✅ Додано заголовки до Analytics")
            
            try:
                self.summary_sheet = analytics_sheet.worksheet("Summary")
                logger.info("✅ Знайдено існуючий лист Summary")
            except gspread.WorksheetNotFound:
                self.summary_sheet = analytics_sheet.add_worksheet(title="Summary", rows="100", cols="5")
                logger.info("✅ Створено новий лист Summary")
                
                summary_data = [
                    ["Метрика", "Значення", "Останнє оновлення"],
                    ["Загальна кількість запитів", "0", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                    ["Кількість унікальних користувачів", "0", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                    ["Середня оцінка відповідності", "0", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                    ["Кількість оцінок", "0", datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
                ]
                
                for row in summary_data:
                    self.summary_sheet.append_row(row)
                    
                logger.info("✅ Додано початкові дані до Summary")
            
            logger.info("🧪 Тестую можливість запису до Analytics...")
            test_success = await self.test_analytics_write()
            if test_success:
                logger.info("✅ Тест запису до Analytics успішний!")
            else:
                logger.error("❌ Тест запису до Analytics не вдався!")
                
        except Exception as e:
            logger.error(f"Помилка ініціалізації Analytics: {e}")
            self.analytics_sheet = None
    
    async def test_analytics_write(self):
        """Тест запису до Analytics аркуша"""
        if not self.analytics_sheet:
            return False
        
        try:
            headers = self.analytics_sheet.row_values(1)
            logger.info(f"📋 Заголовки Analytics: {headers}")
            
            test_row = [
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "TEST_USER",
                "TEST_REQUEST", 
                "TEST_RESTAURANT",
                "5",
                "Test explanation",
                datetime.now().strftime("%Y-%m-%d"),
                datetime.now().strftime("%H:%M:%S")
            ]
            
            self.analytics_sheet.append_row(test_row)
            logger.info("✅ Тестовий запис додано успішно")
            
            all_values = self.analytics_sheet.get_all_values()
            if len(all_values) > 1:
                last_row = len(all_values)
                if "TEST_USER" in all_values[-1]:
                    self.analytics_sheet.delete_rows(last_row)
                    logger.info("✅ Тестовий запис видалено")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Помилка тесту запису: {e}")
            return False

    def _filter_by_establishment_type(self, user_request: str, restaurant_list):
        """Фільтрує ресторани за типом закладу"""
        user_lower = user_request.lower()
        logger.info(f"🏢 Аналізую запит на тип закладу: '{user_request}'")
        
        # Визначаємо тип закладу з запиту користувача
        type_keywords = {
            'ресторан': {
                'user_keywords': ['ресторан', 'обід', 'вечеря', 'побачення', 'романтик', 'святкув', 'банкет', 'посидіти', 'поїсти'],
                'establishment_types': ['ресторан']
            },
            'кав\'ярня': {
                'user_keywords': ['кава', 'капучіно', 'латте', 'еспресо', 'кав\'ярн', 'десерт', 'тирамісу', 'круасан', 'випити кави', 'кофе'],
                'establishment_types': ['кав\'ярня', 'кафе']
            },
            'to-go': {
                'user_keywords': ['швидко', 'на винос', 'перекус', 'поспішаю', 'to-go', 'takeaway', 'на швидку руку', 'перехопити'],
                'establishment_types': ['to-go', 'takeaway']
            },
            'доставка': {
                'user_keywords': ['доставка', 'додому', 'замовити', 'привезти', 'delivery', 'не хочу йти'],
                'establishment_types': ['доставка', 'delivery']
            }
        }
        
        # Знаходимо відповідний тип закладу
        detected_types = []
        for establishment_type, keywords in type_keywords.items():
            user_match = any(keyword in user_lower for keyword in keywords['user_keywords'])
            if user_match:
                detected_types.extend(keywords['establishment_types'])
        
        # Якщо тип не визначено, не фільтруємо
        if not detected_types:
            logger.info("🏢 Тип закладу не визначено, повертаю всі заклади")
            return restaurant_list
        
        logger.info(f"🏢 Виявлено типи закладів: {detected_types}")
        
        # Фільтруємо за типом закладу
        filtered_restaurants = []
        for restaurant in restaurant_list:
            establishment_type = restaurant.get('тип закладу', restaurant.get('type', '')).lower()
            
            # Перевіряємо чи збігається тип закладу
            type_match = any(detected_type.lower() in establishment_type or establishment_type in detected_type.lower() 
                           for detected_type in detected_types)
            
            if type_match:
                filtered_restaurants.append(restaurant)
                logger.info(f"   ✅ {restaurant.get('name', '')}: тип '{establishment_type}' підходить")
            else:
                logger.info(f"   ❌ {restaurant.get('name', '')}: тип '{establishment_type}' не підходить")
        
        if filtered_restaurants:
            logger.info(f"🏢 Відфільтровано {len(filtered_restaurants)} закладів відповідного типу з {len(restaurant_list)}")
            return filtered_restaurants
        else:
            logger.warning("⚠️ Жоден заклад не підходить за типом, повертаю всі")
            return restaurant_list

    def _filter_by_vibe(self, user_request: str, restaurant_list):
        """Фільтрує ресторани за атмосферою (vibe)"""
        user_lower = user_request.lower()
        logger.info(f"✨ Аналізую запит на атмосферу: '{user_request}'")
        
        # Ключові слова для атмосфери
        vibe_keywords = {
            'романтичн': ['романт', 'побачен', 'інтимн', 'затишн', 'свічки', 'романс', 'двох'],
            'веsel': ['весел', 'живо', 'енергійн', 'гучн', 'драйв', 'динамічн'],
            'спокійн': ['спокійн', 'тих', 'релакс', 'умиротворен'],
            'елегантн': ['елегантн', 'розкішн', 'стильн', 'преміум', 'вишукан'],
            'casual': ['casual', 'невимушен', 'простий', 'домашн'],
            'затишн': ['затишн', 'домашн', 'теплий', 'комфортн']
        }
        
        # Знаходимо відповідну атмосферу
        detected_vibes = []
        for vibe_type, keywords in vibe_keywords.items():
            user_match = any(keyword in user_lower for keyword in keywords)
            if user_match:
                detected_vibes.append(vibe_type)
        
        if not detected_vibes:
            logger.info("✨ Атмосфера не визначена, повертаю всі заклади")
            return restaurant_list
        
        logger.info(f"✨ Виявлено атмосферу: {detected_vibes}")
        
        # Фільтруємо за атмосферою
        filtered_restaurants = []
        for restaurant in restaurant_list:
            restaurant_vibe = restaurant.get('vibe', '').lower()
            
            # Перевіряємо збіг атмосфери
            vibe_match = any(
                any(keyword in restaurant_vibe for keyword in vibe_keywords[detected_vibe])
                for detected_vibe in detected_vibes
            )
            
            if vibe_match:
                filtered_restaurants.append(restaurant)
                logger.info(f"   ✅ {restaurant.get('name', '')}: атмосфера '{restaurant_vibe}' підходить")
            else:
                logger.info(f"   ❌ {restaurant.get('name', '')}: атмосфера '{restaurant_vibe}' не підходить")
        
        if filtered_restaurants:
            logger.info(f"✨ Відфільтровано {len(filtered_restaurants)} закладів відповідної атмосфери з {len(restaurant_list)}")
            return filtered_restaurants
        else:
            logger.warning("⚠️ Жоден заклад не підходить за атмосферою, повертаю всі")
            return restaurant_list

    def _filter_by_aim(self, user_request: str, restaurant_list):
        """Фільтрує ресторани за призначенням (aim)"""
        user_lower = user_request.lower()
        logger.info(f"🎯 Аналізую запит на призначення: '{user_request}'")
        
        # Ключові слова для призначення
        aim_keywords = {
            'сімейн': ['сім', 'діт', 'родин', 'батьк', 'мам', 'дитин', 'всією родиною'],
            'діл': ['діл', 'зустріч', 'перегов', 'бізнес', 'робоч', 'офіс', 'партнер'],
            'друз': ['друз', 'компан', 'гуртом', 'тусовк', 'молодіжн'],
            'пар': ['пар', 'двох', 'побачен', 'романт', 'коханої', 'коханого'],
            'святков': ['святкув', 'день народж', 'ювіле', 'свято', 'торжеств', 'банкет'],
            'самот': ['сам', 'одн', 'поодин', 'без компанії'],
            'груп': ['груп', 'багат', 'велик компан', 'корпоратив']
        }
        
        # Знаходимо відповідне призначення
        detected_aims = []
        for aim_type, keywords in aim_keywords.items():
            user_match = any(keyword in user_lower for keyword in keywords)
            if user_match:
                detected_aims.append(aim_type)
        
        if not detected_aims:
            logger.info("🎯 Призначення не визначено, повертаю всі заклади")
            return restaurant_list
        
        logger.info(f"🎯 Виявлено призначення: {detected_aims}")
        
        # Фільтруємо за призначенням
        filtered_restaurants = []
        for restaurant in restaurant_list:
            restaurant_aim = restaurant.get('aim', '').lower()
            
            # Перевіряємо збіг призначення
            aim_match = any(
                any(keyword in restaurant_aim for keyword in aim_keywords[detected_aim])
                for detected_aim in detected_aims
            )
            
            if aim_match:
                filtered_restaurants.append(restaurant)
                logger.info(f"   ✅ {restaurant.get('name', '')}: призначення '{restaurant_aim}' підходить")
            else:
                logger.info(f"   ❌ {restaurant.get('name', '')}: призначення '{restaurant_aim}' не підходить")
        
        if filtered_restaurants:
            logger.info(f"🎯 Відфільтровано {len(filtered_restaurants)} закладів відповідного призначення з {len(restaurant_list)}")
            return filtered_restaurants
        else:
            logger.warning("⚠️ Жоден заклад не підходить за призначенням, повертаю всі")
            return restaurant_list

    def _filter_by_context(self, user_request: str, restaurant_list):
        """Фільтрує ресторани за контекстом запиту"""
        user_lower = user_request.lower()
        logger.info(f"🎯 Аналізую запит на контекст: '{user_request}'")
        
        context_filters = {
            'romantic': {
                'user_keywords': ['романт', 'побачен', 'двох', 'інтимн', 'затишн', 'свічки', 'романс'],
                'restaurant_keywords': ['романт', 'інтимн', 'затишн', 'для пар', 'камерн', 'приват']
            },
            'family': {
                'user_keywords': ['сім', 'діт', 'родин', 'батьк', 'мам', 'дитин'],
                'restaurant_keywords': ['сімейн', 'діт', 'родин', 'для всієї сім']
            },
            'business': {
                'user_keywords': ['діл', 'зустріч', 'перегов', 'бізнес', 'робоч', 'офіс'],
                'restaurant_keywords': ['діл', 'зустріч', 'бізнес', 'перегов', 'офіц']
            },
            'friends': {
                'user_keywords': ['друз', 'компан', 'гуртом', 'весел', 'тусовк'],
                'restaurant_keywords': ['компан', 'друз', 'молодіжн', 'весел', 'гучн']
            },
            'celebration': {
                'user_keywords': ['святкув', 'день народж', 'ювіле', 'свято', 'торжеств'],
                'restaurant_keywords': ['святков', 'просторн', 'банкет', 'торжеств', 'груп']
            },
            'quick': {
                'user_keywords': ['швидк', 'перекус', 'фаст', 'поспіша', 'на швидку руку'],
                'restaurant_keywords': ['швидк', 'casual', 'фаст', 'перекус']
            }
        }
        
        detected_contexts = []
        for context, keywords in context_filters.items():
            user_match = any(keyword in user_lower for keyword in keywords['user_keywords'])
            if user_match:
                detected_contexts.append(context)
        
        if not detected_contexts:
            logger.info("📝 Контекст не визначено, повертаю всі ресторани")
            return restaurant_list
        
        logger.info(f"🎯 Виявлено контекст(и): {detected_contexts}")
        
        filtered_restaurants = []
        for restaurant in restaurant_list:
            restaurant_text = f"{restaurant.get('vibe', '')} {restaurant.get('aim', '')} {restaurant.get('cuisine', '')} {restaurant.get('name', '')}".lower()
            
            restaurant_score = 0
            matched_contexts = []
            
            for context in detected_contexts:
                context_keywords = context_filters[context]['restaurant_keywords']
                if any(keyword in restaurant_text for keyword in context_keywords):
                    restaurant_score += 1
                    matched_contexts.append(context)
            
            if restaurant_score > 0:
                filtered_restaurants.append((restaurant_score, restaurant, matched_contexts))
                logger.info(f"   ✅ {restaurant.get('name', '')}: збіг по {matched_contexts}")
            else:
                logger.info(f"   ❌ {restaurant.get('name', '')}: не підходить за контекстом")
        
        if filtered_restaurants:
            filtered_restaurants.sort(key=lambda x: x[0], reverse=True)
            result = [item[1] for item in filtered_restaurants]
            logger.info(f"🎯 Відфільтровано {len(result)} релевантних ресторанів з {len(restaurant_list)}")
            return result
        else:
            logger.warning("⚠️ Жоден ресторан не підходить за контекстом, повертаю всі")
            return restaurant_list

    def _filter_by_menu(self, user_request: str, restaurant_list):
        """Фільтрує ресторани по меню"""
        user_lower = user_request.lower()
        
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
        
        requested_dishes = []
        for dish, keywords in food_keywords.items():
            if any(keyword in user_lower for keyword in keywords):
                requested_dishes.append(dish)
        
        if requested_dishes:
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
            logger.info("🔍 Загальний запит, аналізую всі ресторани")
            return restaurant_list

    async def get_recommendation(self, user_request: str) -> Optional[Dict]:
        """Отримання рекомендації через OpenAI з урахуванням типу закладу, контексту та меню"""
        try:
            global openai_client
            if openai_client is None:
                import openai
                openai.api_key = OPENAI_API_KEY
                openai_client = openai
                logger.info("✅ OpenAI клієнт ініціалізовано")
            
            if not self.restaurants_data:
                logger.error("❌ Немає даних про ресторани")
                return None
            
            import random
            shuffled_restaurants = self.restaurants_data.copy()
            random.shuffle(shuffled_restaurants)
            
            logger.info(f"🎲 Перемішав порядок ресторанів для різноманітності")
            
            # ТРЬОХЕТАПНА ФІЛЬТРАЦІЯ для максимальної точності:
            
            # 1. Спочатку фільтруємо за ТИПОМ ЗАКЛАДУ (ресторан/кав'ярня/доставка/to-go)
            type_filtered = self._filter_by_establishment_type(user_request, shuffled_restaurants)
            
            # 2. Потім фільтруємо за КОНТЕКСТОМ (романтика/сім'я/друзі тощо)
            context_filtered = self._filter_by_context(user_request, type_filtered)
            
            # 3. Нарешті фільтруємо по МЕНЮ (якщо шукають конкретну страву)
            final_filtered = self._filter_by_menu(user_request, context_filtered)
            
            restaurants_details = []
            for i, r in enumerate(final_filtered):
                establishment_type = r.get('тип закладу', r.get('type', 'Не вказано'))
                detail = f"""Варіант {i+1}:
- Назва: {r.get('name', 'Без назви')}
- Тип: {establishment_type}
- Атмосфера: {r.get('vibe', 'Не описана')}
- Призначення: {r.get('aim', 'Не вказано')}
- Кухня: {r.get('cuisine', 'Не вказана')}"""
                restaurants_details.append(detail)
            
            restaurants_text = "\n\n".join(restaurants_details)
            
            prompt = f"""ЗАПИТ КОРИСТУВАЧА: "{user_request}"

ВАЖЛИВО: Всі заклади нижче пройшли ЧОТИРЬОХЕТАПНУ ФІЛЬТРАЦІЮ і максимально підходять під запит.

{restaurants_text}

ЗАВДАННЯ:
1. Обери 2 НАЙКРАЩІ варіанти (якщо є тільки 1 варіант, то тільки його)
2. Вкажи який з них є ПРІОРИТЕТНИМ і коротко поясни ЧОМУ

ФОРМАТ ВІДПОВІДІ:
Варіанти: [номер1, номер2]
Пріоритет: [номер] - [коротке пояснення причини]

ПРИКЛАД:
Варіанти: [1, 3]
Пріоритет: 1 - ідеально підходить за атмосферою та розташуванням

ТВОЯ ВІДПОВІДЬ:"""

            logger.info(f"🤖 Запитую у OpenAI 2 найкращі варіанти з {len(final_filtered)} відфільтрованих...")
            
            # Показуємо деталі всіх варіантів для діагностики
            for i, r in enumerate(final_filtered):
                logger.info(f"   {i+1}. {r.get('name', '')} ({r.get('тип закладу', r.get('type', ''))} | {r.get('vibe', '')} | {r.get('aim', '')})")

            def make_openai_request():
                return openai_client.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "Ти експерт-ресторатор. Аналізуй варіанти та обирай найкращі з обґрунтуванням."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=200,
                    temperature=0.3,
                    top_p=0.9
                )
            
            response = await asyncio.wait_for(
                asyncio.to_thread(make_openai_request),
                timeout=20
            )
            
            choice_text = response.choices[0].message.content.strip()
            logger.info(f"🤖 OpenAI повна відповідь: '{choice_text}'")
            
            # Парсимо відповідь OpenAI
            recommendations = self._parse_dual_recommendation(choice_text, final_filtered)
            
            if recommendations:
                return recommendations
            else:
                logger.warning("⚠️ Не вдалось розпарсити відповідь OpenAI, використовую резервний алгоритм")
                # Резервний варіант - беремо 2 найкращі за резервним алгоритмом
                return self._fallback_dual_selection(user_request, final_filtered)
            
            def make_openai_request():
                return openai_client.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "Ти експерт-ресторатор. Обирай варіанти різноманітно з УЖЕ ВІДФІЛЬТРОВАНОГО списку."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=200,
                    temperature=0.4,
                    top_p=0.9
                )
            
            response = await asyncio.wait_for(
                asyncio.to_thread(make_openai_request),
                timeout=20
            )
            
            choice_text = response.choices[0].message.content.strip()
            logger.info(f"🤖 OpenAI повна відповідь: '{choice_text}'")
            
            numbers = re.findall(r'\d+', choice_text)
            
            if numbers:
                choice_num = int(numbers[0]) - 1
                logger.info(f"🔍 Знайдено число в відповіді: {numbers[0]} → індекс {choice_num}")
                
                if 0 <= choice_num < len(final_filtered):
                    chosen_restaurant = final_filtered[choice_num]
                    logger.info(f"✅ OpenAI обрав ВІДФІЛЬТРОВАНИЙ ресторан: {chosen_restaurant.get('name', '')} (варіант {choice_num + 1} з {len(final_filtered)})")
                else:
                    logger.warning(f"⚠️ Число {choice_num + 1} поза межами, використовую резервний алгоритм")
                    chosen_restaurant = self._smart_fallback_selection(user_request, final_filtered)
            else:
                logger.warning("⚠️ Не знайдено чисел в відповіді, використовую резервний алгоритм")
                chosen_restaurant = self._smart_fallback_selection(user_request, final_filtered)
        except asyncio.TimeoutError:
            logger.error("⏰ Timeout при запиті до OpenAI, використовую резервний алгоритм")
            return self._fallback_dual_selection(user_request, self.restaurants_data)
        except Exception as e:
            logger.error(f"❌ Помилка отримання рекомендації: {e}")
            return self._fallback_dual_selection(user_request, self.restaurants_data)

    def _parse_dual_recommendation(self, openai_response: str, filtered_restaurants):
        """Парсить відповідь OpenAI з двома рекомендаціями"""
        try:
            lines = openai_response.strip().split('\n')
            variants_line = ""
            priority_line = ""
            
            for line in lines:
                line = line.strip()
                if line.lower().startswith('варіант') and '[' in line:
                    variants_line = line
                elif line.lower().startswith('пріоритет') and '-' in line:
                    priority_line = line
            
            logger.info(f"🔍 Парсинг - Варіанти: '{variants_line}', Пріоритет: '{priority_line}'")
            
            # Витягуємо номери варіантів
            import re
            numbers = re.findall(r'\d+', variants_line)
            
            if len(numbers) >= 1:
                # Конвертуємо в індекси (мінус 1)
                indices = [int(num) - 1 for num in numbers[:2]]  # Беремо максимум 2
                
                # Перевіряємо що індекси в межах
                valid_indices = [idx for idx in indices if 0 <= idx < len(filtered_restaurants)]
                
                if not valid_indices:
                    logger.warning("⚠️ Всі індекси поза межами")
                    return None
                
                restaurants = [filtered_restaurants[idx] for idx in valid_indices]
                
                # Визначаємо пріоритетний ресторан
                priority_num = None
                priority_explanation = "найкращий варіант за всіма критеріями"
                
                if priority_line and '-' in priority_line:
                    # Шукаємо номер пріоритету
                    priority_match = re.search(r'(\d+)', priority_line.split('-')[0])
                    if priority_match:
                        priority_num = int(priority_match.group(1))
                    
                    # Витягуємо пояснення
                    explanation_part = priority_line.split('-', 1)[1].strip()
                    if explanation_part:
                        priority_explanation = explanation_part
                
                # Визначаємо який ресторан пріоритетний
                if priority_num and (priority_num - 1) in valid_indices:
                    priority_index = valid_indices.index(priority_num - 1)
                else:
                    priority_index = 0  # За замовчуванням перший
                
                logger.info(f"✅ Розпарсено: {len(restaurants)} ресторанів, пріоритет: {priority_index + 1}")
                
                # Повертаємо структуру з двома рекомендаціями
                result = {
                    "restaurants": [],
                    "priority_index": priority_index,
                    "priority_explanation": priority_explanation
                }
                
                for restaurant in restaurants:
                    photo_url = restaurant.get('photo', '')
                    if photo_url:
                        photo_url = self._convert_google_drive_url(photo_url)
                    
                    result["restaurants"].append({
                        "name": restaurant.get('name', 'Ресторан'),
                        "address": restaurant.get('address', 'Адреса не вказана'),
                        "socials": restaurant.get('socials', 'Соц-мережі не вказані'),
                        "vibe": restaurant.get('vibe', 'Приємна атмосфера'),
                        "aim": restaurant.get('aim', 'Для будь-яких подій'),
                        "cuisine": restaurant.get('cuisine', 'Смачна кухня'),
                        "menu": restaurant.get('menu', ''),
                        "menu_url": restaurant.get('menu_url', ''),
                        "photo": photo_url,
                        "type": restaurant.get('тип закладу', restaurant.get('type', 'Заклад'))
                    })
                
                return result
            
            logger.warning("⚠️ Не знайдено номерів у відповіді OpenAI")
            return None
            
        except Exception as e:
            logger.error(f"❌ Помилка парсингу відповіді OpenAI: {e}")
            return None

    def _fallback_dual_selection(self, user_request: str, restaurant_list):
        """Резервний алгоритм для двох рекомендацій"""
        if not restaurant_list:
            return None
        
        import random
        
        # Якщо тільки один ресторан
        if len(restaurant_list) == 1:
            chosen = restaurant_list[0]
            photo_url = chosen.get('photo', '')
            if photo_url:
                photo_url = self._convert_google_drive_url(photo_url)
                
            return {
                "restaurants": [{
                    "name": chosen.get('name', 'Ресторан'),
                    "address": chosen.get('address', 'Адреса не вказана'),
                    "socials": chosen.get('socials', 'Соц-мережі не вказані'),
                    "vibe": chosen.get('vibe', 'Приємна атмосфера'),
                    "aim": chosen.get('aim', 'Для будь-яких подій'),
                    "cuisine": chosen.get('cuisine', 'Смачна кухня'),
                    "menu": chosen.get('menu', ''),
                    "menu_url": chosen.get('menu_url', ''),
                    "photo": photo_url,
                    "type": chosen.get('тип закладу', chosen.get('type', 'Заклад'))
                }],
                "priority_index": 0,
                "priority_explanation": "єдиний доступний варіант після фільтрації"
            }
        
        # Використовуємо розумний алгоритм для вибору 2 найкращих
        scored_restaurants = []
        user_lower = user_request.lower()
        
        keywords_map = {
            'romantic': (['романт', 'побачен', 'інтимн'], ['інтимн', 'романт', 'пар']),
            'family': (['сім', 'діт', 'родин'], ['сімейн', 'діт', 'родин']),
            'business': (['діл', 'зустріч', 'бізнес'], ['діл', 'бізнес']),
            'friends': (['друз', 'компан', 'весел'], ['компан', 'друз', 'молодіжн'])
        }
        
        for restaurant in restaurant_list:
            score = 0
            restaurant_text = f"{restaurant.get('vibe', '')} {restaurant.get('aim', '')}".lower()
            
            for category, (user_keywords, restaurant_keywords) in keywords_map.items():
                user_match = any(keyword in user_lower for keyword in user_keywords)
                if user_match:
                    restaurant_match = any(keyword in restaurant_text for keyword in restaurant_keywords)
                    if restaurant_match:
                        score += 3
            
            score += random.uniform(0, 1)  # Невеликий випадковий бонус
            scored_restaurants.append((score, restaurant))
        
        # Сортуємо та беремо топ-2
        scored_restaurants.sort(key=lambda x: x[0], reverse=True)
        top_restaurants = [item[1] for item in scored_restaurants[:2]]
        
        # Формуємо результат
        result = {
            "restaurants": [],
            "priority_index": 0,
            "priority_explanation": "найвищий рейтинг за алгоритмом відповідності"
        }
        
        for restaurant in top_restaurants:
            photo_url = restaurant.get('photo', '')
            if photo_url:
                photo_url = self._convert_google_drive_url(photo_url)
            
            result["restaurants"].append({
                "name": restaurant.get('name', 'Ресторан'),
                "address": restaurant.get('address', 'Адреса не вказана'),
                "socials": restaurant.get('socials', 'Соц-мережі не вказані'),
                "vibe": restaurant.get('vibe', 'Приємна атмосфера'),
                "aim": restaurant.get('aim', 'Для будь-яких подій'),
                "cuisine": restaurant.get('cuisine', 'Смачна кухня'),
                "menu": restaurant.get('menu', ''),
                "menu_url": restaurant.get('menu_url', ''),
                "photo": photo_url,
                "type": restaurant.get('тип закладу', restaurant.get('type', 'Заклад'))
            })
        
        logger.info(f"🎯 Резервний алгоритм: обрано {len(result['restaurants'])} ресторанів")
        return result

    def _smart_fallback_selection(self, user_request: str, restaurant_list):
        """Резервний алгоритм з рандомізацією"""
        import random
        
        user_lower = user_request.lower()
        
        keywords_map = {
            'romantic': (['романт', 'побачен', 'двох', 'інтимн', 'затишн'], ['інтимн', 'романт', 'для пар', 'затишн']),
            'family': (['сім', 'діт', 'родин', 'батьк'], ['сімейн', 'діт', 'родин']),
            'business': (['діл', 'зустріч', 'перегов', 'бізнес'], ['діл', 'зустріч', 'бізнес']),
            'friends': (['друз', 'компан', 'гуртом', 'весел'], ['компан', 'друз', 'молодіжн']),
            'quick': (['швидк', 'перекус', 'фаст', 'поспіша'], ['швидк', 'casual', 'фаст']),
            'celebration': (['святкув', 'день народж', 'ювіле', 'свято'], ['святков', 'простор', 'груп'])
        }
        
        scored_restaurants = []
        for restaurant in restaurant_list:
            score = 0
            restaurant_text = f"{restaurant.get('vibe', '')} {restaurant.get('aim', '')} {restaurant.get('cuisine', '')}".lower()
            
            for category, (user_keywords, restaurant_keywords) in keywords_map.items():
                user_match = any(keyword in user_lower for keyword in user_keywords)
                if user_match:
                    restaurant_match = any(keyword in restaurant_text for keyword in restaurant_keywords)
                    if restaurant_match:
                        score += 5
                    
            score += random.uniform(0, 2)
            scored_restaurants.append((score, restaurant))
        
        scored_restaurants.sort(key=lambda x: x[0], reverse=True)
        
        if scored_restaurants[0][0] > 0:
            top_candidates = scored_restaurants[:min(3, len(scored_restaurants))]
            chosen = random.choice(top_candidates)[1]
            logger.info(f"🎯 Резервний алгоритм обрав: {chosen.get('name', '')} (випадково з ТОП-3)")
            return chosen
        else:
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

    async def log_request(self, user_id: int, user_request: str, restaurant_name: str, rating: Optional[int] = None, explanation: str = ""):
        """Логування запиту до аналітичної таблиці"""
        if not self.analytics_sheet:
            logger.warning("Analytics sheet не доступний")
            return
            
        try:
            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            date = now.strftime("%Y-%m-%d")
            time = now.strftime("%H:%M:%S")
            
            row_data = [
                timestamp,
                str(user_id),
                user_request,
                restaurant_name,
                str(rating) if rating else "",
                explanation,
                date,
                time
            ]
            
            self.analytics_sheet.append_row(row_data)
            logger.info(f"📊 Записано до Analytics: {user_id} - {restaurant_name} - Оцінка: {rating} - Пояснення: {explanation[:50]}...")
            
            await self.update_summary_stats()
            
        except Exception as e:
            logger.error(f"Помилка логування: {e}")
    
    async def update_summary_stats(self):
        """Оновлення зведеної статистики"""
        if not self.analytics_sheet or not self.summary_sheet:
            return
            
        try:
            all_records = self.analytics_sheet.get_all_records()
            
            if not all_records:
                return
            
            total_requests = len(all_records)
            unique_users = len(set(record['User ID'] for record in all_records))
            
            ratings = [int(record['Rating']) for record in all_records if record['Rating'] and str(record['Rating']).isdigit()]
            avg_rating = sum(ratings) / len(ratings) if ratings else 0
            rating_count = len(ratings)
            
            avg_requests_per_user = total_requests / unique_users if unique_users > 0 else 0
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            self.summary_sheet.update('B2', str(total_requests))
            self.summary_sheet.update('C2', timestamp)
            
            self.summary_sheet.update('B3', str(unique_users))
            self.summary_sheet.update('C3', timestamp)
            
            self.summary_sheet.update('B4', f"{avg_rating:.2f}")
            self.summary_sheet.update('C4', timestamp)
            
            self.summary_sheet.update('B5', str(rating_count))
            self.summary_sheet.update('C5', timestamp)
            
            try:
                self.summary_sheet.update('A6', "Середня кількість запитів на користувача")
                self.summary_sheet.update('B6', f"{avg_requests_per_user:.2f}")
                self.summary_sheet.update('C6', timestamp)
            except:
                self.summary_sheet.append_row(["Середня кількість запитів на користувача", f"{avg_requests_per_user:.2f}", timestamp])
            
            logger.info(f"📈 Оновлено статистику: Запитів: {total_requests}, Користувачів: {unique_users}, Середня оцінка: {avg_rating:.2f}")
            
        except Exception as e:
            logger.error(f"Помилка оновлення статистики: {e}")

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
    
    if user_id not in user_states:
        await update.message.reply_text("Напишіть /start, щоб почати")
        return
    
    user_text = update.message.text
    current_state = user_states[user_id]
    
    if current_state == "waiting_explanation":
        explanation = user_text
        rating_data = user_rating_data.get(user_id, {})
        
        if rating_data:
            await restaurant_bot.log_request(
                user_id, 
                rating_data['user_request'], 
                rating_data['restaurant_name'], 
                rating_data['rating'], 
                explanation
            )
            
            await update.message.reply_text(
                f"Дякую за детальну оцінку! 🙏\n\n"
                f"Ваша оцінка: {rating_data['rating']}/10\n"
                f"Пояснення записано в базу даних.\n\n"
                f"Напишіть /start, щоб знайти ще один ресторан!"
            )
            
            user_states[user_id] = "completed"
            if user_id in user_last_recommendation:
                del user_last_recommendation[user_id]
            if user_id in user_rating_data:
                del user_rating_data[user_id]
            
            logger.info(f"💬 Користувач {user_id} надав пояснення оцінки: {explanation[:100]}...")
            return
    
    if current_state == "waiting_rating" and user_text.isdigit():
        rating = int(user_text)
        if 1 <= rating <= 10:
            restaurant_name = user_last_recommendation.get(user_id, "Невідомий ресторан")
            user_rating_data[user_id] = {
                'rating': rating,
                'restaurant_name': restaurant_name,
                'user_request': 'Оцінка'
            }
            
            user_states[user_id] = "waiting_explanation"
            
            await update.message.reply_text(
                f"Дякую за оцінку {rating}/10! ⭐\n\n"
                f"🤔 <b>Чи можеш пояснити чому така оцінка?</b>\n"
                f"Напиши, що сподобалось або не сподобалось у рекомендації.",
                parse_mode='HTML'
            )
            
            logger.info(f"⭐ Користувач {user_id} оцінив {restaurant_name} на {rating}/10, очікуємо пояснення")
            return
        else:
            await update.message.reply_text("Будь ласка, напишіть число від 1 до 10")
            return
    
    if current_state == "waiting_request":
        user_request = user_text
        logger.info(f"🔍 Користувач {user_id} написав: {user_request}")
        
        processing_message = await update.message.reply_text("🔍 Шукаю ідеальний ресторан для вас...")
        
        recommendation = await restaurant_bot.get_recommendation(user_request)
        
        try:
            await processing_message.delete()
        except:
            pass
        
        if recommendation:
            # Тепер recommendation це словник з кількома ресторанами
            restaurants = recommendation["restaurants"]
            priority_index = recommendation["priority_index"]
            priority_explanation = recommendation["priority_explanation"]
            
            # Логуємо основний (пріоритетний) ресторан
            main_restaurant = restaurants[priority_index]
            await restaurant_bot.log_request(user_id, user_request, main_restaurant["name"])
            
            # Зберігаємо пріоритетний ресторан для оцінки
            user_last_recommendation[user_id] = main_restaurant["name"]
            user_states[user_id] = "waiting_rating"
            
            # Формуємо повідомлення з двома варіантами
            if len(restaurants) == 1:
                # Якщо тільки один варіант
                response_text = f"""🏠 <b>Рекомендую цей заклад:</b>

<b>{restaurants[0]['name']}</b>
📍 {restaurants[0]['address']}
🏢 Тип: {restaurants[0]['type']}
📱 Соц-мережі: {restaurants[0]['socials']}
✨ Атмосфера: {restaurants[0]['vibe']}
🎯 Підходить для: {restaurants[0]['aim']}"""
            else:
                # Якщо два варіанти
                priority_restaurant = restaurants[priority_index]
                alternative_restaurant = restaurants[1 - priority_index]
                
                response_text = f"""🎯 <b>2 найкращі варіанти для вас:</b>

<b>🏆 ПРІОРИТЕТНА РЕКОМЕНДАЦІЯ:</b>
<b>{priority_restaurant['name']}</b>
📍 {priority_restaurant['address']}
🏢 Тип: {priority_restaurant['type']}
📱 Соц-мережі: {priority_restaurant['socials']}
✨ Атмосфера: {priority_restaurant['vibe']}
🎯 Підходить для: {priority_restaurant['aim']}

💡 <i>Чому пріоритет: {priority_explanation}</i>

➖➖➖➖➖➖➖➖➖➖

<b>🥈 АЛЬТЕРНАТИВНИЙ ВАРІАНТ:</b>
<b>{alternative_restaurant['name']}</b>
📍 {alternative_restaurant['address']}
🏢 Тип: {alternative_restaurant['type']}
📱 Соц-мережі: {alternative_restaurant['socials']}
✨ Атмосфера: {alternative_restaurant['vibe']}
🎯 Підходить для: {alternative_restaurant['aim']}"""

            # Додаємо посилання на меню для пріоритетного ресторану
            main_menu_url = main_restaurant.get('menu_url', '')
            if main_menu_url and main_menu_url.startswith('http'):
                response_text += f"\n\n📋 <a href='{main_menu_url}'>Переглянути меню пріоритетного варіанту</a>"

            # Відправляємо фото пріоритетного ресторану (якщо є)
            main_photo_url = main_restaurant.get('photo', '')
            
            if main_photo_url and main_photo_url.startswith('http'):
                try:
                    logger.info(f"📸 Надсилаю фото пріоритетного ресторану: {main_photo_url}")
                    await update.message.reply_photo(
                        photo=main_photo_url,
                        caption=response_text,
                        parse_mode='HTML'
                    )
                    logger.info(f"✅ Надіслано рекомендації з фото: {main_restaurant['name']}")
                except Exception as photo_error:
                    logger.warning(f"⚠️ Не вдалося надіслати фото: {photo_error}")
                    response_text += f"\n\n📸 <a href='{main_photo_url}'>Переглянути фото пріоритетного ресторану</a>"
                    await update.message.reply_text(response_text, parse_mode='HTML')
                    logger.info(f"✅ Надіслано рекомендації з посиланням на фото: {main_restaurant['name']}")
            else:
                await update.message.reply_text(response_text, parse_mode='HTML')
                logger.info(f"✅ Надіслано текстові рекомендації: {main_restaurant['name']}")
            
            # Просимо оцінити ПРІОРИТЕТНИЙ варіант
            rating_text = f"""⭐ <b>Оціни ПРІОРИТЕТНУ рекомендацію від 1 до 10</b>
(оцінюємо "{main_restaurant['name']}")

1 - зовсім не підходить
10 - ідеально підходить

Напиши цифру в чаті 👇"""
            await update.message.reply_text(rating_text, parse_mode='HTML')
            
        else:
            await update.message.reply_text("Вибачте, не знайшов закладів з потрібними стравами. Спробуйте змінити запит або вказати конкретну страву.")
            logger.warning(f"⚠️ Не знайдено рекомендацій для користувача {user_id}")
    
    else:
        if current_state == "waiting_rating":
            await update.message.reply_text("Будь ласка, оцініть попередню рекомендацію числом від 1 до 10")
        elif current_state == "waiting_explanation":
            pass
        else:
            await update.message.reply_text("Напишіть /start, щоб почати знову")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда для перегляду статистики"""
    user_id = update.effective_user.id
    
    admin_ids = [980047923]
    
    if user_id not in admin_ids:
        await update.message.reply_text("У вас немає доступу до статистики")
        return
    
    try:
        if not restaurant_bot.summary_sheet:
            await update.message.reply_text("Статистика недоступна")
            return
        
        summary_data = restaurant_bot.summary_sheet.get_all_values()
        
        if len(summary_data) < 6:
            await update.message.reply_text("Недостатньо даних для статистики")
            return
        
        stats_text = f"""📊 <b>Статистика бота</b>

📈 Загальна кількість запитів: <b>{summary_data[1][1]}</b>
👥 Кількість унікальних користувачів: <b>{summary_data[2][1]}</b>
⭐ Середня оцінка відповідності: <b>{summary_data[3][1]}</b>
🔢 Кількість оцінок: <b>{summary_data[4][1]}</b>
📊 Середня кількість запитів на користувача: <b>{summary_data[5][1]}</b>

🕐 Останнє оновлення: {summary_data[1][2]}"""
        
        await update.message.reply_text(stats_text, parse_mode='HTML')
        
    except Exception as e:
        logger.error(f"Помилка отримання статистики: {e}")
        await update.message.reply_text("Помилка при отриманні статистики")

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
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        logger.info("✅ Telegram додаток створено успішно!")
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("stats", stats_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)
        
        logger.info("🔗 Підключаюся до Google Sheets...")
        loop.run_until_complete(restaurant_bot.init_google_sheets())
        
        logger.info("✅ Всі сервіси підключено! Бот готовий до роботи!")
        
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
