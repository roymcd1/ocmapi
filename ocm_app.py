from flask import Flask, request, jsonify
import datetime, re, os, requests, json
from dateutil import parser as dtparse  # pip install python-dateutil

app = Flask(__name__)

# --- Configuration ---
OCM_API_BASE = os.getenv("OCM_API_BASE", "https://oncallmanager.ibm.com")
TEAMS_CONFIG_PATH = "config/teams.json"

# Load team configuration once
with open(TEAMS_CONFIG_PATH) as f:
    TEAMS = json.load(f)

def find_team_entry(group=None, team_key=None, env_prefix=None):
    """
    Resolve a team entry by:
      1) exact group name match (preferred)
      2) team_key equal to a key in TEAMS
      3) env_prefix match
    Returns (team_name, team_info) or (None, None)
    """
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
    """
    From team_info, pull env-based username/password.
    subscription_id = part BEFORE the slash.
    """
    env_prefix = team_info["env_prefix"]
    username = os.getenv(f"{env_prefix}_OCM_USERNAME")
    password = os.getenv(f"{env_prefix}_OCM_PASSWORD")
    if not username or not password:
        print(f"[ERROR] Missing credentials for env_prefix={env_prefix}")
        return None, None, None
    subscription_id = username.split("/")[0]
    return username, password, subscription_id

def fetch_window(subscription_id, start_str, end_str, username, password, group_hint=None):
    """
    Fetch a wide window of schedules. Many tenants ignore group filters, so we
    request the window *without* group filter and filter locally.
    """
    url = f"{OCM_API_BASE}/api/ocdm/v1/{subscription_id}/crosssubscriptionschedules"
    params = {"from": start_str, "to": end_str}

    print(f"[INFO] GET {url} from={start_str} to={end_str} (no server-side group filter)")
    try:
        resp = requests.get(url, auth=(username, password), params=params, timeout=30)
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                print(f"[WARN] 200 OK but non-JSON body: {resp.text[:200]}")
                return []
            if data is None:
                print("[INFO] OCM returned null (treat as empty)")
                return []
            if isinstance(data, list):
                print(f"[INFO] OCM returned top-level list with {len(data)} group buckets")
                return data
            else:
                print(f"[WARN] 200 OK but payload is {type(data)}")
                return []
        else:
            print(f"[WARN] HTTP {resp.status_code}: {resp.text[:200]}")
            return []
    except Exception as e:
        print(f"[ERROR] OCM API call failed: {e}")
        return []

def normalize_entries(raw_payload):
    """
    Flatten payload:
      input: [ { "group": "...", "schedulingDetails": [ {GroupId, Date, Timezone, Shifts:[...]}, ... ] }, ... ]
      output: list of rows:
        {
          "GroupId": "...",
          "Date": "YYYYMMDD",
          "Timezone": "...",
          "StartTime": "...Z",
          "EndTime": "...Z",
          "Users": [ { "FullName": "...", "UserId": "...", "MobileNumber": "...", ... } ]
        }
    """
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
                row = {
                    "GroupId": group_id,
                    "Date": date,
                    "Timezone": tz,
                    "StartTime": shift.get("StartTime"),
                    "EndTime": shift.get("EndTime"),
                    "Users": shift.get("UserDetails", []) or []
                }
                out.append(row)
    return out

def overlaps_day(start_iso, end_iso, day_start_utc, day_end_utc):
    try:
        start = dtparse.isoparse(start_iso)
        end = dtparse.isoparse(end_iso)
        # All comparisons are UTC-aware now
        return (start < day_end_utc) and (end > day_start_utc)
    except Exception as e:
        print(f"[WARN] Failed to parse StartTime/EndTime: {e} :: {start_iso} / {end_iso}")
        return False

def pick_display_users(users):
    """
    Convert API user objects to a compact display list.
    Prefer 'FullName' if looks like a name/email; fallback to 'UserId'.
    """
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
    """
    JSON body (any one of):
      - groupPrefix: exact group name (e.g., 'OMS-DBA-SEV1-Primary')
      - OR teamKey: key from teams.json (e.g., 'CDS team', 'OMS-DBA-SEV1')
      - OR envPrefix: env prefix (e.g., 'CDS_TEAM', 'DBA_TEAM', 'P2PAAS_TEAM')
      - optional groupOverride: if team has multiple groups

    Query:
      - todayOnly=true  OR  date=YYYYMMDD
    """
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

    # ✅ FIXED: Make day window UTC-aware
    from datetime import timezone
    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    day_end = day_start + datetime.timedelta(days=1)

    # Wider query window
    query_start = (day_start - datetime.timedelta(days=30)).strftime("%Y%m%d")
    query_end = (day_end + datetime.timedelta(days=30)).strftime("%Y%m%d")

    # Resolve team
    team_name, team_info = find_team_entry(group=group, team_key=team_key, env_prefix=env_prefix)
    if not team_info:
        return jsonify({
            "error": "No team configuration matched your request.",
            "hint": {"groupPrefix": group, "teamKey": team_key, "envPrefix": env_prefix,
                     "valid_teams": list(TEAMS.keys())}
        }), 500

    # Pick concrete group
    if group:
        resolved_group = group
    else:
        groups = team_info.get("groups", [])
        if group_override and group_override in groups:
            resolved_group = group_override
        elif len(groups) == 1:
            resolved_group = groups[0]
        else:
            return jsonify({
                "error": "Multiple groups configured for this team; specify 'groupOverride'.",
                "groups": groups
            }), 400

    print(f"[INFO] getSchedule: group={resolved_group}, date={date_str} (team={team_name})")

    username, password, subscription_id = get_team_credentials(team_info)
    if not username or not password:
        return jsonify({"error": "Missing or invalid OCM credentials for the selected team"}), 500

    # Fetch wide window and normalize
    raw = fetch_window(subscription_id, query_start, query_end, username, password, group_hint=resolved_group)
    flat = normalize_entries(raw)

    # Local filtering by group + date overlap
    filtered = []
    for row in flat:
        if (row.get("GroupId") == resolved_group and
            overlaps_day(row.get("StartTime",""), row.get("EndTime",""), day_start, day_end)):
            filtered.append({
                "GroupId": row["GroupId"],
                "Date": date_str,
                "Timezone": row.get("Timezone"),
                "StartTime": row.get("StartTime"),
                "EndTime": row.get("EndTime"),
                "Users": pick_display_users(row.get("Users", []))
            })

    if not filtered:
        return jsonify({
            "message": f"No on-call assignments found for {resolved_group} on {date_str}"
        }), 404

    return jsonify(filtered), 200

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

