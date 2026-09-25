#!/usr/bin/env python3
"""가격 · RSI 알림 발송

주식서포터.html 의 SEED_STOCKS(방금 갱신된 최신 시세)를 읽고,
Firestore alerts 컬렉션에 저장된 사용자 알림 조건과 비교해
조건을 만족하면 Web Push 로 폰에 알림을 보낸다.

- 알림 조건 문서 구조 (alerts/{포트폴리오ID} 의 json 필드)
    {
      "subs":  { "<구독키>": {"endpoint": ..., "keys": {"p256dh": ..., "auth": ...}} },
      "rules": { "<티커>": {"name", "market",
                            "price", "priceDir", "priceFired",
                            "rsi",   "rsiDir",   "rsiFired"} }
    }
- 한 번 보낸 조건은 Fired 플래그를 세워 반복 발송을 막고,
  값이 조건에서 다시 벗어나면 플래그를 풀어 다음 도달 때 또 알린다.

환경변수: VAPID_PRIVATE_KEY (필수)
"""

import json
import os
import re
import sys

import requests
from pywebpush import WebPushException, webpush

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "주식서포터.html")

FIREBASE_PROJECT_ID = "stock-supporter-5f99e"
FIREBASE_API_KEY = "AIzaSyD7KGEvtqFIhSauLO-G2xzPcuG1v-VZtFo"
FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}"
    "/databases/default/documents"
)

VAPID_CLAIMS = {"sub": "mailto:stock-supporter@users.noreply.github.com"}
APP_URL = (
    "https://990501s-design.github.io/stock-supporter/"
    "%EC%A3%BC%EC%8B%9D%EC%84%9C%ED%8F%AC%ED%84%B0.html"
)


def load_quotes():
    """HTML에서 티커별 현재가/RSI를 읽어온다."""
    with open(HTML_PATH, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"var SEED_STOCKS = (\[.*\]);", html)
    if not m:
        print("🚨 SEED_STOCKS 를 찾지 못했습니다.")
        sys.exit(1)
    quotes = {}
    for s in json.loads(m.group(1)):
        quotes[s["ticker"]] = s
    return quotes


def list_alert_docs():
    """alerts 컬렉션의 모든 문서를 [(문서ID, 내용)] 로 반환."""
    docs = []
    page_token = None
    while True:
        params = {"key": FIREBASE_API_KEY, "pageSize": 300}
        if page_token:
            params["pageToken"] = page_token
        res = requests.get(f"{FIRESTORE_BASE}/alerts", params=params, timeout=20)
        if res.status_code == 404:
            return docs
        res.raise_for_status()
        body = res.json()
        for doc in body.get("documents", []):
            doc_id = doc["name"].rsplit("/", 1)[-1]
            raw = doc.get("fields", {}).get("json", {}).get("stringValue")
            if not raw:
                continue
            try:
                docs.append((doc_id, json.loads(raw)))
            except json.JSONDecodeError:
                continue
        page_token = body.get("nextPageToken")
        if not page_token:
            return docs


def save_alert_doc(doc_id, data):
    body = {"fields": {"json": {"stringValue": json.dumps(data, ensure_ascii=False)}}}
    requests.patch(
        f"{FIRESTORE_BASE}/alerts/{doc_id}",
        params={"key": FIREBASE_API_KEY, "updateMask.fieldPaths": "json"},
        json=body,
        timeout=20,
    ).raise_for_status()


def reached(value, target, direction):
    return value >= target if direction == "above" else value <= target


def fmt_price(value, market):
    if market == "KR":
        return f"{round(value):,}원"
    return f"${value:,.2f}".rstrip("0").rstrip(".")


def send_push(sub, payload, private_key):
    """발송 성공 시 True, 구독이 만료됐으면 None(삭제 대상), 일시 오류면 False."""
    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=private_key,
            vapid_claims=dict(VAPID_CLAIMS),
            ttl=1800,
        )
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            return None
        print(f"  ⚠️ 푸시 실패(status={status}): {e}")
        return False


def main():
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
    if not private_key:
        print("VAPID_PRIVATE_KEY 가 없어 알림 발송을 건너뜁니다.")
        return

    quotes = load_quotes()
    docs = list_alert_docs()
    if not docs:
        print("등록된 알림이 없습니다.")
        return

    for doc_id, data in docs:
        rules = data.get("rules") or {}
        subs = data.get("subs") or {}
        if not rules or not subs:
            continue

        messages = []
        changed = False

        for ticker, rule in rules.items():
            stock = quotes.get(ticker)
            if not stock:
                continue
            market = rule.get("market") or stock.get("market")
            label = rule.get("name") or ticker

            price, price_target = stock.get("price"), rule.get("price")
            if price is not None and price_target is not None:
                hit = reached(price, price_target, rule.get("priceDir"))
                if hit and not rule.get("priceFired"):
                    rule["priceFired"] = True
                    changed = True
                    messages.append(
                        f"{label} {fmt_price(price, market)} "
                        f"(목표 {fmt_price(price_target, market)} 도달)"
                    )
                elif not hit and rule.get("priceFired"):
                    rule["priceFired"] = False
                    changed = True

            rsi, rsi_target = stock.get("rsi"), rule.get("rsi")
            if rsi is not None and rsi_target is not None:
                hit = reached(rsi, rsi_target, rule.get("rsiDir"))
                if hit and not rule.get("rsiFired"):
                    rule["rsiFired"] = True
                    changed = True
                    messages.append(f"{label} RSI {rsi} (목표 {rsi_target} 도달)")
                elif not hit and rule.get("rsiFired"):
                    rule["rsiFired"] = False
                    changed = True

        if messages:
            payload = {
                "title": "주식 서포터 알림",
                "body": "\n".join(messages),
                "tag": f"stock-alert-{doc_id}",
                "url": APP_URL,
            }
            for sub_key in list(subs.keys()):
                result = send_push(subs[sub_key], payload, private_key)
                if result is None:
                    del subs[sub_key]
                    changed = True
            print(f"📨 {doc_id}: {len(messages)}건 발송 ({len(subs)}개 기기)")

        if changed:
            data["rules"], data["subs"] = rules, subs
            save_alert_doc(doc_id, data)

    print("✅ 알림 검사 완료")


if __name__ == "__main__":
    main()
