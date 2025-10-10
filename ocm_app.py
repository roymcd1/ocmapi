from flask import Flask, request, jsonify
import requests, os, base64, datetime

app = Flask(__name__)

@app.route("/getSchedule", methods=["POST"])
def get_schedule():
    data = request.get_json(force=True)
    group = data.get("group")
    offering_id = data.get("offeringId", "")

    username = os.getenv("OCM_USERNAME")
    password = os.getenv("OCM_PASSWORD")

    auth_str = f"{username}:{password}"
    encoded = base64.b64encode(auth_str.encode()).decode()

    # Build date range (today → +1 year)
    today = datetime.datetime.utcnow()
    end = today + datetime.timedelta(days=365)
    start_str = today.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")

    subscription_id = username.split("/")[0]
    url = (
        f"https://oncallmanager.ibm.com/api/ocdm/v1/{subscription_id}/crosssubscriptionschedules"
        f"?groupname={group}&from={start_str}&to={end_str}"
    )

    headers = {
        "Accept": "application/json",
        "Authorization": f"Basic {encoded}"
    }

    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code != 200:
        return jsonify({"error": r.text, "status": r.status_code}), r.status_code

    raw = r.json()
    # Simplify response if you want:
    return jsonify(raw)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

