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
    'enabled': True,  # Головний перемикач
    'fuzzy_matching': True,  # Fuzzy matching
    'fuzzy_threshold': 80,  # Мінімальний % схожості для fuzzy match
    'regex_boundaries': True,  # Використання word boundaries
    'negation_detection': True,  # Детекція заперечень
    'extended_synonyms': True,  # Розширені синоніми
    'fallback_to_old': True  # Fallback до старої логіки якщо нова не знайде результатів
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
            # Типи закладів
            'ресторан': ['ресторан', 'ресторани', 'ресторанчик', 'їдальня', 'заклад'],
            'кав\'ярня': ['кав\'ярня', 'кафе', 'кава', 'каварня', 'coffee', 'кофе'],
            'піца': ['піца', 'піцца', 'pizza', 'піци', 'піззу'],
            'суші': ['суші', 'sushi', 'роли', 'роллы', 'сашімі'],
            'бургер': ['бургер', 'burger', 'гамбургер', 'чізбургер'],
            
            # Атмосфера
            'романтик': ['романтик', 'романтичний', 'побачення', 'інтимний', 'затишний', 'свічки'],
            'сімейний': ['сімейний', 'сім\'я', 'родина', 'діти', 'дитячий', 'для всієї сім\'ї'],
            'веселий': ['веселий', 'жвавий', 'енергійний', 'гучний', 'драйвовий', 'молодіжний'],
            
            # Контекст
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
        
        logger.warning(f"Не вдалось витягнути ID з Google Drive посилання: {url}")
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

    def _check_dish_availability(self, user_request: str) -> Tuple[bool, List[str]]:
        """
        Перевіряє, чи є потрібна страва в меню хоча б одного ресторану
        
        Returns:
            (є_страва_в_меню, список_знайдених_страв)
        """
        user_lower = user_request.lower()
        logger.info(f"🔍 Перевіряю наявність конкретних страв в запиті: '{user_request}'")
        
        # Розширений словник страв з синонімами
        food_keywords = {
            'піца': ['піца', 'піцц', 'pizza', 'піци', 'піззу'],
            'паста': ['паста', 'спагеті', 'pasta', 'спагетті', 'макарони'],
            'бургер': ['бургер', 'burger', 'гамбургер', 'чізбургер'],
            'суші': ['суші', 'sushi', 'роли', 'ролл', 'сашімі'],
            'салат': ['салат', 'salad'],
            'хумус': ['хумус', 'hummus'],
            'фалафель': ['фалафель', 'falafel'],
            'шаурма': ['шаурм', 'shawarma', 'шаверма'],
            'стейк': ['стейк', 'steak', 'м\'ясо', 'біфштекс'],
            'риба': ['риба', 'fish', 'лосось', 'семга', 'тунець', 'форель'],
            'курка': ['курк', 'курчат', 'chicken', 'курица'],
            'десерт': ['десерт', 'торт', 'тірамісу', 'морозиво', 'чізкейк', 'тістечко'],
            'мідії': ['мідії', 'мидии', 'мідія', 'молюски', 'мідій'],
            'креветки': ['креветки', 'креветка', 'shrimp', 'prawns'],
            'устриці': ['устриці', 'устрица', 'oysters'],
            'каламари': ['каламари', 'кальмари', 'squid'],
            'равіолі': ['равіолі', 'ravioli', 'равиоли'],
            'лазанья': ['лазанья', 'lasagna', 'лазања'],
            'різотто': ['різотто', 'risotto', 'ризотто'],
            'гнокі': ['гноки', 'gnocchi', 'ньокі'],
            'тартар': ['тартар', 'tartar'],
            'карпачо': ['карпачо', 'carpaccio'],
        }
        
        # Знаходимо які страви згадав користувач
        requested_dishes = []
        for dish, keywords in food_keywords.items():
            match_found = False
            
            # Перевіряємо різними способами
            for keyword in keywords:
                if ENHANCED_SEARCH_CONFIG['enabled'] and ENHANCED_SEARCH_CONFIG['regex_boundaries']:
                    # Використовуємо word boundaries для точнішого пошуку
                    pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
                    if re.search(pattern, user_lower):
                        match_found = True
                        logger.info(f"🎯 Знайдено страву '{dish}' через keyword '{keyword}' (regex)")
                        break
                else:
                    # Простий пошук підрядка
                    if keyword.lower() in user_lower:
                        match_found = True
                        logger.info(f"🎯 Знайдено страву '{dish}' через keyword '{keyword}' (substring)")
                        break
            
            # Fuzzy matching як додатковий метод
            if not match_found and ENHANCED_SEARCH_CONFIG['fuzzy_matching'] and FUZZY_AVAILABLE:
                user_words = user_lower.split()
                for user_word in user_words:
                    if len(user_word) > 3:  # Тільки для слів довше 3 символів
                        for keyword in keywords:
                            if len(keyword) > 3:
                                fuzzy_score = fuzz.ratio(keyword.lower(), user_word)
                                if fuzzy_score >= 85:  # Високий поріг для страв
                                    match_found = True
                                    logger.info(f"🔍 Знайдено страву '{dish}' через fuzzy matching: '{keyword}' ≈ '{user_word}' (score: {fuzzy_score})")
                                    break
                    if match_found:
                        break
            
            if match_found:
                requested_dishes.append(dish)
        
        if not requested_dishes:
            logger.info("🤔 Конкретні страви не знайдені в запиті")
            return False, []
        
        logger.info(f"🍽️ Користувач шукає страви: {requested_dishes}")
        
        # Тепер перевіряємо чи є ці страви в меню ресторанів
        dishes_found_in_restaurants = []
        
        for dish in requested_dishes:
            found_in_any_restaurant = False
            dish_keywords = food_keywords[dish]
            
            for restaurant in self.restaurants_data:
                menu_text = restaurant.get('menu', '').lower()
                
                # Перевіряємо кожен синонім страви в меню ресторану
                for keyword in dish_keywords:
                    if ENHANCED_SEARCH_CONFIG['regex_boundaries']:
                        pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
                        if re.search(pattern, menu_text):
                            found_in_any_restaurant = True
                            logger.info(f"✅ Страву '{dish}' знайдено в меню '{restaurant.get('name', 'Невідомий')}'")
                            break
                    else:
                        if keyword.lower() in menu_text:
                            found_in_any_restaurant = True
                            logger.info(f"✅ Страву '{dish}' знайдено в меню '{restaurant.get('name', 'Невідомий')}'")
                            break
                
                if found_in_any_restaurant:
                    break
            
            if found_in_any_restaurant:
                dishes_found_in_restaurants.append(dish)
            else:
                logger.info(f"❌ Страву '{dish}' НЕ знайдено в жодному меню")
        
        # Якщо хоча б одна страва знайдена - все ОК
        if dishes_found_in_restaurants:
            logger.info(f"🎉 Знайдено страви в ресторанах: {dishes_found_in_restaurants}")
            return True, dishes_found_in_restaurants
        else:
            logger.warning(f"😞 Жодна з запитаних страв не знайдена в ресторанах: {requested_dishes}")
            return False, requested_dishes

    def _enhanced_keyword_match(self, user_text: str, keywords: List[str], context: str = "") -> Tuple[bool, float, List[str]]:
        """
        Покращений пошук ключових слів з різними методами
        
        Returns:
            (знайдено, впевненість, знайдені_слова)
        """
        if not ENHANCED_SEARCH_CONFIG['enabled']:
            # Fallback до старої логіки
            old_match = any(keyword in user_text.lower() for keyword in keywords)
            return old_match, 1.0 if old_match else 0.0, []
        
        user_lower = user_text.lower()
        found_keywords = []
        max_confidence = 0.0
        any_match = False
        
        # Спочатку перевіряємо заперечення
        if ENHANCED_SEARCH_CONFIG['negation_detection']:
            if self._has_negation_near_keywords(user_text, keywords):
                logger.info(f"🚫 NEGATION: Знайдено заперечення для {keywords[:3]}...")
                return False, 0.0, []
        
        for keyword in keywords:
            keyword_lower = keyword.lower()
            confidence = 0.0
            
            # 1. Exact match (найвища пріоритетність)
            if keyword_lower in user_lower:
                if ENHANCED_SEARCH_CONFIG['regex_boundaries']:
                    # Перевіряємо word boundaries щоб уникнути false positives
                    pattern = r'\b' + re.escape(keyword_lower) + r'\b'
                    if re.search(pattern, user_lower):
                        confidence = 1.0
                        any_match = True
                        found_keywords.append(keyword)
                        logger.info(f"✅ EXACT: '{keyword}' знайдено з word boundaries")
                else:
                    confidence = 0.9  # Трохи менше за exact з boundaries
                    any_match = True
                    found_keywords.append(keyword)
                    logger.info(f"✅ SUBSTRING: '{keyword}' знайдено (без boundaries)")
            
            # 2. Fuzzy matching для опечаток
            elif ENHANCED_SEARCH_CONFIG['fuzzy_matching'] and FUZZY_AVAILABLE:
                # Розбиваємо на слова і перевіряємо кожне
                user_words = user_lower.split()
                for user_word in user_words:
                    if len(user_word) > 2 and len(keyword_lower) > 2:  # Тільки для слів довше 2 символів
                        fuzzy_score = fuzz.ratio(keyword_lower, user_word)
                        if fuzzy_score >= ENHANCED_SEARCH_CONFIG['fuzzy_threshold']:
                            confidence = max(confidence, fuzzy_score / 100.0 * 0.8)  # Fuzzy менш пріоритетний
                            any_match = True
                            found_keywords.append(f"{keyword}~{user_word}")
                            logger.info(f"🔍 FUZZY: '{keyword}' ≈ '{user_word}' (score: {fuzzy_score})")
            
            # 3. Синоніми
            if ENHANCED_SEARCH_CONFIG['extended_synonyms']:
                try:
                    synonym_match, synonym_confidence, synonym_words = self._check_synonyms(user_lower, keyword)
                    if synonym_match:
                        confidence = max(confidence, synonym_confidence * 0.7)  # Синоніми трохи менш пріоритетні
                        any_match = True
                        found_keywords.extend([f"{keyword}→{sw}" for sw in synonym_words])
                except Exception as e:
                    logger.warning(f"⚠️ Помилка перевірки синонімів для '{keyword}': {e}")
            
            max_confidence = max(max_confidence, confidence)
        
        return any_match, max_confidence, found_keywords
    
    def _has_negation_near_keywords(self, user_text: str, keywords: List[str], window: int = 5) -> bool:
        """Перевіряє чи є заперечення поблизу ключових слів"""
        user_lower = user_text.lower()
        words = user_lower.split()
        
        # Знаходимо позиції ключових слів
        keyword_positions = []
        for i, word in enumerate(words):
            for keyword in keywords:
                if keyword.lower() in word or (FUZZY_AVAILABLE and fuzz.ratio(keyword.lower(), word) > 85):
                    keyword_positions.append(i)
        
        # Перевіряємо заперечення в околиці
        for pos in keyword_positions:
            start = max(0, pos - window)
            end = min(len(words), pos + window + 1)
            
            for i in range(start, end):
                if i != pos:  # Не перевіряємо саме ключове слово
                    word = words[i]
                    for negation in self.negation_words:
                        if negation in word or word in negation:
                            logger.info(f"🚫 Знайдено заперечення '{negation}' поблизу позиції {pos}")
                            return True
        
        return False
    
    def _check_synonyms(self, user_text: str, keyword: str) -> Tuple[bool, float, List[str]]:
        """Перевіряє синоніми для ключового слова"""
        keyword_lower = keyword.lower()
        found_synonyms = []
        max_confidence = 0.0
        
        # Перевіряємо чи є keyword в наших розширених синонімах
        for base_word, synonyms in self.extended_synonyms.items():
            if keyword_lower in [s.lower() for s in synonyms]:
                # Перевіряємо всі синоніми цієї групи
                for synonym in synonyms:
                    if synonym.lower() in user_text:
                        found_synonyms.append(synonym)
                        max_confidence = max(max_confidence, 0.8)  # Високий рейтинг для синонімів
                        logger.info(f"📚 SYNONYM: '{keyword}' → '{synonym}'")
        
    def _comprehensive_content_analysis(self, user_request: str) -> Tuple[bool, List[Dict], str]:
        """
        Комплексний аналіз запиту користувача по всіх колонках таблиці
        
        Returns:
            (знайдено_релевантні_заклади, список_закладів_з_оцінками, пояснення)
        """
        user_lower = user_request.lower()
        logger.info(f"🔎 КОМПЛЕКСНИЙ АНАЛІЗ: '{user_request}'")
        
        # Розширені ключові слова для пошуку по всіх колонках
        search_criteria = {
            # Напої та специфічні речі
            'матча': {
                'keywords': ['матча', 'matcha', 'матчі', 'матчу'],
                'columns': ['menu', 'aim', 'vibe', 'cuisine', 'name'],
                'weight': 3.0  # Висока вага для специфічних запитів
            },
            'кава': {
                'keywords': ['кава', 'кофе', 'coffee', 'капучіно', 'латте', 'еспресо'],
                'columns': ['menu', 'aim', 'cuisine', 'name'],
                'weight': 2.5
            },
            
            # Страви
            'піца': {
                'keywords': ['піца', 'піцц', 'pizza'],
                'columns': ['menu', 'cuisine', 'name'],
                'weight': 3.0
            },
            'суші': {
                'keywords': ['суші', 'sushi', 'роли', 'ролл', 'сашімі'],
                'columns': ['menu', 'cuisine', 'name'],
                'weight': 3.0
            },
            'паста': {
                'keywords': ['паста', 'pasta', 'спагеті'],
                'columns': ['menu', 'cuisine'],
                'weight': 2.5
            },
            'мідії': {
                'keywords': ['мідії', 'мідія', 'мідій', 'молюски'],
                'columns': ['menu', 'cuisine'],
                'weight': 3.0
            },
            
            # Типи закладів
            'ресторан': {
                'keywords': ['ресторан', 'ресторани', 'їдальня'],
                'columns': ['type', 'тип закладу', 'aim'],
                'weight': 2.0
            },
            'кав\'ярня': {
                'keywords': ['кав\'ярня', 'кафе', 'coffee shop'],
                'columns': ['type', 'тип закладу', 'aim'],
                'weight': 2.0
            },
            
            # Атмосфера
            'романтично': {
                'keywords': ['романт', 'побачення', 'інтимн', 'затишн'],
                'columns': ['vibe', 'aim'],
                'weight': 2.0
            },
            'сімейно': {
                'keywords': ['сім\'я', 'сімейн', 'діти', 'родин'],
                'columns': ['vibe', 'aim'],
                'weight': 2.0
            },
            'друзі': {
                'keywords': ['друз', 'компан', 'гурт'],
                'columns': ['aim', 'vibe'],
                'weight': 2.0
            },
            
            # Призначення
            'працювати': {
                'keywords': ['працювати', 'попрацювати', 'робота', 'ноутбук'],
                'columns': ['aim'],
                'weight': 2.5
            },
            'сніданок': {
                'keywords': ['сніданок', 'ранок', 'зранку'],
                'columns': ['aim', 'menu'],
                'weight': 2.0
            },
            'обід': {
                'keywords': ['обід', 'пообідати'],
                'columns': ['aim'],
                'weight': 1.5
            },
            'вечеря': {
                'keywords': ['вечер', 'повечеряти'],
                'columns': ['aim'],
                'weight': 1.5
            },
            
            # Кухні
            'італійський': {
                'keywords': ['італ', 'italian', 'італійськ'],
                'columns': ['cuisine', 'vibe', 'name'],
                'weight': 2.0
            },
            'японський': {
                'keywords': ['япон', 'japanese', 'азійськ'],
                'columns': ['cuisine', 'vibe'],
                'weight': 2.0
            },
            'грузинський': {
                'keywords': ['грузин', 'georgian'],
                'columns': ['cuisine', 'vibe', 'name'],
                'weight': 2.0
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
                    
                    for column in columns:
                        column_text = str(restaurant.get(column, '')).lower()
                        
                        if any(keyword in column_text for keyword in keywords):
                            restaurant_has_criterion = True
                            logger.info(f"   ✅ {restaurant.get('name', '')} має '{criterion_name}' в колонці '{column}'")
                            break
                    
                    if restaurant_has_criterion:
                        total_score += weight
                        matched_criteria.append(criterion_name)
            
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
            top_restaurants = [item for item in restaurant_scores if item['score'] >= top_score * 0.7]  # 70% від найкращої оцінки
            
            explanation = f"знайдено {len(top_restaurants)} закладів що відповідають критеріям"
            logger.info(f"🎉 КОМПЛЕКСНИЙ АНАЛІЗ: {explanation}")
            
            return True, top_restaurants, explanation
        else:
            logger.info("🤔 КОМПЛЕКСНИЙ АНАЛІЗ: не знайдено специфічних критеріів")
            return False, [], "не знайдено специфічних критеріїв"
    
    def _get_dish_keywords(self, dish: str) -> List[str]:
        """Повертає список ключових слів для конкретної страви"""
        food_keywords = {
            'піца': ['піца', 'піцц', 'pizza', 'піци', 'піззу'],
            'паста': ['паста', 'спагеті', 'pasta', 'спагетті', 'макарони'],
            'бургер': ['бургер', 'burger', 'гамбургер', 'чізбургер'],
            'суші': ['суші', 'sushi', 'роли', 'ролл', 'сашімі'],
            'салат': ['салат', 'salad'],
            'хумус': ['хумус', 'hummus'],
            'фалафель': ['фалафель', 'falafel'],
            'шаурма': ['шаурм', 'shawarma', 'шаверма'],
            'стейк': ['стейк', 'steak', 'м\'ясо', 'біфштекс'],
            'риба': ['риба', 'fish', 'лосось', 'семга', 'тунець', 'форель'],
            'курка': ['курк', 'курчат', 'chicken', 'курица'],
            'десерт': ['десерт', 'торт', 'тірамісу', 'морозиво', 'чізкейк', 'тістечко'],
            'мідії': ['мідії', 'мидии', 'мідія', 'молюски', 'мідій'],
            'креветки': ['креветки', 'креветка', 'shrimp', 'prawns'],
            'устриці': ['устриці', 'устрица', 'oysters'],
            'каламари': ['каламари', 'кальмари', 'squid'],
            'равіолі': ['равіолі', 'ravioli', 'равиоли'],
            'лазанья': ['лазанья', 'lasagna', 'лазања'],
            'різотто': ['різотто', 'risotto', 'ризотто'],
            'гнокі': ['гноки', 'gnocchi', 'ньокі'],
            'тартар': ['тартар', 'tartar'],
            'карпачо': ['карпачо', 'carpaccio'],
        }
        
        return food_keywords.get(dish, [dish])

    def _enhanced_filter_by_establishment_type(self, user_request: str, restaurant_list):
        """Покращена фільтрація за типом закладу"""
        user_lower = user_request.lower()
        logger.info(f"🏢 ENHANCED: Аналізую запит '{user_request}'")
        
        if not restaurant_list:
            return restaurant_list
        
        # Покращені категорії з розширеними синонімами
        enhanced_type_keywords = {
            'ресторан': {
                'user_keywords': ['ресторан', 'ресторани', 'ресторанчик', 'обід', 'вечеря', 'побачення', 'романтик', 'святкування', 'банкет', 'посідіти', 'поїсти', 'заклад'],
                'establishment_types': ['ресторан']
            },
            'кав\'ярня': {
                'user_keywords': ['кава', 'капучіно', 'латте', 'еспресо', 'кав\'ярня', 'десерт', 'тірамісу', 'круасан', 'випити кави', 'кофе', 'кафе', 'coffee'],
                'establishment_types': ['кав\'ярня', 'кафе']
            },
            'to-go': {
                'user_keywords': ['швидко', 'на винос', 'перекус', 'поспішаю', 'to-go', 'takeaway', 'на швидку руку', 'перехопити'],
                'establishment_types': ['to-go', 'takeaway']
            },
            'доставка': {
                'user_keywords': ['доставка', 'додому', 'замовити', 'привезти', 'delivery', 'не хочу йти', 'вдома'],
                'establishment_types': ['доставка', 'delivery']
            }
        }
        
        # Знаходимо відповідний тип закладу з покращеним пошуком
        detected_types = []
        detection_details = []
        
        for establishment_type, keywords in enhanced_type_keywords.items():
            match_found, confidence, found_words = self._enhanced_keyword_match(
                user_request, 
                keywords['user_keywords'], 
                f"establishment_type_{establishment_type}"
            )
            
            if match_found:
                detected_types.extend(keywords['establishment_types'])
                detection_details.append({
                    'type': establishment_type,
                    'confidence': confidence,
                    'found_words': found_words
                })
                logger.info(f"🎯 ENHANCED: Виявлено тип '{establishment_type}' з впевненістю {confidence:.2f}")
        
        # Якщо тип не визначено, не фільтруємо
        if not detected_types:
            logger.info("🏢 ENHANCED: Тип закладу не визначено, повертаю всі заклади")
            return restaurant_list
        
        logger.info(f"🏢 ENHANCED: Шукані типи закладів: {detected_types}")
        
        # Фільтруємо за типом закладу
        filtered_restaurants = []
        for restaurant in restaurant_list:
            establishment_type = restaurant.get('тип закладу', restaurant.get('type', '')).lower().strip()
            
            # Перевіряємо збіг типу закладу
            type_match = any(
                detected_type.lower().strip() in establishment_type or 
                establishment_type in detected_type.lower().strip() 
                for detected_type in detected_types
            )
            
            if type_match:
                filtered_restaurants.append(restaurant)
                logger.info(f"   ✅ ENHANCED: {restaurant.get('name', '')}: тип '{establishment_type}' ПІДХОДИТЬ")
            else:
                logger.info(f"   ❌ ENHANCED: {restaurant.get('name', '')}: тип '{establishment_type}' НЕ ПІДХОДИТЬ")
        
        # Fallback до старої логіки якщо нова не знайшла результатів
        if not filtered_restaurants and ENHANCED_SEARCH_CONFIG['fallback_to_old']:
            logger.warning("⚠️ ENHANCED: Нова логіка не знайшла результатів, fallback до старої")
            return self._filter_by_establishment_type(user_request, restaurant_list)
        
        if filtered_restaurants:
            logger.info(f"🏢 ENHANCED: УСПІХ! Відфільтровано {len(filtered_restaurants)} закладів відповідного типу з {len(restaurant_list)}")
        else:
            logger.warning(f"🏢 ENHANCED: ПРОБЛЕМА! Жоден заклад не підходить за типом, повертаю всі {len(restaurant_list)} закладів")
            return restaurant_list
        
        return filtered_restaurants
    
    # Старі методи залишаємо для fallback
    def _filter_by_establishment_type(self, user_request: str, restaurant_list):
        """СТАРА ЛОГІКА: Фільтрує ресторани за типом закладу"""
        user_lower = user_request.lower()
        logger.info(f"🏢 OLD: Аналізую запит '{user_request}'")
        
        # Визначаємо тип закладу з запиту користувача
        type_keywords = {
            'ресторан': {
                'user_keywords': ['ресторан', 'обід', 'вечеря', 'побачення', 'романтик', 'святкув', 'банкет', 'посідіти', 'поїсти'],
                'establishment_types': ['ресторан']
            },
            'кав\'ярня': {
                'user_keywords': ['кава', 'капучіно', 'латте', 'еспресо', 'кав\'ярня', 'десерт', 'тірамісу', 'круасан', 'випити кави', 'кофе', 'кафе'],
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
                logger.info(f"🎯 OLD: Виявлено збіг '{establishment_type}'")
        
        # Якщо тип не визначено, не фільтруємо
        if not detected_types:
            logger.info("🏢 OLD: Тип закладу не визначено, повертаю всі заклади")
            return restaurant_list
        
        # Фільтруємо за типом закладу
        filtered_restaurants = []
        for restaurant in restaurant_list:
            establishment_type = restaurant.get('тип закладу', restaurant.get('type', '')).lower().strip()
            type_match = any(detected_type.lower().strip() in establishment_type or establishment_type in detected_type.lower().strip() 
                           for detected_type in detected_types)
            
            if type_match:
                filtered_restaurants.append(restaurant)
        
        return filtered_restaurants if filtered_restaurants else restaurant_list

    def _filter_by_vibe(self, user_request: str, restaurant_list):
        """Фільтрує ресторани за атмосферою (vibe)"""
        user_lower = user_request.lower()
        logger.info(f"✨ Аналізую запит на атмосферу: '{user_request}'")
        
        # Ключові слова для атмосфери
        vibe_keywords = {
            'романтичний': ['романт', 'побачен', 'інтимн', 'затишн', 'свічки', 'романс', 'двох'],
            'веселий': ['весел', 'живо', 'енергійн', 'гучн', 'драйв', 'динамічн'],
            'спокійний': ['спокійн', 'тих', 'релакс', 'умиротворен'],
            'елегантний': ['елегантн', 'розкішн', 'стильн', 'преміум', 'вишукан'],
            'casual': ['casual', 'невимушен', 'простий', 'домашн'],
            'затишний': ['затишн', 'домашн', 'теплий', 'комфортн']
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
            'сімейний': ['сім', 'діт', 'родин', 'батьк', 'мам', 'дитин'],
            'діл': ['діл', 'зустріч', 'перегов', 'бізнес', 'робоч', 'офіс', 'партнер'],
            'друз': ['друз', 'компан', 'гуртом', 'тусовк', 'молодіжн'],
            'пар': ['пар', 'двох', 'побачен', 'романт', 'коханою', 'коханого'],
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
                'restaurant_keywords': ['інтимн', 'романт', 'для пар', 'камерн', 'приват']
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
                'restaurant_keywords': ['святков', 'простор', 'банкет', 'торжеств', 'груп']
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
            logger.info("🔍 Контекст не визначено, повертаю всі ресторани")
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
            'стейк': ['стейк', 'steak', ' м\'ясо'],
            'риба': [' риб', 'fish', 'лосось'],
            'курка': [' курк', 'курчат', 'chicken'],
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
            
            # 🔎 КОМПЛЕКСНИЙ АНАЛІЗ ПО ВСІХ КОЛОНКАХ
            has_specific_criteria, relevant_restaurants, analysis_explanation = self._comprehensive_content_analysis(user_request)
            
            if has_specific_criteria:
                # Знайдено специфічні критерії - використовуємо тільки релевантні заклади
                logger.info(f"🎯 ВИКОРИСТОВУЮ КОМПЛЕКСНИЙ АНАЛІЗ: {analysis_explanation}")
                shuffled_restaurants = [item['restaurant'] for item in relevant_restaurants]
                logger.info(f"📊 Відібрано {len(shuffled_restaurants)} найрелевантніших закладів")
            else:
                # Не знайдено специфічних критеріїв - перевіряємо чи це запит про конкретну страву
                logger.info("🔍 Комплексний аналіз не знайшов критеріїв, перевіряю конкретні страви...")
                
                has_dish, dishes_info = self._check_dish_availability(user_request)
                
                # Якщо користувач шукав конкретні страви
                if dishes_info:  # Якщо були знайдені конкретні страви в запиті
                    if not has_dish:  # Але їх немає в меню ресторанів
                        missing_dishes = ", ".join(dishes_info)
                        logger.warning(f"❌ ВІДСУТНЯ СТРАВА: користувач шукав '{missing_dishes}', але її немає в жодному ресторані")
                        
                        return {
                            "dish_not_found": True,
                            "missing_dishes": missing_dishes,
                            "message": f"На жаль, {missing_dishes} ще немає в нашому переліку. Спробуй іншу страву!"
                        }
                    else:  # Страви є - фільтруємо тільки ресторани з цими стравами
                        logger.info(f"🎯 ФОКУС НА СТРАВАХ: користувач шукав '{dishes_info}' - фільтрую тільки ресторани з цими стравами")
                        # Фільтруємо shuffled_restaurants до тільки тих, що мають потрібні страви
                        dish_filtered_restaurants = []
                        for restaurant in shuffled_restaurants:
                            menu_text = restaurant.get('menu', '').lower()
                            has_required_dish = False
                            
                            for dish in dishes_info:
                                dish_keywords = self._get_dish_keywords(dish)
                                for keyword in dish_keywords:
                                    if ENHANCED_SEARCH_CONFIG['regex_boundaries']:
                                        pattern = r'\b' + re.escape(keyword.lower()) + r'\b'
                                        if re.search(pattern, menu_text):
                                            has_required_dish = True
                                            logger.info(f"   ✅ {restaurant.get('name', '')} має {dish}")
                                            break
                                    else:
                                        if keyword.lower() in menu_text:
                                            has_required_dish = True
                                            logger.info(f"   ✅ {restaurant.get('name', '')} має {dish}")
                                            break
                                if has_required_dish:
                                    break
                            
                            if has_required_dish:
                                dish_filtered_restaurants.append(restaurant)
                        
                        if not dish_filtered_restaurants:
                            logger.error(f"❌ КРИТИЧНА ПОМИЛКА: функція сказала що страви є, але фільтр не знайшов ресторанів")
                            return {
                                "dish_not_found": True,
                                "missing_dishes": ", ".join(dishes_info),
                                "message": f"На жаль, {', '.join(dishes_info)} ще немає в нашому переліку. Спробуй іншу страву!"
                            }
                        
                        logger.info(f"🍽️ Відфільтровано до {len(dish_filtered_restaurants)} ресторанів з потрібними стравами з {len(shuffled_restaurants)}")
                        shuffled_restaurants = dish_filtered_restaurants
            
            # ТРЬОХЕТАПНА ФІЛЬТРАЦІЯ для максимальної точності:
            
            # 1. Спочатку фільтруємо за ТИПОМ ЗАКЛАДУ (покращено!)
            if ENHANCED_SEARCH_CONFIG['enabled']:
                type_filtered = self._enhanced_filter_by_establishment_type(user_request, shuffled_restaurants)
            else:
                type_filtered = self._filter_by_establishment_type(user_request, shuffled_restaurants)
            
            # 2. Потім фільтруємо за КОНТЕКСТОМ
            context_filtered = self._filter_by_context(user_request, type_filtered)
            
            # 3. Наreshті фільтруємо по МЕНЮ
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
                logger.warning("⚠️ Не вдалось розпарсити відповідь OpenAI, використовую резервний алгоритм")
                return self._fallback_dual_selection(user_request, final_filtered)
            
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

# Глобальний екземпляр покращеного бота
restaurant_bot = EnhancedRestaurantBot()

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
            # Перевіряємо чи це повідомлення про відсутність страви
            if recommendation.get("dish_not_found"):
                await update.message.reply_text(
                    f"😔 {recommendation['message']}\n\n"
                    f"Спробуй знайти щось інше або напиши /start для нового пошуку!"
                )
                logger.info(f"❌ Повідомлено користувачу {user_id} про відсутність страви: {recommendation['missing_dishes']}")
                return
            
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
                    logger.info(f"✅ Надіслано рекомендацію з фото: {main_restaurant['name']}")
                except Exception as photo_error:
                    logger.warning(f"⚠️ Не вдалося надіслати фото: {photo_error}")
                    response_text += f"\n\n📸 <a href='{main_photo_url}'>Переглянути фото пріоритетного ресторану</a>"
                    await update.message.reply_text(response_text, parse_mode='HTML')
                    logger.info(f"✅ Надіслано рекомендацію з посиланням на фото: {main_restaurant['name']}")
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
        
        # Додаємо інформацію про покращення
        enhanced_status = "✅ Увімкнено" if ENHANCED_SEARCH_CONFIG['enabled'] else "❌ Вимкнено"
        fuzzy_status = "✅ Увімкнено" if (ENHANCED_SEARCH_CONFIG['fuzzy_matching'] and FUZZY_AVAILABLE) else "❌ Вимкнено"
        
        stats_text = f"""📊 <b>Статистика бота</b>

📈 Загальна кількість запитів: <b>{summary_data[1][1]}</b>
👥 Кількість унікальних користувачів: <b>{summary_data[2][1]}</b>
⭐ Середня оцінка відповідності: <b>{summary_data[3][1]}</b>
📢 Кількість оцінок: <b>{summary_data[4][1]}</b>
📊 Середня кількість запитів на користувача: <b>{summary_data[5][1]}</b>

🔧 <b>Покращений пошук:</b>
• Статус: {enhanced_status}
• Fuzzy matching: {fuzzy_status}
• Negation detection: {'✅' if ENHANCED_SEARCH_CONFIG['negation_detection'] else '❌'}
• Regex boundaries: {'✅' if ENHANCED_SEARCH_CONFIG['regex_boundaries'] else '❌'}

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
    
    logger.info("🚀 Запускаю покращений бота...")
    
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        logger.info("✅ Telegram додаток створено успішно!")
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("stats", stats_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)
        
        logger.info("🔗 Підключаюсь до Google Sheets...")
        loop.run_until_complete(restaurant_bot.init_google_sheets())
        
        # Логуємо конфігурацію покращеного пошуку
        logger.info(f"🔧 Конфігурація покращеного пошуку: {ENHANCED_SEARCH_CONFIG}")
        if FUZZY_AVAILABLE:
            logger.info("✅ Fuzzy matching доступний")
        else:
            logger.warning("⚠️ Fuzzy matching недоступний - встановіть fuzzywuzzy: pip install fuzzywuzzy")
        
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
