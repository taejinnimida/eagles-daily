from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT = DATA_DIR / "eagles.json"

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
TODAY = NOW.date()

URLS = {
    "standings": "https://www.koreabaseball.com/Record/TeamRank/TeamRankDaily.aspx",
    "team_hitting": "https://www.koreabaseball.com/Record/Team/Hitter/Basic1.aspx",
    "team_pitching": "https://www.koreabaseball.com/Record/Team/Pitcher/Basic1.aspx",
    "player_hitting": "https://www.koreabaseball.com/Record/Player/HitterBasic/Basic1.aspx",
    "player_pitching": "https://www.koreabaseball.com/Record/Player/PitcherBasic/Basic1.aspx",
    "schedule": "https://eng.koreabaseball.com/Schedule/DailySchedule.aspx",
}

TEAM_KO = {
    "LG": "LG 트윈스",
    "KT": "KT 위즈",
    "삼성": "삼성 라이온즈",
    "KIA": "KIA 타이거즈",
    "한화": "한화 이글스",
    "두산": "두산 베어스",
    "NC": "NC 다이노스",
    "롯데": "롯데 자이언츠",
    "SSG": "SSG 랜더스",
    "키움": "키움 히어로즈",
}

TEAM_EN = {
    "LG": "LG 트윈스",
    "KT": "KT 위즈",
    "SAMSUNG": "삼성 라이온즈",
    "KIA": "KIA 타이거즈",
    "HANWHA": "한화 이글스",
    "DOOSAN": "두산 베어스",
    "NC": "NC 다이노스",
    "LOTTE": "롯데 자이언츠",
    "SSG": "SSG 랜더스",
    "KIWOOM": "키움 히어로즈",
}

VENUE_KO = {
    "JAMSIL": "잠실",
    "DAEJEON": "대전 한화생명 볼파크",
    "DAEGU": "대구",
    "SUWON": "수원",
    "SAJIK": "부산 사직",
    "MUNHAK": "인천 문학",
    "GWANGJU": "광주",
    "CHANGWON": "창원",
    "GOCHEOKSKY": "고척",
}


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/149 Safari/537.36"
            )
        }
    )
    return session


HTTP = make_session()


def get_soup(url: str) -> BeautifulSoup:
    response = HTTP.get(url, timeout=(15, 45))
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return BeautifulSoup(response.text, "html.parser")


def load_old() -> dict[str, Any]:
    if not OUTPUT.exists():
        return {}
    try:
        return json.loads(OUTPUT.read_text(encoding="utf-8"))
    except Exception:
        return {}


def table_rows(
    soup: BeautifulSoup,
    required_headers: tuple[str, ...],
) -> tuple[list[str], list[list[str]]]:
    for table in soup.find_all("table"):
        header_cells = table.select("thead th")
        if header_cells:
            headers = [clean(cell.get_text(" ", strip=True)) for cell in header_cells]
            body_rows = table.select("tbody tr")
        else:
            all_rows = table.find_all("tr")
            if not all_rows:
                continue
            headers = [
                clean(cell.get_text(" ", strip=True))
                for cell in all_rows[0].find_all(["th", "td"])
            ]
            body_rows = all_rows[1:]

        header_text = " ".join(headers)
        if not all(token in header_text for token in required_headers):
            continue

        rows: list[list[str]] = []
        for tr in body_rows:
            cells = [
                clean(cell.get_text(" ", strip=True))
                for cell in tr.find_all(["th", "td"])
            ]
            if cells:
                rows.append(cells)
        return headers, rows

    raise RuntimeError(
        "필요한 KBO 표를 찾지 못했습니다: " + ", ".join(required_headers)
    )


def parse_reference_date(soup: BeautifulSoup) -> str | None:
    text = clean(soup.get_text(" ", strip=True))
    match = re.search(r"(20\d{2})[.\-/년]\s*(\d{1,2})[.\-/월]\s*(\d{1,2})", text)
    if not match:
        return None
    year, month, day = map(int, match.groups())
    return date(year, month, day).isoformat()


def parse_standings(soup: BeautifulSoup) -> tuple[str | None, list[dict[str, str]]]:
    _, rows = table_rows(soup, ("순위", "팀명", "게임차", "최근10경기"))
    output: list[dict[str, str]] = []

    for cells in rows:
        if len(cells) < 10 or not cells[0].isdigit():
            continue
        team_short = cells[1]
        if team_short not in TEAM_KO:
            continue
        output.append(
            {
                "rank": cells[0],
                "team": TEAM_KO[team_short],
                "short_team": team_short,
                "games": cells[2],
                "wins": cells[3],
                "losses": cells[4],
                "draws": cells[5],
                "pct": cells[6],
                "gb": cells[7],
                "last10": cells[8],
                "streak": cells[9],
            }
        )

    if len(output) != 10:
        raise RuntimeError(f"KBO 순위표가 {len(output)}개 팀만 수집됐습니다.")
    return parse_reference_date(soup), output


def parse_team_hitting(soup: BeautifulSoup) -> list[dict[str, str]]:
    _, rows = table_rows(soup, ("팀명", "AVG", "G", "HR", "RBI"))
    output: list[dict[str, str]] = []

    for cells in rows:
        if len(cells) < 13 or not cells[0].isdigit():
            continue
        team_short = cells[1]
        if team_short not in TEAM_KO:
            continue
        output.append(
            {
                "rank": cells[0],
                "team": TEAM_KO[team_short],
                "avg": cells[2],
                "games": cells[3],
                "pa": cells[4],
                "ab": cells[5],
                "runs": cells[6],
                "hits": cells[7],
                "doubles": cells[8],
                "triples": cells[9],
                "hr": cells[10],
                "tb": cells[11],
                "rbi": cells[12],
            }
        )
    return output


def parse_team_pitching(soup: BeautifulSoup) -> list[dict[str, str]]:
    _, rows = table_rows(soup, ("팀명", "ERA", "G", "WHIP"))
    output: list[dict[str, str]] = []

    for cells in rows:
        if len(cells) < 18 or not cells[0].isdigit():
            continue
        team_short = cells[1]
        if team_short not in TEAM_KO:
            continue
        output.append(
            {
                "rank": cells[0],
                "team": TEAM_KO[team_short],
                "era": cells[2],
                "games": cells[3],
                "wins": cells[4],
                "losses": cells[5],
                "saves": cells[6],
                "holds": cells[7],
                "wpct": cells[8],
                "innings": cells[9],
                "hits": cells[10],
                "hr": cells[11],
                "walks": cells[12],
                "hbp": cells[13],
                "strikeouts": cells[14],
                "runs": cells[15],
                "earned_runs": cells[16],
                "whip": cells[17],
            }
        )
    return output


def parse_hitter_leaders(soup: BeautifulSoup) -> list[dict[str, str]]:
    _, rows = table_rows(soup, ("선수명", "팀명", "AVG", "G", "HR", "RBI"))
    output: list[dict[str, str]] = []

    for cells in rows:
        if len(cells) < 14 or not cells[0].isdigit():
            continue
        if cells[2] != "한화":
            continue
        output.append(
            {
                "rank": cells[0],
                "name": cells[1],
                "team": "한화 이글스",
                "avg": cells[3],
                "games": cells[4],
                "pa": cells[5],
                "ab": cells[6],
                "runs": cells[7],
                "hits": cells[8],
                "doubles": cells[9],
                "triples": cells[10],
                "hr": cells[11],
                "tb": cells[12],
                "rbi": cells[13],
            }
        )
    return output


def parse_pitcher_leaders(soup: BeautifulSoup) -> list[dict[str, str]]:
    _, rows = table_rows(soup, ("선수명", "팀명", "ERA", "G", "WHIP"))
    output: list[dict[str, str]] = []

    for cells in rows:
        if len(cells) < 19 or not cells[0].isdigit():
            continue
        if cells[2] != "한화":
            continue
        output.append(
            {
                "rank": cells[0],
                "name": cells[1],
                "team": "한화 이글스",
                "era": cells[3],
                "games": cells[4],
                "wins": cells[5],
                "losses": cells[6],
                "saves": cells[7],
                "holds": cells[8],
                "wpct": cells[9],
                "innings": cells[10],
                "hits": cells[11],
                "hr": cells[12],
                "walks": cells[13],
                "hbp": cells[14],
                "strikeouts": cells[15],
                "runs": cells[16],
                "earned_runs": cells[17],
                "whip": cells[18],
            }
        )
    return output


def parse_schedule(soup: BeautifulSoup) -> list[dict[str, Any]]:
    teams = sorted(TEAM_EN, key=len, reverse=True)
    team_pattern = "|".join(re.escape(team) for team in teams)
    game_pattern = re.compile(
        rf"\b({team_pattern})\b\s*(\d{{0,2}})\s*:\s*(\d{{0,2}})\s*\b({team_pattern})\b"
    )
    date_pattern = re.compile(r"(\d{2})\.(\d{2})\([A-Z]{3}\)")
    time_pattern = re.compile(r"\b(\d{1,2}:\d{2})\b")

    schedule_table = None
    for table in soup.find_all("table"):
        text = clean(table.get_text(" ", strip=True))
        if "DATE" in text and "GAME" in text and "LOCATION" in text:
            schedule_table = table
            break
    if schedule_table is None:
        raise RuntimeError("KBO 영문 일정표를 찾지 못했습니다.")

    current_game_date: date | None = None
    output: list[dict[str, Any]] = []

    for tr in schedule_table.find_all("tr"):
        cells = [
            clean(cell.get_text(" ", strip=True))
            for cell in tr.find_all(["th", "td"])
        ]
        if not cells:
            continue

        combined = " ".join(cells)
        date_match = date_pattern.search(combined)
        if date_match:
            month, day = map(int, date_match.groups())
            current_game_date = date(TODAY.year, month, day)

        game_match = game_pattern.search(combined)
        if not game_match or current_game_date is None:
            continue

        away, away_score, home_score, home = game_match.groups()
        if "HANWHA" not in (away, home):
            continue

        venue = ""
        for venue_code, venue_name in VENUE_KO.items():
            if venue_code in combined:
                venue = venue_name
                break

        time_match = time_pattern.search(combined)
        anchors = tr.find_all("a", href=True)
        game_url = ""
        for anchor in anchors:
            href = anchor.get("href", "")
            if "GameCenter" in href or "gameId=" in href:
                game_url = urljoin(URLS["schedule"], href)
                break

        completed = bool(away_score and home_score)
        if away == "HANWHA":
            hanwha_score = away_score
            opponent_score = home_score
            opponent = home
            home_away = "원정"
        else:
            hanwha_score = home_score
            opponent_score = away_score
            opponent = away
            home_away = "홈"

        result = ""
        if completed:
            hs = int(hanwha_score)
            os = int(opponent_score)
            result = "승" if hs > os else "패" if hs < os else "무"

        output.append(
            {
                "date": current_game_date.isoformat(),
                "time": time_match.group(1) if time_match else "",
                "opponent": TEAM_EN[opponent],
                "hanwha_score": hanwha_score,
                "opponent_score": opponent_score,
                "result": result,
                "completed": completed,
                "venue": venue,
                "home_away": home_away,
                "game_url": game_url,
            }
        )

    output.sort(key=lambda row: (row["date"], row.get("time", "")))
    return output


def extract_game_detail(game_url: str) -> dict[str, str]:
    if not game_url:
        return {}

    try:
        soup = get_soup(game_url)
    except Exception:
        return {}

    text = clean(soup.get_text(" ", strip=True))
    output: dict[str, str] = {}

    patterns = {
        "winning_hit": r"결승타\s*[:：]?\s*([가-힣A-Za-z]{2,15})",
        "winning_pitcher": r"승리투수\s*[:：]?\s*([가-힣A-Za-z]{2,15})",
        "save_pitcher": r"세이브\s*[:：]?\s*([가-힣A-Za-z]{2,15})",
    }

    blocked = {"없음", "기록", "선수", "투수", "타자", "경기"}
    for key, pattern in patterns.items():
        match = re.search(pattern, text)
        if match and match.group(1) not in blocked:
            output[key] = match.group(1)

    return output


def collect_news() -> list[dict[str, str]]:
    query = (
        '"한화 이글스" when:7d '
        "-한화솔루션 -한화에어로스페이스 -한화오션 -한화생명"
    )
    url = (
        "https://news.google.com/rss/search?q="
        + quote(query)
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )

    try:
        response = HTTP.get(url, timeout=(15, 45))
        response.raise_for_status()
        feed = feedparser.parse(response.content)
    except Exception:
        return []

    output: list[dict[str, str]] = []
    seen: set[str] = set()

    for entry in feed.entries[:30]:
        title = clean(entry.get("title"))
        source_data = entry.get("source") or {}
        source = (
            clean(source_data.get("title"))
            if isinstance(source_data, dict)
            else ""
        )
        if source:
            title = re.sub(
                rf"\s*[-–—]\s*{re.escape(source)}\s*$",
                "",
                title,
                flags=re.I,
            )
        key = re.sub(r"[^0-9a-z가-힣]", "", title.lower())
        if not key or key in seen:
            continue
        seen.add(key)

        published = clean(entry.get("published", entry.get("updated", "")))
        output.append(
            {
                "title": title,
                "source": source or "Google 뉴스",
                "published": published,
                "url": clean(entry.get("link")),
            }
        )
        if len(output) == 12:
            break
    return output


def choose_team_row(
    rows: list[dict[str, str]],
    team_name: str = "한화 이글스",
) -> dict[str, str]:
    return next((row for row in rows if row.get("team") == team_name), {})


def main() -> None:
    old = load_old()
    status: dict[str, str] = {}

    standings_soup = get_soup(URLS["standings"])
    rank_date, standings = parse_standings(standings_soup)
    status["KBO 순위"] = f"{rank_date or '기준일 미확인'} · 10개 구단"

    try:
        hitting_rows = parse_team_hitting(get_soup(URLS["team_hitting"]))
        team_hitting = choose_team_row(hitting_rows)
        status["팀 타격"] = "정상"
    except Exception as exc:
        team_hitting = old.get("team_hitting", {})
        status["팀 타격"] = f"기존 자료 유지 · {type(exc).__name__}"

    try:
        pitching_rows = parse_team_pitching(get_soup(URLS["team_pitching"]))
        team_pitching = choose_team_row(pitching_rows)
        status["팀 투수"] = "정상"
    except Exception as exc:
        team_pitching = old.get("team_pitching", {})
        status["팀 투수"] = f"기존 자료 유지 · {type(exc).__name__}"

    try:
        schedule = parse_schedule(get_soup(URLS["schedule"]))
        completed = [
            game for game in schedule
            if game["completed"] and date.fromisoformat(game["date"]) <= TODAY
        ]
        upcoming = [
            game for game in schedule
            if not game["completed"] and date.fromisoformat(game["date"]) >= TODAY
        ]
        recent_games = list(reversed(completed[-5:]))
        latest_game = completed[-1] if completed else old.get("latest_game", {})
        next_game = upcoming[0] if upcoming else old.get("next_game", {})
        if latest_game:
            latest_game = dict(latest_game)
            latest_game["detail"] = extract_game_detail(
                latest_game.get("game_url", "")
            )
        status["경기 일정"] = f"최근 {len(recent_games)}경기"
    except Exception as exc:
        latest_game = old.get("latest_game", {})
        recent_games = old.get("recent_games", [])
        next_game = old.get("next_game", {})
        status["경기 일정"] = f"기존 자료 유지 · {type(exc).__name__}"

    try:
        hitters = parse_hitter_leaders(get_soup(URLS["player_hitting"]))
        status["주요 타자"] = f"{len(hitters)}명"
    except Exception as exc:
        hitters = old.get("hitters", [])
        status["주요 타자"] = f"기존 자료 유지 · {type(exc).__name__}"

    try:
        pitchers = parse_pitcher_leaders(get_soup(URLS["player_pitching"]))
        status["주요 투수"] = f"{len(pitchers)}명"
    except Exception as exc:
        pitchers = old.get("pitchers", [])
        status["주요 투수"] = f"기존 자료 유지 · {type(exc).__name__}"

    news = collect_news() or old.get("news", [])
    status["뉴스"] = f"{len(news)}건"

    hanwha_standing = choose_team_row(standings)

    payload = {
        "updated_at": NOW.strftime("%Y-%m-%d %H:%M KST"),
        "rank_date": rank_date,
        "source_status": status,
        "latest_game": latest_game,
        "recent_games": recent_games,
        "next_game": next_game,
        "standings": standings,
        "hanwha_standing": hanwha_standing,
        "team_hitting": team_hitting,
        "team_pitching": team_pitching,
        "hitters": hitters,
        "pitchers": pitchers,
        "news": news,
        "sources": {
            "standings": URLS["standings"],
            "team_hitting": URLS["team_hitting"],
            "team_pitching": URLS["team_pitching"],
            "player_hitting": URLS["player_hitting"],
            "player_pitching": URLS["player_pitching"],
            "schedule": URLS["schedule"],
        },
    }

    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("=== 이글스데일리 자동수집 ===")
    for name, message in status.items():
        print(f"{name}: {message}")
    print(
        "RESULT "
        f"rank_date={rank_date} "
        f"rank={hanwha_standing.get('rank', '-')} "
        f"recent_games={len(recent_games)} "
        f"news={len(news)}"
    )


if __name__ == "__main__":
    main()
