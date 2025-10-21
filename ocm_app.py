from flask import Flask, request, jsonify
import requests, re, datetime, os

# Create Flask app
app = Flask(__name__)

@app.route("/check-document", methods=["POST"])
def check_document():
    """
    Wrapper API to fetch on-call schedule from the backend OCM API.
    Returns Primary + Standby for a given date or today.
    """
    data = request.get_json(force=True)
    group = data.get("group")
    if not group:
        return jsonify({"error": "Missing 'group' parameter"}), 400

    # --- Handle query parameters ---
    date_str = request.args.get("date")
    today_flag = request.args.get("todayOnly", "").lower()

    if date_str:
        # Validate date format
        if not re.fullmatch(r"^\d{8}$", date_str):
            return jsonify({"error": "Invalid date format. Use YYYYMMDD."}), 400
        if today_flag == "true":
            return jsonify({"error": "Provide either 'todayOnly' or 'date', not both."}), 400
    else:
        # Default to today's UTC date if no date provided
        date_str = datetime.datetime.utcnow().strftime("%Y%m%d")
        today_flag = "true"

    # --- Call the real backend OCM API ---
    ocm_url = "https://ocm-api-call.203sr19ngo3o.us-south.codeengine.appdomain.cloud/getSchedule"
    query_params = {"todayOnly": "true"} if today_flag == "true" else {"date": date_str}
    payload = {"group": group}

    try:
        resp = requests.post(ocm_url, params=query_params, json=payload, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Failed to reach OCM API: {str(e)}"}), 502

    result = resp.json()

    # --- Filter locally by Date (OCM returns full year sometimes) ---
    result["Primary"] = [e for e in result.get("Primary", []) if e.get("Date") == date_str]
    result["Standby"] = [e for e in result.get("Standby", []) if e.get("Date") == date_str]

    # --- Merge Primary and Standby results ---
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


# --- App entrypoint (runs on port 8080) ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=True)

