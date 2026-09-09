"""
send_unpaid_reminders.py

Reads the "Cocktail Crawl" sheet of a payments spreadsheet, finds everyone
whose "Paid?" column is NOT checked (True), sends each of them a WhatsApp
DM reminder via neonize, and then increments the "Notified" column in the spreadsheet, which tracks
HOW MANY TIMES each person has been sent a reminder (not just whether).

This is a one-shot batch job (not a listening auto-reply bot): it connects,
sends the reminders, updates the sheet, then exits.

Setup:
1. pip install neonize openpyxl
2. Fill in config_payments.json:
     - excelFile: path to the .xlsx file
     - sheetName / nameColumnIndex / paidColumnIndex / notifiedColumnIndex:
       which sheet & columns to read/write (0-indexed, so column A = 0)
     - maxNotifications: optional cap (e.g. 3) - people whose Notified count
       has already reached this many are skipped on this run. Leave as null
       (or omit) to always message every unpaid person with a phone number,
       regardless of how many times they've already been reminded.
     - phoneBook: maps each name EXACTLY as it appears in the spreadsheet to
       their WhatsApp number (country code + number, no "+" or spaces,
       e.g. "447911123456")
     - message: template sent to each unpaid person, {name} is substituted
3. Run: python send_unpaid_reminders.py
   (First run will show a QR code to link the WhatsApp session, same as
   your existing auto-reply bot.)

Note on writing back to the sheet: the workbook is saved after EACH
message is sent (not just at the end), so if the bot crashes or WhatsApp
disconnects partway through, the Notified counts for people already
messaged this run are safely persisted, and only the un-sent remainder
would be retried next time.
"""

import json
import time

import openpyxl
from neonize.client import NewClient
from neonize.events import ConnectedEv
from neonize.utils import build_jid
from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps

CONFIG_PATH = "test-config.json"


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def is_paid_cell(value) -> bool:
    """Excel checkbox-style paid cells can come back as True, 1, 1.0, 'TRUE', etc."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def get_notified_count(value) -> int:
    """Notified column is a running tally of how many reminders were sent."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def get_targets(xlsx_path, sheet_name, name_col, paid_col, notified_col, max_notifications):
    """
    Returns (workbook, worksheet, targets) where targets is a list of
    (row_number, name, current_notified_count) for people who are unpaid
    and, if max_notifications is set, haven't already hit that cap.
    row_number is the actual 1-indexed Excel row, so it can be written
    back to directly.
    """
    wb = openpyxl.load_workbook(xlsx_path)  # NOT data_only: we need a writable copy
    ws = wb[sheet_name]

    targets = []
    for row_idx, row in enumerate(
        ws.iter_rows(min_row=2, values_only=True), start=2
    ):
        name = row[name_col]
        if name is None:
            continue

        paid = row[paid_col]
        if is_paid_cell(paid):
            continue  # already paid, nothing to do

        notified_count = get_notified_count(row[notified_col]) if notified_col is not None else 0
        if max_notifications is not None and notified_count >= max_notifications:
            continue  # already reminded the max number of times

        targets.append((row_idx, str(name).strip(), notified_count))

    return wb, ws, targets


def main():
    config = load_config()

    # get columns from spreadsheet
    name_col = config["nameColumnIndex"]
    paid_col = config["paidColumnIndex"]
    notified_col = config.get("notifiedColumnIndex")
    max_notifications = config.get("maxNotifications")  # None = no cap

    # get people who need to be messaged
    wb, ws, targets = get_targets(
        config["excelFile"], config["sheetName"], name_col, paid_col, notified_col, max_notifications
    )

    # if nobody that needs to be messaged
    if not targets:
        print("Nobody to notify (everyone is either paid or has hit maxNotifications).")
        return

    # get relevant items from config
    phone_book = config.get("phoneBook", {})
    message_template = config.get("message")
    delay = config.get("sendDelaySeconds", 2)

    # add people without known phone number to printed list
    missing = [name for _, name, _ in targets if name not in phone_book]
    if missing:
        print("WARNING: no phone number in phoneBook for:", ", ".join(missing))
        print("These people will NOT be messaged until you add their number.\n")

    # add everyone that meets the requirements to be messaged to list
    to_message = [
        (row_idx, name, phone_book[name], count)
        for row_idx, name, count in targets
        if name in phone_book
    ]

    # if no on to message
    if not to_message:
        print("No target has a matching phone number. Update phoneBook and re-run.")
        return

    # say how many timse each person about to messaged has been messaged
    print(f"About to message {len(to_message)} people:")
    for _, name, number, count in to_message:
        print(f"  - {name} -> {number} (notified {count} time(s) so far)")

    client = NewClient(
        name="payments-reminder-bot",
        props=DeviceProps(os="XC Payments Bot", platformType=DeviceProps.ANDROID_PHONE),
    )

    # message everyone in to_message
    @client.event(ConnectedEv)
    def on_connected(client: NewClient, event: ConnectedEv):
        print("\nConnected to WhatsApp. Sending reminders...")
        for row_idx, name, number, count in to_message:
            jid = build_jid(number)
            text = message_template.format(name=name)
            try:
                client.send_message(jid, text)
                print(f"  sent -> {name} ({number})")

                if notified_col is not None:
                    # openpyxl columns are 1-indexed; notified_col is 0-indexed
                    ws.cell(row=row_idx, column=notified_col + 1).value = count + 1
                    wb.save(config["excelFile"])
            except Exception as e:
                print(f"  FAILED -> {name} ({number}): {e}")
                # Count not incremented, so it'll be retried on the next run.
            time.sleep(delay)  # avoid sending too fast / getting flagged as spam

        print("Done sending all reminders and updating the sheet. Exiting.")
        client.stop()

    client.connect()


if __name__ == "__main__":
    main()