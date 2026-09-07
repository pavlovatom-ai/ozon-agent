import os
import bleach
import markdown
from fastapi import FastAPI, Request, Form, HTTPException, File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import uvicorn
from main import Database, Config, OzonAPIClient
from reporting import MonthlyReportAgent, REPORTS_DIR, delete_report, list_reports, previous_month_range, register_report, report_filename
from analytics_dashboard import DashboardAnalytics, load_snapshot

app = FastAPI(title="Ozon AI Helper - Web Interface")


@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    return PlainTextResponse(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="16" fill="#17212b"/>'
        '<path d="M14 45 25 16h8l-5 13h12l-5 16h-8l4-11H25l-4 11z" fill="#75bfff"/>'
        '<circle cx="48" cy="18" r="6" fill="#7ee2b8"/></svg>',
        media_type="image/svg+xml",
    )

templates = Jinja2Templates(directory="templates")
templates.env.cache = {}
templates.env.cache_size = 0

db = Database()


def parse_file_names(raw_value: str) -> list[str]:
    if not raw_value:
        return []
    return [name.strip() for name in raw_value.replace("\r", "").split(";") if name.strip()]


def serialize_file_names(names: list[str]) -> str:
    return "; ".join([name.strip() for name in names if name and name.strip()])


def format_rating_label(name: str) -> str:
    raw_name = str(name or "").strip()
    if not raw_name:
        return "Рейтинг"

    rating_labels = {
        "rating_price_green": "Процент товаров в зеленой зоне по прайс-индексу",
        "rating_price_yellow": "Процент товаров в желтой зоне по прайс-индексу",
        "rating_price_red": "Процент товаров в красной зоне по прайс-индексу",
        "rating_price_super": "Процент товаров в супервыгодной зоне по прайс-индексу",
        "rating_review_avg_score_total": "Оценка товаров",
        "rating_shipment_delay_cb": "Процент просрочек отгрузки",
        "rating_general_indicator_fbs_rfbs": "Рейтинг по прогрессивной шкале",
        "rating_delivery_complaints_fbo": "Жалобы по FBO",
        "rating_delivery_complaints_fbs": "Жалобы по FBS",
        "rating_delivery_complaints_rfbs_sd": "Жалобы по rFBS",
    }
    if raw_name in rating_labels:
        return rating_labels[raw_name]

    cleaned = raw_name.replace("rating_", "").replace("_", " ").strip()
    if not cleaned:
        return "Рейтинг"

    aliases = {
        "price super": "Цена/качество",
        "price": "Цена",
        "quality": "Качество",
        "service": "Сервис",
        "delivery": "Доставка",
        "overall": "Общий рейтинг",
        "product": "Товар",
        "assortment": "Ассортимент",
        "packaging": "Упаковка",
        "support": "Поддержка",
    }

    lowered = cleaned.lower()
    for key, value in aliases.items():
        if key in lowered:
            return value

    return " ".join(part.capitalize() for part in cleaned.split())


def format_rating_value(rating: dict) -> str:
    if not isinstance(rating, dict):
        return "—"

    current_value = rating.get("current_value")
    if isinstance(current_value, dict):
        for key in ("formatted", "value", "score"):
            value = current_value.get(key)
            if value not in (None, "", " ") and not str(value).startswith("rating_"):
                return str(value)

    for key in ("value", "score"):
        value = rating.get(key)
        if value not in (None, "", " ") and not str(value).startswith("rating_"):
            return str(value)

    rating_name = rating.get("rating")
    if rating_name not in (None, "", " ") and not str(rating_name).startswith("rating_"):
        return str(rating_name)

    return "—"


def normalize_ratings(raw_ratings):
    if not isinstance(raw_ratings, list):
        return []

    normalized = []
    for rating in raw_ratings:
        if not isinstance(rating, dict):
            continue

        normalized.append({
            "label": format_rating_label(rating.get("name")),
            "value": format_rating_value(rating),
        })
    return normalized


def report_period_defaults():
    start, end = previous_month_range()
    return start.isoformat(), end.isoformat()


def render_report_markdown(content: str) -> str:
    rendered = markdown.markdown(
        content,
        extensions=["extra", "tables", "fenced_code", "sane_lists"],
    )
    allowed_tags = set(bleach.sanitizer.ALLOWED_TAGS).union({
        "h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "code",
        "table", "thead", "tbody", "tr", "th", "td", "hr", "br",
    })
    allowed_attributes = {"a": ["href", "title"], "code": ["class"]}
    return bleach.clean(rendered, tags=allowed_tags, attributes=allowed_attributes, strip=True)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM questions")
        total_q = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM questions WHERE is_answered=1")
        answered_q = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM questions WHERE need_manual_review=1")
        manual_q = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM ai_test_logs")
        logs_count = cursor.fetchone()[0]

    ozon = OzonAPIClient(Config.OZON_CLIENT_ID, Config.OZON_API_KEY)
    seller_info = ozon.fetch_seller_info() or {}
    company = seller_info.get("company", {}) if isinstance(seller_info, dict) and "company" in seller_info else {}
    subscription = seller_info.get("subscription", {}) if isinstance(seller_info, dict) and "subscription" in seller_info else {}
    ratings = seller_info.get("ratings", []) if isinstance(seller_info, dict) and "ratings" in seller_info else []
    ratings = normalize_ratings(ratings)
    seller_error = seller_info.get("error") if isinstance(seller_info, dict) and "error" in seller_info else ""

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "total_questions": total_q,
            "answered": answered_q,
            "manual": manual_q,
            "logs": logs_count,
            "seller_info": seller_info,
            "company": company,
            "subscription": subscription,
            "ratings": ratings,
            "seller_error": seller_error,
            "reports": list_reports()[:5],
        }
    )


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    date_from, date_to = report_period_defaults()
    return templates.TemplateResponse(
        request,
        "reports.html",
        {"reports": list_reports(), "date_from": date_from, "date_to": date_to, "error": ""},
    )


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    snapshot = load_snapshot()
    selected_skus = request.query_params.getlist("sku")
    return templates.TemplateResponse(
        request,
        "analytics.html",
        {"snapshot": snapshot, "error": "", "selected_skus": selected_skus},
    )


@app.post("/analytics/refresh", response_class=HTMLResponse)
async def refresh_analytics(
    request: Request,
    date_from: str = Form(""),
    date_to: str = Form(""),
    selected_skus: list[str] = Form(default=[]),
):
    try:
        snapshot = DashboardAnalytics().refresh(date_from.strip() or None, date_to.strip() or None)
        return templates.TemplateResponse(
            request,
            "analytics.html",
            {"snapshot": snapshot, "error": "", "selected_skus": selected_skus},
        )
    except Exception as exc:
        __import__("logging").getLogger(__name__).exception("Analytics refresh failed")
        return templates.TemplateResponse(
            request,
            "analytics.html",
            {"snapshot": load_snapshot(), "error": str(exc), "selected_skus": selected_skus},
            status_code=502,
        )


@app.post("/reports", response_class=HTMLResponse)
async def generate_report(
    request: Request,
    date_from: str = Form(...),
    date_to: str = Form(...),
    ignore_zero_impact_skus: str = Form("0"),
):
    try:
        report_path = MonthlyReportAgent().generate(
            date_from.strip(),
            date_to.strip(),
            ignore_zero_impact_skus=ignore_zero_impact_skus == "1",
        )
        register_report(report_path, "manual")
        return RedirectResponse(url=f"/reports/{report_path.name}", status_code=303)
    except Exception as exc:
        logger = __import__("logging").getLogger(__name__)
        logger.exception("Report generation failed")
        default_from, default_to = report_period_defaults()
        return templates.TemplateResponse(
            request,
            "reports.html",
            {"reports": list_reports(), "date_from": date_from or default_from, "date_to": date_to or default_to, "error": str(exc)},
            status_code=502,
        )


@app.get("/reports/{filename}", response_class=HTMLResponse)
async def report_detail(request: Request, filename: str):
    safe_name = os.path.basename(filename)
    report_path = REPORTS_DIR / safe_name
    if safe_name != filename or report_path.suffix != ".md" or not report_path.is_file():
        raise HTTPException(404, "Отчёт не найден")
    return templates.TemplateResponse(
        request,
        "report_detail.html",
        {
            "filename": safe_name,
            "content": report_path.read_text(encoding="utf-8"),
            "rendered_content": render_report_markdown(report_path.read_text(encoding="utf-8")),
        },
    )


@app.get("/reports/{filename}/download")
async def download_report(filename: str):
    safe_name = os.path.basename(filename)
    report_path = REPORTS_DIR / safe_name
    if safe_name != filename or report_path.suffix != ".md" or not report_path.is_file():
        raise HTTPException(404, "Отчёт не найден")
    return FileResponse(report_path, media_type="text/markdown", filename=safe_name)


@app.post("/reports/{filename}/delete")
async def delete_report_route(filename: str):
    safe_name = os.path.basename(filename)
    if safe_name != filename or not delete_report(safe_name):
        raise HTTPException(404, "Отчёт не найден")
    return RedirectResponse(url="/reports", status_code=303)

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    env = {
        "OZON_CLIENT_ID": os.getenv("OZON_CLIENT_ID", ""),
        "OZON_API_KEY": os.getenv("OZON_API_KEY", ""),
        "OPENROUTER_API_KEY": os.getenv("OPENROUTER_API_KEY", ""),
    }
    system_prompt = db.get_setting("system_prompt", "")
    processing_mode = db.get_processing_mode()
    reports_auto_enabled = db.get_setting("reports_auto_enabled", "0") == "1"
    reports_schedule = db.get_setting("reports_schedule", "monthly")
    reports_period = db.get_setting("reports_period", "current_month")
    analytics_auto_enabled = db.get_setting("analytics_auto_enabled", "0") == "1"
    analytics_schedule = db.get_setting("analytics_schedule", "daily")
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "env": env,
            "system_prompt": system_prompt,
            "processing_mode": processing_mode,
            "reports_auto_enabled": reports_auto_enabled,
            "reports_schedule": reports_schedule,
            "reports_period": reports_period,
            "analytics_auto_enabled": analytics_auto_enabled,
            "analytics_schedule": analytics_schedule,
            "processing_modes": [
                ("test", "test"),
                ("semi_automatic", "semi-auto"),
                ("automatic", "auto")
            ]
        }
    )

@app.post("/settings")
async def update_settings(
    request: Request,
    ozon_client_id: str = Form(...),
    ozon_api_key: str = Form(...),
    openrouter_api_key: str = Form(...),
    system_prompt: str = Form(...),
    processing_mode: str = Form("semi_automatic"),
    reports_auto_enabled: str = Form("0"),
    reports_schedule: str = Form("monthly"),
    reports_period: str = Form("current_month"),
    analytics_auto_enabled: str = Form("0"),
    analytics_schedule: str = Form("daily")
):
    if reports_schedule not in {"daily", "weekly", "monthly", "quarterly", "half_yearly", "yearly"}:
        reports_schedule = "monthly"
    if reports_period not in {"current_month", "previous_month", "current_quarter", "current_half_year", "current_year"}:
        reports_period = "current_month"
    env_content = f"""OZON_CLIENT_ID={ozon_client_id}
OZON_API_KEY={ozon_api_key}
OPENROUTER_API_KEY={openrouter_api_key}
PROCESSING_MODE={processing_mode}
"""
    with open(".env", "w", encoding="utf-8") as f:
        f.write(env_content)
    db.set_setting("system_prompt", system_prompt)
    db.set_processing_mode(processing_mode)
    db.set_setting("reports_auto_enabled", "1" if reports_auto_enabled == "1" else "0")
    db.set_setting("reports_schedule", reports_schedule)
    db.set_setting("reports_period", reports_period)
    db.set_setting("analytics_auto_enabled", "1" if analytics_auto_enabled == "1" else "0")
    db.set_setting("analytics_schedule", analytics_schedule if analytics_schedule in {"daily", "weekly"} else "daily")
    os.environ["OZON_CLIENT_ID"] = ozon_client_id
    os.environ["OZON_API_KEY"] = ozon_api_key
    os.environ["OPENROUTER_API_KEY"] = openrouter_api_key
    os.environ["PROCESSING_MODE"] = processing_mode
    return RedirectResponse(url="/settings", status_code=303)

@app.get("/instructions", response_class=HTMLResponse)
async def instructions_page(request: Request):
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT sku, instruction_text, source_file_name, updated_at FROM sku_instructions ORDER BY sku")
        rows = cursor.fetchall()
    instructions = [{
        "sku": r[0],
        "text": r[1],
        "files": parse_file_names(r[2]),
        "updated": r[3]
    } for r in rows]
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT sku FROM questions ORDER BY sku")
        skus = [r[0] for r in cursor.fetchall()]
    return templates.TemplateResponse(
        request,
        "instructions.html",
        {
            "instructions": instructions,
            "all_skus": skus
        }
    )


@app.post("/instructions")
async def update_instruction(
    request: Request,
    sku: str = Form(...),
    instruction_text: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    existing_file_name: str = Form(""),
    remove_file_name: str = Form("")
):
    full_text = instruction_text.strip() if instruction_text else ""
    saved_file_names = parse_file_names(existing_file_name)

    if remove_file_name:
        saved_file_names = [name for name in saved_file_names if name != remove_file_name]
        db.set_instruction(sku, full_text, serialize_file_names(saved_file_names))
        return RedirectResponse(url="/instructions", status_code=303)

    if files:
        uploaded_names = []
        uploaded_texts = []
        for file in files:
            if not file or not file.filename:
                continue
            uploaded_names.append(file.filename)
            try:
                content = await file.read()
                text_from_file = content.decode('utf-8').strip()
                if text_from_file:
                    uploaded_texts.append(text_from_file)
            except Exception as e:
                print(f"Ошибка чтения файла: {e}")

        if uploaded_names:
            saved_file_names = list(dict.fromkeys(saved_file_names + uploaded_names))

        if uploaded_texts and not full_text:
            full_text = "\n\n".join(uploaded_texts)

    db.set_instruction(sku, full_text, serialize_file_names(saved_file_names))
    return RedirectResponse(url="/instructions", status_code=303)

@app.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    params = request.query_params
    sku_filter = params.get("sku", "").strip()
    status_filter = params.get("status", "all").strip()
    date_from = params.get("date_from", "").strip()
    date_to = params.get("date_to", "").strip()
    sort_by = params.get("sort_by", "date_desc").strip()

    allowed_sort = {
        "date_desc": "created_at DESC, question_id DESC",
        "date_asc": "created_at ASC, question_id ASC",
        "sku_asc": "sku ASC, created_at DESC",
        "sku_desc": "sku DESC, created_at DESC",
        "status": "pending_ai_review DESC, need_manual_review DESC, is_ai_generated DESC, created_at DESC",
        "publication": "CASE COALESCE((SELECT qa.status_publication FROM question_answers qa WHERE qa.question_id = q.question_id ORDER BY qa.published_at DESC, qa.answer_id DESC LIMIT 1), '') WHEN 'PUBLISHED' THEN 1 WHEN 'MODERATION' THEN 2 ELSE 3 END ASC, created_at DESC"
    }
    sort_value = allowed_sort.get(sort_by, allowed_sort["date_desc"])

    query = """
        SELECT q.question_id, q.sku, q.question_text, q.answer_text, q.is_answered, q.is_ai_generated,
               q.need_manual_review, q.pending_ai_review, q.published_at, q.created_at,
               (
                   SELECT qa.status_publication
                   FROM question_answers qa
                   WHERE qa.question_id = q.question_id
                   ORDER BY qa.published_at DESC, qa.answer_id DESC
                   LIMIT 1
               ) AS publication_status
        FROM questions q
        WHERE 1=1
    """
    values = []

    if sku_filter:
        query += " AND sku = ?"
        values.append(sku_filter)
    if status_filter == "pending":
        query += " AND pending_ai_review = 1"
    elif status_filter == "ai":
        query += " AND is_ai_generated = 1 AND is_answered = 1 AND pending_ai_review = 0"
    elif status_filter == "manual":
        query += " AND need_manual_review = 1"
    elif status_filter == "edited":
        query += " AND is_answered = 1 AND is_ai_generated = 0"
    elif status_filter == "new":
        query += " AND is_answered = 0 AND pending_ai_review = 0 AND need_manual_review = 0"

    if date_from:
        query += " AND date(created_at) >= date(?)"
        values.append(date_from)
    if date_to:
        query += " AND date(created_at) <= date(?)"
        values.append(date_to)

    query += f" ORDER BY {sort_value}"

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, values)
        rows = cursor.fetchall()

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT sku FROM questions WHERE sku IS NOT NULL AND sku != '' ORDER BY CAST(sku AS INTEGER), sku")
        available_skus = [r[0] for r in cursor.fetchall()]

    logs = []
    for r in rows:
        q_id, sku, question, answer, is_answered, is_ai_generated, need_manual_review, pending_ai_review, published_at, created_at, publication_status = r
        if pending_ai_review:
            status = "Ожидает подтверждения"
        elif need_manual_review:
            status = "Ручная проверка"
        elif is_answered and is_ai_generated:
            status = "ИИ отправил"
        elif is_answered:
            status = "Редактировано / вручную"
        else:
            status = "Новый"
        logs.append({
            "id": q_id,
            "sku": sku,
            "question": question,
            "answer": answer or "",
            "date": published_at or created_at,
            "status": status,
            "pending": bool(pending_ai_review),
            "is_ai_generated": bool(is_ai_generated),
            "publication_status": publication_status or "",
        })
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "logs": logs,
            "all_skus": available_skus,
            "filters": {
                "sku": sku_filter,
                "status": status_filter,
                "date_from": date_from,
                "date_to": date_to,
                "sort_by": sort_by,
            }
        }
    )


@app.post("/logs/approve")
async def approve_ai_answer(
    request: Request,
    question_id: str = Form(...),
    answer_text: str = Form(...)
):
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT answer_text, sku, pending_ai_review, need_manual_review FROM questions WHERE question_id = ?", (question_id,))
        row = cursor.fetchone()
        if not row:
            raise HTTPException(404, "Вопрос не найден")
        original_answer, sku, pending_ai_review, need_manual_review = row

    if not answer_text or not answer_text.strip():
        raise HTTPException(400, "Ответ не может быть пустым")

    cleaned_answer = answer_text.strip()
    is_manual_edit = bool(original_answer and cleaned_answer != original_answer.strip())
    is_manual_review = bool(need_manual_review)

    with db.get_connection() as conn:
        cursor = conn.cursor()
        if is_manual_review:
            cursor.execute('''
                UPDATE questions
                SET is_answered = 1, is_ai_generated = 0, is_processed = 1, pending_ai_review = 0,
                    need_manual_review = 0, answer_text = ?
                WHERE question_id = ?
            ''', (cleaned_answer, question_id))
            cursor.execute('''
                INSERT OR REPLACE INTO question_answers (
                    answer_id, question_id, sku, answer_text, author_name, published_at, status_publication, is_ai_generated
                ) VALUES (?, ?, ?, ?, 'Менеджер', datetime('now'), 'MODERATION', 0)
            ''', (f"manual_{question_id}", question_id, sku, cleaned_answer))
        elif is_manual_edit:
            cursor.execute('''
                UPDATE questions
                SET is_answered = 1, is_ai_generated = 0, is_processed = 1, pending_ai_review = 0,
                    need_manual_review = 0, answer_text = ?
                WHERE question_id = ?
            ''', (cleaned_answer, question_id))
            cursor.execute('''
                INSERT OR REPLACE INTO question_answers (
                    answer_id, question_id, sku, answer_text, author_name, published_at, status_publication, is_ai_generated
                ) VALUES (?, ?, ?, ?, 'Менеджер', datetime('now'), 'MODERATION', 0)
            ''', (f"manual_{question_id}", question_id, sku, cleaned_answer))
        else:
            cursor.execute('''
                UPDATE questions
                SET is_answered = 1, is_ai_generated = 1, is_processed = 1, pending_ai_review = 0,
                    need_manual_review = 0, answer_text = ?
                WHERE question_id = ?
            ''', (cleaned_answer, question_id))
            cursor.execute('''
                INSERT OR REPLACE INTO question_answers (
                    answer_id, question_id, sku, answer_text, author_name, published_at, status_publication, is_ai_generated
                ) VALUES (?, ?, ?, ?, 'ИИ', datetime('now'), 'MODERATION', 1)
            ''', (f"ai_{question_id}", question_id, sku, cleaned_answer))
        conn.commit()

    ozon = OzonAPIClient(Config.OZON_CLIENT_ID, Config.OZON_API_KEY)
    answer_id = ozon.create_answer(question_id, sku, cleaned_answer)
    if answer_id:
        with db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE questions SET is_answered = 1 WHERE question_id = ?", (question_id,))
            conn.commit()
        return RedirectResponse(url="/logs", status_code=303)

    raise HTTPException(500, "Не удалось отправить ответ в Ozon")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)