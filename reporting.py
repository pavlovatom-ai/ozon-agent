import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import OpenAI

from main import Config, OzonAPIClient
from unit_economics import UnitEconomicsInput, calculate_unit_economics, recommendation

logger = logging.getLogger(__name__)
REPORTS_DIR = Path(os.getenv("REPORTS_DIR", Path(__file__).resolve().parent / "reports"))
REPORTS_DIR.mkdir(exist_ok=True)
REPORT_METADATA_PATH = REPORTS_DIR / "report-metadata.json"


def previous_month_range(reference: Optional[date] = None):
    current = reference or date.today()
    first_of_current = current.replace(day=1)
    previous_last = first_of_current - timedelta(days=1)
    return previous_last.replace(day=1), previous_last


def current_month_range(reference: Optional[date] = None):
    current = reference or date.today()
    return current.replace(day=1), current


def report_period_range(period: str, reference: Optional[date] = None):
    current = reference or date.today()
    if period == "current_month":
        return current_month_range(current)
    if period == "previous_month":
        return previous_month_range(current)
    if period == "current_quarter":
        quarter_start_month = ((current.month - 1) // 3) * 3 + 1
        return current.replace(month=quarter_start_month, day=1), current
    if period == "current_half_year":
        half_start_month = 1 if current.month <= 6 else 7
        return current.replace(month=half_start_month, day=1), current
    if period == "current_year":
        return current.replace(month=1, day=1), current
    raise ValueError(f"Неизвестный период отчета: {period}")


def report_filename(date_from: str, date_to: str) -> str:
    return f"ozon-report-{date_from}-to-{date_to}.md"


def _load_report_metadata() -> Dict[str, Dict[str, str]]:
    if not REPORT_METADATA_PATH.exists():
        return {}
    try:
        value = json.loads(REPORT_METADATA_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def register_report(path: Path, source: str = "manual"):
    metadata = _load_report_metadata()
    metadata[path.name] = {
        "source": "automatic" if source == "automatic" else "manual",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    REPORT_METADATA_PATH.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_report(filename: str) -> bool:
    path = REPORTS_DIR / filename
    if path.suffix != ".md" or not path.is_file():
        return False
    path.unlink()
    metadata = _load_report_metadata()
    metadata.pop(filename, None)
    REPORT_METADATA_PATH.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return True


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _compact_json(value: Any, limit: int = 28000) -> str:
    text = json.dumps(value, ensure_ascii=False, default=_json_default)
    if len(text) > limit:
        return text[:limit] + "\n...[данные сокращены для контекста ИИ]"
    return text


class OzonReportTools:
    def __init__(self, client: OzonAPIClient):
        self.client = client

    def analytics(self, date_from: str, date_to: str, dimension: str = "sku") -> Dict:
        metrics = [
            "revenue", "ordered_units", "delivered_units", "hits_view_search",
            "hits_tocart", "hits_view_pdp", "returns", "cancellations"
        ]
        payload = {
            "date_from": f"{date_from}T00:00:00Z",
            "date_to": f"{date_to}T23:59:59Z",
            "dimension": [dimension],
            "metrics": metrics,
            "filters": [],
            "sort": [{"key": "revenue", "order": "DESC"}],
            "limit": 1000,
            "offset": 0,
        }
        response = self.client._make_request("/v1/analytics/data", payload)
        return {"endpoint": "/v1/analytics/data", "dimension": dimension, "data": response or {}}

    def transactions(self, date_from: str, date_to: str) -> Dict:
        operations: List[Dict] = []
        page = 1
        while page <= 20:
            payload = {
                "filter": {
                    "date": {
                        "from": f"{date_from}T00:00:00Z",
                        "to": f"{date_to}T23:59:59Z",
                    }
                },
                "page": page,
                "page_size": 1000,
            }
            response = self.client._make_request("/v3/finance/transaction/list", payload) or {}
            chunk = response.get("operations", response.get("transactions", []))
            if not isinstance(chunk, list):
                break
            operations.extend(chunk)
            if len(chunk) < 1000:
                break
            page += 1
        return {"endpoint": "/v3/finance/transaction/list", "operations": operations}

    def realization(self, year: int, month: int) -> Dict:
        response = self.client._make_request("/v2/finance/realization", {"year": year, "month": month})
        return {"endpoint": "/v2/finance/realization", "data": response or {}}

    def returns(self, date_from: str, date_to: str) -> Dict:
        payload = {
            "filter": {
                "date_from": f"{date_from}T00:00:00Z",
                "date_to": f"{date_to}T23:59:59Z",
            },
            "limit": 500,
        }
        response = self.client._make_request("/v1/returns/list", payload)
        return {"endpoint": "/v1/returns/list", "data": response or {}}

    def stocks(self, skus: List[str]) -> Dict:
        clean_skus = [str(sku) for sku in skus if str(sku).strip()][:1000]
        response = self.client._make_request("/v1/analytics/stocks", {"skus": clean_skus})
        return {"endpoint": "/v1/analytics/stocks", "data": response or {}}

    def prices(self, skus: List[str]) -> Dict:
        clean_skus = [str(sku) for sku in skus if str(sku).strip()][:1000]
        response = self.client._make_request(
            "/v5/product/info/prices",
            {"filter": {"offer_id": [], "product_id": clean_skus}, "limit": 1000},
        )
        return {"endpoint": "/v5/product/info/prices", "data": response or {}}

    def calculate(self, operation: str, values: List[float]) -> Dict:
        numbers = [float(value) for value in values]
        if operation == "sum":
            result = sum(numbers)
        elif operation == "average":
            result = sum(numbers) / len(numbers) if numbers else 0
        elif operation == "min":
            result = min(numbers) if numbers else 0
        elif operation == "max":
            result = max(numbers) if numbers else 0
        else:
            raise ValueError("Поддерживаются операции sum, average, min, max")
        return {"operation": operation, "result": result, "currency": "RUB"}

    def unit_economics(self, price: float, cost: float, commission_rate: float, logistics: float, tax_rate: float = 0.06, other_expenses: float = 0.0, minimum_margin_rate: float = 0.20) -> Dict:
        inputs = UnitEconomicsInput(
            price=price,
            cost=cost,
            commission_rate=commission_rate,
            logistics=logistics,
            tax_rate=tax_rate,
            other_expenses=other_expenses,
            minimum_margin_rate=minimum_margin_rate,
        )
        result = calculate_unit_economics(inputs)
        return {
            "net_profit": result.net_profit,
            "net_margin_rate": result.net_margin_rate,
            "roi": result.roi,
            "minimum_safe_price": result.minimum_safe_price,
            "total_expenses": result.total_expenses,
            "recommendation": recommendation(inputs),
            "currency": "RUB",
        }


class MonthlyReportAgent:
    def __init__(self, ozon: Optional[OzonAPIClient] = None):
        self.ozon = ozon or OzonAPIClient(Config.OZON_CLIENT_ID, Config.OZON_API_KEY)
        self.tools = OzonReportTools(self.ozon)
        self.client = OpenAI(
            base_url=Config.OPENROUTER_BASE_URL,
            api_key=Config.OPENROUTER_API_KEY,
            default_headers={
                "HTTP-Referer": "https://github.com/your-app",
                "X-Title": "Ozon AI Financial Reports",
            },
        )

    def _definitions(self):
        return [
            {"type": "function", "function": {"name": "get_analytics", "description": "Получить агрегированную аналитику продаж и метрик по дням или SKU.", "parameters": {"type": "object", "properties": {"date_from": {"type": "string"}, "date_to": {"type": "string"}, "dimension": {"type": "string", "enum": ["day", "sku", "category1"]}}, "required": ["date_from", "date_to", "dimension"]}}},
            {"type": "function", "function": {"name": "get_transactions", "description": "Получить финансовые транзакции с начислениями, комиссиями, доставкой и возвратами.", "parameters": {"type": "object", "properties": {"date_from": {"type": "string"}, "date_to": {"type": "string"}}, "required": ["date_from", "date_to"]}}},
            {"type": "function", "function": {"name": "get_realization", "description": "Получить официальный месячный отчет реализации.", "parameters": {"type": "object", "properties": {"year": {"type": "integer"}, "month": {"type": "integer"}}, "required": ["year", "month"]}}},
            {"type": "function", "function": {"name": "get_returns", "description": "Получить возвраты и отмены за период.", "parameters": {"type": "object", "properties": {"date_from": {"type": "string"}, "date_to": {"type": "string"}}, "required": ["date_from", "date_to"]}}},
            {"type": "function", "function": {"name": "get_stocks", "description": "Получить остатки и оборачиваемость для переданных SKU.", "parameters": {"type": "object", "properties": {"skus": {"type": "array", "items": {"type": "string"}}}, "required": ["skus"]}}},
            {"type": "function", "function": {"name": "get_prices", "description": "Получить цены и комиссии для переданных SKU.", "parameters": {"type": "object", "properties": {"skus": {"type": "array", "items": {"type": "string"}}}, "required": ["skus"]}}},
            {"type": "function", "function": {"name": "calculate", "description": "Посчитать сумму, среднее, минимум или максимум чисел в RUB.", "parameters": {"type": "object", "properties": {"operation": {"type": "string", "enum": ["sum", "average", "min", "max"]}, "values": {"type": "array", "items": {"type": "number"}}}, "required": ["operation", "values"]}}},
            {"type": "function", "function": {"name": "calculate_unit_economics", "description": "Рассчитать прибыль, маржу, ROI и минимальную безопасную цену SKU в RUB.", "parameters": {"type": "object", "properties": {"price": {"type": "number"}, "cost": {"type": "number"}, "commission_rate": {"type": "number"}, "logistics": {"type": "number"}, "tax_rate": {"type": "number"}, "other_expenses": {"type": "number"}, "minimum_margin_rate": {"type": "number"}}, "required": ["price", "cost", "commission_rate", "logistics"]}}},
        ]

    def _invoke(self, name: str, arguments: Dict) -> Dict:
        try:
            if name == "get_analytics":
                return self.tools.analytics(**arguments)
            if name == "get_transactions":
                return self.tools.transactions(**arguments)
            if name == "get_realization":
                return self.tools.realization(**arguments)
            if name == "get_returns":
                return self.tools.returns(**arguments)
            if name == "get_stocks":
                return self.tools.stocks(**arguments)
            if name == "get_prices":
                return self.tools.prices(**arguments)
            if name == "calculate":
                return self.tools.calculate(**arguments)
            if name == "calculate_unit_economics":
                return self.tools.unit_economics(**arguments)
            return {"error": f"Неизвестный инструмент: {name}"}
        except Exception as exc:
            logger.exception("Report tool failed: %s", name)
            return {"error": str(exc), "tool": name}

    def _history_context(self, current_filename: str) -> str:
        history = []
        for report in list_reports():
            if report["filename"] == current_filename:
                continue
            path = REPORTS_DIR / report["filename"]
            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                continue
            history.append(f"### {report['filename']}\n{content[:12000]}")
            if len(history) >= 4:
                break
        return "\n\n".join(history) or "Исторических отчетов пока нет."

    def generate(self, date_from: str, date_to: str, ignore_zero_impact_skus: bool = False) -> Path:
        start = date.fromisoformat(date_from)
        end = date.fromisoformat(date_to)
        if end < start:
            raise ValueError("Дата окончания раньше даты начала")
        filename = report_filename(date_from, date_to)
        history_context = self._history_context(filename)
        sku_focus_instruction = """
При анализе SKU игнорируй товары, у которых в выбранном периоде нет продаж, заказов, доставок, выручки или другого заметного влияния на KPI. Не включай их в подробные таблицы и рекомендации; при необходимости укажи одной строкой, сколько SKU исключено как несущественные.
""" if ignore_zero_impact_skus else """
Анализируй все SKU, включая товары без продаж, если они помогают оценить ассортимент, остатки или будущие возможности.
"""
        messages = [
            {"role": "system", "content": """Ты финансовый аналитик продавца Ozon. Самостоятельно исследуй период через инструменты. Сначала получи продажи по SKU, затем финансовые транзакции, официальный отчет реализации, возвраты и отмены. Если из продаж видны SKU, обязательно запроси остатки и цены/комиссии по ключевым SKU. При необходимости используй калькулятор. Все деньги трактуй и показывай в RUB. Не выдумывай отсутствующие данные: отмечай ошибки API и ограничения.

Используй исторические отчеты ниже для динамики. Сравни текущий период с доступными прошлыми периодами: выручку, продажи, комиссии, возвраты, концентрацию по SKU, остатки и риски. Отдельно указывай, где сравнение невозможно из-за отсутствия сопоставимых данных. Делай осторожный прогноз на следующий период: объясняй его допущения, не выдавай прогноз за факт и не придумывай точность.

В финале верни только красивый полноценный Markdown-отчет на русском языке с разделами: резюме, KPI, динамика относительно истории, выручка и продажи, комиссии и логистика, возвраты и отмены, анализ по SKU, остатки и цены, прогноз, риски, мнение ИИ, конкретные рекомендации и методология.

Исторические отчеты:
""" + history_context + "\n\nПравило детализации SKU:\n" + sku_focus_instruction},
            {"role": "user", "content": f"Сформируй глубокий финансово-операционный отчет Ozon за период {date_from} — {date_to}. Начни исследование и продолжай вызывать инструменты, пока данных достаточно для обоснованных выводов."},
        ]
        for _ in range(10):
            completion = self.client.chat.completions.create(
                model=Config.AI_MODEL,
                messages=messages,
                tools=self._definitions(),
                tool_choice="auto",
                temperature=0.2,
                max_tokens=6000,
            )
            message = completion.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                content = (message.content or "").strip()
                if not content:
                    raise RuntimeError("ИИ не вернул текст отчета")
                path = REPORTS_DIR / filename
                path.write_text(content + "\n", encoding="utf-8")
                return path
            messages.append({"role": "assistant", "content": message.content, "tool_calls": [call.model_dump() for call in tool_calls]})
            for call in tool_calls:
                arguments = json.loads(call.function.arguments or "{}")
                result = self._invoke(call.function.name, arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": _compact_json(result)})
        raise RuntimeError("Агент превысил лимит итераций инструментов")

    def generate_previous_month_if_missing(self) -> Optional[Path]:
        start, end = previous_month_range()
        path = REPORTS_DIR / report_filename(start.isoformat(), end.isoformat())
        if path.exists():
            return None
        return self.generate(start.isoformat(), end.isoformat())

    def generate_current_month_if_missing(self) -> Optional[Path]:
        start, end = current_month_range()
        path = REPORTS_DIR / report_filename(start.isoformat(), end.isoformat())
        if path.exists():
            return None
        return self.generate(start.isoformat(), end.isoformat())


def list_reports() -> List[Dict[str, str]]:
    metadata = _load_report_metadata()
    reports = []
    for path in sorted(REPORTS_DIR.glob("*.md"), reverse=True):
        item_metadata = metadata.get(path.name, {})
        created_at = item_metadata.get("created_at")
        try:
            created = datetime.fromisoformat(created_at) if created_at else datetime.fromtimestamp(path.stat().st_mtime)
        except ValueError:
            created = datetime.fromtimestamp(path.stat().st_mtime)
        reports.append({
            "filename": path.name,
            "title": path.stem.replace("ozon-report-", "Отчёт Ozon: "),
            "date": created.strftime("%d.%m.%Y"),
            "source": "Автоматически" if item_metadata.get("source") == "automatic" else "Вручную",
            "updated": created.strftime("%d.%m.%Y"),
        })
    return reports
