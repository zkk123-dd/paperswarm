#!/usr/bin/env python
"""把论文提交到 Stanford Agentic Reviewer (paperreview.ai)，取回评分。

官方 API 来自站点自己的 ``/openapi.json``（FastAPI/uvicorn）：

1. ``POST /api/get-upload-url``   -> ``{success, presigned_url, s3_key, presigned_fields}``
2. ``POST <presigned_url>``       -> multipart，**presigned_fields 必须先于 file** 出现
3. ``POST /api/confirm-upload``   -> ``{success, token, ...}``，token 与本次提交一一对应
4. ``GET  /api/status/{token}``   -> 处理状态
5. ``GET  /api/review/{token}``   -> 评测正文与分数

提交是**不可撤销**的外部副作用：本脚本在真正发请求前会打印将要提交的文件指纹，
``--dry-run`` 只走第 1 步（拿预签名地址，不落任何文件），用来单独验证连通性与限流。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://paperreview.ai"
TIMEOUT = 60


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def _json_or_text(resp: requests.Response) -> object:
    try:
        return resp.json()
    except Exception:
        return resp.text[:2000]


def get_upload_url(filename: str, venue: str) -> dict:
    """第 1 步：向官方要一个 S3 预签名 POST 地址。"""
    _log(f"POST {BASE}/api/get-upload-url  filename={filename!r} venue={venue!r}")
    resp = requests.post(
        f"{BASE}/api/get-upload-url",
        json={"filename": filename, "venue": venue},
        timeout=TIMEOUT,
    )
    body = _json_or_text(resp)
    _log(f"  -> http={resp.status_code} body={json.dumps(body)[:400] if not isinstance(body, str) else body}")
    if resp.status_code == 429:
        raise RuntimeError(f"被限流(429): {body}")
    resp.raise_for_status()
    if not isinstance(body, dict) or not body.get("success"):
        raise RuntimeError(f"get-upload-url 未返回 success: {body}")
    for key in ("presigned_url", "s3_key", "presigned_fields"):
        if not body.get(key):
            raise RuntimeError(f"get-upload-url 缺少字段 {key}: {body}")
    return body


def upload_to_s3(pdf: Path, url_data: dict) -> None:
    """第 2 步：把 PDF 直传到 S3。presigned_fields 必须先于 file。"""
    _log(f"POST {url_data['presigned_url'][:80]}...  ({pdf.stat().st_size} bytes)")
    with pdf.open("rb") as fh:
        # requests 会先序列化 data 再追加 files，正好满足 S3 的字段顺序要求
        resp = requests.post(
            url_data["presigned_url"],
            data=url_data["presigned_fields"],
            files={"file": (pdf.name, fh, "application/pdf")},
            timeout=300,
        )
    _log(f"  -> http={resp.status_code} bytes={len(resp.content)}")
    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"S3 上传失败 http={resp.status_code}: {resp.text[:600]}")


def confirm_upload(s3_key: str, venue: str, email: str) -> dict:
    """第 3 步：确认上传，服务端建立提交记录并返回 access token。"""
    _log(f"POST {BASE}/api/confirm-upload  email={email} venue={venue!r}")
    resp = requests.post(
        f"{BASE}/api/confirm-upload",
        data={"s3_key": s3_key, "venue": venue, "email": email},
        timeout=TIMEOUT,
    )
    body = _json_or_text(resp)
    _log(f"  -> http={resp.status_code}")
    if resp.status_code == 429:
        raise RuntimeError(f"被限流(429): {body}")
    resp.raise_for_status()
    if not isinstance(body, dict) or not body.get("success"):
        raise RuntimeError(f"confirm-upload 未返回 success: {body}")
    if not body.get("token"):
        raise RuntimeError(f"confirm-upload 未返回 token: {body}")
    return body


def get_status(token: str) -> dict:
    resp = requests.get(f"{BASE}/api/status/{token}", timeout=TIMEOUT)
    body = _json_or_text(resp)
    return body if isinstance(body, dict) else {"raw": body, "http": resp.status_code}


def get_review(token: str) -> dict:
    resp = requests.get(f"{BASE}/api/review/{token}", timeout=TIMEOUT)
    body = _json_or_text(resp)
    return body if isinstance(body, dict) else {"raw": body, "http": resp.status_code}


def main() -> int:
    ap = argparse.ArgumentParser(description="提交论文到 Stanford Agentic Reviewer")
    ap.add_argument("--pdf", required=True, help="要提交的 PDF")
    ap.add_argument("--email", required=True, help="接收评测通知的邮箱")
    ap.add_argument("--venue", default="ICLR", help="venue（ICLR 才输出 1-10 综合分）")
    ap.add_argument("--out", required=True, help="提交记录落盘路径（JSON）")
    ap.add_argument("--dry-run", action="store_true", help="只测连通性，不真正提交")
    ap.add_argument("--poll-seconds", type=int, default=0, help="提交后轮询等待秒数")
    args = ap.parse_args()

    pdf = Path(args.pdf).resolve()
    if not pdf.is_file():
        print(f"PDF 不存在: {pdf}", file=sys.stderr)
        return 2

    raw = pdf.read_bytes()
    record: dict = {
        "submitted_at": _ts(),
        "endpoint": BASE,
        "pdf": str(pdf),
        "pdf_bytes": len(raw),
        "pdf_sha256": hashlib.sha256(raw).hexdigest(),
        "email": args.email,
        "venue": args.venue,
    }
    _log(f"待提交文件: {pdf.name}  {len(raw)} bytes  sha256={record['pdf_sha256'][:16]}...")

    try:
        url_data = get_upload_url(pdf.name, args.venue)
        record["get_upload_url"] = {k: v for k, v in url_data.items() if k != "presigned_fields"}
        record["presigned_field_keys"] = sorted(url_data.get("presigned_fields", {}).keys())
        if args.dry_run:
            record["dry_run"] = True
            record["status"] = "dry-run-ok（未提交）"
            _log("dry-run：连通性与预签名地址均正常，未上传、未提交。")
        else:
            upload_to_s3(pdf, url_data)
            record["s3_uploaded"] = True
            confirmed = confirm_upload(url_data["s3_key"], args.venue, args.email)
            record["confirm"] = confirmed
            record["token"] = confirmed["token"]
            record["status"] = "submitted"
            _log(f"提交成功。access token = {record['token']}")

            if args.poll_seconds > 0:
                deadline = time.time() + args.poll_seconds
                while time.time() < deadline:
                    st = get_status(record["token"])
                    record.setdefault("status_polls", []).append({"at": _ts(), "body": st})
                    _log(f"  status -> {json.dumps(st)[:300]}")
                    state = str(st.get("status", "")).lower()
                    if state in ("completed", "done", "finished", "ready", "failed", "error"):
                        break
                    time.sleep(20)
                rev = get_review(record["token"])
                record["review"] = rev
                _log(f"  review -> {json.dumps(rev)[:600]}")
    except Exception as exc:  # noqa: BLE001 - 提交链路任何失败都要留痕
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["status"] = "failed"
        _log(f"失败: {record['error']}")
    finally:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        _log(f"提交记录已落盘: {out}")

    return 0 if record.get("status") in ("submitted", "dry-run-ok（未提交）") else 1


if __name__ == "__main__":
    raise SystemExit(main())
