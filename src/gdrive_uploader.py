"""
Upload files to Google Drive using a service account.

Requires the target files to be pre-created in Google Drive and shared with the
service account as Editor. Updating an existing file does not require storage quota;
only creating new files does (which fails for service accounts on personal Google accounts).

Env vars:
  GDRIVE_SERVICE_ACCOUNT_JSON  — full JSON string of the service account key file
  GDRIVE_PICKS_FILE_ID         — Drive file ID for MLB_Picks.xlsx
  GDRIVE_RESULTS_FILE_ID       — Drive file ID for MLB_Results.xlsx
  GDRIVE_TRACKER_FILE_ID       — Drive file ID for MLB_Tracker.xlsx

Usage:
    from gdrive_uploader import upload_file
    upload_file("MLB_Picks.xlsx")
"""

import os
import json

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Map local filenames to the env var that holds the Drive file ID
_FILE_ID_ENV = {
    "MLB_Picks.xlsx":   "GDRIVE_PICKS_FILE_ID",
    "MLB_Results.xlsx": "GDRIVE_RESULTS_FILE_ID",
    "MLB_Tracker.xlsx": "GDRIVE_TRACKER_FILE_ID",
}


def _get_service():
    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise RuntimeError("GDRIVE_SERVICE_ACCOUNT_JSON env var not set")

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_info = json.loads(sa_json)
    creds   = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_file(local_path):
    """
    Update a pre-existing Google Drive file with the contents of local_path.
    The Drive file ID is read from the appropriate env var based on the filename.
    """
    from googleapiclient.http import MediaFileUpload

    if not os.path.exists(local_path):
        raise FileNotFoundError(f"File not found: {local_path}")

    fname   = os.path.basename(local_path)
    id_env  = _FILE_ID_ENV.get(fname)
    if not id_env:
        raise ValueError(f"No Drive file ID env var configured for '{fname}'")

    file_id = os.environ.get(id_env)
    if not file_id:
        raise RuntimeError(f"Env var {id_env} not set — create the file in Drive, share with service account, add the ID as a secret")

    mime = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if fname.endswith(".xlsx") else "application/octet-stream"
    )

    service = _get_service()
    media   = MediaFileUpload(local_path, mimetype=mime, resumable=True)
    service.files().update(fileId=file_id, media_body=media).execute()
    print(f"  Drive: updated '{fname}' (id: {file_id})")
