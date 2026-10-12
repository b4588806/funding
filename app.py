"""Streamlit 前端。啟動：python3 -m streamlit run app.py"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import backtest as bt
import config as cfg
import db
import etl
import loan

# 投資標的固定為 8 種（安聯為假設模型，其餘 7 檔以 yfinance 取得）
ALLIANZ = "安聯收益成長基金"
TARGETS = [ALLIANZ, "0050", "006208", "0056", "00878", "00919", "0052", "00940"]
NAMES = {ALLIANZ: ALLIANZ, "0050": "0050 元大台灣50", "006208": "006208 富邦台50",
         "0056": "0056 元大高股息", "00878": "00878 國泰永續高股息",
         "00919": "00919 群益台灣精選高息", "0052": "0052 富邦科技", "00940": "00940 元大台灣價值高息"}
STAT_COLS = ["building_type", "n", "mean_price", "median_price", "p25", "p75"]
BORROW_MODES = ["滿額 (100%)", "輸入比例 %", "輸入金額"]

twd = lambda x: f"NT$ {x:,.0f}"
wan = lambda x: f"{x / 10000:,.1f} 萬"
signed = lambda x: f"{x:+,.0f}"

st.set_page_config(page_title="房貸・信貸槓桿套利試算", layout="wide")
# 縮小 Metric 數字，並允許換行，避免大額數字被截斷
st.markdown("""<style>
[data-testid="stMetricValue"] {font-size: 1.45rem; line-height: 1.25;}
[data-testid="stMetricValue"] > div {white-space: normal !important; overflow: visible !important;
    text-overflow: clip !important; word-break: break-all;}
[data-testid="stMetricLabel"] p {font-size: 0.9rem;}
</style>""", unsafe_allow_html=True)
st.title("房貸・信貸槓桿套利試算")

# ------------------------------------------------------------ 側邊欄：資料庫
with st.sidebar:
    st.header("實價登錄資料庫")
    ready = db.db_ready()
    if ready:
        st.success(f"已就緒　最後更新：{db.last_updated()}")
    else:
        st.warning("尚未建立資料庫。請指定已下載的資料夾後按「建立資料庫」。")
    if st.session_state.get("etl_msg"):
        st.info(st.session_state.pop("etl_msg"))

    raw_dir = st.text_input("原始資料夾（可含子資料夾與 .zip）", str(cfg.RAW_DIR))
    lookback = st.number_input("取樣天數（預設近一年）", 30, 3650, cfg.LOOKBACK_DAYS, 30)

    def _run(download: bool) -> None:
        bar = st.progress(0.0, text="準備中…")
        try:
            r = etl.run_etl(download=download, raw_dir=raw_dir, lookback_days=int(lookback),
                            progress=lambda p, msg: bar.progress(min(p, 1.0), text=msg))
            msg = (f"完成：{r['cities']} 個縣市、{r['districts']} 個行政區，"
                   f"有效交易 {r['rows']:,} 筆（原始 {r['raw_rows']:,} 筆）。")
            if r["failed"]:
                msg += f" 下載失敗 {len(r['failed'])} 項。"
            st.session_state["etl_msg"] = msg
            st.rerun()
        except Exception as exc:
            st.error(f"ETL 失敗：{exc}")

    if st.button("用本機檔案建立 / 更新資料庫", type="primary"):
        _run(False)
    with st.expander("從內政部下載近 5 季（選用）"):
        if st.button("下載並建立"):
            _run(True)
    st.caption("行情僅供參考，實際鑑價以銀行為準。")

tab1, tab2, tab3 = st.tabs(["① 行情與額度", "② 回測設定", "③ 套利結算"])


# ============================================================ 第一頁
def choose_principal(cap: float, key: str) -> float:
    """借款金額：滿額 / 輸入比例 / 輸入金額。超過上限一律以上限計，不會 KeyError 或超貸。"""
    mode = st.radio("借款方式", BORROW_MODES, horizontal=True, key=f"{key}_mode")
    if cap <= 0:
        st.info("可貸上限為 0，無法借款。")
        return 0.0
    if mode == BORROW_MODES[0]:
        amt = cap
    elif mode == BORROW_MODES[1]:
        pct = st.number_input("借款比例 %", 0.0, 100.0, 100.0, 1.0, key=f"{key}_pct")
        amt = cap * pct / 100
    else:
        amt = float(st.number_input("借款金額 (NTD)", min_value=0, value=int(cap), step=100_000, key=f"{key}_amt"))
        if amt > cap:
            st.warning("超過可貸上限，已自動以上限計算。")
            amt = cap
    st.caption(f"實際借款 **{twd(amt)}**（占上限 {amt / cap:.0%}）")
    return float(amt)


with tab1:
    # ---------- ① 房屋與收入
    with st.container(border=True):
        st.markdown("##### ① 房屋與收入")
        c1, c2, c3 = st.columns(3)
        city = c1.selectbox("縣市", db.list_cities() if ready else list(cfg.CITY_CODES))
        districts = db.list_districts(city) if ready else []
        district = (c2.selectbox("行政區", districts) if districts
                    else c2.text_input("行政區（資料庫尚無資料，可自行輸入）"))
        prop_type = c3.radio("房屋型態", ["透天", "大樓"], horizontal=True)

        stats = (db.district_stats(city, district) if (ready and district)
                 else pd.DataFrame(columns=STAT_COLS))  # 無資料時仍保留欄位，避免 KeyError
        if stats.empty:
            st.info("查無該行政區行情，可改用手動輸入市值或單價。")
        else:
            show = stats.rename(columns={"building_type": "型態", "n": "樣本數", "mean_price": "平均單價",
                                         "median_price": "中位數單價", "p25": "25百分位", "p75": "75百分位"})
            st.caption("近一年成交行情（元/坪，已排除特殊交易與離群值）")
            st.dataframe(show.style.format({c: "{:,.0f}" for c in show.columns if c != "型態"}),
                         hide_index=True, use_container_width=True)

        basis = st.radio("估值採用單價", ["平均", "中位數"], horizontal=True)
        unit_price = None
        row = stats[stats["building_type"] == prop_type]
        if ready and (row.empty or int(row.iloc[0]["n"]) < cfg.MIN_SAMPLES_WARN):
            city_df = db.city_stats(city)
            crow = city_df[city_df["building_type"] == prop_type]
            if not crow.empty:
                st.warning(f"{district or '該行政區'}「{prop_type}」樣本不足，估值改用 {city} 整體"
                           f"「{prop_type}」行情（{int(crow.iloc[0]['n'])} 筆）。")
                row = crow
        if not row.empty:
            unit_price = float(row.iloc[0]["mean_price" if basis == "平均" else "median_price"])

        mode = st.radio("房屋市值輸入方式", ["直接輸入市值", "輸入坪數（自動帶入行情單價）"], horizontal=True)
        i1, i2, i3, i4 = st.columns(4)
        if mode.startswith("直接"):
            value = float(i1.number_input("房屋市值 (NTD)", min_value=0, value=15_000_000, step=500_000))
        else:
            ping = i1.number_input("坪數", min_value=0.0, value=35.0, step=1.0)
            if unit_price is None:
                unit_price = float(i1.number_input("手動單價 (NTD/坪)", min_value=0, value=400_000, step=10_000))
            value = ping * unit_price
            i1.caption(f"{ping:,.1f} 坪 × {unit_price:,.0f} = {twd(value)}")
        salary = float(i2.number_input("個人月薪 (NTD)", min_value=0, value=80_000, step=5_000))
        existing = float(i3.number_input("目前未償房貸餘額 (NTD)", min_value=0, value=0, step=100_000))
        other_debt = float(i4.number_input("既有負債月付額 (NTD)", min_value=0, value=0, step=1_000,
                                           help="含原房貸月付、車貸、卡循等，會計入 DSR"))

    if value <= 0 or salary <= 0:
        st.session_state.pop("plan", None)
        st.info("請輸入大於 0 的市值與月薪以開始試算。")
    else:
        mort_cap = loan.mortgage_capacity(value, existing)        # 市值 × 80% − 原房貸餘額
        # ---------- ② 貸款設定
        with st.container(border=True):
            st.markdown("##### ② 貸款設定")
            mc, cc = st.columns(2)
            with mc:
                st.markdown(f"**房貸 / 增貸**　可貸上限 {twd(mort_cap)}")
                r1, r2 = st.columns(2)
                m_rate = r1.number_input("房貸利率 %", 0.0, 10.0, cfg.DEFAULT_MORTGAGE["rate"], 0.05)
                m_years = int(r2.number_input("房貸年限", 1, 40, cfg.DEFAULT_MORTGAGE["years"]))
                m_principal = choose_principal(mort_cap, "borrow_mort")
            with cc:
                mult = st.number_input("信貸倍數（月薪 × N；法規上限 22 倍，可下調）",
                                       1.0, float(cfg.DBR_MULTIPLE), float(cfg.DBR_MULTIPLE), 1.0)
                cred_cap = loan.credit_capacity(salary, mult)
                st.markdown(f"**信貸**　可貸上限 {twd(cred_cap)}")
                r3, r4 = st.columns(2)
                c_rate = r3.number_input("信貸利率 %", 0.0, 20.0, cfg.DEFAULT_CREDIT["rate"], 0.05)
                c_years = int(r4.number_input("信貸年限", 1, 10, cfg.DEFAULT_CREDIT["years"]))
                c_principal = choose_principal(cred_cap, "borrow_cred")

        mortgage = loan.LoanSpec("房貸", m_principal, m_rate, m_years)
        credit = loan.LoanSpec("信貸", c_principal, c_rate, c_years)
        new_pay = mortgage.payment + credit.payment
        dsr = loan.dsr_check(salary, new_pay, other_debt)

        # ---------- ③ 試算結果
        with st.container(border=True):
            st.markdown("##### ③ 試算結果")
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("房貸可貸上限", wan(mort_cap))
            k1.caption(f"{twd(mort_cap)}｜市值×80%−原房貸")
            k2.metric("信貸可貸上限", wan(cred_cap))
            k2.caption(f"{twd(cred_cap)}｜月薪×{mult:g}")
            k3.metric("預計每月總月付", twd(new_pay))
            k3.caption(f"房貸 {twd(mortgage.payment)}＋信貸 {twd(credit.payment)}")
            k4.metric("DSR 佔比", f"{dsr['ratio']:.1%}",
                      delta=f"{dsr['ratio'] - cfg.DSR_LIMIT:+.1%} 對紅線 {cfg.DSR_LIMIT:.0%}",
                      delta_color="inverse")
            k4.caption(f"含既有負債 {twd(dsr['total'])}｜上限 {twd(dsr['cap'])}")
            st.progress(min(dsr["total"] / dsr["cap"], 1.0) if dsr["cap"] > 0 else 1.0,
                        text=f"已使用安全額度 {dsr['total'] / dsr['cap']:.0%}（100% = 月薪 {cfg.DSR_LIMIT:.0%} 紅線）")

            if dsr["ok"]:
                st.success(f"✅ 通過：月付總額 {twd(dsr['total'])} ≤ 紅線 {twd(dsr['cap'])}，"
                           f"每月尚有 {twd(dsr['headroom'])} 空間。")
                st.session_state["plan"] = {"mortgage": mortgage, "credit": credit}
            else:
                excess = -dsr["headroom"]
                st.session_state.pop("plan", None)  # 超標時不放行到第②頁
                st.error(f"🚨 超過 DSR 紅線！月付總額 {twd(dsr['total'])} > 上限 {twd(dsr['cap'])}，"
                         f"每月超出 **{twd(excess)}**。請先調整借款再進入回測。")
                tips = loan.dsr_advice(excess, mortgage, credit)
                if tips:
                    st.markdown("**調整方式（擇一或組合）：**\n" + "\n".join(f"- {t}" for t in tips))

            with st.expander("計算過程"):
                st.markdown(f"""
- 房貸上限 = {value:,.0f} × {cfg.LTV:.0%} − {existing:,.0f} = **{mort_cap:,.0f}**
- 信貸上限 = {salary:,.0f} × {mult:g} = **{cred_cap:,.0f}**
- 月付公式：`PMT = P·r·(1+r)^n / ((1+r)^n − 1)`，r = 年利率/12
- 房貸：P={mortgage.principal:,.0f}，r={m_rate / 1200:.6f}，n={mortgage.n_months} → **{mortgage.payment:,.0f}**
- 信貸：P={credit.principal:,.0f}，r={c_rate / 1200:.6f}，n={credit.n_months} → **{credit.payment:,.0f}**
- DSR 紅線 = {salary:,.0f} × {cfg.DSR_LIMIT:.0%} = {dsr['cap']:,.0f}；
  實際 = {mortgage.payment:,.0f} + {credit.payment:,.0f} + {other_debt:,.0f} = {dsr['total']:,.0f}
""")


# ============================================================ 第二頁
@st.cache_data(ttl=12 * 3600, show_spinner=False)
def load_tw_etf(code: str, start: str) -> pd.DataFrame:
    return bt.fetch_tw_etf(code, start)


def weights_ui(key: str, default_targets: list[str]) -> dict:
    sel = st.multiselect("投資標的（固定 8 選）", TARGETS, default=default_targets,
                         format_func=lambda c: NAMES.get(c, c), key=f"{key}_sel")
    weights: dict = {}
    if sel:
        for col, t in zip(st.columns(len(sel)), sel):
            weights[t] = col.number_input(f"{t} 權重", 0.0, 100.0, round(100 / len(sel), 1), 5.0, key=f"{key}_w_{t}")
    return weights


def loan_sleeve(key: str, name: str, spec, default_targets: list[str]):
    """房貸/信貸資金來源：本金、利率、年限、月付全部取自第①頁，唯讀。"""
    with st.container(border=True):
        st.markdown(f"**{name}**（資料來自第①頁）")
        if spec is None or spec.principal <= 0:
            st.info("第①頁尚無此貸款（借款金額為 0 或 DSR 超標）。")
            return None
        a, b, c, d = st.columns(4)
        a.metric("投入本金", wan(spec.principal))
        b.metric("利率 / 年限", f"{spec.annual_rate_pct:g}% / {spec.years} 年")
        c.metric("每月月付", twd(spec.payment))
        d.metric("貸款總利息", wan(spec.payment * spec.n_months - spec.principal))
        if not st.checkbox("啟用此資金來源", value=True, key=f"{key}_en"):
            return None
        weights = weights_ui(key, default_targets)
        strat = st.radio("投入策略", ["單筆全額投入", "分批投入"], horizontal=True, key=f"{key}_mode")
        monthly = 0.0
        if strat == "分批投入":
            n = int(st.number_input("平均分幾個月投入", 1, 120, 12, key=f"{key}_n"))
            monthly = spec.principal / n
            st.caption(f"每月投入 {twd(monthly)}，未投入部分以閒置現金計息。")
        return bt.Sleeve(name=name, pool=spec.principal, weights=weights, loan=spec,
                         mode="lump" if strat == "單筆全額投入" else "dca", monthly_amount=monthly)


def own_sleeve(key: str, name: str, default_targets: list[str]):
    with st.container(border=True):
        st.markdown(f"**{name}**（本身現金，不涉及貸款）")
        if not st.checkbox("啟用此資金來源", value=False, key=f"{key}_en"):
            return None
        pool = float(st.number_input("自有資金 (NTD)", 0, value=1_000_000, step=100_000, key=f"{key}_pool"))
        weights = weights_ui(key, default_targets)
        strat = st.radio("投入策略", ["單筆全額投入", "定期定額"], horizontal=True, key=f"{key}_mode")
        monthly, ext = 0.0, False
        if strat == "定期定額":
            monthly = float(st.number_input("每月投入金額 (NTD)", 0, value=20_000, step=5_000, key=f"{key}_mo"))
            ext = st.checkbox("資金用完後以薪資自有資金續扣", value=True, key=f"{key}_ext")
        return bt.Sleeve(name=name, pool=pool, weights=weights, loan=None,
                         mode="lump" if strat == "單筆全額投入" else "dca", monthly_amount=monthly, dca_external=ext)


with tab2:
    plan = st.session_state.get("plan") or {}
    if not plan:
        st.warning("尚未取得第①頁的有效借款方案（未試算，或 DSR 超過紅線）。房貸／信貸資金暫不可用，僅可回測自有資金。")

    with st.expander(f"{ALLIANZ} 假設參數（選用該標的時生效）"):
        al_ret = st.number_input("假設年化總報酬 %", -10.0, 30.0, 6.0, 0.5)
        al_dist = st.number_input("假設年配息率 %", 0.0, 20.0, 7.0, 0.5)
        st.caption("無歷史淨值，採假設模型；美元計價，未計匯率與配息含本金之影響。")

    sleeves = [loan_sleeve("mort", "房貸資金", plan.get("mortgage"), ["0050"]),
               loan_sleeve("cred", "信貸資金", plan.get("credit"), ["0056"]),
               own_sleeve("own", "自有資金", ["0050"])]
    sleeves = [x for x in sleeves if x is not None]

    with st.expander("進階設定"):
        g1, g2, g3, g4 = st.columns(4)
        reinvest = g1.checkbox("股息再投入（預設否：股息拿來繳貸款）", value=False)
        idle = g2.number_input("閒置現金年利率 %", 0.0, 5.0, 1.0, 0.1)
        nhi = g3.checkbox("扣二代健保 2.11%（單次 ≥ 2 萬）", value=True)
    years = int(st.number_input("回測年數（1–10）", 1, 10, 5))

    if st.button("執行回測", type="primary", disabled=not sleeves):
        start = (pd.Timestamp.today() - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
        needed = sorted({t for sp in sleeves for t, w_ in sp.weights.items() if w_ > 0})
        assets, errors = {}, []
        with st.spinner("取得歷史資料…"):
            for t in needed:
                if t == ALLIANZ:
                    assets[t] = bt.make_synthetic_fund(start, None, al_ret / 100, al_dist / 100)
                    continue
                try:
                    assets[t] = load_tw_etf(t, start)
                except Exception as exc:
                    errors.append(f"{t}：{exc}")
        for e in errors:
            st.error(e)
        if assets:
            try:
                conf = bt.BacktestConfig(sleeves=sleeves, years=years, reinvest=reinvest, idle_rate=idle / 100,
                                         nhi_enabled=nhi, checkpoints=tuple(y for y in (3, 5, 10) if y <= years))
                st.session_state["bt"] = bt.run_backtest(assets, conf)
                st.session_state["bt_quality"] = pd.DataFrame(
                    [bt.quality_report(t, assets[t]) for t in assets if t != ALLIANZ])
            except Exception as exc:
                st.error(f"回測失敗：{exc}")

    res = st.session_state.get("bt")
    if res:
        for w_ in res.warnings:
            st.warning(w_)
        st.caption(f"實際回測區間：{res.eff_start:%Y-%m-%d} ～ {res.eff_end:%Y-%m-%d}　→ 詳細結算請看「③ 套利結算」")
        end = res.snapshots["期末"][bt.TOTAL]
        q1, q2, q3 = st.columns(3)
        q1.metric("期末投資市值（合計）", wan(end["mv"]))
        q2.metric("累積稅後股息（合計）", wan(end["cum_div_net"]))
        q3.metric("淨資產盈餘差（合計）", wan(end["net"]))
        qdf = st.session_state.get("bt_quality")
        if qdf is not None and not qdf.empty:
            with st.expander("資料驗證（股價、股息、含息報酬交叉比對）", expanded=bool((qdf["狀態"] != "OK").any())):
                pct = {c: "{:.2%}" for c in qdf.columns if "報酬" in c or "殖利率" in c}
                st.dataframe(qdf.style.format(pct), hide_index=True, use_container_width=True)
                st.caption("回測採「未還原股價＋現金股利」計算含息報酬；Yahoo 還原股價僅供交叉比對。")


# ============================================================ 第三頁
def signed_style(df: pd.DataFrame, cols: list[str], fmt_cols: list[str]):
    fn = lambda v: "color:#d62728" if v < 0 else ("color:#2ca02c" if v > 0 else "")
    sty = df.style.format({c: "{:,.0f}" for c in fmt_cols})
    return (sty.map if hasattr(sty, "map") else sty.applymap)(fn, subset=cols)


with tab3:
    res = st.session_state.get("bt")
    if not res:
        st.info("請先在「② 回測設定」執行回測。")
    else:
        names = [bt.TOTAL] + res.sleeve_names
        who = st.radio("檢視對象", names, horizontal=True)
        labels = list(res.snapshots)                       # 依時間排序，最後為「期末」
        S = {lab: res.snapshots[lab][who] for lab in labels}
        has_loan = any(v["cum_payments"] > 0 or v["remaining"] > 0 for v in S.values())

        # ---------- A. 現金流對比
        st.markdown("### A. 每月股息收入 vs 貸款月付（套利現金流）")
        y = res.yearly[who]
        if y.empty:
            st.info("回測期間不足一個完整月份。")
        else:
            ytab = pd.DataFrame({"第N年": y["year"], "年度股息": y["dividend"], "年度月付": y["payment"],
                                 "年度套利差額": y["diff"], "月均股息": y["avg_monthly_dividend"],
                                 "月均月付": y["avg_monthly_payment"], "月均套利差額": y["avg_monthly_arbitrage"]})
            money_cols = [c for c in ytab.columns if c != "第N年"]
            st.dataframe(signed_style(ytab, ["年度套利差額", "月均套利差額"], money_cols),
                         hide_index=True, use_container_width=True)
            st.caption("正數＝股息大於月付（多賺）；負數＝現金流缺口（需自掏腰包）。")
            cf = res.monthly_cf[who]
            st.bar_chart(cf.set_index("月份")["套利現金流差額"])
            with st.expander("逐月明細"):
                st.dataframe(signed_style(cf, ["套利現金流差額", "累計差額"], ["股息收入", "貸款月付", "套利現金流差額", "累計差額"]),
                             hide_index=True, use_container_width=True)

        # ---------- B. 本金抵銷結算
        st.markdown("### B. 本金抵銷：貸款有多少是靠股息繳掉的")
        if not has_loan:
            st.info("此對象沒有貸款，無本金抵銷可計算。")
        else:
            def cov_row(v: dict) -> list[str]:
                pay, div, pp = v["cum_payments"], v["cum_div_net"], v["principal_paid"]
                cov = div / pay if pay > 0 else None
                loan_total = pp + v["remaining"]
                return [f"{pay:,.0f}", f"{pp:,.0f}", f"{v['interest_paid']:,.0f}",
                        f"{pp / loan_total:.1%}" if loan_total > 0 else "—",
                        f"{div:,.0f}", f"{cov:.0%}" if cov is not None else "—",
                        f"{min(div, pay):,.0f}", f"{pp * min(1.0, cov):,.0f}" if cov is not None else "—",
                        f"{max(0.0, pay - div):,.0f}"]
            idx = ["貸款累積繳款（本息）", "　其中已還本金", "　其中利息", "已還本金占借款 %",
                   "累積稅後股息", "股息覆蓋率（股息 ÷ 繳款）", "靠股息繳掉的金額",
                   "　其中約幫忙還本金（依比例估算）", "仍需自掏腰包的金額"]
            st.dataframe(pd.DataFrame({lab: cov_row(v) for lab, v in S.items()}, index=idx),
                         use_container_width=True)
            st.caption("覆蓋率 ≥ 100% 代表股息足以支付全部月付。若勾選「股息再投入」，股息已轉入投資而非用於繳款。")

        # ---------- C. 最終現金結算
        st.markdown("### C. 最終現金結算（假設結算日全數賣出並清償剩餘貸款）")
        e = S["期末"]
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("投資標的總市值", wan(e["mv"]))
        m2.metric("剩餘未償貸款", wan(e["remaining"]))
        m3.metric("套利現金累計（股息−月付）", wan(e["net_arb_cash"]))
        m4.metric("淨資產盈餘差", wan(e["net"]))
        for col, v in zip((m1, m2, m3, m4), (e["mv"], e["remaining"], e["net_arb_cash"], e["net"])):
            col.caption(twd(v))

        def settle_row(v: dict) -> list[str]:
            return [f"{v['mv']:,.0f}", f"{v['pool']:,.0f}", f"-{v['remaining']:,.0f}",
                    f"{v['mv'] + v['pool'] - v['remaining']:,.0f}", signed(v["net_arb_cash"]),
                    f"-{v['external']:,.0f}", f"-{v['own_initial']:,.0f}" if v["own_initial"] else "0",
                    signed(v["net"])]
        idx = ["投資標的總市值", "＋閒置現金", "－剩餘貸款", "＝賣出並清償後剩餘",
               "套利現金累計（股息−月付）", "－額外自掏腰包", "－自有本金", "＝淨資產盈餘差"]
        st.dataframe(pd.DataFrame({lab: settle_row(v) for lab, v in S.items()}, index=idx),
                     use_container_width=True)
        st.caption("標示「到期」的欄位為該貸款完全清償當月的結算；房貸若尚未到期，會以剩餘本金計入負債。")

        shock = st.number_input("壓力測試：投資標的市值下跌 %", 0, 90, 30)
        stress = pd.DataFrame({lab: [f"{v['net']:,.0f}", f"{v['net'] - v['mv'] * shock / 100:,.0f}"]
                               for lab, v in S.items()}, index=["原淨資產盈餘差", f"市值下跌 {shock}% 後"])
        st.dataframe(stress, use_container_width=True)

        st.warning("風險提示：歷史績效不代表未來；槓桿投資在市場下跌時損失放大，且貸款月付固定、股息不固定。"
                   "利率若為機動，月付額可能上升；本工具未計入證交稅、手續費、股利所得稅、匯率與通膨。"
                   "非投資建議，重大決策請諮詢合格理財顧問與銀行。")
