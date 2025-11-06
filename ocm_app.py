from flask import Flask, request, jsonify
import datetime
import re
import os
import requests
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dateutil import parser as dtparse  # pip install python-dateutil

app = Flask(__name__)

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# --- Configuration ---
OCM_API_BASE = os.getenv("OCM_API_BASE", "https://oncallmanager.ibm.com")
TEAMS_CONFIG_PATH = "config/teams.json"

# Load team configuration once
with open(TEAMS_CONFIG_PATH) as f:
    TEAMS = json.load(f)


# --- Utility Functions ---
def find_team_entry(group=None, team_key=None, env_prefix=None):
    """Resolve a team entry from config."""
    if group:
        for name, info in TEAMS.items():
            if group in info.get("groups", []):
                return name, info

    if team_key and team_key in TEAMS:
        return team_key, TEAMS[team_key]

    if env_prefix:
        for name, info in TEAMS.items():
            if info.get("env_prefix") == env_prefix:
                return name, info

    return None, None


def get_team_credentials(team_info):
    """Return username, password, subscription_id from env vars."""
    env_prefix = team_info["env_prefix"]
    username = os.getenv(f"{env_prefix}_OCM_USERNAME")
    password = os.getenv(f"{env_prefix}_OCM_PASSWORD")

    if not username or not password:
        logger.error(f"Missing credentials for env_prefix={env_prefix}")
        return None, None, None

    subscription_id = username.split("/")[0]
    return username, password, subscription_id


def fetch_window(subscription_id, start_str, end_str, username, password, group_hint=None):
    """Fetch a wide time window of schedules."""
    url = f"{OCM_API_BASE}/api/ocdm/v1/{subscription_id}/crosssubscriptionschedules"
    params = {"from": start_str, "to": end_str}
    logger.info(f"GET {url} from={start_str} to={end_str} (group_hint={group_hint})")

    try:
        resp = requests.get(url, auth=(username, password), params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if not data:
                return []
            if isinstance(data, list):
                logger.info(f"OCM returned {len(data)} buckets")
                return data
            return []
        else:
            logger.warning(f"HTTP {resp.status_code}: {resp.text[:150]}")
            return []
    except Exception as e:
        logger.error(f"fetch_window failed: {e}")
        return []


def normalize_entries(raw_payload):
    """Flatten OCM payload structure."""
    out = []
    if not isinstance(raw_payload, list):
        return out

    for bucket in raw_payload:
        bucket_group = bucket.get("group") or bucket.get("GroupId")
        details = bucket.get("schedulingDetails", [])

        for det in details:
            group_id = det.get("GroupId") or bucket_group
            date = det.get("Date")
            tz = det.get("Timezone")

            for shift in det.get("Shifts", []):
                out.append({
                    "GroupId": group_id,
                    "Date": date,
                    "Timezone": tz,
                    "StartTime": shift.get("StartTime"),
                    "EndTime": shift.get("EndTime"),
                    "Users": shift.get("UserDetails", []) or []
                })
    return out


def overlaps_day(start_iso, end_iso, day_start_utc, day_end_utc):
    """Return True if the schedule overlaps the target day window."""
    try:
        start = dtparse.isoparse(start_iso)
        end = dtparse.isoparse(end_iso)
        return (start < day_end_utc) and (end > day_start_utc)
    except Exception as e:
        logger.warning(f"Failed to parse times: {e}")
        return False


def pick_display_users(users):
    """Simplify user info for display."""
    out = []
    for u in users:
        name = u.get("FullName") or u.get("UserId") or ""
        out.append({
            "name": name,
            "userId": u.get("UserId") or "",
            "mobile": u.get("MobileNumber") or ""
        })
    return out


# --- Routes ---
@app.route("/getSchedule", methods=["POST"])
def get_schedule():
    """Fetch on-call schedule for a specific date."""
    data = request.get_json(force=True) or {}

    group = data.get("groupPrefix")
    team_key = data.get("teamKey")
    env_prefix = data.get("envPrefix")
    date_str = request.args.get("date")

    if date_str:
        date_str = date_str.replace("-", "")
        if not re.fullmatch(r"^\d{8}$", date_str):
            return jsonify({"error": "Invalid date format. Use YYYYMMDD or YYYY-MM-DD."}), 400
    else:
        date_str = datetime.datetime.utcnow().strftime("%Y%m%d")

    try:
        target_date = datetime.datetime.strptime(date_str, "%Y%m%d")
    except ValueError:
        return jsonify({"error": "Invalid date value."}), 400

    from datetime import timezone
    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    day_end = day_start + datetime.timedelta(days=1)
    query_start = (day_start - datetime.timedelta(days=30)).strftime("%Y%m%d")
    query_end = (day_end + datetime.timedelta(days=30)).strftime("%Y%m%d")

    team_name, team_info = find_team_entry(group=group, team_key=team_key, env_prefix=env_prefix)
    if not team_info:
        return jsonify({
            "error": "No team configuration matched your request.",
            "hint": {
                "groupPrefix": group,
                "teamKey": team_key,
                "envPrefix": env_prefix,
                "valid_teams": list(TEAMS.keys())
            }
        }), 500

    username, password, subscription_id = get_team_credentials(team_info)
    if not username or not password:
        return jsonify({"error": "Missing credentials"}), 500

    groups_to_query = [group] if group else team_info.get("groups", [])
    results = []

    with ThreadPoolExecutor(max_workers=min(len(groups_to_query), 5)) as executor:
        futures = {
            executor.submit(fetch_window, subscription_id, query_start, query_end, username, password, grp): grp
            for grp in groups_to_query
        }

        for future in as_completed(futures):
            grp = futures[future]
            raw = future.result()
            flat = normalize_entries(raw)

            for row in flat:
                if (
                    row.get("GroupId") == grp and
                    overlaps_day(row.get("StartTime", ""), row.get("EndTime", ""), day_start, day_end)
                ):
                    results.append({
                        "GroupId": row["GroupId"],
                        "Date": date_str,
                        "Timezone": row.get("Timezone"),
                        "StartTime": row.get("StartTime"),
                        "EndTime": row.get("EndTime"),
                        "Users": pick_display_users(row.get("Users", []))
                    })

    if not results:
        return jsonify({"message": f"No on-call assignments found for {groups_to_query} on {date_str}"}), 404

    summary_lines = []
    for entry in results:
        try:
            user = entry["Users"][0]["name"] if entry["Users"] else "Unknown"
            start = entry["StartTime"][11:16] if entry.get("StartTime") else "?"
            end = entry["EndTime"][11:16] if entry.get("EndTime") else "?"
            summary_lines.append(f"- {entry['GroupId']}: {user} ({start} - {end})")
        except Exception as e:
            logger.warning(f"Summary formatting failed: {e}")

    summary_text = f"On-call schedule for {date_str}:\n" + "\n".join(summary_lines)
    return jsonify({"status": 200, "body": results, "summary": summary_text}), 200


@app.route("/getNextOnCall", methods=["POST"])
def get_next_on_call():
    """Find the next time a user (email) is on call across all configured teams."""
    data = request.get_json(force=True) or {}
    user_id = data.get("userId")
    if not user_id:
        return jsonify({"error": "Missing userId (email address)"}), 400

    days_ahead = int(request.args.get("daysAhead", 30))
    limit = int(request.args.get("limit", 1))
    today = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
    start_str = today.strftime("%Y%m%d")
    end_str = (today + datetime.timedelta(days=days_ahead)).strftime("%Y%m%d")

    all_results = []

    # Loop through all configured teams
    for team_name, team_info in TEAMS.items():
        username, password, subscription_id = get_team_credentials(team_info)
        if not username or not password:
            continue

        groups_to_query = team_info.get("groups", [])
        with ThreadPoolExecutor(max_workers=min(len(groups_to_query), 5)) as executor:
            futures = {
                executor.submit(fetch_window, subscription_id, start_str, end_str, username, password, grp): grp
                for grp in groups_to_query
            }

            for future in as_completed(futures):
                grp = futures[future]
                raw = future.result()
                flat = normalize_entries(raw)

                for row in flat:
                    for user in row.get("Users", []):
                        if user_id.lower() == (user.get("UserId") or "").lower():
                            all_results.append({
                                "Team": team_name,
                                "GroupId": row["GroupId"],
                                "Date": row["Date"],
                                "StartTime": row.get("StartTime"),
                                "EndTime": row.get("EndTime"),
                                "Timezone": row.get("Timezone")
                            })

    # Remove duplicates
    unique_results = {
        (r["GroupId"], r["StartTime"], r["EndTime"]): r for r in all_results
    }.values()

    if not unique_results:
        return jsonify({
            "message": f"No upcoming on-call assignments found for {user_id} in the next {days_ahead} days."
        }), 404

    sorted_results = sorted(unique_results, key=lambda x: x.get("StartTime", ""))
    next_shifts = sorted_results[:limit]

    entry = next_shifts[0]
    start = entry["StartTime"][11:16] if entry.get("StartTime") else "?"
    end = entry["EndTime"][11:16] if entry.get("EndTime") else "?"

    summary_text = (
        f"{user_id} is next on call for {entry['GroupId']} "
        f"on {entry['Date']} ({start} - {end})"
    )

    return jsonify({
        "status": 200,
        "user": user_id,
        "nextOnCall": list(next_shifts),
        "summary": summary_text
    }), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "OCM On-Call API",
        "status": "running",
        "endpoints": ["/getSchedule (POST)", "/getNextOnCall (POST)"]
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting OCM API backend on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

