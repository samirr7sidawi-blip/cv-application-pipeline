"""
FastAPI webhook server that triggers the apply pipeline on POST /apply.

Designed to run on a DigitalOcean droplet behind Caddy (TLS termination).
The iOS Shortcut on the user's phone POSTs the job URL here; the server
authenticates via bearer token, enqueues the run, and returns 202 immediately.
The pipeline runs in the background and emails the CV/cover letter when done.

Usage:
    uvicorn tools.webhook_server:app --host 127.0.0.1 --port 8000

Required env vars (typically in /etc/cv_agent.env, loaded by systemd):
    WEBHOOK_TOKEN — bearer token shared with the iOS Shortcut
    (plus all the other vars the pipeline needs: ANTHROPIC_API_KEY, FLOWCV_*, GMAIL_*, etc.)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, model_validator


load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.getenv("CV_AGENT_LOG_DIR", "/var/log/cv_agent"))
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "")

logger = logging.getLogger("cv_agent.webhook")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


# Serialize pipeline runs — at personal volume there's no point in concurrent
# Playwright / FlowCV sessions, and serializing keeps the .tmp/ state simple.
_pipeline_lock = asyncio.Lock()


class ApplyRequest(BaseModel):
    job_url: str | None = Field(default=None, max_length=2048)
    job_text: str | None = Field(default=None, max_length=50_000)
    apply_url: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ApplyRequest":
        url = (self.job_url or "").strip()
        text = (self.job_text or "").strip()
        apply = (self.apply_url or "").strip()
        if not url and not text:
            raise ValueError("provide either job_url or job_text")
        if url and text:
            raise ValueError("provide only one of job_url or job_text, not both")
        if text and len(text) < 80:
            raise ValueError("job_text is too short to be a real description (min 80 chars)")
        # apply_url is only meaningful in text mode (URL mode discovers it itself)
        if url and apply:
            apply = ""
        self.job_url = url or None
        self.job_text = text or None
        self.apply_url = apply or None
        return self


def _verify_token(authorization: str | None = Header(default=None)) -> None:
    if not WEBHOOK_TOKEN:
        logger.error("WEBHOOK_TOKEN env var is empty — refusing all requests")
        raise HTTPException(status_code=500, detail="server misconfigured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    presented = authorization.removeprefix("Bearer ").strip()
    # constant-time-ish comparison
    if len(presented) != len(WEBHOOK_TOKEN) or not all(a == b for a, b in zip(presented, WEBHOOK_TOKEN)):
        raise HTTPException(status_code=403, detail="invalid token")


def _validate_url(url: str) -> None:
    try:
        parsed = urlparse(url)
    except ValueError:
        raise HTTPException(status_code=400, detail="malformed URL")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="URL must be http(s)")
    if not parsed.netloc or parsed.hostname in ("localhost", "127.0.0.1", "0.0.0.0"):
        raise HTTPException(status_code=400, detail="URL host not allowed")


async def _run_pipeline_job(job_id: str, job_url: str | None, job_text: str | None, apply_url: str | None) -> None:
    """Background task: run tools/run_apply_pipeline.py and tee its output to a per-job log file."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{job_id}.log"

    if job_url:
        mode_desc = f"url={job_url}"
        cli_args = ["--job-url", job_url]
    else:
        mode_desc = f"text={len(job_text or '')} chars" + (f" apply_url={apply_url}" if apply_url else "")
        cli_args = ["--job-text", job_text or ""]
        if apply_url:
            cli_args += ["--apply-url", apply_url]

    async with _pipeline_lock:
        start = time.monotonic()
        logger.info("job=%s starting %s log=%s", job_id, mode_desc, log_path)

        with open(log_path, "w", encoding="utf-8") as log_file:
            log_file.write(f"job_id={job_id}\nmode={mode_desc}\n\n")
            log_file.flush()

            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "tools/run_apply_pipeline.py",
                *cli_args,
                cwd=str(REPO_ROOT),
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
            )
            rc = await proc.wait()

        elapsed = time.monotonic() - start
        if rc == 0:
            logger.info("job=%s succeeded in %.1fs", job_id, elapsed)
        else:
            logger.error("job=%s FAILED in %.1fs (exit %d) — see %s", job_id, elapsed, rc, log_path)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not WEBHOOK_TOKEN:
        logger.warning("WEBHOOK_TOKEN is not set — server will refuse all /apply requests")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("cv_agent webhook ready — logs at %s", LOG_DIR)
    yield


app = FastAPI(title="CV Agent webhook", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/apply", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(_verify_token)])
async def apply(req: ApplyRequest) -> dict:
    if req.job_url:
        _validate_url(req.job_url)
    if req.apply_url:
        _validate_url(req.apply_url)
    job_id = uuid.uuid4().hex[:12]
    asyncio.create_task(_run_pipeline_job(job_id, req.job_url, req.job_text, req.apply_url))
    if req.job_url:
        logger.info("job=%s enqueued url=%s", job_id, req.job_url)
    else:
        logger.info("job=%s enqueued text=%d chars apply_url=%s", job_id, len(req.job_text or ""), req.apply_url or "-")
    return {"status": "started", "job_id": job_id, "mode": "url" if req.job_url else "text"}
