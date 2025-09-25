import logging
import os
from typing import Dict, Optional, List, Tuple
import asyncio
import json
import re
from datetime import datetime
import time
import hashlib
from enum import Enum

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

# Налаштування логування з структурованими повідомленнями
class StructuredLogger:
    def __init__(self, name: str):
        self.logger = logging.getLogger(name)
        
    def log_filtering_step(self, step: str, user_id: int, input_count: int, output_count: int, duration: float, details: dict = None):
        """Структуроване логування етапів фільтрації"""
        log_data = {
            'step': step,
            'user_id': user_id,
            'input_count': input_count,
            'output_count': output_count,
            'duration_ms': round(duration * 1000, 2),
            'reduction_ratio': round((input_count - output_count) / input_count * 100, 1) if input_count > 0 else 0,
            'details': details or {}
        }
        self.logger.info(f"FILTERING_STEP: {json.dumps(log_data)}")
        
    def log_ab_test(self, user_id: int, test_variant: str, request: str, results: dict):
        """Логування A/B тестування"""
        ab_data = {
            'user_id': user_id,
            'test_variant': test_variant,
            'request': request[:100],
            'results': results,
            'timestamp': datetime.now().isoformat()
        }
        self.logger.info(f"AB_TEST: {json.dumps(ab_data)}")

# Глобальний структурований логер
structured_logger = StructuredLogger(__name__)

# Стандартне логування
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('restaurant_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# A/B тестування конфігурація
class FilteringStrategy(Enum):
    OLD_LOGIC = "old_logic"
    NEW_LOGIC = "new_logic"

class ABTestConfig:
    """Конфігурація A/B тестування"""
    def __init__(self):
        self.test_ratio = 0.5  # 50% користувачів отримують нову логіку
        self.force_strategy = os.getenv('FORCE_FILTERING_STRATEGY', None)
        
    def get_strategy_for_user(self, user_id: int) -> FilteringStrategy:
        """Визначає стратегію фільтрації для користувача"""
        if self.force_strategy:
            try:
                return FilteringStrategy(self.force_strategy)
            except ValueError:
                logger.warning(f"Invalid FORCE_FILTERING_STRATEGY: {self.force_strategy}")
        
        # Консистентний розподіл на основі hash user_id
        user_hash = int(hashlib.md5(str(user_id).encode()).hexdigest(), 16)
        return FilteringStrategy.NEW_LOGIC if (user_hash % 100) < (self.test_ratio * 100) else FilteringStrategy.OLD_LOGIC

# Конфігурація
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
ab_test_config = ABTestConfig()

class RestaurantDataValidator:
    """Валідатор даних ресторанів"""
    
    REQUIRED_FIELDS = ['name']
    OPTIONAL_FIELDS = ['address', 'type', 'тип закладу', 'vibe', 'aim', 'cuisine', 'menu', 'socials', 'photo', 'menu_url']
    
    @classmethod
    def validate_restaurant(cls, restaurant: dict) -> bool:
        """Валідує дані ресторану"""
        if not isinstance(restaurant, dict):
            return False
            
        for field in cls.REQUIRED_FIELDS:
            if not restaurant.get(field) or not str(restaurant.get(field, '')).strip():
                logger.warning(f"Restaurant missing required field '{field}': {restaurant}")
                return False
        
        return True
    
    @classmethod  
    def clean_restaurant_data(cls, restaurant: dict) -> dict:
        """Очищає та нормалізує дані ресторану"""
        cleaned = {}
        
        for field in cls.REQUIRED_FIELDS + cls.OPTIONAL_FIELDS:
            value = restaurant.get(field, '')
            if isinstance(value, str):
                value = value.strip()
            cleaned[field] = value or ''  # Гарантуємо що немає None
        
        # Нормалізуємо тип закладу
        establishment_type = cleaned.get('тип закладу') or cleaned.get('type', '')
        cleaned['normalized_type'] = str(establishment_type).lower().strip()
        
        return cleaned

class EnhancedRestaurantBot:
    def __init__(self):
        self.restaurants_data = []
        self.google_sheets_available = False
        self.analytics_sheet = None
        self.summary_sheet = None
        self.gc = None
        self.validator = RestaurantDataValidator()
        
        # Словники для пошуку конкретних страв (використовується тільки тут)
        self.dish_synonyms = {
            'піца': ['піца', 'піцца', 'pizza'],
            'суші': ['суші', 'sushi', 'роли', 'ролли'],
            'паста': ['паста', 'спагеті', 'pasta'],
            'бургер': ['бургер', 'burger', 'гамбургер'],
            'мідії': ['мідії', 'мидии', 'мідіі', 'молюски'],
            'стейк': ['стейк', 'steak', 'м\'ясо', 'біфштекс'],
            'матча': ['матча', 'matcha', 'матчі']
        }
        
        # Словники для загальної фільтрації меню (ширший список)
        self.menu_keywords = {
            'піца': ['піц', 'pizza'],
            'паста': ['паст', 'спагеті', 'pasta'],
            'бургер': ['бургер', 'burger'],
            'суші': ['суші', 'sushi', 'рол'],
            'салат': ['салат', 'salad'],
            'стейк': ['стейк', 'steak', 'м\'ясо'],
            'риба': ['риб', 'fish', 'лосось'],
            'десерт': ['десерт', 'торт', 'тірамісу'],
            'мідії': ['мідії', 'мидии', 'молюск'],
            'матча': ['матча', 'matcha']
        }
    
    def _convert_google_drive_url(self, url: str) -> str:
        """Безпечне перетворення Google Drive посилань"""
        if not url or not isinstance(url, str) or 'drive.google.com' not in url:
            return url
        
        try:
            match = re.search(r'/file/d/([a-zA-Z0-9-_]+)', url)
            if match:
                file_id = match.group(1)
                direct_url = f"https://drive.google.com/uc?export=view&id={file_id}"
                logger.info(f"Перетворено Google Drive посилання: {url} → {direct_url}")
                return direct_url
        except Exception as e:
            logger.warning(f"Помилка перетворення Google Drive URL: {e}")
        
        return url
    
    async def init_google_sheets(self):
        """Ініціалізація підключення до Google Sheets з повною валідацією"""
        if not GOOGLE_CREDENTIALS_JSON or not GOOGLE_SHEET_URL:
            logger.error("Google Sheets credentials не налаштовано")
            return False
            
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
                valid_restaurants = []
                for restaurant in records:
                    if self.validator.validate_restaurant(restaurant):
                        cleaned = self.validator.clean_restaurant_data(restaurant)
                        valid_restaurants.append(cleaned)
                    else:
                        logger.warning(f"Пропущено невалідний ресторан: {restaurant.get('name', 'Unknown')}")
                
                self.restaurants_data = valid_restaurants
                self.google_sheets_available = True
                logger.info(f"Завантажено {len(self.restaurants_data)} валідних закладів з {len(records)} записів")
            else:
                logger.warning("Google Sheets порожній")
                return False
            
            await self.init_analytics_sheet()
            return True
                
        except Exception as e:
            logger.error(f"Помилка Google Sheets: {type(e).__name__}: {str(e)}")
            return False
    
    async def init_analytics_sheet(self):
        """Ініціалізація аналітичної таблиці"""
        try:
            analytics_sheet = self.gc.open_by_url(ANALYTICS_SHEET_URL)
            
            # Analytics аркуш
            try:
                self.analytics_sheet = analytics_sheet.worksheet("Analytics")
                logger.info("Знайдено існуючий лист Analytics")
            except gspread.WorksheetNotFound:
                self.analytics_sheet = analytics_sheet.add_worksheet(title="Analytics", rows="1000", cols="15")
                headers = [
                    "Timestamp", "User ID", "User Request", "Restaurant Name", 
                    "Rating", "Rating Explanation", "Date", "Time",
                    "Filtering Strategy", "Processing Time", "Steps Count", "AB Test Data"
                ]
                self.analytics_sheet.append_row(headers)
                logger.info("Створено новий лист Analytics")
            
            # Summary аркуш
            try:
                self.summary_sheet = analytics_sheet.worksheet("Summary")
            except gspread.WorksheetNotFound:
                self.summary_sheet = analytics_sheet.add_worksheet(title="Summary", rows="100", cols="5")
                summary_data = [
                    ["Метрика", "Значення", "Останнє оновлення"],
                    ["Загальна кількість запитів", "0", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                    ["Кількість унікальних користувачів", "0", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                    ["Середня оцінка відповідності", "0", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                    ["Кількість оцінок", "0", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
                    ["Нова логіка vs Стара (%)", "0", datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
                ]
                for row in summary_data:
                    self.summary_sheet.append_row(row)
                logger.info("Створено новий лист Summary")
            
        except Exception as e:
            logger.error(f"Помилка ініціалізації Analytics: {e}")
            self.analytics_sheet = None
            self.summary_sheet = None

    def _detect_specific_dishes(self, user_request: str) -> Tuple[bool, List[str], str]:
        """
        Точне виявлення КОНКРЕТНИХ страв (не загальна фільтрація меню)
        Використовується тільки для випадків коли користувач шукає конкретну страву
        """
        if not user_request or not isinstance(user_request, str):
            return False, [], "порожній запит"
            
        user_lower = user_request.strip().lower()
        logger.info(f"Перевіряю конкретні страви в: '{user_request}'")
        
        found_dishes = set()
        
        for dish, synonyms in self.dish_synonyms.items():
            for synonym in synonyms:
                pattern = r'\b' + re.escape(synonym.lower()) + r'\b'
                if re.search(pattern, user_lower):
                    found_dishes.add(dish)
                    logger.info(f"Знайдено конкретну страву '{dish}' через синонім '{synonym}'")
                    break
        
        found_dishes_list = list(found_dishes)
        
        if not found_dishes_list:
            return False, [], "конкретні страви не знайдені"
        
        # Перевіряємо доступність у меню
        available_dishes = []
        for dish in found_dishes_list:
            synonyms = self.dish_synonyms[dish]
            dish_available = False
            
            for restaurant in self.restaurants_data:
                menu_text = str(restaurant.get('menu', '')).lower()
                if any(synonym.lower() in menu_text for synonym in synonyms):
                    dish_available = True
                    break
            
            if dish_available:
                available_dishes.append(dish)
                
        if available_dishes:
            explanation = f"знайдено конкретні страви: {', '.join(available_dishes)}"
            return True, available_dishes, explanation
        else:
            explanation = f"страви {', '.join(found_dishes_list)} відсутні в меню"
            return False, found_dishes_list, explanation

    def _filter_by_establishment_type_old(self, user_request: str, restaurant_list: List) -> List:
        """СТАРА ЛОГІКА фільтрації за типом закладу"""
        start_time = time.time()
        user_lower = user_request.lower()
        
        simple_patterns = {
            'ресторан': ['ресторан', 'ресторани'],
            'кав\'ярня': ['кав\'ярня', 'кафе', 'coffee']
        }
        
        detected_type = None
        for establishment_type, keywords in simple_patterns.items():
            if any(keyword in user_lower for keyword in keywords):
                detected_type = establishment_type
                break
        
        if not detected_type:
            duration = time.time() - start_time
            structured_logger.log_filtering_step("OLD_TYPE_FILTER", 0, len(restaurant_list), len(restaurant_list), duration, {"detected_type": None})
            return restaurant_list
        
        filtered = []
        for restaurant in restaurant_list:
            rest_type = restaurant.get('normalized_type', '')
            if detected_type.lower() in rest_type or rest_type in detected_type.lower():
                filtered.append(restaurant)
        
        duration = time.time() - start_time
        structured_logger.log_filtering_step("OLD_TYPE_FILTER", 0, len(restaurant_list), len(filtered), duration, {"detected_type": detected_type})
        
        return filtered if filtered else restaurant_list

    def _filter_by_establishment_type_new(self, user_request: str, restaurant_list: List) -> List:
        """НОВА ЛОГІКА фільтрації за типом закладу з покращеною точністю"""
        start_time = time.time()
        user_lower = user_request.lower()
        
        type_patterns = {
            'ресторан': {'keywords': ['ресторан', 'ресторани', 'ресторанчик'], 'weight': 3.0},
            'кав\'ярня': {'keywords': ['кав\'ярня', 'кафе', 'coffee'], 'weight': 3.0},
            'піцерія': {'keywords': ['піца', 'піцца', 'pizza'], 'weight': 2.8},
            'to-go': {'keywords': ['швидко', 'на винос', 'перекус'], 'weight': 2.5},
            'доставка': {'keywords': ['доставка', 'додому', 'привезти'], 'weight': 2.5}
        }
        
        detected_types = []
        best_weight = 0
        
        for type_name, config in type_patterns.items():
            for keyword in config['keywords']:
                pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
                if re.search(pattern, user_lower):
                    weight = config['weight']
                    if weight > best_weight:
                        best_weight = weight
                        detected_types = [type_name]
                    elif weight == best_weight and type_name not in detected_types:
                        detected_types.append(type_name)
                    break
        
        if not detected_types:
            duration = time.time() - start_time
            structured_logger.log_filtering_step("NEW_TYPE_FILTER", 0, len(restaurant_list), len(restaurant_list), duration, {"detected_types": []})
            return restaurant_list
        
        filtered = []
        for restaurant in restaurant_list:
            rest_type = restaurant.get('normalized_type', '')
            type_match = any(
                detected_type.lower() in rest_type or rest_type in detected_type.lower()
                for detected_type in detected_types
            )
            if type_match:
                filtered.append(restaurant)
        
        duration = time.time() - start_time
        structured_logger.log_filtering_step("NEW_TYPE_FILTER", 0, len(restaurant_list), len(filtered), duration, {"detected_types": detected_types, "best_weight": best_weight})
        
        return filtered if filtered else restaurant_list

    def _filter_by_context(self, user_request: str, restaurant_list: List) -> List:
        """Фільтрація за контекстом (vibe, aim)"""
        start_time = time.time()
        user_lower = user_request.lower()
        
        context_filters = {
            'romantic': {
                'user_keywords': ['романт', 'побачен', 'двох', 'інтимн', 'затишн'],
                'restaurant_keywords': ['інтимн', 'романт', 'для пар', 'камерн']
            },
            'family': {
                'user_keywords': ['сім', 'діт', 'родин', 'батьк', 'мам'],
                'restaurant_keywords': ['сімейн', 'діт', 'родин']
            },
            'business': {
                'user_keywords': ['діл', 'зустріч', 'перегов', 'бізнес', 'робоч'],
                'restaurant_keywords': ['діл', 'зустріч', 'бізнес']
            },
            'friends': {
                'user_keywords': ['друз', 'компан', 'гуртом', 'весел'],
                'restaurant_keywords': ['компан', 'друз', 'молодіжн', 'весел']
            }
        }
        
        detected_contexts = []
        for context, keywords in context_filters.items():
            if any(keyword in user_lower for keyword in keywords['user_keywords']):
                detected_contexts.append(context)
        
        if not detected_contexts:
            duration = time.time() - start_time
            structured_logger.log_filtering_step("CONTEXT_FILTER", 0, len(restaurant_list), len(restaurant_list), duration, {"detected_contexts": []})
            return restaurant_list
        
        scored_restaurants = []
        for restaurant in restaurant_list:
            restaurant_text = f"{restaurant.get('vibe', '')} {restaurant.get('aim', '')}".lower()
            score = 0
            matched_contexts = []
            
            for context in detected_contexts:
                context_keywords = context_filters[context]['restaurant_keywords']
                if any(keyword in restaurant_text for keyword in context_keywords):
                    score += 1
                    matched_contexts.append(context)
            
            if score > 0:
                scored_restaurants.append((score, restaurant, matched_contexts))
        
        if scored_restaurants:
            scored_restaurants.sort(key=lambda x: x[0], reverse=True)
            filtered = [item[1] for item in scored_restaurants]
        else:
            filtered = restaurant_list
        
        duration = time.time() - start_time
        structured_logger.log_filtering_step("CONTEXT_FILTER", 0, len(restaurant_list), len(filtered), duration, {"detected_contexts": detected_contexts})
        
        return filtered

    def _filter_by_menu(self, user_request: str, restaurant_list: List) -> List:
        """Фільтрація за меню (загальна, не конкретні страви)"""
        start_time = time.time()
        user_lower = user_request.lower()
        
        requested_dishes = []
        for dish, keywords in self.menu_keywords.items():
            if any(keyword in user_lower for keyword in keywords):
                requested_dishes.append(dish)
        
        if not requested_dishes:
            duration = time.time() - start_time
            structured_logger.log_filtering_step("MENU_FILTER", 0, len(restaurant_list), len(restaurant_list), duration, {"requested_dishes": []})
            return restaurant_list
        
        filtered = []
        for restaurant in restaurant_list:
            menu_text = str(restaurant.get('menu', '')).lower()
            has_dish = False
            
            for dish in requested_dishes:
                dish_keywords = self.menu_keywords[dish]
                if any(keyword in menu_text for keyword in dish_keywords):
                    has_dish = True
                    break
            
            if has_dish:
                filtered.append(restaurant)
        
        duration = time.time() - start_time
        structured_logger.log_filtering_step("MENU_FILTER", 0, len(restaurant_list), len(filtered), duration, {"requested_dishes": requested_dishes})
        
        return filtered if filtered else restaurant_list

    async def get_recommendation(self, user_request: str, user_id: int) -> Optional[Dict]:
        """
        ОСНОВНА ФУНКЦІЯ: отримання рекомендацій з A/B тестуванням
        """
        overall_start_time = time.time()
        
        # Визначаємо стратегію
        strategy = ab_test_config.get_strategy_for_user(user_id)
        logger.info(f"Користувач {user_id} отримує стратегію: {strategy.value}")
        
        try:
            # Перевіряємо доступність даних
            if not self.restaurants_data:
                logger.error("Немає даних про ресторани")
                return None

            # Ініціалізуємо OpenAI
            global openai_client
            if openai_client is None:
                import openai
                openai.api_key = OPENAI_API_KEY
                openai_client = openai
            
            # Перемішуємо ресторани для різноманітності
            import random
            shuffled_restaurants = self.restaurants_data.copy()
            random.shuffle(shuffled_restaurants)
            
            # Перевіряємо конкретні страви (однаково для обох стратегій)
            has_dishes, dishes_list, dish_explanation = self._detect_specific_dishes(user_request)
            
            if has_dishes and dishes_list:
                # Фільтруємо тільки за конкретними стравами
                dish_filtered = []
                for restaurant in shuffled_restaurants:
                    menu_text = str(restaurant.get('menu', '')).lower()
                    has_required_dish = False
                    
                    for dish in dishes_list:
                        synonyms = self.dish_synonyms.get(dish, [dish])
                        if any(synonym.lower() in menu_text for synonym in synonyms):
                            has_required_dish = True
                            break
                    
                    if has_required_dish:
                        dish_filtered.append(restaurant)
                
                if not dish_filtered:
                    return {
                        "dish_not_found": True,
                        "missing_dishes": ", ".join(dishes_list),
                        "message": f"На жаль, {', '.join(dishes_list)} ще немає в нашому переліку."
                    }
                
                final_filtered = dish_filtered
                filtering_path = "DISH_SPECIFIC"
                
            else:
                # Застосовуємо стратегію фільтрації
                if strategy == FilteringStrategy.OLD_LOGIC:
                    type_filtered = self._filter_by_establishment_type_old(user_request, shuffled_restaurants)
                    context_filtered = self._filter_by_context(user_request, type_filtered)
                    final_filtered = self._filter_by_menu(user_request, context_filtered)
                    filtering_path = "OLD_LOGIC"
                else:
                    type_filtered = self._filter_by_establishment_type_new(user_request, shuffled_restaurants)
                    context_filtered = self._filter_by_context(user_request, type_filtered)
                    final_filtered = self._filter_by_menu(user_request, context_filtered)
                    filtering_path = "NEW_LOGIC"
            
            # Генеруємо рекомендації через OpenAI
            if final_filtered:
                recommendations = await self._generate_openai_recommendations(user_request, final_filtered)
            else:
                recommendations = None
            
            # Логуємо результати A/B тесту
            total_duration = time.time() - overall_start_time
            ab_results = {
                'strategy': strategy.value,
                'filtering_path': filtering_path,
                'initial_count': len(shuffled_restaurants),
                'final_count': len(final_filtered) if final_filtered else 0,
                'total_duration': round(total_duration, 3),
                'has_recommendations': recommendations is not None,
                'dishes_detected': dishes_list if has_dishes else []
            }
            
            structured_logger.log_ab_test(user_id, strategy.value, user_request, ab_results)
            
            return recommendations
            
        except Exception as e:
            logger.error(f"Помилка в get_recommendation: {e}")
            return None

    async def _generate_openai_recommendations(self, user_request: str, filtered_restaurants: List) -> Optional[Dict]:
        """Генерація рекомендацій через OpenAI"""
        try:
            if not filtered_restaurants:
                return None
                
            # Формуємо дані для OpenAI (максимум 10 варіантів)
            restaurants_details = []
            for i, restaurant in enumerate(filtered_restaurants[:10]):
                detail = f"""Варіант {i+1}:
- Назва: {restaurant.get('name', 'Без назви')}
- Тип: {restaurant.get('тип закладу', restaurant.get('type', 'Не вказано'))}
- Атмосфера: {restaurant.get('vibe', 'Не описана')}
- Призначення: {restaurant.get('aim', 'Не вказано')}
- Кухня: {restaurant.get('cuisine', 'Не вказана')}"""
                restaurants_details.append(detail)
            
            restaurants_text = "\n\n".join(restaurants_details)
            
            prompt = f"""ЗАПИТ: "{user_request}"

ВІДФІЛЬТРОВАНІ ВАРІАНТИ:
{restaurants_text}

Обери 1-2 найкращі варіанти та поясни вибір.
Формат: Варіанти: [1,2] Пріоритет: 1 - пояснення"""

            def make_request():
                return openai_client.ChatCompletion.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": "Ти експерт з ресторанів. Аналізуй варіанти та обирай найкращі."},
                        {"role": "user", "content": prompt}
                    ],
                    max_tokens=200,
                    temperature=0.3
                )
            
            response = await asyncio.wait_for(asyncio.to_thread(make_request), timeout=10.0)
            choice_text = response.choices[0].message.content.strip()
            
            return self._parse_openai_response(choice_text, filtered_restaurants)
            
        except asyncio.TimeoutError:
            logger.error("Timeout при запиті до OpenAI")
            return self._fallback_selection(filtered_restaurants)
        except Exception as e:
            logger.error(f"Помилка OpenAI API: {e}")
            return self._fallback_selection(filtered_restaurants)

    def _parse_openai_response(self, response: str, restaurants: List) -> Optional[Dict]:
        """Парсинг відповіді OpenAI"""
        try:
            import re
            numbers = re.findall(r'\d+', response)
            
            if not numbers:
                return self._fallback_selection(restaurants)
            
            indices = [int(num) - 1 for num in numbers[:2]]
            valid_indices = [idx for idx in indices if 0 <= idx < len(restaurants)]
            
            if not valid_indices:
                return self._fallback_selection(restaurants)
            
            selected_restaurants = [restaurants[idx] for idx in valid_indices]
            
            # Витягуємо пояснення
            priority_explanation = "найкращий варіант за критеріями"
            if '-' in response:
                try:
                    explanation_part = response.split('-', 1)[1].strip()
                    if explanation_part and len(explanation_part) > 5:
                        priority_explanation = explanation_part[:100]
                except:
                    pass
            
            return self._format_recommendation_result(selected_restaurants, 0, priority_explanation)
            
        except Exception as e:
            logger.error(f"Помилка парсингу OpenAI відповіді: {e}")
            return self._fallback_selection(restaurants)

    def _fallback_selection(self, restaurants: List) -> Optional[Dict]:
        """Резервний алгоритм вибору"""
        if not restaurants:
            return None
            
        import random
        
        if len(restaurants) == 1:
            return self._format_recommendation_result(restaurants, 0, "єдиний доступний варіант")
        
        selected = random.sample(restaurants, min(2, len(restaurants)))
        return self._format_recommendation_result(selected, 0, "випадковий вибір")

    def _format_recommendation_result(self, restaurants: List, priority_index: int, explanation: str) -> Dict:
        """Форматування результату рекомендації"""
        if not restaurants:
            return None
            
        result = {
            "restaurants": [],
            "priority_index": priority_index,
            "priority_explanation": explanation
        }
        
        for restaurant in restaurants:
            photo_url = restaurant.get('photo', '')
            if photo_url:
                photo_url = self._convert_google_drive_url(photo_url)
            
            result["restaurants"].append({
                "name": restaurant.get('name', 'Ресторан'),
                "address": restaurant.get('address', 'Адреса не вказана'),
                "socials": restaurant.get('socials', 'Контакти не вказані'),
                "vibe": restaurant.get('vibe', 'Приємна атмосфера'),
                "aim": restaurant.get('aim', 'Для будь-яких подій'),
                "cuisine": restaurant.get('cuisine', 'Смачна кухня'),
                "menu": restaurant.get('menu', ''),
                "menu_url": restaurant.get('menu_url', ''),
                "photo": photo_url,
                "type": restaurant.get('тип закладу', restaurant.get('type', 'Заклад'))
            })
        
        return result

    async def log_request(self, user_id: int, user_request: str, restaurant_name: str, 
                         rating: Optional[int] = None, explanation: str = "",
                         filtering_strategy: str = "", processing_time: float = 0,
                         ab_test_data: dict = None):
        """Розширене логування"""
        if not self.analytics_sheet:
            return
            
        try:
            now = datetime.now()
            row_data = [
                now.strftime("%Y-%m-%d %H:%M:%S"),
                str(user_id),
                user_request[:500],
                restaurant_name,
                str(rating) if rating else "",
                explanation[:200],
                now.strftime("%Y-%m-%d"),
                now.strftime("%H:%M:%S"),
                filtering_strategy,
                f"{processing_time:.3f}",
                str(len(ab_test_data.get('steps', []))) if ab_test_data else "0",
                json.dumps(ab_test_data) if ab_test_data else "{}"
            ]
            
            self.analytics_sheet.append_row(row_data)
            await self.update_summary_stats()
            
        except Exception as e:
            logger.error(f"Помилка логування: {e}")
    
    async def update_summary_stats(self):
        """Оновлення статистики"""
        if not self.analytics_sheet or not self.summary_sheet:
            return
            
        try:
            all_records = self.analytics_sheet.get_all_records()
            
            if not all_records:
                return
            
            total_requests = len(all_records)
            unique_users = len(set(record.get('User ID', '') for record in all_records))
            
            # Рахуємо рейтинги
            ratings = []
            for record in all_records:
                rating_str = record.get('Rating', '')
                if rating_str and str(rating_str).isdigit():
                    ratings.append(int(rating_str))
                    
            avg_rating = sum(ratings) / len(ratings) if ratings else 0
            
            # A/B тест статистика
            new_logic_requests = sum(1 for record in all_records 
                                   if record.get('Filtering Strategy', '').startswith('NEW') or 'new_logic' in record.get('Filtering Strategy', ''))
            ab_ratio = (new_logic_requests / total_requests * 100) if total_requests > 0 else 0
            
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # Оновлюємо статистику
            updates = [
                ('B2', str(total_requests)),
                ('B3', str(unique_users)),
                ('B4', f"{avg_rating:.2f}"),
                ('B5', str(len(ratings))),
                ('B6', f"{ab_ratio:.1f}")
            ]
            
            for cell, value in updates:
                self.summary_sheet.update(cell, value)
                self.summary_sheet.update(cell.replace('B', 'C'), timestamp)
            
            logger.info(f"Оновлено статистику: Запитів: {total_requests}, A/B ratio: {ab_ratio:.1f}%")
            
        except Exception as e:
            logger.error(f"Помилка оновлення статистики: {e}")

# Глобальний екземпляр бота
restaurant_bot = EnhancedRestaurantBot()

async def show_processing_status(update: Update, context: ContextTypes.DEFAULT_TYPE, user_request: str):
    """ВИПРАВЛЕНО: Красивий прогрес обробки з анімацією"""
    status_messages = [
        "🔍 Аналізую ваш запит...",
        "🧠 Розумію ваші побажання...", 
        "📊 Шукаю найкращі варіанти...",
        "🎯 Фільтрую за критеріями...",
        "🤖 Консультуюся з AI експертом...",
        "✨ Готую персональні рекомендації..."
    ]
    
    processing_message = await update.message.reply_text(status_messages[0])
    
    try:
        for i, status in enumerate(status_messages[1:], 1):
            await asyncio.sleep(0.8)
            try:
                await processing_message.edit_text(status)
            except:
                pass
        
        await asyncio.sleep(0.5)
        await processing_message.edit_text("🎉 Готово! Ось найкращі варіанти для вас:")
        
        return processing_message
        
    except Exception as e:
        logger.warning(f"Помилка показу статусу: {e}")
        return processing_message

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обробник команди /start"""
    user_id = update.effective_user.id
    user_states[user_id] = "waiting_request"
    
    strategy = ab_test_config.get_strategy_for_user(user_id)
    
    message = f"""🍽 <b>Вітаю в Restaurant Bot!</b>

Я допоможу знайти ідеальний заклад для будь-якої ситуації!

<b>Просто напишіть що шукаєте:</b>
• "Романтичний ресторан для побачення"
• "Кав'ярня де можна працювати"  
• "Піца з друзями"
• "Де випити матчу?"

<b>Корисні команди:</b>
/help - Детальна інструкція
/list_restaurants - Всі заклади за типами

<b>Готові почати?</b> Опишіть що шукаєте! ✨

<i>💡 Версія алгоритму: {strategy.value}</i>"""
    
    await update.message.reply_text(message, parse_mode='HTML')
    logger.info(f"Користувач {user_id} почав діалог зі стратегією {strategy.value}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /help"""
    help_text = """🤖 <b>Довідка по Restaurant Bot</b>

<b>🎯 Як користуватися:</b>
Просто напишіть що шукаєте природною мовою!

<b>🔍 Приклади запитів:</b>
• "Хочу піцу з друзями"
• "Потрібен ресторан для побачення"
• "Де можна випити матчу?"
• "Сімейне місце для обіду"
• "Швидко перекусити"

<b>🔍 Що бот розуміє:</b>
• <i>Страви:</i> піца, суші, паста, мідії, стейк та ін.
• <i>Атмосферу:</i> романтично, сімейно, весело, затишно
• <i>Призначення:</i> побачення, друзі, робота, святкування
• <i>Типи:</i> ресторан, кав'ярня, доставка, to-go

<b>🧪 A/B тестування:</b>
Бот автоматично тестує різні алгоритми для покращення якості!

<b>⭐ Оцінювання:</b>
Ваші оцінки допомагають покращувати систему!

<b>📋 Команди:</b>
/start - Почати пошук
/help - Ця довідка
/list_restaurants - Список закладів
/stats - Статистика (для адмінів)

Готові знайти ідеальне місце? Напишіть свій запит! 🍽️"""

    await update.message.reply_text(help_text, parse_mode='HTML')

async def list_restaurants_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /list_restaurants"""
    user_id = update.effective_user.id
    
    if not restaurant_bot.restaurants_data:
        await update.message.reply_text("❌ База даних ресторанів недоступна")
        return
    
    # Групуємо за типами
    grouped_restaurants = {}
    for restaurant in restaurant_bot.restaurants_data:
        establishment_type = restaurant.get('тип закладу', restaurant.get('type', 'Інше'))
        if not establishment_type or establishment_type.strip() == '':
            establishment_type = 'Інше'
        
        if establishment_type not in grouped_restaurants:
            grouped_restaurants[establishment_type] = []
        grouped_restaurants[establishment_type].append(restaurant)
    
    strategy = ab_test_config.get_strategy_for_user(user_id)
    total_count = len(restaurant_bot.restaurants_data)
    
    # Формуємо повідомлення
    message_parts = [f"🏢 <b>Всі заклади за типами:</b>\n"]
    
    sorted_types = sorted(grouped_restaurants.items(), key=lambda x: len(x[1]), reverse=True)
    
    for establishment_type, restaurants in sorted_types:
        count = len(restaurants)
        icon = {
            'ресторан': '🍽️', 'кав\'ярня': '☕', 'кафе': '☕',
            'доставка': '🚚', 'delivery': '🚚', 'to-go': '🥡',
            'takeaway': '🥡', 'бар': '🍸'
        }.get(establishment_type.lower(), '🪩')
        
        message_parts.append(f"\n{icon} <b>{establishment_type.upper()}</b> ({count})")
        
        # Показуємо перші 3 заклади
        for restaurant in restaurants[:3]:
            name = restaurant.get('name', 'Без назви')
            cuisine = restaurant.get('cuisine', '')
            if cuisine:
                message_parts.append(f"   • {name} <i>({cuisine})</i>")
            else:
                message_parts.append(f"   • {name}")
        
        if count > 3:
            message_parts.append(f"   • ... та ще {count - 3}")
    
    message_parts.extend([
        f"\n📊 <b>Загалом:</b> {total_count} закладів",
        f"🤖 <b>Версія алгоритму:</b> {strategy.value}",
        f"\n🔍 Для пошуку просто напишіть що шукаєте!"
    ])
    
    full_message = '\n'.join(message_parts)
    
    # Перевіряємо довжину
    if len(full_message) > 4000:
        short_message = f"""🏢 <b>База закладів:</b>

📊 <b>Загалом:</b> {total_count} закладів
🤖 <b>Версія алгоритму:</b> {strategy.value}

Типи закладів: {', '.join([f"{t} ({len(r)})" for t, r in sorted_types[:5]])}

🔍 Для детального пошуку напишіть що шукаєте!"""
        full_message = short_message
    
    await update.message.reply_text(full_message, parse_mode='HTML')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда статистики з A/B даними"""
    user_id = update.effective_user.id
    admin_ids = [980047923]  # Замінити на реальні ID
    
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
        
        stats_text = f"""📊 <b>Статистика бота з A/B тестуванням</b>

📈 Загальна кількість запитів: <b>{summary_data[1][1]}</b>
👥 Унікальних користувачів: <b>{summary_data[2][1]}</b>
⭐ Середня оцінка: <b>{summary_data[3][1]}</b>
🔢 Кількість оцінок: <b>{summary_data[4][1]}</b>
🧪 Нова логіка: <b>{summary_data[5][1]}%</b>

🔧 <b>Налаштування A/B тесту:</b>
• Fuzzy matching: {'✅' if FUZZY_AVAILABLE else '❌'}
• Співвідношення: {ab_test_config.test_ratio*100}% нова логіка
• Форсована стратегія: {ab_test_config.force_strategy or 'Немає'}

🕐 Останнє оновлення: {summary_data[1][2]}

<b>Для тестування:</b>
<code>FORCE_FILTERING_STRATEGY=old_logic</code> - стара логіка
<code>FORCE_FILTERING_STRATEGY=new_logic</code> - нова логіка"""
        
        await update.message.reply_text(stats_text, parse_mode='HTML')
        
    except Exception as e:
        logger.error(f"Помилка статистики: {e}")
        await update.message.reply_text("Помилка отримання статистики")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ВИПРАВЛЕНИЙ обробник з поверненням UX дизайну"""
    user_id = update.effective_user.id
    
    if user_id not in user_states:
        await update.message.reply_text("Напишіть /start, щоб почати")
        return
    
    user_text = update.message.text
    current_state = user_states[user_id]
    
    # Обробка пояснення оцінки
    if current_state == "waiting_explanation":
        explanation = user_text
        rating_data = user_rating_data.get(user_id, {})
        
        if rating_data:
            await restaurant_bot.log_request(
                user_id, 
                rating_data.get('user_request', ''), 
                rating_data.get('restaurant_name', ''), 
                rating_data.get('rating'),
                explanation,
                rating_data.get('filtering_strategy', ''),
                rating_data.get('processing_time', 0),
                rating_data.get('ab_test_data', {})
            )
            
            await update.message.reply_text(
                f"Дякую за детальну оцінку! 🙏\n\n"
                f"Ваша оцінка: {rating_data['rating']}/10\n"
                f"Стратегія: {rating_data.get('filtering_strategy', 'невідома')}\n\n"
                f"Напишіть /start, щоб знайти ще один ресторан!"
            )
            
            # Очищення
            user_states[user_id] = "completed"
            user_last_recommendation.pop(user_id, None)
            user_rating_data.pop(user_id, None)
            
        return
    
    # Обробка оцінки
    if current_state == "waiting_rating" and user_text.isdigit():
        rating = int(user_text)
        if 1 <= rating <= 10:
            restaurant_name = user_last_recommendation.get(user_id, "Невідомий ресторан")
            
            # Зберігаємо всі дані для логування
            existing_data = user_rating_data.get(user_id, {})
            user_rating_data[user_id] = {
                **existing_data,
                'rating': rating,
                'restaurant_name': restaurant_name,
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
    
    # Обробка нового запиту
    if current_state == "waiting_request":
        user_request = user_text
        logger.info(f"Користувач {user_id} надіслав запит: {user_request}")
        
        # ВИПРАВЛЕНО: Показуємо красивий прогрес
        processing_message = await show_processing_status(update, context, user_request)
        
        # Отримуємо рекомендації
        start_time = time.time()
        recommendation = await restaurant_bot.get_recommendation(user_request, user_id)
        processing_time = time.time() - start_time
        
        # Видаляємо статус
        try:
            await processing_message.delete()
        except:
            pass
        
        if recommendation:
            if recommendation.get("dish_not_found"):
                not_found_message = f"""😔 <b>Страву не знайдено</b>

{recommendation['message']}

💡 <b>Поради:</b>
• Спробуйте інші варіанти: піца, суші, паста, салати
• Або опишіть атмосферу: "романтичне місце", "кав'ярня для роботи"
• Використайте /list_restaurants для перегляду всіх закладів

Напишіть новий запит або /start для початку! 🔄"""
                
                await update.message.reply_text(not_found_message, parse_mode='HTML')
                return
            
            # Успішні рекомендації
            restaurants = recommendation["restaurants"]
            priority_index = recommendation["priority_index"]
            priority_explanation = recommendation["priority_explanation"]
            
            main_restaurant = restaurants[priority_index]
            strategy = ab_test_config.get_strategy_for_user(user_id)
            
            # Зберігаємо для оцінки
            user_last_recommendation[user_id] = main_restaurant["name"]
            user_rating_data[user_id] = {
                'user_request': user_request,
                'restaurant_name': main_restaurant["name"],
                'filtering_strategy': strategy.value,
                'processing_time': processing_time,
                'ab_test_data': {
                    'strategy': strategy.value,
                    'processing_time': processing_time,
                    'restaurants_count': len(restaurants)
                }
            }
            user_states[user_id] = "waiting_rating"
            
            # ВИПРАВЛЕНО: Форматуємо красиво як в оригіналі
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

            # Додаємо меню
            main_menu_url = main_restaurant.get('menu_url', '')
            if main_menu_url and main_menu_url.startswith('http'):
                response_text += f"\n\n📋 <a href='{main_menu_url}'>Переглянути меню головної рекомендації</a>"

            # Відправляємо з фото
            main_photo_url = main_restaurant.get('photo', '')
            
            if main_photo_url and main_photo_url.startswith('http'):
                try:
                    await update.message.reply_photo(
                        photo=main_photo_url,
                        caption=response_text,
                        parse_mode='HTML'
                    )
                except Exception:
                    response_text += f"\n\n📸 <a href='{main_photo_url}'>Переглянути фото головної рекомендації</a>"
                    await update.message.reply_text(response_text, parse_mode='HTML')
            else:
                await update.message.reply_text(response_text, parse_mode='HTML')
            
            # Просимо оцінку
            rating_text = f"""⭐ <b>Оцініть головну рекомендацію</b>

🎯 <b>Оцінюємо:</b> "{main_restaurant['name']}"
🤖 <b>Стратегія:</b> {strategy.value}

<b>Шкала оцінки:</b>
1-3: Зовсім не підходить
4-6: Частково підходить  
7-8: Добре підходить
9-10: Ідеально підходить

<b>Напишіть число від 1 до 10:</b> 👇

💡 <i>Ваші оцінки допомагають боту краще розуміти ваші вподобання!</i>"""
            await update.message.reply_text(rating_text, parse_mode='HTML')
            
        else:
            no_results_message = """😔 <b>Нічого не знайдено</b>

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
            await update.message.reply_text("Будь ласка, оцініть попередню рекомендацію числом від 1 до 10")
        else:
            await update.message.reply_text("Напишіть /start, щоб почати знову")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Обробник помилок"""
    logger.error(f"❌ Помилка: {context.error}")

def main():
    """Основна функція з валідацією"""
    required_vars = [TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, GOOGLE_SHEET_URL]
    if not all(required_vars):
        missing = [name for name, val in [
            ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
            ("OPENAI_API_KEY", OPENAI_API_KEY), 
            ("GOOGLE_SHEET_URL", GOOGLE_SHEET_URL)
        ] if not val]
        logger.error(f"❌ Відсутні змінні середовища: {missing}")
        return
    
    logger.info("🚀 Запускаю бота з A/B тестуванням та UX дизайном...")
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        
        # Додаємо обробники
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("list_restaurants", list_restaurants_command))
        application.add_handler(CommandHandler("stats", stats_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)
        
        # Ініціалізуємо Google Sheets
        logger.info("🔗 Підключаюся до Google Sheets...")
        sheets_success = loop.run_until_complete(restaurant_bot.init_google_sheets())
        
        if not sheets_success:
            logger.error("❌ Не вдалось підключитись до Google Sheets")
            return
        
        # Логуємо конфігурацію
        logger.info(f"🔧 A/B тест: {ab_test_config.test_ratio*100}% нова логіка")
        logger.info(f"🔧 Fuzzy matching: {'доступний' if FUZZY_AVAILABLE else 'недоступний'}")
        logger.info(f"🔧 Форсована стратегія: {ab_test_config.force_strategy or 'немає'}")
        
        logger.info("✅ Бот готовий! UX дизайн + A/B тестування + детальне логування")
        
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
