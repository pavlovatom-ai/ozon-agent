import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

from main import Config, OzonAPIClient
from reporting import OzonReportTools, current_month_range

logger = logging.getLogger(__name__)
SNAPSHOT_PATH = Path(os.getenv("ANALYTICS_SNAPSHOT_PATH", Path(__file__).resolve().parent / "reports" / "analytics-dashboard.json"))


def _number(value: Any) -> float:
    if value in (None, "", "-"):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _first(item: Dict, *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return 0


def _rows_from_analytics(payload: Dict) -> List[Dict]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("data", "result", "rows", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            rows = _rows_from_analytics(value)
            if rows:
                return rows
    return []


def _sku(row: Dict) -> str:
    dimensions = row.get("dimensions", row.get("dimension", []))
    if isinstance(dimensions, list) and dimensions:
        first = dimensions[0]
        if isinstance(first, dict):
            return str(first.get("id", first.get("value", first.get("name", ""))))
        return str(first)
    return str(_first(row, "sku", "product_id", "id"))


def _metric(row: Dict, *names: str) -> float:
    metrics = row.get("metrics", row.get("metric", []))
    if isinstance(metrics, list):
        for metric in metrics:
            if isinstance(metric, dict):
                name = str(metric.get("name", metric.get("key", ""))).lower()
                if any(candidate.lower() in name for candidate in names):
                    return _number(metric.get("value", metric.get("data", 0)))
            elif len(names) == 1:
                return _number(metric)
    return _number(_first(row, *names))


def _stock_rows(payload: Dict) -> Dict[str, Dict]:
    data = payload.get("data", payload)
    items = data.get("items", []) if isinstance(data, dict) else []
    result = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        sku = str(_first(item, "sku", "product_id", "id"))
        if sku:
            result[sku] = item
    return result


class DashboardAnalytics:
    def __init__(self, client: OzonAPIClient = None):
        self.client = client or OzonAPIClient(Config.OZON_CLIENT_ID, Config.OZON_API_KEY)
        self.tools = OzonReportTools(self.client)

    def refresh(self, date_from: str = None, date_to: str = None) -> Dict:
        if not date_from or not date_to:
            start, end = current_month_range()
            date_from, date_to = start.isoformat(), end.isoformat()

        analytics = self.tools.analytics(date_from, date_to, "sku")
        last_error = getattr(self.client, "last_error", "")
        if not analytics.get("data") and last_error:
            raise RuntimeError(f"Не удалось обновить аналитику: {last_error}")
        analytics_rows = _rows_from_analytics(analytics)
        active_skus = [_sku(row) for row in analytics_rows if _sku(row)]
        stocks = self.tools.stocks(active_skus) if active_skus else {"data": {}}
        stock_map = _stock_rows(stocks)

        rows = []
        for item in analytics_rows:
            sku = _sku(item)
            if not sku:
                continue
            revenue = _metric(item, "revenue")
            ordered = _metric(item, "ordered_units", "ordered")
            delivered = _metric(item, "delivered_units", "delivered")
            returns = _metric(item, "returns", "return")
            impressions = _metric(item, "hits_view_search", "impressions", "view")
            carts = _metric(item, "hits_tocart", "cart")
            clicks = _metric(item, "hits_view_pdp", "clicks")
            days = max((date.fromisoformat(date_to) - date.fromisoformat(date_from)).days + 1, 1)
            stock = stock_map.get(sku, {})
            available = _number(_first(stock, "available_stock_count", "available", "stock"))
            rows.append({
                "category": "—",
                "model": "—",
                "offer_id": "—",
                "sku": sku,
                "abc_revenue": "—",
                "abc_margin": "—",
                "revenue": revenue,
                "gross_profit": "—",
                "margin": "—",
                "roi": "—",
                "revenue_per_unit": revenue / delivered if delivered else 0,
                "average_order_price": revenue / ordered if ordered else 0,
                "cost": "—",
                "advertising": "—",
                "ozon_expenses": "—",
                "commission": "—",
                "logistics": "—",
                "tax": "—",
                "own_expenses": "—",
                "impressions": impressions,
                "ctr": clicks / impressions * 100 if impressions else "—",
                "clicks": clicks,
                "carts": carts,
                "cart_conversion": ordered / carts * 100 if carts else "—",
                "buyout_rate": delivered / ordered * 100 if ordered else "—",
                "return_rate": returns / delivered * 100 if delivered else "—",
                "orders": ordered,
                "sales": delivered,
                "returns": returns,
                "cancellations": _metric(item, "cancellations", "cancel"),
                "orders_per_day": ordered / days,
                "sales_per_day": delivered / days,
                "cost_per_unit": "—",
                "turnover_days": _first(stock, "idc", "turnover_days") or "—",
                "stock_units": available,
                "stock_rub": "—",
            })

        snapshot = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "date_from": date_from,
            "date_to": date_to,
            "source": ["/v1/analytics/data", "/v1/analytics/stocks"],
            "rows": rows,
            "warnings": [] if analytics_rows else ["Ozon не вернул строки аналитики за выбранный период."],
        }
        temporary_path = SNAPSHOT_PATH.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_path.replace(SNAPSHOT_PATH)
        return snapshot


def load_snapshot() -> Dict:
    if not SNAPSHOT_PATH.exists():
        return {"updated_at": "", "date_from": "", "date_to": "", "rows": [], "warnings": ["Данные ещё не обновлялись."]}
    try:
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"updated_at": "", "date_from": "", "date_to": "", "rows": [], "warnings": ["Не удалось прочитать последний снимок данных."]}
