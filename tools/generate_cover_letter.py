"""
Generate a tailored cover letter using Claude.

Default mode: write cover_letter_final.txt directly (no interactive pause).
Pass --review to enable the interactive APPROVE/revise loop.

Usage:
    python3 tools/generate_cover_letter.py            # auto mode (default)
    python3 tools/generate_cover_letter.py --review   # interactive APPROVE/revise
    python3 tools/generate_cover_letter.py --draft    # write draft only, don't write final
    python3 tools/generate_cover_letter.py --revise <feedback_file>

Requires:
    ANTHROPIC_API_KEY in .env
    .tmp/cv_raw.json
    .tmp/job_description.json

Optional (and decoupled — these can be generated in parallel with this tool):
    .tmp/job_relevance.json         (from extract_job_relevance.py) — preferred
    .tmp/tailored_cv_sections.json  (from tailor_cv_claude.py) — used if present

Output:
    .tmp/cover_letter_final.txt
"""

from __future__ import annotations

import json
import os
import anthropic
from dotenv import load_dotenv

load_dotenv()
os.makedirs(".tmp", exist_ok=True)


def _detect_language() -> str:
    """Prefer the shared job_relevance.json language. Fall back to tailored_cv_sections.json
    (legacy), then to a keyword sniff of the job description, then 'en'."""
    for path in (".tmp/job_relevance.json", ".tmp/tailored_cv_sections.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            lang = (d.get("language") or "").strip().lower()
            if lang in ("en", "de"):
                return lang
        except (FileNotFoundError, json.JSONDecodeError):
            continue

    try:
        with open(".tmp/job_description.json", "r", encoding="utf-8") as f:
            j = json.load(f)
        text = (j.get("description", "") + " " + j.get("title", "")).lower()
        de_markers = ("der ", "die ", "das ", "und ", "für ", "wir ", "sie ", "ihre ", "stellenbeschreibung", "anforderungen", "kenntnisse")
        if sum(text.count(m) for m in de_markers) >= 3:
            return "de"
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return "en"


CV_LANGUAGE = _detect_language()

LANG_BLOCK_DE = """LANGUAGE: Write the ENTIRE cover letter in German (Deutsch). The candidate applies as a Werkstudent (working student) in Germany.

GERMAN ANSCHREIBEN CONVENTIONS FOR A WERKSTUDENT:
- Use Sie-Form throughout, but in a warm, genuine tone — NOT stiff corporate-speak.
- Salutation: "Sehr geehrte Damen und Herren," if no name. "Sehr geehrter Herr [Name]," / "Sehr geehrte Frau [Name]," if known.
- Closing: "Mit freundlichen Grüßen," followed by the candidate's full name.
- AVOID clichéd openings: "hiermit bewerbe ich mich", "ich bin ein motivierter Student der gerne...".
- AVOID overclaiming senior-level verbs that don't fit a student: NEVER use "geleitet", "verantwortet", "aufgebaut", "implementiert" unless the candidate's CV explicitly shows that scope.
- USE student-appropriate verbs: gelernt, gearbeitet, mitgewirkt, unterstützt, ausprobiert, mich beschäftigt, mich vertieft in, ergänzend zum Studium.
- It's GOOD to mention what the candidate is currently studying or learning — being a student is the point of the role.

GERMAN CLICHÉ BLACKLIST — these phrases are empty clichés, never write any of them:
- "Das hat direkt geklickt", "hat geklickt", "klingelt" (any variant)
- "die Brücke schlagen zwischen X und Y", "die Schnittstelle zwischen X und Y" (as buzzword bridging)
- "Genau diese Schnittstelle suche ich"
- "passt dazu besser als viele andere Stellen / Programme"
- "ist nichts, womit ich nicht vertraut bin", "ist mir nicht fremd"
- Dreigliedrige Komma-Listen in einem Atemzug ("X auswerten, Y lokalisieren, Z aufbereiten") — symmetrical triads = robotic cadence
- "mit großem Interesse", "spannende Herausforderung", "dynamisches Umfeld", "Teamplayer", "zukunftsorientiert", "motivierter Student"
- "sofort angesprochen", "absolut", "total" as enthusiasm intensifiers

If you start writing any of these — STOP and find a more concrete, honest phrasing grounded in this specific candidate's CV.
"""

LANG_BLOCK_EN = """LANGUAGE: Write the entire cover letter in English. Use natural, friendly-but-formal English (NOT corporate jargon). Address as "Dear Hiring Manager," if no contact name; otherwise "Dear Mr./Ms. [Name],". Close with "Kind regards," followed by the candidate's full name. The candidate applies as a working student — frame the letter from that perspective (currently studying, looking to learn alongside the role, modest but genuine)."""

LANG_BLOCK = LANG_BLOCK_DE if CV_LANGUAGE == "de" else LANG_BLOCK_EN

SYSTEM_PROMPT = f"""You are a thoughtful writing assistant helping a real candidate draft a genuine, well-crafted cover letter.

Your job is to organize and express the candidate's REAL experience clearly and specifically — not to inflate it, and not to fall back on generic filler. A good cover letter reads like the candidate actually wrote it: specific, honest, and grounded in what they have really done.

CORE PRINCIPLES

1. HONESTY FIRST — NEVER INVENT
You may only organize, prioritize, clarify, compress, and reframe REAL information the candidate provides. Never invent motivation, interest, achievements, metrics, projects, or experience. If something important is missing, leave it out rather than fabricating it. If the candidate is still learning a skill the job asks for, say so plainly ("Grundkenntnisse", "in Einarbeitung", "currently learning") — never overstate.

2. BE SPECIFIC, NOT GENERIC
The letter should be impossible to copy-paste to another company. Reference concrete details from the candidate's CV and from this specific role. Specificity is what makes a letter worth reading.

3. AVOID CORPORATE FILLER AND CLICHÉS
Skip tired buzzwords and filler: delve, tapestry, realm, landscape, spearhead, leverage, unleash, unlock, harness, moreover, furthermore, in conclusion, ultimately, it's worth noting, not only X but Y, passionate about, excited to apply, writing to express my interest, strong candidate, dynamic environment, fast-paced environment, team player, results-driven, go-getter, synergistic, highly motivated. They're empty — replace each with something concrete and true.

4. WRITE LIKE A PERSON
Vary sentence length and rhythm naturally. Short sentences are fine. Don't over-polish into a flat, perfectly balanced corporate cadence. Aim for a natural, readable voice, not marketing copy.

5. NO GENERIC OPENINGS
Don't open with "I am writing to apply…", "I am excited to apply…", "I believe my experience…", or "Please accept my application…". Start with something concrete: a real connection to the role, a relevant project, or a genuine reason for interest.

6. MATCH THE ROLE'S LANGUAGE NATURALLY
Mirror the role's terminology where it genuinely fits the candidate's experience — naturally, in context. Never keyword-stuff or repeat phrases unnaturally.

7. EXPLAIN FIT, DON'T REPEAT THE CV
Don't just summarize the resume. Connect the candidate's real experience to what this role actually needs, and be honest about where they're still growing.

BEFORE WRITING
Read the job description carefully — what the team does, which skills matter most, and how the candidate's real background connects — then write a focused letter around that.

═══════════════════════════════════════════════════
WERKSTUDENT CALIBRATION
═══════════════════════════════════════════════════

- The candidate is a STUDENT applying to Werkstudent (working student) positions in Germany. The output voice is the candidate's own: a thoughtful student, not a senior consultant.
- Werkstudents are hired primarily to LEARN and SUPPORT, alongside their studies. Do NOT manufacture senior-level metrics or ownership claims the CV doesn't support. If a JD asks for a skill the candidate is currently learning, label it honestly inline: "Grundkenntnisse", "im Selbststudium", "in Einarbeitung", "Basic familiarity, currently learning". Never inflate.
- LENGTH HARD CAP: 180–220 words total, two to three short paragraphs. Anything longer reads as overcompensating for a Werkstudent role. Cut aggressively.
- Keep it in the voice of a real person writing about real experience — not a polished pitch deck.

{LANG_BLOCK}

OUTPUT FORMAT:
- Plain text, no headers, no bullet points.
- Include the date, salutation, and closing signature appropriate to the target language.
- Use the candidate's name from their CV for the signature."""


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_prompt(cv: dict, job: dict, relevance: dict | None = None, tailored: dict | None = None) -> str:
    raw_text = cv.get("raw_text", "")

    name = ""
    for line in raw_text.splitlines():
        if line.strip() and "@" not in line and not any(c.isdigit() for c in line[:5]):
            name = line.strip()
            break
    if not name:
        name = "the candidate"

    summary = ""
    relevant_exp = ""
    if tailored:
        summary_obj = tailored.get("summary") or {}
        summary = summary_obj.get("proposed") if isinstance(summary_obj, dict) else (summary_obj or "")
        for kw in (tailored.get("experience_keywords") or [])[:3]:
            company = kw.get("company", "")
            bullet = kw.get("proposed_bullet", "")
            relevant_exp += f"- {company}: {bullet[:250]}\n"

    if not relevant_exp and raw_text:
        relevant_exp = f"(See full CV below)\n{raw_text[:1500]}"

    relevance_block = ""
    if relevance:
        relevance_block = (
            f"\nShared job-relevance brief (the CV tailor is using the SAME brief — keep the cover letter aligned with these keywords and emphasis areas):\n"
            f"---\n{json.dumps(relevance, indent=2, ensure_ascii=False)}\n---\n"
        )

    today = __import__('datetime').date.today()
    if CV_LANGUAGE == "de":
        months_de = ["Januar", "Februar", "März", "April", "Mai", "Juni",
                     "Juli", "August", "September", "Oktober", "November", "Dezember"]
        date_str = f"{today.day}. {months_de[today.month - 1]} {today.year}"
    else:
        date_str = today.strftime("%B %d, %Y")

    summary_block = f"Candidate's tailored summary:\n{summary}\n\n" if summary else ""

    return (
        f"Write a cover letter for {name}, a STUDENT applying to a Werkstudent position: "
        f"{job.get('title')} at {job.get('company')}.\n\n"
        f"Keep it short (150–200 words), honest about being a student, specific to this role.\n\n"
        f"{summary_block}"
        f"Most relevant experience from the candidate's CV:\n{relevant_exp}\n"
        f"{relevance_block}"
        f"IMPORTANT: The candidate has BASIC level only with: JavaScript, Cypress, Playwright. "
        f"If the cover letter mentions these, frame them honestly as 'Grundkenntnisse / in Einarbeitung' — "
        f"do NOT claim experience or expertise. Same rule for any tech the candidate is currently learning.\n\n"
        f"Job description:\n{job.get('description', '')[:3000]}\n\n"
        f"Today's date for the letter header: {date_str}\n\n"
        f"Write the full cover letter now. Remember: student voice, not consultant pitch. Short."
    )


def call_claude(client: anthropic.Anthropic, messages: list) -> str:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=messages,
    )
    return response.content[0].text.strip()


def interactive_review(client: anthropic.Anthropic, initial_draft: str, cv: dict, job: dict, relevance: dict | None, tailored: dict | None) -> str:
    draft = initial_draft
    messages = [
        {"role": "user", "content": build_prompt(cv, job, relevance, tailored)},
        {"role": "assistant", "content": draft},
    ]

    while True:
        print("\n" + "=" * 60)
        print("COVER LETTER DRAFT")
        print("=" * 60)
        print(draft)
        print("=" * 60)
        print("\nType APPROVE to finalize, or type your feedback to request edits:")
        feedback = input("> ").strip()

        if feedback.upper() == "APPROVE":
            return draft

        messages.append({"role": "user", "content": feedback})
        print("\nRevising...")
        revised = call_claude(client, messages)
        messages.append({"role": "assistant", "content": revised})
        draft = revised


def _load_optional_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main():
    import sys

    draft_only = "--draft" in sys.argv
    review_mode = "--review" in sys.argv
    revise_path = None
    for i, arg in enumerate(sys.argv):
        if arg == "--revise" and i + 1 < len(sys.argv):
            revise_path = sys.argv[i + 1]

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY must be set in .env")
        raise SystemExit(1)

    cv = load_json(".tmp/cv_raw.json")
    job = load_json(".tmp/job_description.json")
    relevance = _load_optional_json(".tmp/job_relevance.json")
    tailored = _load_optional_json(".tmp/tailored_cv_sections.json")

    print(f"Generating cover letter for: {job.get('title')} at {job.get('company')}")
    if relevance:
        print(f"  Using shared relevance brief (lang={relevance.get('language', '?')})")
    if tailored:
        print("  Including tailored CV sections for richer experience context")

    client = anthropic.Anthropic(api_key=api_key)

    if revise_path:
        with open(".tmp/cover_letter_draft.txt", "r", encoding="utf-8") as f:
            current_draft = f.read()
        with open(revise_path, "r", encoding="utf-8") as f:
            feedback = f.read().strip()
        messages = [
            {"role": "user", "content": build_prompt(cv, job, relevance, tailored)},
            {"role": "assistant", "content": current_draft},
            {"role": "user", "content": feedback},
        ]
        revised = call_claude(client, messages)
        with open(".tmp/cover_letter_draft.txt", "w", encoding="utf-8") as f:
            f.write(revised)
        print("\n--- REVISED COVER LETTER ---\n")
        print(revised)
        return

    initial_messages = [
        {"role": "user", "content": build_prompt(cv, job, relevance, tailored)}
    ]
    print("Generating first draft...")
    first_draft = call_claude(client, initial_messages)

    if draft_only:
        with open(".tmp/cover_letter_draft.txt", "w", encoding="utf-8") as f:
            f.write(first_draft)
        print("\n--- COVER LETTER DRAFT ---\n")
        print(first_draft)
        return

    if review_mode:
        final = interactive_review(client, first_draft, cv, job, relevance, tailored)
    else:
        final = first_draft
        print("\n--- COVER LETTER (auto mode) ---\n")
        print(final)
        print("\n(Pass --review to enable interactive APPROVE/revise loop.)")

    with open(".tmp/cover_letter_final.txt", "w", encoding="utf-8") as f:
        f.write(final)

    print("\nCover letter saved to .tmp/cover_letter_final.txt")
    return final


if __name__ == "__main__":
    main()
