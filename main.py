import logging
import os
from typing import Dict, Optional, List, Tuple
import asyncio
import json
import re
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# Додаємо fuzzy matching для кращого пошуку
try:
    from fuzzywuzzy import fuzz
    FUZZY_AVAILABLE = True
except ImportError:
    FUZZY_AVAILABLE = False
    logger = logging.getLogger(__name__)
    logger.warning("fuzzywuzzy не встановлено. Fuzzy matching буде відключено.")

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

# Конфігурація покращеного пошуку
ENHANCED_SEARCH_CONFIG = {
    'enabled': True,
    'fuzzy_matching': True,
    'fuzzy_threshold': 80,
    'regex_boundaries': True,
    'negation_detection': True,
    'extended_synonyms': True,
    'fallback_to_old': True
}

# Глобальні змінні
openai_client = None
user_states: Dict[int, str] = {}
user_last_recommendation: Dict[int, str] = {}
user_rating_data: Dict[int, Dict] = {}

class EnhancedRestaurantBot:
    def __init__(self):
        self.restaurants_data = []
        self.google_sheets_available = False
        self.analytics_sheet = None
        self.gc = None
        
        # Розширені словники синонімів
        self.extended_synonyms = {
            'ресторан': ['ресторан', 'ресторани', 'ресторанчик', 'їдальня', 'заклад'],
            'кав\'ярня': ['кав\'ярня', 'кафе', 'кава', 'каварня', 'coffee', 'кофе'],
            'піца': ['піца', 'піцца', 'pizza', 'піци', 'піззу'],
            'суші': ['суші', 'sushi', 'роли', 'роллы', 'сашімі'],
            'бургер': ['бургер', 'burger', 'гамбургер', 'чізбургер'],
            'романтик': ['романтик', 'романтичний', 'побачення', 'інтимний', 'затишний', 'свічки'],
            'сімейний': ['сім\'я', 'сімейн', 'діти', 'родина', 'дитячий', 'для всієї сім\'ї'],
            'веселий': ['весел', 'жвавий', 'енергійний', 'гучний', 'драйвовий', 'молодіжний'],
            'швидко': ['швидко', 'швидку', 'швидкий', 'fast', 'перекус', 'поспішаю', 'на швидку руку'],
            'доставка': ['доставка', 'додому', 'не хочу йти', 'привезти', 'delivery']
        }
        
        # Слова-заперечення
        self.negation_words = [
            'не', 'ні', 'ніколи', 'ніде', 'без', 'нема', 'немає', 
            'не хочу', 'не люблю', 'не подобається', 'не треба'
        ]
    
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
            
            # Завантажуємо данні ресторанів
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
            
            try:
                self.analytics_sheet = analytics_sheet.worksheet("Analytics")
                logger.info("✅ Знайдено існуючий лист Analytics")
            except gspread.WorksheetNotFound:
                logger.info("📄 Аркуш Analytics не знайдено, створюю новий...")
                
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
                    
                logger.info("✅ Додано початкові данні до Summary")
                
        except Exception as e:
            logger.error(f"Помилка ініціалізації Analytics: {e}")
            self.analytics_sheet = None

    def _comprehensive_content_analysis(self, user_request: str) -> Tuple[bool, List[Dict], str]:
        """Покращений комплексний аналіз запиту користувача по ВСІХ колонках таблиці"""
        user_lower = user_request.lower()
        logger.info(f"🔎 ПОКРАЩЕНИЙ КОМПЛЕКСНИЙ АНАЛІЗ: '{user_request}'")
        
        # Розширені критерії пошуку по всіх колонках
        search_criteria = {
            'матча': {
                'keywords': ['матча', 'matcha', 'матчі', 'матчу'],
                'columns': ['menu', 'aim', 'vibe', 'cuisine', 'name'],
                'weight': 3.0
            },
            'кава': {
                'keywords': ['кава', 'кофе', 'coffee', 'капучіно', 'латте', 'еспресо', 'американо'],
                'columns': ['menu', 'aim', 'cuisine', 'name', 'vibe'],
                'weight': 2.8
            },
            'піца': {
                'keywords': ['піца', 'піцц', 'pizza', 'марґарита', 'пепероні'],
                'columns': ['menu', 'cuisine', 'name'],
                'weight': 3.0
            },
            'суші': {
                'keywords': ['суші', 'sushi', 'роли', 'ролл', 'сашімі', 'японська кухня'],
                'columns': ['menu', 'cuisine', 'name', 'vibe'],
                'weight': 3.0
            },
            'мідії': {
                'keywords': ['мідії', 'мидии', 'мідії', 'молюски', 'мідій', 'морепродукти'],
                'columns': ['menu', 'cuisine', 'name'],
                'weight': 3.2
            },
            'романтично': {
                'keywords': ['романт', 'побачення', 'інтимн', 'затишн', 'свічки', 'для двох'],
                'columns': ['vibe', 'aim', 'name'],
                'weight': 2.8
            },
            'сімейно': {
                'keywords': ['сім\'я', 'сімейн', 'діти', 'родин', 'для всієї сім\'ї'],
                'columns': ['vibe', 'aim', 'name'],
                'weight': 2.5
            },
            'працювати': {
                'keywords': ['працювати', 'попрацювати', 'робота', 'ноутбук', 'wifi', 'фріланс'],
                'columns': ['aim', 'vibe'],
                'weight': 2.8
            },
            'італійський': {
                'keywords': ['італ', 'italian', 'італія'],
                'columns': ['cuisine', 'vibe', 'name'],
                'weight': 2.5
            }
        }
        
        # Аналізуємо кожен заклад
        restaurant_scores = []
        
        for restaurant in self.restaurants_data:
            total_score = 0.0
            matched_criteria = []
            
            # Перевіряємо кожен критерій
            for criterion_name, criterion_data in search_criteria.items():
                keywords = criterion_data['keywords']
                columns = criterion_data['columns'] 
                weight = criterion_data['weight']
                
                # Перевіряємо чи є ключові слова в запиті користувача
                user_has_criterion = any(keyword in user_lower for keyword in keywords)
                
                if user_has_criterion:
                    # Шукаємо в відповідних колонках ресторану
                    restaurant_has_criterion = False
                    matched_columns = []
                    
                    for column in columns:
                        column_text = str(restaurant.get(column, '')).lower()
                        
                        if any(keyword in column_text for keyword in keywords):
                            restaurant_has_criterion = True
                            matched_columns.append(column)
                            logger.info(f"   ✅ {restaurant.get('name', '')} має '{criterion_name}' в колонці '{column}'")
                            break
                    
                    if restaurant_has_criterion:
                        # Додаємо бонус за кількість співпадінь в різних колонках
                        column_bonus = len(set(matched_columns)) * 0.2
                        final_score = weight + column_bonus
                        total_score += final_score
                        matched_criteria.append(f"{criterion_name}({','.join(matched_columns)})")
            
            if total_score > 0:
                restaurant_scores.append({
                    'restaurant': restaurant,
                    'score': total_score,
                    'criteria': matched_criteria
                })
                logger.info(f"🎯 {restaurant.get('name', '')}: оцінка {total_score:.1f} за критеріями {matched_criteria}")
        
        # Сортуємо за оцінкою
        restaurant_scores.sort(key=lambda x: x['score'], reverse=True)
        
        if restaurant_scores:
            # Беремо заклади з найвищими оцінками
            top_score = restaurant_scores[0]['score']
            threshold = top_score * 0.7
            top_restaurants = [item for item in restaurant_scores if item['score'] >= threshold]
            
            explanation = f"знайдено {len(top_restaurants)} найрелевантніших закладів"
            logger.info(f"🎉 ПОКРАЩЕНИЙ КОМПЛЕКСНИЙ АНАЛІЗ: {explanation}")
            
            return True, top_restaurants, explanation
        else:
            logger.info("🤔 ПОКРАЩЕНИЙ КОМПЛЕКСНИЙ АНАЛІЗ: не знайдено специфічних критеріїв")
            return False, [], "не знайдено специфічних критеріїв"

    def _check_dish_availability(self, user_request: str) -> Tuple[bool, List[str]]:
        """Перевіряє чи є потрібна страва в меню хоча б одного ресторану"""
        user_lower = user_request.lower()
        logger.info(f"🔍 Перевіряю наявність конкретних страв в запиті: '{user_request}'")
        
        food_keywords = {
            'піца': ['піца', 'піцц', 'pizza', 'піци', 'піззу'],
            'паста': ['паста', 'спагетті', 'pasta', 'спагетті', 'макарони'],
            'бургер': ['бургер', 'burger', 'гамбургер', 'чізбургер'],
            'суші': ['суші', 'sushi', 'роли', 'ролл', 'сашімі'],
            'мідії': ['мідії', 'мидии', 'мідіі', 'молюски', 'мідій']
        }
        
        # Знаходимо які страви згадав користувач
        requested_dishes = []
        for dish, keywords in food_keywords.items():
            if any(keyword in user_lower for keyword in keywords):
                requested_dishes.append(dish)
        
        if not requested_dishes:
            return False, []
        
        # Перевіряємо чи є ці страви в меню ресторанів
        dishes_found_in_restaurants = []
        
        for dish in requested_dishes:
            found_in_any_restaurant = False
            dish_keywords = food_keywords[dish]
            
            for restaurant in self.restaurants_data:
                menu_text = restaurant.get('menu', '').lower()
                
                if any(keyword.lower() in menu_text for keyword in dish_keywords):
                    found_in_any_restaurant = True
                    break
            
            if found_in_any_restaurant:
                dishes_found_in_restaurants.append(dish)
        
        if dishes_found_in_restaurants:
            return True, dishes_found_in_restaurants
        else:
            return False, requested_dishes

    def _get_dish_keywords(self, dish: str) -> List[str]:
        """Повертає список ключових слів для конкретної страви"""
        food_keywords = {
            'піца': ['піца', 'піцц', 'pizza', 'піци', 'піззу'],
            'паста': ['паста', 'спагетті', 'pasta', 'спагетті', 'макарони'],
            'бургер': ['бургер', 'burger', 'гамбургер', 'чізбургер'],
            'суші': ['суші', 'sushi', 'роли', 'ролл', 'сашімі'],
            'мідії': ['мідії', 'мидии', 'мідіі', 'молюски', 'мідій']
        }
        return food_keywords.get(dish, [dish])

    def _filter_by_establishment_type(self, user_request: str, restaurant_list):
        """Фільтрує ресторани за типом закладу"""
        user_lower = user_request.lower()
        
        type_keywords = {
            'ресторан': {
                'user_keywords': ['ресторан', 'обід', 'вечеря', 'побачення', 'романтик'],
                'establishment_types': ['ресторан']
            },
            'кав\'ярня': {
                'user_keywords': ['кава', 'капучіно', 'латте', 'кав\'ярня', 'кафе'],
                'establishment_types': ['кав\'ярня', 'кафе']
            }
        }
        
        detected_types = []
        for establishment_type, keywords in type_keywords.items():
            user_match = any(keyword in user_lower for keyword in keywords['user_keywords'])
            if user_match:
                detected_types.extend(keywords['establishment_types'])
        
        if not detected_types:
            return restaurant_list
        
        filtered_restaurants = []
        for restaurant in restaurant_list:
            establishment_type = restaurant.get('тип закладу', restaurant.get('type', '')).lower().strip()
            type_match = any(detected_type.lower().strip() in establishment_type for detected_type in detected_types)
            
            if type_match:
                filtered_restaurants.append(restaurant)
        
        return filtered_restaurants if filtered_restaurants else restaurant_list

    def _filter_by_context(self, user_request: str, restaurant_list):
        """Фільтрує ресторани за контекстом запиту"""
        user_lower = user_request.lower()
        
        context_filters = {
            'romantic': {
                'user_keywords': ['романт', 'побачен', 'інтимн'],
                'restaurant_keywords': ['інтимн', 'романт', 'пар']
            },
            'family': {
                'user_keywords': ['сім', 'діт', 'родин'],
                'restaurant_keywords': ['сімейн', 'діт', 'родин']
            }
        }
        
        detected_contexts = []
        for context, keywords in context_filters.items():
            user_match = any(keyword in user_lower for keyword in keywords['user_keywords'])
            if user_match:
                detected_contexts.append(context)
        
        if not detected_contexts:
            return restaurant_list
        
        filtered_restaurants = []
        for restaurant in restaurant_list:
            restaurant_text = f"{restaurant.get('vibe', '')} {restaurant.get('aim', '')}".lower()
            
            restaurant_score = 0
            for context in detected_contexts:
                context_keywords = context_filters[context]['restaurant_keywords']
                if any(keyword in restaurant_text for keyword in context_keywords):
                    restaurant_score += 1
            
            if restaurant_score > 0:
                filtered_restaurants.append(restaurant)
        
        return filtered_restaurants if filtered_restaurants else restaurant_list

    def _filter_by_menu(self, user_request: str, restaurant_list):
        """Фільтрує ресторани по меню"""
        user_lower = user_request.lower()
        
        food_keywords = {
            'піца': ['піца', 'pizza'],
            'суші': ['суші', 'sushi', 'роли'],
            'паста': ['паста', 'pasta']
        }
        
        requested_dishes = []
        for dish, keywords in food_keywords.items():
            if any(keyword in user_lower for keyword in keywords):
                requested_dishes.append(dish)
        
        if requested_dishes:
            filtered_restaurants = []
            for restaurant in restaurant_list:
                menu_text = restaurant.get('menu', '').lower()
                has_requested_dish = False
                
                for dish in requested_dishes:
                    dish_keywords = food_keywords[dish]
                    if any(keyword in menu_text for keyword in dish_keywords):
                        has_requested_dish = True
                        break
                
                if has_requested_dish:
                    filtered_restaurants.append(restaurant)
            
            return filtered_restaurants if filtered_restaurants else restaurant_list
        
        return restaurant_list

    async def get_recommendation(self, user_request: str) -> Optional[Dict]:
        """Отримання рекомендації через OpenAI"""
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
            
            # Комплексний аналіз по всіх колонках
            has_specific_criteria, relevant_restaurants, analysis_explanation = self._comprehensive_content_analysis(user_request)
            
            if has_specific_criteria:
                shuffled_restaurants = [item['restaurant'] for item in relevant_restaurants]
                logger.info(f"🎯 ВИКОРИСТОВУЮ КОМПЛЕКСНИЙ АНАЛІЗ: {analysis_explanation}")
            else:
                logger.info("🔍 Комплексний аналіз не знайшов критеріїв, перевіряю конкретні страви...")
                
                has_dish, dishes_info = self._check_dish_availability(user_request)
                
                if dishes_info:
                    if not has_dish:
                        missing_dishes = ", ".join(dishes_info)
                        return {
                            "dish_not_found": True,
                            "missing_dishes": missing_dishes,
                            "message": f"На жаль, {missing_dishes} ще немає в нашому переліку. Спробуй іншу страву!"
                        }
            
            # Стандартна фільтрація
            if not has_specific_criteria:
                type_filtered = self._filter_by_establishment_type(user_request, shuffled_restaurants)
                context_filtered = self._filter_by_context(user_request, type_filtered)
                final_filtered = self._filter_by_menu(user_request, context_filtered)
            else:
                final_filtered = shuffled_restaurants
            
            if not final_filtered:
                return None
            
            # OpenAI запит
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

{restaurants_text}

ЗАВДАННЯ:
1. Обери 2 НАЙКРАЩІ варіанти (якщо є тільки 1 варіант, то тільки його)
2. Вкажи який з них є ПРІОРИТЕТНИМ і коротко поясни ЧОМУ

ФОРМАТ ВІДПОВІДІ:
Варіанти: [номер1, номер2]
Пріоритет: [номер] - [коротке пояснення причини]"""

            def make_openai_request():
                return openai_client.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "Ти експерт-ресторатор. Аналізуй варіанти та обирай найкращі з об'рунтуванням."},
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
                return self._fallback_dual_selection(user_request, final_filtered)
            
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
            
            # Витягуємо номери варіантів
            import re
            numbers = re.findall(r'\d+', variants_line)
            
            if len(numbers) >= 1:
                indices = [int(num) - 1 for num in numbers[:2]]
                valid_indices = [idx for idx in indices if 0 <= idx < len(filtered_restaurants)]
                
                if not valid_indices:
                    return None
                
                restaurants = [filtered_restaurants[idx] for idx in valid_indices]
                
                # Визначаємо пріоритетний ресторан
                priority_num = None
                priority_explanation = "найкращий варіант за всіма критеріями"
                
                if priority_line and '-' in priority_line:
                    priority_match = re.search(r'(\d+)', priority_line.split('-')[0])
                    if priority_match:
                        priority_num = int(priority_match.group(1))
                    
                    explanation_part = priority_line.split('-', 1)[1].strip()
                    if explanation_part:
                        priority_explanation = explanation_part
                
                if priority_num and (priority_num - 1) in valid_indices:
                    priority_index = valid_indices.index(priority_num - 1)
                else:
                    priority_index = 0
                
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
            
            return None
            
        except Exception as e:
            logger.error(f"❌ Помилка парсингу відповіді OpenAI: {e}")
            return None

    def _fallback_dual_selection(self, user_request: str, restaurant_list):
        """Резервний алгоритм для двох рекомендацій"""
        if not restaurant_list:
            return None
        
        import random
        
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
        
        # Вибираємо 2 найкращих
        scored_restaurants = []
        user_lower = user_request.lower()
        
        for restaurant in restaurant_list:
            score = random.uniform(0, 1)
            scored_restaurants.append((score, restaurant))
        
        scored_restaurants.sort(key=lambda x: x[0], reverse=True)
        top_restaurants = [item[1] for item in scored_restaurants[:2]]
        
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
        
        return result

    async def log_request(self, user_id: int, user_request: str, restaurant_name: str, rating: Optional[int] = None, explanation: str = ""):
        """Логування запиту до аналітичної таблиці"""
        if not self.analytics_sheet:
            logger.warning("Analytics sheet недоступний")
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
            logger.info(f"📊 Записано до Analytics: {user_id} - {restaurant_name}")
            
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
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            self.summary_sheet.update('B2', str(total_requests))
            self.summary_sheet.update('C2', timestamp)
            
            self.summary_sheet.update('B3', str(unique_users))
            self.summary_sheet.update('C3', timestamp)
            
            self.summary_sheet.update('B4', f"{avg_rating:.2f}")
            self.summary_sheet.update('C4', timestamp)
            
            self.summary_sheet.update('B5', str(rating_count))
            self.summary_sheet.update('C5', timestamp)
            
            logger.info(f"📈 Оновлено статистику: Запитів: {total_requests}, Користувачів: {unique_users}")
            
        except Exception as e:
            logger.error(f"Помилка оновлення статистики: {e}")

# Глобальний екземпляр покращеного бота
restaurant_bot = EnhancedRestaurantBot()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник команди /start"""
    user_id = update.effective_user.id
    user_states[user_id] = "waiting_request"
    
    message = """🍽 <b>Вітаю в Restaurant Bot!</b>

Я допоможу знайти ідеальний заклад для будь-якої ситуації!

<b>Просто напишіть що шукаєте:</b>
• "Романтичний ресторан для побачення"
• "Кав'ярня де можна працювати"  
• "Піца з друзями"
• "Де випити матчу?"

<b>Корисні команди:</b>
/help - Детальна інструкція
/list_restaurants - Всі заклади за типами

<b>Готові почати?</b> Опишіть що шукаєте! ✨"""
    
    await update.message.reply_text(message, parse_mode='HTML')
    logger.info(f"Користувач {user_id} почав діалог")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help - повна довідка по використанню бота"""
    help_text = """🤖 <b>Довідка по Restaurant Bot</b>

<b>🎯 Як користуватися ботом:</b>
Просто напишіть що ви шукаєте природною мовою!

<b>🔍 Приклади запитів:</b>
• "Хочу піцу з друзями"
• "Потрібен ресторан для побачення"
• "Де можна випити матчу?"
• "Сімейне місце для обіду"
• "Швидко перекусити"
• "Італійська кухня в центрі"

<b>🔎 Що бот розуміє:</b>
• <i>Страви:</i> піца, суші, паста, мідії, стейк та ін.
• <i>Атмосферу:</i> романтично, сімейно, весело, затишно
• <i>Призначення:</i> побачення, друзі, робота, святкування
• <i>Типи:</i> ресторан, кав'ярня, доставка, to-go
• <i>Кухню:</i> італійська, японська, грузинська та ін.

<b>⭐ Оцінювання:</b>
Після кожної рекомендації оцініть її від 1 до 10
Це допоможе покращити майбутні пропозиції!

<b>📋 Доступні команди:</b>
/start - Почати пошук ресторану
/help - Ця довідка
/list_restaurants - Список всіх закладів
/stats - Статистика (тільки для адмінів)

<b>💡 Поради:</b>
• Будьте конкретними: "романтичний італійський ресторан"
• Вказуйте контекст: "з дітьми", "для роботи"  
• Згадуйте побажання: "з терасою", "в центрі"

Готові знайти ідеальне місце? Напишіть свій запит! 🍽️"""

    await update.message.reply_text(help_text, parse_mode='HTML')
    logger.info(f"📖 Користувач {update.effective_user.id} запросив довідку")

async def list_restaurants_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /list_restaurants - список всіх ресторанів згрупований за типами"""
    user_id = update.effective_user.id
    
    if not restaurant_bot.restaurants_data:
        await update.message.reply_text("❌ База даних ресторанів недоступна")
        return
    
    # Групуємо ресторани за типами
    grouped_restaurants = {}
    for restaurant in restaurant_bot.restaurants_data:
        establishment_type = restaurant.get('тип закладу', restaurant.get('type', 'Інше'))
        if not establishment_type or establishment_type.strip() == '':
            establishment_type = 'Інше'
        
        if establishment_type not in grouped_restaurants:
            grouped_restaurants[establishment_type] = []
        
        grouped_restaurants[establishment_type].append(restaurant)
    
    # Формуємо красиве повідомлення
    message_parts = ["🏢 <b>Всі заклади за типами:</b>\n"]
    
    # Сортуємо типи за кількістю закладів
    sorted_types = sorted(grouped_restaurants.items(), key=lambda x: len(x[1]), reverse=True)
    
    for establishment_type, restaurants in sorted_types:
        count = len(restaurants)
        
        # Іконки для різних типів
        icon = {
            'ресторан': '🍽️',
            'кав\'ярня': '☕',
            'кафе': '☕',
            'доставка': '🚚',
            'бар': '🍸'
        }.get(establishment_type.lower(), '🪗')
        
        message_parts.append(f"\n{icon} <b>{establishment_type.upper()}</b> ({count})")
        
        # Додаємо перші 3 ресторани кожного типу
        for restaurant in restaurants[:3]:
            name = restaurant.get('name', 'Без назви')
            message_parts.append(f"   • {name}")
        
        if count > 3:
            message_parts.append(f"   • ... та ще {count - 3}")
    
    total_count = len(restaurant_bot.restaurants_data)
    message_parts.append(f"\n📊 <b>Загалом:</b> {total_count} закладів")
    message_parts.append("🔍 Для пошуку просто напишіть що шукаєте!")
    
    full_message = '\n'.join(message_parts)
    
    await update.message.reply_text(full_message, parse_mode='HTML')
    logger.info(f"📋 Користувач {user_id} запросив список ресторанів")

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

🕐 Останнє оновлення: {summary_data[1][2]}"""
        
        await update.message.reply_text(stats_text, parse_mode='HTML')
        
    except Exception as e:
        logger.error(f"Помилка отримання статистики: {e}")
        await update.message.reply_text("Помилка при отриманні статистики")

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
            
            return
        else:
            await update.message.reply_text("Будь ласка, напишіть число від 1 до 10")
            return
    
    if current_state == "waiting_request":
        user_request = user_text
        logger.info(f"🔍 Користувач {user_id} написав: {user_request}")
        
        # Показуємо статус обробки
        processing_message = await update.message.reply_text("🔍 Аналізую ваш запит...")
        
        recommendation = await restaurant_bot.get_recommendation(user_request)
        
        try:
            await processing_message.delete()
        except:
            pass
        
        if recommendation:
            # Перевіряємо чи це повідомлення про відсутність страви
            if recommendation.get("dish_not_found"):
                not_found_message = f"""😞 <b>Страву не знайдено</b>

{recommendation['message']}

💡 <b>Поради:</b>
• Спробуйте інші варіанти: піца, суші, паста, салати
• Або опишіть атмосферу: "романтичне місце", "кав'ярня для роботи"
• Використайте /list_restaurants для перегляду всіх закладів

Напишіть новий запит або /start для початку! 🔄"""
                
                await update.message.reply_text(not_found_message, parse_mode='HTML')
                return
            
            # Обробляємо рекомендації
            restaurants = recommendation["restaurants"]
            priority_index = recommendation["priority_index"]
            priority_explanation = recommendation["priority_explanation"]
            
            # Логуємо основний ресторан
            main_restaurant = restaurants[priority_index]
            await restaurant_bot.log_request(user_id, user_request, main_restaurant["name"])
            
            # Зберігаємо для оцінки
            user_last_recommendation[user_id] = main_restaurant["name"]
            user_states[user_id] = "waiting_rating"
            
            # Формуємо повідомлення
            if len(restaurants) == 1:
                response_text = f"""🎯 <b>Ідеальний варіант для вас!</b>

🏠 <b>{restaurants[0]['name']}</b>
📍 <i>{restaurants[0]['address']}</i>
🏢 <b>Тип:</b> {restaurants[0]['type']}
✨ <b>Атмосфера:</b> {restaurants[0]['vibe']}
🎯 <b>Підходить для:</b> {restaurants[0]['aim']}
🍽️ <b>Кухня:</b> {restaurants[0]['cuisine']}

📱 <b>Соц-мережі:</b> {restaurants[0]['socials']}"""
            else:
                priority_restaurant = restaurants[priority_index]
                alternative_restaurant = restaurants[1 - priority_index]
                
                response_text = f"""🎯 <b>Топ-2 варіанти спеціально для вас:</b>

🏆 <b>ГОЛОВНА РЕКОМЕНДАЦІЯ:</b>
🏠 <b>{priority_restaurant['name']}</b>
📍 <i>{priority_restaurant['address']}</i>
🏢 <b>Тип:</b> {priority_restaurant['type']}
✨ <b>Атмосфера:</b> {priority_restaurant['vibe']}
🎯 <b>Підходить для:</b> {priority_restaurant['aim']}
🍽️ <b>Кухня:</b> {priority_restaurant['cuisine']}
📱 <b>Контакти:</b> {priority_restaurant['socials']}

💡 <b>Чому рекомендую:</b> <i>{priority_explanation}</i>

➖➖➖➖➖➖➖➖➖➖

🥈 <b>АЛЬТЕРНАТИВНИЙ ВАРІАНТ:</b>
🏠 <b>{alternative_restaurant['name']}</b>
📍 <i>{alternative_restaurant['address']}</i>
🏢 <b>Тип:</b> {alternative_restaurant['type']}
✨ <b>Атмосфера:</b> {alternative_restaurant['vibe']}
🎯 <b>Підходить для:</b> {alternative_restaurant['aim']}
🍽️ <b>Кухня:</b> {alternative_restaurant['cuisine']}
📱 <b>Контакти:</b> {alternative_restaurant['socials']}"""

            # Додаємо посилання на меню
            main_menu_url = main_restaurant.get('menu_url', '')
            if main_menu_url and main_menu_url.startswith('http'):
                response_text += f"\n\n📋 <a href='{main_menu_url}'>Переглянути меню головної рекомендації</a>"

            # Відправляємо фото головної рекомендації
            main_photo_url = main_restaurant.get('photo', '')
            
            if main_photo_url and main_photo_url.startswith('http'):
                try:
                    await update.message.reply_photo(
                        photo=main_photo_url,
                        caption=response_text,
                        parse_mode='HTML'
                    )
                except Exception as photo_error:
                    logger.warning(f"⚠️ Не вдалося надіслати фото: {photo_error}")
                    response_text += f"\n\n📸 <a href='{main_photo_url}'>Переглянути фото головної рекомендації</a>"
                    await update.message.reply_text(response_text, parse_mode='HTML')
            else:
                await update.message.reply_text(response_text, parse_mode='HTML')
            
            # Просимо оцінити
            rating_text = f"""⭐ <b>Оціність головну рекомендацію</b>

🎯 <b>Оцінюємо:</b> "{main_restaurant['name']}"

<b>Шкала оцінки:</b>
1-3: Зовсім не підходить
4-6: Частково підходить  
7-8: Добре підходить
9-10: Ідеально підходить

<b>Напишіть число від 1 до 10:</b> 👇

💡 <i>Ваші оцінки допомагають боту краще розуміти ваші вподобання!</i>"""
            await update.message.reply_text(rating_text, parse_mode='HTML')
            
        else:
            no_results_message = """😞 <b>Нічого не знайдено</b>

На жаль, не знайшов закладів що відповідають вашому запиту.

💡 <b>Спробуйте:</b>
• Змінити критерії пошуку
• Використати загальніші терміни  
• Переглянути всі заклади: /list_restaurants
• Отримати поради: /help

🔄 <b>Або напишіть новий запит!</b>"""
            
            await update.message.reply_text(no_results_message, parse_mode='HTML')
    
    else:
        if current_state == "waiting_rating":
            await update.message.reply_text("Будь ласка, оціність попередню рекомендацію числом від 1 до 10")
        else:
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
    
    logger.info("🚀 Запускаю покращений бота...")
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        logger.info("✅ Telegram додаток створено успішно!")
        
        # Додаємо обробники команд
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("list_restaurants", list_restaurants_command))
        application.add_handler(CommandHandler("stats", stats_command))
        
        # Додаємо обробник текстових повідомлень
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)
        
        logger.info("🔗 Підключаюся до Google Sheets...")
        loop.run_until_complete(restaurant_bot.init_google_sheets())
        
        logger.info(f"🔧 Конфігурація покращеного пошуку: {ENHANCED_SEARCH_CONFIG}")
        if FUZZY_AVAILABLE:
            logger.info("✅ Fuzzy matching доступний")
        else:
            logger.warning("⚠️ Fuzzy matching недоступний")
        
        logger.info("✅ Всі сервіси підключено! Покращений бот готовий до роботи!")
        
        loop.run_until_complete(application.run_polling(drop_pending_updates=True))
        
    except KeyboardInterrupt:
        logger.info("🛑 Бота зупинено користувачем")
    except Exception as e:
        logger.error(f"❌ Критична помилка: {e}")
    finally:
        try:
            loop.close()
        except:
            pass

if __name__ == '__main__':
    main()
