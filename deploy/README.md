# Droplet setup — CV Agent webhook

End-to-end recipe to host the apply pipeline on a DigitalOcean droplet so it
can be triggered from your phone while your Mac is off.

## What you'll have when done

- DigitalOcean droplet running 24/7 in Frankfurt
- HTTPS webhook at `https://<your-subdomain>.duckdns.org/apply`
- iOS Shortcut on your phone that POSTs a job URL to the webhook
- The apply pipeline (CV tailor + cover letter + email + Sheets) runs on the droplet
- You get the email with the tailored CV + cover letter in ~60 seconds

## Cost

- Droplet: $6/month (free for ~33 months with your **GitHub Student Pack $200 DigitalOcean credit**)
- duckdns subdomain: free
- TLS cert via Caddy + Let's Encrypt: free

---

## Step 1 — Spin up the droplet

1. Apply for the GitHub Student Pack credit at https://education.github.com/pack — DigitalOcean grants $200 valid 12 months.
2. Create droplet:
   - Image: **Ubuntu 22.04 LTS** (or newer)
   - Plan: **Basic, Regular intel, $6/month** (1 GB RAM, 1 vCPU, 25 GB SSD)
   - Region: **Frankfurt (FRA1)** — closest to Dortmund, lowest latency
   - Authentication: **SSH key** (paste your public key)
   - Hostname: `cv-agent`
3. Note the droplet's public IPv4 address.

## Step 2 — Register a free subdomain at duckdns.org

1. Go to https://www.duckdns.org and sign in with Google/GitHub.
2. Create a subdomain, e.g. `cvagent-samir.duckdns.org`.
3. Set its IP to your droplet's public IPv4.
4. Wait ~30s for DNS to propagate. Test:
   ```
   dig +short cvagent-samir.duckdns.org
   ```
   Should return your droplet's IP.

## Step 3 — Clone the repo and configure secrets

SSH into the droplet:
```bash
ssh root@<droplet-ip>
```

Clone:
```bash
mkdir -p /opt && cd /opt
git clone https://github.com/<your-user>/cv_agent.git
cd cv_agent
```
(If your repo is private, set up an SSH deploy key first or use a personal access token.)

Copy and fill in env:
```bash
cp deploy/.env.template /etc/cv_agent.env
nano /etc/cv_agent.env
```

Fill in every value. Generate the webhook token first:
```bash
openssl rand -hex 32
```
Paste it as `WEBHOOK_TOKEN` and keep a copy — you'll need it in the iOS Shortcut.

Copy your Google Sheets credentials to the droplet:
```bash
# On your Mac:
scp credentials.json root@<droplet-ip>:/opt/cv_agent/credentials.json
```

## Step 4 — Run the installer

On the droplet, as root:
```bash
bash /opt/cv_agent/deploy/install.sh
```

This installs Python, Playwright/Chromium, Caddy, creates the `cvagent` service user, sets up the venv, registers and starts the systemd unit, and installs the Caddyfile.

## Step 5 — Point Caddy at your subdomain

```bash
nano /etc/caddy/Caddyfile
# Replace <SUBDOMAIN> with cvagent-samir.duckdns.org (your actual subdomain)
systemctl reload caddy
```

Wait ~30s for Caddy to acquire the Let's Encrypt cert, then test:
```bash
curl https://cvagent-samir.duckdns.org/health
# expect: {"status":"ok"}
```

## Step 6 — Prime the FlowCV cache

Once, on the droplet:
```bash
sudo -u cvagent /opt/cv_agent/.venv/bin/python /opt/cv_agent/tools/read_cv_flowcv.py --refresh
```

This logs in to FlowCV with the credentials from `/etc/cv_agent.env` and saves `.tmp/cv_master.html` so subsequent pipeline runs don't have to re-fetch.

Re-run this command whenever you edit your FlowCV.

## Step 7 — Smoke-test the webhook

From your Mac:
```bash
TOKEN="<the WEBHOOK_TOKEN from /etc/cv_agent.env>"
curl -X POST https://cvagent-samir.duckdns.org/apply \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"job_url": "https://boards.greenhouse.io/<example>/jobs/<id>"}'
# expect: 202 {"status":"started","job_id":"<hex>"}
```

Watch the log on the droplet:
```bash
tail -f /var/log/cv_agent/<job_id>.log
```

When it finishes (~60s), check Gmail for the tailored CV + cover letter and the Google Sheet for the new row.

## Step 8 — iOS Shortcut

On your iPhone, open the **Shortcuts** app and create a new shortcut:

1. **Receive** "URLs" from Share Sheet
2. **Get Contents of URL**
   - URL: `https://cvagent-samir.duckdns.org/apply`
   - Method: `POST`
   - Headers:
     - `Authorization`: `Bearer <your token>`
     - `Content-Type`: `application/json`
   - Request Body: JSON
     - Key: `job_url`, Value: Shortcut input (the shared URL)
3. **Show Notification**: "Applying to job — check email in ~60s"

Name it "Apply with CV Agent" and enable it in the share sheet.

Test: open a Gmail job-alert email on your phone, tap a job link, then in the share sheet pick "Apply with CV Agent". Watch for the notification. Check Gmail in ~60s.

---

## Operations

### View logs
```bash
journalctl -u cv_agent.service -f         # webhook + uvicorn
ls /var/log/cv_agent/                     # per-job pipeline logs
journalctl -u caddy.service -n 50         # TLS / proxy
```

### Restart after code changes
```bash
cd /opt/cv_agent
git pull
sudo -u cvagent /opt/cv_agent/.venv/bin/pip install -r requirements.txt
systemctl restart cv_agent.service
```

### Rotate the webhook token
```bash
NEW_TOKEN=$(openssl rand -hex 32)
sed -i "s/^WEBHOOK_TOKEN=.*/WEBHOOK_TOKEN=$NEW_TOKEN/" /etc/cv_agent.env
systemctl restart cv_agent.service
# Update the same token in your iOS Shortcut
```

### Troubleshoot

- `curl /health` returns 502 → uvicorn isn't running. Check `systemctl status cv_agent.service`.
- Caddy can't get a cert → confirm duckdns subdomain points to droplet IP and ports 80/443 are open in DigitalOcean firewall.
- `/apply` returns 403 → token mismatch between Shortcut and `/etc/cv_agent.env`.
- Pipeline fails on `read_cv_flowcv` → FlowCV creds wrong or session expired; re-run `--refresh` manually.

---

## Architecture diagram (quick reference)

```
iPhone Shortcut
   │  HTTPS POST
   ▼
Caddy (TLS, :443)
   │  reverse_proxy
   ▼
uvicorn → tools/webhook_server.py (FastAPI, :8000)
   │  asyncio.create_subprocess_exec
   ▼
tools/run_apply_pipeline.py
   │  shells out, in stages
   ▼
[scrape_job, read_cv_flowcv, extract_job_relevance,
 tailor_cv_claude, generate_cover_letter,
 build_tailored_pdf, send_email, log_to_sheets]
```
