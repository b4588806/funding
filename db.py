"""SQLite 查詢層：供前端讀取行情，所有函式在無資料時回傳空結果而非拋錯。"""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pandas as pd

import config as cfg


def _query(sql: str, params: tuple = ()) -> pd.DataFrame:
    with closing(sqlite3.connect(cfg.DB_PATH)) as conn:
        return pd.read_sql_query(sql, conn, params=params)


def db_ready() -> bool:
    if not cfg.DB_PATH.exists():
        return False
    try:
        return not _query("SELECT 1 FROM district_stats LIMIT 1").empty
    except Exception:
        return False


def last_updated() -> str:
    try:
        df = _query("SELECT value FROM meta WHERE key='updated_at'")
        return df.iloc[0, 0] if not df.empty else "未知"
    except Exception:
        return "未知"


def list_cities() -> list[str]:
    df = _query("SELECT DISTINCT city FROM district_stats")
    order = {c: i for i, c in enumerate(cfg.CITY_CODES)}
    return sorted(df["city"].tolist(), key=lambda c: order.get(c, 99))


def list_districts(city: str) -> list[str]:
    return _query("SELECT DISTINCT district FROM district_stats WHERE city=? ORDER BY district",
                  (city,))["district"].tolist()


def district_stats(city: str, district: str) -> pd.DataFrame:
    """回傳欄位：building_type, n, mean_price, median_price, p25, p75（單位：元/坪）。"""
    return _query("SELECT building_type, n, mean_price, median_price, p25, p75 "
                  "FROM district_stats WHERE city=? AND district=?", (city, district))


def city_stats(city: str) -> pd.DataFrame:
    """縣市層級行情（行政區樣本不足或查無時的備援）。欄位同 district_stats。"""
    tx = _query("SELECT building_type, unit_price_ping FROM transactions WHERE city=?", (city,))
    if tx.empty:
        return tx
    g = tx.groupby("building_type")["unit_price_ping"]
    return g.agg(n="size", mean_price="mean", median_price="median",
                 p25=lambda x: x.quantile(0.25), p75=lambda x: x.quantile(0.75)).reset_index()
