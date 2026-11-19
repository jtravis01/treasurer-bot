"""
queue_messages.py

Two-phase reminder bot. Each person gets ONE message, even if they owe on several sheets.

  python queue_messages.py build   -> read every sheet in config, write one pending entry
                                      per person to message_queue.json (nothing is sent)
  python queue_messages.py show    -> print what's currently pending
  python queue_messages.py send    -> send everything pending, update Notified counts

message_queue.json is plain JSON: you can edit an entry's "text" or delete an entry
before sending.

config_payments.json additions:

  "groupedMessage": "Hi {name}, you still owe payments for: {items}. Thanks!",
  "sheets": [
    {"sheetName": "Cocktail Crawl", "label": "the Cocktail Crawl",
     "nameColumnIndex": 0, "paidColumnIndex": 1, "notifiedColumnIndex": 2,
     "message": "Hi {name}, your Cocktail Crawl payment is still outstanding!"},
    {"sheetName": "VestFleece", "label": "your vest/fleece",
     "nameColumnIndex": 0, "paidColumnIndex": 3, "notifiedColumnIndex": 4,
     "maxNotifications": 3,
     "message": "Hi {name}, your vest/fleece payment is still outstanding!"}
  ]

- A person owing on ONE sheet gets that sheet's "message".
- A person owing on SEVERAL gets "groupedMessage", where {items} becomes
  "the Cocktail Crawl and your vest/fleece" (built from each sheet's "label").
- Any message or groupedMessage may include {clubAccount} and/or {sumsLink}; they are
  filled from the top-level config keys of the same name (see MESSAGE_VARIABLES).
  Text is filled in at build time, so changing the config later does not change
  messages already in the queue.
- A sheet may set "followUp" (a string, or a list of strings): extra messages sent
  SEPARATELY, right after the main message, for anyone owing on that sheet. They can
  use {name}, {clubAccount} and {sumsLink}. If someone owes on several sheets with
  follow-ups, they get each one (identical texts are sent once), in config order.
  If they paid that sheet before sending, its follow-up is dropped.
- "label" defaults to the sheet name. A sheet may set its own "spreadsheetId".
- People are grouped by phone number, so name spellings that differ between sheets
  still combine as long as they map to the same number in phoneBook.
"""

import json
import os
import sys
import time
from datetime import datetime

import gspread
from neonize.client import NewClient
from neonize.events import ConnectedEv
from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import DeviceProps
from neonize.utils import build_jid

CONFIG_PATH = "new_config_payments.json"
QUEUE_PATH = "message_queue.json"
DEFAULT_GROUPED = "Hi {name}, you still have outstanding payments for: {items}."


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_queue():
    if not os.path.exists(QUEUE_PATH):
        return []
    with open(QUEUE_PATH, "r", encoding="utf-8") as f:
        queue = json.load(f)
    if any("items" not in e for e in queue):
        raise SystemExit(
            f"{QUEUE_PATH} is in the old one-sheet-per-entry format. "
            "Send or delete it first, then run build again."
        )
    return queue


def save_queue(queue):
    # Write to a temp file then swap it in, so a crash can't leave a half-written queue.
    tmp = QUEUE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2, ensure_ascii=False)
    os.replace(tmp, QUEUE_PATH)


def is_paid(value):
    return str(value).strip().upper() in ("TRUE", "1", "YES")


def get_count(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def cell(row, idx):
    return row[idx] if idx is not None and idx < len(row) else ""


def join_labels(labels):
    if len(labels) <= 2:
        return " and ".join(labels)
    return ", ".join(labels[:-1]) + " and " + labels[-1]


# Config keys that message templates may use as {placeholders}. Add a key here to
# make it available; templates that don't mention it are unaffected.
MESSAGE_VARIABLES = ("clubAccount", "sumsLink", "leedsSocialTicketLink", "leedsNoSocialTicketLink")


def message_variables(config):
    return {k: config[k] for k in MESSAGE_VARIABLES if k in config}


def fill(template, **values):
    """Fill a template, failing loudly (before anything is queued) on a bad placeholder."""
    try:
        return template.format(**values)
    except KeyError as err:
        raise SystemExit(
            f"A message template uses {{{err.args[0]}}}, which isn't available.\n"
            f"Available placeholders: {', '.join('{' + k + '}' for k in sorted(values))}\n"
            f"Template: {template!r}"
        )
    except (ValueError, IndexError):
        raise SystemExit(
            f"A message template has a stray {{ or }}. Use {{name}}-style placeholders only.\n"
            f"Template: {template!r}"
        )


def compose(name, items, grouped_template, variables):
    first = name.split()[0]
    if len(items) == 1:
        return fill(items[0]["message"], name=first, **variables)
    labels = [fill(i["label"], name=first, **variables) for i in items]
    return fill(grouped_template, name=first,
                items=join_labels(labels), **variables)


def message_for_row(sheet_config, row):
    """Resolve a sheet's message/label templates, including row-dependent variants."""
    message_by_value = sheet_config.get("messageByValue")
    label_by_value = sheet_config.get("labelByValue", {})
    if message_by_value is None:
        return sheet_config["message"], sheet_config.get("label", sheet_config["sheetName"]), None

    value_column = sheet_config["messageValueColumnIndex"]
    value = str(cell(row, value_column)).strip().upper()
    if value not in message_by_value:
        raise ValueError(
            f"Unexpected value {cell(row, value_column)!r} in column "
            f"{value_column + 1} of {sheet_config['sheetName']}; "
            f"expected one of: {', '.join(message_by_value)}"
        )
    message = message_by_value[value]
    label = label_by_value.get(value, sheet_config.get("label", sheet_config["sheetName"]))
    return message, label, value


def refresh_item_message(item, sheet_config, row):
    """Refresh a row-dependent message if its choice column changed since queueing."""
    if "messageByValue" not in sheet_config:
        return False
    message, label, value = message_for_row(sheet_config, row)
    changed = value != item.get("messageValue")
    item["message"] = message
    item["label"] = label
    item["messageValue"] = value
    return changed


def compose_followups(name, items, variables):
    """Extra separate messages (sent after the main one) for the sheets involved."""
    first = name.split()[0]
    out = []
    for item in items:
        follow = item.get("followUp")
        if not follow:
            continue
        for template in ([follow] if isinstance(follow, str) else follow):
            text = fill(template, name=first, **variables)
            if text not in out:  # two sheets with the same follow-up send it once
                out.append(text)
    return out


def open_sheets(config, gc):
    """Returns {sheetName: worksheet} for every sheet in config."""
    books, sheets = {}, {}
    for s in config["sheets"]:
        book_id = s.get("spreadsheetId", config["spreadsheetId"])
        if book_id not in books:
            books[book_id] = gc.open_by_key(book_id)
        sheets[s["sheetName"]] = books[book_id].worksheet(s["sheetName"])
    return sheets


def merge_people_into_queue(queue, people, grouped_template, variables):
    """
    Adds each person to the queue. If they already have a pending entry, any items
    not already in it are merged in instead of being dropped.
    Returns (names added, names merged into, names whose hand-edited text is now stale).
    """
    pending = {e["number"]: e for e in queue if e["status"] == "pending"}
    added, merged, stale = [], [], []

    for number, person in people.items():
        entry = pending.get(number)
        if entry is None:
            queue.append({
                "name": person["name"],
                "number": number,
                "text": compose(person["name"], person["items"], grouped_template, variables),
                "followUps": compose_followups(person["name"], person["items"], variables),
                "items": person["items"],
                "status": "pending",
                "queuedAt": datetime.now().isoformat(timespec="seconds"),
            })
            added.append(person["name"])
            continue

        have = {(i["sheet"], i["row"]) for i in entry["items"]}
        new_items = [i for i in person["items"] if (i["sheet"], i["row"]) not in have]
        if not new_items:
            continue

        # Only rewrite the wording if the person hasn't hand-edited it: i.e. the text
        # still equals what the current items would have produced.
        unedited = entry["text"] == compose(entry["name"], entry["items"], grouped_template, variables)
        follow_unedited = (entry.get("followUps", [])
                           == compose_followups(entry["name"], entry["items"], variables))
        entry["items"].extend(new_items)
        merged.append(entry["name"])
        if unedited:
            entry["text"] = compose(entry["name"], entry["items"], grouped_template, variables)
        if follow_unedited:
            entry["followUps"] = compose_followups(entry["name"], entry["items"], variables)
        if not (unedited and follow_unedited):
            stale.append(entry["name"])

    return added, merged, stale


def load_phone_book(config):
    """
    Combines every phone book in the config ("phoneBook", "phoneBookLastNames", or any
    other key starting with "phoneBook") into one name -> number lookup.
    """
    book = {}
    for key, entries in config.items():
        if not key.startswith("phoneBook") or not isinstance(entries, dict):
            continue
        for name, number in entries.items():
            if name in book and book[name] != number:
                print(f"WARNING: {name!r} has different numbers in different phone books; "
                      f"using {book[name]}")
                continue
            book.setdefault(name, number)
    return book


def build(config):
    gc = gspread.service_account(filename=config["credentialsFile"])
    worksheets = open_sheets(config, gc)
    phone_book = load_phone_book(config)
    grouped_template = config.get("groupedMessage", DEFAULT_GROUPED)
    variables = message_variables(config)

    queue = load_queue()

    people = {}  # phone number -> {"name": first name seen, "items": [...]}
    missing = []

    for s in config["sheets"]:
        rows = worksheets[s["sheetName"]].get_all_values()
        for row_idx, row in enumerate(rows[1:], start=2):  # row 1 is the header
            name = cell(row, s["nameColumnIndex"]).strip()
            if not name or is_paid(cell(row, s["paidColumnIndex"])) or name == "Total":
                continue

            count = get_count(cell(row, s.get("notifiedColumnIndex")))
            cap = s.get("maxNotifications")
            if cap is not None and count >= cap:
                continue
            if name not in phone_book:
                missing.append(f"{name} ({s['sheetName']})")
                continue

            number = phone_book[name]
            person = people.setdefault(number, {"name": name, "items": []})
            message, label, message_value = message_for_row(s, row)
            item = {
                "sheet": s["sheetName"],
                "row": row_idx,
                "name": name,  # as written in THIS sheet, used to verify the row later
                "label": label,
                "message": message,
                "followUp": s.get("followUp"),
                "nameCol": s["nameColumnIndex"],
                "paidCol": s["paidColumnIndex"],
                "notifiedCol": s.get("notifiedColumnIndex"),
            }
            if message_value is not None:
                item["messageValue"] = message_value
                item["messageValueCol"] = s["messageValueColumnIndex"]
            person["items"].append(item)

    added, merged, stale = merge_people_into_queue(queue, people, grouped_template, variables)

    save_queue(queue)
    print(f"Queued {len(added)} new message(s) in {QUEUE_PATH}.")
    if merged:
        print("Added new items to existing pending entries for:", ", ".join(merged))
    if stale:
        print("These entries had hand-edited text or follow-ups, so their wording was NOT updated "
              "to mention the new items (edit it yourself or delete the entry):",
              ", ".join(stale))
    if missing:
        print("No phone number for (not queued):", ", ".join(missing))


def show():
    pending = [e for e in load_queue() if e["status"] == "pending"]
    print(f"{len(pending)} pending:")
    for e in pending:
        sheets = ", ".join(i["sheet"] for i in e["items"])
        print(f"  {e['name']} -> {e['number']} [{sheets}]\n      {e['text']}")
        for follow_up in e.get("followUps", []):
            print(f"      + then: {follow_up}")


def send(config):
    queue = load_queue()
    pending = [e for e in queue if e["status"] == "pending"]
    if not pending:
        print("Nothing pending.")
        return

    gc = gspread.service_account(filename=config["credentialsFile"])
    worksheets = open_sheets(config, gc)
    delay = config.get("sendDelaySeconds", 2)
    grouped_template = config.get("groupedMessage", DEFAULT_GROUPED)
    variables = message_variables(config)

    client = NewClient(
        name="payments-reminder-bot",
        props=DeviceProps(os="XC Payments Bot", platformType=DeviceProps.DESKTOP),
    )

    @client.event(ConnectedEv)
    def on_connected(client: NewClient, event: ConnectedEv):
        print(f"Connected. Sending {len(pending)} queued message(s)...")
        for e in pending:
            try:
                # The queue may be days old, so re-check every live row before sending.
                live = []  # (item, row values) for debts that are still owed
                for item in e["items"]:
                    row = worksheets[item["sheet"]].row_values(item["row"])
                    if cell(row, item["nameCol"]).strip() != item["name"]:
                        print(f"  note: {e['name']} / {item['sheet']}: row {item['row']} "
                              f"no longer matches, dropping that item")
                    elif is_paid(cell(row, item["paidCol"])):
                        print(f"  note: {e['name']} has paid for {item['sheet']} since queueing")
                    else:
                        sheet_config = next(
                            s for s in config["sheets"] if s["sheetName"] == item["sheet"]
                        )
                        if refresh_item_message(item, sheet_config, row):
                            item["messageConfigChanged"] = True
                        live.append((item, row))

                message_config_changed = any([
                    item.pop("messageConfigChanged", False) for item, _ in live
                ])
                if not live:
                    e["status"] = "skipped_nothing_due"
                    print(f"  SKIPPED {e['name']}: nothing left to chase")
                else:
                    text = e["text"]
                    follow_ups = e.get("followUps", [])
                    if len(live) != len(e["items"]) or message_config_changed:
                        # The queued wording no longer matches what they owe, so rewrite it.
                        # (This replaces any hand edits made to this entry's text/follow-ups.)
                        live_items = [i for i, _ in live]
                        text = compose(e["name"], live_items, grouped_template, variables)
                        follow_ups = compose_followups(e["name"], live_items, variables)
                        print(f"  note: rewrote message for {e['name']} (items changed)")

                    client.send_message(build_jid(e["number"]), text)
                    e["status"] = "sent"
                    e["text"] = text
                    e["followUps"] = follow_ups
                    e["sentAt"] = datetime.now().isoformat(timespec="seconds")
                    print(f"  sent -> {e['name']} ({len(live)} item(s))")

                    # Follow-ups are separate messages. A failure here must not mark the
                    # entry failed: the main message already went out and re-sending it
                    # would duplicate it.
                    for follow_up in follow_ups:
                        time.sleep(config.get("followUpDelaySeconds", 1))
                        try:
                            client.send_message(build_jid(e["number"]), follow_up)
                            print(f"  sent follow-up -> {e['name']}")
                        except Exception as err:
                            e["followUpError"] = str(err)
                            print(f"  WARNING: main message sent to {e['name']}, "
                                  f"but a follow-up failed: {err}")
                            break

                    for item, row in live:
                        if item["notifiedCol"] is None:
                            continue
                        try:
                            # Increment from the live value, not the one seen at queue time.
                            new_count = get_count(cell(row, item["notifiedCol"])) + 1
                            worksheets[item["sheet"]].update_cell(
                                item["row"], item["notifiedCol"] + 1, new_count
                            )
                        except Exception as err:
                            print(f"  WARNING: sent, but couldn't update {item['sheet']} "
                                  f"count for {e['name']}: {err}")
            except Exception as err:
                e["status"] = "failed"
                e["error"] = str(err)
                print(f"  FAILED {e['name']}: {err}")

            save_queue(queue)  # persist after every person so a crash loses nothing
            time.sleep(delay)

        print("Done.")
        client.stop()

    client.connect()


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "build":
        build(load_config())
    elif command == "show":
        show()
    elif command == "send":
        send(load_config())
    else:
        print(__doc__)