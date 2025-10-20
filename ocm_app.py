from flask import Flask, request, jsonify
import requests, re, datetime

# Flask app entrypoint (used by Code Engine)
app = Flask(__name__)

@app.route("/check-document", methods=["POST"])
def check_document():
    """Return Primary + Standby on-call schedule for today or a specific date."""
    data = request.get_json(force=True)
    group = data.get("group")
    if not group:
        return jsonify({"error": "Missing 'group' parameter"}), 400

    # --- Query parameter handling ---
    date_str = request.args.get("date")
    today_flag = request.args.get("todayOnly", "").lower()

    if date_str:
        if not re.fullmatch(r"^\d{8}$", date_str):
            return jsonify({"error": "Invalid date format. Use YYYYMMDD."}), 400
        if today_flag == "true":
            return jsonify({"error": "Provide either 'todayOnly' or 'date', not both."}), 400
    else:
        # Default to today's UTC date
        date_str = datetime.datetime.utcnow().strftime("%Y%m%d")
        today_flag = "true"

    # --- Build API call ---
    ocm_url = "https://ocm-api-call.203sr19ngo3o.us-south.codeengine.appdomain.cloud/getSchedule"
    query_params = {"todayOnly": "true"} if today_flag == "true" else {"date": date_str}
    payload = {"group": group}

    try:
        resp = requests.post(ocm_url, params=query_params, json=payload, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Failed to reach OCM API: {str(e)}"}), 502

    result = resp.json()

    # --- Local filter by Date field ---
    result["Primary"] = [e for e in result.get("Primary", []) if e.get("Date") == date_str]
    result["Standby"] = [e for e in result.get("Standby", []) if e.get("Date") == date_str]

    # --- Merge Primary and Standby ---
    merged_schedule = []
    for entry in result.get("Primary", []):
        merged_schedule.append({
            "Name": entry.get("Primary"),
            "Role": "Primary",
            "Date": entry.get("Date"),
            "StartTime": entry.get("StartTime"),
            "EndTime": entry.get("EndTime"),
            "Timezone": entry.get("Timezone")
        })
    for entry in result.get("Standby", []):
        merged_schedule.append({
            "Name": entry.get("Standby"),
            "Role": "Standby",
            "Date": entry.get("Date"),
            "StartTime": entry.get("StartTime"),
            "EndTime": entry.get("EndTime"),
            "Timezone": entry.get("Timezone")
        })

    return jsonify(merged_schedule), 200


# --- For local debugging only ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
