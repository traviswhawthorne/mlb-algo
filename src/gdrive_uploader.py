"""
Upload files to Google Drive using a service account.

Credentials are read from the GDRIVE_SERVICE_ACCOUNT_JSON env var (full JSON string)
and an optional GDRIVE_FOLDER_ID env var (Drive folder to upload into).

Usage:
    from gdrive_uploader import upload_file
    upload_file("MLB_Picks.xlsx")
"""

import os
import json

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _get_service():
    sa_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    if not sa_json:
        raise RuntimeError("GDRIVE_SERVICE_ACCOUNT_JSON env var not set")

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    sa_info = json.loads(sa_json)
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_file(local_path, filename=None, folder_id=None):
    """
    Upload local_path to Google Drive. Updates the file if it already exists
    in the target folder; creates it otherwise. Returns the Drive file ID.
    """
    from googleapiclient.http import MediaFileUpload

    if not os.path.exists(local_path):
        raise FileNotFoundError(f"File not found: {local_path}")

    service   = _get_service()
    fname     = filename or os.path.basename(local_path)
    folder_id = folder_id or os.environ.get("GDRIVE_FOLDER_ID")

    mime = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if local_path.endswith(".xlsx") else "application/octet-stream"
    )

    # Check for an existing file in the same folder to avoid duplicates
    query = f"name = '{fname}' and trashed = false"
    if folder_id:
        query += f" and '{folder_id}' in parents"
    existing = service.files().list(q=query, fields="files(id,name)").execute().get("files", [])

    media = MediaFileUpload(local_path, mimetype=mime, resumable=True)

    if existing:
        file_id = existing[0]["id"]
        service.files().update(fileId=file_id, media_body=media).execute()
        print(f"  Drive: updated '{fname}' (id: {file_id})")
    else:
        metadata = {"name": fname}
        if folder_id:
            metadata["parents"] = [folder_id]
        result = service.files().create(body=metadata, media_body=media, fields="id").execute()
        file_id = result["id"]
        print(f"  Drive: created '{fname}' (id: {file_id})")

    return file_id
