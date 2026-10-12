"""貸款計算：額度、本息攤還、剩餘本金、DSR 檢核。純函式，無外部依賴。"""
from __future__ import annotations

from dataclasses import dataclass

import config as cfg


def monthly_payment(principal: float, annual_rate_pct: float, years: int) -> float:
    """本息平均攤還：PMT = P·r·(1+r)^n / ((1+r)^n − 1)，r 為月利率。"""
    n = years * 12
    if principal <= 0 or n <= 0:
        return 0.0
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return principal / n
    f = (1 + r) ** n
    return principal * r * f / (f - 1)


def remaining_balance(principal: float, annual_rate_pct: float, years: int, k_paid: int) -> float:
    """繳完 k 期後的剩餘本金：B_k = P(1+r)^k − PMT·((1+r)^k − 1)/r。"""
    n = years * 12
    k = max(0, min(k_paid, n))
    if principal <= 0:
        return 0.0
    if k >= n:
        return 0.0
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return principal * (1 - k / n)
    pmt = monthly_payment(principal, annual_rate_pct, years)
    return principal * (1 + r) ** k - pmt * ((1 + r) ** k - 1) / r


def max_principal_for_payment(payment: float, annual_rate_pct: float, years: int) -> float:
    """在月付額上限內可借的最大本金（monthly_payment 的反函數）。"""
    n = years * 12
    if payment <= 0 or n <= 0:
        return 0.0
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return payment * n
    f = (1 + r) ** n
    return payment * (f - 1) / (r * f)


def mortgage_capacity(market_value: float, existing_balance: float = 0.0, ltv: float = cfg.LTV) -> float:
    """房貸/增貸可貸額度 = 市值 × 成數 − 未償房貸餘額（不得為負）。"""
    return max(0.0, market_value * ltv - existing_balance)


def credit_capacity(monthly_salary: float, multiple: float = cfg.DBR_MULTIPLE) -> float:
    """信貸上限 = 月薪 × 22（DBR 22 倍，倍數以「月薪」計，不再乘 12）。"""
    return max(0.0, monthly_salary * multiple)


@dataclass(frozen=True)
class LoanSpec:
    name: str
    principal: float
    annual_rate_pct: float
    years: int

    @property
    def n_months(self) -> int:
        return self.years * 12

    @property
    def payment(self) -> float:
        return monthly_payment(self.principal, self.annual_rate_pct, self.years)

    def balance_after(self, k_paid: int) -> float:
        return remaining_balance(self.principal, self.annual_rate_pct, self.years, k_paid)


def dsr_check(salary: float, payments: float, other_debt_payment: float = 0.0,
              limit: float = cfg.DSR_LIMIT) -> dict:
    """DSR = (新貸款月付 + 既有負債月付) / 月薪，與上限比較。"""
    cap = salary * limit
    total = payments + other_debt_payment
    return {"cap": cap, "total": total, "ratio": total / salary if salary else float("inf"),
            "ok": total <= cap, "headroom": cap - total}


def dsr_advice(excess: float, mortgage: "LoanSpec", credit: "LoanSpec",
               max_mortgage_years: int = 40, max_credit_years: int = 10) -> list[str]:
    """總月付超出 DSR 上限時，回傳具體調整建議（精確到金額）。excess 為每月超出金額。"""
    tips: list[str] = []
    if excess <= 0:
        return tips
    if mortgage.principal > 0:
        cut = min(mortgage.principal, max_principal_for_payment(excess, mortgage.annual_rate_pct, mortgage.years))
        tips.append(f"降低房貸借款至少 NT$ {cut:,.0f}（約月付少 NT$ {excess:,.0f}）")
        if mortgage.years < max_mortgage_years:
            new = monthly_payment(mortgage.principal, mortgage.annual_rate_pct, max_mortgage_years)
            tips.append(f"房貸年限由 {mortgage.years} 年延長至 {max_mortgage_years} 年，月付由 "
                        f"NT$ {mortgage.payment:,.0f} 降為 NT$ {new:,.0f}（少 NT$ {mortgage.payment - new:,.0f}）")
    if credit.principal > 0:
        cut = min(credit.principal, max_principal_for_payment(excess, credit.annual_rate_pct, credit.years))
        tips.append(f"降低信貸借款至少 NT$ {cut:,.0f}（約月付少 NT$ {excess:,.0f}）")
        if credit.years < max_credit_years:
            new = monthly_payment(credit.principal, credit.annual_rate_pct, max_credit_years)
            tips.append(f"信貸年限由 {credit.years} 年延長至 {max_credit_years} 年，月付由 "
                        f"NT$ {credit.payment:,.0f} 降為 NT$ {new:,.0f}（少 NT$ {credit.payment - new:,.0f}）")
    return tips
