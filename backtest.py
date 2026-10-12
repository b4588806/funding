"""投資回測引擎：資料取得（yfinance / 假設模型）、資料驗證、多資金來源月度模擬。

三個資金來源（房貸資金、信貸資金、自有資金）各自獨立：自己的標的權重、投入策略與貸款月付。
價格採「未還原股價 + 現金股利」逐筆計算（現金流法），不使用還原股價，避免重複計入股息；
yfinance 的還原股價（Adj Close）只用來做交叉驗證。

模擬順序（逐交易日）：
  遇新月份 m → ①閒置現金計息 ②繳第 m 期貸款（m≥1）→ 處理當日除息（用除息前持股）
  → ③若為結算月份則快照 → ④依各來源策略投入資金。
淨資產盈餘差 = 投資市值 + 閒置現金 + 實領套利現金 − 剩餘貸款 − 額外自掏腰包 − 自有本金
  實領套利現金 = 未再投入的稅後股息 − 累積貸款繳款（為負代表股息不夠繳、需自掏腰包）
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

import config as cfg
from loan import LoanSpec

TOTAL = "合計"


class DataUnavailable(Exception):
    pass


# ---------------------------------------------------------------- 資料來源
def fetch_tw_etf(code: str, start: str, end: Optional[str] = None) -> pd.DataFrame:
    """以 yfinance 取得台股 ETF 未還原收盤價、現金股利與還原股價（供驗證）。

    先試上市 .TW，再試上櫃 .TWO。yfinance 已對股票分割調整價格與股利（如 0050 的 1 拆 4）。
    """
    import yfinance as yf  # 延後匯入，無網路環境仍可載入其他模組

    end = end or (pd.Timestamp.today() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    for suffix in (".TW", ".TWO"):
        try:
            h = yf.Ticker(code + suffix).history(start=start, end=end, auto_adjust=False, actions=True)
        except Exception:
            continue
        if h is not None and not h.empty:
            h.index = pd.DatetimeIndex(h.index).tz_localize(None).normalize()
            if "Dividends" not in h.columns:
                h["Dividends"] = 0.0
            cols = ["Close", "Dividends"] + (["Adj Close"] if "Adj Close" in h.columns else [])
            return h[cols].dropna(subset=["Close"])
    raise DataUnavailable(f"取不到 {code} 的歷史資料（網路或代號問題）")


def make_synthetic_fund(start: str, end: Optional[str], total_return: float,
                        dist_rate: float, nav0: float = 10.0) -> pd.DataFrame:
    """以假設年化總報酬/配息率產生月配基金序列（無歷史淨值時使用）。

    淨值年成長率 g = 總報酬 − 配息率；每月月底配息 = 淨值 × 配息率 / 12。
    """
    end_ts = pd.Timestamp(end) if end else pd.Timestamp.today().normalize()
    idx = pd.bdate_range(start, end_ts)
    g = total_return - dist_rate
    years = (idx - idx[0]).days / 365.25
    s = pd.DataFrame({"Close": nav0 * (1 + g) ** years, "Dividends": 0.0}, index=idx)
    month_end = s.groupby([s.index.year, s.index.month]).tail(1).index
    s.loc[month_end, "Dividends"] = s.loc[month_end, "Close"] * dist_rate / 12
    return s


def quality_report(code: str, df: pd.DataFrame) -> dict:
    """資料驗證：價格報酬、含息報酬（自算）與 yfinance 還原股價報酬交叉比對，並檢查常見資料問題。"""
    d = df.sort_index()
    first, last = d.index[0], d.index[-1]
    yrs = max((last - first).days / 365.25, 1 / 365.25)
    cagr = lambda r: (1 + r) ** (1 / yrs) - 1 if r > -1 else float("nan")
    price_ret = d["Close"].iloc[-1] / d["Close"].iloc[0] - 1
    tr_ret = ((d["Close"] + d["Dividends"]) / d["Close"].shift(1)).iloc[1:].prod() - 1  # 除息日再投入
    adj_ret = (d["Adj Close"].iloc[-1] / d["Adj Close"].iloc[0] - 1) if "Adj Close" in d else float("nan")
    n_div = int((d["Dividends"] > 0).sum())
    yield_est = d["Dividends"].sum() / yrs / d["Close"].mean()
    jumps = int((d["Close"].pct_change().abs() > 0.15).sum())  # 台股單日漲跌幅上限 10%，>15% 多半是分割未調整
    warns = []
    if n_div == 0:
        warns.append("未取得任何配息資料，股息可能被低估")
    if jumps:
        warns.append(f"有 {jumps} 天單日價格變動 >15%，可能有分割或資料錯誤")
    if not np.isnan(adj_ret) and abs(cagr(tr_ret) - cagr(adj_ret)) > 0.02:
        warns.append("自算含息報酬與還原股價年化差異 >2 個百分點，請核對")
    return {"標的": code, "資料起點": f"{first:%Y-%m-%d}", "配息次數": n_div,
            "估計年化殖利率": yield_est, "價格年化報酬": cagr(price_ret),
            "含息年化報酬(自算)": cagr(tr_ret), "含息年化報酬(Yahoo還原)": cagr(adj_ret),
            "狀態": "；".join(warns) if warns else "OK"}


# ---------------------------------------------------------------- 設定與結果
@dataclass
class Sleeve:
    """一個獨立資金來源。"""
    name: str
    pool: float                              # 初始資金池：借款所得或自有資金
    weights: dict                            # {標的: 權重}，自動正規化
    loan: Optional[LoanSpec] = None          # 對應貸款（自有資金為 None）
    mode: str = "lump"                       # lump 單筆 / dca 定期定額（分批）
    lump_amount: Optional[float] = None      # 單筆投入金額，None=全額；餘額留作閒置現金
    monthly_amount: float = 0.0              # 定期定額每月投入
    dca_external: bool = False               # 資金池用盡後是否以自有資金繼續扣款


@dataclass
class BacktestConfig:
    sleeves: list
    years: int = 10
    reinvest: bool = False                   # 股息是否再投入（預設否：股息拿來繳貸款）
    idle_rate: float = 0.01                  # 閒置現金年利率
    nhi_enabled: bool = True                 # 是否扣二代健保補充保費
    checkpoints: tuple = (3, 5, 10)


@dataclass
class BacktestResult:
    monthly: pd.DataFrame        # 欄位：date, m, mv, net, 及 'mv|來源'、'net|來源'
    snapshots: dict              # {結算標籤: {來源名或'合計': 指標 dict}}，依時間排序，最後為「期末」
    yearly: dict                 # {來源名或'合計': 逐年現金流 DataFrame}
    monthly_cf: dict             # {來源名或'合計': 逐月 股息/月付/差額 DataFrame}
    warnings: list
    eff_start: pd.Timestamp
    eff_end: pd.Timestamp
    sleeve_names: list


def _build_yearly(div_by_m: dict, pay_by_m: dict, n_complete: int) -> pd.DataFrame:
    cf = pd.DataFrame({"m": range(n_complete)})
    cf["dividend"] = cf["m"].map(lambda k: div_by_m.get(k, 0.0))
    cf["payment"] = cf["m"].map(lambda k: pay_by_m.get(k, 0.0))
    cf["year"] = cf["m"] // 12 + 1
    y = cf.groupby("year").agg(months=("m", "size"), dividend=("dividend", "sum"),
                               payment=("payment", "sum")).reset_index()
    y["diff"] = y["dividend"] - y["payment"]
    y["avg_monthly_dividend"] = y["dividend"] / y["months"]
    y["avg_monthly_payment"] = y["payment"] / y["months"]
    y["avg_monthly_arbitrage"] = y["avg_monthly_dividend"] - y["avg_monthly_payment"]
    return y


def _build_monthly(div_by_m: dict, pay_by_m: dict, labels: list, n_complete: int) -> pd.DataFrame:
    df = pd.DataFrame({"月份": labels[:n_complete]})
    df["股息收入"] = [div_by_m.get(k, 0.0) for k in range(n_complete)]
    df["貸款月付"] = [pay_by_m.get(k, 0.0) for k in range(n_complete)]
    df["套利現金流差額"] = df["股息收入"] - df["貸款月付"]
    df["累計差額"] = df["套利現金流差額"].cumsum()
    return df


# ---------------------------------------------------------------- 模擬
def run_backtest(assets: dict, cfg_: BacktestConfig) -> BacktestResult:
    warnings: list[str] = []

    S: dict[str, dict] = {}
    for sp in cfg_.sleeves:
        ws = {t: v for t, v in sp.weights.items() if t in assets and v > 0}
        dropped = [t for t, v in sp.weights.items() if v > 0 and t not in assets]
        if dropped:
            warnings.append(f"{sp.name}：{', '.join(dropped)} 無資料，已排除。")
        funded = sp.pool > 0 or (sp.loan and sp.loan.principal > 0) or (sp.dca_external and sp.monthly_amount > 0)
        if not funded:
            continue
        if not ws:
            warnings.append(f"{sp.name}：沒有可用標的，整個資金來源略過。")
            continue
        tot = sum(ws.values())
        principal = sp.loan.principal if sp.loan else 0.0
        S[sp.name] = dict(spec=sp, w={t: v / tot for t, v in ws.items()},
                          shares={t: 0.0 for t in ws}, pool=sp.pool, own_initial=sp.pool - principal,
                          principal=principal, div_cash=0.0, div_gross=0.0, div_net=0.0,
                          reinvested=0.0, paid=0.0, ext=0.0)
    if not S:
        raise ValueError("沒有可回測的資金來源（請確認金額、標的與資料）")
    tickers = sorted({t for s in S.values() for t in s["w"]})

    end = max(assets[t].index.max() for t in tickers)
    req_start = end - pd.DateOffset(years=cfg_.years)
    eff_start = req_start
    for t in tickers:
        first = assets[t].index.min()
        if first > req_start + pd.Timedelta(days=10):
            warnings.append(f"{t} 最早資料為 {first:%Y-%m-%d}，晚於請求起點，回測起點已順延。")
            eff_start = max(eff_start, first)
    idx = sorted(set().union(*[set(assets[t].index) for t in tickers]))
    px = pd.DataFrame({t: assets[t]["Close"] for t in tickers}).reindex(idx).ffill()
    px = px.loc[px.index >= eff_start].dropna()
    if px.empty:
        raise ValueError("可用交易日為空")
    n_days = len(px)

    divm = np.zeros((n_days, len(tickers)))     # 除息日對應到交易日
    for j, t in enumerate(tickers):
        s = assets[t]["Dividends"]
        for dt, amt in s[(s > 0) & (s.index >= px.index[0])].items():
            pos = px.index.searchsorted(dt)
            if pos < n_days:
                divm[pos, j] += amt

    # 結算月份：第 N 年底，以及各貸款到期月（貸款完全清償後的結算）
    ck: dict[int, str] = {12 * y: f"第{y}年" for y in cfg_.checkpoints}
    for st in S.values():
        L = st["spec"].loan
        if L and L.principal > 0:
            tag = f"{L.name}到期"
            ck[L.n_months] = f"{ck[L.n_months]}（{tag}）" if L.n_months in ck else f"第{L.years}年（{tag}）"

    div_by_m = {n: {} for n in S}
    pay_by_m = {n: {} for n in S}
    labels: list[str] = []
    snaps: dict[str, dict] = {}
    rows: list[dict] = []

    def buy(st: dict, amount: float, d) -> None:
        for t, wt in st["w"].items():
            st["shares"][t] += amount * wt / px.at[d, t]

    def snap_one(name: str, d, m: int) -> dict:
        st = S[name]
        L = st["spec"].loan
        mv = sum(sh * px.at[d, t] for t, sh in st["shares"].items())
        remaining = L.balance_after(m) if L else 0.0
        principal_paid = st["principal"] - remaining
        net_arb = st["div_cash"] - st["paid"]
        net = mv + st["pool"] + net_arb - remaining - st["ext"] - st["own_initial"]
        return dict(mv=mv, pool=st["pool"], cum_div_gross=st["div_gross"], cum_div_net=st["div_net"],
                    reinvested=st["reinvested"], cum_payments=st["paid"], principal_paid=principal_paid,
                    interest_paid=st["paid"] - principal_paid, remaining=remaining,
                    net_arb_cash=net_arb, external=st["ext"], own_initial=st["own_initial"], net=net)

    def snap_all(d, m: int) -> dict:
        out = {n: snap_one(n, d, m) for n in S}
        out[TOTAL] = {k: sum(v[k] for v in out.values()) for k in next(iter(out.values()))}
        return out

    def flat(sn: dict, d, m: int) -> dict:
        row = {"date": d, "m": m, "mv": sn[TOTAL]["mv"], "net": sn[TOTAL]["net"]}
        for n in S:
            row[f"mv|{n}"], row[f"net|{n}"] = sn[n]["mv"], sn[n]["net"]
        return row

    m, last_key = -1, None
    for i, d in enumerate(px.index):
        new_month = (d.year, d.month) != last_key
        if new_month:
            last_key = (d.year, d.month)
            m += 1
            labels.append(f"{d:%Y-%m}")
            if m > 0:
                for n, st in S.items():
                    st["pool"] *= 1 + cfg_.idle_rate / 12
                    L = st["spec"].loan
                    pay = L.payment if (L and m <= L.n_months) else 0.0
                    st["paid"] += pay
                    pay_by_m[n][m - 1] = pay_by_m[n].get(m - 1, 0.0) + pay

        for j, t in enumerate(tickers):         # 除息：用除息前持股；補充保費按「同標的當日全帳戶」合併判斷
            per_share = divm[i, j]
            if per_share <= 0:
                continue
            total_sh = sum(st["shares"].get(t, 0.0) for st in S.values())
            if total_sh <= 0:
                continue
            factor = (1 - cfg.NHI_RATE) if (cfg_.nhi_enabled and per_share * total_sh >= cfg.NHI_THRESHOLD) else 1.0
            for n, st in S.items():
                sh = st["shares"].get(t, 0.0)
                if sh <= 0:
                    continue
                gross = per_share * sh
                net = gross * factor
                st["div_gross"] += gross
                st["div_net"] += net
                div_by_m[n][m] = div_by_m[n].get(m, 0.0) + net
                if cfg_.reinvest:
                    st["shares"][t] += net / px.at[d, t]
                    st["reinvested"] += net
                else:
                    st["div_cash"] += net

        if new_month:
            if m in ck:
                snaps[ck[m]] = snap_all(d, m)
            for n, st in S.items():
                sp = st["spec"]
                if sp.mode == "lump":
                    if m == 0:
                        amt = st["pool"] if sp.lump_amount is None else min(sp.lump_amount, st["pool"])
                        st["pool"] -= amt
                        if amt > 0:
                            buy(st, amt, d)
                elif sp.monthly_amount > 0:
                    take = min(st["pool"], sp.monthly_amount)
                    st["pool"] -= take
                    short = sp.monthly_amount - take
                    if sp.dca_external and short > 0:
                        st["ext"] += short
                        take += short
                    if take > 0:
                        buy(st, take, d)
            rows.append(flat(snap_all(d, m), d, m))

    last_d = px.index[-1]
    final = snap_all(last_d, m)
    snaps["期末"] = final
    rows.append(flat(final, last_d, m))
    monthly = pd.DataFrame(rows).drop_duplicates(subset="date", keep="last").reset_index(drop=True)

    tot_div = {k: sum(div_by_m[n].get(k, 0.0) for n in S) for k in range(m)}
    tot_pay = {k: sum(pay_by_m[n].get(k, 0.0) for n in S) for k in range(m)}
    yearly = {n: _build_yearly(div_by_m[n], pay_by_m[n], m) for n in S}
    yearly[TOTAL] = _build_yearly(tot_div, tot_pay, m)
    mcf = {n: _build_monthly(div_by_m[n], pay_by_m[n], labels, m) for n in S}
    mcf[TOTAL] = _build_monthly(tot_div, tot_pay, labels, m)
    return BacktestResult(monthly, snaps, yearly, mcf, warnings, px.index[0], last_d, list(S))
