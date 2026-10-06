#!/usr/bin/env python3
"""firstturn: check what callers actually heard, call by call.

telli's builder check reads the agent *script* and looks for an AI disclosure
in the first four spoken sentences. This reads the *live* transcripts from
GET /v1/get-call/{id} and asks the question the EU AI Act page asks: did the
person hear "AI" before the conversation turned to the topic or to their data,
and was recording consented to before it started?

Usage:
  python3 firstturn.py demo/calls.json --html report.html
  TELLI_API_KEY=... python3 firstturn.py --fetch <call-id> [<call-id> ...]
Exit code 1 when any call fails, so it can gate a release in CI.
"""
import argparse
import html
import json
import os
import re
import sys
import urllib.request

I = re.I

AI_DISCLOSURE = re.compile(
    r"\b(?:ai|a\.i\.|artificial intelligence|virtual|digital|automated)[- ](?:assistant|agent|receptionist)\b"
    r"|\bi(?:'m| am) (?:an? )?(?:ai|a\.i\.|bot|robot|virtual assistant)\b"
    r"|\bnot a (?:real )?(?:human|person)\b"
    r"|\bki[- ]?(?:assistent|agent|stimme)\w*"
    r"|\bk[üu]nstliche[nr]? intelligenz\b"
    r"|\b(?:virtuelle|digitale)[rn]? (?:assistent|sprachassistent)\w*"
    r"|\bich bin (?:eine?|ein) (?:ki|bot|roboter)\b"
    r"|\bkein mensch\b",
    I,
)
# A greeting that stops mid-disclosure: the caller talked over it.
CUT_STEM = re.compile(r"(?:ich bin (?:eine?|ein)|i'?m an?|i am an?|this is an?)\s*[-,]?\s*$", I)
ASKS_IF_AI = re.compile(
    r"\b(?:are you|is this|am i (?:talking|speaking) (?:to|with)) (?:a |an )?(?:real )?(?:human|person|robot|bot|machine|ai|computer)\b"
    r"|\b(?:bist du|sind sie|spreche ich mit) (?:ein(?:e|em|er)? )?(?:echte[nr]? )?(?:mensch|person|roboter|bot|maschine|ki|computer)\w*",
    I,
)
SCREENING = re.compile(
    r"record your name and reason for calling|using a screening service|say your name and why you'?re calling"
    r"|see if this person is available|nennen sie (?:bitte )?ihren namen und den grund|grund ihres anrufs"
    r"|ob die person verf[üu]gbar ist",
    I,
)
REC_NOTICE = re.compile(
    r"\b(?:call|conversation) (?:is|will be|may be) (?:being )?recorded\b|\brecord(?:ing)? (?:this|the|our) (?:call|conversation)\b"
    r"|\baufgezeichnet\b|\baufzeichn(?:en|ung)\b|\bmitschnitt\b",
    I,
)
YES = re.compile(r"^\W*(?:yes|yeah|yep|sure|ok(?:ay)?|fine|that'?s fine|go ahead|ja|jawohl|klar|einverstanden|in ordnung|passt|gerne)\b", I)
NO = re.compile(r"^\W*(?:no|nope|nein)\b|\b(?:rather not|bitte nicht|lieber nicht|don'?t record|nicht aufnehmen)\b", I)
TOPIC = re.compile(
    r"\b(?:contract|appointment|invoice|bill|payment|order|tariff|offer|insurance|policy|claim|meter|delivery|"
    r"account|booking|renewal|subscription|balance|overdue|quote|damage|repair|installation|"
    r"vertrag\w*|termin\w*|rechnung\w*|zahlung\w*|bestellung\w*|tarif\w*|angebot\w*|versicherung\w*|police|"
    r"schaden\w*|z[äa]hler\w*|lieferung\w*|konto|buchung\w*|verl[äa]ngerung|abo|guthaben|mahnung\w*|"
    r"reparatur\w*|k[üu]ndigung\w*|abschlag\w*)\b",
    I,
)
ASKS_PII = re.compile(
    r"\b(?:date of birth|birthday|iban|bank details|home address|postcode|customer number|account number|policy number|"
    r"geburtsdatum|bankverbindung|anschrift|adresse|postleitzahl|kundennummer|vertragsnummer)\b",
    I,
)
PII = re.compile(
    r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}\b"  # IBAN
    r"|\b\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2}\b"  # date of birth
    r"|\b[\w.+-]+@[\w-]+\.[\w.]+\b"  # email
    r"|\b\d{6,}\b"  # customer / contract number
    r"|\b\w+(?:stra(?:ss|ß)e|str\.|street|road|weg)\s+\d+",  # street address
    I,
)
SENTENCE = re.compile(r"[^.!?…]+[.!?…]+")
GERMAN_PREFIXES = ("+49",)  # explicit recording consent (telli EU AI Act page)


def turns_of(call):
    return call.get("transcriptObject") or []


def contact_number(call):
    return call.get("to_number" if call.get("direction") == "outbound" else "from_number") or ""


def check(call):
    """Return (verdict, findings, marks). findings: (level, code, turn, text). marks: turn -> css class."""
    turns, findings, marks = turns_of(call), [], {}

    def add(level, code, turn, text):
        findings.append((level, code, turn, text))
        if turn is not None:
            marks.setdefault(turn, "bad" if level == "FAIL" else "warn")

    status = call.get("status")
    if status == "voicemail":
        said = " ".join(t.get("content", "") for t in turns if t.get("role") == "agent")
        if said and not AI_DISCLOSURE.search(said):
            add("WARN", "voicemail-no-disclosure", None, "Voicemail message never says it is an AI; the person hears it later without a live greeting.")
        return ("WARN" if findings else "PASS"), findings, marks
    if status not in (None, "connected"):
        return "SKIP", [("INFO", "not-connected", None, f"status={status}")], marks

    # Where did the real person start listening? After iOS call screening, the greeting went to the screener.
    pickup = 0
    for i, t in enumerate(turns):
        if t.get("role") == "user" and SCREENING.search(t.get("content", "")):
            pickup = i + 1
    if pickup:
        while pickup < len(turns) and turns[pickup].get("role") != "user":
            pickup += 1

    disclosure = topic = pii_turn = None
    for i, t in enumerate(turns):
        text, role = t.get("content", ""), t.get("role")
        if role == "agent" and text and i >= pickup:  # tool calls are silent; only what was said counts
            d, m = AI_DISCLOSURE.search(text), TOPIC.search(text) or ASKS_PII.search(text)
            if d and disclosure is None and i >= pickup:
                disclosure = (i, d.start())
            if m and topic is None:
                topic = (i, m.start())
        if role == "user" and text and pii_turn is None and i >= pickup and PII.search(text):
            pii_turn = i

    if disclosure:
        marks[disclosure[0]] = "ok"
    if pickup and disclosure is None and any(
        t.get("role") == "agent" and AI_DISCLOSURE.search(t.get("content", "")) for t in turns[:pickup]
    ):
        add("FAIL", "disclosed-to-screener", pickup, "The AI disclosure was spoken to iOS call screening. The person who picked up never heard it.")
    elif disclosure is None:
        cut = next((i for i, t in enumerate(turns) if t.get("role") == "agent" and CUT_STEM.search(t.get("content", ""))), None)
        if cut is not None:
            add("FAIL", "disclosure-cut-off", cut, "The greeting was cut off mid-disclosure and the agent never repeated it.")
        else:
            add("FAIL", "no-disclosure", None, "No AI disclosure anywhere in the call.")
    if topic and (disclosure is None or topic < disclosure):
        add("FAIL", "topic-before-disclosure", topic[0], "The agent turned to the topic before saying it is an AI.")
    if pii_turn is not None and (disclosure is None or pii_turn < disclosure[0]):
        add("FAIL", "data-before-disclosure", pii_turn, "The person shared personal data before hearing it is an AI.")

    for i, t in enumerate(turns):
        if t.get("role") == "user" and ASKS_IF_AI.search(t.get("content", "")):
            reply = next((r for r in turns[i + 1:] if r.get("role") == "agent" and r.get("content")), None)
            if reply is None or not AI_DISCLOSURE.search(reply.get("content", "")):
                add("FAIL", "asked-not-told", i, "Asked directly whether it is an AI; the next answer did not say so.")

    # Recording: recording_url exists only when audio was kept.
    if call.get("recording_url"):
        german = contact_number(call).startswith(GERMAN_PREFIXES)
        notice = next((i for i, t in enumerate(turns) if t.get("role") == "agent" and REC_NOTICE.search(t.get("content", ""))), None)
        answer = next((i for i, t in enumerate(turns) if notice is not None and i > notice and t.get("role") == "user"), None)
        said = turns[answer].get("content", "") if answer is not None else ""
        if notice is None:
            add("FAIL", "recorded-no-notice", None, "Audio was recorded but the person was never told.")
        elif answer is not None and NO.search(said):
            add("FAIL", "recorded-after-refusal", answer, "The person refused recording, and a recording exists.")
        elif german and not (answer is not None and YES.search(said)):
            add("FAIL", "recorded-no-consent", notice, "German number: recording needs explicit consent, and there is no clear yes.")
        elif topic and notice > topic[0]:
            add("WARN", "recording-notice-late", notice, "Recording was announced after the conversation had started on the topic.")

    if disclosure:
        before = " ".join(t.get("content", "") for t in turns[: disclosure[0]] if t.get("role") == "agent")
        before += " " + turns[disclosure[0]].get("content", "")[: disclosure[1]]
        n = len(SENTENCE.findall(before)) + 1
        if n > 4:
            add("WARN", "outside-first-four", disclosure[0], f"Disclosure came at spoken sentence {n}, outside the four-sentence window the builder check uses.")

    levels = {f[0] for f in findings}
    return ("FAIL" if "FAIL" in levels else "WARN" if "WARN" in levels else "PASS"), findings, marks


def load(path):
    text = open(path, encoding="utf-8").read().strip()
    data = json.loads(text) if text.startswith("[") or text.startswith("{") and "\n{" not in text else [json.loads(l) for l in text.splitlines() if l.strip()]
    data = data if isinstance(data, list) else [data]
    return [d.get("call", d) for d in data]


def fetch(call_id, key):
    req = urllib.request.Request(f"https://api.telli.com/v1/get-call/{call_id}", headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("call")


CSS = """
:root{--bg:#f7f7f5;--card:#fff;--ink:#16171a;--mute:#6b6f76;--line:#e6e6e2;--ok:#127a46;--okbg:#e6f4ec;--warn:#9a5b00;--warnbg:#fdf1dc;--bad:#b42318;--badbg:#fde8e6}
@media (prefers-color-scheme:dark){:root{--bg:#111214;--card:#1a1b1e;--ink:#ececec;--mute:#9a9da3;--line:#2a2c30;--ok:#5fd39a;--okbg:#14301f;--warn:#f0b54d;--warnbg:#33270f;--bad:#ff8a80;--badbg:#3a1714}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 16px 64px}h1{font-size:28px;margin:0 0 4px;letter-spacing:-.02em}
.sub{color:var(--mute);margin:0 0 24px}.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:28px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}.stat b{display:block;font-size:26px}
.stat span{color:var(--mute);font-size:13px}.call{background:var(--card);border:1px solid var(--line);border-radius:16px;margin:0 0 14px;overflow:hidden}
summary{cursor:pointer;list-style:none;padding:14px 18px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}summary::-webkit-details-marker{display:none}
.pill{font-size:12px;font-weight:700;padding:3px 10px;border-radius:99px}.PASS{color:var(--ok);background:var(--okbg)}.WARN{color:var(--warn);background:var(--warnbg)}
.FAIL{color:var(--bad);background:var(--badbg)}.SKIP{color:var(--mute);background:var(--line)}.meta{color:var(--mute);font-size:13px}.first{flex:1 1 300px}
.body{padding:0 18px 18px}.finding{margin:6px 0;font-size:14px}.turns{margin-top:12px;border-top:1px solid var(--line);padding-top:12px}
.t{display:grid;grid-template-columns:28px 64px 1fr;gap:8px;padding:6px 8px;border-radius:8px;font-size:14px}.t .n,.t .r{color:var(--mute);font-size:12px;padding-top:2px}
.t.ok{background:var(--okbg)}.t.warn{background:var(--warnbg)}.t.bad{background:var(--badbg)}.tool{color:var(--mute);font-style:italic}
code{font-size:12px;color:var(--mute)}
"""


def report(results):
    counts = {k: sum(1 for r in results if r[1] == k) for k in ("PASS", "WARN", "FAIL", "SKIP")}
    cards = []
    for call, verdict, findings, marks in results:
        rows = []
        for i, t in enumerate(turns_of(call)):
            body = f'<span class="tool">tool: {html.escape(t["toolActivity"])}</span>' if "toolActivity" in t else html.escape(t.get("content", ""))
            rows.append(f'<div class="t {marks.get(i, "")}"><span class="n">{i}</span><span class="r">{t.get("role")}</span><span>{body}</span></div>')
        fl = "".join(f'<div class="finding"><span class="pill {lv if lv != "INFO" else "SKIP"}">{lv}</span> {html.escape(tx)} <code>{c}{"" if n is None else f" @turn {n}"}</code></div>' for lv, c, n, tx in findings)
        first = html.escape(findings[0][3]) if findings else "Disclosure heard before the topic." + (" Recording consented." if call.get("recording_url") else "")
        cards.append(
            f'<details class="call"{" open" if verdict == "FAIL" else ""}><summary><span class="pill {verdict}">{verdict}</span>'
            f'<span class="meta">{html.escape(call.get("call_id", "")[:8])} · {call.get("direction", "")} · {html.escape(contact_number(call)[:3])}</span>'
            f'<span class="first">{first}</span></summary><div class="body">{fl}<div class="turns">{"".join(rows)}</div></div></details>'
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>firstturn report</title><style>{CSS}</style></head><body><main>
<h1>What callers actually heard</h1>
<p class="sub">{len(results)} calls from GET /v1/get-call. A script check reads the configured greeting; these verdicts come from what was said on each call: who spoke first, iOS call screening, barge-in, improvised answers, recording consent.</p>
<div class="stats">{"".join(f'<div class="stat"><b>{counts[k]}</b><span>{k.lower()}</span></div>' for k in counts)}</div>
{"".join(cards)}</main></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="Get Call JSON: one response, a list, or JSONL")
    ap.add_argument("--fetch", nargs="+", metavar="CALL_ID", help="pull calls from api.telli.com (needs TELLI_API_KEY)")
    ap.add_argument("--html", help="write a shareable HTML report here")
    a = ap.parse_args()
    calls = [c for f in a.files for c in load(f)]
    if a.fetch:
        key = os.environ.get("TELLI_API_KEY") or sys.exit("set TELLI_API_KEY")
        calls += [fetch(i, key) for i in a.fetch]
    if not calls:
        ap.error("no calls given")
    results = [(c, *check(c)) for c in calls]
    for call, verdict, findings, _ in results:
        first = findings[0] if findings else ("", "", None, "")
        print(f"{verdict:4}  {call.get('call_id', '')[:8]:8}  {call.get('direction', ''):8}  {first[1]:26} {first[3]}")
    if a.html:
        open(a.html, "w", encoding="utf-8").write(report(results))
        print(f"\nreport: {a.html}")
    sys.exit(1 if any(r[1] == "FAIL" for r in results) else 0)


if __name__ == "__main__":
    main()
