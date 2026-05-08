#!/usr/bin/env python3
"""generate_stock_briefing.py — 일일 국내 증시 브리핑 생성·전송.

흐름:
  1. KOSPI / KOSDAQ 1주 변동률 (FinanceDataReader)
  2. 시총 5,000억 이상 종목 1주 변동률 절대값 Top 10
  3. 같은 모집단 1개월 변동률 절대값 Top 10
  4. 향후 전망 — Google News RSS 3섹션 (증시 / 반도체 / 방산)
  5. 텔레그램 HTML 메시지 발송 (scripts/send_telegram.py 호출)

종목 fetch는 한 번 (영업일 ~30일치)으로 1주/1개월 두 변동률을 동시에 계산한다.
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
import urllib.parse
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
LOOKBACK_CALENDAR_DAYS = 45  # 영업일 ~30개 + 안전 마진 (1개월 변동률 계산용)
INDEX_LOOKBACK_DAYS = 14
MIN_MARCAP = 500_000_000_000
TOP_N = 10
NEWS_PER_TOPIC = 3
MAX_WORKERS = 12

# 영업일 인덱스 (오늘이 -1, 1주 ≈ 5영업일 전 = -6, 1개월 ≈ 21영업일 전 = -22)
BIZDAYS_1W = 6
BIZDAYS_1M = 22

NEWS_TOPICS: list[tuple[str, list[str]]] = [
    ("증시 전망", ["증시 전망", "코스피 전망"]),
    ("반도체 전망", ["반도체 전망", "메모리 반도체 업황"]),
    ("방산 전망", ["방산 전망", "K방산 수출"]),
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def fetch_index(name: str, code: str) -> dict | None:
    end = TODAY
    start = end - dt.timedelta(days=INDEX_LOOKBACK_DAYS)
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
    base_idx = -BIZDAYS_1W if len(closes) >= BIZDAYS_1W else 0
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
    if last <= 0:
        return None

    def pct_for(bizdays: int) -> float | None:
        idx = -bizdays if len(closes) >= bizdays else 0
        base = float(closes.iloc[idx])
        if base <= 0:
            return None
        return (last / base - 1) * 100

    p1w = pct_for(BIZDAYS_1W)
    p1m = pct_for(BIZDAYS_1M)
    if p1w is None and p1m is None:
        return None
    return {
        "code": code,
        "name": name,
        "marcap": marcap,
        "last": last,
        "pct_1w": p1w,
        "pct_1m": p1m,
    }


def fetch_all_movers() -> list[dict]:
    """시총 임계값 이상 종목의 1주/1개월 변동률을 한 번의 fetch로 산출."""
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
    return results


def top_n(rows: list[dict], key: str, n: int = TOP_N) -> list[dict]:
    valid = [r for r in rows if r.get(key) is not None]
    valid.sort(key=lambda x: abs(x[key]), reverse=True)
    return valid[:n]


def fetch_news_for_topic(queries: list[str], limit: int) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for q in queries:
        url = (
            "https://news.google.com/rss/search?q="
            + urllib.parse.quote_plus(q)
            + "&hl=ko&gl=KR&ceid=KR:ko"
        )
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            log(f"WARN: feed '{q}' failed: {e}")
            continue
        for entry in feed.entries[:6]:
            title = (entry.get("title") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            out.append({"title": title, "link": entry.get("link", "")})
            if len(out) >= limit:
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


def render_movers_section(title: str, movers: list[dict], pct_key: str) -> list[str]:
    lines = [f"<b>{title}</b>"]
    if movers:
        for idx, m in enumerate(movers, 1):
            lines.append(
                f"{idx}. {escape(m['name'])} ({m['code']}) "
                f"<b>{fmt_pct(m[pct_key])}</b> | {fmt_marcap(m['marcap'])}"
            )
    else:
        lines.append("- (변동률 산출 실패)")
    lines.append("")
    return lines


def render_news_section(section_no: int, label: str, news: list[dict]) -> list[str]:
    lines = [f"<b>{section_no}. 향후 전망 — {label}</b>"]
    if news:
        for n in news:
            link = n["link"] or "#"
            lines.append(f'- <a href="{escape(link)}">{escape(n["title"])}</a>')
    else:
        lines.append("- (RSS 수신 실패)")
    lines.append("")
    return lines


def build_message(
    indices: list,
    movers_1w: list[dict],
    movers_1m: list[dict],
    news_by_topic: list[tuple[str, list[dict]]],
) -> str:
    lines = [f"<b>[국내 증시 브리핑] {TODAY.isoformat()} (KST)</b>", ""]

    # 1. 지수
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

    # 2. 1주 Top 10
    lines += render_movers_section(
        f"2. 1주 변동률 Top {TOP_N} (시총 {fmt_marcap(MIN_MARCAP)} 이상)",
        movers_1w, "pct_1w",
    )

    # 3. 1개월 Top 10
    lines += render_movers_section(
        f"3. 1개월 변동률 Top {TOP_N} (시총 {fmt_marcap(MIN_MARCAP)} 이상)",
        movers_1m, "pct_1m",
    )

    # 4~6. 향후 전망 (3섹션)
    for offset, (label, news) in enumerate(news_by_topic):
        lines += render_news_section(4 + offset, label, news)

    return "\n".join(lines).rstrip() + "\n"


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

    all_movers = fetch_all_movers()
    movers_1w = top_n(all_movers, "pct_1w", TOP_N)
    movers_1m = top_n(all_movers, "pct_1m", TOP_N)

    news_by_topic = [
        (label, fetch_news_for_topic(queries, NEWS_PER_TOPIC))
        for label, queries in NEWS_TOPICS
    ]

    msg = build_message(indices, movers_1w, movers_1m, news_by_topic)
    send_via_telegram(msg)


if __name__ == "__main__":
    main()
