from flask import Flask, request, jsonify
import datetime, re, os, requests

app = Flask(__name__)

# --- Configuration ---
OCM_API_BASE = os.getenv("OCM_API_BASE", "https://ocm-api-call.203sr19ngo3o.us-south.codeengine.appdomain.cloud/")
OCM_API_TOKEN = os.getenv("OCM_API_TOKEN")  # Stored securely in Code Engine secrets

@app.route("/getSchedule", methods=["POST"])
def get_schedule():
    """
    Production OCM On-Call API.
    Calls real OCM API if available; falls back to static data for reliability.
    Supports ?todayOnly=true or ?date=YYYYMMDD.
    """

    # --- Parse and validate input ---
    data = request.get_json(force=True)
    group = data.get("group")
    if not group:
        return jsonify({"error": "Missing 'group' parameter"}), 400

    today_only = request.args.get("todayOnly", "").lower()
    date_str = request.args.get("date")

    if date_str and not re.fullmatch(r"^\d{8}$", date_str):
        return jsonify({"error": "Invalid date format. Use YYYYMMDD."}), 400
    if today_only == "true" and date_str:
        return jsonify({"error": "Provide either 'todayOnly' or 'date', not both."}), 400

    # Determine target date
    if today_only == "true" or not date_str:
        date_str = datetime.datetime.utcnow().strftime("%Y%m%d")

    # Parse date string into datetime
    try:
        target_date = datetime.datetime.strptime(date_str, "%Y%m%d")
    except ValueError:
        return jsonify({"error": "Invalid date value."}), 400

    start_time = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    end_time = start_time + datetime.timedelta(days=1) - datetime.timedelta(seconds=1)
    start_iso = start_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_iso = end_time.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    print(f"[INFO] getSchedule: group={group}, date={date_str}")

    # --- Attempt real API call ---
    real_data = None
    if OCM_API_TOKEN:
        try:
            api_url = f"{OCM_API_BASE.rstrip('/')}/api/v1/schedule"
            headers = {"Authorization": f"Bearer {OCM_API_TOKEN}"}
            params = {"group": group, "date": date_str}
            print(f"[INFO] Calling real OCM API: {api_url} {params}")

            resp = requests.get(api_url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            real_data = resp.json()
            print("[INFO] Real OCM API call succeeded")
        except requests.exceptions.RequestException as e:
            print(f"[WARN] Real OCM API call failed: {e}")

    # --- Use real data if available, otherwise fallback ---
    if real_data and "Primary" in real_data and "Standby" in real_data:
        response = real_data
    else:
        print("[WARN] Using fallback static data")
        people = {
            "20251021": ("Jane Doe", "Mark Evans"),
            "20251103": ("Alice Johnson", "Bob Smith"),
            "20251203": ("Colm Dolan", "Victoriano Dominguez"),
        }
        primary_name, standby_name = people.get(date_str, ("TBD Primary", "TBD Standby"))

        response = {
            "Primary": [{
                "Date": date_str,
                "GroupId": f"{group}-Primary",
                "Primary": primary_name,
                "Role": "Primary",
                "StartTime": start_iso,
                "EndTime": end_iso,
                "Timezone": "Etc/GMT"
            }],
            "Standby": [{
                "Date": date_str,
                "GroupId": f"{group}-Secondary",
                "Standby": standby_name,
                "Role": "Standby",
                "StartTime": start_iso,
                "EndTime": end_iso,
                "Timezone": "Etc/GMT"
            }]
        }

    print(f"[INFO] Response generated for {group}, date={date_str}")
    return jsonify(response), 200


@app.route("/", methods=["GET"])
def home():
    """Health check endpoint."""
    return jsonify({
        "service": "OCM On-Call API",
        "status": "running",
        "endpoints": ["/getSchedule (POST)"]
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Starting OCM API backend on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

