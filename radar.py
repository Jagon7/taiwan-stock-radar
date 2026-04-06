#!/usr/bin/env python3
"""台股早鳥雷達 — Gemini + Google Search 版"""
import os, re, time, datetime
import requests
from google import genai
from google.genai import types

# ── 環境變數 ────────────────────────────────────────────────
GEMINI_KEY  = os.environ["GEMINI_API_KEY"]
TG_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT     = os.environ["TELEGRAM_CHAT_ID"]
NOTION_TOK  = os.environ["NOTION_TOKEN"]
NOTION_PID  = os.environ["NOTION_PAGE_ID"]

today_str = datetime.date.today().strftime("%Y-%m-%d")

# ── Prompt ───────────────────────────────────────────────────
PROMPT = f"""今天是 {today_str}（台灣時間早上07:00）。你是台股早鳥雷達，請用 Google Search 搜尋最新資訊，找出所有可能影響今日台股開盤的信號，特別是任何類股即將噴發的跡象。

## 掃描框架（四層信號由遠至近）

- 第一層（領先6–9個月）：美股設備/材料股大漲、美股法說重要聲明
- 第二層（領先3–6個月）：日股/韓股一線廠商財測上修、產能漲價公告
- 第三層（領先2–3個月）：TrendForce/研究機構缺貨報告、外資升評目標價大幅上調
- 第四層（領先0–1個月）：台廠月營收爆炸成長、廠商啟動漲價協商、投信連續4日以上異常買超同族群

## 掃描範圍一：歐美盤隔夜動向（最優先）

搜尋昨晚（{today_str} 前一天）的以下資訊：
- 大盤：S&P 500、Nasdaq、費城半導體指數（SOX）漲跌幅與主因
- 台股供應鏈相關美股：NVDA、AMD、AVGO、AMAT、LRCX、KLAC、ASML、MU、LITE（Lumentum）、COHR（Coherent）、VRT（Vertiv）、ETN（Eaton）、ASTS（AST SpaceMobile）
- 雲端巨頭資本支出動向：META、MSFT、GOOGL、AMZN
- 法說/財報：前一晚有無重要法說，特別是提到台灣供應鏈、AI需求、缺貨的發言
- 歐股：ASML 訂單消息、諾基亞/愛立信衛星消息
- 總經：美債殖利率、美元指數異動、重要數據（CPI、PMI、Fed發言）

## 掃描範圍二：台股產業信號（所有類股，不放過任何題材）

搜尋今天台灣市場的最新消息：
- CPO光通訊/矽光子：上詮(3363)、光聖(6442)、聯亞(3081)、全新(2455)、旺矽(6233)、華星光(4979)
- 低軌衛星：昇達科技(3491)、中磊(5388)、啟碁(6285)、致新(8081)、同欣電(6271)
- AI散熱/液冷：奇鋐(3017)、雙鴻(3324)、健策(3653)
- ABF載板：南電(8046)、景碩(3189)、欣興(3037)
- 記憶體：南亞科(2408)、華邦電(2344)
- BBU/MLCC電源：禾伸堂(3026)、順達(3211)、新盛力(4931)、國巨(2327)
- 先進封裝CoWoS：日月光(3711)、京元電(2449)、力成(6239)
- 重電/電網：華城(1519)、中興電(1513)、士林電機(1503)
- 伺服器ODM：緯穎(6669)、廣達(2382)、英業達(2356)
- 半導體設備材料：家登(3680)、辛耘(3583)
- 軍工/國防：漢翔(2634)及中科院相關廠商
- 投信法人：任何族群連續4日以上異常買超
- 其他新興題材：主動搜尋有沒有不在上述清單的新族群異動

## 重要判斷原則

- 「美股已先動」是最強信號，優先報告
- 投信連續4日以上異常買超是最可操作的本地信號
- 外資升評報告是加速器，不是起漲點
- 寧可誤報勿漏報，第一、二層信號尤其重要

## 輸出格式

請用 Telegram HTML 格式輸出（只用 <b> <i> <code> 標籤），格式如下：

📡 <b>台股早鳥雷達｜{today_str}</b>

🌍 <b>歐美盤隔夜重點</b>
SOX [漲跌]% ｜ NVDA [漲跌]% ｜ AMAT [漲跌]%
[其他重要美股]
[法說/財報重點聲明，若有]

🔴 <b>高機率噴發信號</b>
• [信號內容，含對應台股代碼]
（若無則寫：今日無高機率信號）

🟡 <b>中機率觀察中</b>
• [信號內容]
（若無則寫：今日無）

🟢 <b>低機率但留意</b>
• [信號內容]
（若無則寫：今日無）

📌 <b>今日操作重點</b>
[2–3句核心結論，直接說今天要注意什麼]

⏰ 下次掃描：明天 07:00
"""

# ── Telegram ─────────────────────────────────────────────────
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # 移除不支援的 HTML 標籤
    safe = re.sub(r"<(?!/?(?:b|i|code|pre|a)(?:\s[^>]*)?>)[^>]+>", "", text)
    chunks = [safe[i:i+4000] for i in range(0, len(safe), 4000)]
    for chunk in chunks:
        r = requests.post(url, data={
            "chat_id": TG_CHAT,
            "parse_mode": "HTML",
            "text": chunk,
        }, timeout=10)
        print(f"Telegram {r.status_code}: {r.json().get('ok')}")
        time.sleep(0.5)

# ── Notion ───────────────────────────────────────────────────
def write_notion(report: str):
    headers = {
        "Authorization": f"Bearer {NOTION_TOK}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    # 建立子頁面
    res = requests.post("https://api.notion.com/v1/pages", headers=headers, json={
        "parent": {"page_id": NOTION_PID},
        "icon": {"type": "emoji", "emoji": "📡"},
        "properties": {
            "title": {"title": [{"type": "text", "text": {"content": f"早鳥雷達｜{today_str}"}}]}
        },
    }, timeout=10)
    page = res.json()
    page_id = page.get("id")
    if not page_id:
        print(f"Notion 建頁失敗: {page}")
        return

    # 清除 HTML 標籤，轉成段落 blocks
    clean = re.sub(r"<[^>]+>", "", report)
    lines = [l.strip() for l in clean.split("\n") if l.strip()]
    blocks = [{
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": line[:2000]}}]}
    } for line in lines]

    for i in range(0, len(blocks), 100):
        r = requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=headers,
            json={"children": blocks[i:i+100]},
            timeout=10,
        )
        print(f"Notion blocks {r.status_code}")

# ── Main ─────────────────────────────────────────────────────
def main():
    print(f"🚀 台股早鳥雷達啟動 {today_str}")

    # Gemini + Google Search
    client = genai.Client(api_key=GEMINI_KEY)
    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=PROMPT,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2,
            ),
        )
        report = response.text
        print(f"Gemini 回應長度：{len(report)} 字元")
    except Exception as e:
        report = (
            f"📡 <b>台股早鳥雷達｜{today_str}</b>\n\n"
            f"❌ Gemini 搜尋失敗：{e}\n\n"
            f"⏰ 下次掃描：明天 07:00"
        )
        print(f"Gemini 錯誤：{e}")

    # 輸出
    send_telegram(report)
    write_notion(report)
    print("✅ 完成")

if __name__ == "__main__":
    main()
