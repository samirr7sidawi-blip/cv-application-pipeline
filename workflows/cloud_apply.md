# Workflow: Cloud-hosted apply pipeline (phone-triggered)

This is the deployment + day-to-day-usage workflow for running the apply pipeline
from your phone, without your Mac being on. The local workflow at
[apply_to_job.md](apply_to_job.md) still works for testing on the Mac.

## How it works at a glance

```
At work → phone receives a LinkedIn/Xing/StepStone/Indeed/Stellenwerk alert email
   ↓ tap an interesting job posting → iOS share sheet
   ↓ "Apply with CV Agent" shortcut → POSTs URL to droplet webhook
   ↓ droplet runs apply pipeline → emails tailored CV + cover letter
   ↓ Google Sheet row appears
```

You read alerts on your phone naturally (your eyes filter), the agent prepares
the CV/letter for any job you flag, and you decide whether to submit on the
platform itself.

## One-time setup

See [deploy/README.md](../deploy/README.md) for the full step-by-step. Summary:

1. Spin up DigitalOcean droplet (Ubuntu 22.04, Frankfurt, $6/mo — covered by Student Pack credit)
2. Register a free subdomain at duckdns.org pointing at the droplet IP
3. SSH to droplet, clone repo, fill `/etc/cv_agent.env`, run `bash deploy/install.sh`
4. Point Caddy at the subdomain; reload
5. Prime the FlowCV cache once: `read_cv_flowcv.py --refresh`
6. Configure the iOS Shortcut

## Day-to-day usage

**On your phone:**
1. Job alert email arrives in Gmail (LinkedIn/Xing/StepStone/Indeed/Stellenwerk)
2. Tap the job link to open the posting
3. Share sheet → **Apply with CV Agent**
4. Wait ~60s for the tailored CV + cover letter email

**That's it.** No SSH, no laptop, nothing else.

## When you edit your FlowCV

SSH to the droplet:
```bash
sudo -u cvagent /opt/cv_agent/.venv/bin/python /opt/cv_agent/tools/read_cv_flowcv.py --refresh
```

This re-pulls the master HTML cache. From the next pipeline run onwards, the
tailoring uses the updated CV. You don't need to restart the service.

## When you change pipeline code locally

```bash
ssh root@<droplet-ip>
cd /opt/cv_agent
git pull
sudo -u cvagent /opt/cv_agent/.venv/bin/pip install -r requirements.txt   # if requirements changed
systemctl restart cv_agent.service
```

## Watching logs

```bash
journalctl -u cv_agent.service -f       # uvicorn + webhook
ls -lt /var/log/cv_agent/ | head        # per-job pipeline logs (newest first)
tail -f /var/log/cv_agent/<job_id>.log  # follow a specific job
```

## Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Shortcut shows "Server returned 403" | Token mismatch | Re-paste `WEBHOOK_TOKEN` from `/etc/cv_agent.env` into the Shortcut header |
| Shortcut hangs / no notification | Caddy or droplet down | `curl https://<sub>.duckdns.org/health` from your laptop — should return `{"status":"ok"}` |
| No email arrives | Pipeline crashed | Check `/var/log/cv_agent/<job_id>.log` — usually FlowCV session expired or scrape_job failed |
| Pipeline log shows "FlowCV login failed" | Session expired or password changed | Re-run `read_cv_flowcv.py --refresh` to re-authenticate |
| FastAPI returns 422 | Body shape wrong | iOS Shortcut must send JSON `{"job_url": "..."}` not form data |

## How this differs from the local workflow

| Concern | Local ([apply_to_job.md](apply_to_job.md)) | Cloud (this doc) |
|---|---|---|
| Orchestration | Run interactively, stage by stage | `tools/run_apply_pipeline.py` runs deterministically via asyncio |
| Trigger | User pastes URL in chat | iOS Shortcut → HTTPS POST |
| Where it runs | User's Mac, must be awake | DigitalOcean droplet, always on |
| Cover letter review | `--review` opt-in (interactive APPROVE) | Always auto (no terminal to type into) |
| FlowCV cache | `.tmp/cv_master.html` on Mac | `.tmp/cv_master.html` on droplet |
| Email + Sheet output | Same SMTP / Sheets API calls | Same SMTP / Sheets API calls |

Both workflows share all 9 tools in `tools/`. The cloud workflow is a thin
deployment layer on top — no business logic differs.

## What this workflow does NOT do

- It does not auto-submit applications. You apply manually on the platform.
- It does not aggregate or dedupe job postings (you read your own alert emails)
- It does not retry on failure (one shot per webhook call). Re-trigger from the Shortcut if needed.

## Future tweaks (not yet built)

- Push-notification feedback (Pushover/ntfy.sh) when the pipeline finishes, instead of waiting for the email
- Migrate from a self-hosted droplet to Anthropic Routines if/when the user confirms a Pro plan — `run_apply_pipeline.py` ports as-is
