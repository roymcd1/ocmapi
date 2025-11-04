from flask import Flask, request, jsonify
import datetime, re, os, requests, json, logging, time, hmac, hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dateutil import parser as dtparse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

app = Flask(__name__)

# ---------------------- LOGGING ----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------- CONFIG -----------------------
OCM_API_BASE = os.getenv("OCM_API_BASE", "https://oncallmanager.ibm.com")
TEAMS_CONFIG_PATH = "config/teams.json"

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "").strip()
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "").strip()
DEFAULT_TEAMKEY = os.getenv("DEFAULT_TEAMKEY", "").strip()
DEFAULT_GROUP_PREFIX = os.getenv("DEFAULT_GROUP_PREFIX", "").strip()

slack = WebClient(token=SLACK_BOT_TOKEN) if SLACK_BOT_TOKEN else None

with open(TEAMS_CONFIG_PATH) as f:
    TEAMS = json.load(f)

# ---------------------- HELPERS ----------------------
def find_team_entry(group=None, team_key=None, env_prefix=None):
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
    env_prefix = team_info["env_prefix"]
    username = os.getenv(f"{env_prefix}_OCM_USERNAME")
    password = os.getenv(f"{env_prefix}_OCM_PASSWORD")
    if not username or not password:
        logger.error(f"Missing credentials for env_prefix={env_prefix}")
        return None, None, None
    subscription_id = username.split("/")[0]
    return username, password, subscription_id


def fetch_window(subscription_id, start_str, end_str, username, password, group_hint=None):
    url = f"{OCM_API_BASE}/api/ocdm/v1/{subscription_id}/crosssubscriptionschedules"
    params = {"from": start_str, "to": end_str}
    logger.info(f"GET {url} from={start_str} to={end_str} (group_hint={group_hint})")
    try:
        resp = requests.get(url, auth=(username, password), params=params, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                logger.info(f"OCM returned {len(data)} buckets")
                return data
        return []
    except Exception as e:
        logger.error(f"fetch_window failed: {e}")
        return []


def normalize_entries(raw_payload):
    out = []
    if not isinstance(raw_payload, list):
        return out
    for bucket in raw_payload:
        bucket_group = bucket.get("group") or bucket.get("GroupId")
        for det in bucket.get("schedulingDetails", []):
            group_id = det.get("GroupId") or bucket_group
            for shift in det.get("Shifts", []):
                out.append({
                    "GroupId": group_id,
                    "Timezone": det.get("Timezone"),
                    "StartTime": shift.get("StartTime"),
                    "EndTime": shift.get("EndTime"),
                    "Users": shift.get("UserDetails", []) or []
                })
    return out


def overlaps_day(start_iso, end_iso, day_start_utc, day_end_utc):
    try:
        start = dtparse.isoparse(start_iso)
        end = dtparse.isoparse(end_iso)
        return (start < day_end_utc) and (end > day_start_utc)
    except Exception as e:
        logger.warning(f"Failed to parse times: {e}")
        return False


def pick_display_users(users):
    out = []
    for u in users:
        name = u.get("FullName") or u.get("UserId") or ""
        out.append({
            "name": name,
            "userId": u.get("UserId") or "",
            "mobile": u.get("MobileNumber") or ""
        })
    return out


# ---------------------- GET SCHEDULE ----------------------
@app.route("/getSchedule", methods=["POST"])
def get_schedule():
    data = request.get_json(force=True) or {}
    group = data.get("groupPrefix")
    team_key = data.get("teamKey")
    env_prefix = data.get("envPrefix")
    date_str = request.args.get("date") or datetime.datetime.utcnow().strftime("%Y%m%d")

    date_str = date_str.replace("-", "")
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
            for row in normalize_entries(future.result()):
                if (row.get("GroupId") == grp and
                    overlaps_day(row.get("StartTime",""), row.get("EndTime",""), day_start, day_end)):
                    results.append({
                        "GroupId": grp,
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
        user = entry["Users"][0]["name"] if entry["Users"] else "Unknown"
        start = entry["StartTime"][11:16] if entry.get("StartTime") else "00:00"
        end = entry["EndTime"][11:16] if entry.get("EndTime") else "00:00"
        summary_lines.append(f"{entry['GroupId']}: {user} — {start} → {end}")

    summary_text = f"Here’s who’s on call for {date_str}:\n\n" + "\n".join(summary_lines)
    return jsonify({"status": 200, "body": results, "summary": summary_text}), 200


# ---------------------- SLACK SIGNATURE CHECK ----------------------
def verify_slack_signature(req) -> bool:
    if not SLACK_SIGNING_SECRET:
        logger.warning("Missing Slack signing secret; rejecting request.")
        return False
    ts = req.headers.get("X-Slack-Request-Timestamp", "0")
    sig = req.headers.get("X-Slack-Signature", "")
    try:
        ts_i = int(ts)
    except ValueError:
        return False
    # reject if older than 5 min
    if abs(time.time() - ts_i) > 60 * 5:
        return False
    body = req.get_data(as_text=True)
    base = f"v0:{ts}:{body}".encode("utf-8")
    my_sig = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode("utf-8"), base, hashlib.sha256).hexdigest()
    valid = hmac.compare_digest(my_sig, sig)
    if not valid:
        logger.warning("Invalid Slack signature.")
    return valid


# ---------------------- SLACK EVENT HANDLER ----------------------
@app.route("/slack/events", methods=["POST"])
def slack_events():
    if not verify_slack_signature(request):
        return "invalid signature", 401

    logger.info("Incoming /slack/events payload:")
    try:
        logger.info(json.dumps(request.get_json(force=True), indent=2))
    except Exception as e:
        logger.warning(f"Could not decode JSON: {e}")

    data = request.get_json(force=True) or {}
    if data.get("type") == "url_verification":
        return jsonify({"challenge": data.get("challenge")})

    if data.get("type") != "event_callback":
        return "", 200

    event = data.get("event", {}) or {}
    if event.get("subtype") in ("message_changed", "bot_message"):
        return "", 200
    if event.get("bot_id"):
        return "", 200

    channel = event.get("channel")
    text = event.get("text", "")
    root_ts = event.get("ts")
    thread_ts = event.get("thread_ts") or root_ts
    channel_type = event.get("channel_type")

    if channel_type == "im":
        logger.info("Ignoring DM event (channel_type=im)")
        return "", 200

    if "on call" not in text.lower():
        logger.info("Message not relevant to bot")
        return "", 200

    date_str = datetime.datetime.utcnow().strftime("%Y%m%d")
    payload = {}
    if DEFAULT_GROUP_PREFIX:
        payload["groupPrefix"] = DEFAULT_GROUP_PREFIX
    if DEFAULT_TEAMKEY:
        payload["teamKey"] = DEFAULT_TEAMKEY

    logger.info(f"Fetching schedule for {date_str}")
    try:
        r = requests.post(request.url_root.rstrip("/") + "/getSchedule",
                          params={"date": date_str}, json=payload, timeout=30)
        summary = r.json().get("summary", "No summary returned.")
    except Exception as e:
        summary = f"Failed to get schedule: {e}"

    if slack:
        try:
            slack.chat_postMessage(channel=channel, text=summary, thread_ts=thread_ts)
        except SlackApiError as e:
            logger.error(f"Slack post failed: {e.response.get('error')}")
    else:
        logger.info(f"Slack message (simulated): {summary}")

    return "", 200


# ---------------------- HEALTH ----------------------
@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "service": "OCM On-Call API (Slack integrated)",
        "status": "running",
        "endpoints": ["/getSchedule (POST)", "/slack/events (POST)"]
    }), 200


# ---------------------- MAIN ----------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"🚀 Starting OCM API backend on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

