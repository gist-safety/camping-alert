"""광주 전천후테니스장(풍암동) 빈자리 감시 → 카톡 + ntfy 알림.

tennis_dates.txt 에 적힌 날짜마다 1~4코트의 시간대별 예약 상태를 확인하고,
새로 "예약가능"이 된 시간대가 생기면 카톡과 ntfy 로 알림을 보낸다.

예약 페이지(gwangjusportsinfo.org)는 로그인 없이 월별 달력 HTML 을 보여준다.
달력 칸(td id="YYYY-MM-DD") 안의 <li> 하나가 2시간짜리 시간대 하나이고,
예약가능이면 초록색(#8FC31F) 글씨로 "주간(07:00~09:00)" 처럼, 예약완료면 빨간색으로
예약자 이름이 나온다. 아직 예약이 열리지 않은 날은 <li> 가 없다.

GitHub 예약 실행(cron)은 몇 시간씩 밀리기 일쑤라, 한 번 실행되면
LOOP_MINUTES 동안 INTERVAL_SEC 마다 반복 확인한다.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

BASE = "https://www.gwangjusportsinfo.org"
FACILITY = 1                      # 전천후테니스장
COURTS = [1, 2, 3, 4]             # 1코트~4코트
BOOK_URL = BASE + f"/reservation/reservation_view/{FACILITY}/1?agree=1"
PLACE = "전천후테니스장"
# 달력의 시간대 번호(selType) → 시작 시각 (글씨가 잘려 보일 때 대비)
SLOT_TIMES = {"144": "07:00", "77": "09:00", "125": "11:00", "126": "13:00",
              "127": "15:00", "152": "17:00", "128": "19:00", "148": "21:00"}

HERE = os.path.dirname(os.path.abspath(__file__))
DATES_NAME = "tennis_dates.txt"
DATES_FILE = os.path.join(HERE, DATES_NAME)
STATE_FILE = os.path.join(HERE, "tennis_state.json")
KST = timezone(timedelta(hours=9))
LOOP_MINUTES = float(os.environ.get("LOOP_MINUTES", "0"))  # 0 이면 한 번만
INTERVAL_SEC = int(os.environ.get("INTERVAL_SEC", "120"))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def dates_text():
    """실행 도중 날짜 파일이 GitHub 에서 수정돼도 반영되도록 원격 최신본을 우선 읽는다."""
    if LOOP_MINUTES:
        try:
            subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=HERE, check=True, timeout=30)
            return subprocess.run(["git", "show", f"FETCH_HEAD:{DATES_NAME}"], cwd=HERE, check=True,
                                  capture_output=True, timeout=30).stdout.decode("utf-8")
        except Exception as e:
            print(f"원격 {DATES_NAME} 읽기 실패, 로컬 파일 사용: {e}")
    with open(DATES_FILE, encoding="utf-8") as f:
        return f.read()


def read_dates():
    """한 줄에 하나: 날짜 [n코트 ...] [HH:MM ...]

    2026-12-05                → 모든 코트, 모든 시간
    2026-12-05 19:00 21:00    → 19시, 21시 시작 시간대만
    2026-12-05 2코트 3코트    → 2·3코트만
    """
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
        courts, times = [], []
        for p in parts[1:]:
            m = re.fullmatch(r"(\d)\s*코트", p)
            if m and int(m.group(1)) in COURTS:
                courts.append(int(m.group(1)))
                continue
            m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?시?", p)
            if m:
                times.append(f"{int(m.group(1)):02d}:{m.group(2) or '00'}")
                continue
            print(f"알 수 없는 조건 '{p}' 무시: {line}")
        targets.append((d.isoformat(), courts or list(COURTS), times))
    return targets


def fetch_page(court, year, month):
    url = (BASE + f"/reservation/reservation_view/{FACILITY}/{court}?"
           + urllib.parse.urlencode({"year": year, "month": month, "agree": 1}))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


TD_RE = re.compile(r'<td[^>]*\bid="(\d{4}-\d{2}-\d{2})"[^>]*>(.*?)</td>', re.S)
LI_RE = re.compile(r"<li\b([^>]*)>(.*?)</li>", re.S)


def parse_month(html):
    """{'2026-12-01': ['07:00~09:00', ...]} : 날짜별 예약가능 시간대 목록."""
    if "btn_pop" not in html:
        raise RuntimeError("달력을 찾지 못했어요 (사이트 구조가 바뀌었을 수 있음)")
    result = {}
    for ymd, cell in TD_RE.findall(html):
        free = []
        for attrs, body in LI_RE.findall(cell):
            if "8FC31F" not in body.upper() and "child" not in attrs:
                continue  # 빨간 글씨 = 예약완료
            text = re.sub(r"<[^>]+>|\s+", " ", body)
            m = re.search(r"(\d{2}:\d{2})\s*~\s*(\d{2}:\d{2})", text)
            if m:
                free.append(f"{m.group(1)}~{m.group(2)}")
            else:
                sel = re.search(r'name="selType"\s+value="(\d+)"', body)
                free.append(SLOT_TIMES.get(sel.group(1), sel.group(1)) if sel else text.strip())
        result[ymd] = free
    return result


def fetch_month(court, year, month):
    for attempt in (1, 2):
        try:
            return parse_month(fetch_page(court, year, month))
        except (urllib.error.URLError, TimeoutError, RuntimeError):
            if attempt == 2:
                raise
            time.sleep(3)


def send_ntfy(title, message):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    payload = {"topic": topic, "title": title, "message": message,
               "priority": 4, "tags": ["tennis"], "click": BOOK_URL}
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
    """한 번 조회하고 알림을 보낸 뒤 새 상태를 돌려준다.

    state 형식: {"2026-12-05": {"1": ["19:00~21:00"], "2": []}, ...}  (예약가능 시간대)
    """
    targets = read_dates()
    months = {}  # (court, year, month) → {날짜: [시간대]}
    new_state = {}
    alerts = []
    for ymd, courts, times in targets:
        prev = state.get(ymd, {})
        new_state[ymd] = {}
        y, m = int(ymd[:4]), int(ymd[5:7])
        for c in courts:
            before = prev.get(str(c), [])
            key = (c, y, m)
            try:
                if key not in months:
                    months[key] = fetch_month(c, y, m)
                    time.sleep(1)
                free = months[key].get(ymd, [])
            except Exception as e:  # 하나 실패해도 나머지는 계속
                print(f"{ymd} {c}코트 조회 실패: {e}")
                new_state[ymd][str(c)] = before
                continue
            if times:
                free = [s for s in free if s[:5] in times]
            new_state[ymd][str(c)] = free
            print(f"{ymd} {c}코트 예약가능: {', '.join(free) or '없음'}")
            opened = [s for s in free if s not in before]
            if opened:
                alerts.append(f"{ymd[5:]} {c}코트 {', '.join(opened)}")

    if alerts:
        notify(f"🎾 {PLACE} 빈자리!", "\n".join(alerts))
    return new_state


def site_check_summary():
    """테스트 때 사이트 조회가 되는지 이번 달 1코트 달력을 한 번 읽어 본다."""
    now = datetime.now(KST)
    try:
        days = fetch_month(1, now.year, now.month)
        today = now.date().isoformat()
        slots = sum(len(v) for d, v in days.items() if d >= today)
        text = f"사이트 조회 성공: 이번 달 1코트 남은 예약가능 {slots}타임"
    except Exception as e:
        text = f"사이트 조회 실패: {e}"
    print(text)
    return text


def main():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    first = state

    if os.environ.get("TEST_NOTIFY") == "true":
        notify("🎾 테스트 알림", f"{PLACE} 감시를 시작했어요. 빈자리가 생기면 이렇게 알려드려요.\n"
               + site_check_summary())

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
