"""
Append one row to the "Job Applications" Google Sheet.
On first run (GOOGLE_SHEETS_ID empty), creates the sheet and writes its ID back to .env.

Usage:
    python3 tools/log_to_sheets.py

Requires:
    GOOGLE_CREDENTIALS_FILE, GOOGLE_SHEETS_ID (can be blank), GMAIL_EMAIL in .env
    .tmp/job_description.json
    .tmp/tailored_cv_sections.json
    .tmp/cover_letter_final.txt

Output:
    One row appended to Google Sheets.
    GOOGLE_SHEETS_ID written to .env if it was empty.
"""

import json
import os
from datetime import date
from dotenv import load_dotenv, set_key
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "")
SHEET_OWNER_EMAIL = os.getenv("SHEET_OWNER_EMAIL", os.getenv("GMAIL_EMAIL", ""))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column layout MATCHES the OneDrive Excel tracker (log_to_excel.py) exactly,
# so the Google Sheet is a 1:1 mirror that works on the cloud droplet (where
# OneDrive isn't available).
HEADERS = [
    "Company",
    "Position",
    "Date Applied",
    "Status",
    "CV Version",
    "Job Description",
    "Job Link",
    "Interview Notes",
    "Cover Letter",
]

STATUS_APPLIED = "🟡 applied"


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_text(path: str, default: str = "") -> str:
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def get_client() -> gspread.Client:
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def gmail_search_url(title: str = "", company: str = "") -> str:
    """Build a Gmail search URL keyed on the COMPANY name.
    Uses the `#search/` hash fragment, which the Gmail desktop web UI honors.
    Caveat: Gmail mobile apps don't execute hash-fragment searches — that
    limitation is solved separately via Google Drive uploads (the Drive link
    works the same on laptop and phone)."""
    from urllib.parse import quote
    if not company:
        return ""
    return "https://mail.google.com/mail/u/0/#search/" + quote(f'subject:"{company}"')


def hyperlink_formula(url: str, display: str) -> str:
    """Wrap a URL in a Google Sheets HYPERLINK formula so the cell renders as
    a clickable link. The sheet must be written with value_input_option=USER_ENTERED."""
    if not url:
        return ""
    url_safe = url.replace('"', '""')
    display_safe = display.replace('"', '""')
    return f'=HYPERLINK("{url_safe}", "{display_safe}")'


def get_or_create_sheet(gc: gspread.Client) -> gspread.Worksheet:
    global GOOGLE_SHEETS_ID

    if GOOGLE_SHEETS_ID:
        # Explicit ID was provided — don't silently fall back to create-new,
        # which would just produce a Drive-quota error and hide the real issue
        # (almost always: the sheet wasn't shared with the service account).
        try:
            sh = gc.open_by_key(GOOGLE_SHEETS_ID)
            print(f"Opened existing sheet: {sh.title}")
        except Exception as e:
            sa_email = ""
            try:
                import json as _json
                with open(GOOGLE_CREDENTIALS_FILE, "r") as _f:
                    sa_email = _json.load(_f).get("client_email", "")
            except Exception:
                pass
            raise SystemExit(
                f"\nCannot open sheet {GOOGLE_SHEETS_ID}: {type(e).__name__}\n\n"
                f"Most likely the sheet is not shared with the service account.\n"
                f"Open the sheet in your browser, click Share, and add this email as Editor:\n"
                f"  {sa_email or '(see client_email in credentials.json)'}\n"
            )
    else:
        # Service accounts have 0 Drive storage and cannot own files, so we
        # cannot programmatically create a new sheet. The user must create one
        # manually in their own Drive and paste the ID into .env.
        sa_email = ""
        try:
            import json as _json
            with open(GOOGLE_CREDENTIALS_FILE, "r") as _f:
                sa_email = _json.load(_f).get("client_email", "")
        except Exception:
            pass
        raise SystemExit(
            "\nGOOGLE_SHEETS_ID is blank. Service accounts can't create new sheets.\n"
            "Create a sheet manually:\n"
            "  1. https://sheets.google.com (logged in as the account you want to own the sheet)\n"
            "  2. + Blank → name it 'Job Applications' → copy the long ID from the URL\n"
            "  3. Share the sheet (Editor role) with this service account:\n"
            f"     {sa_email or '(see client_email in credentials.json)'}\n"
            "  4. Paste the ID into GOOGLE_SHEETS_ID in .env, save, re-run.\n"
        )

    try:
        ws = sh.worksheet("Applications")
    except gspread.WorksheetNotFound:
        ws = sh.get_worksheet(0)
        ws.update_title("Applications")

    return ws


def ensure_headers(ws: gspread.Worksheet) -> None:
    existing = ws.row_values(1)
    if existing != HEADERS:
        # Overwrite row 1 in place rather than inserting — keeps the row order
        # stable if migrate_excel_to_sheets.py just populated the sheet.
        ws.update(values=[HEADERS], range_name="A1")
        print("Headers (re)written to sheet")


NOTE_LABEL_DESCRIPTION = "📄 View description"
NOTE_LABEL_COVER_LETTER = "📄 View letter"
# Sheets cell-note hard limit is 50k chars; we stay under to leave headroom.
NOTE_MAX = 30000


def apply_compact_formatting(ws: gspread.Worksheet) -> None:
    """For each data row, move long Job Description / Cover Letter text from
    the cell VALUE into the cell NOTE (the little popup that appears when you
    tap/click the cell), and replace the cell value with a short label like
    `📄 View description`. Result: sheet stays compact; clicking a cell reveals
    the full content in the note popup. Idempotent — cells already converted
    to label form are skipped on subsequent runs."""
    sheet_id = ws.id
    desc_idx = HEADERS.index("Job Description")
    cl_idx = HEADERS.index("Cover Letter")

    # Set narrow column widths first so the labels look tidy.
    width_requests = []
    for idx in (desc_idx, cl_idx):
        width_requests.append({
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "COLUMNS",
                    "startIndex": idx,
                    "endIndex": idx + 1,
                },
                "properties": {"pixelSize": 180},
                "fields": "pixelSize",
            }
        })
    if width_requests:
        ws.spreadsheet.batch_update({"requests": width_requests})

    # Now sweep every data row and convert text → note where needed.
    all_rows = ws.get_all_values()
    requests = []
    for row_idx_0, row in enumerate(all_rows[1:], start=1):  # skip header
        def _maybe_swap(col_idx: int, label: str) -> None:
            if col_idx >= len(row):
                return
            value = row[col_idx]
            if not value or value.startswith("📄"):
                return  # already a label, nothing to do
            requests.append({
                "updateCells": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_idx_0,
                        "endRowIndex": row_idx_0 + 1,
                        "startColumnIndex": col_idx,
                        "endColumnIndex": col_idx + 1,
                    },
                    "rows": [{
                        "values": [{
                            "userEnteredValue": {"stringValue": label},
                            "note": value[:NOTE_MAX],
                        }]
                    }],
                    "fields": "userEnteredValue,note",
                }
            })

        _maybe_swap(desc_idx, NOTE_LABEL_DESCRIPTION)
        _maybe_swap(cl_idx, NOTE_LABEL_COVER_LETTER)

    if requests:
        ws.spreadsheet.batch_update({"requests": requests})


def main():
    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        print(f"Error: credentials file not found at '{GOOGLE_CREDENTIALS_FILE}'")
        print("Create a service account JSON from Google Cloud Console and set GOOGLE_CREDENTIALS_FILE in .env")
        raise SystemExit(1)

    job = load_json(".tmp/job_description.json")
    cover_letter = load_text(".tmp/cover_letter_final.txt", default="")

    today = date.today().strftime("%Y-%m-%d")
    email_link = hyperlink_formula(
        gmail_search_url(job.get("title", ""), job.get("company", "")),
        "📧 Open email",
    )

    row = [
        job.get("company", ""),
        job.get("title", ""),
        today,
        STATUS_APPLIED,
        email_link,
        (job.get("description", "") or "")[:32000],
        job.get("url", "") or job.get("apply_url", ""),
        "",
        cover_letter,
    ]

    gc = get_client()
    ws = get_or_create_sheet(gc)
    ensure_headers(ws)
    ws.append_row(row, value_input_option="USER_ENTERED")
    apply_compact_formatting(ws)

    sheet_id = os.getenv("GOOGLE_SHEETS_ID", GOOGLE_SHEETS_ID)
    print(f"Application logged to Google Sheets")
    print(f"  Row: {job.get('title')} at {job.get('company')} — {today}")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{sheet_id}")


if __name__ == "__main__":
    main()
