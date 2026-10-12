"""全域設定：路徑、實價登錄來源、清洗參數。調整行為時優先改這裡。"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"          # 下載或手動放置的原始 CSV
DB_PATH = DATA_DIR / "realprice.db"  # SQLite 資料庫

# 1 坪 = 3.30578 平方公尺；每坪單價 = 每平方公尺單價 × 3.30578
SQM_TO_PING = 3.30578

# 全台 22 縣市對應內政部實價登錄檔名代碼（a_lvr_land_a.csv = 臺北市不動產買賣）
# 順序即前端下拉選單順序：六都在前，其餘縣市在後
CITY_CODES = {
    "臺北市": "a", "新北市": "f", "桃園市": "h", "臺中市": "b", "臺南市": "d", "高雄市": "e",
    "基隆市": "c", "新竹市": "o", "新竹縣": "j", "苗栗縣": "k", "彰化縣": "n", "南投縣": "m",
    "雲林縣": "p", "嘉義市": "i", "嘉義縣": "q", "屏東縣": "t", "宜蘭縣": "g", "花蓮縣": "u",
    "臺東縣": "v", "澎湖縣": "x", "金門縣": "w", "連江縣": "z",
}

# 季別批次下載網址（season 例：113S4）。若內政部改版導致失效，
# 可改為手動下載 CSV 放進 data/raw/，檔名須以「{代碼}_lvr_land_a.csv」結尾。
DOWNLOAD_URL = (
    "https://plvr.land.moi.gov.tw/DownloadSeason"
    "?season={season}&fileName={code}_lvr_land_a.csv"
)
SEASONS_TO_FETCH = 5      # 5 季可完整涵蓋近 12 個月
REFRESH_AFTER_DAYS = 7    # 已下載檔案超過幾天才重抓

# ---- 清洗規則 ----
LOOKBACK_DAYS = 365                # 預設只取近一年；前端可調整
# 建物型態關鍵字 -> 分類（未列入者如華廈、公寓、套房不納入統計）
BUILDING_TYPE_MAP = (("住宅大樓", "大樓"), ("透天厝", "透天"))
# 備註欄出現下列字樣視為特殊交易，予以排除
SPECIAL_REMARK_PATTERN = (
    "親友|員工|共有人|關係企業|特殊關係|急買急賣|債權債務|法拍|拍賣|瑕疵|毀損|"
    "凶宅|海砂|輻射|地上權|分割|股東|政府機關|受贈|贈與|信託|畸零地|協議價購|公共設施保留地"
)
MIN_UNIT_PRICE_PING = 10_000       # 元/坪，合理性下限（離島、偏鄉透天單價較低）
MAX_UNIT_PRICE_PING = 3_000_000    # 元/坪，合理性上限
MIN_AREA_PING = 5                  # 排除過小面積
IQR_MULTIPLIER = 1.5               # 同區同型態 IQR 離群值倍數
MIN_GROUP_FOR_IQR = 8              # 樣本數少於此值不做 IQR 剔除
MIN_SAMPLES_WARN = 10              # 樣本數少於此值時前端提示行情參考性低

# ---- 貸款與風控預設 ----
LTV = 0.80
DBR_MULTIPLE = 22
DSR_LIMIT = 0.70
DEFAULT_MORTGAGE = {"rate": 2.2, "years": 30}
DEFAULT_CREDIT = {"rate": 2.5, "years": 7}

# ---- 投資端 ----
NHI_RATE = 0.0211          # 二代健保補充保費費率
NHI_THRESHOLD = 20_000     # 單次給付達此金額才扣
