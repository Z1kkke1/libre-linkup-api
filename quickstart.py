
from pylibrelinkup import PyLibreLinkUp
from pylibrelinkup.api_url import APIUrl  # <- fixed

EMAIL = "Jan.zika@icloud.com"
PASSWORD = "Nomoresorrow12"

client = PyLibreLinkUp(email=EMAIL, password=PASSWORD, api_url=APIUrl.EU)
client.authenticate()

patients = client.get_patients()
if not patients:
    raise SystemExit("No patients found in LibreLinkUp. Share to this account first (in the app).")

patient = patients[0]
latest = client.latest(patient_identifier=patient)
print(f"glucose={latest.value} mmol/L, trend={latest.trend.name}, time={latest.timestamp}")
