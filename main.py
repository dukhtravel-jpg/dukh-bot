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
            'сімейний': ['сім\'я', 'сімейн', 'діти', 'родина', 'дитячий', 'для всієї сім\'ї'],
            'веселий': ['весел', 'жвавий', 'енергійний', 'гучний', 'драйвовий', 'молодіжний'],
            
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
                    
                logger.info("✅ Додано початкові данні до Summary")
            
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

    def _comprehensive_content_analysis(self, user_request: str) -> Tuple[bool, List[Dict], str]:
        """
        Покращений комплексний аналіз запиту користувача по ВСІХ колонках таблиці
        
        Returns:
            (знайдено_релевантні_заклади, список_закладів_з_оцінками, пояснення)
        """
        user_lower = user_request.lower()
        logger.info(f"🔎 ПОКРАЩЕНИЙ КОМПЛЕКСНИЙ АНАЛІЗ: '{user_request}'")
        
        # Розширені критерії пошуку по всіх колонках
        search_criteria = {
            # 🍵 Напої та специфічні речі
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
            'чай': {
                'keywords': ['чай', 'tea', 'травяний', 'зелений чай', 'чорний чай'],
                'columns': ['menu', 'aim', 'cuisine', 'name'],
                'weight': 2.5
            },
            
            # 🍽️ Страви та кухня
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
            'паста': {
                'keywords': ['паста', 'pasta', 'спагетті', 'карбонара', 'болоньєзе', 'італійська кухня'],
                'columns': ['menu', 'cuisine', 'vibe', 'name'],
                'weight': 2.8
            },
            'мідії': {
                'keywords': ['мідії', 'мидии', 'мідії', 'молюски', 'мідій', 'морепродукти'],
                'columns': ['menu', 'cuisine', 'name'],
                'weight': 3.2
            },
            'стейк': {
                'keywords': ['стейк', 'steak', "м'ясо", 'біфштекс', 'філе'],
                'columns': ['menu', 'cuisine', 'name'],
                'weight': 2.8
            },
            'бургер': {
                'keywords': ['бургер', 'burger', 'гамбургер', 'чізбургер'],
                'columns': ['menu', 'cuisine', 'name'],
                'weight': 2.5
            },
            'салат': {
                'keywords': ['салат', 'salad', 'свіжий', 'овочі'],
                'columns': ['menu', 'cuisine', 'name'],
                'weight': 2.0
            },
            'десерт': {
                'keywords': ['десерт', 'торт', 'тірамісу', 'морозиво', 'чізкейк', 'солодке'],
                'columns': ['menu', 'cuisine', 'name'],
                'weight': 2.2
            },
            
            # 🏢 Типи закладів
            'ресторан': {
                'keywords': ['ресторан', 'ресторани', 'ресторанчик', 'їдальня', 'заклад'],
                'columns': ['type', 'тип закладу', 'aim', 'name'],
                'weight': 2.5
            },
            "кав'ярня": {
                'keywords': ["кав'ярня", 'кафе', 'coffee shop', 'кавова', 'кавня'],
                'columns': ['type', 'тип закладу', 'aim', 'name'],
                'weight': 2.5
            },
            'бар': {
                'keywords': ['бар', 'паб', 'таверна', 'випити', 'алкоголь'],
                'columns': ['type', 'тип закладу', 'aim', 'vibe'],
                'weight': 2.3
            },
            
            # 💕 Атмосфера та настрій
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
            'друзі': {
                'keywords': ['друз', 'компан', 'гуртом', 'веселитися'],
                'columns': ['aim', 'vibe', 'name'],
                'weight': 2.3
            },
            'весело': {
                'keywords': ['весел', 'жвав', 'енергійн', 'гучн', 'драйв', 'молодіжн'],
                'columns': ['vibe', 'aim'],
                'weight': 2.0
            },
            'затишно': {
                'keywords': ['затишн', 'тих', 'спокійн', 'релакс', 'домашн'],
                'columns': ['vibe', 'aim'],
                'weight': 2.0
            },
            
            # 🎯 Призначення та активності
            'працювати': {
                'keywords': ['працювати', 'попрацювати', 'робота', 'ноутбук', 'wifi', 'фріланс'],
                'columns': ['aim', 'vibe'],
                'weight': 2.8
            },
            'зустріч': {
                'keywords': ['зустріч', 'переговори', 'бізнес', 'ділов', 'офіційн'],
                'columns': ['aim', 'vibe'],
                'weight': 2.5
            },
            'сніданок': {
                'keywords': ['сніданок', 'ранок', 'зранку', 'morning'],
                'columns': ['aim', 'menu'],
                'weight': 2.3
            },
            'обід': {
                'keywords': ['обід', 'пообідати', 'lunch'],
                'columns': ['aim'],
                'weight': 2.0
            },
            'вечеря': {
                'keywords': ['вечер', 'повечеряти', 'dinner'],
                'columns': ['aim'],
                'weight': 2.0
            },
            'святкування': {
                'keywords': ['святкув', 'день народж', 'ювілей', 'свято', 'торжеств'],
                'columns': ['aim', 'vibe'],
                'weight': 2.5
            },
            
            # 🌍 Кухні світу
            'італійський': {
                'keywords': ['італ', 'italian', 'італія'],
                'columns': ['cuisine', 'vibe', 'name'],
                'weight': 2.5
            },
            'японський': {
                'keywords': ['япон', 'japanese', 'азійськ'],
                'columns': ['cuisine', 'vibe', 'name'],
                'weight': 2.5
            },
            'грузинський': {
                'keywords': ['грузин', 'georgian', 'кавказьк', 'хачапурі', 'хінкалі'],
                'columns': ['cuisine', 'vibe', 'name'],
                'weight': 2.5
            },
            'французький': {
                'keywords': ['франц', 'french'],
                'columns': ['cuisine', 'vibe', 'name'],
                'weight': 2.3
            },
            'американський': {
                'keywords': ['америк', 'american', 'usa'],
                'columns': ['cuisine', 'vibe', 'name'],
                'weight': 2.0
            },
            'мексиканський': {
                'keywords': ['мексик', 'mexican', 'буріто', 'тако'],
                'columns': ['cuisine', 'vibe', 'name'],
                'weight': 2.3
            },
            'турецький': {
                'keywords': ['турец', 'turkish', 'кебаб', 'донер'],
                'columns': ['cuisine', 'vibe', 'name'],
                'weight': 2.3
            },
            
            # ⚡ Контекст швидкості
            'швидко': {
                'keywords': ['швидко', 'швидку', 'швидкий', 'fast', 'перекус', 'поспішаю', 'на швидку руку'],
                'columns': ['aim', 'type', 'тип закладу'],
                'weight': 2.5
            },
            'доставка': {
                'keywords': ['доставка', 'додому', 'замовити', 'привезти', 'delivery', 'не хочу йти', 'вдома'],
                'columns': ['type', 'тип закладу', 'aim'],
                'weight': 2.5
            },
            
            # 🏙️ Локація та оточення
            'центр': {
                'keywords': ['центр', 'центральн', 'downtown'],
                'columns': ['address', 'name'],
                'weight': 1.8
            },
            'тераса': {
                'keywords': ['тераса', 'літня', 'веранда', 'надворі', 'outdoor'],
                'columns': ['vibe', 'aim', 'menu'],
                'weight': 2.0
            },
            'красивий_вид': {
                'keywords': ['вид', 'краєвид', 'панорам', 'view'],
                'columns': ['vibe', 'name'],
                'weight': 1.8
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
                            logger.info(f"   ✅ {restaurant.get('name', '')} має '{criterion_name}' в колонці '{column}': {column_text[:50]}...")
                        
                        # Додаткова перевірка з fuzzy matching
                        elif ENHANCED_SEARCH_CONFIG['fuzzy_matching'] and FUZZY_AVAILABLE:
                            for keyword in keywords:
                                if len(keyword) > 3:
                                    for word in column_text.split():
                                        if len(word) > 3:
                                            fuzzy_score = fuzz.ratio(keyword.lower(), word)
                                            if fuzzy_score >= 85:
                                                restaurant_has_criterion = True
                                                matched_columns.append(f"{column}(fuzzy)")
                                                logger.info(f"   🔍 {restaurant.get('name', '')} має '{criterion_name}' через fuzzy в '{column}': {keyword}≈{word}")
                                                break
                                    if restaurant_has_criterion:
                                        break
                        
                        if restaurant_has_criterion:
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
            # Беремо заклади з найвищими оцінками (топ 70% від найкращої оцінки)
            top_score = restaurant_scores[0]['score']
            threshold = top_score * 0.7
            top_restaurants = [item for item in restaurant_scores if item['score'] >= threshold]
            
            # Додаємо детальне пояснення
            explanation = f"знайдено {len(top_restaurants)} найрелевантніших закладів (оцінка {threshold:.1f}+)"
            logger.info(f"🎉 ПОКРАЩЕНИЙ КОМПЛЕКСНИЙ АНАЛІЗ: {explanation}")
            
            return True, top_restaurants, explanation
        else:
            logger.info("🤔 ПОКРАЩЕНИЙ КОМПЛЕКСНИЙ АНАЛІЗ: не знайдено специфічних критеріїв")
            return False, [], "не знайдено специфічних критеріїв"

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
            'паста': ['паста', 'спагетті', 'pasta', 'спагетті', 'макарони'],
            'бургер': ['бургер', 'burger', 'гамбургер', 'чізбургер'],
            'суші': ['суші', 'sushi', 'роли', 'ролл', 'сашімі'],
            'салат': ['салат', 'salad'],
            'хумус': ['хумус', 'hummus'],
            'фалафель': ['фалафель', 'falafel'],
            'шаурма': ['шаурм', 'shawarma', 'шаверма'],
            'стейк': ['стейк', 'steak', 'м\'ясо', 'біфштекс'],
            'риба': ['риба', 'fish', 'лосось', 'семга', 'тунець', 'форель'],
            'курка': ['курк', 'курчат', 'chicken', 'курица'],
            'десерт': ['десерт', 'торт', 'тірамісу', 'морозиво', 'чізкейк', 'тісточко'],
            'мідії': ['мідії', 'мидии', 'мідіі', 'молюски', 'мідій'],
            'креветки': ['креветки', 'креветка', 'shrimp', 'prawns'],
            'устриці': ['устриці', 'устрица', 'oysters'],
            'кальмари': ['кальмари', 'кальмари', 'squid'],
            'равіолі': ['равіолі', 'ravioli', 'равиоли'],
            'лазанья': ['лазанья', 'lasagna', 'лазања'],
            'різотто': ['різотто', 'risotto', 'ризотто'],
            'гнокі': ['гноки', 'gnocchi', 'нькі'],
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

    def _get_dish_keywords(self, dish: str) -> List[str]:
        """Повертає список ключових слів для конкретної страви"""
        food_keywords = {
            'піца': ['піца', 'піцц', 'pizza', 'піци', 'піззу'],
            'паста': ['паста', 'спагетті', 'pasta', 'спагетті', 'макарони'],
            'бургер': ['бургер', 'burger', 'гамбургер', 'чізбургер'],
            'суші': ['суші', 'sushi', 'роли', 'ролл', 'сашімі'],
            'салат': ['салат', 'salad'],
            'хумус': ['хумус', 'hummus'],
            'фалафель': ['фалафель', 'falafel'],
            'шаурма': ['шаурм', 'shawarma', 'шаверма'],
            'стейк': ['стейк', 'steak', 'м\'ясо', 'біфштекс'],
            'риба': ['риба', 'fish', 'лосось', 'семга', 'тунець', 'форель'],
            'курка': ['курк', 'курчат', 'chicken', 'курица'],
            'десерт': ['десерт', 'торт', 'тірамісу', 'морозиво', 'чізкейк', 'тісточко'],
            'мідії': ['мідії', 'мидии', 'мідіі', 'молюски', 'мідій'],
            'креветки': ['креветки', 'креветка', 'shrimp', 'prawns'],
            'устриці': ['устриці', 'устрица', 'oysters'],
            'кальмари': ['кальмари', 'кальмари', 'squid'],
            'равіолі': ['равіолі', 'ravioli', 'равиоли'],
            'лазанья': ['лазанья', 'lasagna', 'лазања'],
            'різотто': ['різотто', 'risotto', 'ризотто'],
            'гнокі': ['гноки', 'gnocchi', 'нькі'],
            'тартар': ['тартар', 'tartar'],
            'карпачо': ['карпачо', 'carpaccio'],
        }
        
        return food_keywords.get(dish, [dish])

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
        
        return len(found_synonyms) > 0, max_confidence, found_synonyms

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
            logger.warning(f"🏢 ENHANCED: ПРОБЛЕМА! Жоден заклад не підходить за типом, повертаю всі {len(restaurant_list)} заклади")
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
                'user_keywords': ['діл', 'зустріч', 'переговор', 'бізнес', 'робоч', 'офіс'],
                'restaurant_keywords': ['діл', 'зустріч', 'бізнес', 'переговор', 'офіц']
            },
            'friends': {
                'user_keywords': ['друз', 'компан', 'гуртом', 'весел', 'тусовк'],
                'restaurant_keywords': ['компан', 'друз', 'молодіжн', 'весел', 'гучн']
            },
            'celebration': {
                'user_keywords': ['святкув', 'день народж', 'ювілей', 'свято', 'торжеств'],
                'restaurant_keywords': ['святков', 'простор', 'банкет', 'торжеств', 'груп']
            },
            'quick': {
                'user_keywords': ['швидк', 'перекус', 'фаст', 'поспішаю', 'на швидку руку'],
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
            'паста': [' паст', 'спагетті', 'pasta'],
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
            
            # Визначаємо фінальний список закладів на основі типу аналізу
            if has_specific_criteria:
                # Комплексний аналіз вже відібрав найкращі заклади
                final_filtered = shuffled_restaurants
                logger.info(f"🎯 КОМПЛЕКСНИЙ РЕЗУЛЬТАТ: використовую {len(final_filtered)} попередньо відфільтрованих закладів")
            else:
                # Стандартна трьохетапна фільтрація для загальних запитів
                logger.info("📋 СТАНДАРТНА ФІЛЬТРАЦІЯ: загальний запит без специфічних критеріїв")
                
                # 1. Фільтруємо за ТИПОМ ЗАКЛАДУ (покращено!)
                if ENHANCED_SEARCH_CONFIG['enabled']:
                    type_filtered = self._enhanced_filter_by_establishment_type(user_request, shuffled_restaurants)
                else:
                    type_filtered = self._filter_by_establishment_type(user_request, shuffled_restaurants)
                
                # 2. Потім фільтруємо за КОНТЕКСТОМ
                context_filtered = self._filter_by_context(user_request, type_filtered)
                
                # 3. Наешті фільтруємо по МЕНЮ (якщо не зроблено вище)
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
                logger.warning("⚠️ Не вдалося розпарсити відповідь OpenAI, використовую резервний алгоритм")
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
    
    # Сортуємо типи за кількістю закладів (найбільше спочатку)
    sorted_types = sorted(grouped_restaurants.items(), key=lambda x: len(x[1]), reverse=True)
    
    for establishment_type, restaurants in sorted_types:
        count = len(restaurants)
        
        # Іконки для різних типів
        icon = {
            'ресторан': '🍽️',
            'кав\'ярня': '☕',
            'кафе': '☕',
            'доставка': '🚚',
            'delivery': '🚚',
            'to-go': '🥡',
            'takeaway': '🥡',
            'бар': '🍸'
        }.get(establishment_type.lower(), '🪗')
        
        message_parts.append(f"\n{icon} <b>{establishment_type.upper()}</b> ({count})")
        
        # Додаємо перші 5 ресторанів кожного типу
        for i, restaurant in enumerate(restaurants[:5]):
            name = restaurant.get('name', 'Без назви')
            cuisine = restaurant.get('cuisine', '')
            if cuisine:
                message_parts.append(f"   • {name} <i>({cuisine})</i>")
            else:
                message_parts.append(f"   • {name}")
        
        # Якщо ресторанів більше 5, показуємо "..."
        if count > 5:
            message_parts.append(f"   • ... та ще {count - 5}")
    
    total_count = len(restaurant_bot.restaurants_data)
    message_parts.append(f"\n📊 <b>Загалом:</b> {total_count} закладів")
    message_parts.append(f"🔍 Для пошуку просто напишіть що шукаєте!")
    
    full_message = '\n'.join(message_parts)
    
    # Перевіряємо довжину повідомлення (Telegram ліміт ~4096 символів)
    if len(full_message) > 4000:
        # Якщо занадто довге, відправляємо скорочену версію
        short_message_parts = ["🏢 <b>Заклади за типами (скорочено):</b>\n"]
        for establishment_type, restaurants in sorted_types:
            count = len(restaurants)
            icon = {
                'ресторан': '🍽️',
                'кав\'ярня': '☕',
                'кафе': '☕',
                'доставка': '🚚',
                'to-go': '🥡',
                'бар': '🍸'
            }.get(establishment_type.lower(), '🪗')
            short_message_parts.append(f"{icon} <b>{establishment_type}</b>: {count} закладів")
        
        short_message_parts.append(
