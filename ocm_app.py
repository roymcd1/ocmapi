from flask import Flask, request, jsonify
import datetime, re, os, requests, json, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dateutil import parser as dtparse  # pip install python-dateutil
from datetime import timezone

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
CACHE_FILE = "cache/schedule_cache.json"
CACHE_TTL_HOURS = 24
CACHE_MONTHS = 6

# Ensure cache directory exists
os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)

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
    """Main endpoint for fetching on-call schedule (supports multi-group teams and flexible date)."""
    data = request.get_json(force=True) or {}
    group = data.get("groupPrefix")
    team_key = data.get("teamKey")
    env_prefix = data.get("envPrefix")

    today_only = request.args.get("todayOnly", "").lower()
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

    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    day_end = day_start + datetime.timedelta(days=1)
    query_start = (day_start - datetime.timedelta(days=30)).strftime("%Y%m%d")
    query_end = (day_end + datetime.timedelta(days=30)).strftime("%Y%m%d")

    team_name, team_info = find_team_entry(group=group, team_key=team_key, env_prefix=env_prefix)
    if not team_info:
        return jsonify({"error": "No team configuration matched your request."}), 500

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
                results.append({
                    "GroupId": row["GroupId"],
                    "Date": date_str,
                    "Timezone": row.get("Timezone"),
                    "StartTime": row.get("StartTime"),
                    "EndTime": row.get("EndTime"),
                    "Users": pick_display_users(row.get("Users", []))
                })

    return jsonify({"status": 200, "body": results}), 200


# --- NEW ENDPOINT ---
@app.route("/when-am-i-on-call", methods=["POST"])
def when_am_i_on_call():
    """Return the next on-call shift for a given user (cached for 24h)."""
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Missing 'email' field."}), 400

    # --- Load or refresh cache ---
    cache_valid = False
    if os.path.exists(CACHE_FILE):
        age_hours = (datetime.datetime.utcnow() - datetime.datetime.utcfromtimestamp(os.path.getmtime(CACHE_FILE))).total_seconds() / 3600
        cache_valid = age_hours < CACHE_TTL_HOURS

    if cache_valid:
        logger.info("Using cached schedule data.")
        with open(CACHE_FILE) as f:
            all_entries = json.load(f)
    else:
        logger.info("Cache expired or missing — fetching fresh 6 months of schedule data.")
        all_entries = []
        now = datetime.datetime.utcnow().replace(tzinfo=timezone.utc)
        start_str = now.strftime("%Y%m%d")
        end_str = (now + datetime.timedelta(days=30 * CACHE_MONTHS)).strftime("%Y%m%d")

        for team_name, team_info in TEAMS.items():
            username, password, subscription_id = get_team_credentials(team_info)
            if not username or not password:
                continue
            for group in team_info.get("groups", []):
                raw = fetch_window(subscription_id, start_str, end_str, username, password, group)
                flat = normalize_entries(raw)
                for row in flat:
                    for u in pick_display_users(row.get("Users", [])):
                        all_entries.append({
                            "GroupId": row["GroupId"],
                            "StartTime": row["StartTime"],
                            "EndTime": row["EndTime"],
                            "Timezone": row["Timezone"],
                            "UserId": u.get("userId", "").lower(),
                            "FullName": u.get("name", "")
                        })

        with open(CACHE_FILE, "w") as f:
            json.dump(all_entries, f)
        logger.info(f"Cached {len(all_entries)} entries to {CACHE_FILE}")

    # --- Find next shift for the user ---
    now_utc = datetime.datetime.utcnow().replace(tzinfo=timezone.utc)
    future_shifts = []
    for entry in all_entries:
        if entry.get("UserId") == email:
            try:
                start = dtparse.isoparse(entry["StartTime"])
                if start > now_utc:
                    future_shifts.append(entry)
            except Exception:
                continue

    if not future_shifts:
        return jsonify({"message": f"No upcoming on-call shifts found for {email}."}), 404

    # Sort by start time
    future_shifts.sort(key=lambda e: dtparse.isoparse(e["StartTime"]))
    next_shift = future_shifts[0]

    return jsonify({
        "name": next_shift.get("FullName"),
        "group": next_shift.get("GroupId"),
        "next_on_call_start": next_shift.get("StartTime"),
        "next_on_call_end": next_shift.get("EndTime"),
        "timezone": next_shift.get("Timezone")
    }), 200


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "OCM On-Call API",
        "status": "running",
        "endpoints": ["/getSchedule (POST)", "/when-am-i-on-call (POST)"]
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🚀 Starting OCM API backend on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

