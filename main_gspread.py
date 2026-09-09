"""
send_unpaid_reminders_gsheets.py

Same behavior as send_unpaid_reminders.py, but reads and writes a live
Google Sheet instead of a local .xlsx file - no more "file is open in
Excel" permission errors, and anyone with edit access to the sheet always
sees the current Notified counts.

Finds everyone whose "Paid?" column is NOT checked, sends each of them a
WhatsApp DM reminder via neonize, and increments their "Notified" count
directly in the Google Sheet.

Setup (see the step-by-step guide for the Google Cloud side of this):
1. pip install neonize gspread google-auth
2. Enable the Google Sheets API on a Google Cloud project, create a
   service account, and download its JSON key (e.g. credentials.json).
3. Share your Google Sheet with the service account's email (found in the
   JSON file's "client_email" field) as an Editor.
4. Fill in config_payments.json:
     - spreadsheetId: the long ID from your sheet's URL
       (docs.google.com/spreadsheets/d/THIS_PART/edit)
     - credentialsFile: path to the service account JSON key
     - sheetName / nameColumnIndex / paidColumnIndex / notifiedColumnIndex:
       which tab & columns to read/write (0-indexed, column A = 0)
     - maxNotifications: optional cap - people whose Notified count has
       already reached this many are skipped. Leave null for no cap.
     - phoneBook: maps each name EXACTLY as it appears in the sheet to
       their WhatsApp number (country code + number, no "+" or spaces)
     - message: template sent to each unpaid person, {name} is substituted
5. Run: python send_unpaid_reminders_gsheets.py
   (First run will show a QR code to link the WhatsApp session.)

Note: each person's Notified count is written back to the sheet
immediately after their message sends successfully, one cell at a time -
so if the script is interrupted partway through, whoever was already
messaged is already recorded, and the rest are simply retried next run.
"""

import json
import time

import gspread
from neonize.client import NewClient
from neonize.events import ConnectedEv
from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
from neonize.utils import build_jid

CONFIG_PATH = "config.json"


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def is_paid_cell(value) -> bool:
    """Google Sheets checkboxes/values come back as strings via get_all_values()."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().upper() in ("TRUE", "1", "YES")
    if isinstance(value, (int, float)):
        return value == 1
    return False


def get_notified_count(value) -> int:
    """Notified column is a running tally of how many reminders were sent."""
    if value in (None, ""):
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def open_worksheet(config: dict):
    gc = gspread.service_account(filename=config["credentialsFile"])
    sh = gc.open_by_key(config["spreadsheetId"])
    ws = sh.worksheet(config["sheetName"])
    return ws


def get_targets(ws, name_col, paid_col, notified_col, max_notifications):
    """
    Returns a list of (row_number, name, current_notified_count) for people
    who are unpaid and, if max_notifications is set, haven't already hit
    that cap. row_number is the actual 1-indexed sheet row (header is row
    1, so data starts at row 2), for writing back directly.
    """
    all_rows = ws.get_all_values()  # list of lists of strings

    targets = []
    for row_idx, row in enumerate(all_rows[1:], start=2):  # skip header row
        if name_col >= len(row) or not row[name_col].strip():
            continue
        name = row[name_col].strip()

        paid = row[paid_col] if paid_col < len(row) else ""
        if is_paid_cell(paid):
            continue  # already paid, nothing to do

        notified_raw = row[notified_col] if (notified_col is not None and notified_col < len(row)) else None
        notified_count = get_notified_count(notified_raw)
        if max_notifications is not None and notified_count >= max_notifications:
            continue  # already reminded the max number of times

        targets.append((row_idx, name, notified_count))

    return targets


def main():
    config = load_config()

    name_col = config["nameColumnIndex"]
    paid_col = config["paidColumnIndex"]
    notified_col = config.get("notifiedColumnIndex")
    max_notifications = config.get("maxNotifications")  # None = no cap

    ws = open_worksheet(config)
    targets = get_targets(ws, name_col, paid_col, notified_col, max_notifications)

    if not targets:
        print("Nobody to notify (everyone is either paid or has hit maxNotifications).")
        return

    phone_book = config.get("phoneBook", {})
    message_template = config.get(
        "message",
        "Hi {name}, just a reminder that your payment is still outstanding.",
    )
    delay = config.get("sendDelaySeconds", 2)

    missing = [name for _, name, _ in targets if name not in phone_book]
    if missing:
        print("WARNING: no phone number in phoneBook for:", ", ".join(missing))
        print("These people will NOT be messaged until you add their number.\n")

    to_message = [
        (row_idx, name, phone_book[name], count)
        for row_idx, name, count in targets
        if name in phone_book
    ]

    if not to_message:
        print("No target has a matching phone number. Update phoneBook and re-run.")
        return

    print(f"About to message {len(to_message)} people:")
    for _, name, number, count in to_message:
        print(f"  - {name} -> {number} (notified {count} time(s) so far)")

    client = NewClient(
        name="payments-reminder-bot",
        props=DeviceProps(os="XC Payments Bot", platformType=DeviceProps.DESKTOP),
    )

    @client.event(ConnectedEv)
    def on_connected(client: NewClient, event: ConnectedEv):
        print("\nConnected to WhatsApp. Sending reminders...")
        for row_idx, name, number, count in to_message:
            jid = build_jid(number)
            text = message_template.format(name=name)
            try:
                client.send_message(jid, text)
                print(f"  sent -> {name} ({number})")
            except Exception as e:
                print(f"  SEND FAILED -> {name} ({number}): {e}")
                time.sleep(delay)
                continue

            if notified_col is not None:
                try:
                    # gspread rows/cols are 1-indexed; notified_col is 0-indexed
                    ws.update_cell(row_idx, notified_col + 1, count + 1)
                except Exception as e:
                    print(f"  WARNING: sent to {name}, but couldn't update the sheet: {e}")
                    # Keep going - unlike a locked local file, a transient API
                    # error here won't block later writes from succeeding.
            time.sleep(delay)  # avoid sending too fast / getting flagged as spam

        print("Done sending all reminders and updating the sheet.")
        client.stop()

    client.connect()


if __name__ == "__main__":
    main()