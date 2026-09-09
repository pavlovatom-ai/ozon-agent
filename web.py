import os
import hashlib
import json
import logging
import time
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

import bleach
import markdown
import requests
from datetime import date, datetime
from fastapi import FastAPI, Request, Form, HTTPException, File, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uvicorn
from main import AIClient, Database, Config, OzonAPIClient
from reporting import MonthlyReportAgent, REPORTS_DIR, delete_report, list_reports, previous_month_range, register_report, report_filename
from analytics_dashboard import DashboardAnalytics, load_snapshot
from unit_economics import UnitEconomicsInput, calculate_unit_economics, recommendation
from competitor_monitor import check_competitor

app = FastAPI(title="Ozon AI Helper - Web Interface")
WEB_LOGGER = logging.getLogger(__name__)
RUNTIME_IMAGE_DIR = Path("runtime") / "images"
IMAGE_CACHE_TTL_DAYS = max(1, int(os.getenv("IMAGE_CACHE_TTL_DAYS", "7")))
RUNTIME_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/runtime-images", StaticFiles(directory=str(RUNTIME_IMAGE_DIR)), name="runtime-images")


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
OZON_CACHE = {}
OZON_CACHE_LOCK = Lock()


def cached_ozon_call(cache_key: str, ttl_seconds: int, loader):
    now = time.monotonic()
    with OZON_CACHE_LOCK:
        cached = OZON_CACHE.get(cache_key)
        if cached and now - cached["created_at"] < ttl_seconds:
            return cached["value"]

    value = loader()
    if value is not None:
        with OZON_CACHE_LOCK:
            OZON_CACHE[cache_key] = {"created_at": time.monotonic(), "value": value}
    return value


def get_image_extension(image_url: str) -> str:
    path = urlparse(image_url).path or ""
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}:
        return suffix
    return ".jpg"


def cache_sku_image(
    sku: str,
    image_url: str,
    current_local_path: str = "",
    cached_at: str = "",
) -> tuple[str, str]:
    clean_sku = str(sku or "").strip()
    clean_url = str(image_url or "").strip()
    if not clean_sku or not clean_url:
        return "", ""

    local_name = str(current_local_path or "").strip()
    cached_time = None
    if cached_at:
        try:
            cached_time = datetime.fromisoformat(str(cached_at).replace("Z", "+00:00"))
        except ValueError:
            cached_time = None
    if cached_time is not None:
        if cached_time.tzinfo is not None:
            cached_time = cached_time.replace(tzinfo=None)
        cache_is_fresh = (datetime.utcnow() - cached_time).total_seconds() < IMAGE_CACHE_TTL_DAYS * 86400
    else:
        cache_is_fresh = False

    if local_name:
        current_file = RUNTIME_IMAGE_DIR / local_name
        if current_file.is_file() and cache_is_fresh:
            return local_name, str(cached_at)

    image_hash = hashlib.sha1(clean_url.encode("utf-8")).hexdigest()[:12]
    file_name = f"{clean_sku}_{image_hash}{get_image_extension(clean_url)}"
    target_path = RUNTIME_IMAGE_DIR / file_name
    if target_path.is_file() and cache_is_fresh:
        return file_name, str(cached_at)

    try:
        response = requests.get(clean_url, timeout=15)
        response.raise_for_status()
        content = response.content
        if not content:
            return local_name if RUNTIME_IMAGE_DIR.joinpath(local_name).is_file() else "", str(cached_at) if local_name else ""
        target_path.write_bytes(content)
        return file_name, datetime.utcnow().isoformat(timespec="seconds")
    except Exception as exc:
        WEB_LOGGER.warning("Не удалось закэшировать изображение SKU %s: %s", clean_sku, exc)
        if local_name and (RUNTIME_IMAGE_DIR / local_name).is_file():
            return local_name, str(cached_at)
        return "", ""


def apply_display_image_urls(products: list[dict]):
    for product in products:
        local_name = str(product.get("local_image_path", "")).strip()
        if local_name and (RUNTIME_IMAGE_DIR / local_name).is_file():
            product["display_image_url"] = f"/runtime-images/{local_name}"
        else:
            product["display_image_url"] = str(product.get("image_url", "")).strip()


def format_ui_date(value) -> str:
    if not value:
        return "—"
    if isinstance(value, datetime):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y")

    raw_value = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        return parsed.strftime("%d.%m.%Y")
    except ValueError:
        try:
            return date.fromisoformat(raw_value).strftime("%d.%m.%Y")
        except ValueError:
            return raw_value


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


def build_inventory_rows(products: list[dict], stock_payload: dict | None, warehouse_map: dict | None = None) -> list[dict]:
    if not stock_payload:
        return []
    data = stock_payload.get("data", stock_payload)
    items = []
    if isinstance(data, dict):
        items = data.get("items", data.get("products", []))
    product_map = {product["sku"]: product for product in products}
    warehouse_map = warehouse_map or {}
    grouped = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        sku = str(item.get("sku", item.get("product_id", ""))).strip()
        product = product_map.get(sku)
        if not product:
            continue
        grouped.setdefault(sku, {"product": product, "total": 0, "reserved": 0, "warehouses": []})
        stock_items = item.get("stocks", item.get("warehouse_stocks", []))
        if isinstance(stock_items, dict):
            stock_items = stock_items.get("items", stock_items.get("stocks", []))
        if not stock_items and item.get("warehouse_id") not in (None, ""):
            stock_items = [item]
        elif not stock_items and any(key in item for key in ("available_stock_count", "present", "available", "stock")):
            quantity = item.get("present", item.get("available_stock_count", item.get("available", item.get("stock", 0)))) or 0
            reserved = item.get("reserved", item.get("reserved_stock_count", 0)) or 0
            try:
                grouped[sku]["total"] += int(float(quantity))
            except (TypeError, ValueError):
                pass
            try:
                grouped[sku]["reserved"] += int(float(reserved))
            except (TypeError, ValueError):
                pass
            stock_items = []
        warehouses = []
        for stock in stock_items if isinstance(stock_items, list) else []:
            if not isinstance(stock, dict):
                continue
            quantity = stock.get("present", stock.get("available_stock_count", stock.get("available", stock.get("stock", 0))))
            try:
                quantity = int(float(quantity or 0))
            except (TypeError, ValueError):
                quantity = 0
            reserved = stock.get("reserved", stock.get("reserved_stock_count", 0))
            try:
                reserved = int(float(reserved or 0))
            except (TypeError, ValueError):
                reserved = 0
            warehouse_id = str(stock.get("warehouse_id", "")).strip()
            warehouse_info = warehouse_map.get(warehouse_id, {})
            warehouses.append({
                "name": stock.get("warehouse_name", stock.get("name", warehouse_info.get("name", stock.get("source", "")))) or f"Склад ID {warehouse_id}",
                "cluster": stock.get("cluster_name", stock.get("cluster", warehouse_info.get("cluster", ""))) or f"Кластер для склада ID {warehouse_id}",
                "quantity": quantity,
                "reserved": reserved,
            })
        grouped[sku]["warehouses"].extend(warehouses)

    rows = []
    for group in grouped.values():
        cluster_map = {}
        for warehouse in group["warehouses"]:
            group["total"] += warehouse["quantity"]
            cluster_name = str(warehouse["cluster"]).strip() or "Кластер не указан"
            warehouse["name"] = str(warehouse["name"]).strip() or "Склад не указан"
            cluster_map.setdefault(cluster_name, []).append(warehouse)
        if not group["warehouses"]:
            cluster_map["Итого по товару"] = []
        group["clusters"] = [
            {
                "name": cluster_name,
                "warehouses": cluster_warehouses,
                "total": group["total"] if not cluster_warehouses else sum(warehouse["quantity"] for warehouse in cluster_warehouses),
                "reserved": group["reserved"] if not cluster_warehouses else sum(warehouse["reserved"] for warehouse in cluster_warehouses),
            }
            for cluster_name, cluster_warehouses in cluster_map.items()
        ]
        group.pop("warehouses", None)
        rows.append(group)
    return rows


def extract_analytics_stock_items(payload: dict | None) -> list[dict]:
    if not payload:
        return []
    data = payload.get("data", payload)
    if isinstance(data, dict):
        for key in ("items", "products", "rows", "stocks"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return data if isinstance(data, list) else []


def analytics_stock_by_sku(payload: dict | None) -> dict[str, dict]:
    result = {}
    for item in extract_analytics_stock_items(payload):
        sku = str(item.get("sku", item.get("product_id", item.get("id", "")))).strip()
        if sku:
            result[sku] = item
    return result


def first_stock_value(item: dict, *keys: str):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return None


def stock_metric_rows(item: dict | None, warehouse_total: int, warehouse_reserved: int) -> list[dict]:
    item = item or {}
    present = first_stock_value(item, "present", "present_stock_count", "total_stock_count", "stock")
    reserved = first_stock_value(item, "reserved", "reserved_stock_count")
    available = first_stock_value(item, "available", "available_stock_count", "available_to_sell")
    preparing = first_stock_value(item, "preparing_for_sale", "preparing_for_sale_count", "in_process_at_warehouse")
    removed = first_stock_value(item, "removed_from_sale", "removed_from_sale_count", "defect_stock_count")
    in_transit = first_stock_value(item, "in_transit", "in_transit_count", "transit_stock_count")
    if present is None:
        present = warehouse_total
    if reserved is None:
        reserved = warehouse_reserved
    if available is None and present is not None and reserved is not None:
        available = max(int(float(present)) - int(float(reserved)), 0)
    return [
        {"label": "Всего товара", "value": present},
        {"label": "Доступно к продаже", "value": available},
        {"label": "Зарезервировано", "value": reserved},
        {"label": "Готовится к продаже", "value": preparing},
        {"label": "Снято с продажи", "value": removed},
        {"label": "В пути", "value": in_transit},
    ]


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


def load_inventory_data() -> dict:
    ozon = OzonAPIClient(Config.OZON_CLIENT_ID, Config.OZON_API_KEY)
    visible_products = [product for product in db.list_sku_catalog() if product["is_visible"]]
    apply_display_image_urls(visible_products)
    seller_info = cached_ozon_call("seller_info", 300, ozon.fetch_seller_info) or {}
    if seller_info:
        db.save_ozon_snapshot("seller_info", seller_info)

    inventory_error = ""
    inventory_rows = []
    cluster_list = cached_ozon_call("cluster_list", 3600, ozon.fetch_cluster_list)
    if cluster_list:
        db.save_ozon_snapshot("cluster_list", cluster_list)

    if visible_products:
        warehouse_map = {}
        for cluster in cluster_list or []:
            cluster_name = cluster.get("name", f"Кластер {cluster.get('id', '')}")
            for logistic_cluster in cluster.get("logistic_clusters", []):
                for warehouse in logistic_cluster.get("warehouses", []):
                    warehouse_id = str(warehouse.get("warehouse_id", "")).strip()
                    if warehouse_id:
                        warehouse_map[warehouse_id] = {
                            "name": warehouse.get("name", ""),
                            "cluster": cluster_name,
                        }
        visible_skus = tuple(product["sku"] for product in visible_products)
        stock_payload = cached_ozon_call(
            f"stock_info:{visible_skus}",
            60,
            lambda: ozon.fetch_stock_info(list(visible_skus)),
        )
        if stock_payload:
            db.save_ozon_snapshot("stock_info", stock_payload, ",".join(visible_skus))
        if stock_payload is None:
            stock_error = ozon.last_error
            product_info = cached_ozon_call(
                f"product_info:{visible_skus}",
                300,
                lambda: ozon.fetch_product_info(list(visible_skus)),
            )
            if product_info is not None:
                inventory_rows = build_inventory_rows(visible_products, {"items": product_info}, warehouse_map)
                if not inventory_rows:
                    inventory_error = "Ozon не вернул складские остатки для отмеченных товаров."
            else:
                inventory_error = f"Не удалось получить остатки Ozon: {stock_error}"
        else:
            inventory_rows = build_inventory_rows(visible_products, stock_payload, warehouse_map)

        analytics_payload = cached_ozon_call(
            f"analytics_stock:{visible_skus}",
            300,
            lambda: ozon.fetch_analytics_stock_info(list(visible_skus)),
        )
        if analytics_payload:
            db.save_ozon_snapshot("analytics_stock", analytics_payload, ",".join(visible_skus))
        analytics_by_sku = analytics_stock_by_sku(analytics_payload)
        for row in inventory_rows:
            product_sku = str(row["product"].get("sku", "")).strip()
            row["stock_metrics"] = stock_metric_rows(
                analytics_by_sku.get(product_sku),
                row.get("total", 0),
                row.get("reserved", 0),
            )

    return {
        "inventory_rows": inventory_rows,
        "inventory_error": inventory_error,
        "visible_products_count": len(visible_products),
    }


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

    inventory = load_inventory_data()
    ozon = OzonAPIClient(Config.OZON_CLIENT_ID, Config.OZON_API_KEY)
    seller_info = cached_ozon_call("seller_info", 300, ozon.fetch_seller_info) or {}
    if seller_info:
        db.save_ozon_snapshot("seller_info", seller_info)
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
            "inventory_rows": inventory["inventory_rows"],
            "inventory_error": inventory["inventory_error"],
            "reports": list_reports()[:5],
        }
    )


@app.get("/warehouses", response_class=HTMLResponse)
async def warehouses_page(request: Request):
    inventory = load_inventory_data()
    return templates.TemplateResponse(
        request,
        "warehouses.html",
        inventory,
    )


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    date_from, date_to = report_period_defaults()
    return templates.TemplateResponse(
        request,
        "reports.html",
        {"reports": list_reports(), "date_from": date_from, "date_to": date_to, "error": ""},
    )


@app.get("/ozon-history", response_class=HTMLResponse)
async def ozon_history_page(request: Request):
    selected_type = request.query_params.get("type", "").strip()
    snapshot_rows = db.list_ozon_snapshots(selected_type, 100)
    snapshots = []
    for row in snapshot_rows:
        try:
            payload = json.loads(row["payload_json"])
            payload_preview = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        except (TypeError, json.JSONDecodeError):
            payload_preview = row["payload_json"]
        snapshots.append({**row, "payload_preview": payload_preview})
    return templates.TemplateResponse(
        request,
        "ozon_history.html",
        {
            "snapshot_types": db.list_ozon_snapshot_types(),
            "snapshots": snapshots,
            "selected_type": selected_type,
        },
    )


@app.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    snapshot = load_snapshot()
    selected_skus = request.query_params.getlist("sku")
    snapshot["display_date_from"] = format_ui_date(snapshot.get("date_from"))
    snapshot["display_date_to"] = format_ui_date(snapshot.get("date_to"))
    snapshot["display_updated_at"] = format_ui_date(snapshot.get("updated_at"))
    return templates.TemplateResponse(
        request,
        "analytics.html",
        {"snapshot": snapshot, "error": "", "selected_skus": selected_skus},
    )


@app.get("/economics", response_class=HTMLResponse)
async def economics_page(request: Request, sku: str = ""):
    snapshot = load_snapshot()
    available_skus = sorted({str(row.get("sku")) for row in snapshot.get("rows", []) if row.get("sku")})
    selected_sku = sku or (available_skus[0] if available_skus else "")
    values = db.get_sku_economics(selected_sku) if selected_sku else {
        "cost": 0.0, "commission_rate": 0.0, "logistics": 0.0,
        "tax_rate": 0.06, "other_expenses": 0.0, "minimum_margin_rate": 0.20,
    }
    return templates.TemplateResponse(request, "economics.html", {
        "skus": available_skus,
        "selected_sku": selected_sku,
        "values": values,
        "result": None,
        "error": "",
    })


@app.get("/competitors", response_class=HTMLResponse)
async def competitors_page(request: Request):
    competitors = []
    snapshot_rows = {str(row.get("sku")): row for row in load_snapshot().get("rows", [])}
    for item in db.list_competitors():
        economics = db.get_sku_economics(item["sku"])
        our_price = float(snapshot_rows.get(item["sku"], {}).get("average_order_price") or item["current_price"])
        safe_price = None
        if economics["cost"] or economics["logistics"]:
            safe_price = calculate_unit_economics(UnitEconomicsInput(
                price=our_price, cost=economics["cost"], commission_rate=economics["commission_rate"],
                logistics=economics["logistics"], tax_rate=economics["tax_rate"],
                other_expenses=economics["other_expenses"], minimum_margin_rate=economics["minimum_margin_rate"],
            )).minimum_safe_price
        check = check_competitor(our_price, item["current_price"], safe_price)
        item.update({"our_price": our_price, "status": check.status, "difference": check.difference, "minimum_safe_price": safe_price or "—", "economics": economics})
        competitors.append(item)
    return templates.TemplateResponse(request, "competitors.html", {"competitors": competitors, "error": ""})


@app.post("/competitors")
async def add_competitor(
    sku: str = Form(...), name: str = Form(...), url: str = Form(""), current_price: float = Form(...)
):
    db.add_competitor(sku.strip(), name.strip(), url.strip(), current_price)
    return RedirectResponse(url="/competitors", status_code=303)


@app.post("/competitors/{competitor_id}/delete")
async def remove_competitor(competitor_id: int):
    db.delete_competitor(competitor_id)
    return RedirectResponse(url="/competitors", status_code=303)


@app.post("/economics", response_class=HTMLResponse)
async def save_economics(
    request: Request,
    sku: str = Form(...),
    price: float = Form(0),
    cost: float = Form(0),
    commission_rate: float = Form(0),
    logistics: float = Form(0),
    tax_rate: float = Form(0.06),
    other_expenses: float = Form(0),
    minimum_margin_rate: float = Form(0.20),
):
    values = {
        "cost": cost, "commission_rate": commission_rate / 100,
        "logistics": logistics, "tax_rate": tax_rate / 100,
        "other_expenses": other_expenses, "minimum_margin_rate": minimum_margin_rate / 100,
    }
    db.set_sku_economics(sku, values)
    inputs = UnitEconomicsInput(price=price, **values)
    result = calculate_unit_economics(inputs)
    snapshot = load_snapshot()
    available_skus = sorted({str(row.get("sku")) for row in snapshot.get("rows", []) if row.get("sku")})
    return templates.TemplateResponse(request, "economics.html", {
        "skus": available_skus, "selected_sku": sku, "values": values,
        "price": price, "result": result, "recommendation": recommendation(inputs), "error": "",
    })


@app.post("/analytics/refresh", response_class=HTMLResponse)
async def refresh_analytics(
    request: Request,
    date_from: str = Form(""),
    date_to: str = Form(""),
    selected_skus: list[str] = Form(default=[]),
):
    try:
        snapshot = DashboardAnalytics().refresh(date_from.strip() or None, date_to.strip() or None)
        snapshot["display_date_from"] = format_ui_date(snapshot.get("date_from"))
        snapshot["display_date_to"] = format_ui_date(snapshot.get("date_to"))
        snapshot["display_updated_at"] = format_ui_date(snapshot.get("updated_at"))
        return templates.TemplateResponse(
            request,
            "analytics.html",
            {"snapshot": snapshot, "error": "", "selected_skus": selected_skus},
        )
    except Exception as exc:
        __import__("logging").getLogger(__name__).exception("Analytics refresh failed")
        snapshot = load_snapshot()
        snapshot["display_date_from"] = format_ui_date(snapshot.get("date_from"))
        snapshot["display_date_to"] = format_ui_date(snapshot.get("date_to"))
        snapshot["display_updated_at"] = format_ui_date(snapshot.get("updated_at"))
        return templates.TemplateResponse(
            request,
            "analytics.html",
            {"snapshot": snapshot, "error": str(exc), "selected_skus": selected_skus},
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
        "updated": format_ui_date(r[3])
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

@app.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    selected_category = request.query_params.get("category", "").strip()
    with db.get_connection() as conn:
        rows = conn.execute("""
            SELECT DISTINCT sku FROM (
                SELECT sku FROM questions
                UNION ALL SELECT sku FROM sku_economics
                UNION ALL SELECT sku FROM sku_instructions
                UNION ALL SELECT sku FROM competitors
            ) WHERE sku IS NOT NULL AND trim(sku) != ''
        """).fetchall()
    known_skus = [row[0] for row in rows]
    known_skus.extend(
        str(row.get("sku", "")).strip()
        for row in load_snapshot().get("rows", [])
        if row.get("sku")
    )
    error = ""
    ozon = OzonAPIClient(Config.OZON_CLIENT_ID, Config.OZON_API_KEY)
    category_names = {}
    ozon_catalog = cached_ozon_call("product_catalog", 300, ozon.fetch_product_list_paginated)
    if ozon_catalog:
        db.save_ozon_snapshot("product_catalog", ozon_catalog)
    if ozon_catalog is None:
        error = f"Не удалось получить список товаров из Ozon: {ozon.last_error}"
    else:
        for item in ozon_catalog:
            sku = item.get("sku")
            if sku not in (None, ""):
                known_skus.append(str(sku).strip())
    db.sync_sku_catalog(known_skus)
    products = db.list_sku_catalog()
    catalog_skus = [product["sku"] for product in products]
    if catalog_skus:
        product_info = cached_ozon_call(
            f"product_info:{tuple(catalog_skus)}",
            300,
            lambda: ozon.fetch_product_info(catalog_skus),
        )
        if product_info:
            db.save_ozon_snapshot("product_info", product_info, ",".join(catalog_skus))
        if product_info is None:
            error = f"Не удалось получить карточки товаров из Ozon: {ozon.last_error}"
        else:
            returned_skus = set()
            existing_category_names = {product["sku"]: product["category_name"] for product in products}
            existing_image_urls = {product["sku"]: str(product.get("image_url", "")).strip() for product in products}
            existing_local_paths = {product["sku"]: str(product.get("local_image_path", "")).strip() for product in products}
            existing_image_cached_at = {product["sku"]: str(product.get("image_cached_at", "")).strip() for product in products}
            for item in product_info:
                sku = str(item.get("sku", "")).strip()
                if not sku:
                    sku = str(item.get("id", item.get("product_id", ""))).strip()
                if not sku:
                    continue
                returned_skus.add(sku)
                primary_image = item.get("primary_image")
                if isinstance(primary_image, list):
                    primary_image = primary_image[0] if primary_image else ""
                image_url = str(primary_image or "").strip()
                if not image_url:
                    images = item.get("images") or []
                    image_url = str(images[0]).strip() if isinstance(images, list) and images else ""
                current_local_path = existing_local_paths.get(sku, "")
                current_cached_at = existing_image_cached_at.get(sku, "")
                if existing_image_urls.get(sku, "") != image_url:
                    current_local_path = ""
                    current_cached_at = ""
                cached_local_path, cached_at = cache_sku_image(
                    sku, image_url, current_local_path, current_cached_at
                ) if image_url else ("", "")
                db.update_sku_product_info(
                    sku,
                    str(item.get("name", "")),
                    str(item.get("offer_id", item.get("offerId", ""))),
                    image_url,
                    str(item.get("description_category_id", "")),
                    existing_category_names.get(sku, ""),
                    cached_local_path,
                    cached_at,
                )
            if "123" in catalog_skus and "123" not in returned_skus:
                db.delete_sku_catalog("123")
        products = db.list_sku_catalog()
    category_ids = {product["category_id"] for product in products if product["category_id"]}
    if category_ids:
        category_names = cached_ozon_call("category_tree", 3600, ozon.fetch_category_tree) or {}
        if category_names:
            db.save_ozon_snapshot("category_tree", category_names)
        if category_names:
            for product in products:
                category_id = product["category_id"]
                if category_id in category_names:
                    db.update_sku_product_info(
                        product["sku"],
                        product["ozon_name"],
                        product["seller_article"],
                        product["image_url"],
                        category_id,
                        category_names[category_id],
                    )
            products = db.list_sku_catalog()
    categories = sorted({
        (product["category_id"], product["category_name"] or f"Категория {product['category_id']}")
        for product in products if product["category_id"]
    }, key=lambda value: value[1])
    if selected_category:
        products = [product for product in products if product["category_id"] == selected_category]
    apply_display_image_urls(products)
    return templates.TemplateResponse(request, "products.html", {
        "products": products,
        "categories": categories,
        "selected_category": selected_category,
        "error": error,
    })

@app.post("/products")
async def update_products(
    request: Request,
    visible_skus: list[str] = Form(default=[]),
    category: str = Form(""),
):
    products = db.list_sku_catalog()
    visible = {str(sku).strip() for sku in visible_skus}
    for product in products:
        sku = product["sku"]
        if not category or product["category_id"] == category:
            db.update_sku_catalog(sku, sku in visible)
    return RedirectResponse(url="/products", status_code=303)


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
        cursor.execute("SELECT sku, instruction_text, source_file_name, updated_at FROM sku_instructions ORDER BY CAST(sku AS INTEGER), sku")
        instruction_rows = cursor.fetchall()

    instructions = [{
        "sku": r[0],
        "text": r[1],
        "files": parse_file_names(r[2]),
        "updated": format_ui_date(r[3]),
    } for r in instruction_rows]

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
            "date": format_ui_date(published_at or created_at),
            "status": status,
            "pending": bool(pending_ai_review),
            "manual": bool(need_manual_review),
            "is_ai_generated": bool(is_ai_generated),
            "publication_status": publication_status or "",
        })
    return templates.TemplateResponse(
        request,
        "logs.html",
        {
            "logs": logs,
            "all_skus": available_skus,
            "instructions": instructions,
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

    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE questions
            SET is_answered = 0,
                is_processed = 1,
                pending_ai_review = CASE WHEN is_ai_generated = 1 THEN 1 ELSE 0 END,
                need_manual_review = CASE WHEN is_ai_generated = 0 THEN 1 ELSE 0 END
            WHERE question_id = ?
        ''', (question_id,))
        conn.commit()
    raise HTTPException(500, "Не удалось отправить ответ в Ozon")


@app.post("/logs/retry-ai")
async def retry_ai_answer(question_id: str = Form(...)):
    with db.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT sku, question_text, answer_text, is_answered, pending_ai_review, need_manual_review
            FROM questions
            WHERE question_id = ?
        ''', (question_id,))
        row = cursor.fetchone()
    if not row:
        raise HTTPException(404, "Вопрос не найден")
    sku, question_text, previous_draft, is_answered, pending_ai_review, need_manual_review = row
    if is_answered:
        raise HTTPException(400, "Вопрос уже получил ответ")
    if not pending_ai_review and not need_manual_review:
        raise HTTPException(400, "Для этого вопроса пока нет черновика для повторной генерации")

    context_reviews, context_answers = db.get_context_for_sku(sku)
    instruction = db.get_instruction(sku)
    ai_answer = AIClient(Config.OPENROUTER_API_KEY).generate_answer(
        question_text=question_text,
        sku=sku,
        context_reviews=context_reviews,
        context_answers=context_answers,
        instruction=instruction,
        previous_draft=previous_draft or None,
    )
    if not ai_answer:
        raise HTTPException(502, "AI не смогла подготовить черновик. Проверьте ключ OpenRouter и повторите попытку.")

    db.log_ai_test(question_id, sku, question_text, ai_answer, context_reviews, context_answers)
    db.mark_question_pending_ai_review(question_id, ai_answer)
    return RedirectResponse(url="/logs", status_code=303)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)