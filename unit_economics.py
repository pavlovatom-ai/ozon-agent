from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class UnitEconomicsInput:
    price: float
    cost: float
    commission_rate: float = 0.0
    logistics: float = 0.0
    tax_rate: float = 0.06
    other_expenses: float = 0.0
    minimum_margin_rate: float = 0.20


@dataclass(frozen=True)
class UnitEconomicsResult:
    net_profit: float
    net_margin_rate: float
    roi: Optional[float]
    minimum_safe_price: Optional[float]
    total_expenses: float


def calculate_unit_economics(values: UnitEconomicsInput) -> UnitEconomicsResult:
    price = max(float(values.price), 0.0)
    cost = max(float(values.cost), 0.0)
    commission = price * max(float(values.commission_rate), 0.0)
    tax = price * max(float(values.tax_rate), 0.0)
    logistics = max(float(values.logistics), 0.0)
    other_expenses = max(float(values.other_expenses), 0.0)
    total_expenses = cost + commission + tax + logistics + other_expenses
    net_profit = price - total_expenses
    net_margin_rate = net_profit / price if price else 0.0
    invested = cost + logistics + other_expenses
    roi = net_profit / invested if invested else None

    denominator = 1.0 - max(float(values.commission_rate), 0.0) - max(float(values.tax_rate), 0.0) - max(float(values.minimum_margin_rate), 0.0)
    fixed_costs = cost + logistics + other_expenses
    minimum_safe_price = fixed_costs / denominator if denominator > 0 else None

    return UnitEconomicsResult(
        net_profit=net_profit,
        net_margin_rate=net_margin_rate,
        roi=roi,
        minimum_safe_price=minimum_safe_price,
        total_expenses=total_expenses,
    )


def recommendation(values: UnitEconomicsInput) -> str:
    result = calculate_unit_economics(values)
    if result.minimum_safe_price is None:
        return "Невозможно рассчитать безопасную цену: комиссия, налог и минимальная маржа слишком велики."
    if values.price < result.minimum_safe_price:
        return f"Риск продажи ниже целевой маржи. Минимальная безопасная цена: {result.minimum_safe_price:.2f} RUB."
    if result.net_profit < 0:
        return "Продажа убыточна: пересмотрите цену, себестоимость или расходы."
    if result.net_margin_rate < values.minimum_margin_rate:
        return f"Маржа ниже цели: {result.net_margin_rate:.1%} при цели {values.minimum_margin_rate:.1%}."
    return "Экономика SKU соответствует заданной минимальной марже."
