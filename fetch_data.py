#!/usr/bin/env python3
"""
台股資料抓取器 — Jagon Space Station 資料層
每日收盤後執行，輸出 JSON 至 Next.js data/ 目錄

資料來源：
  - TWSE OpenAPI  (上市)
  - TPEx OpenAPI  (上櫃)
  - TWSE 處置名單
  - TWSE 大盤統計
"""
import os, sys, json, re, time, datetime
from pathlib import Path
import requests

# ── 設定 ─────────────────────────────────────────────────────
JSS_DATA_DIR = Path(
    os.environ.get("JSS_DATA_DIR",
                   Path.home() / "Desktop" / "jagon-space-station" / "data")
)
JSS_DATA_DIR.mkdir(parents=True, exist_ok=True)

TODAY    = datetime.date.today()
TODAY_AD = TODAY.strftime("%Y-%m-%d")
TODAY_RC = str(TODAY.year - 1911)                # e.g. "115"
TODAY_YMD = f"{TODAY_RC}{TODAY.month:02d}{TODAY.day:02d}"  # e.g. "1150509"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (compatible; JSS-fetcher/1.0)"})

LIMIT_UP_MIN = 9.0    # 最低漲幅門檻（考慮 tick 捨入）
LIMIT_UP_MAX = 11.0   # 超過此值視為新掛牌/復牌異常，排除

# ── 產業別 → 族群名稱 ─────────────────────────────────────────
# 來源：TWSE 上市公司產業別代碼（t187ap03_L）
INDUSTRY_MAP = {
    "01": "水泥",      "02": "食品",      "03": "塑膠",
    "04": "紡織纖維",  "05": "電機機械",  "06": "電器電纜",
    "08": "玻璃陶瓷",  "09": "造紙",      "10": "鋼鐵",
    "11": "橡膠",      "12": "汽車",      "14": "建材營造",
    "15": "航運",      "16": "觀光餐旅",  "17": "金融保險",
    "18": "貿易百貨",  "20": "其他",      "21": "化工",
    "22": "生技醫療",  "23": "油電燃氣",  "24": "半導體",
    "25": "電腦週邊",  "26": "光電",      "27": "通信網路",
    "28": "電子零組件","29": "電子通路",  "30": "資訊服務",
    "31": "其他電子",  "35": "綠能環保",  "36": "數位雲端",
    "37": "運動休閒",  "38": "居家生活",  "91": "存託憑證",
}

def log(msg: str):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

def save(filename: str, data: dict):
    path = JSS_DATA_DIR / filename
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✓ {filename} 已寫入（{path}）")

def fetch(url: str, timeout: int = 20) -> dict | list | None:
    try:
        r = SESSION.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"✗ fetch 失敗 {url[:60]}... → {e}")
        return None

def fetch_post(url: str, data: dict = None, timeout: int = 20) -> dict | None:
    try:
        r = SESSION.post(url, data=data or {}, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"✗ fetch_post 失敗 {url[:60]}... → {e}")
        return None

# ── 工具函式 ────────────────────────────────────────────────
def parse_price(s: str) -> float:
    try:
        return float(str(s).replace(",", "").strip())
    except:
        return 0.0

def calc_change_pct(close: float, change: float) -> float:
    if close <= 0 or change == 0:
        return 0.0
    prev = close - change
    if prev <= 0:
        return 0.0
    return round(change / prev * 100, 2)

def roc_to_ad(roc_date: str) -> str:
    """'1150508' → '2026-05-08'"""
    s = str(roc_date).strip()
    if len(s) == 7:
        year = int(s[:3]) + 1911
        return f"{year}-{s[3:5]}-{s[5:7]}"
    return TODAY_AD

# ── 三大法人買賣超 ────────────────────────────────────────────
def _parse_insti_num(s) -> int:
    """解析千分位整數，轉換成張（股數 ÷ 1000）"""
    try:
        return int(str(s).replace(",", "").replace("+", "").strip()) // 1000
    except:
        return 0

def fetch_institutional() -> dict[str, dict]:
    """
    回傳 {股號: {foreign, trust, dealer, total}} 單位：張
    先嘗試當日，無資料時退回前一個交易日（最多往前 5 日）
    """
    log("抓取三大法人買賣超...")

    def _recent_date_str(offset: int) -> str:
        d = TODAY - datetime.timedelta(days=offset)
        return d.strftime("%Y%m%d")

    result: dict[str, dict] = {}

    # ── 上市 TWSE T86 ──
    for days_back in range(0, 6):
        date_str = _recent_date_str(days_back)
        data = fetch(f"https://www.twse.com.tw/fund/T86?response=json&date={date_str}&selectType=ALLBUT0999")
        if data and data.get("stat") == "OK" and data.get("data"):
            for row in data["data"]:
                try:
                    code = str(row[0]).strip()
                    if not _is_stock_code(code):
                        continue
                    result[code] = {
                        "foreign": _parse_insti_num(row[4]),
                        "trust":   _parse_insti_num(row[10]),
                        "dealer":  _parse_insti_num(row[11]),
                        "total":   _parse_insti_num(row[18]) if len(row) > 18 else 0,
                    }
                except (IndexError, ValueError):
                    continue
            log(f"  上市三大法人：{date_str}，{len(result)} 檔")
            break

    # ── 上櫃 TPEx insti/dailyTrade ──
    roc_today = f"{TODAY_RC}/{TODAY.month:02d}/{TODAY.day:02d}"
    tpex_count = 0
    for days_back in range(0, 6):
        d = TODAY - datetime.timedelta(days=days_back)
        roc_date = f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"
        data = fetch_post("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
                          {"type": "Daily", "date": roc_date, "sect": "EW"})
        rows = (data or {}).get("tables", [{}])[0].get("data", [])
        if rows:
            for row in rows:
                code = str(row[0]).strip()
                if not _is_stock_code(code):
                    continue
                # 外資合計[10]、自營自行[13]+避險[16]、投信[19]、三大[23]
                result[code] = {
                    "foreign": _parse_insti_num(row[10]),
                    "trust":   _parse_insti_num(row[19]),
                    "dealer":  _parse_insti_num(row[13]) + _parse_insti_num(row[16]),
                    "total":   _parse_insti_num(row[23]),
                }
                tpex_count += 1
            log(f"  上櫃三大法人：{roc_date}，{tpex_count} 檔")
            break

    return result

# ── 建立股號 → 產業別 對照表 ────────────────────────────────
def build_sector_map() -> dict[str, str]:
    log("載入公司產業別資料...")
    result = {}

    # 上市公司
    data = fetch("https://openapi.twse.com.tw/v1/opendata/t187ap03_L")
    if data:
        for row in data:
            code = row.get("公司代號", "").strip()
            ind  = row.get("產業別", "").strip()
            if code:
                result[code] = INDUSTRY_MAP.get(ind, "其他")

    # 上櫃公司（TPEx）
    data = fetch("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_listed_companies")
    if data:
        for row in data:
            code = row.get("SecuritiesCompanyCode", "").strip()
            ind  = row.get("IndustryCode", "").strip()
            if code:
                result[code] = INDUSTRY_MAP.get(ind, "其他")

    log(f"  產業別對照表：{len(result)} 筆")
    return result

# ── 漲停股 ─────────────────────────────────────────────────
def fetch_limit_up(sector_map: dict, insti_map: dict = None) -> list[dict]:
    log("抓取漲停股清單...")
    stocks = []

    # ── 上市 (TWSE) ──
    twse = fetch("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    if twse:
        for row in twse:
            code  = row.get("Code", "").strip()
            close = parse_price(row.get("ClosingPrice", 0))
            chg   = parse_price(row.get("Change", 0))
            vol   = int(parse_price(row.get("TradeVolume", 0)) / 1000)  # 張
            tv    = parse_price(row.get("TradeValue", 0))               # 成交金額（元）
            pct   = calc_change_pct(close, chg)
            if not (LIMIT_UP_MIN <= pct <= LIMIT_UP_MAX):
                continue
            if not code.isdigit():
                continue
            insti = (insti_map or {}).get(code, {})
            stocks.append({
                "code":       code,
                "name":       row.get("Name", "").strip(),
                "sector":     sector_map.get(code, "其他"),
                "change":     pct,
                "volume":     vol,
                "tradeValue": tv,
                "closePrice": close,
                "openPrice":  parse_price(row.get("OpeningPrice", close)),
                "limitTime":  "--:--",
                "market":     "上市",
                "instiForeign": insti.get("foreign", None),
                "instiTrust":   insti.get("trust",   None),
                "instiDealer":  insti.get("dealer",  None),
                "instiTotal":   insti.get("total",   None),
                "reason":     "",
            })

    # ── 上櫃 (TPEx) ──
    tpex = fetch("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes")
    if tpex:
        for row in tpex:
            code  = row.get("SecuritiesCompanyCode", "").strip()
            close = parse_price(row.get("Close", 0))
            chg   = parse_price(row.get("Change", 0))
            vol   = int(parse_price(row.get("TradingShares", 0)) / 1000)
            tv    = parse_price(row.get("TransactionAmount", 0))
            pct   = calc_change_pct(close, chg)
            if not (LIMIT_UP_MIN <= pct <= LIMIT_UP_MAX):
                continue
            if not code.isdigit():
                continue
            insti = (insti_map or {}).get(code, {})
            stocks.append({
                "code":       code,
                "name":       row.get("CompanyName", "").strip(),
                "sector":     sector_map.get(code, "其他"),
                "change":     pct,
                "volume":     vol,
                "tradeValue": tv,
                "closePrice": close,
                "openPrice":  parse_price(row.get("Open", close)),
                "limitTime":  "--:--",
                "market":     "上櫃",
                "instiForeign": insti.get("foreign", None),
                "instiTrust":   insti.get("trust",   None),
                "instiDealer":  insti.get("dealer",  None),
                "instiTotal":   insti.get("total",   None),
                "reason":     "",
            })

    # 依當日成交金額由大到小排序
    stocks.sort(key=lambda s: -s["tradeValue"])
    log(f"  漲停股：{len(stocks)} 檔（上市+上櫃）")
    return stocks

# ── 族群統計 ────────────────────────────────────────────────
def summarize_sectors(stocks: list[dict]) -> list[dict]:
    from collections import Counter
    cnt = Counter(s["sector"] for s in stocks if s["sector"] != "其他")
    result = []
    for sector, count in cnt.most_common(10):
        momentum = "strong" if count >= 6 else "normal" if count >= 3 else "weak"
        result.append({"name": sector, "count": count, "momentum": momentum})
    return result

# ── 大盤統計 ────────────────────────────────────────────────
def fetch_market_summary(limit_up_count: int, sector_count: int) -> dict:
    log("抓取大盤統計...")
    limit_up_total = limit_up_count
    taiex_change   = 0.0
    mood           = "中性"

    # 氣氛判斷用純股票漲停家數（與清單一致）
    if limit_up_total >= 60:
        mood = "強勢"
    elif limit_up_total >= 40:
        mood = "偏強"
    elif limit_up_total >= 20:
        mood = "中性"
    elif limit_up_total >= 10:
        mood = "偏弱"
    else:
        mood = "弱勢"

    return {
        "date":             TODAY_AD,
        "limitUpCount":     limit_up_total,
        "sectorCount":      sector_count,
        "dispositionCount": 0,   # 由處置模組填入
        "announcementCount": 0,
        "marketMood":       mood,
        "taiexChange":      taiex_change,
    }

def _is_stock_code(code: str) -> bool:
    """只保留 4 位純數字（一般股票），排除 ETF / 權證 / 可轉債等"""
    return code.isdigit() and len(code) == 4

def _parse_roc_period_end(period: str) -> str:
    """'115/05/07～115/05/20' 或 '115/05/07~115/05/20' → '2026-05-20'"""
    # 支援全形 ～ 與半形 ~
    end_roc = period.replace("～", "~").split("~")[-1].strip()
    try:
        parts = end_roc.split("/")
        return f"{int(parts[0]) + 1911}-{parts[1]}-{parts[2]}"
    except:
        return "9999-12-31"

def _strip_html(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", " ", text).strip()

# ── 注意股：上市 (TWSE) ──────────────────────────────────────
def _fetch_twse_notice() -> list[dict]:
    data = fetch("https://www.twse.com.tw/announcement/notice?response=json")
    if not data or data.get("stat") != "OK":
        return []
    stocks = []
    for row in data.get("data", []):
        try:
            code  = str(row[1]).strip()
            if not _is_stock_code(code):
                continue
            stocks.append({
                "code":        code,
                "name":        str(row[2]).strip(),
                "noticeCount": int(row[3]) if row[3] else 1,
                "info":        str(row[4]).strip(),
                "date":        str(row[5]).strip(),
                "closePrice":  parse_price(row[6]) if len(row) > 6 else 0,
                "market":      "上市",
            })
        except:
            continue
    return stocks

# ── 注意股：上櫃 (TPEx) ──────────────────────────────────────
def _fetch_tpex_notice() -> list[dict]:
    # fields: ['編號','證券代號','證券名稱','累計','注意交易資訊','公告日期','收盤價','本益比','link']
    data = fetch_post("https://www.tpex.org.tw/www/zh-tw/bulletin/attention")
    if not data:
        return []
    rows = data.get("tables", [{}])[0].get("data", [])
    stocks = []
    for row in rows:
        try:
            code = str(row[1]).strip()
            if not _is_stock_code(code):
                continue
            stocks.append({
                "code":        code,
                "name":        str(row[2]).strip(),
                "noticeCount": int(row[3]) if row[3] else 1,
                "info":        _strip_html(str(row[4])),
                "date":        str(row[5]).strip(),
                "closePrice":  parse_price(row[6]) if len(row) > 6 else 0,
                "market":      "上櫃",
            })
        except:
            continue
    return stocks

def fetch_notice_stocks() -> list[dict]:
    log("抓取注意股名單（上市 + 上櫃）...")
    twse = _fetch_twse_notice()
    tpex = _fetch_tpex_notice()
    stocks = twse + tpex
    log(f"  注意股：{len(stocks)} 檔（上市 {len(twse)} + 上櫃 {len(tpex)}）")
    return stocks

# ── 處置股：上市 (TWSE) ──────────────────────────────────────
def _fetch_twse_disposition() -> list[dict]:
    data = fetch("https://www.twse.com.tw/announcement/punish?response=json")
    if not data or data.get("stat") != "OK":
        return []
    stocks = []
    for row in data.get("data", []):
        try:
            code = str(row[2]).strip()
            if not _is_stock_code(code):
                continue
            period = str(row[6]).strip()
            stocks.append({
                "code":         code,
                "name":         str(row[3]).strip(),
                "count":        int(row[4]) if row[4] else 1,
                "condition":    str(row[5]).strip(),
                "period":       period,
                "endDate":      _parse_roc_period_end(period),
                "announceDate": str(row[1]).strip(),
                "market":       "上市",
            })
        except:
            continue
    return stocks

# ── 處置股：上櫃 (TPEx) ──────────────────────────────────────
def _fetch_tpex_disposition() -> list[dict]:
    import re
    # fields: ['編號','公布日期','證券代號','證券名稱','累計','處置起訖時間','處置原因','處置內容',...]
    data = fetch_post("https://www.tpex.org.tw/www/zh-tw/bulletin/disposal")
    if not data:
        return []
    rows = data.get("tables", [{}])[0].get("data", [])
    stocks = []
    for row in rows:
        try:
            code = str(row[2]).strip()
            if not _is_stock_code(code):
                continue
            # name 欄位可能含連結，如 '川寶(../../...)' → 只取前段
            raw_name = str(row[3])
            name = re.sub(r"\(.*?\)", "", raw_name).strip()
            period = str(row[5]).strip()
            stocks.append({
                "code":         code,
                "name":         name,
                "count":        int(row[4]) if row[4] else 1,
                "condition":    _strip_html(str(row[6])),
                "period":       period,
                "endDate":      _parse_roc_period_end(period),
                "announceDate": str(row[1]).strip(),
                "market":       "上櫃",
            })
        except:
            continue
    return stocks

def fetch_disposition_stocks() -> list[dict]:
    log("抓取處置股名單（上市 + 上櫃）...")
    twse = _fetch_twse_disposition()
    tpex = _fetch_tpex_disposition()
    stocks = twse + tpex

    # 同一股票可能有多筆（舊期間未到期 + 新期間），只保留 endDate 最晚的那筆
    latest: dict[str, dict] = {}
    for s in stocks:
        key = s["code"]
        if key not in latest or s["endDate"] > latest[key]["endDate"]:
            latest[key] = s
    stocks = list(latest.values())

    # 處置快結束的排前面
    stocks.sort(key=lambda s: s["endDate"])
    log(f"  處置股：{len(stocks)} 檔（上市 {len(twse)} + 上櫃 {len(tpex)}）")
    return stocks

# ── 漲停原因：Google News + Claude ────────────────────────────
def _google_news_headlines(code: str, name: str) -> list[str]:
    import xml.etree.ElementTree as ET, urllib.parse
    q = urllib.parse.quote(f"{name} 漲停")
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    try:
        r = SESSION.get(url, timeout=8)
        root = ET.fromstring(r.content)
        return [item.findtext("title", "") for item in root.findall(".//item")][:4]
    except:
        return []

def fetch_limit_up_reasons(stocks: list[dict]) -> dict[str, str]:
    """
    每檔漲停股：Google News 抓標題 → Claude Haiku 歸納一句原因
    需要 ANTHROPIC_API_KEY 環境變數，缺少時回傳空字典
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        log("⚠ ANTHROPIC_API_KEY 未設定，漲停原因略過")
        return {}

    try:
        import anthropic
    except ImportError:
        log("⚠ anthropic 套件未安裝，執行 pip install anthropic")
        return {}

    log(f"抓取漲停原因（Google News × {len(stocks)} 檔 → Claude）...")

    # 1. 並行抓各股新聞
    news_map: dict[str, list[str]] = {}
    for s in stocks:
        news_map[s["code"]] = _google_news_headlines(s["code"], s["name"])

    # 2. 組成 prompt
    lines = [
        f"今日（{TODAY_AD}）台股漲停。根據以下各股新聞標題，",
        "為每支股票寫一句漲停主因（繁體中文，12字以內，不要冠股名）：\n",
    ]
    for s in stocks:
        lines.append(f"【{s['code']} {s['name']}】")
        news = news_map.get(s["code"], [])
        if news:
            for h in news[:3]:
                lines.append(f"  · {h}")
        else:
            lines.append("  · （今日無相關新聞）")
    lines.append(
        f"\n以 JSON 格式回覆，鍵為股號字串，值為原因字串：\n"
        '{"3034":"原因","8210":"原因",...}'
    )

    # 3. 呼叫 Claude Haiku
    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            system="你只輸出純 JSON，不加任何 markdown、說明或換行以外的文字。",
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        text = resp.content[0].text.strip()
        log(f"  Claude 回應（前 200 字）：{text[:200]}")

        # 依序嘗試解析，容錯 markdown fence 或前後文字
        reasons: dict[str, str] = {}
        candidates = [
            text,
            re.sub(r"```(?:json)?\s*|\s*```", "", text).strip(),
        ]
        m = re.search(r"\{[\s\S]+\}", text)
        if m:
            candidates.append(m.group())

        for candidate in candidates:
            try:
                reasons = json.loads(candidate)
                break
            except Exception:
                continue

        if not reasons:
            log("✗ Claude 回傳內容無法解析為 JSON，漲停原因略過")
        return reasons

    except Exception as e:
        log(f"✗ Claude 呼叫失敗：{e}")

    return {}

# ── CB 詢圈：輔助函式 ─────────────────────────────────────────
def _clean_legal_name(name: str) -> str:
    for suffix in ["股份有限公司", "有限公司", "（創新板）", "(創新板)", "－KY", "-KY"]:
        name = name.replace(suffix, "")
    return name.strip()

def _match_code(legal_name: str, name_map: dict) -> tuple | None:
    """法人全稱 → (股號, 簡稱)。最長子字串優先；唯一前兩字首碼為備援。"""
    clean = _clean_legal_name(legal_name)
    best = max(
        ((c, n) for c, n in name_map.items() if len(n) >= 2 and n in clean),
        key=lambda x: len(x[1]),
        default=None,
    )
    if best:
        return best
    prefix = clean[:2]
    cands = [(c, n) for c, n in name_map.items() if n.startswith(prefix)]
    return cands[0] if len(cands) == 1 else None

def _build_code_meta() -> dict:
    """回傳 {股號: {name, price, market}}，涵蓋上市 + 上櫃所有股票。"""
    meta: dict[str, dict] = {}
    twse = fetch("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    if twse:
        for r in twse:
            c = r.get("Code", "").strip()
            if _is_stock_code(c):
                meta[c] = {
                    "name":   r.get("Name", "").strip(),
                    "price":  parse_price(r.get("ClosingPrice", 0)),
                    "market": "上市",
                }
    tpex = fetch("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes")
    if tpex:
        for r in tpex:
            c = r.get("SecuritiesCompanyCode", "").strip()
            if _is_stock_code(c):
                meta[c] = {
                    "name":   r.get("CompanyName", "").strip(),
                    "price":  parse_price(r.get("Close", 0)),
                    "market": "上櫃",
                }
    return meta

def _parse_ad_slash(date_str: str) -> str:
    """'2026/01/07' → '2026-01-07'"""
    try:
        parts = date_str.strip().split("/")
        return f"{parts[0]}-{parts[1]}-{parts[2]}"
    except:
        return ""

# ── CB 詢圈：主抓取函式 ────────────────────────────────────────
def fetch_cb_issuances() -> list[dict]:
    log("抓取 CB 詢圈資料（TWSA 詢圈公告）...")
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log("⚠ 需要安裝 beautifulsoup4：pip install beautifulsoup4")
        return []

    base_url = "https://web.twsa.org.tw/edoc2/default.aspx"
    hdrs = {"Referer": base_url, "Accept": "text/html,application/xhtml+xml"}

    # Step 1: GET 取得 ViewState
    try:
        r1 = SESSION.get(base_url, headers=hdrs, timeout=20)
        r1.raise_for_status()
    except Exception as e:
        log(f"✗ TWSA GET 失敗：{e}")
        return []

    soup1 = BeautifulSoup(r1.content, "html.parser")

    def _hidden(name: str) -> str:
        tag = soup1.find("input", {"name": name, "type": "hidden"})
        return tag.get("value", "") if tag else ""

    post_data = {
        "__VIEWSTATE":           _hidden("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR":  _hidden("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION":     _hidden("__EVENTVALIDATION"),
        "__EVENTTARGET":         "ctl00$cphMain$rblReportType",
        "__EVENTARGUMENT":       "",
        "ctl00$cphMain$rblReportType": "BookBuilding",
        "ctl00$cphMain$ddlYear": str(TODAY.year),   # AD year e.g. 2026
    }

    # Step 2: POST 取得詢圈清單
    try:
        r2 = SESSION.post(base_url, data=post_data, headers=hdrs, timeout=20)
        r2.raise_for_status()
    except Exception as e:
        log(f"✗ TWSA POST 失敗：{e}")
        return []

    soup2 = BeautifulSoup(r2.content, "html.parser")

    # GridView 固定 ID
    table = soup2.find("table", id="ctl00_cphMain_gvResult")
    if not table:
        log("✗ TWSA：找不到 ctl00_cphMain_gvResult 表格")
        return []

    rows = table.find_all("tr")
    if len(rows) < 2:
        log("✗ TWSA：表格無資料列")
        return []

    # 欄位索引（依實際 header 動態對應）
    # 實際 headers: 序號 發行公司 主辦承銷商 發行性質 承銷股數(千股/張) 詢圈銷售股數(千股/張) 圈購期間 價格(元) 公告檔
    header_cells = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
    log(f"  TWSA 欄位：{header_cells}")

    def col(candidates: list, default: int) -> int:
        for name in candidates:
            for i, h in enumerate(header_cells):
                if name in h:
                    return i
        return default

    idx_serial = col(["序號"], 0)
    idx_name   = col(["發行公司", "公司名稱"], 1)
    idx_uw     = col(["承銷商"], 2)
    idx_type   = col(["發行性質", "有價證券種類", "種類"], 3)
    idx_shares = col(["承銷股數"], 4)
    idx_period = col(["圈購期間"], 6)

    meta = _build_code_meta()
    name_map = {c: v["name"] for c, v in meta.items()}
    cutoff = (TODAY - datetime.timedelta(days=90)).isoformat()

    results = []
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if len(cells) < 5:
            continue

        def cell(i: int) -> str:
            return cells[i].get_text(strip=True) if i < len(cells) else ""

        issue_type = cell(idx_type)
        if "轉換公司債" not in issue_type:
            continue

        legal_name  = cell(idx_name)
        underwriter = cell(idx_uw)
        period      = cell(idx_period)   # e.g. "2026/01/07~2026/01/09" (AD format)

        # 圈購起訖日
        bb_start = bb_end = ""
        if period:
            parts = period.replace("～", "~").split("~")
            bb_start = _parse_ad_slash(parts[0]) if len(parts) >= 1 else ""
            bb_end   = _parse_ad_slash(parts[1]) if len(parts) >= 2 else ""

        # 預估掛牌日：定價日（圈購結束後首個工作日）+ 7 天 → 順延至當週或下週五
        # 根據歷史觀察，台灣 CB 掛牌通常在定價後第一個可用週五
        listing_date = ""
        if bb_end:
            try:
                d = datetime.date.fromisoformat(bb_end)
                # 定價日 = 圈購結束後首個工作日
                pricing = d + datetime.timedelta(days=1)
                while pricing.weekday() >= 5:
                    pricing += datetime.timedelta(days=1)
                # 目標日 = 定價日 + 7 天，順延至最近的週五
                target = pricing + datetime.timedelta(days=7)
                days_to_fri = (4 - target.weekday()) % 7   # Friday = weekday 4
                listing_date = (target + datetime.timedelta(days=days_to_fri)).isoformat()
            except:
                pass

        # 超過 90 天前已掛牌者略過
        if listing_date and listing_date < cutoff:
            continue
        if not listing_date and bb_start and bb_start < cutoff:
            continue

        # 承銷股數 (張) → 億元（1張 = NT$100,000；1000張 = 1億）
        amount = None
        try:
            amount = round(float(cell(idx_shares).replace(",", "")) / 1000, 2)
        except:
            pass

        # CB 次別（由序號推算，格式 115001 → 序號年內第1次）
        serial = cell(idx_serial)
        cb_num = serial[3:] if len(serial) >= 4 else ""
        cb_series = f"第{int(cb_num)}次" if cb_num.isdigit() else ""

        # 比對股號
        match = _match_code(legal_name, name_map)
        code       = match[0] if match else ""
        short_name = match[1] if match else _clean_legal_name(legal_name)
        close_px   = meta.get(code, {}).get("price") if code else None
        market     = meta.get(code, {}).get("market", "") if code else ""

        results.append({
            "code":            code,
            "name":            short_name,
            "cbSeries":        cb_series,
            "filingDate":      bb_start,    # TWSA 無申報日，以圈購開始日代替
            "bookBuildDate":   bb_start,
            "listingDate":     listing_date,
            "amount":          amount,
            "conversionPrice": None,         # 定價後才確定，TWSA 不提供
            "closePrice":      close_px,
            "market":          market,
            "underwriter":     underwriter,
        })

    # 最近要掛牌的排最前面
    results.sort(key=lambda x: x["listingDate"] or "9999-12-31")
    log(f"  CB 詢圈：{len(results)} 筆")
    return results

# ── 公告（MOPS - 基礎版）─────────────────────────────────────
def fetch_announcements() -> list[dict]:
    log("公告資料：使用空清單（待接 MOPS API）")
    return []

# ── 主流程 ──────────────────────────────────────────────────
def main():
    log(f"=== JSS 資料抓取開始 {TODAY_AD} ===")
    log(f"輸出目錄：{JSS_DATA_DIR}")

    sector_map    = build_sector_map()
    insti_map     = fetch_institutional()
    limit_stocks  = fetch_limit_up(sector_map, insti_map)
    sectors       = summarize_sectors(limit_stocks)
    market        = fetch_market_summary(len(limit_stocks), len(sectors))
    notice        = fetch_notice_stocks()
    disposition   = fetch_disposition_stocks()
    cb_issuances  = fetch_cb_issuances()
    announcements = fetch_announcements()

    market["noticeCount"]      = len(notice)
    market["dispositionCount"] = len(disposition)

    # ── 寫檔 ──
    save("limit-up.json",      {"date": TODAY_AD, "data": limit_stocks})
    save("sectors.json",       {"date": TODAY_AD, "data": sectors})
    save("market-summary.json", market)
    save("notice.json",        {"date": TODAY_AD, "data": notice})
    save("disposition.json",   {"date": TODAY_AD, "data": disposition})
    save("cb-watch.json",      {"date": TODAY_AD, "data": cb_issuances})
    save("announcements.json", {"date": TODAY_AD, "data": announcements})
    save("last-updated.json",  {"updatedAt": datetime.datetime.now().isoformat(), "date": TODAY_AD})

    log(f"=== 完成 ===")
    log(f"  漲停：{len(limit_stocks)} 檔 | 族群：{len(sectors)} 個 | 注意：{len(notice)} 檔 | 處置：{len(disposition)} 檔 | CB詢圈：{len(cb_issuances)} 筆")

if __name__ == "__main__":
    main()
