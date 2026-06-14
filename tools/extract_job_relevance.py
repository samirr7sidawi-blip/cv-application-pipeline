"""
Single shared Claude call that extracts structured relevance info from a job
description. This output is consumed by BOTH tools/tailor_cv_claude.py and
tools/generate_cover_letter.py so the two downstream tasks can run in parallel
and stay aligned on keywords, tone, language, and emphasis areas.

Usage:
    python3 tools/extract_job_relevance.py

Requires:
    ANTHROPIC_API_KEY in .env
    .tmp/job_description.json  (from scrape_job.py)
    .tmp/cv_raw.json           (from read_cv_flowcv.py)

Output:
    .tmp/job_relevance.json
"""

import json
import os
import re
import anthropic
from dotenv import load_dotenv

load_dotenv()
os.makedirs(".tmp", exist_ok=True)


SYSTEM_PROMPT = """You analyze a job description against a candidate's CV and extract a compact, structured relevance brief.

This brief is consumed by two downstream agents — a CV tailor and a cover-letter writer — that run in parallel. They MUST agree on language, keywords, and emphasis areas. Your output is the single source of truth for both.

Be concrete. No fluff, no hedging. Use the candidate's actual CV experience — do not invent.

LANGUAGE DETECTION RULES:
- If the job description is in German → "de"
- If the job description is in English → "en"
- Never mix. Detect from the body of the posting, not the title.

OUTPUT FORMAT (strict JSON, no markdown, no prose outside the JSON):

{
  "language": "en" | "de",
  "language_rationale": "<one sentence>",

  "company": "<company name from the posting>",
  "role": "<role title from the posting>",
  "seniority": "junior | mid | senior | lead | unknown",

  "must_have_keywords": [
    "<8-12 keywords/phrases the ATS will scan for, in the JOB's language>"
  ],

  "nice_to_have_keywords": [
    "<3-6 secondary keywords>"
  ],

  "recommended_emphasis_areas": [
    "<3-5 short phrases naming which candidate experiences/skills to emphasize, based on overlap with the job>"
  ],

  "company_hooks": [
    "<2-3 concrete hooks the cover letter can open with — real things about this role/company/product that the candidate can credibly tie to their background. Each hook is one sentence.>"
  ],

  "tone_hints": "<1-2 sentences: formal vs casual, German Sie/Du, technical-depth expected, any specific phrasing the JD uses repeatedly>",

  "honest_caveats": [
    "<0-3 short notes flagging skills the candidate has only basic familiarity with, that the JD asks for. Use the same honest-learning labels the CV uses: 'Basic familiarity', 'Currently learning', 'Grundkenntnisse', 'In Einarbeitung'. Empty list if none.>"
  ]
}

Return raw JSON only — no code fences, no preamble, no trailing prose."""


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_claude_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    return json.loads(text)


def build_user_message(cv: dict, job: dict) -> str:
    cv_text = cv.get("raw_text") or json.dumps(cv, indent=2, ensure_ascii=False)
    return (
        f"Candidate's CV (live text from FlowCV):\n\n"
        f"---\n{cv_text}\n---\n\n"
        f"Job posting:\n\n"
        f"Title: {job.get('title', '')}\n"
        f"Company: {job.get('company', '')}\n\n"
        f"{job.get('description', '')}\n\n"
        f"Return the relevance brief as JSON per the schema."
    )


def main():
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY must be set in .env")
        raise SystemExit(1)

    cv = load_json(".tmp/cv_raw.json")
    job = load_json(".tmp/job_description.json")

    print(f"Extracting job relevance for: {job.get('title')} at {job.get('company')}")

    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": build_user_message(cv, job)}
        ],
    )

    raw = response.content[0].text

    try:
        relevance = parse_claude_json(raw)
    except json.JSONDecodeError as e:
        print(f"JSON parse error: {e}")
        print(f"Raw response:\n{raw[:500]}")
        retry_msg = build_user_message(cv, job) + "\n\nIMPORTANT: Return valid JSON only. No prose, no code fences."
        response2 = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": retry_msg}],
        )
        relevance = parse_claude_json(response2.content[0].text)

    with open(".tmp/job_relevance.json", "w", encoding="utf-8") as f:
        json.dump(relevance, f, indent=2, ensure_ascii=False)

    # Backfill company and role into job_description.json if the scraper
    # missed them. Stellenwerk and similar boards don't expose company name
    # in structured markup, but Claude reliably extracts it from the body.
    updates = {}
    if not job.get("company") and relevance.get("company"):
        updates["company"] = relevance["company"]
    if not job.get("title") and relevance.get("role"):
        updates["title"] = relevance["role"]
    if updates:
        job.update(updates)
        with open(".tmp/job_description.json", "w", encoding="utf-8") as f:
            json.dump(job, f, indent=2, ensure_ascii=False)
        print(f"Backfilled job_description.json: {list(updates.keys())}")

    print("Job relevance saved to .tmp/job_relevance.json")
    print(f"  Language:   {relevance.get('language', '?')}")
    print(f"  Must-have:  {', '.join(relevance.get('must_have_keywords', [])[:6])}...")
    print(f"  Emphasis:   {', '.join(relevance.get('recommended_emphasis_areas', []))}")
    if relevance.get("honest_caveats"):
        print(f"  Caveats:    {relevance.get('honest_caveats')}")

    usage = response.usage
    print(f"\n  Tokens: {usage.input_tokens} in / {usage.output_tokens} out")
    if hasattr(usage, "cache_read_input_tokens") and usage.cache_read_input_tokens:
        print(f"  Cache hit: {usage.cache_read_input_tokens} tokens read from cache")


if __name__ == "__main__":
    main()
