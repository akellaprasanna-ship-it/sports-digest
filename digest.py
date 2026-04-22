"""
Daily Sports Digest → Telegram
Runs once per day, fetches results/news for enabled leagues,
asks Claude to synthesize a brief, and sends it to Telegram.
"""

import os
import sys
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
import feedparser
from anthropic import Anthropic

# ---------------------------------------------------------------------------
# CONFIG — edit this block to change what the digest covers
# ---------------------------------------------------------------------------

LEAGUES = {
    "ipl": {
        "enabled": True,
        "topics": ["results", "standings", "top_performers", "injuries", "storylines"],
    },
    "nba": {
        "enabled": True,
        "topics": ["playoff_results", "injuries", "upcoming", "storylines", "coaching_changes"],
    },
    "epl": {
        "enabled": True,
        "topics": ["results", "table", "transfers", "injuries", "storylines"],
    },
    "ucl": {
        "enabled": True,
        "topics": ["results", "fixtures", "storylines", "injuries"],
    },
    "laliga": {
        "enabled": True,
        "topics": ["results", "table", "storylines", "transfers", "injuries"],
    },
    "bundesliga": {
        "enabled": True,
        "topics": ["results", "table", "storylines", "transfers", "injuries"],
    },
    "seriea": {
        "enabled": True,
        "topics": ["results", "table", "storylines", "transfers", "injuries"],
    },
    "f1": {
        "enabled": True,
        "topics": ["race_results", "qualifying", "rule_changes", "contracts",
                   "driver_moves", "team_news", "storylines"],
        # F1 is only included when a session is within the race-weekend window
        # (Fri of race week through Mon after the race). See fetch_f1().
    },
}

CLAUDE_MODEL = "claude-sonnet-4-6"  # Sonnet chosen for stricter grounding to provided context
TARGET_WORD_COUNT = 450             # Raised from 300 to fit 8 possible leagues
DRY_RUN = False                      # If True, print to stdout instead of sending to Telegram

# RSS feeds for news context
# For European football leagues, Guardian is used in addition to/instead of BBC because
# BBC's feed is heavily EPL-skewed. Guardian covers Premier League, European and World
# football more evenly. For F1, Autosport is far richer on off-track news (regulations,
# contracts, testing) than ESPN's race-centric feed.
RSS_FEEDS = {
    "ipl": "https://www.espncricinfo.com/rss/content/story/feeds/6.xml",
    "nba": "https://www.espn.com/espn/rss/nba/news",
    "epl": "https://feeds.bbci.co.uk/sport/football/rss.xml",
    "ucl": "https://www.theguardian.com/football/rss",
    "laliga": "https://www.theguardian.com/football/rss",
    "bundesliga": "https://www.theguardian.com/football/rss",
    "seriea": "https://www.theguardian.com/football/rss",
    "f1": "https://www.autosport.com/rss/feed/f1/news/",
}

# How many headlines to pass to Claude per league (keeps prompt tight)
MAX_HEADLINES_PER_LEAGUE = 8

# Only include RSS items published within this many hours.
# Prevents week-old transfer rumors from polluting today's brief.
RSS_FRESHNESS_HOURS = 36

# Keyword filters for leagues that share a general feed (Guardian football).
# An item is kept only if its title or summary contains at least one keyword (case-insensitive).
# If a league has no entry here, no filtering is applied.
RSS_KEYWORD_FILTERS = {
    "laliga": ["la liga", "laliga", "barcelona", "real madrid", "atletico", "atlético",
               "sevilla", "valencia", "villarreal", "athletic bilbao", "real sociedad",
               "spanish", "spain"],
    "bundesliga": ["bundesliga", "bayern", "dortmund", "leverkusen", "leipzig",
                   "borussia", "eintracht", "stuttgart", "wolfsburg", "german",
                   "germany"],
    "seriea": ["serie a", "inter", "milan", "ac milan", "juventus", "napoli", "roma",
               "lazio", "atalanta", "fiorentina", "italian", "italy"],
    "ucl": ["champions league", "ucl", "uefa"],
}

# API-Sports football league IDs (stable, from their docs)
FOOTBALL_LEAGUES = {
    "epl":        {"id": 39,  "label": "EPL",           "season": 2025},
    "ucl":        {"id": 2,   "label": "UCL",           "season": 2025},
    "laliga":     {"id": 140, "label": "La Liga",       "season": 2025},
    "bundesliga": {"id": 78,  "label": "Bundesliga",    "season": 2025},
    "seriea":     {"id": 135, "label": "Serie A",       "season": 2025},
}

# F1 race-weekend window: include section if a session falls in this many days
# before/after today. Typical race week: practice Fri, quali Sat, race Sun.
# Window of -1 to +3 days covers Mon-after-race (recap) through Thu-before (build-up).
F1_WINDOW_DAYS_AHEAD = 3
F1_WINDOW_DAYS_BEHIND = 1

# ---------------------------------------------------------------------------
# SETUP
# ---------------------------------------------------------------------------

# Load .env from the same directory as this script, so the script works correctly
# regardless of the working directory the user runs it from (matters for GitHub Actions
# and any cron-style execution where CWD may not be the script's folder).
_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CRICKET_DATA_API_KEY = os.getenv("CRICKET_DATA_API_KEY")
API_SPORTS_KEY = os.getenv("API_SPORTS_KEY")

# Fail fast if any key is missing — better than a confusing 401 later
required = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
    "CRICKET_DATA_API_KEY": CRICKET_DATA_API_KEY,
    "API_SPORTS_KEY": API_SPORTS_KEY,
}
missing = [k for k, v in required.items() if not v]
if missing:
    sys.exit(f"Missing env vars: {', '.join(missing)}")

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Request counter per API — prints at the end so you can see budget burn
request_counts = {"cricketdata": 0, "api_sports": 0, "rss": 0}


# ---------------------------------------------------------------------------
# FETCHERS — each returns a dict of raw-ish data, or {"error": "..."} on fail
# Design: one league failing should never kill the digest.
# ---------------------------------------------------------------------------

def _safe_get(url, headers=None, params=None, timeout=15, counter_key=None):
    """Wrapper that returns parsed JSON or raises; caller handles errors."""
    if counter_key:
        request_counts[counter_key] = request_counts.get(counter_key, 0) + 1
    r = requests.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_ipl():
    """CricketData.org — /currentMatches returns live and recently-completed matches.
    We only pull completed matches here (today's upcoming fixture is not reliably
    available on the free tier until it starts). The brief covers yesterday's result;
    today's fixture, if live by 10 AM, will appear here; otherwise it's skipped."""
    try:
        base = "https://api.cricapi.com/v1"
        current = _safe_get(
            f"{base}/currentMatches",
            params={"apikey": CRICKET_DATA_API_KEY, "offset": 0},
            counter_key="cricketdata",
        )
        all_matches = current.get("data", []) or []

        # Filter to IPL matches
        ipl_all = [
            m for m in all_matches
            if "indian premier league" in (m.get("name", "") + m.get("series", "")).lower()
            or "ipl" in m.get("name", "").lower()
        ]

        # Split by IST calendar day
        now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        today_ist = now_ist.date()
        yesterday_ist = (now_ist - timedelta(days=1)).date()

        yesterday_matches = []
        today_matches = []
        skipped_other = 0

        for m in ipl_all:
            dt_str = m.get("dateTimeGMT") or m.get("date")
            if not dt_str:
                skipped_other += 1
                continue
            try:
                dt_utc = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                if dt_utc.tzinfo is None:
                    dt_utc = dt_utc.replace(tzinfo=timezone.utc)
                dt_ist = dt_utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
                match_date = dt_ist.date()
            except (ValueError, AttributeError):
                skipped_other += 1
                continue

            if match_date == yesterday_ist:
                yesterday_matches.append(m)
            elif match_date == today_ist:
                today_matches.append(m)
            else:
                skipped_other += 1

        if skipped_other:
            print(f"  [ipl] filtered out {skipped_other} IPL matches outside yesterday/today IST window")
        print(f"  [ipl] found {len(yesterday_matches)} yesterday + {len(today_matches)} today")

        return {
            "yesterday": yesterday_matches,
            "today": today_matches,
            "fetched_at_ist": now_ist.isoformat(),
        }
    except Exception as e:
        return {"error": f"IPL fetch failed: {e}"}


def fetch_nba():
    """API-Sports NBA — yesterday's games + standings."""
    try:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        headers = {"x-apisports-key": API_SPORTS_KEY}

        yest_games = _safe_get(
            "https://v2.nba.api-sports.io/games",
            headers=headers,
            params={"date": yesterday},
            counter_key="api_sports",
        )
        today_games = _safe_get(
            "https://v2.nba.api-sports.io/games",
            headers=headers,
            params={"date": today},
            counter_key="api_sports",
        )
        return {
            "yesterday": yest_games.get("response", [])[:12],
            "today": today_games.get("response", [])[:12],
        }
    except Exception as e:
        return {"error": f"NBA fetch failed: {e}"}


def fetch_football(league_id, label, season=2025):
    """API-Sports Football — yesterday's fixtures + today's fixtures for given league.
    Season = starting year of the European season (e.g., 2025 for the 2025/26 season)."""
    try:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        headers = {"x-apisports-key": API_SPORTS_KEY}

        yest_fixtures = _safe_get(
            "https://v3.football.api-sports.io/fixtures",
            headers=headers,
            params={"league": league_id, "season": season, "date": yesterday},
            counter_key="api_sports",
        )
        today_fixtures = _safe_get(
            "https://v3.football.api-sports.io/fixtures",
            headers=headers,
            params={"league": league_id, "season": season, "date": today},
            counter_key="api_sports",
        )
        return {
            "label": label,
            "yesterday": yest_fixtures.get("response", []),
            "today": today_fixtures.get("response", []),
        }
    except Exception as e:
        return {"error": f"{label} fetch failed: {e}"}


def fetch_f1():
    """API-Sports F1 — returns data only when a race session is within the weekend window.
    Off-weekend days return {"skip": True, "reason": "..."} to signal the orchestrator
    to omit the F1 section entirely.

    Costs 1 request on non-race days (just the season schedule check) and 2 on race days
    (schedule + recent races)."""
    try:
        headers = {"x-apisports-key": API_SPORTS_KEY}
        season = datetime.now(timezone.utc).year

        # Get the full season schedule first. This endpoint returns all races with dates.
        races_resp = _safe_get(
            "https://v1.formula-1.api-sports.io/races",
            headers=headers,
            params={"season": season, "type": "Race"},
            counter_key="api_sports",
        )
        races = races_resp.get("response", [])

        if not races:
            return {"skip": True, "reason": "no races in season schedule"}

        # Find the closest race to today
        today_utc = datetime.now(timezone.utc).date()
        ahead_cutoff = today_utc + timedelta(days=F1_WINDOW_DAYS_AHEAD)
        behind_cutoff = today_utc - timedelta(days=F1_WINDOW_DAYS_BEHIND)

        in_window = []
        next_race = None
        last_race = None
        for race in races:
            date_str = race.get("date", "")
            if not date_str:
                continue
            try:
                race_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                race_date = race_dt.date()
            except (ValueError, AttributeError):
                continue

            if behind_cutoff <= race_date <= ahead_cutoff:
                in_window.append(race)
            if race_date >= today_utc and (next_race is None or race_date < next_race["_date"]):
                next_race = {**race, "_date": race_date}
            if race_date <= today_utc and (last_race is None or race_date > last_race["_date"]):
                last_race = {**race, "_date": race_date}

        if not in_window:
            next_info = f"next race on {next_race['_date'].isoformat()}" if next_race else "no upcoming race"
            return {"skip": True, "reason": f"no F1 session in window; {next_info}"}

        # We're in a race weekend window — fetch last race results for context
        race_in_window = in_window[0]
        race_id = race_in_window.get("id")
        ranking = None
        if race_id and race_in_window.get("status", "").lower() in ("completed", "finished"):
            try:
                ranking_resp = _safe_get(
                    "https://v1.formula-1.api-sports.io/rankings/races",
                    headers=headers,
                    params={"race": race_id},
                    counter_key="api_sports",
                )
                ranking = ranking_resp.get("response", [])[:10]  # top 10 finishers
            except Exception as e:
                ranking = {"error": f"ranking fetch failed: {e}"}

        return {
            "in_window_race": race_in_window,
            "results_top10": ranking,
            "next_race": {k: v for k, v in (next_race or {}).items() if k != "_date"},
        }
    except Exception as e:
        return {"error": f"F1 fetch failed: {e}"}


def fetch_rss(url, league_key):
    """Parse RSS feed, return recent headlines + short summaries.
    Filters by freshness (RSS_FRESHNESS_HOURS) and, for leagues using a general feed
    (e.g., Guardian football for La Liga), by keyword match against the headline/summary.
    Keyword filter prevents EPL-dominant general feeds from bleeding into other league sections."""
    try:
        request_counts["rss"] += 1
        feed = feedparser.parse(url)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=RSS_FRESHNESS_HOURS)
        keywords = RSS_KEYWORD_FILTERS.get(league_key)
        items = []
        skipped_stale = 0
        skipped_off_topic = 0
        for entry in feed.entries:
            # Freshness check — use parsed date if available; otherwise include (be noisy, not silent)
            pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub_struct:
                pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
                if pub_dt < cutoff:
                    skipped_stale += 1
                    continue

            title = entry.get("title", "")
            summary = entry.get("summary", "")

            # Keyword filter — if the league has one, require at least one match
            if keywords:
                haystack = (title + " " + summary).lower()
                if not any(kw in haystack for kw in keywords):
                    skipped_off_topic += 1
                    continue

            items.append({
                "title": title,
                "summary": summary[:300],
                "published": entry.get("published", ""),
            })
            if len(items) >= MAX_HEADLINES_PER_LEAGUE:
                break
        if skipped_stale:
            print(f"  [{league_key}] filtered out {skipped_stale} stale headlines "
                  f"(older than {RSS_FRESHNESS_HOURS}h)")
        if skipped_off_topic:
            print(f"  [{league_key}] filtered out {skipped_off_topic} off-topic headlines "
                  f"(no keyword match)")
        return items
    except Exception as e:
        return [{"error": f"RSS {league_key} failed: {e}"}]


# ---------------------------------------------------------------------------
# ORCHESTRATION
# ---------------------------------------------------------------------------

def gather_all():
    """Fetch data and news for every enabled league. Returns dict keyed by league.
    Leagues that return {"skip": True} are omitted entirely (F1 on non-race weeks)."""
    results = {}

    if LEAGUES["ipl"]["enabled"]:
        results["ipl"] = {
            "data": fetch_ipl(),
            "news": fetch_rss(RSS_FEEDS["ipl"], "ipl"),
            "topics": LEAGUES["ipl"]["topics"],
        }

    if LEAGUES["nba"]["enabled"]:
        results["nba"] = {
            "data": fetch_nba(),
            "news": fetch_rss(RSS_FEEDS["nba"], "nba"),
            "topics": LEAGUES["nba"]["topics"],
        }

    # Football leagues — all use same API-Football endpoint, differ only by league ID
    for key, info in FOOTBALL_LEAGUES.items():
        if not LEAGUES.get(key, {}).get("enabled"):
            continue
        results[key] = {
            "data": fetch_football(info["id"], info["label"], info["season"]),
            "news": fetch_rss(RSS_FEEDS[key], key),
            "topics": LEAGUES[key]["topics"],
        }

    # F1 is conditional — only include on race weekends
    if LEAGUES.get("f1", {}).get("enabled"):
        f1_data = fetch_f1()
        if f1_data.get("skip"):
            print(f"  [f1] skipping section: {f1_data.get('reason', 'off-weekend')}")
        else:
            results["f1"] = {
                "data": f1_data,
                "news": fetch_rss(RSS_FEEDS["f1"], "f1"),
                "topics": LEAGUES["f1"]["topics"],
            }

    return results


# ---------------------------------------------------------------------------
# CLAUDE — synthesize raw data into a brief
# ---------------------------------------------------------------------------

DIGEST_PROMPT = """You are writing a short daily sports brief for a fan in India (IST timezone).
The brief is delivered to Telegram at 10 AM IST, so it should cover overnight results
and the day ahead.

Target length: ~{word_count} words total. Be scannable, not comprehensive.

Leagues and what the reader cares about:
{topic_summary}

===========================================================================
ACCURACY RULES — these are strict. A wrong fact is worse than a missing one.
===========================================================================

You receive two kinds of input below: DATA (from sports APIs) and NEWS (from RSS feeds).
Treat them differently:

1. MATCH RESULTS, SCORES, STANDINGS, LINEUPS, FIXTURES must come ONLY from the DATA
   section. If a score or team is not in DATA, do not report it.

2. STORYLINES, INJURIES, TRANSFERS, MANAGER QUOTES, CONTROVERSIES come ONLY from
   the NEWS section. When you include one, phrase it to signal it's reported news,
   not a verified result. Use phrases like:
     - "Reports suggest..."
     - "According to [source]..."
     - "Per BBC Sport..."
     - "Headlines: ..."
   Do NOT present news-derived claims as if they were match facts.

3. DO NOT STITCH ACROSS SOURCES. If DATA says "Chelsea lost to Brighton" and NEWS
   has a headline from last week about "Chelsea's winless run," do NOT combine these
   into a single narrative. Report the match result from DATA; if the winless-run
   headline is still fresh and relevant, report it separately as news.

4. NO INVENTED ENTITIES. Every proper noun you mention — every player name, team name,
   coach name, manager name, stadium, city — MUST appear somewhere in the DATA or NEWS
   input provided below. Before writing any name, mentally verify it is in the input.
   If you cannot find it in the input, DO NOT USE IT. This rule has no exceptions.
   You do not have knowledge of sports events that is more recent than this input —
   do not rely on your training data for any name, result, or event.

5. IF A TOPIC HAS NO INPUT, SKIP THE TOPIC. The reader's "topics of interest" (injuries,
   transfers, storylines, etc.) are PRIORITIES, not REQUIREMENTS. If there are no transfer
   headlines for EPL in the NEWS section, write nothing about transfers. Do NOT fill the
   space with generic or invented items. A short brief is better than a padded one.

6. IF DATA SAYS "error", write one line acknowledging data was unavailable for that
   league. You may still lean on NEWS headlines for a single sentence of context.

7. STAY IN YOUR LEAGUE. Each league's NEWS items are provided under that league's key.
   News filed under EPL is for the EPL section. Do not move news between leagues.
   Do not include items about other sports or leagues that aren't among the four covered.

===========================================================================
FORMAT
===========================================================================

- Telegram MarkdownV2-safe: *bold* for league names and team names. Keep formatting minimal.
- One short section per league that has content in the INPUT. Possible section headers
  (use only the ones present in INPUT, in this order):
  *IPL*, *NBA*, *EPL*, *La Liga*, *Bundesliga*, *Serie A*, *UCL*, *F1*
- OMIT A LEAGUE'S SECTION ENTIRELY IF IT HAS NOTHING TO REPORT. "Nothing to report" means:
  (a) DATA has no matches in yesterday OR today, AND
  (b) NEWS has no items specifically about that league.
  In that case, do NOT write the section header, do NOT write a placeholder line like
  "no matches" or "no news". Just skip it. The digest is allowed to cover only 2-3 leagues
  on a quiet day.
- If a league has partial content (e.g., no matches but one fresh storyline), include it
  and lead with the storyline.
- If DATA for a league returns an error, mention it in one line only if there's no news
  to report instead; otherwise skip the section.
- Lead each section with the most interesting verified fact (usually a result or key stat).
- Neutral tone. No hype. No emojis.

===========================================================================
INPUT
===========================================================================

=== DATA (authoritative — use for scores, standings, fixtures) ===
{data_json}

=== NEWS (recent headlines — use for context, mark as reported) ===
{news_json}

Write only the brief. No preamble, no sign-off."""


def build_topic_summary(results):
    lines = []
    labels = {
        "ipl": "IPL (cricket)",
        "nba": "NBA Playoffs",
        "epl": "EPL (football)",
        "ucl": "UEFA Champions League",
        "laliga": "La Liga (football)",
        "bundesliga": "Bundesliga (football)",
        "seriea": "Serie A (football)",
        "f1": "Formula 1",
    }
    for key, payload in results.items():
        topics = ", ".join(payload["topics"])
        lines.append(f"- {labels.get(key, key)}: focus on {topics}")
    return "\n".join(lines)


def generate_brief(results):
    data_only = {k: v["data"] for k, v in results.items()}
    news_only = {k: v["news"] for k, v in results.items()}

    prompt = DIGEST_PROMPT.format(
        word_count=TARGET_WORD_COUNT,
        topic_summary=build_topic_summary(results),
        data_json=json.dumps(data_only, indent=2, default=str)[:40000],  # safety trim
        news_json=json.dumps(news_only, indent=2, default=str)[:15000],
    )

    resp = anthropic_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ---------------------------------------------------------------------------
# TELEGRAM
# ---------------------------------------------------------------------------

def escape_markdown_v2(text):
    """Telegram MarkdownV2 requires escaping these characters outside formatting entities.
    We take a conservative approach: escape only the characters that commonly break messages,
    assuming Claude produced well-formed *bold* markers."""
    # These need escaping when not part of markdown syntax
    escape_chars = r"_[]()~`>#+-=|{}.!"
    out = []
    in_bold = False
    for ch in text:
        if ch == "*":
            in_bold = not in_bold
            out.append(ch)
        elif ch in escape_chars and not in_bold:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def send_to_telegram(text):
    """Send via Telegram Bot API. Falls back to plain text if MarkdownV2 parse fails."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    # First try: MarkdownV2 with escaping
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": escape_markdown_v2(text),
        "parse_mode": "MarkdownV2",
    }
    r = requests.post(url, json=payload, timeout=15)
    if r.status_code == 200:
        return True

    # Fallback: plain text, no parse mode
    print(f"MarkdownV2 send failed ({r.status_code}): {r.text[:300]}")
    print("Falling back to plain text...")
    payload_plain = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    r2 = requests.post(url, json=payload_plain, timeout=15)
    if r2.status_code == 200:
        return True
    print(f"Plain text send also failed ({r2.status_code}): {r2.text[:300]}")
    return False


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print(f"[{datetime.now().isoformat()}] Starting digest...")

    print("Gathering data...")
    results = gather_all()

    print("Request counts so far:", request_counts)

    print("Generating brief with Claude...")
    brief = generate_brief(results)

    print("\n--- DIGEST ---\n")
    print(brief)
    print("\n--------------\n")

    if DRY_RUN:
        print("DRY_RUN=True — skipping Telegram send.")
        return

    print("Sending to Telegram...")
    ok = send_to_telegram(brief)
    print("Sent!" if ok else "Send failed.")
    print("Final request counts:", request_counts)


if __name__ == "__main__":
    main()
