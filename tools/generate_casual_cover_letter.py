"""
Generate a SECOND, casual/natural-voice cover letter alongside the formal one.

This runs in parallel with tools/generate_cover_letter.py during Stage 3 and
produces .tmp/cover_letter_casual.txt. Both versions get attached to the email
so the user can pick which one fits the situation.

Voice: short, human, like a WhatsApp message from a thoughtful young person
applying for a Werkstudent role. Anti-Floskel, no "hiermit bewerbe ich mich",
no corporate filler. Honest about being a student.

Usage:
    python3 tools/generate_casual_cover_letter.py

Requires:
    ANTHROPIC_API_KEY in .env
    .tmp/cv_raw.json
    .tmp/job_description.json

Optional:
    .tmp/job_relevance.json  (language + emphasis areas)

Output:
    .tmp/cover_letter_casual.txt
"""

from __future__ import annotations

import json
import os
import anthropic
from dotenv import load_dotenv

load_dotenv()
os.makedirs(".tmp", exist_ok=True)


def _detect_language() -> str:
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
        de_markers = ("der ", "die ", "das ", "und ", "für ", "wir ", "sie ", "ihre ",
                      "stellenbeschreibung", "anforderungen", "kenntnisse")
        if sum(text.count(m) for m in de_markers) >= 3:
            return "de"
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return "en"


CV_LANGUAGE = _detect_language()


SYSTEM_PROMPT_DE = """Schreib mir ein Bewerbungsschreiben auf Deutsch für einen Job. Der Ton soll sehr natürlich, locker und menschlich sein – eher wie eine echte Nachricht als ein klassischer Cover Letter. Kein formeller Einstieg, keine Floskeln wie „hiermit bewerbe ich mich". Kurz, direkt und ehrlich. Fokus auf Motivation, Persönlichkeit und Arbeitsweise (z. B. zuverlässig, mitdenkend, anpackend). Kein übertriebenes Selbstlob. Es soll so klingen, als hätte es eine junge Person selbst geschrieben.

Halte es locker, aber trotzdem so, dass es im Job-Kontext seriös wirkt.

VERBOTENE FLOSKELN (DEUTSCH):
KEINE dieser Phrasen verwenden — sie sind leere Floskeln und klingen unecht:
- „Das hat direkt geklickt", „hat geklickt", „klingelt" (irgendeine Variante)
- „die Brücke schlagen zwischen X und Y", „die Schnittstelle zwischen X und Y"
- „Genau diese Schnittstelle suche ich"
- „passt dazu besser als viele andere Stellen / Programme"
- „ist nichts, womit ich nicht vertraut bin", „ist mir nicht fremd"
- Dreigliedrige Komma-Listen in einem Atemzug („X auswerten, Y lokalisieren, Z aufbereiten") — diese symmetrische Kadenz klingt mechanisch
- „mit großem Interesse", „spannende Herausforderung", „dynamisches Umfeld", „Teamplayer", „zukunftsorientiert", „motivierter Student"
- Übertrieben begeisterte Adverbien („absolut", „total", „sofort begeistert")

Wenn du eine dieser Formulierungen schreiben willst — STOP und finde eine konkretere, ehrlichere Variante, die wirklich aus der Lebensrealität der Person kommt.

OUTPUT: nur den fertigen Brieftext, ohne Überschriften, ohne Markdown, ohne Erklärung. Niemals Erfahrungen erfinden, die nicht im Lebenslauf stehen."""


SYSTEM_PROMPT_EN = """Write me a cover letter in English for a job. The tone should be very natural, relaxed, and human — more like a real message than a classic cover letter. No formal opening, no clichés like "I am writing to apply". Short, direct, honest. Focus on motivation, personality, and how the person works (reliable, thinks along, gets things done). No overclaiming. It should sound like a young person wrote it themselves.

Keep it casual, but still professional enough for the job context.

FORBIDDEN CLICHÉS (ENGLISH):
NONE of these phrases — they're empty filler and read as insincere:
- "X just clicked", "it clicked for me", "instantly clicked"
- "build a bridge between X and Y", "the intersection of X and Y" (when used as buzzword bridging)
- "this is exactly the intersection I'm looking for"
- "fits better than many other roles / programs"
- "is not something I'm unfamiliar with", "is no stranger to me"
- Triadic comma lists in one breath ("analyze X, locate Y, prepare Z") — that symmetrical cadence reads as mechanical
- "passionate about", "dynamic environment", "fast-paced environment", "team player", "results-driven", "thrilled to apply"
- Overly enthusiastic adverbs ("absolutely", "totally", "immediately drawn")

If you find yourself about to write any of these — STOP and find a more concrete, honest phrasing rooted in the actual person's experience.

OUTPUT: only the finished letter text, no headings, no markdown, no explanation. Never invent experience that isn't in the CV."""


SYSTEM_PROMPT = SYSTEM_PROMPT_DE if CV_LANGUAGE == "de" else SYSTEM_PROMPT_EN


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_optional_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def build_prompt(cv: dict, job: dict, relevance: dict | None) -> str:
    raw_text = cv.get("raw_text", "")

    name = ""
    for line in raw_text.splitlines():
        if line.strip() and "@" not in line and not any(c.isdigit() for c in line[:5]):
            name = line.strip()
            break
    if not name:
        name = "the candidate"

    relevance_block = ""
    if relevance:
        relevance_block = (
            f"\nKontext zum Job (auch verfügbar für den formellen Cover Letter — bleib konsistent damit):\n"
            f"---\n{json.dumps(relevance, indent=2, ensure_ascii=False)}\n---\n"
        )

    today = __import__('datetime').date.today()
    if CV_LANGUAGE == "de":
        months_de = ["Januar", "Februar", "März", "April", "Mai", "Juni",
                     "Juli", "August", "September", "Oktober", "November", "Dezember"]
        date_str = f"{today.day}. {months_de[today.month - 1]} {today.year}"
    else:
        date_str = today.strftime("%B %d, %Y")

    if CV_LANGUAGE == "de":
        return (
            f"Schreib einen lockeren, menschlichen Cover Letter für {name}, eine*n Student*in, "
            f"die/der sich auf die Werkstudentenstelle bewirbt: "
            f"{job.get('title')} bei {job.get('company')}.\n\n"
            f"Datum für den Brief: {date_str}\n\n"
            f"Lebenslauf des/der Bewerber*in:\n---\n{raw_text[:2000]}\n---\n"
            f"{relevance_block}"
            f"Stellenbeschreibung:\n---\n{job.get('description', '')[:3000]}\n---\n\n"
            f"Jetzt den ganzen Brieftext schreiben. Erinnerung: locker, kurz, ehrlich, "
            f"klingt wie eine Person — nicht wie ein Roboter."
        )
    else:
        return (
            f"Write a casual, human cover letter for {name}, a student applying to "
            f"the working-student position: {job.get('title')} at {job.get('company')}.\n\n"
            f"Date for the letter: {date_str}\n\n"
            f"Candidate's CV:\n---\n{raw_text[:2000]}\n---\n"
            f"{relevance_block}"
            f"Job description:\n---\n{job.get('description', '')[:3000]}\n---\n\n"
            f"Now write the full letter text. Reminder: casual, short, honest, "
            f"sounds like a person — not a robot."
        )


def main():
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("Error: ANTHROPIC_API_KEY must be set in .env")
        raise SystemExit(1)

    cv = load_json(".tmp/cv_raw.json")
    job = load_json(".tmp/job_description.json")
    relevance = _load_optional_json(".tmp/job_relevance.json")

    print(f"Generating casual cover letter for: {job.get('title')} at {job.get('company')}")
    print(f"  Language: {CV_LANGUAGE}")

    client = anthropic.Anthropic(api_key=api_key)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": build_prompt(cv, job, relevance)}
        ],
    )

    text = response.content[0].text.strip()

    with open(".tmp/cover_letter_casual.txt", "w", encoding="utf-8") as f:
        f.write(text)

    print("\n--- CASUAL COVER LETTER ---\n")
    print(text)
    print("\nSaved to .tmp/cover_letter_casual.txt")


if __name__ == "__main__":
    main()
