"""
Single-script orchestrator for the apply-to-job pipeline.

Runs the 5 stages from workflows/apply_to_job.md deterministically, with
parallel stages launched concurrently via asyncio.

Usage:
    python3 tools/run_apply_pipeline.py --job-url "<URL>"
    python3 tools/run_apply_pipeline.py --job-text "<PASTED JOB DESCRIPTION>"
    python3 tools/run_apply_pipeline.py --job-url "<URL>" --review

Either --job-url OR --job-text is required. Text mode skips the scraper —
useful when the platform (Indeed, LinkedIn) anti-bots the droplet IP and we
just want to paste the JD straight from the iOS share sheet.

Designed to be called by tools/webhook_server.py from the cloud droplet, but
runs identically on a local Mac for testing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _extract_url_from_text(text: str) -> str | None:
    """Find the first http(s) URL in pasted text and strip trailing punctuation.
    Used in text-mode so the user can paste 'URL\\n<JD body>' into the shortcut
    and the email still gets a clickable apply link."""
    m = _URL_RE.search(text or "")
    if not m:
        return None
    url = m.group(0)
    while url and url[-1] in '.,;:!?)]>"\'':
        url = url[:-1]
    return url or None


REPO_ROOT = Path(__file__).resolve().parent.parent
TMP_DIR = REPO_ROOT / ".tmp"


class StageFailed(RuntimeError):
    def __init__(self, stage: str, returncode: int, stderr: str):
        self.stage = stage
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"Stage '{stage}' failed (exit {returncode}): {stderr.strip()[:300]}")


async def _run(cmd: list[str], label: str) -> tuple[int, str, str]:
    """Run a subprocess, stream stdout/stderr to our own streams with a label prefix,
    return (returncode, captured_stdout, captured_stderr) so callers can decide."""
    start = time.monotonic()
    print(f"[{label}] starting: {' '.join(cmd)}", flush=True)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    async def drain(stream: asyncio.StreamReader, buf: list[str], out_stream) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            buf.append(text)
            print(f"[{label}] {text}", file=out_stream, flush=True)

    await asyncio.gather(
        drain(proc.stdout, stdout_lines, sys.stdout),
        drain(proc.stderr, stderr_lines, sys.stderr),
    )
    rc = await proc.wait()

    elapsed = time.monotonic() - start
    print(f"[{label}] done in {elapsed:.1f}s (exit {rc})", flush=True)
    return rc, "\n".join(stdout_lines), "\n".join(stderr_lines)


async def _run_required(cmd: list[str], label: str) -> None:
    rc, _out, err = await _run(cmd, label)
    if rc != 0:
        raise StageFailed(label, rc, err)


async def stage1_parallel(job_url: str) -> None:
    """Scrape job + ensure CV cache. Both are independent."""
    await asyncio.gather(
        _run_required([sys.executable, "tools/scrape_job.py", job_url], "scrape_job"),
        _run_required([sys.executable, "tools/read_cv_flowcv.py"], "read_cv"),
    )


def write_job_description_from_text(job_text: str, apply_url: str | None = None) -> None:
    """Text mode: bypass the scraper by writing a stub job_description.json
    directly. extract_job_relevance.py (Stage 2) will backfill title + company
    from the body via Claude, so the email subject and sheet row still come
    out right. If apply_url is provided (or extractable from the pasted text),
    it lands in both `url` and `apply_url` so the email + Sheet row link back
    to the posting."""
    TMP_DIR.mkdir(exist_ok=True)
    apply = (apply_url or "").strip()
    if not apply:
        extracted = _extract_url_from_text(job_text)
        if extracted:
            apply = extracted
            print(f"[scrape_job] auto-extracted apply_url from pasted text: {apply}", flush=True)
    job = {
        "title": "",
        "company": "",
        "url": apply,
        "description": job_text.strip(),
        "apply_url": apply,
    }
    with open(TMP_DIR / "job_description.json", "w", encoding="utf-8") as f:
        json.dump(job, f, indent=2, ensure_ascii=False)


async def stage1_text_mode(job_text: str, apply_url: str | None) -> None:
    """Text mode Stage 1: write the JD stub locally, only fetch the CV cache."""
    write_job_description_from_text(job_text, apply_url)
    note = f", apply_url provided" if apply_url else ""
    print(f"[scrape_job] skipped (text mode, {len(job_text)} chars pasted{note})", flush=True)
    await _run_required([sys.executable, "tools/read_cv_flowcv.py"], "read_cv")


async def stage2_extract() -> None:
    await _run_required([sys.executable, "tools/extract_job_relevance.py"], "extract_relevance")


async def stage3_parallel(review: bool) -> None:
    """Tailor CV + write two cover letters (formal + casual), all in parallel.
    The casual letter is warn-only — if it fails, the formal one still goes out."""
    cover_args = [sys.executable, "tools/generate_cover_letter.py"]
    if review:
        cover_args.append("--review")

    casual_task = asyncio.create_task(
        _run([sys.executable, "tools/generate_casual_cover_letter.py"], "cover_casual")
    )
    await asyncio.gather(
        _run_required([sys.executable, "tools/tailor_cv_claude.py"], "tailor_cv"),
        _run_required(cover_args, "cover_letter"),
    )
    casual_rc, _, casual_err = await casual_task
    if casual_rc != 0:
        print(f"[cover_casual] WARNING: casual cover letter failed; only the formal one will go out. {casual_err[:200]}", file=sys.stderr, flush=True)


async def stage4_build_pdf() -> None:
    await _run_required([sys.executable, "tools/build_tailored_pdf.py"], "build_pdf")


async def stage5_parallel() -> None:
    """Email + Google Sheet log + OneDrive Excel log, in parallel.
    Email is the only hard failure — both trackers are warn-only so a missing
    OneDrive path on the cloud droplet (or a Sheets API hiccup) doesn't kill
    the run after the email has already gone out."""
    email_task = asyncio.create_task(
        _run([sys.executable, "tools/send_email.py"], "send_email")
    )
    sheet_task = asyncio.create_task(
        _run([sys.executable, "tools/log_to_sheets.py"], "log_sheets")
    )
    excel_task = asyncio.create_task(
        _run([sys.executable, "tools/log_to_excel.py"], "log_excel")
    )
    email_rc, _, email_err = await email_task
    sheet_rc, _, sheet_err = await sheet_task
    excel_rc, _, excel_err = await excel_task

    if email_rc != 0:
        raise StageFailed("send_email", email_rc, email_err)
    if sheet_rc != 0:
        print(f"[log_sheets] WARNING: sheet logging failed but email was sent. {sheet_err[:200]}", file=sys.stderr, flush=True)
    if excel_rc != 0:
        print(f"[log_excel] WARNING: Excel logging failed but email was sent. {excel_err[:200]}", file=sys.stderr, flush=True)


async def run_pipeline(job_url: str | None, job_text: str | None, apply_url: str | None, review: bool) -> None:
    TMP_DIR.mkdir(exist_ok=True)
    overall_start = time.monotonic()

    if job_text:
        print(f"\n=== STAGE 1: (text mode) write JD stub || read_cv_flowcv ===", flush=True)
        await stage1_text_mode(job_text, apply_url)
    else:
        print(f"\n=== STAGE 1: scrape_job || read_cv_flowcv ===", flush=True)
        await stage1_parallel(job_url)

    print(f"\n=== STAGE 2: extract_job_relevance ===", flush=True)
    await stage2_extract()

    print(f"\n=== STAGE 3: tailor_cv || cover_letter ===", flush=True)
    await stage3_parallel(review)

    print(f"\n=== STAGE 4: build_tailored_pdf ===", flush=True)
    await stage4_build_pdf()

    print(f"\n=== STAGE 5: send_email || log_to_sheets || log_to_excel ===", flush=True)
    await stage5_parallel()

    total = time.monotonic() - overall_start
    print(f"\n=== PIPELINE COMPLETE in {total:.1f}s ===", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the apply-to-job pipeline end-to-end.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--job-url", help="Job posting URL to scrape")
    source.add_argument("--job-text", help="Job description text pasted directly (bypasses the scraper)")
    parser.add_argument("--apply-url", help="Optional apply URL when using --job-text, included in email + sheet row")
    parser.add_argument("--review", action="store_true", help="Enable interactive cover letter review (skip for cloud/webhook runs)")
    args = parser.parse_args()

    if args.apply_url and args.job_url:
        # URL mode discovers apply_url itself; ignore the explicit override
        args.apply_url = None

    try:
        asyncio.run(run_pipeline(args.job_url, args.job_text, args.apply_url, args.review))
        return 0
    except StageFailed as e:
        print(f"\nFAILED: {e}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr, flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
