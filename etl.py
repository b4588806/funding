"""實價登錄 ETL：讀取本機檔案（或下載）-> 清洗 -> 離群值處理 -> 寫入 SQLite。

支援的本機資料來源（遞迴掃描 raw_dir，含子資料夾）：
  * 任意位置的 {代碼}_lvr_land_a.csv（例如 113S4/a_lvr_land_a.csv、a_lvr_land_a.csv）
  * 內政部整包下載的 .zip（內含各縣市 CSV）
指令列：python etl.py --no-download [--raw-dir 路徑] [--lookback-days 365]
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import requests

import config as cfg

log = logging.getLogger(__name__)
CODE_TO_CITY = {v: k for k, v in cfg.CITY_CODES.items()}
FILE_RE = re.compile(r"(?:^|_)([a-z])_lvr_land_a\.csv$", re.I)


# ---------------------------------------------------------------- 下載（選用）
def recent_seasons(today: Optional[date] = None, n: int = cfg.SEASONS_TO_FETCH) -> list[str]:
    """回傳最近 n 個民國季別字串，例如 ['115S4', '115S3', ...]。"""
    today = today or date.today()
    roc, q = today.year - 1911, (today.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append(f"{roc}S{q}")
        q -= 1
        if q == 0:
            q, roc = 4, roc - 1
    return out


def download_season(season: str, code: str, force: bool = False,
                    raw_dir: Optional[Path] = None) -> Optional[Path]:
    """下載單一季別/縣市 CSV；失敗時回傳既有檔案（若有）或 None。"""
    raw_dir = Path(raw_dir or cfg.RAW_DIR)
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{season}_{code}_lvr_land_a.csv"
    if path.exists() and not force:
        if (time.time() - path.stat().st_mtime) / 86400 < cfg.REFRESH_AFTER_DAYS:
            return path
    url = cfg.DOWNLOAD_URL.format(season=season, code=code)
    try:
        resp = requests.get(url, timeout=90, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = resp.content.decode("utf-8-sig", errors="replace")
        if "鄉鎮市區" not in text[:300]:
            log.warning("內容格式不符，略過：%s", url)
            return path if path.exists() else None
        path.write_text(text, encoding="utf-8")
        return path
    except requests.RequestException as exc:
        log.warning("下載失敗 %s：%s", url, exc)
        return path if path.exists() else None


# ---------------------------------------------------------------- 讀取
def _read_csv(src) -> pd.DataFrame:
    # 第 2 列為英文欄名，需略過
    return pd.read_csv(src, dtype=str, encoding="utf-8-sig", skiprows=[1])


def load_raw_files(raw_dir: Optional[Path] = None) -> pd.DataFrame:
    """遞迴讀取 raw_dir 內所有買賣 CSV / ZIP，加上縣市欄位後合併。"""
    raw_dir = Path(raw_dir or cfg.RAW_DIR)
    frames: list[pd.DataFrame] = []

    def add(df: pd.DataFrame, code: str) -> None:
        city = CODE_TO_CITY.get(code.lower())
        if city:
            df["city"] = city
            frames.append(df)

    def skip(name: str) -> bool:
        # macOS 壓縮時夾帶的 __MACOSX 資料夾與 "._" 開頭的中繼檔不是真正的 CSV
        return name.startswith("._") or "__MACOSX" in name

    for p in sorted(raw_dir.rglob("*")):
        if not p.is_file() or skip(p.as_posix()) and p.suffix.lower() != ".zip":
            continue
        suffix = p.suffix.lower()
        if suffix == ".csv":
            m = FILE_RE.search(p.name)
            if m:
                try:
                    add(_read_csv(p), m.group(1))
                except Exception as exc:  # 單一檔案毀損不應中斷整批作業
                    log.warning("讀取失敗 %s：%s", p, exc)
        elif suffix == ".zip":
            try:
                zf = zipfile.ZipFile(p)
            except Exception as exc:
                log.warning("無法開啟 %s：%s", p, exc)
                continue
            with zf:
                for name in zf.namelist():
                    if skip(name):
                        continue
                    m = FILE_RE.search(Path(name).name)
                    if not m:
                        continue
                    try:
                        with zf.open(name) as f:
                            add(_read_csv(f), m.group(1))
                    except Exception as exc:  # 逐檔處理，壞檔不影響同一個 zip 內其他檔案
                        log.warning("讀取失敗 %s!%s：%s", p.name, name, exc)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


# ---------------------------------------------------------------- 清洗
def parse_roc_date(series: pd.Series) -> pd.Series:
    """民國日期字串（如 1130815）轉西元 Timestamp；無法解析為 NaT。"""
    s = series.fillna("").str.strip().str.zfill(7)
    year = pd.to_numeric(s.str[:-4], errors="coerce") + 1911
    text = year.astype("Int64").astype(str) + s.str[-4:]
    return pd.to_datetime(text, format="%Y%m%d", errors="coerce")


def classify_building(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    for keyword, label in cfg.BUILDING_TYPE_MAP:
        if keyword in value:
            return label
    return None


def clean(raw: pd.DataFrame, today: Optional[date] = None,
          lookback_days: int = cfg.LOOKBACK_DAYS) -> pd.DataFrame:
    """欄位正規化、期間篩選、型態分類、特殊交易排除與單價換算。"""
    today = today or date.today()
    df = raw.copy()
    df.columns = [re.sub(r"[()（）\s]", "", str(c)) for c in df.columns]

    required = ["鄉鎮市區", "交易標的", "交易年月日", "建物型態", "建物移轉總面積平方公尺", "總價元"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"CSV 缺少必要欄位：{missing}（內政部欄位可能改版）")
    for col in ("車位移轉總面積平方公尺", "車位總價元"):
        if col not in df.columns:
            df[col] = "0"
    for col in ("備註", "編號", "土地位置建物門牌", "總樓層數"):
        if col not in df.columns:
            df[col] = ""

    df["trade_date"] = parse_roc_date(df["交易年月日"])
    df = df[df["trade_date"] >= pd.Timestamp(today - timedelta(days=lookback_days))]

    df["building_type"] = df["建物型態"].map(classify_building)
    df = df[df["building_type"].notna()]
    df = df[df["交易標的"].fillna("").str.contains("房地")]
    df = df[~df["備註"].fillna("").str.contains(cfg.SPECIAL_REMARK_PATTERN)]

    num = lambda c: pd.to_numeric(df[c], errors="coerce")
    # 扣除車位後的淨建物面積與淨總價，避免車位拉高/稀釋單價
    area_sqm = num("建物移轉總面積平方公尺") - num("車位移轉總面積平方公尺").fillna(0)
    price = num("總價元") - num("車位總價元").fillna(0)
    df = df.assign(_area=area_sqm, _price=price)
    df = df[(df["_area"] > 0) & (df["_price"] > 0)]

    df["unit_price_ping"] = df["_price"] / df["_area"] * cfg.SQM_TO_PING  # 元/平方公尺 × 3.30578
    df["area_ping"] = df["_area"] / cfg.SQM_TO_PING
    df = df[df["area_ping"] >= cfg.MIN_AREA_PING]
    df = df[df["unit_price_ping"].between(cfg.MIN_UNIT_PRICE_PING, cfg.MAX_UNIT_PRICE_PING)]

    # 跨季/跨檔重複紀錄（以編號去重；各縣市編號不會互撞，仍併入縣市作為 key）
    key = df["city"] + "|" + df["編號"].fillna("")
    df = df[~(df["編號"].fillna("").ne("") & key.duplicated())]

    out = df.rename(columns={"鄉鎮市區": "district", "土地位置建物門牌": "address",
                             "總樓層數": "floors", "編號": "serial_no"})
    out = out.assign(total_price=df["_price"])
    out["trade_date"] = out["trade_date"].dt.strftime("%Y-%m-%d")
    cols = ["city", "district", "building_type", "trade_date", "address",
            "floors", "total_price", "area_ping", "unit_price_ping", "serial_no"]
    return out[cols].reset_index(drop=True)


def trim_outliers(df: pd.DataFrame) -> pd.DataFrame:
    """同縣市/行政區/型態內以 IQR 法剔除離群單價（樣本過少的群組不剔除）。"""
    keys = ["city", "district", "building_type"]
    g = df.groupby(keys)["unit_price_ping"]
    q1 = g.transform(lambda s: s.quantile(0.25))
    q3 = g.transform(lambda s: s.quantile(0.75))
    n = g.transform("size")
    iqr = q3 - q1
    k = cfg.IQR_MULTIPLIER
    keep = (n < cfg.MIN_GROUP_FOR_IQR) | df["unit_price_ping"].between(q1 - k * iqr, q3 + k * iqr)
    return df[keep].reset_index(drop=True)


def build_stats(df: pd.DataFrame) -> pd.DataFrame:
    return (df.groupby(["city", "district", "building_type"])["unit_price_ping"]
              .agg(n="size", mean_price="mean", median_price="median",
                   p25=lambda s: s.quantile(0.25), p75=lambda s: s.quantile(0.75))
              .reset_index())


# ---------------------------------------------------------------- 寫入
def save_to_sqlite(df: pd.DataFrame, stats: pd.DataFrame) -> None:
    cfg.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(cfg.DB_PATH) as conn:
        df.to_sql("transactions", conn, if_exists="replace", index=False)
        stats.to_sql("district_stats", conn, if_exists="replace", index=False)
        pd.DataFrame({"key": ["updated_at", "rows"],
                      "value": [datetime.now().strftime("%Y-%m-%d %H:%M"), str(len(df))]}
                     ).to_sql("meta", conn, if_exists="replace", index=False)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_tx ON transactions(city, district, building_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_st ON district_stats(city, district)")


def run_etl(download: bool = False,
            progress: Optional[Callable[[float, str], None]] = None,
            raw_dir: Optional[Path] = None,
            lookback_days: int = cfg.LOOKBACK_DAYS) -> dict:
    """完整 ETL。回傳摘要 dict；完全沒有資料時拋出 RuntimeError。"""
    raw_dir = Path(raw_dir or cfg.RAW_DIR)
    failed: list[str] = []
    if download:
        seasons = recent_seasons()
        total, done = len(seasons) * len(cfg.CITY_CODES), 0
        for season in seasons:
            for city, code in cfg.CITY_CODES.items():
                if download_season(season, code, raw_dir=raw_dir) is None:
                    failed.append(f"{season} {city}")
                done += 1
                if progress:
                    progress(done / total, f"下載 {season} {city}")
    if progress:
        progress(1.0, "讀取並清洗資料…")
    raw = load_raw_files(raw_dir)
    if raw.empty:
        raise RuntimeError(f"在 {raw_dir} 找不到任何買賣 CSV/ZIP（檔名需以 {{代碼}}_lvr_land_a.csv 結尾）。")
    cleaned = clean(raw, lookback_days=lookback_days)
    if cleaned.empty:
        raise RuntimeError(f"清洗後無有效資料。若你的資料不是近 {lookback_days} 天內的交易，請加大「取樣天數」。")
    cleaned = trim_outliers(cleaned)
    save_to_sqlite(cleaned, build_stats(cleaned))
    return {"rows": len(cleaned), "raw_rows": len(raw), "failed": failed,
            "cities": cleaned["city"].nunique(), "districts": int(cleaned.groupby("city")["district"].nunique().sum())}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true", help="先從內政部下載近 5 季資料")
    ap.add_argument("--raw-dir", default=None)
    ap.add_argument("--lookback-days", type=int, default=cfg.LOOKBACK_DAYS)
    a = ap.parse_args()
    print(run_etl(download=a.download, raw_dir=a.raw_dir, lookback_days=a.lookback_days,
                  progress=lambda p, m: print(f"[{p:4.0%}] {m}")))
