# Workflow: Apply to Job

## Objective
Given one job URL from the user, produce a tailored CV PDF and cover letter,
email both to the configured recipient (EMAIL_RECIPIENT in .env), log the application to Google Sheets,
and report the direct application link back to the user.

The user applies to jobs themselves — this pipeline does NOT submit applications.
The goal is to be FAST so the user can decide which jobs to send.

## Required Inputs
- `JOB_URL` — provided by the user in chat

## Pre-flight Checks
Before running any steps, verify:
1. `.env` has all values set: `ANTHROPIC_API_KEY`, `FLOWCV_EMAIL`, `FLOWCV_PASSWORD`,
   `GMAIL_EMAIL`, `GMAIL_APP_PASSWORD`, `GOOGLE_CREDENTIALS_FILE`
   (`GOOGLE_SHEETS_ID` may be blank — it's created on first run)
2. `.tmp/` directory exists (create with `mkdir -p .tmp`)
3. `credentials.json` exists at the path in `GOOGLE_CREDENTIALS_FILE`
4. If any required value is missing, stop and ask the user to fill it in

---

## Architecture (parallelized)

```
┌────────────────────────────────────────────────────┐
│ STAGE 1 — PARALLEL                                 │
│   tools/scrape_job.py "<JOB_URL>"                  │
│   tools/read_cv_flowcv.py                          │  ← skipped if cv_master.html cached
└─────────────────────┬──────────────────────────────┘
                      ▼
            ┌──────────────────────┐
            │ STAGE 2 — SEQUENTIAL │
            │ extract_job_relevance│  ← one small Claude call
            └──────────┬───────────┘
                       ▼
┌────────────────────────────────────────────────────┐
│ STAGE 3 — PARALLEL                                 │
│   tools/tailor_cv_claude.py                        │
│   tools/generate_cover_letter.py    (auto mode)    │
└─────────────────────┬──────────────────────────────┘
                      ▼
            ┌──────────────────────┐
            │ STAGE 4 — SEQUENTIAL │
            │ build_tailored_pdf   │
            └──────────┬───────────┘
                       ▼
┌────────────────────────────────────────────────────┐
│ STAGE 5 — PARALLEL                                 │
│   tools/send_email.py                              │
│   tools/log_to_sheets.py                           │
└────────────────────────────────────────────────────┘
```

**Implementation note for the agent:** launch each parallel stage as multiple
Bash tool calls in a single message — the harness runs them concurrently and
waits for all to complete before the next stage starts.

---

## Stage 1 — Scrape Job + Read CV (PARALLEL)

Launch both commands in parallel:

```bash
python3 tools/scrape_job.py "<JOB_URL>"
python3 tools/read_cv_flowcv.py
```

**Outputs:** `.tmp/job_description.json`, `.tmp/cv_raw.json`, `.tmp/cv_master.html`

**Cache behavior:** `read_cv_flowcv.py` skips the FlowCV login entirely if
`.tmp/cv_master.html` already exists (typical fast path — runs in <1s). The user
must run `python3 tools/read_cv_flowcv.py --refresh` themselves after editing
their FlowCV.

**On scrape failure:** Ask the user: "I couldn't scrape that URL. Can you paste the job description text?"
Then save it manually:
```python
import json
data = {"title": "<ask user>", "company": "<ask user>", "url": JOB_URL, "description": "<pasted text>", "apply_url": JOB_URL}
with open(".tmp/job_description.json", "w") as f: json.dump(data, f)
```

**On read_cv_flowcv failure with no cache:** Ask the user to paste their CV text; save as `.tmp/cv_raw.json` with `{"raw_text": "..."}`.

---

## Stage 2 — Extract Job Relevance (SEQUENTIAL)

```bash
python3 tools/extract_job_relevance.py
```

**Output:** `.tmp/job_relevance.json` — language, keywords, emphasis areas, company hooks, tone hints

This is a small, fast Claude call (~3–8s). Its output is the **shared source of truth** consumed by the two parallel agents in Stage 3, so they stay aligned on language and keywords.

**On failure:** Retry once. If it still fails, skip — both downstream tools fall back to reading `.tmp/job_description.json` directly.

---

## Stage 3 — Tailor CV + Cover Letter (PARALLEL)

Launch both commands in parallel:

```bash
python3 tools/tailor_cv_claude.py
python3 tools/generate_cover_letter.py
```

**Outputs:** `.tmp/tailored_cv_sections.json`, `.tmp/cover_letter_final.txt`

Both tools read `.tmp/job_relevance.json` and produce aligned outputs. The cover letter
runs in **auto mode by default** (no interactive APPROVE pause). Pass `--review`
to `generate_cover_letter.py` if the user explicitly wants to review/revise the draft
before email goes out.

**On tailor failure:** Retry once. If it fails again, save an empty JSON `{}` and
continue — the PDF will be exported without changes.

**On cover letter failure:** Retry once. If it fails, abort the run.

---

## Stage 4 — Build Tailored PDF (SEQUENTIAL)

```bash
python3 tools/build_tailored_pdf.py
```

**Output:** `.tmp/tailored_cv.pdf`

This is the local-cache HTML editor — it consumes `.tmp/cv_master.html` and
`.tmp/tailored_cv_sections.json`, applies whole-bullet replacements + manual
edits, and renders the final PDF offline (no FlowCV trip).

**On failure:** If the script can't find the cache, ask the user to run
`python3 tools/read_cv_flowcv.py --refresh` to repopulate it.

---

## Stage 5 — Send Email + Log to Sheets + Log to Excel (PARALLEL)

Launch all three commands in parallel:

```bash
python3 tools/send_email.py
python3 tools/log_to_sheets.py
python3 tools/log_to_excel.py
```

**Outputs:** Email sent; row appended to "Job Applications" Google Sheet; row appended to OneDrive `job applications tracker.xlsx`.

**Note:** Excel logging only works on the Mac (OneDrive sync path). On the cloud droplet, this command fails harmlessly — Google Sheets remains the cloud tracker.

**send_email.py failure modes:**
- `SMTPAuthenticationError`: Gmail App Password is wrong. Generate at https://myaccount.google.com/apppasswords (requires 2FA)
- `SMTPConnectError`: Network issue; retry once

**log_to_sheets.py first run:** Creates the sheet, shares it with your configured Google account, writes Sheet ID to `.env`.

**log_to_sheets.py failure modes:**
- `credentials.json` missing: create service account at console.cloud.google.com, enable Sheets API + Drive API, download JSON key, save as `credentials.json`
- `Permission denied on sheet`: service account not shared as editor; re-run to recreate

---

## Stage 6 — Report to User

After all stages complete, report:
1. The direct application URL from `.tmp/job_description.json["apply_url"]`
2. The Google Sheet URL: `https://docs.google.com/spreadsheets/d/<GOOGLE_SHEETS_ID>`
3. Confirm: "Email sent with tailored CV and cover letter."

---

## Edge Cases

| Situation | What to do |
|---|---|
| LinkedIn login wall | Scraper handles it automatically. If it still fails, user pastes description |
| Workday multi-step apply | apply_url is the job page itself — user applies manually from there |
| FlowCV session expired during --refresh | re-authenticates automatically |
| FlowCV DOM changed | Dump page HTML, ask Claude for updated selector, update the tool, document it here |
| User edited FlowCV recently | They must run `read_cv_flowcv.py --refresh` to update the cache |
| Gmail "Less secure app" error | Must use App Password, not regular Gmail password |
| Google Sheet quota exceeded | Quota is 300 writes/minute — far above our use; if hit, wait 60s and retry |
| Job description too short | Ask user to verify the URL is a public job posting (not behind a login) |

---

## Known Quirks (update as discovered)
- FlowCV editor requires a click before keyboard input registers; `fill_contenteditable()` handles this
- FlowCV PDF export sometimes opens a new tab; `expect_download()` handles this
- LinkedIn job URLs often redirect — pass the original URL, Playwright follows redirects
- Greenhouse "apply" links sometimes use relative paths — `scrape_job.py` resolves them to absolute
- `tools/update_cv_flowcv.py` is legacy — `build_tailored_pdf.py` handles all CV mutation locally now

---

## One-Time Setup Checklist

Run this once before the first job application:

```bash
# Install dependencies
pip3 install -r requirements.txt
playwright install chromium

# Fill in .env
#   ANTHROPIC_API_KEY  — from console.anthropic.com
#   FLOWCV_EMAIL / FLOWCV_PASSWORD — your FlowCV login
#   GMAIL_APP_PASSWORD — from myaccount.google.com/apppasswords
#   GOOGLE_CREDENTIALS_FILE — path to service account JSON (default: credentials.json)

# Prime the FlowCV cache (does the one slow login)
python3 tools/read_cv_flowcv.py --refresh
cat .tmp/cv_raw.json

# Test scraper with a known Greenhouse URL
python3 tools/scrape_job.py "https://boards.greenhouse.io/example/jobs/12345"
```
