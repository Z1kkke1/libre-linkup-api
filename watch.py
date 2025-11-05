#!/usr/bin/env python
import os, time
from datetime import datetime, timezone
from dotenv import load_dotenv
from pylibrelinkup import PyLibreLinkUp
from pylibrelinkup.api_url import APIUrl
from pylibrelinkup.exceptions import RedirectError

load_dotenv()

EMAIL = os.getenv("LIBRE_EMAIL")
PASSWORD = os.getenv("LIBRE_PASSWORD")
REGION = getattr(APIUrl, os.getenv("LIBRE_REGION", "EU"))

POLL_SECONDS = 30  # LLU má nový vzorek ~každých 60 s

def login(api: APIUrl) -> PyLibreLinkUp:
    cli = PyLibreLinkUp(email=EMAIL, password=PASSWORD, api_url=api)
    try:
        cli.authenticate()
    except RedirectError as e:
        api2 = e.args[0] if isinstance(e.args[0], APIUrl) else api
        cli = PyLibreLinkUp(email=EMAIL, password=PASSWORD, api_url=api2)
        cli.authenticate()
    return cli

def main():
    if not EMAIL or not PASSWORD:
        raise SystemExit("Chybí LIBRE_EMAIL / LIBRE_PASSWORD v .env")

    cli = login(REGION)
    patients = cli.get_patients()
    if not patients:
        raise SystemExit("No patients found in LibreLinkUp. Sdílej data na tento účet v appce.")

    patient = patients[0]
    last_ts = None

    while True:
        try:
            m = cli.latest(patient_identifier=patient)
            ts = m.timestamp
            if last_ts is None or ts > last_ts:
                last_ts = ts
                mgdl = round(m.value * 18)
                print(
                    f"{datetime.now(timezone.utc).isoformat()}  "
                    f"{m.value:.1f} mmol/L ({mgdl} mg/dL)  {m.trend.name}"
                )
        except Exception:
            # síť/expirace tokenu apod. → re-auth
            cli = login(REGION)
        time.sleep(POLL_SECONDS)

if __name__ == "__main__":
    main()
