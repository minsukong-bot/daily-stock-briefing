#!/usr/bin/env python3
"""send_telegram.py — Telegram 메시지/문서 전송 (UTF-8 안전, Windows 호환)

사용법:
    # 메시지 전송 (인자)
    python send_telegram.py "메시지 본문"

    # 메시지 전송 (stdin)
    echo "메시지" | python send_telegram.py -

    # 메시지 전송 (파일)
    python send_telegram.py --file path/to/msg.txt

    # 문서 첨부
    python send_telegram.py --doc path/to/file.md --caption "캡션"

옵션:
    --parse-mode HTML|MarkdownV2|none  (default: HTML)
    --no-preview                        링크 미리보기 비활성 (default: 비활성)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib import request, parse, error

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if not ENV_FILE.exists():
        sys.exit(f"ERROR: .env not found at {ENV_FILE}")
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    for required in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not env.get(required):
            sys.exit(f"ERROR: {required} missing in .env")
    return env


def send_message(token: str, chat_id: str, text: str,
                 parse_mode: str = "HTML",
                 disable_preview: bool = True) -> dict:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_preview,
    }
    if parse_mode and parse_mode.lower() != "none":
        payload["parse_mode"] = parse_mode
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url, data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        sys.exit(f"FAILED ({e.code}): {body}")
    except Exception as e:
        sys.exit(f"FAILED: {e}")


def send_document(token: str, chat_id: str, file_path: Path,
                  caption: str = "", parse_mode: str = "HTML") -> dict:
    """multipart/form-data 직접 구성 (외부 의존성 없음)"""
    url = f"https://api.telegram.org/bot{token}/sendDocument"

    boundary = "----DDB" + os.urandom(8).hex()
    crlf = b"\r\n"
    body = bytearray()

    def add_field(name: str, value: str) -> None:
        body.extend(f"--{boundary}".encode("utf-8") + crlf)
        body.extend(
            f'Content-Disposition: form-data; name="{name}"'.encode("utf-8")
            + crlf + crlf
        )
        body.extend(value.encode("utf-8") + crlf)

    add_field("chat_id", chat_id)
    if caption:
        add_field("caption", caption)
    if parse_mode and parse_mode.lower() != "none":
        add_field("parse_mode", parse_mode)

    body.extend(f"--{boundary}".encode("utf-8") + crlf)
    body.extend(
        f'Content-Disposition: form-data; name="document"; '
        f'filename="{file_path.name}"'.encode("utf-8") + crlf
    )
    body.extend(b"Content-Type: application/octet-stream" + crlf + crlf)
    body.extend(file_path.read_bytes())
    body.extend(crlf)
    body.extend(f"--{boundary}--".encode("utf-8") + crlf)

    req = request.Request(
        url, data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        body_err = e.read().decode("utf-8", errors="replace")
        sys.exit(f"FAILED ({e.code}): {body_err}")
    except Exception as e:
        sys.exit(f"FAILED: {e}")


def main() -> None:
    p = argparse.ArgumentParser(description="Send Telegram message or document")
    p.add_argument("text", nargs="?", help='메시지 본문 (또는 "-" → stdin)')
    p.add_argument("--file", help="메시지 본문을 읽어올 텍스트 파일")
    p.add_argument("--doc", help="첨부할 문서 경로")
    p.add_argument("--caption", default="", help="문서 캡션")
    p.add_argument("--parse-mode", default="HTML",
                   choices=["HTML", "MarkdownV2", "none"])
    p.add_argument("--preview", action="store_true",
                   help="링크 미리보기 활성화 (기본 비활성)")
    args = p.parse_args()

    env = load_env()
    token = env["TELEGRAM_BOT_TOKEN"]
    chat_id = env["TELEGRAM_CHAT_ID"]

    if args.doc:
        path = Path(args.doc)
        if not path.is_file():
            sys.exit(f"ERROR: file not found: {path}")
        result = send_document(token, chat_id, path,
                               caption=args.caption,
                               parse_mode=args.parse_mode)
    else:
        if args.file:
            text = Path(args.file).read_text(encoding="utf-8")
        elif args.text == "-" or args.text is None:
            text = sys.stdin.read()
        else:
            text = args.text
        if not text.strip():
            sys.exit("ERROR: empty message")
        result = send_message(token, chat_id, text,
                              parse_mode=args.parse_mode,
                              disable_preview=not args.preview)

    if result.get("ok"):
        print("OK")
    else:
        sys.exit(f"FAILED: {json.dumps(result, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
