"""광주 시민의숲 야영장 빈자리 감시 → ntfy 알림.

dates.txt 에 적힌 날짜마다 일반(G)/자동차(C) 야영장의 예약가능 사이트를 조회하고,
지난번보다 새로 열린 자리가 있으면 ntfy 로 휴대폰에 알림을 보낸다.

GitHub 예약 실행(cron)은 몇 시간씩 밀리기 일쑤라, 한 번 실행되면
LOOP_MINUTES 동안 INTERVAL_SEC 마다 반복 확인한다.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
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
LOOP_MINUTES = float(os.environ.get("LOOP_MINUTES", "0"))  # 0 이면 한 번만
INTERVAL_SEC = int(os.environ.get("INTERVAL_SEC", "120"))


def dates_text():
    """실행 도중 dates.txt 가 GitHub 에서 수정돼도 반영되도록 원격 최신본을 우선 읽는다."""
    if LOOP_MINUTES:
        try:
            subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=HERE, check=True, timeout=30)
            return subprocess.run(["git", "show", "FETCH_HEAD:dates.txt"], cwd=HERE, check=True,
                                  capture_output=True, timeout=30).stdout.decode("utf-8")
        except Exception as e:
            print(f"원격 dates.txt 읽기 실패, 로컬 파일 사용: {e}")
    with open(DATES_FILE, encoding="utf-8") as f:
        return f.read()


def read_dates():
    """dates.txt 한 줄에 하나: '2026-11-07' (둘 다) 또는 '2026-11-07 C' (자동차만)."""
    today = datetime.now(KST).date()
    targets = []
    for line in dates_text().splitlines():
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


def send_ntfy(title, message):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    payload = {"topic": topic, "title": title, "message": message,
               "priority": 4, "tags": ["camping"], "click": BOOK_URL}
    req = urllib.request.Request(
        "https://ntfy.sh/", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()
    return True


_kakao = {"token": None, "expires": 0, "warned": False}


def _kakao_post(url, data, token=None):
    headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "detail": e.read().decode(errors="replace")}


def _kakao_warn(text):
    """카톡 재연결이 필요할 때 실행당 한 번만 ntfy 로 알린다 (토큰 값은 출력하지 않음)."""
    print(text)
    if not _kakao["warned"]:
        _kakao["warned"] = True
        send_ntfy("⚠️ 카톡 알림 재연결 필요", text)


def kakao_access_token():
    rest = os.environ.get("KAKAO_REST_KEY")
    refresh = os.environ.get("KAKAO_REFRESH_TOKEN")
    if not rest or not refresh:
        return None
    if _kakao["token"] and time.time() < _kakao["expires"] - 300:
        return _kakao["token"]
    res = _kakao_post("https://kauth.kakao.com/oauth/token", {
        "grant_type": "refresh_token", "client_id": rest, "refresh_token": refresh,
        "client_secret": os.environ.get("KAKAO_CLIENT_SECRET", ""),
    })
    if "access_token" not in res:
        _kakao_warn("카카오 연결이 만료돼 카톡 알림을 못 보내고 있어요. "
                    "PC에서 kakao_setup.py 를 다시 실행해 주세요. (ntfy 알림은 계속 옵니다)")
        return None
    if "refresh_token" in res:  # 만료 한 달 이내라 새 토큰이 왔지만 여기선 저장할 수 없음
        _kakao_warn("카톡 알림 연결이 한 달 안에 만료돼요. "
                    "PC에서 kakao_setup.py 를 다시 실행해서 새 토큰으로 바꿔 주세요.")
    _kakao["token"] = res["access_token"]
    _kakao["expires"] = time.time() + int(res.get("expires_in", 0))
    return _kakao["token"]


def send_kakao(title, message):
    token = kakao_access_token()
    if not token:
        return False
    template = {"object_type": "text", "text": f"{title}\n{message}"[:200],
                "link": {"web_url": BOOK_URL, "mobile_web_url": BOOK_URL},
                "button_title": "예약 페이지"}
    res = _kakao_post("https://kapi.kakao.com/v2/api/talk/memo/default/send",
                      {"template_object": json.dumps(template, ensure_ascii=False)}, token)
    if res.get("result_code") != 0:
        print(f"카톡 발송 실패: {res.get('error')}")
        return False
    return True


def notify(title, message):
    sent = []
    for name, fn in (("카톡", send_kakao), ("ntfy", send_ntfy)):
        try:
            if fn(title, message):
                sent.append(name)
        except Exception as e:  # 한쪽이 실패해도 다른 쪽은 보낸다
            print(f"{name} 발송 오류: {e}")
    if sent:
        print("알림 발송:", ", ".join(sent))
    else:
        print("[알림 채널 없음 - 알림 생략]", title, message, sep="\n")


def check_once(state):
    """한 번 조회하고 알림을 보낸 뒤 새 상태를 돌려준다."""
    targets = read_dates()
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
        notify("🏕️ 시민의숲 야영장 빈자리!", "\n".join(alerts))
    return new_state


def main():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    first = state

    if os.environ.get("TEST_NOTIFY") == "true":
        notify("🏕️ 테스트 알림", "GitHub에서 보낸 테스트예요. 빈자리가 생기면 이렇게 알려드려요.")

    deadline = time.monotonic() + LOOP_MINUTES * 60
    while True:
        print(f"--- {datetime.now(KST):%m-%d %H:%M:%S}")
        try:
            state = check_once(state)
        except Exception as e:  # 일시적 오류로 반복이 끊기지 않게
            print(f"확인 중 오류: {e}")
        sys.stdout.flush()
        if time.monotonic() + INTERVAL_SEC > deadline:
            break
        time.sleep(INTERVAL_SEC)

    if state != first:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


if __name__ == "__main__":
    sys.exit(main())
