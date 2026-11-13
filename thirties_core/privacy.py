"""What never leaves the computer for a cloud model: nothing extremely sensitive.

One filter, applied at the single exit to any cloud model (``inference.py``: agy and the Gemini API, for app
turns, background jobs and every eval harness alike). The on-device model is left alone: nothing leaves the
machine.

It is deliberately light: patterns and labels, nothing heavy.

- **Secrets:** labelled passwords, PINs, keys, tokens, 2FA and backup codes ("password: ..."), key-shaped
  strings (known key prefixes, private key blocks, JWTs) and links with a password or token in them.
- **Financial numbers:** card numbers (Luhn-checked), IBANs (checksum-checked), labelled bank account and
  routing numbers.
- **Government IDs:** Social Security numbers, and labelled passport, licence and ID numbers.
- **Medical details:** diagnosis and medication names become their category ("[medication]"); the bodies of
  private notes are never sent (a note tagged "private" in Joplin, or any list named Medical, Health or
  Finance), and their items go as short titles only.

Each value is replaced with a placeholder ("[password]", "[card ..1234]"). The mapping stays in this process, so
a reply or a tool call that uses a placeholder is turned back into the real value before anything acts on it.
Only counts of what was filtered are logged, never the values.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

# --- secrets -------------------------------------------------------------------------------------------------

_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S)
_KEY_SHAPES = re.compile(
    r"(?<![\w-])(?:"
    r"AIza[0-9A-Za-z_\-]{35}"                      # Google API key
    r"|sk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}"     # OpenAI / Anthropic style
    r"|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    r"|xox[abposr]-[A-Za-z0-9\-]{10,}"            # Slack
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"                 # AWS access key id
    r"|glpat-[A-Za-z0-9_\-]{20,}"
    r"|\d{8,10}:AA[A-Za-z0-9_\-]{30,}"            # Telegram bot token
    r"|eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"  # JWT
    r")(?![\w-])")
_URL_CREDENTIALS = re.compile(r"(?P<head>\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:)(?P<value>[^\s/@]+)(?P<tail>@)", re.I)
_URL_SECRET_PARAM = re.compile(
    r"(?P<head>[?&](?:password|passwd|pwd|pass|token|access_token|auth|api_?key|key|secret|sig|signature|code)=)"
    r"(?P<value>[^&\s#]+)", re.I)

_SECRET_LABELS = (
    ("password", r"(?:wi-?fi |email |account |login |admin |root )?pass(?:word|wd|code|phrase)|pwd"),
    ("PIN", r"(?:pin|pin code|pin number)"),
    ("security code", r"(?:security code|cvv2?|cvc|csc)"),
    ("API key", r"(?:api[ _-]?key|access[ _-]?key|secret[ _-]?key|client[ _-]?secret|app[ _-]?password)"),
    ("token", r"(?:(?:auth|access|bearer|bot|api|refresh)[ _-]?token|token)"),
    ("secret", r"secret"),
)
_CODE_LABELS = r"(?:2fa|two[- ]factor|backup|recovery|one[- ]time|otp|verification|mfa)(?: codes?| keys?| pins?)?"
_ASSIGN = r"\s*(?:[:=]|\bis\b|\bwas\b)\s*"


def _labelled(label: str) -> re.Pattern:
    return re.compile(rf"(?P<head>(?<!\[)\b(?:my |the |our )?(?:{label})(?:s)?{_ASSIGN}[\"']?)"
                      rf"(?P<value>[^\s\"',;]*[^\s\"',;.!?)\]])", re.I)


_SECRET_PATTERNS = [(kind, _labelled(label)) for kind, label in _SECRET_LABELS]
_CODES = re.compile(rf"(?P<head>\b{_CODE_LABELS}\s*[:=]\s*)(?P<value>[A-Za-z0-9\-]*\d[A-Za-z0-9\-]*(?:[,;]?\s+[A-Za-z0-9\-]*\d[A-Za-z0-9\-]*)*)", re.I)


def _looks_secret(value: str, head: str) -> bool:
    """After "is"/"was", only a value that looks like one counts ("the password is on the fridge" doesn't)."""
    if re.search(r"[:=]\s*$", head):
        return True
    v = value.strip("\"'")
    return bool(re.search(r"\d", v) or re.search(r"[^A-Za-z]", v) or (v != v.lower() and v != v.capitalize()))


# --- financial numbers and IDs -------------------------------------------------------------------------------

_CARD = re.compile(r"(?<![\w.\-])(?:\d[ \-]?){12,18}\d(?![\w\-])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b")
_SSN = re.compile(r"(?<![\w\-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\w\-])")
_SSN_LABELLED = re.compile(r"(?P<head>\b(?:ssn|social security(?: number| no\.?| #)?)\s*(?:[:#]|\bis\b)?\s*)"
                           r"(?P<value>\d{3}[ \-]?\d{2}[ \-]?\d{4})\b", re.I)
_ACCOUNT = re.compile(r"(?P<head>\b(?:bank )?(?:account|acct|a/c)(?: number| no\.?| #|#)?\s*"
                      r"(?:[:#]|\bis\b)?\s*)(?P<value>\d[\d \-]{3,20}\d)\b", re.I)
_ROUTING = re.compile(r"(?P<head>\b(?:routing|aba|sort code|transit|bsb)(?: number| no\.?| #|#)?\s*"
                      r"(?:[:#]|\bis\b)?\s*)(?P<value>\d[\d \-]{4,10}\d)\b", re.I)
_GOV_ID = re.compile(r"(?P<head>\b(?:passport|driver'?s? licen[cs]e|licen[cs]e|dl|state id|id card|national id|"
                     r"national insurance|ni|medicare|medicaid|member id|tax id|tin|ein|itin)"
                     r"(?: number| no\.?| #|#)?\s*(?:[:#]|\bis\b)\s*)(?P<value>[A-Z0-9][A-Z0-9\-]{4,})\b", re.I)


def luhn_ok(digits: str) -> bool:
    total, odd = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if odd:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        odd = not odd
    return total % 10 == 0


def iban_ok(text: str) -> bool:
    s = text.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    s = s[4:] + s[:4]
    try:
        return int("".join(str(int(c, 36)) for c in s)) % 97 == 1
    except ValueError:
        return False


# --- medical words -------------------------------------------------------------------------------------------

_CONDITIONS = (
    "cancer", "chemo", "chemotherapy", "radiotherapy", "tumou?r", "biopsy", "oncolog(?:y|ist)", "leukemia",
    "lymphoma", "melanoma", "diabetes", "diabetic", "hepatitis", "herpes", "chlamydia", "gonorrh?oea|gonorrhea",
    "syphilis", "depression", "depressive", "anxiety", "panic attacks?", "panic disorder", "bipolar",
    "schizo(?:phrenia|affective)", "autism", "autistic", "dementia", "alzheimer'?s?", "parkinson'?s?", "epilepsy",
    "epileptic", "seizures?", "multiple sclerosis", "lupus", "crohn'?s?", "ulcerative colitis", "anorexia",
    "bulimia", "eating disorder", "miscarriage", "abortion", "infertility", "pregnancy test", "overdose",
    "suicidal", "suicide", "self[- ]harm", "addiction", "alcoholism", "rehab", "hypertension", "arrhythmia",
    "heart failure", "asthma", "sleep apnea", "endometriosis", "hypothyroidism", "hyperthyroidism",
    "kidney disease", "dialysis", "cirrhosis", "colonoscopy", "mammogram", "pap smear", "std test",
)
_CONDITION_ACRONYMS = ("HIV", "AIDS", "ADHD", "OCD", "PTSD", "STD", "STDs", "STI", "STIs", "IVF", "COPD", "PCOS",
                       "HPV", "UTI", "IBS")
_MEDICATIONS = (
    "insulin", "metformin", "ozempic", "wegovy", "mounjaro", "adderall", "ritalin", "vyvanse", "concerta",
    "lexapro", "zoloft", "prozac", "paxil", "celexa", "wellbutrin", "cymbalta", "effexor", "xanax", "valium",
    "klonopin", "ativan", "ambien", "lithium", "lamictal", "seroquel", "abilify", "risperdal", "zyprexa",
    "prednisone", "gabapentin", "lyrica", "levothyroxine", "synthroid", "suboxone", "naltrexone", "methadone",
    "buprenorphine", "truvada", "descovy", "plan b", "birth control", "the pill", "accutane", "humira",
    "eliquis", "xarelto", "warfarin", "lipitor", "crestor", "tramadol", "oxycodone", "hydrocodone", "vicodin",
    "percocet", "fentanyl", "morphine", "methylphenidate", "amphetamine", "lisdexamfetamine", "sertraline",
    "fluoxetine", "escitalopram", "citalopram", "paroxetine", "bupropion", "venlafaxine", "duloxetine",
    "trazodone", "mirtazapine", "quetiapine", "aripiprazole", "olanzapine", "risperidone", "lamotrigine",
    "valproate", "topiramate", "clonazepam", "lorazepam", "alprazolam", "diazepam", "zolpidem", "semaglutide",
    "tirzepatide", "spironolactone", "finasteride", "minoxidil", "testosterone", "estradiol", "progesterone",
)
# Name endings shared by whole drug families; long enough not to hit ordinary words.
_MED_SUFFIXES = ("pril", "olol", "statin", "azole", "cillin", "mycin", "prazole", "sartan", "triptan", "azepam",
                 "azolam", "oxetine", "traline", "codone", "morphone", "tidine", "floxacin", "cycline", "gliptin",
                 "gliflozin", "dronate", "lukast", "dipine", "umab", "imab", "tinib", "vudine", "navir")

_CONDITION_RE = re.compile(r"\b(?:" + "|".join(_CONDITIONS) + r")\b", re.I)
_CONDITION_ACRONYM_RE = re.compile(r"\b(?:" + "|".join(_CONDITION_ACRONYMS) + r")\b")
_MEDICATION_RE = re.compile(r"\b(?:" + "|".join(re.escape(m) for m in _MEDICATIONS) + r")\b", re.I)
_MED_SUFFIX_RE = re.compile(r"\b[A-Za-z]{3,}(?:" + "|".join(_MED_SUFFIXES) + r")\b", re.I)

# --- private notes -------------------------------------------------------------------------------------------

PRIVATE_TAG = "private"
PRIVATE_LIST_RE = re.compile(r"\b(?:medical|health|healthcare|finances?|financial)\b", re.I)
_CHECKBOX = re.compile(r"^\s*[-*+]\s+\[[ xX]\]\s+(.*\S)\s*$")
PRIVATE_BODY = "[private note: contents not shown]"
# All the placeholder kinds the filter makes: one left in text that's about to be written into the user's lists means
# the real wording would be lost there (leftover_placeholder)
PLACEHOLDER_KINDS = ("private key", "password", "PIN", "security code", "API key", "token", "secret", "backup codes",
                     "SSN", "IBAN", "card", "routing number", "account number", "account", "ID number", "condition",
                     "medication", "private note")
_LEFTOVER = re.compile(r"\[\s*(?:" + "|".join(re.escape(k).replace(r"\ ", r"\s*")
                                             for k in sorted(PLACEHOLDER_KINDS, key=len, reverse=True))
                       + r")(?:\s*(?:\d+|\.\.\s*\d{2,4}|:[^\]\n]{0,40}))?\s*(?:\]|(?![\w.]))", re.IGNORECASE)


def leftover_placeholder(value: Any) -> Optional[str]:
    """The first placeholder still in ``value`` (a string, or the strings inside a list or dict), or None."""
    if isinstance(value, str):
        m = _LEFTOVER.search(value)
        return m.group(0) if m else None
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        return next((found for v in value for found in [leftover_placeholder(v)] if found), None)
    return None
MIN_PRIVATE_LINE = 12  # a shorter private line is too likely to match ordinary words elsewhere


def is_private_note(note: Dict[str, Any]) -> bool:
    """A note whose body never leaves the computer: tagged "private" in Joplin, or in a list (or notebook) named
    Medical, Health or Finance."""
    tags = [str(t).strip().lower() for t in note.get("tags") or []]
    if PRIVATE_TAG in tags:
        return True
    path = " > ".join(str(p) for p in (note.get("source_notebook") or "", note.get("title") or ""))
    return bool(PRIVATE_LIST_RE.search(path))


class Shield:
    """The filter, with the placeholder mapping it keeps for this process (the same value gets the same
    placeholder every time, so a conversation re-sent each turn stays consistent)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._placeholder_of: Dict[Tuple[str, str], str] = {}  # (kind, value) -> placeholder
        self._value_of: Dict[str, str] = {}                    # placeholder -> value
        self._kind_count: Dict[str, int] = {}
        self._private_lines: List[str] = []                    # private note body lines, longest first
        self._private_items: List[Tuple[str, str]] = []        # (long item line, short title), longest first

    # -- private notes --

    def set_private_notes(self, notes: Iterable[Dict[str, Any]], remembered: Optional[Dict[str, Any]] = None) -> int:
        """Remember which note bodies are private, from this read of the lists. What was private stays private:
        a line that leaves its private note (filed as done, edited, moved, or the note unmarked) is still in the
        conversation history, which is sent again each turn, so it's never forgotten (``remembered``: what earlier
        runs kept, private_state()). Returns how many private notes this read had."""
        from thirties_core.titles import split_title
        lines, items, count = set(), {}, 0
        with self._lock:
            lines.update(self._private_lines)
            items.update(dict(self._private_items))
        lines.update((remembered or {}).get("lines") or [])
        items.update({long: short for long, short in (remembered or {}).get("items") or []})
        for note in notes or []:
            if not is_private_note(note):
                continue
            count += 1
            for raw in str(note.get("body") or "").splitlines():
                box = _CHECKBOX.match(raw)
                if box:
                    text = box.group(1).strip()
                    short = split_title(text)[0]
                    if short and short != text:
                        items[text] = short
                    continue
                text = re.sub(r"^\s*(?:#+|[-*+]|\d+[.)])\s+", "", raw).strip()
                if len(text) >= MIN_PRIVATE_LINE:
                    lines.add(text)
        with self._lock:
            self._private_lines = sorted(lines, key=len, reverse=True)
            self._private_items = sorted(items.items(), key=lambda kv: len(kv[0]), reverse=True)
        return count

    def private_state(self) -> Dict[str, Any]:
        """What's remembered as private, to keep across restarts (on this device only)."""
        with self._lock:
            return {"lines": list(self._private_lines), "items": [list(kv) for kv in self._private_items]}

    # -- placeholders --

    def _placeholder(self, kind: str, value: str, label: Optional[str] = None) -> str:
        key = (kind, value)
        with self._lock:
            found = self._placeholder_of.get(key)
            if found:
                return found
            base = label or kind
            n = self._kind_count.get(base, 0) + 1
            self._kind_count[base] = n
            name = f"[{base}]" if n == 1 else f"[{base} {n}]"
            self._placeholder_of[key] = name
            self._value_of[name] = value
            return name

    def _sub(self, pattern: re.Pattern, text: str, kind: str, counts: Dict[str, int],
             label: Callable[[str], Optional[str]] = lambda v: None,
             keep: Callable[[re.Match], bool] = lambda m: True) -> str:
        def repl(m: re.Match) -> str:
            value = m.group("value") if "value" in pattern.groupindex else m.group(0)
            if not keep(m):
                return m.group(0)
            counts[kind] = counts.get(kind, 0) + 1
            ph = self._placeholder(kind, value, label(value))
            if "value" in pattern.groupindex:  # only the value goes; its label stays, so the sentence still reads
                return m.string[m.start():m.start("value")] + ph + m.string[m.end("value"):m.end()]
            return ph
        return pattern.sub(repl, text)

    # -- the filter --

    def redact(self, text: str, counts: Optional[Dict[str, int]] = None) -> str:
        """``text`` with everything sensitive replaced by placeholders. ``counts`` collects how many of each."""
        if not text:
            return text
        counts = {} if counts is None else counts
        with self._lock:
            private_items, private_lines = list(self._private_items), list(self._private_lines)
        for long, short in private_items:
            if long in text:
                counts["private item detail"] = counts.get("private item detail", 0) + text.count(long)
                text = text.replace(long, short)
        for line in private_lines:
            if line in text:
                counts["private note line"] = counts.get("private note line", 0) + text.count(line)
                text = text.replace(line, PRIVATE_BODY)
        text = self._sub(_PEM, text, "private key", counts)
        text = self._sub(_URL_CREDENTIALS, text, "password", counts)
        text = self._sub(_URL_SECRET_PARAM, text, "token", counts)
        text = self._sub(_KEY_SHAPES, text, "API key", counts)
        text = self._sub(_CODES, text, "backup codes", counts)
        for kind, pattern in _SECRET_PATTERNS:
            text = self._sub(pattern, text, kind, counts,
                             keep=lambda m: _looks_secret(m.group("value"), m.group("head"))
                             and not m.group("value").startswith("["))
        text = self._sub(_SSN_LABELLED, text, "SSN", counts)
        text = self._sub(_SSN, text, "SSN", counts)
        text = self._sub(_IBAN, text, "IBAN", counts, keep=lambda m: iban_ok(m.group(0)),
                         label=lambda v: f"IBAN ..{v.replace(' ', '')[-4:]}")
        text = self._sub(_CARD, text, "card", counts,
                         keep=lambda m: luhn_ok(re.sub(r"\D", "", m.group(0))),
                         label=lambda v: f"card ..{re.sub(r'[^0-9]', '', v)[-4:]}")
        text = self._sub(_ROUTING, text, "routing number", counts)
        text = self._sub(_ACCOUNT, text, "account number", counts,
                         label=lambda v: f"account ..{re.sub(r'[^0-9]', '', v)[-4:]}")
        text = self._sub(_GOV_ID, text, "ID number", counts, keep=lambda m: bool(re.search(r"\d", m.group("value"))))
        text = self._sub(_CONDITION_RE, text, "condition", counts)
        text = self._sub(_CONDITION_ACRONYM_RE, text, "condition", counts)
        text = self._sub(_MEDICATION_RE, text, "medication", counts)
        text = self._sub(_MED_SUFFIX_RE, text, "medication", counts)
        return text

    def restore(self, text: str) -> str:
        """Placeholders in a model's reply turned back into the real values (longest first, so "[card 2]" isn't
        read as "[card]"). A placeholder the model wrote a little differently is still recognised: any case,
        spaces added or dropped inside it, a missing closing bracket ("[Password]", "[card..1234]", "[pin 2")."""
        if not text or "[" not in text:
            return text
        with self._lock:
            pairs = sorted(self._value_of.items(), key=lambda kv: len(kv[0]), reverse=True)
        for ph, value in pairs:
            if ph in text:
                text = text.replace(ph, value)
        if "[" not in text:
            return text
        for ph, value in pairs:
            parts = [re.escape(p) for p in re.split(r"\s+|(?=\.\.)", ph[1:-1].strip()) if p]
            loose = re.compile(r"\[\s*" + r"\s*".join(parts) + r"\s*(?:\]|(?![\w.]))", re.IGNORECASE)
            text = loose.sub(lambda m: value, text)
        return text

    # -- messages --

    def _walk(self, value: Any, fn: Callable[[str], str]) -> Any:
        if isinstance(value, str):
            return fn(value)
        if isinstance(value, list):
            return [self._walk(v, fn) for v in value]
        if isinstance(value, dict):
            return {k: self._walk(v, fn) for k, v in value.items()}
        return value

    def redact_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Copies of the messages, with every text (contents and tool-call arguments) filtered. Logs counts."""
        counts: Dict[str, int] = {}
        out = []
        for msg in messages:
            m = dict(msg)
            if isinstance(m.get("content"), str):
                m["content"] = self.redact(m["content"], counts)
            if m.get("tool_calls"):
                m["tool_calls"] = [dict(c, arguments=self._walk(c.get("arguments", {}),
                                                                lambda s: self.redact(s, counts)))
                                   for c in m["tool_calls"]]
            out.append(m)
        log_counts(counts)
        return out

    def redact_prompt(self, prompt: str) -> str:
        counts: Dict[str, int] = {}
        text = self.redact(prompt, counts)
        log_counts(counts)
        return text

    def restore_reply(self, reply: Dict[str, Any]) -> Dict[str, Any]:
        """A model's reply with its placeholders turned back into the real values, in the text and the calls."""
        out = dict(reply)
        if isinstance(out.get("content"), str):
            out["content"] = self.restore(out["content"])
        if out.get("tool_calls"):
            out["tool_calls"] = [dict(c, arguments=self._walk(c.get("arguments", {}), self.restore))
                                 for c in out["tool_calls"]]
        return out


def log_counts(counts: Dict[str, int]) -> None:
    if counts:
        logger.info("Kept back from the cloud model: %s",
                    ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items())))


SHIELD = Shield()  # the process's filter: the engines use it, and reading the lists tells it what's private

__all__ = ["SHIELD", "Shield", "PRIVATE_BODY", "PRIVATE_TAG", "is_private_note", "luhn_ok", "iban_ok"]
