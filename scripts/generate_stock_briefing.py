#!/usr/bin/env python3
"""generate_stock_briefing.py — 일일 국내 증시 브리핑 생성·전송.

흐름:
  1. KOSPI / KOSDAQ 1주 변동률 (FinanceDataReader)
  2. 시총 5,000억 이상 종목 중 1주 변동률 절대값 Top 10
  3. 향후 전망 — Google News RSS (증시 전망 / 코스피 전망)
  4. 텔레그램 HTML 메시지 발송 (scripts/send_telegram.py 호출)
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape
from pathlib import Path

import FinanceDataReader as fdr
import feedparser

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

KST = dt.timezone(dt.timedelta(hours=9))
TODAY = dt.datetime.now(KST).date()
LOOKBACK_CALENDAR_DAYS = 14
MIN_MARCAP = 500_000_000_000
TOP_N = 10
NEWS_LIMIT = 6
MAX_WORKERS = 12

NEWS_FEEDS = [
    (
        "증시 전망",
        "https://news.google.com/rss/search?q=%EC%A6%9D%EC%8B%9C+%EC%A0%84%EB%A7%9D&hl=ko&gl=KR&ceid=KR:ko",
    ),
    (
        "코스피 전망",
        "https://news.google.com/rss/search?q=%EC%BD%94%EC%8A%A4%ED%94%BC+%EC%A0%84%EB%A7%9D&hl=ko&gl=KR&ceid=KR:ko",
    ),
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fetch_index(name: str, code: str) -> dict | None:
    end = TODAY
    start = end - dt.timedelta(days=LOOKBACK_CALENDAR_DAYS)
    try:
        df = fdr.DataReader(code, start, end)
    except Exception as e:
        log(f"WARN: index {name} ({code}) fetch failed: {e}")
        return None
    if df.empty or len(df) < 2:
        log(f"WARN: index {name} insufficient rows ({len(df)})")
        return None
    closes = df["Close"]
    last = float(closes.iloc[-1])
    base_idx = -6 if len(closes) >= 6 else 0
    base = float(closes.iloc[base_idx])
    return {
        "name": name,
        "last": last,
        "base": base,
        "pct": (last / base - 1) * 100 if base else 0.0,
        "last_date": closes.index[-1].date(),
        "base_date": closes.index[base_idx].date(),
    }


def fetch_one_stock(code: str, name: str, marcap: float, start, end) -> dict | None:
    try:
        df = fdr.DataReader(code, start, end)
    except Exception:
        return None
    if df.empty or len(df) < 2:
        return None
    closes = df["Close"]
    last = float(closes.iloc[-1])
    base_idx = -6 if len(closes) >= 6 else 0
    base = float(closes.iloc[base_idx])
    if base <= 0:
        return None
    return {
        "code": code,
        "name": name,
        "marcap": marcap,
        "last": last,
        "base": base,
        "pct": (last / base - 1) * 100,
    }


def fetch_top_movers() -> list[dict]:
    listing = fdr.StockListing("KRX")
    listing = listing[listing["Marcap"].fillna(0) >= MIN_MARCAP].copy()
    log(f"INFO: 시총 {MIN_MARCAP/1e8:,.0f}억 이상 = {len(listing)} 종목 스캔")

    end = TODAY
    start = end - dt.timedelta(days=LOOKBACK_CALENDAR_DAYS)
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [
            pool.submit(
                fetch_one_stock,
                row["Code"], row["Name"], float(row["Marcap"]), start, end,
            )
            for _, row in listing.iterrows()
        ]
        for fut in as_completed(futures):
            r = fut.result()
            if r:
                results.append(r)

    log(f"INFO: 변동률 산출 성공 = {len(results)} 종목")
    results.sort(key=lambda x: abs(x["pct"]), reverse=True)
    return results[:TOP_N]


def fetch_news() -> list[dict]:
    out: list[dict] = []
    seen_titles: set[str] = set()
    for src, url in NEWS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            log(f"WARN: feed {src} failed: {e}")
            continue
        for entry in feed.entries[:5]:
            title = (entry.get("title") or "(제목 없음)").strip()
            if title in seen_titles:
                continue
            seen_titles.add(title)
            out.append({
                "src": src,
                "title": title,
                "link": entry.get("link", ""),
            })
            if len(out) >= NEWS_LIMIT:
                return out
    return out


def fmt_marcap(v: float) -> str:
    조 = v / 1e12
    if 조 >= 1:
        return f"{조:,.2f}조"
    억 = v / 1e8
    return f"{억:,.0f}억"


def fmt_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.2f}%"


def build_message(indices: list, movers: list[dict], news: list[dict]) -> str:
    lines = [f"<b>[국내 증시 브리핑] {TODAY.isoformat()} (KST)</b>", ""]

    lines.append("<b>1. 지수 1주 변동</b>")
    if any(indices):
        for i in indices:
            if not i:
                continue
            lines.append(
                f"- {escape(i['name'])}: {i['base']:,.2f} ({i['base_date']}) → "
                f"{i['last']:,.2f} ({i['last_date']}) <b>{fmt_pct(i['pct'])}</b>"
            )
    else:
        lines.append("- (지수 데이터 수신 실패)")
    lines.append("")

    lines.append(
        f"<b>2. 1주 변동률 Top {TOP_N} (시총 {fmt_marcap(MIN_MARCAP)} 이상)</b>"
    )
    if movers:
        for idx, m in enumerate(movers, 1):
            lines.append(
                f"{idx}. {escape(m['name'])} ({m['code']}) "
                f"<b>{fmt_pct(m['pct'])}</b> | {fmt_marcap(m['marcap'])}"
            )
    else:
        lines.append("- (변동률 산출 실패)")
    lines.append("")

    lines.append("<b>3. 향후 전망</b>")
    if news:
        for n in news:
            link = n["link"] or "#"
            lines.append(
                f'- <a href="{escape(link)}">{escape(n["title"])}</a> '
                f'<i>({escape(n["src"])})</i>'
            )
    else:
        lines.append("- (RSS 수신 실패)")

    return "\n".join(lines)


def send_via_telegram(message: str) -> None:
    out_file = LOG_DIR / f"stock_brief_{TODAY.isoformat()}.txt"
    out_file.write_text(message, encoding="utf-8")
    log(f"INFO: wrote {out_file} ({len(message)} chars)")

    sender = ROOT / "scripts" / "send_telegram.py"
    result = subprocess.run(
        [sys.executable, str(sender), "--file", str(out_file), "--parse-mode", "HTML"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.stdout:
        print(result.stdout.strip())
    if result.returncode != 0:
        log(f"ERROR: send_telegram.py failed:\n{result.stderr}")
        sys.exit(1)


def main() -> None:
    indices = [fetch_index("KOSPI", "KS11"), fetch_index("KOSDAQ", "KQ11")]
    movers = fetch_top_movers()
    news = fetch_news()
    msg = build_message(indices, movers, news)
    send_via_telegram(msg)


if __name__ == "__main__":
    main()
