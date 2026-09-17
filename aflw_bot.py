"""
AFLW Team Sheet Discord Alert Bot
==================================
Built against the real AFL Champion Data API:

    https://api.afl.com.au/cfs/afl/matchRosters/round/{roundId}?minimal=true

where roundId looks like "CD_R202626405" (round 5 of the 2026264 season code).

WHAT IT DOES
- Auto-tracks the current round (no manual weekly input needed)
- Polls that round's matchRosters
- Detects: (a) a team sheet going from unannounced -> announced,
           (b) any change to a team's ins/outs after it was announced
             (covers Friday extended-bench cuts AND genuine late changes)
- Posts a formatted Discord embed for each event

SETUP
- Set DISCORD_WEBHOOK_URL as an environment variable.
- First run: edit CURRENT_ROUND_HINT below to roughly the current round
  number (you only ever need to do this once, ever — see auto-advance
  logic in get_active_round()).
"""

import json
import os
import time
import logging
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aflw_bot")

DISCORD_WEBHOOK_URL = os.environ["DISCORD_WEBHOOK_URL"]
DISCORD_ROLE_ID = os.environ.get("DISCORD_ROLE_ID")  # optional — role to @ping, e.g. "123456789012345678"

SEASON_CODE = "2026264"          # the "S2026264" bit seen in competitionId
API_BASE = "https://api.afl.com.au/cfs/afl/matchRosters/round"

STATE_DIR = Path(__file__).parent
STATE_FILE = STATE_DIR / "lineup_state.json"          # last-seen ins/outs per team, for diffing
ROUND_POINTER_FILE = STATE_DIR / "round_pointer.json"  # {"round": 6}

CURRENT_ROUND_HINT = 6  # only used the very first time round_pointer.json doesn't exist

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Referer": "https://www.afl.com.au/",
    "Origin": "https://www.afl.com.au",
    "Accept": "*/*",
    "x-media-mis-token": os.environ.get("AFL_MIS_TOKEN", ""),
}

REASON_EMOJI = {
    "Injured": "🩹",
    "Omitted": "📋",
    "Managed": "🛡️",
    "Suspension": "🚫",
    None: "",
}


# -------------------------------------------------------------------------
# Round tracking — this is what makes it work every week with no input
# -------------------------------------------------------------------------

def round_id(round_number: int) -> str:
    return f"CD_R{SEASON_CODE}{round_number:02d}"


def fetch_round(round_number: int) -> Optional[list]:
    url = f"{API_BASE}/{round_id(round_number)}?minimal=true"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        log.exception("Failed fetching round %s", round_number)
        return None


def load_round_pointer() -> int:
    if ROUND_POINTER_FILE.exists():
        return json.loads(ROUND_POINTER_FILE.read_text())["round"]
    return CURRENT_ROUND_HINT


def save_round_pointer(round_number: int) -> None:
    ROUND_POINTER_FILE.write_text(json.dumps({"round": round_number}))


def get_active_round() -> tuple[int, list]:
    """
    Returns (round_number, matches) for whichever round is currently
    "live" — i.e. has at least one match that isn't CONCLUDED yet.
    If the stored round is fully finished, auto-advances the pointer
    to the next round. Self-heals across seasons starting/ending as
    long as round IDs stay sequential within a season.
    """
    pointer = load_round_pointer()
    data = fetch_round(pointer)

    if data:
        all_concluded = all(m["match"]["status"] == "CONCLUDED" for m in data)
        if not all_concluded:
            save_round_pointer(pointer)
            return pointer, data
        # current round is done — try advancing
        next_data = fetch_round(pointer + 1)
        if next_data:
            save_round_pointer(pointer + 1)
            return pointer + 1, next_data
        # next round not published yet (e.g. mid-week before fixture drops) —
        # keep serving the concluded round; diffing will just find no changes
        save_round_pointer(pointer)
        return pointer, data

    # stored round returned nothing (e.g. season rollover) — try stepping back
    prev_data = fetch_round(pointer - 1)
    if prev_data:
        save_round_pointer(pointer - 1)
        return pointer - 1, prev_data

    save_round_pointer(pointer)
    log.warning("Could not resolve an active round near pointer %s", pointer)
    return pointer, []


# -------------------------------------------------------------------------
# State + diffing
# -------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def player_full_name(p: dict) -> str:
    return f"{p['playerName']['givenName']} {p['playerName']['surname']}"


def format_changes_block(ins: list, outs: list) -> str:
    lines = []
    for i in ins:
        lines.append(f"🟢 **IN:** {player_full_name(i['player'])}")
    for o in outs:
        reason = o.get("reason")
        tag = f" _{reason}_" if reason else ""
        emoji = REASON_EMOJI.get(reason, "")
        lines.append(f"🔴 **OUT:** {player_full_name(o['player'])}{tag} {emoji}")
    return "\n".join(lines) if lines else "No changes."


def format_full_lineup(positions: list) -> str:
    # Group roughly the way the site does: backs / mids / forwards / ruck / int / emerg
    # Champion Data's `position` codes: BPL/FB/HBFL/CHB/HBFR = backs, WL/C/WR = mids,
    # HFFL/CHF/HFFR/FF/FPR = forwards, RK/RR/R = ruck, INT = interchange, EMERG = emergency
    groups = {
        "Backs": ["BPL", "FB", "HBFL", "CHB", "HBFR"],
        "Midfield": ["WL", "C", "WR"],
        "Forwards": ["HFFL", "CHF", "HFFR", "FF", "FPR"],
        "Ruck": ["RK", "RR", "R"],
        "Interchange": ["INT"],
        "Emergencies": ["EMERG"],
    }
    by_pos = {p["position"]: player_full_name(p["player"]) for p in positions}
    lines = []
    for label, codes in groups.items():
        names = [by_pos[c] for c in codes if c in by_pos]
        # INT/EMERG can have repeats of the same code — handle via list comprehension over raw list
        if label in ("Interchange", "Emergencies"):
            code = codes[0]
            names = [player_full_name(p["player"]) for p in positions if p["position"] == code]
        if names:
            lines.append(f"**{label}:** {', '.join(names)}")
    return "\n".join(lines)


def diff_and_alert(round_number: int, matches: list) -> None:
    state = load_state()
    any_change = False

    for entry in matches:
        match_info = entry["match"]
        roster = entry["matchRoster"]
        match_name = match_info["name"]
        match_id = match_info["matchId"]

        newly_announced = []   # list of (team_name, team_dict) that just went FINAL this poll
        late_changed = []      # list of (team_name, new_ins, new_outs) with fresh changes

        for side in ("homeTeam", "awayTeam"):
            team = roster.get(side)
            if not team:
                continue

            team_name = team["teamName"]["teamName"]
            key = f"{match_id}:{team['teamId']}"

            prev = state.get(key)
            current_status = team["teamStatus"]
            current_ins = team.get("ins", [])
            current_outs = team.get("outs", [])

            new_record = {
                "teamStatus": current_status,
                "ins": current_ins,
                "outs": current_outs,
            }

            if current_status != "FINAL_TEAM":
                state[key] = new_record
                continue

            if prev is None or prev.get("teamStatus") != "FINAL_TEAM":
                newly_announced.append((team_name, team))
            else:
                prev_in_ids = {i["player"]["playerId"] for i in prev.get("ins", [])}
                prev_out_ids = {o["player"]["playerId"] for o in prev.get("outs", [])}

                new_ins = [i for i in current_ins if i["player"]["playerId"] not in prev_in_ids]
                new_outs = [o for o in current_outs if o["player"]["playerId"] not in prev_out_ids]

                if new_ins or new_outs:
                    late_changed.append((team_name, new_ins, new_outs))

            state[key] = new_record

        # --- one combined embed per match for announcements ---
        if newly_announced:
            sections = []
            for team_name, team in newly_announced:
                sections.append(
                    f"__**{team_name}**__\n"
                    f"{format_changes_block(team.get('ins', []), team.get('outs', []))}\n\n"
                    f"{format_full_lineup(team.get('positions', []))}"
                )
            send_discord_alert(
                title=f"📣 Team sheet{'s' if len(newly_announced) > 1 else ''} announced — {match_name}",
                description="\n\n".join(sections),
                color=0x2ECC71,
            )
            any_change = True

        # --- one combined embed per match for late changes ---
        if late_changed:
            sections = []
            for team_name, new_ins, new_outs in late_changed:
                sections.append(f"__**{team_name}**__\n{format_changes_block(new_ins, new_outs)}")
            send_discord_alert(
                title=f"🔄 Late change{'s' if len(late_changed) > 1 else ''} — {match_name}",
                description="\n\n".join(sections),
                color=0xE67E22,
            )
            any_change = True

    save_state(state)
    if any_change:
        log.info("Round %s: %s change(s) posted.", round_number, "some")
    else:
        log.info("Round %s: no changes.", round_number)


# -------------------------------------------------------------------------
# Discord posting
# -------------------------------------------------------------------------

def send_discord_alert(title: str, description: str, color: int = 0x00539F) -> None:
    # Discord embed descriptions cap at 4096 chars; trim defensively.
    if len(description) > 4000:
        description = description[:3990] + "\n… (truncated)"

    payload = {"embeds": [{"title": title, "description": description, "color": color}]}

    if DISCORD_ROLE_ID:
        # Role mentions only ping if they're in the top-level "content" field
        # (mentions inside embeds are NOT pinged by Discord), and only if
        # allowed_mentions explicitly permits "roles".
        payload["content"] = f"<@&{DISCORD_ROLE_ID}>"
        payload["allowed_mentions"] = {"parse": ["roles"]}

    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    if resp.status_code >= 300:
        log.error("Discord post failed: %s %s", resp.status_code, resp.text)
    else:
        log.info("Posted: %s", title)


# -------------------------------------------------------------------------
# Main poll cycle
# -------------------------------------------------------------------------

def run_once() -> None:
    round_number, matches = get_active_round()
    if not matches:
        log.warning("No match data available this cycle (round %s).", round_number)
        return
    diff_and_alert(round_number, matches)


if __name__ == "__main__":
    import sys

    if "--once" in sys.argv:
        # Single-shot mode, used by GitHub Actions (or any cron-style scheduler)
        # that starts the process, runs one poll, and exits.
        try:
            run_once()
        except Exception:
            log.exception("Error during single poll")
    else:
        # Long-running mode, used when you run this directly on your own machine/server.
        POLL_SECONDS = 120
        while True:
            try:
                run_once()
            except Exception:
                log.exception("Error during poll cycle")
            time.sleep(POLL_SECONDS)
