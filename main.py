import requests
import sqlite3
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from openai import OpenAI
import os
import json
import sys
from pathlib import Path
from threading import Lock

# ================= ЗАГРУЗКА .env =================
def load_env_file():
    env_path = os.path.join(os.getcwd(), '.env')
    if os.path.exists(env_path):
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if '=' in line:
                        key, value = line.split('=', 1)
                        os.environ[key.strip()] = value.strip()
        print(f"✅ .env file loaded from {env_path}")
        return True
    else:
        print(f"⚠️ .env file not found at {env_path}")
        return False

load_env_file()

# ================= КОНФИГ =================
class Config:
    OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID", "").strip()
    OZON_API_KEY = os.getenv("OZON_API_KEY", "").strip()
    OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
    
    if not OZON_CLIENT_ID or not OZON_API_KEY:
        print("="*60)
        print("❌ ОШИБКА: Переменные OZON_CLIENT_ID или OZON_API_KEY не найдены!")
        print(f"   OZON_CLIENT_ID: {'✅' if OZON_CLIENT_ID else '❌'} ({OZON_CLIENT_ID})")
        print(f"   OZON_API_KEY: {'✅' if OZON_API_KEY else '❌'} ({OZON_API_KEY[:10] if OZON_API_KEY else 'empty'}...)")
        print("="*60)
        sys.exit(1)
    
    OZON_BASE_URL = "https://api-seller.ozon.ru"
    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
    
    POLL_INTERVAL = 60
    QUESTIONS_LIMIT = 100
    AI_MODEL = os.getenv("AI_MODEL", "minimax/minimax-m3").strip()
    AI_MAX_WORDS = int(os.getenv("AI_MAX_WORDS", "45"))
    PROCESSING_MODE = os.getenv("PROCESSING_MODE", "semi_automatic").strip().lower()
    TEST_MODE = PROCESSING_MODE == "test"

    @staticmethod
    def get_processing_mode() -> str:
        mode = os.getenv("PROCESSING_MODE", "semi_automatic").strip().lower()
        if mode not in {"test", "semi_automatic", "automatic"}:
            mode = "semi_automatic"
        return mode

print(f"✅ Конфигурация загружена:")
print(f"   OZON_CLIENT_ID: {Config.OZON_CLIENT_ID}")
print(f"   OZON_API_KEY: {Config.OZON_API_KEY[:10]}...")

# ================= ЛОГГИРОВАНИЕ =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('ozon_ai_helper.log', encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ================= БАЗА ДАННЫХ =================
class Database:
    def __init__(self, db_path=None):
        self.db_path = db_path or os.getenv("DB_PATH", "ozon_data.db")
        self.init_db()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path)
    
    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Таблица вопросов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS questions (
                    question_id TEXT PRIMARY KEY,
                    sku TEXT,
                    question_text TEXT,
                    answer_text TEXT,
                    is_answered BOOLEAN DEFAULT 0,
                    is_ai_generated BOOLEAN DEFAULT 0,
                    is_processed BOOLEAN DEFAULT 0,
                    pending_ai_review BOOLEAN DEFAULT 0,
                    need_manual_review BOOLEAN DEFAULT 0,
                    published_at TEXT,
                    status TEXT,
                    author_name TEXT,
                    answers_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cursor.execute("PRAGMA table_info(questions)")
            question_columns = [col[1] for col in cursor.fetchall()]
            if 'pending_ai_review' not in question_columns:
                cursor.execute('ALTER TABLE questions ADD COLUMN pending_ai_review BOOLEAN DEFAULT 0')
            
            # Таблица для логов ИИ
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ai_test_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question_id TEXT,
                    sku TEXT,
                    question_text TEXT,
                    generated_answer TEXT,
                    context_reviews TEXT,
                    context_answers TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица ответов (исправлена: добавлена колонка status_publication)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS question_answers (
                    answer_id TEXT PRIMARY KEY,
                    question_id TEXT,
                    sku TEXT,
                    answer_text TEXT,
                    author_name TEXT,
                    published_at TEXT,
                    is_ai_generated BOOLEAN DEFAULT 0,
                    status_publication TEXT
                )
            ''')
            
            # Таблица метаданных синхронизации
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sync_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            # Записываем время последней синхронизации (если нет)
            cursor.execute('''
                INSERT OR IGNORE INTO sync_metadata (key, value) VALUES ('last_sync_time', '')
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sku_instructions (
                    sku TEXT PRIMARY KEY,
                    instruction_text TEXT,
                    source_file_name TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            cursor.execute("PRAGMA table_info(sku_instructions)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'source_file_name' not in columns:
                cursor.execute('ALTER TABLE sku_instructions ADD COLUMN source_file_name TEXT')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sku_economics (
                    sku TEXT PRIMARY KEY,
                    cost REAL DEFAULT 0,
                    commission_rate REAL DEFAULT 0,
                    logistics REAL DEFAULT 0,
                    tax_rate REAL DEFAULT 0.06,
                    other_expenses REAL DEFAULT 0,
                    minimum_margin_rate REAL DEFAULT 0.20,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS competitors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku TEXT NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT,
                    current_price REAL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS ozon_data_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    data_type TEXT NOT NULL,
                    scope_key TEXT DEFAULT '',
                    payload_json TEXT NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_ozon_snapshots_type_time
                ON ozon_data_snapshots (data_type, captured_at DESC)
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sku_catalog (
                    sku TEXT PRIMARY KEY,
                    short_name TEXT DEFAULT '',
                    ozon_name TEXT DEFAULT '',
                    seller_article TEXT DEFAULT '',
                    image_url TEXT DEFAULT '',
                    local_image_path TEXT DEFAULT '',
                    image_cached_at TEXT DEFAULT '',
                    category_id TEXT DEFAULT '',
                    category_name TEXT DEFAULT '',
                    is_visible BOOLEAN DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            cursor.execute("PRAGMA table_info(sku_catalog)")
            catalog_columns = [col[1] for col in cursor.fetchall()]
            if "ozon_name" not in catalog_columns:
                cursor.execute("ALTER TABLE sku_catalog ADD COLUMN ozon_name TEXT DEFAULT ''")
            if "seller_article" not in catalog_columns:
                cursor.execute("ALTER TABLE sku_catalog ADD COLUMN seller_article TEXT DEFAULT ''")
            if "image_url" not in catalog_columns:
                cursor.execute("ALTER TABLE sku_catalog ADD COLUMN image_url TEXT DEFAULT ''")
            if "local_image_path" not in catalog_columns:
                cursor.execute("ALTER TABLE sku_catalog ADD COLUMN local_image_path TEXT DEFAULT ''")
            if "image_cached_at" not in catalog_columns:
                cursor.execute("ALTER TABLE sku_catalog ADD COLUMN image_cached_at TEXT DEFAULT ''")
            if "category_id" not in catalog_columns:
                cursor.execute("ALTER TABLE sku_catalog ADD COLUMN category_id TEXT DEFAULT ''")
            if "category_name" not in catalog_columns:
                cursor.execute("ALTER TABLE sku_catalog ADD COLUMN category_name TEXT DEFAULT ''")

            # Отзывы пропускаем
            conn.commit()
            logger.info("База данных инициализирована")
    
    def get_last_sync_time(self) -> Optional[str]:
        """Получить время последней синхронизации (максимальный published_at из вопросов)"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(published_at) FROM questions")
            row = cursor.fetchone()
            return row[0] if row and row[0] else None
    
    def get_instruction(self, sku: str) -> Optional[str]:
        """Получить инструкцию для SKU, если есть"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT instruction_text FROM sku_instructions WHERE sku = ?", (sku,))
            row = cursor.fetchone()
            return row[0] if row else None
    
    def set_instruction(self, sku: str, instruction_text: str, source_file_name: Optional[str] = None):
        """Установить или обновить инструкцию для SKU"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT OR REPLACE INTO sku_instructions (sku, instruction_text, source_file_name, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (sku, instruction_text, source_file_name or ""))
            conn.commit()
    
    def get_setting(self, key: str, default: str = "") -> str:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else default

    def get_processing_mode(self) -> str:
        mode = self.get_setting("processing_mode", "semi_automatic").strip().lower()
        if mode not in {"test", "semi_automatic", "automatic"}:
            mode = "semi_automatic"
        return mode

    def set_processing_mode(self, mode: str):
        normalized = mode.strip().lower()
        if normalized not in {"test", "semi_automatic", "automatic"}:
            normalized = "semi_automatic"
        self.set_setting("processing_mode", normalized)
        os.environ["PROCESSING_MODE"] = normalized

    def set_setting(self, key: str, value: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
            conn.commit()

    def sync_sku_catalog(self, skus: List[str]):
        clean_skus = sorted({str(sku).strip() for sku in skus if str(sku).strip()})
        if not clean_skus:
            return
        with self.get_connection() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO sku_catalog (sku, short_name, is_visible) VALUES (?, '', 0)",
                [(sku,) for sku in clean_skus],
            )
            conn.commit()

    def list_sku_catalog(self) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute(
                "SELECT sku, ozon_name, seller_article, image_url, local_image_path, image_cached_at, category_id, category_name, is_visible FROM sku_catalog ORDER BY CAST(sku AS INTEGER), sku"
            ).fetchall()
        return [dict(zip(("sku", "ozon_name", "seller_article", "image_url", "local_image_path", "image_cached_at", "category_id", "category_name", "is_visible"), row)) for row in rows]

    def update_sku_catalog(self, sku: str, is_visible: bool):
        with self.get_connection() as conn:
            conn.execute('''
                INSERT INTO sku_catalog (sku, is_visible, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(sku) DO UPDATE SET
                    is_visible = excluded.is_visible,
                    updated_at = CURRENT_TIMESTAMP
            ''', (str(sku).strip(), 1 if is_visible else 0))
            conn.commit()

    def update_sku_product_info(
        self,
        sku: str,
        ozon_name: str,
        seller_article: str,
        image_url: str = "",
        category_id: str = "",
        category_name: str = "",
        local_image_path: Optional[str] = None,
        image_cached_at: Optional[str] = None,
    ):
        with self.get_connection() as conn:
            conn.execute('''
                UPDATE sku_catalog
                SET ozon_name = ?, seller_article = ?, image_url = ?,
                    local_image_path = CASE WHEN ? IS NULL THEN local_image_path ELSE ? END,
                    image_cached_at = CASE WHEN ? IS NULL THEN image_cached_at ELSE ? END,
                    category_id = ?, category_name = ?, updated_at = CURRENT_TIMESTAMP
                WHERE sku = ?
            ''', (
                ozon_name.strip(),
                seller_article.strip(),
                image_url.strip(),
                None if local_image_path is None else local_image_path.strip(),
                None if local_image_path is None else local_image_path.strip(),
                None if image_cached_at is None else image_cached_at.strip(),
                None if image_cached_at is None else image_cached_at.strip(),
                category_id.strip(),
                category_name.strip(),
                str(sku).strip(),
            ))
            conn.commit()

    def delete_sku_catalog(self, sku: str):
        with self.get_connection() as conn:
            conn.execute("DELETE FROM sku_catalog WHERE sku = ?", (str(sku).strip(),))
            conn.commit()

    def save_ozon_snapshot(self, data_type: str, payload, scope_key: str = "") -> bool:
        """Save a raw Ozon response unless the same response was saved recently."""
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        with self.get_connection() as conn:
            recent = conn.execute('''
                SELECT payload_json FROM ozon_data_snapshots
                WHERE data_type = ? AND scope_key = ?
                  AND captured_at >= datetime('now', '-60 seconds')
                ORDER BY id DESC LIMIT 1
            ''', (str(data_type), str(scope_key))).fetchone()
            if recent and recent[0] == payload_json:
                return False
            conn.execute('''
                INSERT INTO ozon_data_snapshots (data_type, scope_key, payload_json)
                VALUES (?, ?, ?)
            ''', (str(data_type), str(scope_key), payload_json))
            conn.commit()
        return True

    def list_ozon_snapshot_types(self) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute('''
                SELECT data_type, COUNT(*), MAX(captured_at)
                FROM ozon_data_snapshots
                GROUP BY data_type
                ORDER BY MAX(captured_at) DESC
            ''').fetchall()
        return [
            {"data_type": row[0], "count": row[1], "last_captured_at": row[2]}
            for row in rows
        ]

    def list_ozon_snapshots(self, data_type: str = "", limit: int = 100) -> List[Dict]:
        query = '''
            SELECT id, captured_at, data_type, scope_key, payload_json
            FROM ozon_data_snapshots
        '''
        values = []
        if data_type:
            query += " WHERE data_type = ?"
            values.append(data_type)
        query += " ORDER BY captured_at DESC, id DESC LIMIT ?"
        values.append(max(1, min(int(limit), 500)))
        with self.get_connection() as conn:
            rows = conn.execute(query, values).fetchall()
        return [
            {
                "id": row[0],
                "captured_at": row[1],
                "data_type": row[2],
                "scope_key": row[3],
                "payload_json": row[4],
            }
            for row in rows
        ]

    def get_sku_economics(self, sku: str) -> Dict[str, float]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT cost, commission_rate, logistics, tax_rate, other_expenses, minimum_margin_rate
                FROM sku_economics WHERE sku = ?
            ''', (str(sku),))
            row = cursor.fetchone()
        if not row:
            return {
                "cost": 0.0,
                "commission_rate": 0.0,
                "logistics": 0.0,
                "tax_rate": 0.06,
                "other_expenses": 0.0,
                "minimum_margin_rate": 0.20,
            }
        keys = ("cost", "commission_rate", "logistics", "tax_rate", "other_expenses", "minimum_margin_rate")
        return dict(zip(keys, row))

    def set_sku_economics(self, sku: str, values: Dict[str, float]):
        with self.get_connection() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO sku_economics
                (sku, cost, commission_rate, logistics, tax_rate, other_expenses, minimum_margin_rate, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (
                str(sku), values["cost"], values["commission_rate"], values["logistics"],
                values["tax_rate"], values["other_expenses"], values["minimum_margin_rate"],
            ))
            conn.commit()

    def list_competitors(self) -> List[Dict]:
        with self.get_connection() as conn:
            rows = conn.execute("SELECT id, sku, name, url, current_price, updated_at FROM competitors ORDER BY sku, name").fetchall()
        return [dict(zip(("id", "sku", "name", "url", "current_price", "updated_at"), row)) for row in rows]

    def add_competitor(self, sku: str, name: str, url: str, current_price: float):
        with self.get_connection() as conn:
            conn.execute("INSERT INTO competitors (sku, name, url, current_price) VALUES (?, ?, ?, ?)", (sku, name, url, current_price))
            conn.commit()

    def delete_competitor(self, competitor_id: int):
        with self.get_connection() as conn:
            conn.execute("DELETE FROM competitors WHERE id = ?", (competitor_id,))
            conn.commit()

    def save_questions(self, questions_data: Dict):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for question in questions_data.get('questions', []):
                sku = str(question.get('sku', '')).strip()
                cursor.execute('''
                    INSERT OR IGNORE INTO questions 
                    (question_id, sku, question_text, status, published_at, author_name, answers_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    question.get('id'),
                    sku,
                    question.get('text', ''),
                    question.get('status', ''),
                    question.get('published_at'),
                    question.get('author_name', ''),
                    question.get('answers_count', 0)
                ))
                if sku:
                    cursor.execute(
                        "INSERT OR IGNORE INTO sku_catalog (sku, is_visible) VALUES (?, 0)",
                        (sku,),
                    )
            conn.commit()
            logger.info(f"Сохранено {len(questions_data.get('questions', []))} новых вопросов")
    
    def save_question_answers(self, question_id: str, answers: List[Dict]):
        if not answers:
            return
        with self.get_connection() as conn:
            cursor = conn.cursor()
            for answer in answers:
                cursor.execute('''
                    INSERT INTO question_answers
                    (answer_id, question_id, sku, answer_text, author_name, published_at, status_publication)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(answer_id) DO UPDATE SET
                        status_publication = excluded.status_publication,
                        published_at = excluded.published_at
                ''', (
                    answer.get('id'),
                    question_id,
                    answer.get('sku'),
                    answer.get('text'),
                    answer.get('author_name'),
                    answer.get('published_at'),
                    answer.get('status_publication') or answer.get('status', '')
                ))
            # Обновляем статус вопроса: помечаем как отвеченный
            cursor.execute('''
                UPDATE questions SET is_answered = 1, answer_text = (
                    SELECT answer_text FROM question_answers 
                    WHERE question_id = ? ORDER BY published_at DESC LIMIT 1
                ) WHERE question_id = ?
            ''', (question_id, question_id))
            conn.commit()
            logger.info(f"Сохранено {len(answers)} ответов для вопроса {question_id}")

    def get_questions_with_unfinalized_publication(self) -> List[Tuple[str, str]]:
        with self.get_connection() as conn:
            return conn.execute('''
                SELECT DISTINCT q.question_id, q.sku
                FROM questions q
                JOIN question_answers a ON a.question_id = q.question_id
                WHERE a.status_publication IN ('MODERATION', 'NOT_PUBLISHED', '')
                  AND q.sku IS NOT NULL AND q.sku != ''
            ''').fetchall()
    
    def get_unprocessed_questions(self, limit: int = 1) -> List[Tuple]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT question_id, sku, question_text, published_at
                FROM questions 
                WHERE is_answered = 0 AND is_processed = 0 AND need_manual_review = 0
                ORDER BY published_at ASC LIMIT ?
            ''', (limit,))
            return cursor.fetchall()
    
    def get_context_for_sku(self, sku: str) -> Tuple[str, str]:
        """
        Возвращает ВСЕ пары вопрос-ответ для данного SKU,
     исключая ответы, помеченные как is_ai_generated = 1,
        чтобы избежать циклических ссылок.
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT q.question_text, a.answer_text 
                FROM questions q
                JOIN question_answers a ON q.question_id = a.question_id
                WHERE q.sku = ? AND q.is_answered = 1
                  AND (a.is_ai_generated = 0 OR a.is_ai_generated IS NULL)
                ORDER BY q.published_at DESC
            ''', (sku,))
            rows = cursor.fetchall()

            if not rows:
                return "Нет отзывов", "Нет предыдущих ответов"

            context_parts = []
            for q_text, a_text in rows:
                if not q_text or not a_text:
                    continue
                context_parts.append(f"Вопрос: {q_text}\nОтвет: {a_text}")

            context_answers = "\n\n".join(context_parts) if context_parts else "Нет предыдущих ответов"
            return "Нет отзывов", context_answers
    
    def mark_question_processed_by_ai(self, question_id: str, answer_text: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE questions 
                SET is_answered = 1, is_ai_generated = 1, is_processed = 1, pending_ai_review = 0, answer_text = ?
                WHERE question_id = ?
            ''', (answer_text, question_id))
            conn.commit()

    def mark_question_pending_ai_review(self, question_id: str, answer_text: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE questions
                SET is_answered = 0, is_ai_generated = 1, is_processed = 1, pending_ai_review = 1, need_manual_review = 0, answer_text = ?
                WHERE question_id = ?
            ''', (answer_text, question_id))
            conn.commit()

    def approve_ai_answer(self, question_id: str, approved_answer: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT answer_text FROM questions WHERE question_id = ?
            ''', (question_id,))
            row = cursor.fetchone()
            original_answer = row[0] if row else ""
            is_manual_edit = bool(original_answer and approved_answer.strip() != original_answer.strip())
            cursor.execute('''
                UPDATE questions
                SET is_answered = 1,
                    is_ai_generated = 0 if ? = 1 else 1,
                    is_processed = 1,
                    pending_ai_review = 0,
                    need_manual_review = 0,
                    answer_text = ?
                WHERE question_id = ?
            ''', (1 if is_manual_edit else 0, approved_answer, question_id))
            conn.commit()
            return is_manual_edit
    
    def log_ai_test(self, question_id: str, sku: str, question_text: str, 
                   generated_answer: str, context_reviews: str, context_answers: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO ai_test_logs 
                (question_id, sku, question_text, generated_answer, context_reviews, context_answers)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (question_id, sku, question_text, generated_answer, context_reviews, context_answers))
            conn.commit()
    
    def mark_question_need_manual(self, question_id: str):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE questions
                SET need_manual_review = 1,
                    is_processed = 1,
                    pending_ai_review = 0,
                    is_answered = 0,
                    answer_text = ''
                WHERE question_id = ?
            ''', (question_id,))
            conn.commit()

# ================= API КЛИЕНТ OZON =================
class OzonAPIClient:
    def __init__(self, client_id: str, api_key: str):
        self.client_id = str(client_id).strip()
        self.api_key = str(api_key).strip()
        self.base_url = Config.OZON_BASE_URL
        self.headers = {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json"
        }
        self.last_error = ""
        self._request_lock = Lock()
        logger.info("API Client initialized")
    
    @staticmethod
    def format_ozon_date(dt):
        return dt.isoformat(timespec='milliseconds') + 'Z'
    
    def _make_request(self, endpoint: str, data: Dict = None) -> Optional[Dict]:
        url = f"{self.base_url}{endpoint}"
        json_str = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        json_bytes = json_str.encode('utf-8')
        with self._request_lock:
            for attempt in range(4):
                try:
                    response = requests.post(url, headers=self.headers, data=json_bytes, timeout=30)
                    if response.status_code == 200:
                        self.last_error = ""
                        return response.json()
                    if response.status_code == 429 and attempt < 3:
                        retry_after = response.headers.get("Retry-After")
                        delay = max(float(retry_after), 1.5) if retry_after else 1.5 * (attempt + 1)
                        logger.warning(f"Rate limit for {endpoint}; retrying in {delay:.1f}s ({attempt + 1}/3)")
                        time.sleep(delay)
                        continue
                    self.last_error = f"Ozon API {response.status_code}: {response.text[:300]}"
                    if response.status_code == 403:
                        logger.warning(f"Access denied (403) for {endpoint}: {response.text[:100]}")
                    else:
                        logger.error(f"API Error {endpoint}: {response.status_code}")
                        logger.error(f"Response: {response.text[:200]}")
                    return None
                except Exception as e:
                    self.last_error = str(e)
                    logger.error(f"Exception in {endpoint}: {e}")
                    return None
        return None
    
    def fetch_questions_paginated(self, since: Optional[str] = None, limit: int = 100) -> List[Dict]:
        """
        Загружает вопросы. Если указано since (ISO дата), то только вопросы новее этой даты.
        Иначе загружает все (но максимум 30 страниц).
        """
        all_questions = []
        last_id = ""
        has_next = True
        page = 0
        filter_data = {"status": "ALL"}
        if since:
            filter_data["date_from"] = since
            filter_data["date_to"] = datetime.now().isoformat(timespec='milliseconds') + 'Z'
            logger.info(f"Fetching questions since {since}")
        else:
            logger.info("Fetching all questions (first sync)")
        
        while has_next and page < 30:
            page += 1
            data = {
                "filter": filter_data,
                "limit": limit,
                "last_id": last_id,
                "sort_dir": "DESC"
            }
            logger.info(f"Fetching questions page {page}...")
            response = self._make_request("/v1/question/list", data)
            if not response:
                break
            questions = response.get('questions', [])
            all_questions.extend(questions)
            has_next = response.get('has_next', False)
            last_id = response.get('last_id', '')
            logger.info(f"Got {len(questions)} questions, has_next={has_next}")
            time.sleep(0.5)
        return all_questions

    def fetch_product_list_paginated(self, limit: int = 1000) -> Optional[List[Dict]]:
        """Загружает полный каталог товаров продавца из Ozon."""
        products = []
        last_id = ""
        page = 0
        while page < 100:
            page += 1
            response = self._make_request("/v3/product/list", {
                "filter": {"visibility": "ALL"},
                "last_id": last_id,
                "limit": min(max(limit, 1), 1000),
            })
            if response is None:
                return None
            result = response.get("result", {})
            if not isinstance(result, dict):
                return products
            items = result.get("items", [])
            if isinstance(items, list):
                products.extend(items)
            next_last_id = result.get("last_id", "")
            if not next_last_id or next_last_id == last_id:
                break
            last_id = next_last_id
            if len(items) < limit:
                break
        return products

    def fetch_category_tree(self) -> Optional[Dict[str, str]]:
        response = self._make_request("/v1/description-category/tree", {"language": "DEFAULT"})
        if response is None:
            return None
        category_map = {}

        def visit(value):
            if isinstance(value, list):
                for item in value:
                    visit(item)
                return
            if not isinstance(value, dict):
                return
            category_id = value.get(
                "description_category_id",
                value.get("category_id", value.get("id")),
            )
            category_name = value.get("category_name", value.get("name", ""))
            if category_id not in (None, "") and category_name:
                category_map[str(category_id)] = str(category_name)
            for key in ("children", "categories", "result", "items"):
                visit(value.get(key))

        visit(response)
        return category_map
    
    def fetch_question_answers(self, question_id: str, sku: str) -> List[Dict]:
        all_answers = []
        last_id = ""
        has_more = True
        while has_more:
            data = {
                "question_id": question_id,
                "sku": int(sku),
                "last_id": last_id
            }
            response = self._make_request("/v1/question/answer/list", data)
            if not response:
                break
            answers = response.get('answers', [])
            all_answers.extend(answers)
            last_id = response.get('last_id', '')
            has_more = bool(last_id)
            time.sleep(0.02)  # ~50 запросов/сек
        return all_answers

    def fetch_stock_info(self, skus: List[str]) -> Optional[Dict]:
        values = [str(sku).strip() for sku in skus if str(sku).strip()]
        if not values:
            return {"products": []}
        products = []
        cursor = ""
        while True:
            response = self._make_request("/v1/product/info/stocks-by-warehouse/fbo", {
                "cursor": cursor,
                "limit": 1000,
                "skus": values[:1000],
            })
            if response is None:
                return None
            products.extend(response.get("products", []))
            if not response.get("has_next") or not response.get("cursor") or response.get("cursor") == cursor:
                break
            cursor = response["cursor"]
        return {"products": products}

    def fetch_analytics_stock_info(self, skus: List[str]) -> Optional[Dict]:
        values = [str(sku).strip() for sku in skus if str(sku).strip()]
        if not values:
            return {"items": []}
        response = self._make_request("/v1/analytics/stocks", {"skus": values[:1000]})
        return response if response is not None else None

    def fetch_cluster_list(self) -> Optional[List[Dict]]:
        response = self._make_request("/v1/cluster/list", {
            "cluster_type": "CLUSTER_TYPE_OZON",
        })
        if response is None:
            return None
        clusters = response.get("clusters", [])
        return clusters if isinstance(clusters, list) else []

    def fetch_product_info(self, skus: List[str]) -> Optional[List[Dict]]:
        sku_values = []
        for sku in skus:
            value = str(sku).strip()
            if value:
                sku_values.append(value)
        if not sku_values:
            return []
        products = []
        successful_batches = 0
        for start in range(0, len(sku_values), 1000):
            response = self._make_request(
                "/v3/product/info/list",
                {"sku": sku_values[start:start + 1000]},
            )
            if response is None:
                continue
            successful_batches += 1
            items = response.get("items", [])
            if isinstance(items, list):
                products.extend(items)
        if successful_batches == 0:
            return None
        return products

    def fetch_seller_info(self) -> Dict:
        response = self._make_request("/v1/seller/info", {})
        if not response:
            return {"error": "Ошибка доступа к API Ozon: проверьте права API-ключа для метода /v1/seller/info"}
        if response.get("code"):
            return {
                "error": response.get("message") or "Ошибка API Ozon для /v1/seller/info",
                "code": response.get("code")
            }
        return response
    
    def create_answer(self, question_id: str, sku: str, text: str) -> Optional[str]:
        mode = Database().get_processing_mode()
        if mode == "test":
            logger.info(f"🧪 [TEST] Would send answer to question {question_id}")
            return "test_answer_id_12345"
        data = {
            "question_id": question_id,
            "sku": int(sku),
            "text": text
        }
        response = self._make_request("/v1/question/answer/create", data)
        return response.get('answer_id') if response else None

# ================= НЕЙРОСЕТЬ =================
class AIClient:
    def __init__(self, api_key: str):
        self.client = OpenAI(
            base_url=Config.OPENROUTER_BASE_URL,
            api_key=api_key,
            default_headers={
                "HTTP-Referer": "https://github.com/your-app",
                "X-Title": "Ozon AI Helper"
            }
        )
        self.model = Config.AI_MODEL
        # Загружаем системный промт из БД (если есть)
        self.db = Database()  # временный объект для чтения
        self.system_prompt = self.db.get_setting('system_prompt', default="""Ты - профессиональный менеджер маркетплейса Ozon. Отвечай на вопросы покупателей.
    Правила:
    1. Отвечай четко по делу.
    2. Используй информацию из контекста (отзывы и предыдущие ответы).
    3. Будь вежливым и доброжелательным.
    4. Если у тебя нет точной информации в контексте - честно напиши "НЕ УВЕРЕН".
    5. Отвечай на русском языке.""")

    def generate_answer(self, question_text: str, sku: str, 
                       context_reviews: str, context_answers: str,
                       instruction: Optional[str] = None,
                       previous_draft: Optional[str] = None) -> Optional[str]:
        system_prompt = f"""
        {self.system_prompt}

        ПОРЯДОК ПОДГОТОВКИ ОТВЕТА:
          1. Сначала определи, описывает ли вопрос неисправность, ошибку или неработающую функцию товара.
          2. Для вопроса о неисправности не предлагай ремонт, диагностику или неподтвержденные действия.
              Руководствуйся инструкцией SKU и рекомендуй обратиться в чат поддержки.
          3. Если вопрос не о неисправности, проверь предыдущие подтвержденные ответы для этого товара и используй их факты.
          4. Если подходящего ответа в истории нет, используй специальную инструкцию для SKU.
          5. Если точного ответа нет ни в истории, ни в инструкции, предложи наиболее полезный и осторожный вариант ответа на согласование менеджеру.

        Не возвращай пустой ответ и не отвечай только словами «НЕ УВЕРЕН».
        Не придумывай характеристики, цены, сроки и гарантии. Если данных недостаточно,
        сформулируй нейтральный ответ без неподтвержденных обещаний, который менеджер сможет проверить.
        Отвечай на русском языке, кратко и доброжелательно, максимум в {Config.AI_MAX_WORDS} слов.
        """
        if previous_draft:
            system_prompt += f"""

        ПРЕДЫДУЩИЙ ЧЕРНОВИК:
        {previous_draft}
        Этот вариант уже не подошел менеджеру. Предложи другой ответ: измени формулировку
        и подход, но сохрани подтвержденные факты и не добавляй выдуманных данных.
        """

        # Если есть инструкция для этого SKU – добавляем её с приоритетом
        if instruction:
            system_prompt += f"""

        СПЕЦИАЛЬНАЯ ИНСТРУКЦИЯ ДЛЯ ЭТОГО ТОВАРА (SKU {sku}):
        {instruction}

        ВАЖНО:
        - Эта инструкция имеет ПРИОРИТЕТ над общей историей и контекстом. Если информация из инструкции противоречит контексту, следуй инструкции.
        - Требование "переформулировать своими словами" сохраняется – даже при использовании инструкции, не копируй её дословно.
        """

        user_prompt = f"""
        Товар (SKU): {sku}
        Предыдущие подтвержденные ответы:
        {context_answers}
        Инструкция для SKU:
        {instruction or 'Инструкция отсутствует.'}
        Если вопрос о неисправности товара, обязательно используй сценарий обращения в чат поддержки из инструкции.
        Предыдущий черновик (не повторяй его дословно):
        {previous_draft or 'Нет, это первая попытка.'}
        Вопрос: {question_text}
        Подготовь черновик ответа для проверки менеджером:
        """
        try:
            logger.info(f"🤖 Sending to AI: {self.model}")
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=500
            )
            ai_answer = (completion.choices[0].message.content or "").strip()
            if not ai_answer:
                logger.warning("AI returned an empty answer")
                return None
            words = ai_answer.split()
            if len(words) > Config.AI_MAX_WORDS:
                shortened = " ".join(words[:Config.AI_MAX_WORDS]).rstrip(" ,;:")
                sentence_end = max(shortened.rfind("."), shortened.rfind("!"), shortened.rfind("?"))
                if sentence_end >= len(shortened) // 2:
                    shortened = shortened[:sentence_end + 1]
                else:
                    shortened += "..."
                logger.info(f"AI answer shortened from {len(words)} to {len(shortened.split())} words")
                ai_answer = shortened
            logger.info(f"✅ AI response: {ai_answer[:100]}...")

            unsure_phrases = ["не уверен", "не знаю", "нет информации", "к сожалению", 
                             "у меня нет", "не могу ответить", "не имею данных"]
            if any(phrase in ai_answer.lower() for phrase in unsure_phrases):
                logger.warning("AI answer contains an uncertainty phrase; keeping it as a draft for review")
            return ai_answer
        except Exception as e:
            logger.error(f"❌ AI Error: {e}")
            return None

# ================= ОСНОВНОЙ КЛАСС =================
class OzonAIHelper:
    def __init__(self):
        self.db = Database()
        self.ozon = OzonAPIClient(Config.OZON_CLIENT_ID, Config.OZON_API_KEY)
        self.ai = AIClient(Config.OPENROUTER_API_KEY)
        self.processing_mode = self.db.get_processing_mode()
        self._monthly_report_period_checked = None
        logger.info("="*60)
        logger.info(f"🚀 MODE: {self.processing_mode}")
        logger.info(f"🤖 Model: {Config.AI_MODEL}")
        logger.info("="*60)
    
    def sync_all_data(self):
        logger.info("📥 Syncing data from Ozon (incremental)...")
        self.refresh_publication_statuses()
        
        # Определяем дату последней синхронизации (самый свежий вопрос в БД)
        last_sync = self.db.get_last_sync_time()
        if last_sync:
            logger.info(f"Last sync time: {last_sync}")
        else:
            logger.info("No previous sync found, will fetch all questions.")
        
        # Загружаем только новые вопросы (с датой больше last_sync)
        questions = self.ozon.fetch_questions_paginated(since=last_sync, limit=Config.QUESTIONS_LIMIT)
        if questions:
            # Сохраняем новые вопросы
            self.db.save_questions({"questions": questions})
            logger.info(f"✅ Saved {len(questions)} new questions")
        else:
            logger.info("✅ No new questions found")
            # Если новых вопросов нет, всё равно нужно проверить, есть ли вопросы без ответов, которые мы ещё не обработали
            # Но загрузка ответов для них уже была сделана ранее, поэтому пропускаем этот шаг
            return
        
        # Для новых вопросов, у которых есть ответы, загружаем эти ответы
        new_q_with_answers = []
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            # Выбираем только что добавленные вопросы (у которых is_answered = 0 и answers_count > 0)
            cursor.execute('''
                SELECT question_id, sku, answers_count FROM questions 
                WHERE is_answered = 0 AND answers_count > 0
            ''')
            new_q_with_answers = cursor.fetchall()
        
        if new_q_with_answers:
            logger.info(f"📬 Fetching answers for {len(new_q_with_answers)} new questions with answers...")
            for i, (q_id, sku, count) in enumerate(new_q_with_answers, 1):
                logger.info(f"  ({i}/{len(new_q_with_answers)}) Question {q_id}, answers_count={count}")
                answers = self.ozon.fetch_question_answers(q_id, sku)
                if answers:
                    self.db.save_question_answers(q_id, answers)
                time.sleep(0.02)
        else:
            logger.info("✅ No new answers to fetch")
        
        logger.info("✅ Sync complete")

    def refresh_publication_statuses(self):
        pending_answers = self.db.get_questions_with_unfinalized_publication()
        if not pending_answers:
            return
        logger.info(f"🔄 Refreshing publication status for {len(pending_answers)} questions...")
        for question_id, sku in pending_answers:
            answers = self.ozon.fetch_question_answers(question_id, sku)
            if answers:
                self.db.save_question_answers(question_id, answers)
            time.sleep(0.02)
    
    def process_one_question(self):
        questions = self.db.get_unprocessed_questions(limit=1)
        if not questions:
            logger.debug("Нет новых вопросов для обработки")
            return
        question_id, sku, question_text, published_at = questions[0]
        logger.info("="*60)
        logger.info(f"📩 NEW QUESTION:")
        logger.info(f"  ID: {question_id}")
        logger.info(f"  SKU: {sku}")
        logger.info(f"  Text: {question_text}")
        logger.info("="*60)

        context_reviews, context_answers = self.db.get_context_for_sku(sku)
        instruction = self.db.get_instruction(sku)

        pair_count = context_answers.count("Вопрос:")
        logger.info(f"📚 Передано {pair_count} пар вопрос-ответ в контексте")
        if instruction:
            logger.info(f"📋 Используется инструкция для SKU {sku} (длина {len(instruction)})")

        self.processing_mode = self.db.get_processing_mode()
        ai_answer = self.ai.generate_answer(
            question_text=question_text,
            sku=sku,
            context_reviews=context_reviews,
            context_answers=context_answers,
            instruction=instruction
        )
        if ai_answer:
            logger.info("🤖 AI ANSWER:")
            logger.info(ai_answer)
            logger.info("="*60)
            self.db.log_ai_test(
                question_id, sku, question_text,
                ai_answer, context_reviews, context_answers
            )
            if self.processing_mode == "test":
                self.db.mark_question_processed_by_ai(question_id, ai_answer)
                logger.info(f"🧪 [TEST] Question {question_id} marked as processed (AI answer saved locally)")
            elif self.processing_mode == "semi_automatic":
                self.db.mark_question_pending_ai_review(question_id, ai_answer)
                logger.info(f"⏳ [SEMI-AUTO] Question {question_id} is waiting for approval")
            else:
                answer_id = self.ozon.create_answer(question_id, sku, ai_answer)
                if answer_id:
                    self.db.mark_question_processed_by_ai(question_id, ai_answer)
                    logger.info(f"✅ Question {question_id} answered automatically (answer_id: {answer_id})")
                else:
                    logger.error(f"❌ Failed to send answer for {question_id}")
        else:
            self.db.mark_question_need_manual(question_id)
            logger.info(f"⚠️ Question {question_id} needs manual review (AI not confident)")
    
    def show_statistics(self):
        try:
            with self.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT 
                        COUNT(*) as total,
                        SUM(CASE WHEN is_answered = 1 THEN 1 ELSE 0 END) as answered,
                        SUM(CASE WHEN is_ai_generated = 1 THEN 1 ELSE 0 END) as ai_answered,
                        SUM(CASE WHEN need_manual_review = 1 THEN 1 ELSE 0 END) as manual_review
                    FROM questions
                ''')
                stats = cursor.fetchone()
                cursor.execute('SELECT COUNT(*) FROM ai_test_logs')
                test_logs = cursor.fetchone()[0]
                cursor.execute('SELECT COUNT(*) FROM question_answers')
                answers_count = cursor.fetchone()[0]
                logger.info("="*60)
                logger.info("📊 STATISTICS:")
                logger.info(f"  Total questions: {stats[0] or 0}")
                logger.info(f"  Already answered (from Ozon): {stats[1] or 0}")
                logger.info(f"  AI answered locally: {stats[2] or 0}")
                logger.info(f"  Manual review: {stats[3] or 0}")
                logger.info(f"  Total answer records: {answers_count}")
                logger.info(f"  Test logs: {test_logs}")
                logger.info("="*60)
        except Exception as e:
            logger.error(f"Stats error: {e}")
    
    def check_monthly_report(self):
        from reporting import MonthlyReportAgent, report_period_range

        if self.db.get_setting("reports_auto_enabled", "0") != "1":
            return

        schedule = self.db.get_setting("reports_schedule", "monthly")
        period_type = self.db.get_setting("reports_period", "current_month")
        today = datetime.now().date()
        start, end = report_period_range(period_type, today)

        if schedule == "daily":
            bucket = today.isoformat()
        elif schedule == "weekly":
            bucket = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
        elif schedule == "quarterly":
            bucket = f"{today.year}-Q{((today.month - 1) // 3) + 1}"
        elif schedule == "half_yearly":
            bucket = f"{today.year}-H{1 if today.month <= 6 else 2}"
        elif schedule == "yearly":
            bucket = str(today.year)
        else:
            bucket = today.strftime("%Y-%m")

        marker_key = "reports_last_auto_run"
        if self.db.get_setting(marker_key, "") == f"{schedule}:{period_type}:{bucket}":
            return

        period = (start.isoformat(), end.isoformat())
        if period == self._monthly_report_period_checked:
            return
        agent = MonthlyReportAgent(self.ozon)
        report_path = Path("reports") / f"ozon-report-{period[0]}-to-{period[1]}.md"
        generated_report = None
        if not report_path.exists():
            generated_report = agent.generate(period[0], period[1])
            if generated_report:
                from reporting import register_report
                register_report(generated_report, "automatic")
        self._monthly_report_period_checked = period
        self.db.set_setting(marker_key, f"{schedule}:{period_type}:{bucket}")
        if generated_report:
            logger.info(f"📊 Monthly report generated: {generated_report}")

    def check_analytics_dashboard(self):
        if self.db.get_setting("analytics_auto_enabled", "0") != "1":
            return

        schedule = self.db.get_setting("analytics_schedule", "daily")
        today = datetime.now().date()
        if schedule == "weekly":
            bucket = f"{today.isocalendar().year}-W{today.isocalendar().week:02d}"
        else:
            bucket = today.isoformat()
        marker = f"{schedule}:{bucket}"
        if self.db.get_setting("analytics_last_auto_run", "") == marker:
            return

        from analytics_dashboard import DashboardAnalytics
        DashboardAnalytics(self.ozon).refresh()
        self.db.set_setting("analytics_last_auto_run", marker)
        logger.info("📈 Analytics dashboard updated automatically")

    def run(self):
        logger.info("🚀 Starting Ozon AI Helper")
        self.sync_all_data()
        self.show_statistics()
        try:
            self.check_monthly_report()
            self.check_analytics_dashboard()
        except Exception as report_error:
            logger.error(f"❌ Monthly report generation failed: {report_error}", exc_info=True)
        logger.info(f"🔄 Polling every {Config.POLL_INTERVAL}s")
        while True:
            try:
                self.sync_all_data()
                self.process_one_question()
                try:
                    self.check_monthly_report()
                    self.check_analytics_dashboard()
                except Exception as report_error:
                    logger.error(f"❌ Monthly report generation failed: {report_error}", exc_info=True)
                time.sleep(Config.POLL_INTERVAL)
            except KeyboardInterrupt:
                logger.info("⏹️ Stopping...")
                break
            except Exception as e:
                logger.error(f"❌ Error: {e}", exc_info=True)
                time.sleep(Config.POLL_INTERVAL)

# ================= ЗАПУСК =================
if __name__ == "__main__":
    app = OzonAIHelper()
    app.run()