# CV Application Pipeline

An AI-powered job-application assistant. Give it a job posting — as a URL or
pasted text — and it returns a tailored CV plus two cover letters, emails them
to you, and logs the application to a Google Sheet. The whole thing can be
triggered from an iOS share sheet while your laptop is off, with the work
running on a small cloud server.

The pipeline **prepares** materials; it never auto-submits applications. You
stay in control of what actually gets sent.

## What it does

```
job URL  ─┐
          ├─►  scrape / parse  ─►  analyze fit vs. your CV  ─►  tailor CV (ATS-aware)
job text ─┘                                                 ├─►  formal cover letter
                                                            └─►  casual cover letter
                                                                      │
                              email (CV PDF + both letters)  ◄────────┤
                              Google Sheet row (tracking)    ◄─────────┘
```

1. **Ingest** a posting from a URL (scraped with Playwright/httpx) or from text
   pasted straight from your phone — useful when a site anti-bots cloud IPs.
2. **Analyze** the posting against your master CV in one shared Claude call,
   extracting keywords, emphasis areas, language (auto-detects EN/DE), and tone.
3. **Tailor** the CV (honest, ATS-aware rewriting — no invented experience) and
   write **two** cover letters in parallel: a formal one and a casual one.
4. **Render** the tailored CV to PDF.
5. **Deliver**: email everything to you (via Resend HTTP API) and append a row
   to a Google Sheet tracker.

## Architecture — the WAT framework

The codebase separates **probabilistic reasoning** from **deterministic
execution** so the system stays reliable:

- **Workflows** (`workflows/*.md`) — plain-language SOPs describing each task.
- **Agent** — the orchestrator. `tools/run_apply_pipeline.py` runs the stages
  in order, parallelizing independent ones with `asyncio`.
- **Tools** (`tools/*.py`) — single-purpose, testable scripts that do the actual
  work (scrape, analyze, tailor, render, email, log).

A FastAPI webhook (`tools/webhook_server.py`) exposes the pipeline so an iOS
Shortcut can trigger a run over HTTPS. In production it runs on a DigitalOcean
droplet behind Caddy (automatic TLS), managed by systemd.

## Tech stack

| Area | Tools |
|---|---|
| Language | Python 3.11+, `asyncio` |
| AI | Anthropic Claude (with prompt caching) |
| Scraping | Playwright, httpx, BeautifulSoup |
| PDF | PyMuPDF |
| Web service | FastAPI, Uvicorn |
| Email | Resend HTTP API (SMTP-free, works on cloud hosts) |
| Tracking | Google Sheets API (`gspread` + `google-auth`) |
| Infra | DigitalOcean, Caddy (Let's Encrypt TLS), systemd |
| Trigger | iOS Shortcuts (share sheet → webhook) |

## Quick start (local)

```bash
git clone https://github.com/samirr7sidawi-blip/cv-application-pipeline.git
cd cv-application-pipeline

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp deploy/.env.template .env   # then fill in your own keys
python tools/run_apply_pipeline.py --job-url "https://example.com/job/123"
# or, for sites that block scraping:
python tools/run_apply_pipeline.py --job-text "paste the job description here"
```

You supply your own API keys (Anthropic, Resend, Google service account) in
`.env` — see `deploy/.env.template` for the full list. Nothing sensitive is
committed; `.env`, credentials, and generated files are gitignored.

## Cloud deployment

`deploy/` contains everything to run the webhook on a droplet:
`install.sh` (system deps + Playwright), `cv_agent.service` (hardened systemd
unit), and a `Caddyfile` for TLS termination. See [deploy/README.md](deploy/README.md).

## Notes

This is a personal automation project, shared as a portfolio piece. It assumes
a FlowCV master résumé and is tuned for the author's job search, but the WAT
structure makes each tool reusable in isolation.
