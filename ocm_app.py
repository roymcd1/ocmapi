#!/usr/bin/env python3
"""
OCM Schedule API Wrapper (Flask)
--------------------------------
Fetches Primary and Standby (Secondary) schedules from OCM.
Supports:
  - Full year view (default)
  - Today-only view with ?todayOnly=true
"""

from flask import Flask, request, jsonify
import requests
import os
import base64
import datetime
import urllib.parse

app = Flask(__name__)

def fetch_schedule(group_name, username, password):
    """Helper function to call OCM and return simplified list"""
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    today = datetime.datetime.utcnow()
    end = today + datetime.timedelta(days=365)
    start_str = today.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")
    subscription_id = username.split("/")[0]
    encoded_group = urllib.parse.quote(group_name)
    url = (
        f"https://oncallmanager.ibm.com/api/ocdm/v1/{subscription_id}/crosssubscriptionschedules"
        f"?groupname={encoded_group}&from={start_str}&to={end_str}"
    )

    headers = {"Accept": "application/json", "Authorization": f"Basic {encoded}"}

    print(f"\n📤 Fetching OCM group: {group_name}")
    print(f"URL: {url}")

    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        print(f"❌ Error fetching {group_name}: {e}")
        return []

    # Handle response
    if not isinstance(raw, list) or len(raw) == 0 or "schedulingDetails" not in raw[0]:
        print(f"⚠️ Unexpected structure for {group_name}")
        return []

    schedule_details = raw[0]["schedulingDetails"]
    output = []

    for entry in schedule_details:
        group_id = entry.get("GroupId", group_name)
        tz = entry.get("Timezone", "Etc/GMT")
        for shift in entry.get("Shifts", []):
            users = shift.get("UserDetails", [])
            primary_name = users[0].get("FullName") if len(users) > 0 else None
            secondary_name = users[1].get("FullName") if len(users) > 1 else None
            output.append({
                "Date": shift.get("Date"),
                "GroupId": group_id,
                "Primary": primary_name,
                "Secondary": secondary_name,
                "StartTime": shift.get("StartTime"),
                "EndTime": shift.get("EndTime"),
                "Timezone": tz
            })

    print(f"✅ {group_name}: {len(output)} records")
    return output


@app.route("/getSchedule", methods=["POST"])
def get_schedule():
    """
    Input JSON example:
    {
      "group": "OMS-DBA-SEV1"
    }

    Optional query parameter:
      ?todayOnly=true   → returns only today's Primary & Standby
    """
    data = request.get_json(force=True)
    base_group = data.get("group")
    if not base_group:
        return jsonify({"error": "Missing 'group' parameter"}), 400

    username = os.getenv("OCM_USERNAME")
    password = os.getenv("OCM_PASSWORD")
    if not username or not password:
        return jsonify({"error": "OCM_USERNAME or OCM_PASSWORD not set"}), 400

    # Check if todayOnly is requested
    today_only = request.args.get("todayOnly", "false").lower() == "true"
    today_str = datetime.datetime.utcnow().strftime("%Y%m%d")

    # Build full group names
    primary_group = f"{base_group}-Primary"
    secondary_group = f"{base_group}-Secondary"

    # Fetch both
    primary_data = fetch_schedule(primary_group, username, password)
    standby_data = fetch_schedule(secondary_group, username, password)

    # Adjust standby records
    for s in standby_data:
        s["Standby"] = s.pop("Primary")
        s["Role"] = "Standby"

    for p in primary_data:
        p["Role"] = "Primary"

    # Filter for today if requested
    if today_only:
        print(f"📅 Filtering to today's date: {today_str}")
        primary_data = [p for p in primary_data if p["Date"] == today_str]
        standby_data = [s for s in standby_data if s["Date"] == today_str]

    result = {
        "Primary": primary_data,
        "Standby": standby_data
    }

    print(f"✅ Combined output: {len(primary_data)} primary, {len(standby_data)} standby")
    return jsonify(result)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

