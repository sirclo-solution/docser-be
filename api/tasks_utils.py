import io
import json
import re
from datetime import datetime
from hashlib import sha1

import pdfplumber
from django.conf import settings
from django.core.cache import cache
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import HttpError, build
from pdfminer.pdfdocument import PDFEncryptionError

from authentication.models import UserOAuth2Credentials

from .utils import get_meili_client


def create_user_id_from_email(email):
    return re.sub(r"[.@]", "_", email)


def get_drive_with_admin_credentials():

    user_credentials_obj = UserOAuth2Credentials.objects.get(
        user__email=settings.EMAIL_ADMIN
    )
    user_credentials_dict = json.loads(user_credentials_obj.credentials)
    credentials = Credentials(**user_credentials_dict)
    drive = build("drive", "v3", credentials=credentials)
    return drive


def seperate_files_and_folders(files_and_folders):
    files = []
    folders = []

    while len(files_and_folders) > 0:
        current_data = files_and_folders.pop()
        if current_data["mimeType"] == "application/vnd.google-apps.folder":
            folders.append(current_data)
        else:
            files.append(current_data)

    return files, folders


def fetch_changes_from_drive(drive_service):
    data = []

    new_start_page_token = cache.get("start_page_token", None)
    if not new_start_page_token:
        response = (
            drive_service.changes()
            .getStartPageToken(
                supportsAllDrives=True,
            )
            .execute(num_retries=10)
        )
        new_start_page_token = response.get("startPageToken", None)

    next_page_token = new_start_page_token
    while True:
        response = (
            drive_service.changes()
            .list(
                spaces="drive",
                pageToken=next_page_token,
                pageSize=1000,
                fields="nextPageToken, newStartPageToken, \
                    changes(changeType, removed, fileId, file(id, name, mimeType, \
                        parents, webViewLink, iconLink, \
                        createdTime, modifiedTime, lastModifyingUser, \
                            owners, sharingUser, shared))",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute(num_retries=10)
        )

        next_page_token = response.get("nextPageToken", None)

        data.extend(response.get("changes", []))

        if not next_page_token:
            new_start_page_token = response.get("newStartPageToken", None)
            cache.set("start_page_token", new_start_page_token, None)
            break

    return data


def fetch_files_and_folders_from_drive(drive_service):
    data = []
    next_page_token = None
    while True:
        response = (
            drive_service.files()
            .list(
                spaces="drive",
                pageToken=next_page_token,
                pageSize=1000,
                fields="nextPageToken, \
                    files(id, name, mimeType, parents, webViewLink, iconLink, \
                        createdTime, modifiedTime, lastModifyingUser, \
                            owners, sharingUser, shared)",
                corpora="allDrives",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute(num_retries=10)
        )
        next_page_token = response.get("nextPageToken", None)

        data.extend(response.get("files", []))
        if not next_page_token:
            break
    return data


def cleaning_files_data(folders, files, all_owner):
    for file in files:

        file["location"] = []
        file["locationLink"] = {}
        file["modifiedTime"] = (
            datetime.strptime(file["modifiedTime"], "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()
            * 1000
        )
        file["createdTime"] = (
            datetime.strptime(file["createdTime"], "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()
            * 1000
        )
        file["lastModifiedBy"] = file.pop("lastModifyingUser", {}).get(
            "displayName", ""
        )
        file["content"] = ""
        file["iconLink"] = re.sub(
            r"/[0-9]+/type/", "/128/type/", file["iconLink"], flags=re.A
        )

        current_owners = {}

        file_sharer = file.pop("sharingUser", {})

        if file_sharer and file_sharer.get("emailAddress", None) is not None:
            current_owners[file_sharer["emailAddress"]] = file_sharer["displayName"]

        for file_owner in file.get("owners", []):
            if file_owner.get("emailAddress", None) is not None:
                current_owners[file_owner["emailAddress"]] = file_owner["displayName"]

        file["owners"] = list(current_owners.values())
        all_owner.update(current_owners)

        for folder in folders:
            if len(file.get("parents", [])) and (folder["id"] == file["parents"][0]):
                parent_folder_name = folder["name"]
                file["location"].extend(folder["location"][::-1] + [parent_folder_name])
                file["locationLink"].update(folder["locationLink"])
                file["locationLink"][parent_folder_name] = folder["webViewLink"]
        file.pop("parents", None)

        is_shared = file.get("shared", False)
        file["location"].insert(0, "Shared" if is_shared else "My Drive")

        file["locationLink"][file["location"][0]] = ""


def update_current_folder_data(current_folder, parent_folder):
    parent_folder_name = parent_folder["name"]

    current_folder["location"].extend(
        [parent_folder_name] + parent_folder.get("location", [])
    )

    current_folder["locationLink"][parent_folder_name] = parent_folder["webViewLink"]

    for name, link in parent_folder.get("locationLink", {}).items():
        current_folder["locationLink"][name] = link

    current_folder["parents"].extend(parent_folder["parents"])


def cleaning_folders_data(folders, all_location):
    for current_folder in folders:
        current_folder["location"] = []
        current_folder["locationLink"] = {}

        i = len(current_folder.get("parents", [])) - 1
        current_total_parents = len(current_folder.get("parents", []))

        while (i < current_total_parents) & (i >= 0):
            for folder in folders:
                if (len(folder.get("parents", [])) > 0) & (
                    folder["id"] == current_folder["parents"][i]
                ):
                    update_current_folder_data(current_folder, folder)
                    i += len(folder["parents"])

            if current_total_parents == len(current_folder["parents"]):
                break

            current_total_parents = len(current_folder["parents"])

        all_location.update({current_folder["id"]: current_folder["name"]})


def fetch_files_content(drive_service, files):
    type_conversion = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }

    for file in files:
        try:
            if type_conversion.get(file.get("mimeType", ""), None):
                response = (
                    drive_service.files()
                    .export_media(
                        fileId=file["id"], mimeType=type_conversion[file["mimeType"]]
                    )
                    .execute(num_retries=10)
                )
                file["content"] = response.decode("utf-8")
            if file.get("mimeType", "") == "application/pdf":
                response = (
                    drive_service.files()
                    .get_media(fileId=file["id"], supportsAllDrives=True)
                    .execute(num_retries=10)
                )
                with pdfplumber.open(io.BytesIO(response)) as pdf:
                    for page in pdf.pages[:100]:
                        extracted_text = page.extract_text()
                        if not isinstance(extracted_text, type(None)):
                            file["content"] += extracted_text
        except HttpError:
            pass
        except PDFEncryptionError:
            pass
        file["content"] = re.sub(
            r"[^\w!\#\$%\&'\*\+\-\.\^_`\|\~:;,\[\]\(\)@\"\{\}<>/=\+]",
            " ",
            file["content"],
            flags=re.A,
        )


def sync_to_meili(locations, owners, files, removed_files_and_folders):
    meili_client = get_meili_client()

    if len(locations):
        meili_client.index("file_locations").add_documents(locations)
    if len(owners):
        meili_client.index("file_owners").add_documents(owners)
    if len(files):
        meili_client.index("files").add_documents(files)

    if len(removed_files_and_folders) > 0:
        meili_client.index("file_locations").delete_documents(removed_files_and_folders)
        meili_client.index("files").delete_documents(removed_files_and_folders)


def processing_files_and_folders(
    drive, updated_files_and_folders, removed_files_and_folders=[]
):

    files, folders = seperate_files_and_folders(updated_files_and_folders)

    all_owner = {}
    all_location = {}
    additional_locations = ["My Drive", "Shared"]

    cleaning_folders_data(folders=folders, all_location=all_location)
    cleaning_files_data(folders=folders, files=files, all_owner=all_owner)

    fetch_files_content(drive, files)

    all_owner_restructured = [
        {
            "id": create_user_id_from_email(owner_email),
            "name": owner_name,
            "email": owner_email,
        }
        for owner_email, owner_name in all_owner.items()
    ]

    for index, additional_location in enumerate(additional_locations):
        additional_location_id = (
            f"{index}_{sha1(str(additional_location).encode()).hexdigest()}"
        )
        all_location.update({additional_location_id: additional_location})

    all_location_restructured = [
        {"id": location_id, "name": location_name}
        for location_id, location_name in all_location.items()
    ]

    sync_to_meili(
        all_location_restructured,
        all_owner_restructured,
        files,
        removed_files_and_folders,
    )


def seperate_removed_and_updated_files_and_folders(changed_files_and_folders):
    removed_files_and_folders_id = []
    updated_files_and_folders = []

    while len(changed_files_and_folders) > 0:
        current_data = changed_files_and_folders.pop()
        if current_data.get("changeType", "") == "file":
            if current_data["removed"]:
                removed_files_and_folders_id.append(current_data["fileId"])
            else:
                updated_files_and_folders.append(current_data["file"])

    return removed_files_and_folders_id, updated_files_and_folders
