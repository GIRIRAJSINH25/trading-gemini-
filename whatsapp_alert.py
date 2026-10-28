import os
import requests


def send_whatsapp_message(message: str):
    """Send a message through Twilio WhatsApp API if credentials are configured."""
    account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
    to_number = os.getenv("TWILIO_WHATSAPP_TO", "").strip()

    if not account_sid or not auth_token or not from_number or not to_number:
        print("WhatsApp not configured. Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, and TWILIO_WHATSAPP_TO.")
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    payload = {
        "From": from_number,
        "To": to_number,
        "Body": message,
    }
    response = requests.post(url, data=payload, auth=(account_sid, auth_token), timeout=30)
    print(f"WhatsApp status: {response.status_code}")
    print(response.text)

    try:
        data = response.json()
    except ValueError:
        return False

    return response.status_code in {200, 201, 202} and data.get("sid") is not None
