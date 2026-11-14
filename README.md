# Treasurer Bot: WhatsApp Payment Reminders

A Python tool that automates payment chasing for a university cross-country (XC) running club. It reads a Google Sheet, finds members who haven't paid, sends them a WhatsApp reminder through [`neonize`](https://github.com/krypton-byte/neonize), and increments a notification counter in the sheet so nobody gets reminded forever.

Can be run as a plain script but can also be configured to run on an always-on raspberry pi

---

## Table of Contents

- [How it works](#how-it-works)
- [Project structure](#project-structure)
- [Requirements](#requirements)
- [Setup](#setup)
- [Configuration](#configuration)
- [Running](#running)
- [Deploying to a Raspberry Pi](#deploying-to-a-raspberry-pi)
- [Known gotchas](#known-gotchas)
- [Security](#security)
- [Roadmap](#roadmap)

---

## How it works

1. Connects to Google Sheets with a service account (`gspread`).
2. Reads the member list and identifies rows where the "paid" column is not marked.
3. Looks up each unpaid member's number in the `phoneBook` config.
4. Sends a WhatsApp reminder via `neonize`.
5. Increments that member's notification counter in the sheet. Members who have reached `maxNotifications` are skipped.

### Evolution of the project

The tool has been built up in three stages:

1. **Local script** (`main_local.py`): reads and writes a local `.xlsx` file.
2. **Google Sheets script** (`main_gspread.py`): same logic, but against a shared Google Sheet via a service account.
3. **Web app** (`treasurer_app/`): a FastAPI backend wrapping the shared logic, plus a PWA frontend that can be added to an Android home screen and tapped to send reminders.

### The WhatsApp connection

`neonize` uses WhatsApp's multi-device protocol and connects directly to WhatsApp's servers, so:

- The phone app does **not** need to stay open.
- The linked phone **must** still connect to WhatsApp at least once every 14 days, or WhatsApp will unlink the device.

The bot appears under Linked Devices as **"XC Payments Bot"** (`platformType=DeviceProps.DESKTOP`). Its session is stored in `session.db`.

---

## Project structure

```
treasurer-bot/
├── main_gspread.py           # Script version (Google Sheets)
├── main_local.py             # Script version (local .xlsx)
├── config_payments.json      # Bot configuration
├── credentials.json          # Google service account key (DO NOT COMMIT)
└── session.db                # WhatsApp session (DO NOT COMMIT)
```

## Requirements

- Python 3.9+
- A Google Cloud service account with access to your spreadsheet
- A WhatsApp account to link the bot to
- Python packages:
  - `neonize` (developed against `0.4.3.post0`)
  - `gspread`
  - `fastapi` and an ASGI server such as `uvicorn` (web app only)

---

## Setup

```bash
git clone <your-repo-url>
cd treasurer-bot
pip install neonize==0.4.3.post0 gspread fastapi uvicorn
```

> **Windows note:** if you have multiple Python installs (e.g. Anaconda and Miniforge), call pip through the full interpreter path so packages go to the right environment:
>
> ```
> C:\Users\<you>\anaconda3\python.exe -m pip install neonize gspread
> ```

### Google Sheets access

1. Create a service account in Google Cloud and download its JSON key as `credentials.json`.
2. Share your spreadsheet with the service account's email address.
3. Put the spreadsheet ID and key filename in `config_payments.json`.

### Linking WhatsApp (first run only)

On first launch, `neonize` prints a QR code. Scan it in WhatsApp under **Settings → Linked Devices → Link a Device**. The session is saved to `session.db`, so you won't need to scan again unless the device gets unlinked.

---

## Configuration

Settings live in `config_payments.json`:

| Field | Description |
|-------|-------------|
| `spreadsheetId` | ID of the Google Sheet |
| `credentialsFile` | Path to the service account key (e.g. `credentials.json`) |
| `nameColumnIndex` | Column index containing member names |
| `paidColumnIndex` | Column index indicating whether the member has paid |
| `notifiedColumnIndex` | Column index holding the reminder counter |
| `maxNotifications` | Maximum reminders sent per member |
| `phoneBook` | Map of member name → phone number |

Phone numbers use international format with **no `+` and no spaces**. Example (values are placeholders):

```json
{
  "spreadsheetId": "YOUR_SHEET_ID",
  "credentialsFile": "credentials.json",
  "nameColumnIndex": 1,
  "paidColumnIndex": 3,
  "notifiedColumnIndex": 4,
  "maxNotifications": 3,
  "phoneBook": {
    "Alex Example": "447700900123"
  }
}
```
Names in `phoneBook` need to match the names in the sheet exactly, otherwise a member can't be matched to a number.

There is also an optional groupedMessage parameter, with an optional followUp parameter:

```json
{
  "groupedMessage": "default message",
    "sheets": [
    {"sheetName": "X", "nameColumnIndex": 0, "paidColumnIndex": 1,
     "notifiedColumnIndex": 2, "maxNotifications": 5,
     "message": "hi {name}, your payment for X is still outstanding. please pay as soon as you can here: {link}\n thanks !"},
    {"sheetName": "Y", "nameColumnIndex": 0, "paidColumnIndex": 2,
     "notifiedColumnIndex": 3, "maxNotifications": 5,
     "message": "hi {name}, your payment for Y is still outstanding !",
     "followUp": "hi {name}, your payment for Y is still outstanding, but is no longer avaliable online. please pay through an alternative method"},
}
```
This groups the messages together for people who haven't paid on multiple sheets.
The followUp parameter sends a follow up message to keep messages concise.

---

## Running

**Script only:**

```bash
python main_gspread.py
```

---

## Deploying to a Raspberry Pi

1. SSH into the Pi (the user for this project is `rpbot`).
2. Copy the project over with WinSCP or `scp`, and make sure `treasurer_app/` ends up in the directory you expect.
3. Install the dependencies on the Pi.
4. Link WhatsApp once by scanning the QR code.
5. Run the app as a service (for example with `systemd`) so it starts on boot and restarts on failure.

---

## Known gotchas

These came up with `neonize` `0.4.3.post0`. Check the installed package rather than relying on the docs:

- `NewClient()` does **not** accept a `database=` keyword argument.
- `send_message()` takes the message as a plain positional argument, not `text=`.
- Use `client.stop()` to shut down, not `sys.exit(0)`.
- The linked phone must check in with WhatsApp at least every 14 days.

---

## Security

Never commit these files:

- `credentials.json` (Google service account key)
- `session.db` (WhatsApp session; grants access to the linked account)
- `config_payments.json`, if it contains real members' phone numbers

Add them to `.gitignore`.

---

## Roadmap

- [ ] Confirm `treasurer_app/` is deployed in the right place on the Pi
- [ ] Run the web app as a `systemd` service
