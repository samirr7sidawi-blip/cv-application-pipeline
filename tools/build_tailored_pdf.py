"""
Build a tailored CV PDF entirely from the local cache .tmp/cv_master.html
(snapshotted by read_cv_flowcv.py). NO trip to FlowCV — fast, deterministic,
and re-runnable.

Strategy:
- Whole-bullet atomic replacement: for each experience_keywords entry, find
  the smallest DOM element whose normalized innerText equals original_bullet,
  collapse all its text nodes, and write proposed_bullet into the first text
  node. The "Heading: Body" structure is preserved when both original and
  proposed share that pattern (heading goes back into the original <strong>).
- manual_text_edits: short standalone substring replaces (locations, headers,
  date words, skill items, etc.). Single-line only — multi-line finds are
  rejected to prevent the corruption pattern we saw before.
- Skills additions: append to the matching category info-span by category text.

Usage:
    python3 tools/build_tailored_pdf.py

Requires:
    .tmp/cv_master.html             (from read_cv_flowcv.py)
    .tmp/tailored_cv_sections.json  (from tailor_cv_claude.py)

Output:
    .tmp/tailored_cv.pdf
    .tmp/tailored_resume_render.html  (final HTML used to render the PDF)
"""

import json
import os
import asyncio
from dotenv import load_dotenv

load_dotenv()
os.makedirs(".tmp", exist_ok=True)


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# Atomic JS that does everything: whole-bullet swaps, manual edits,
# skill additions, and snapshots the resulting HTML.
APPLY_JS = r"""
({bullets, manual_edits, skills}) => {
    const root = document.querySelector('.resumePage') || document.body;
    const norm = s => (s || '').replace(/\s+/g, ' ').trim();

    const applied_bullets = [];
    const failed_bullets = [];
    const applied_edits = [];
    const failed_edits = [];
    const applied_skills = [];
    const failed_skills = [];

    // Strip leading bullet glyphs and whitespace, then collapse whitespace.
    // FlowCV's innerText sometimes omits the literal "•" (CSS-rendered) and
    // sometimes includes it; Claude's `original_bullet` may include or omit
    // it. Normalize both sides identically so they match.
    const stripBullet = s => (s || '').replace(/^[\s•·•·\-\*]+/, '').trim();
    const cleanNorm = s => norm(stripBullet(s))
        .replace(/\s*([:;,\.])\s*/g, '$1 ')   // "Word : Body" → "Word: Body"
        .replace(/\s+/g, ' ')
        .trim();
    // Even more aggressive: lowercase, alphanumerics only. Used as a last-ditch
    // matching key when cleanNorm equality fails (e.g. unicode dash variants,
    // soft hyphens, NBSPs).
    const tightNorm = s => (s || '').toLowerCase().replace(/[^a-z0-9äöüß]/gi, '');

    // --- WHOLE-BULLET REPLACEMENT ---
    // Try (in order): exact normalized match → bullet-stripped normalized match
    // → substring containment match (original cleanNorm appears inside the
    // element's cleanNorm innerText). Always pick smallest matching element.
    function findSmallestMatch(originalRaw) {
        const originalClean = cleanNorm(originalRaw);
        if (!originalClean) return null;

        const all = Array.from(root.querySelectorAll('*'));
        const allWithText = all.map(el => ({
            el,
            txt: cleanNorm(el.innerText || el.textContent || ''),
        }));

        // Pass 1: exact (normalized, bullet-stripped) equality
        let candidates = allWithText.filter(x => x.txt === originalClean).map(x => x.el);

        // Pass 2: tight containment — txt contains original AND length is close
        if (!candidates.length) {
            candidates = allWithText
                .filter(x => x.txt.includes(originalClean) && x.txt.length < originalClean.length * 2)
                .map(x => x.el);
        }

        // Pass 3: containment with a slightly looser size cap (3x).
        if (!candidates.length) {
            const containing = allWithText.filter(x =>
                x.txt.includes(originalClean) &&
                x.txt.length < originalClean.length * 3
            );
            if (containing.length) {
                containing.sort((a, b) => a.txt.length - b.txt.length);
                candidates = [containing[0].el];
            }
        }

        // Pass 4: tightNorm equality. Last-ditch match for cases where
        // whitespace/punctuation differs (e.g. ":" on its own line in DOM
        // vs no space in original). Strict 2x size cap to stay safe.
        if (!candidates.length) {
            const originalTight = tightNorm(originalRaw);
            if (originalTight.length >= 30) {
                const tightMatches = allWithText.filter(x => {
                    const t = tightNorm(x.txt);
                    return t === originalTight ||
                           (t.includes(originalTight) && t.length < originalTight.length * 2);
                });
                if (tightMatches.length) {
                    tightMatches.sort((a, b) => a.txt.length - b.txt.length);
                    candidates = [tightMatches[0].el];
                }
            }
        }

        if (!candidates.length) return null;
        // Smallest containing element: not an ancestor of any other candidate
        for (const c of candidates) {
            let isAncestor = false;
            for (const o of candidates) {
                if (o !== c && c.contains(o)) { isAncestor = true; break; }
            }
            if (!isAncestor) return c;
        }
        return candidates[candidates.length - 1];
    }

    function splitHeading(text) {
        // Returns [heading_with_colon, rest_of_text] or null if no colon-heading
        const m = text.match(/^([^:\n]{1,80}:)(\s*.*)$/s);
        if (m) return [m[1], m[2]];
        return null;
    }

    // Detect FlowCV's screen-reader-only spans: tiny font, white text, zero
    // width — they hold the literal "•" glyph. We must NEVER write the bullet
    // body into these (it disappears visually) and we must NEVER blank them
    // (the • bullet glyph is what makes the bullet point render).
    function isVisuallyHidden(el) {
        if (!el || el.nodeType !== 1) return false;
        const s = el.getAttribute && el.getAttribute('style') || '';
        if (!s) return false;
        const low = s.toLowerCase();
        // Heuristic: any TWO of (font-size:1px), (color:white), (width:0px),
        // (max-width:0), (height:0px) ⇒ visually hidden.
        let hits = 0;
        if (/font-size\s*:\s*1px/.test(low)) hits++;
        if (/color\s*:\s*white/.test(low)) hits++;
        if (/(^|[\s;])width\s*:\s*0px/.test(low)) hits++;
        if (/max-width\s*:\s*0/.test(low)) hits++;
        if (/(^|[\s;])height\s*:\s*0px/.test(low)) hits++;
        return hits >= 2;
    }
    function hasHiddenAncestor(node, stopAt) {
        let cur = node.parentElement;
        while (cur && cur !== stopAt) {
            if (isVisuallyHidden(cur)) return true;
            cur = cur.parentElement;
        }
        return false;
    }

    for (const b of bullets) {
        const original = b.original_bullet || '';
        const proposed = b.proposed_bullet || '';
        if (!original || !proposed || cleanNorm(original) === cleanNorm(proposed)) continue;

        // Multi-line originals: FlowCV stores user line breaks as separate
        // sibling DOM elements (<li> + <p>, or sibling <div>s). The flattened
        // cleanNorm text only matches the COMMON ancestor, which also contains
        // adjacent unrelated bullets — clobbering them when we collapse text
        // nodes. Defer these to the safety net's chunk-split + colon-split
        // logic, which targets each chunk individually.
        if (original.includes('\n\n') || original.includes('\n')) {
            failed_bullets.push({company: b.company, original: original.slice(0, 80), reason: 'multi-line — deferred to safety net'});
            continue;
        }

        const target = findSmallestMatch(original);
        if (!target) {
            failed_bullets.push({company: b.company, original: original.slice(0, 80)});
            continue;
        }

        const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
        const allNodes = [];
        let n;
        while ((n = walker.nextNode())) allNodes.push(n);
        const visibleNodes = allNodes.filter(nd => !hasHiddenAncestor(nd, target));

        // Split-bold mode: if the proposed bullet has "Heading: Body" form AND
        // the target contains a <strong>/<b> with a visible text node inside,
        // write the heading (with colon) into the <strong>'s text node and the
        // body into the first visible text node OUTSIDE the <strong>. This
        // preserves FlowCV's heading-bold + body-regular formatting.
        const colonIdx = proposed.indexOf(':');
        let splitHandled = false;
        if (colonIdx > 0 && colonIdx < 80 && visibleNodes.length >= 2) {
            const strongEl = target.querySelector('strong, b');
            if (strongEl) {
                let strongText = null;
                let outsideText = null;
                for (const nd of visibleNodes) {
                    if (strongEl.contains(nd)) {
                        if (!strongText) strongText = nd;
                    } else {
                        if (!outsideText) outsideText = nd;
                    }
                }
                if (strongText && outsideText) {
                    const head = proposed.slice(0, colonIdx + 1);
                    let body = proposed.slice(colonIdx + 1);
                    if (!body.startsWith(' ')) body = ' ' + body;
                    strongText.nodeValue = head;
                    outsideText.nodeValue = body;
                    for (const nd of visibleNodes) {
                        if (nd !== strongText && nd !== outsideText) nd.nodeValue = '';
                    }
                    applied_bullets.push({company: b.company, mode: 'split-bold', preview: proposed.slice(0, 60)});
                    splitHandled = true;
                }
            }
        }

        // Plain mode (fallback): collapse all VISIBLE text nodes into the
        // first visible text node. Used for bullets without a "Heading:"
        // structure, or where no <strong> exists in the target. Hidden-
        // ancestor nodes (the screen-reader • glyph) are left untouched.
        if (!splitHandled) {
            if (visibleNodes.length > 0) {
                visibleNodes[0].nodeValue = proposed;
                for (let k = 1; k < visibleNodes.length; k++) visibleNodes[k].nodeValue = '';
                applied_bullets.push({company: b.company, mode: 'plain', preview: proposed.slice(0, 60)});
            } else if (allNodes.length > 0) {
                allNodes[0].nodeValue = proposed;
                for (let k = 1; k < allNodes.length; k++) allNodes[k].nodeValue = '';
                applied_bullets.push({company: b.company, mode: 'plain-fallback', preview: proposed.slice(0, 60)});
            } else {
                target.textContent = proposed;
                applied_bullets.push({company: b.company, mode: 'textContent', preview: proposed.slice(0, 60)});
            }
        }
    }

    // --- SKILL ADDITIONS (run BEFORE manual_edits) ---
    // Skills must be placed BEFORE manual_text_edits run. Reason: Claude often
    // emits the German category name (e.g. "Testing, Analyse & Tools") AND a
    // manual_edit that renames that very heading (e.g. → "Analysis, BI & Tools").
    // If edits ran first, the wordSpan text would no longer match the category
    // name Claude referenced and the fuzzy fallback would land the skill under
    // an unrelated category. Running skills first guarantees the wordSpan still
    // holds the original German text Claude saw when generating the JSON.
    //
    // Match priority:
    //   1. Exact category text equality (after alias normalization)
    //   2. One-side substring match
    //   3. Keyword overlap (>3 char tokens, stopwords filtered).
    {
        const stop = new Set(['and', 'und', 'or', 'oder', 'the', 'der', 'die', 'das', 'a', 'an', '&']);
        const aliases = {
            'programmiersprachen': 'programming languages',
            'werkzeuge': 'tools',
            'technologien': 'technologies',
            'kenntnisse': 'skills',
            'sprachen': 'languages',
            'bildung': 'education',
            'erfahrung': 'experience',
            'interessen': 'interests',
            'ki-tools': 'ai tools',
            'künstliche intelligenz': 'ai',
            'softwareentwicklung': 'software development',
        };
        function aliasOf(s) {
            const low = s.toLowerCase().trim();
            return aliases[low] || low;
        }
        function tokens(s) {
            return aliasOf(s).split(/[^a-z0-9äöüß]+/i)
                .filter(w => w.length > 3 && !stop.has(w.toLowerCase()))
                .map(w => w.toLowerCase());
        }
        function matchScore(a, b) {
            if (aliasOf(a) === aliasOf(b)) return 100;
            const ta = new Set(tokens(a));
            const tb = new Set(tokens(b));
            if (!ta.size || !tb.size) return 0;
            let overlap = 0;
            ta.forEach(w => { if (tb.has(w)) overlap++; });
            return overlap;
        }
        for (const s of skills) {
            const category = (s.category || '').trim();
            const skillText = (s.skill || '').trim();
            if (!category || !skillText) continue;

            const wordSpans = Array.from(root.querySelectorAll('span[id$="-word"]'));
            let best = null;
            let bestScore = 0;
            for (const ws of wordSpans) {
                const txt = (ws.textContent || '').trim();
                const score = matchScore(category, txt);
                if (score > bestScore) { bestScore = score; best = ws; }
            }

            if (best && bestScore > 0) {
                const baseId = best.id.replace(/-word$/, '');
                const infoSpan = document.getElementById(baseId + '-info');
                if (infoSpan) {
                    // Dedup check: full-text match OR core-name match. The full
                    // skill text often has a qualifier in parens (e.g.
                    // "GitLab (Basic familiarity, currently learning)") that may
                    // not appear verbatim inside an existing bullet, even though
                    // the core skill ("GitLab") already is. Use the first token
                    // before "(" as the core name for the loose check.
                    const haystack = infoSpan.textContent || '';
                    const coreName = skillText.split('(')[0].trim();
                    const alreadyPresent = haystack.includes(skillText)
                        || (coreName.length >= 3 && new RegExp('\\b' + coreName.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&') + '\\b', 'i').test(haystack));
                    if (alreadyPresent) {
                        applied_skills.push(skillText + ' → ' + best.textContent.trim() + ' (already present, skipped)');
                        continue;
                    }
                    const uls = infoSpan.querySelectorAll('ul');
                    const lastUl = uls.length ? uls[uls.length - 1] : null;
                    const lastLi = lastUl ? lastUl.querySelector('li:last-of-type') : null;
                    if (lastLi) {
                        const newLi = lastLi.cloneNode(true);
                        const w2 = document.createTreeWalker(newLi, NodeFilter.SHOW_TEXT);
                        const allN2 = [];
                        let nn2;
                        while ((nn2 = w2.nextNode())) allN2.push(nn2);
                        const visN2 = allN2.filter(nd => !hasHiddenAncestor(nd, newLi));
                        if (visN2.length > 0) {
                            visN2[0].nodeValue = skillText;
                            for (let k = 1; k < visN2.length; k++) visN2[k].nodeValue = '';
                            const bolds = newLi.querySelectorAll('strong, b');
                            bolds.forEach(bld => {
                                const txt = bld.textContent;
                                if (txt) bld.replaceWith(document.createTextNode(txt));
                            });
                        } else {
                            newLi.textContent = skillText;
                        }
                        lastUl.appendChild(newLi);
                        applied_skills.push(skillText + ' → ' + best.textContent.trim() + ' (score=' + bestScore + ', appended)');
                        continue;
                    }
                    infoSpan.appendChild(document.createTextNode(' · ' + skillText));
                    applied_skills.push(skillText + ' → ' + best.textContent.trim() + ' (score=' + bestScore + ', text-append)');
                    continue;
                }
            }
            failed_skills.push(skillText + ' (no category match for "' + category + '")');
        }
    }

    // --- MANUAL TEXT EDITS (run AFTER skills) ---
    // Two-tier strategy:
    //   1. If the find string lands cleanly inside a single text node, do a
    //      simple in-place replace (preserves siblings, fastest, safest).
    //   2. Otherwise, treat it like a whole-bullet replacement: find the
    //      smallest element containing the find text, collapse its text
    //      nodes, and write the replace string. Necessary for FlowCV titles
    //      that are rendered word-by-word across many sibling spans.
    //
    // Process edits LONGEST find FIRST: a short edit like "heute" → "present"
    // can transform a substring inside a longer edit's find string (e.g.
    // "Bachelor of Science: Informatik | TU Dortmund 11/2024 – heute"). If
    // the short edit ran first, the longer edit could no longer match.
    const sorted_edits = manual_edits.slice().sort((a, b) =>
        (b.find || '').length - (a.find || '').length
    );
    for (const m of sorted_edits) {
        const find = m.find;
        const replace = m.replace;
        if (!find || !replace || find === replace) continue;
        if (find.length > 250) {
            failed_edits.push({find: find.slice(0, 60), reason: 'too long — rejected'});
            continue;
        }
        // Reject multi-line finds before the multi-node fallback — they tend
        // to overshoot and collapse adjacent unrelated content.
        const isMultiline = find.includes('\n');

        // Tier 1: single-text-node substring replace — apply to ALL matches.
        // Some find strings appear multiple times (e.g. "Kassierer" is the
        // role title for both Lidl and Kaufland). Replacing only the first
        // occurrence leaves stale German text in the second position. Walk
        // every text node and replace all occurrences in each.
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        let hit = false;
        let node;
        while ((node = walker.nextNode())) {
            if (node.nodeValue && node.nodeValue.includes(find)) {
                // Use split/join to replace ALL occurrences in this text node
                // (avoids regex special-char issues that String.replaceAll has).
                node.nodeValue = node.nodeValue.split(find).join(replace);
                hit = true;
            }
        }
        if (hit) { applied_edits.push(find.slice(0, 60)); continue; }
        if (isMultiline) {
            failed_edits.push({find: find.slice(0, 60), reason: 'multi-line — multi-node fallback skipped (unsafe)'});
            continue;
        }

        // Tier 2: multi-node — use the same smart finder as bullets
        const target = findSmallestMatch(find);
        if (target) {
            const w = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
            const allN = [];
            let nn;
            while ((nn = w.nextNode())) allN.push(nn);
            const nodes = allN.filter(nd => !hasHiddenAncestor(nd, target));
            if (nodes.length > 0) {
                // Split-bold for "Heading: Body" replacements: same trick as
                // bullets — write the heading into the <strong> text node and
                // the body into the first text node OUTSIDE <strong>. Without
                // this, the manual edit's full replacement lands inside
                // <strong> (e.g. "Aktueller Studienfokus: ..." goes fully
                // bold instead of just the heading).
                let split = false;
                const editColon = replace.indexOf(':');
                if (editColon > 0 && editColon < 80 && nodes.length >= 2) {
                    const strongEl = target.querySelector('strong, b');
                    if (strongEl) {
                        let strongText = null;
                        let outsideText = null;
                        for (const nd of nodes) {
                            if (strongEl.contains(nd)) {
                                if (!strongText) strongText = nd;
                            } else {
                                if (!outsideText) outsideText = nd;
                            }
                        }
                        if (strongText && outsideText) {
                            const head = replace.slice(0, editColon + 1);
                            let body = replace.slice(editColon + 1);
                            if (!body.startsWith(' ')) body = ' ' + body;
                            strongText.nodeValue = head;
                            outsideText.nodeValue = body;
                            for (const nd of nodes) {
                                if (nd !== strongText && nd !== outsideText) nd.nodeValue = '';
                            }
                            applied_edits.push(find.slice(0, 60) + ' (multi-node split-bold)');
                            split = true;
                        }
                    }
                }
                if (!split) {
                    // Reconstruct: combine all VISIBLE text nodes, do the substring
                    // replace, put the result in the first visible node and blank
                    // the rest. Hidden-ancestor nodes (screen-reader • glyphs)
                    // are left untouched.
                    const combined = nodes.map(x => x.nodeValue).join('');
                    const combinedNorm = combined.replace(/\s+/g, ' ');
                    const findNorm = find.replace(/\s+/g, ' ');
                    if (combinedNorm.includes(findNorm)) {
                        nodes[0].nodeValue = combinedNorm.replace(findNorm, replace);
                    } else {
                        nodes[0].nodeValue = replace;
                    }
                    for (let k = 1; k < nodes.length; k++) nodes[k].nodeValue = '';
                    applied_edits.push(find.slice(0, 60) + ' (multi-node)');
                }
                continue;
            }
        }

        failed_edits.push({find: find.slice(0, 60), reason: 'not found in any text node or element'});
    }

    // Snapshot the modified HTML
    const el = document.querySelector('.resumePage') || root;
    return {
        html: el.outerHTML,
        applied_bullets, failed_bullets,
        applied_edits, failed_edits,
        applied_skills, failed_skills,
    };
}
"""


async def main():
    if not os.path.exists(".tmp/cv_master.html"):
        raise SystemExit(
            "No .tmp/cv_master.html cache. Run: python3 tools/read_cv_flowcv.py"
        )

    with open(".tmp/cv_master.html", "r", encoding="utf-8") as f:
        master_html = f.read()

    tailored = load_json(".tmp/tailored_cv_sections.json")
    bullets = tailored.get("experience_keywords", []) or []
    manual_edits = tailored.get("manual_text_edits", []) or []
    skills = tailored.get("skills_added", []) or []

    print(f"Bullets to rewrite: {len(bullets)}")
    for b in bullets:
        print(f"  - [{b.get('company','?')}] {(b.get('original_bullet') or '')[:60]}...")
    print(f"Manual text edits: {len(manual_edits)}")
    print(f"Skills to add:     {len(skills)}")

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 1600})
        page = await context.new_page()

        await page.set_content(master_html, wait_until="networkidle", timeout=30000)
        await page.wait_for_selector(".resumePage", timeout=10000)
        await page.wait_for_timeout(500)

        result = await page.evaluate(APPLY_JS, {
            "bullets": bullets,
            "manual_edits": manual_edits,
            "skills": skills,
        })

        if not result or not result.get("html"):
            raise SystemExit("Could not extract .resumePage HTML after edits.")

        ab = result.get("applied_bullets", [])
        fb = result.get("failed_bullets", [])
        ae = result.get("applied_edits", [])
        fe = result.get("failed_edits", [])
        as_ = result.get("applied_skills", [])
        fs = result.get("failed_skills", [])

        print(f"\n[bullets]  applied={len(ab)}  failed={len(fb)}")
        for b in ab:
            print(f"  + [{b.get('company','?')}] ({b.get('mode')}) {b.get('preview')}")
        for b in fb:
            print(f"  ! [{b.get('company','?')}] could not match: {b.get('original')}")

        print(f"[edits]    applied={len(ae)}  failed={len(fe)}")
        for f in fe:
            print(f"  ! '{f.get('find')}' — {f.get('reason')}")

        print(f"[skills]   applied={len(as_)}  failed={len(fs)}")
        for s in as_:
            print(f"  + {s}")
        for f in fs:
            print(f"  ! {f}")

        # Re-render: take the master's <head> (contains all FlowCV styles) and
        # build a fresh body containing only the modified .resumePage.
        new_resume_html = result["html"]
        import re as _re
        head_match = _re.search(r"<head\b[^>]*>.*?</head>", master_html, flags=_re.DOTALL | _re.IGNORECASE)
        head_block = head_match.group(0) if head_match else "<head><meta charset='utf-8'></head>"
        modified_full_html = (
            "<!DOCTYPE html>"
            "<html>"
            f"{head_block}"
            f"<body>{new_resume_html}</body>"
            "</html>"
        )

        # SAFETY NET: brute-force string replace on the final HTML for any
        # bullets/edits the JS DOM-matcher missed. Operates directly on text
        # in the HTML (between tags), so it catches strings split across
        # multiple sibling spans that the DOM-walker can't pin down.
        # We use ALL the (original_bullet, proposed_bullet) pairs and ALL the
        # manual_text_edits — even the ones that already succeeded, since
        # idempotent replacement of an already-translated string is a no-op.
        from html import escape as _esc

        def _safety_replace(html_text: str, find: str, replace: str) -> tuple[str, bool]:
            if not find or not replace or find == replace:
                return html_text, False
            if find in html_text:
                return html_text.replace(find, replace, 1), True
            find_collapsed = " ".join(find.split())
            if find_collapsed and find_collapsed != find and find_collapsed in html_text:
                return html_text.replace(find_collapsed, replace, 1), True
            find_escaped = _esc(find)
            if find_escaped != find and find_escaped in html_text:
                return html_text.replace(find_escaped, _esc(replace), 1), True
            return html_text, False

        # Bullets the JS already applied — skip them to avoid re-application
        # bugs. Common case: original_bullet is a PREFIX of proposed_bullet
        # (Claude expanded a short bullet into a longer one). After JS writes
        # the new prop into the DOM, the safety net's direct replace would
        # find the orig as a substring of the new prop and re-apply, doubling
        # the suffix (e.g. "...Marktleitung. – zuverlässige... – zuverlässige...").
        # JS reports applied bullets via the proposed_bullet preview (60 chars).
        applied_bullet_previews = set()
        for ab_entry in ab:
            prv = ab_entry.get("preview") if isinstance(ab_entry, dict) else None
            if prv:
                applied_bullet_previews.add(prv)

        safety_hits_bullets = 0
        for b in bullets:
            orig = (b.get("original_bullet") or "").strip().lstrip("•·* ").strip()
            prop = (b.get("proposed_bullet") or "").strip().lstrip("•·* ").strip()
            if not orig or not prop:
                continue

            # Skip if JS already applied this bullet (preview matches).
            if prop[:60] in applied_bullet_previews:
                continue

            # First try the whole bullet as one replacement.
            modified_full_html, ok = _safety_replace(modified_full_html, orig, prop)
            if ok:
                safety_hits_bullets += 1
                continue

            # Multi-line fallback: FlowCV often splits user line breaks (\n\n)
            # into separate sibling divs. Split the original on blank-line
            # boundaries and replace each chunk with a slice of the proposed
            # text. The first chunk gets the full English bullet, subsequent
            # chunks get a single zero-width space (so the now-empty div
            # disappears visually but doesn't break layout).
            orig_chunks = [c.strip() for c in orig.split("\n\n") if c.strip()]
            if len(orig_chunks) <= 1:
                # also try single newline
                orig_chunks = [c.strip() for c in orig.split("\n") if c.strip()]
            if len(orig_chunks) <= 1:
                continue

            chunk_hits = 0
            # Process chunks in REVERSE order: clear all continuation chunks
            # (chunks 1..N, replaced with zero-width space) BEFORE chunk[0]
            # is replaced with the full proposed bullet. If we did chunk[0]
            # first, its full-prop text often contains the same words as
            # chunk[1+] (since the new bullet rewrites the whole multi-line
            # original into one sentence), and the subsequent zero-width
            # replaces would hit the wrong location inside the new bullet
            # text, corrupting the heading and leaving the original
            # continuations untouched.
            chunk_order = list(range(len(orig_chunks) - 1, -1, -1))
            for i in chunk_order:
                chunk = orig_chunks[i]
                replacement = prop if i == 0 else "​"  # zero-width space
                # Heading-only chunk[0] trap: when chunk[0] ends with ":" the
                # literal find can land entirely INSIDE the <strong> tag (e.g.
                # "KPI-Auswertung &amp; Root Cause Analysis:" matches inside
                # <strong>...:</strong>). Direct replace then stuffs the full
                # proposed text — body and all — into the <strong>, bolding
                # everything. Skip direct replace and force colon-split.
                is_heading_only = (i == 0
                                   and chunk.rstrip().endswith(":")
                                   and ":" in prop
                                   and prop.split(":", 1)[1].strip())
                if is_heading_only:
                    ok = False
                else:
                    modified_full_html, ok = _safety_replace(modified_full_html, chunk, replacement)
                if ok:
                    chunk_hits += 1
                    continue
                # Special case for chunk 0: it usually has the form
                # "<strong>Heading</strong>: Body". The literal find can't
                # match across the </strong> boundary. Split at the first
                # colon and replace heading + body separately.
                if i == 0 and ":" in chunk:
                    head, body = chunk.split(":", 1)
                    head, body = head.strip(), body.strip()
                    if ":" in prop:
                        new_head, new_body_full = prop.split(":", 1)
                        new_head, new_body_full = new_head.strip(), new_body_full.strip()
                    else:
                        new_head, new_body_full = prop, ""
                    # Try BOTH the raw and HTML-escaped form of the heading
                    # (FlowCV escapes & → &amp;, but other characters too).
                    import re as _r
                    head_variants = [head, _esc(head)]
                    body_variants = [body, _esc(body)]
                    new_head_html = _esc(new_head) if "&" in new_head or "<" in new_head else new_head
                    new_body_html = _esc(new_body_full) if "&" in new_body_full or "<" in new_body_full else new_body_full
                    head_replaced = False
                    colon_inside_strong = False
                    # Heading-only chunk: chunk text was just "Heading:" with
                    # no body — the body comes from chunks[1+] (which we just
                    # zero-width-replaced) or doesn't exist in the original.
                    # We need to inject the new body alongside the new head,
                    # otherwise the body is lost entirely.
                    inject_body_with_head = (not body) and bool(new_body_full)
                    for hv in head_variants:
                        if not hv:
                            continue
                        # Variant A: <strong>head</strong> — colon outside the bold
                        pattern_a = _r.compile(
                            r"(<(?:strong|b)\b[^>]*>)" + _r.escape(hv) + r"(</(?:strong|b)>)",
                            flags=_r.IGNORECASE,
                        )
                        if inject_body_with_head:
                            replacement_a = r"\1" + new_head_html + r"\2: " + new_body_html
                        else:
                            replacement_a = r"\1" + new_head_html + r"\2"
                        new_html, n = pattern_a.subn(replacement_a, modified_full_html, count=1)
                        if n:
                            modified_full_html = new_html
                            chunk_hits += 1
                            head_replaced = True
                            break
                        # Variant B: <strong>head:</strong> — colon INSIDE the bold
                        pattern_b = _r.compile(
                            r"(<(?:strong|b)\b[^>]*>)" + _r.escape(hv) + r":(</(?:strong|b)>)",
                            flags=_r.IGNORECASE,
                        )
                        if inject_body_with_head:
                            replacement_b = r"\1" + new_head_html + r":\2 " + new_body_html
                        else:
                            replacement_b = r"\1" + new_head_html + r":\2"
                        new_html, n = pattern_b.subn(replacement_b, modified_full_html, count=1)
                        if n:
                            modified_full_html = new_html
                            chunk_hits += 1
                            head_replaced = True
                            colon_inside_strong = True
                            break
                    if body:
                        # When colon was inside <strong>, the body in HTML has no
                        # leading ": " — it's just " body" after </strong>.
                        body_prefix = " " if colon_inside_strong else ": "
                        for bv in body_variants:
                            if not bv:
                                continue
                            modified_full_html, ok2 = _safety_replace(
                                modified_full_html, body_prefix + bv,
                                body_prefix + new_body_html
                            )
                            if ok2:
                                chunk_hits += 1
                                break
            if chunk_hits:
                safety_hits_bullets += 1
                print(f"  [safety] split-bullet replace ({chunk_hits} sub-replacements): "
                      f"{prop[:60]}...")

        safety_hits_edits = 0
        # Edits the JS already applied — skip them in the safety net to avoid
        # re-application bugs like "Muttersprache" → "Muttersprache (Native)"
        # being applied twice (the find string is still a substring of the
        # result, so the safety net would compound to "(Native) (Native)").
        # The JS reports applied edits as truncated find strings — we keep
        # full strings here for unambiguous skip matching.
        applied_finds = set()
        for ae_entry in ae:
            # ae_entry format: "<find[:60]>" or "<find[:60]> (multi-node)"
            base = ae_entry.replace(" (multi-node)", "")
            applied_finds.add(base)
        for m in manual_edits:
            f_text = m.get("find") or ""
            r_text = m.get("replace") or ""
            if f_text[:60] in applied_finds:
                continue
            modified_full_html, ok = _safety_replace(modified_full_html, f_text, r_text)
            if ok:
                safety_hits_edits += 1
                continue
            # Multi-line manual edit fallback: split both sides on blank-line
            # boundaries and replace each pair (assume same line count).
            f_lines = [ln.strip() for ln in f_text.split("\n\n") if ln.strip()]
            r_lines = [ln.strip() for ln in r_text.split("\n\n") if ln.strip()]
            if len(f_lines) <= 1:
                f_lines = [ln.strip() for ln in f_text.split("\n") if ln.strip()]
                r_lines = [ln.strip() for ln in r_text.split("\n") if ln.strip()]
            if len(f_lines) > 1 and len(f_lines) == len(r_lines):
                line_hits = 0
                for fl, rl in zip(f_lines, r_lines):
                    modified_full_html, lok = _safety_replace(modified_full_html, fl, rl)
                    if lok:
                        line_hits += 1
                if line_hits:
                    safety_hits_edits += 1
                    print(f"  [safety] split-edit {line_hits}/{len(f_lines)}: {f_lines[0][:50]}...")

        if safety_hits_bullets or safety_hits_edits:
            print(f"\n[safety net] post-DOM string replace caught: "
                  f"bullets={safety_hits_bullets}, edits={safety_hits_edits}")

        with open(".tmp/tailored_resume_render.html", "w", encoding="utf-8") as f:
            f.write(modified_full_html)

        # Render PDF from the modified HTML
        clean_page = await context.new_page()
        await clean_page.set_content(modified_full_html, wait_until="networkidle", timeout=30000)
        await clean_page.wait_for_timeout(1000)

        # Cleanup pass: the safety net's chunk-split replaces continuation
        # chunks with a zero-width space (​) so the original German text
        # disappears, but the surrounding <p>/<div> still occupies vertical
        # space (paragraph margin). Remove leaf elements whose only text
        # content is whitespace + zero-width space, so multi-line bullets
        # collapse cleanly with no gaps to neighboring siblings.
        zws_removed = await clean_page.evaluate(r"""
            () => {
                const ZWS = '​';
                let removed = 0;
                const candidates = Array.from(document.querySelectorAll('p, div, span'));
                for (const el of candidates) {
                    if (el.children.length > 0) continue;       // leaf only
                    if (el.classList && el.classList.contains('resumePage')) continue;
                    const t = el.textContent || '';
                    if (t.includes(ZWS) && t.replace(/[​\s ]/g, '') === '') {
                        el.remove();
                        removed++;
                    }
                }
                return removed;
            }
        """)
        if zws_removed:
            print(f"  [cleanup] removed {zws_removed} empty zero-width-space placeholder element(s)")

        await clean_page.emulate_media(media="print")
        await clean_page.pdf(
            path=".tmp/tailored_cv.pdf",
            format="A4",
            print_background=True,
            margin={"top": "0", "right": "0", "bottom": "0", "left": "0"},
        )
        print("\nPDF saved to .tmp/tailored_cv.pdf")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
