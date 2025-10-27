from flask import Flask, request, jsonify
import datetime, re, os, requests, json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dateutil import parser as dtparse  # pip install python-dateutil

app = Flask(__name__)

# --- Configuration ---
OCM_API_BASE = os.getenv("OCM_API_BASE", "https://oncallmanager.ibm.com")
TEAMS_CONFIG_PATH = "config/teams.json"

# Load team configuration once
with open(TEAMS_CONFIG_PATH) as f:
    TEAMS = json.load(f)


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
        print(f"[ERROR] Missing credentials for env_prefix={env_prefix}")
        return None, None, None
    subscription_id = username.split("/")[0]
    return username, password, subscription_id


def fetch_window(subscription_id, start_str, end_str, username, password, group_hint=None):
    """Fetch a wide time window of schedules."""
    url = f"{OCM_API_BASE}/api/ocdm/v1/{subscription_id}/crosssubscriptionschedules"
    params = {"from": start_str, "to": end_str}
    print(f"[INFO] GET {url} from={start_str} to={end_str} (group_hint={group_hint})")

    try:
        resp = requests.get(url, auth=(username, password), params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if not data:
                return []
            if isinstance(data, list):
                print(f"[INFO] OCM returned {len(data)} buckets")
                return data
            return []
        else:
            print(f"[WARN] HTTP {resp.status_code}: {resp.text[:150]}")
            return []
    except Exception as e:
        print(f"[ERROR] fetch_window failed: {e}")
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
        print(f"[WARN] Failed to parse times: {e}")
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


@app.route("/getSchedule", methods=["POST"])
def get_schedule():
    """Main endpoint for fetching on-call schedule (supports multi-group teams)."""
    data = request.get_json(force=True) or {}
    group = data.get("groupPrefix")
    team_key = data.get("teamKey")
    env_prefix = data.get("envPrefix")
    group_override = data.get("groupOverride")

    today_only = request.args.get("todayOnly", "").lower()
    date_str = request.args.get("date")

    if date_str and not re.fullmatch(r"^\d{8}$", date_str):
        return jsonify({"error": "Invalid date format. Use YYYYMMDD."}), 400
    if today_only == "true" and date_str:
        return jsonify({"error": "Provide either 'todayOnly' or 'date', not both."}), 400
    if today_only == "true" or not date_str:
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
            "hint": {"groupPrefix": group, "teamKey": team_key, "envPrefix": env_prefix,
                     "valid_teams": list(TEAMS.keys())}
        }), 500

    username, password, subscription_id = get_team_credentials(team_info)
    if not username or not password:
        return jsonify({"error": "Missing credentials"}), 500

    # Determine groups to query
    groups_to_query = []
    if group:
        groups_to_query = [group]
    else:
        groups_to_query = team_info.get("groups", [])

    print(f"[INFO] Fetching schedules for groups={groups_to_query}")

    results = []
    with ThreadPoolExecutor(max_workers=len(groups_to_query)) as executor:
        futures = {
            executor.submit(fetch_window, subscription_id, query_start, query_end, username, password, grp): grp
            for grp in groups_to_query
        }
        for future in as_completed(futures):
            grp = futures[future]
            raw = future.result()
            flat = normalize_entries(raw)
            for row in flat:
                if (row.get("GroupId") == grp and
                    overlaps_day(row.get("StartTime",""), row.get("EndTime",""), day_start, day_end)):
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

    # Build summary for Watson
    summary_lines = []
    for entry in results:
        try:
            user = entry["Users"][0]["name"] if entry["Users"] else "Unknown"
            start = entry["StartTime"][11:16] if entry.get("StartTime") else "?"
            end = entry["EndTime"][11:16] if entry.get("EndTime") else "?"
            summary_lines.append(f"{entry['GroupId']}: {user} — {start} → {end}")
        except Exception as e:
            print(f"[WARN] format fail: {e}")

    summary_text = "Here’s who’s on call today:\n\n" + "\n".join(summary_lines)

    return jsonify({
        "status": 200,
        "body": results,
        "summary": summary_text
    }), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "OCM On-Call API",
        "status": "running",
        "endpoints": ["/getSchedule (POST)"]
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Starting OCM API backend on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

