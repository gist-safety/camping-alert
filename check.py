"""광주 시민의숲 야영장 빈자리 감시 → ntfy 알림.

dates.txt 에 적힌 날짜마다 일반(G)/자동차(C) 야영장의 예약가능 사이트를 조회하고,
지난번보다 새로 열린 자리가 있으면 ntfy 로 휴대폰에 알림을 보낸다.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE = "https://www.gwangju.go.kr"
DETAIL_URL = BASE + "/reserve/getParkDetailData.do"
BOOK_URL = BASE + "/reserve/parkBscList.do?pageId=reserve48"
TYPES = {"G": "일반 야영장", "C": "자동차 야영장"}
BSC_SN = {"G": "6", "C": "4"}  # 사이트 내부 시설번호

HERE = os.path.dirname(os.path.abspath(__file__))
DATES_FILE = os.path.join(HERE, "dates.txt")
STATE_FILE = os.path.join(HERE, "state.json")
KST = timezone(timedelta(hours=9))


def read_dates():
    """dates.txt 한 줄에 하나: '2026-11-07' (둘 다) 또는 '2026-11-07 C' (자동차만)."""
    today = datetime.now(KST).date()
    targets = []
    with open(DATES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            try:
                d = date.fromisoformat(parts[0])
            except ValueError:
                print(f"날짜 형식 오류, 건너뜀: {line}")
                continue
            if d < today:
                continue
            types = [t.upper() for t in parts[1:] if t.upper() in TYPES] or list(TYPES)
            targets.append((d.isoformat(), types))
    return targets


def fetch_open_sites(ymd, kind):
    """해당 날짜·야영장의 예약가능 사이트 번호 목록 (예: ['03', '15'])."""
    body = urllib.parse.urlencode({
        "pageId": "reserve48", "ctgry": kind, "bscSn": BSC_SN[kind], "searchYmd": ymd,
    }).encode()
    req = urllib.request.Request(DETAIL_URL, data=body, headers={
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    if data.get("error") != "N":
        raise RuntimeError(f"{ymd} 조회 실패: {data}")
    return sorted(
        str(s["fcltPosition"]).rsplit("_", 1)[-1]
        for s in data.get("useFcltList", [])
        if s.get("sts") == "open" and str(s.get("fcltPosition", "")).startswith(kind + "_"))


def notify(title, message):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("[NTFY_TOPIC 없음 - 알림 생략]", title, message, sep="\n")
        return
    payload = {"topic": topic, "title": title, "message": message,
               "priority": 4, "tags": ["camping"], "click": BOOK_URL}
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def main():
    targets = read_dates()
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}

    new_state = {}
    alerts = []
    for ymd, types in targets:
        prev = state.get(ymd, {})
        new_state[ymd] = {}
        for t in types:
            try:
                now_open = fetch_open_sites(ymd, t)
            except Exception as e:  # 하나 실패해도 나머지는 계속
                print(f"{ymd} {TYPES[t]} 조회 실패: {e}")
                new_state[ymd][t] = prev.get(t, [])
                continue
            before = set(prev.get(t, []))
            newly = [n for n in now_open if n not in before]
            new_state[ymd][t] = now_open
            print(f"{ymd} {TYPES[t]}: {len(now_open)}자리 예약가능")
            if newly:
                alerts.append(f"{ymd} {TYPES[t]}: {len(now_open)}자리 (새로 열림: {', '.join(newly)}번)")
            time.sleep(1)

    if alerts:
        notify("🏕️ 시민의숲 야영장 빈자리!", "\n".join(alerts) + "\n\n눌러서 예약 페이지 열기")

    if new_state != state:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(new_state, f, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    sys.exit(main())
