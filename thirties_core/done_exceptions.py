"""What a done leaves out: the items the user's words name as not part of it, or as still open.

People say a done for a whole group and name what isn't part of it: "Bisque round wrapped up. The glaze test, the
kiln log and the batts order are still open", "apart from the batts order, the bisque is done", "wedged and swept,
but not the glaze test". Which items the done is for is the model's reading (said_for_it.py). What Python keeps is
the structure of the sentence: a name in such a part is never what the done is about, and a tick of an item named
there stops.

- **An exception part**: in a told sentence that says a done, the names after "except (for)", "apart from", "aside
  from", "other than", "with the exception of", "excluding", "minus", "save for", "besides", "but not", "not" or
  "but", up to the first word that isn't part of a name ("the rest but the batts order got finished on the sheet"
  leaves out the batts order). After "but", "not" and "besides" only names that end the clause count ("but the
  glaze test is still going" is a clause of its own, read as one). A clause about them that follows ("which is
  still firing") belongs to the part. After "except that", or "except" with no name next, the part is a clause
  ("except I still have to sweep the wheel bay"): the items it names, when it says no done of its own. A done that
  is itself denied ("did none save for the kiln log") leaves nothing out: what follows is the done. "Besides" with
  "also", "too" or "as well" adds; it leaves nothing out.
- **Said to be still open**: names said to be still open, still waiting, left, to do, pending, outstanding or not
  done, or put off ("the tallow can wait", "the kiln log later", "the batts order tomorrow"), on their own or as a
  list sharing those words ("the glaze test, the kiln log and the batts order are still open"). A name counts
  only where it starts its stretch: "wedged the stoneware, the glaze test is still open" leaves out only the glaze
  test. Words about the app's record ("still shows open", "still open on the list") aren't about the work, and
  neither is a still-open said beside a done that names the item or points back to it ("the glaze test is still
  open, I finished it at 2:10").
- **A reply to the app's question** about which items are done carries the done itself: "both, except the
  bailers" leaves out the bailers (``answered``).
- **A skip** leaves out the same way: "skip the bisque round except the wedging" keeps the wedging (``kind="skip"``).

Questions say nothing here. Whether a name is the item's is read as the other not-done checks read it
(write_guard._about). ``left_out`` says why a tick (or a skip) of an item named so stops; ``without_exceptions`` is
the message with those parts taken out, which is how the other checks read what the done is about.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

# a word, or a mark that separates the parts of a sentence
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*|[,;:&]|(?<=\s)[-–—](?=\s)")
# what joins the entries of a list
_LIST_JOIN = {",", "&", "and", "or", "nor", "plus"}
# small words that go before a name ("the", "my")
_BEFORE_A_NAME = {"the", "a", "an", "my", "our", "your", "their", "that", "this", "those", "these",
                  "for", "of"}
# words that never name a thing, besides the done checks' own (done_fit._breaks)
_NOT_A_NAME = {"which", "who", "whom", "whose", "where", "though", "tho", "although", "because", "cause", "cos",
               "unless", "whereas", "except", "apart", "aside", "besides", "excluding", "minus", "save", "nor",
               "and", "plus", "also", "too", "well"}
# a clause about the names just said ("which is still firing", "both still drying")
_ABOUT_THEM_RE = re.compile(r"\s*,?\s*(?:which|that|who|whose|they|they'?re|theyre|those|these|both|each|"
                            r"all of which|neither|none of which|it|it'?s|its)\b[^,;:.!?\n]*", re.IGNORECASE)

# --- an exception part ---
# leaves out what follows it: names, or a clause
_STRONG = (r"except(?:\s+for)?(?P<that>\s+that)?|apart\s+from|aside\s+from|other\s+than|with\s+the\s+exception\s+of|"
           r"excluding|minus|save\s+for")
# leaves out only names that end the clause ("bisque round wrapped but the glaze test", "not the kiln log")
_WEAK = r"but\s+not|but|not|besides"
_MARK_RE = re.compile(rf"\b(?:(?P<strong>{_STRONG})|(?P<weak>{_WEAK}))\b", re.IGNORECASE)
# a bulk word just before "but" ("the rest but the kiln log got finished on the sheet"): the names after it are
# left out, whatever follows them
_BULK_BUT_RE = re.compile(r"\b(?:everything|everythings|all|the\s+(?:whole\s+)?lot|the\s+rest|every\s+(?:one|item|"
                          r"thing|task|job))\s+(?P<but>but)\b", re.IGNORECASE)
# "besides" that adds rather than leaves out
_ADDS_RE = re.compile(r"\b(?:also|too|as well)\b", re.IGNORECASE)
# a done that is itself denied: then its exception is what's done
_DENIED_RE = re.compile(r"\b(?:nothing|none|neither|not|never|no)\b|n['’]t\b", re.IGNORECASE)
# where a clause ends
_CLAUSE_END_RE = re.compile(r"[,;:.!?\n]|\s[-–—]\s|\b(?:and|but|though|although|while|whereas|so|then)\b",
                            re.IGNORECASE)
_ENDS_RE = re.compile(r"\s*(?:$|[,;:.!?\n]|[-–—]\s|(?:and|but|though|tho|although|while|whereas|so|then|yet|"
                      r"either|anyway|for now)\b)", re.IGNORECASE)
# --- end an exception part ---

# --- said to be still open ---
_OPEN_RE = re.compile(
    r"\bstill\s+(?:open|waiting|left|to\s+do|to\s+go|pending|outstanding|undone|unfinished)\b|"
    r"\b(?:left|yet)\s+to\s+(?:do|go|be\s+done)\b|\b(?:is|are|'s|'re)\s+left\b|\bto\s+go\b|"
    r"\b(?:pending|outstanding|undone|unfinished)\b|"
    # put off: "the tallow can wait", "the kiln log later", "the batts order tomorrow"
    r"\b(?:can|will|could|has\s+to|have\s+to|gets\s+to)\s+wait\b|\b(?:for\s+)?later\b|\btomorrow\b|"
    r"\banother\s+day\b|\bnext\s+time\b|"
    r"(?:\b(?:isn|aren|wasn|weren|haven|hasn)['’]?t|\bnot)\s+(?:yet\s+|really\s+|quite\s+|even\s+)?(?:been\s+)?"
    r"(?:done|finished|complete|completed|started|happening|happened)\b|\bnot\s+yet\b", re.IGNORECASE)
# the words between a list and what's said of it ("are", "'re", "both")
_LINK = {"is", "are", "was", "were", "s", "re", "remain", "remains", "stay", "stays", "still", "both", "all", "each",
         "currently", "also", "too"}
# words about the app's record, not the work ("still shows open", "still open on the list")
_RECORD_RE = re.compile(r"\b(?:shows?|showing|shown|says?|saying|reads?|listed|marked|ticked|unticked|checked|"
                        r"(?:on|in)\s+(?:the|my|your)\s+(?:list|plan|app|screen|schedule|agenda|note)|app|screen)\b",
                        re.IGNORECASE)
# a pointer back to something just named
_POINTER_RE = re.compile(r"\b(?:it|that|this|them|those|these|both)\b", re.IGNORECASE)
# where the parts of a sentence that say different things meet
_PART_SEP = {";", ":", "-", "–", "—", "though", "tho", "although", "but", "while", "whereas", "yet", "so", "then",
             "because", "cause", "since", "except", "apart", "aside", "besides"}
# --- end said to be still open ---


def _plain(word: str) -> str:
    return re.sub(r"['’]", "", (word or "").lower())


def _naming(word: str) -> bool:
    """The word can be part of an item's name ("glaze", "batts"): not a small word, a done word or a past."""
    from thirties_core.done_fit import _breaks
    w = _plain(word)
    return bool(w) and w not in _NOT_A_NAME and w not in _BEFORE_A_NAME and not _breaks(w)


def _tokens(text: str, start: int = 0, end: Optional[int] = None) -> List[Tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group(0))
            for m in _TOKEN_RE.finditer(text, start, len(text) if end is None else end)]


def _list_at(text: str, start: int) -> Tuple[int, List[Tuple[int, int]]]:
    """The names that open text[start:], one after another ("the glaze test, the kiln log and the batts order"):
    (the end of that run, each name's (start, end)). (start, []) when no name opens it."""
    tokens = _tokens(text, start)
    names: List[Tuple[int, int]] = []
    end, i = start, 0
    while i < len(tokens):
        j = i
        while j < len(tokens) and _plain(tokens[j][2]) in _BEFORE_A_NAME:
            j += 1
        k = j
        while k < len(tokens) and _naming(tokens[k][2]):
            k += 1
        if k == j:
            break
        names.append((tokens[i][0], tokens[k - 1][1]))
        end = tokens[k - 1][1]
        if k < len(tokens) and _plain(tokens[k][2]) in _LIST_JOIN:
            k += 1
            if k < len(tokens) and _plain(tokens[k][2]) in ("and", "or", "nor"):  # ", and"
                k += 1
            i = k
            continue
        break
    return end, names


def _sentences(message: Optional[str]) -> List[Tuple[int, str]]:
    """The told sentences of the message, with where each starts: a question leaves nothing out."""
    from thirties_core.said_for_it import _SENTENCE_RE, is_asking
    return [(m.start(), m.group(0)) for m in _SENTENCE_RE.finditer(message or "")
            if m.group(0).strip() and not is_asking(m.group(0))]


def _says(kind: str, text: str) -> bool:
    """The text says a done in a clause that doesn't deny it, or, for a skip, a skip."""
    if kind == "skip":
        from thirties_core.change_asks import _removal_words
        return bool(_removal_words().search(text))
    from thirties_core.plain_done import says_it_happened
    from thirties_core.write_guard import _CLAUSE_RE
    from thirties_core.started import denies_done
    return any(says_it_happened(c) and not denies_done(c) for c in _CLAUSE_RE.split(text) if c and c.strip())


def _exception_spans(sentence: str, kind: str,
                     answered: bool = False) -> List[Tuple[int, int, int, List[Tuple[int, int]]]]:
    """Each exception part of a told sentence: (start, end, where what it leaves out starts, the names it leaves
    out; [] when it's a clause). ``answered``: the sentence replies to the app's question about a done, which
    carries the done ("both, except the bailers"), unless the reply denies it."""
    out: List[Tuple[int, int, int, List[Tuple[int, int]]]] = []
    marks = [(m.start("but"), m.end(), "bulk", False) for m in _BULK_BUT_RE.finditer(sentence)]
    marks += [(m.start(), m.end(), "strong" if m.group("strong") else "weak", bool(m.group("that")))
              for m in _MARK_RE.finditer(sentence)]
    for a, b, how, that in sorted(marks):
        if any(x <= a < y for x, y, _, _ in out):
            continue
        if sentence[a:b].lower().startswith("besides") and _ADDS_RE.search(sentence):
            continue
        end, names = (b, []) if that else _list_at(sentence, b)
        if names and how == "weak" and not (_ENDS_RE.match(sentence, end) or _ABOUT_THEM_RE.match(sentence, end)):
            continue  # "but the glaze test is still going": a clause of its own, read as one (write_guard.py)
        if not names:
            if how != "strong":
                continue
            m = _CLAUSE_END_RE.search(sentence, b)  # "except I still have to sweep the wheel bay"
            end = m.start() if m else len(sentence)
            if not sentence[b:end].strip():
                continue
        else:
            about = _ABOUT_THEM_RE.match(sentence, end)
            if about and about.group(0).strip(" ,"):
                end = about.end()
        if answered:
            if _DENIED_RE.search(sentence[:a] + " " + sentence[end:]):
                continue  # "none of them, except the bailers": what it leaves out is the done
        elif not (_says(kind, sentence[:a]) or _says(kind, sentence[end:])):
            continue  # no done beside it, or one it denies ("did none save for the kiln log")
        out.append((a, end, b, names))
    return out


def _open_spans(sentence: str) -> List[Tuple[int, int, List[Tuple[int, int]]]]:
    """Each stretch of a told sentence that says names are still open: (start, end, the names)."""
    out = []
    for m in _OPEN_RE.finditer(sentence):
        tokens = _tokens(sentence, 0, m.start())
        i = len(tokens)
        while i > 0 and _plain(tokens[i - 1][2]) in _LINK:  # "are", "is", "both"
            i -= 1
        names: List[Tuple[int, int]] = []
        start = None
        while i > 0:
            k = i
            while k > 0 and _naming(tokens[k - 1][2]):
                k -= 1
            if k == i:
                break
            j = k
            while j > 0 and _plain(tokens[j - 1][2]) in _BEFORE_A_NAME:
                j -= 1
            before = _plain(tokens[j - 1][2]) if j > 0 else ""
            if before in _LIST_JOIN:
                names.insert(0, (tokens[k][0], tokens[i - 1][1]))
                start = tokens[j][0] if j < len(tokens) else tokens[k][0]
                i = j - 1
                if i > 0 and _plain(tokens[i - 1][2]) == "," and before in ("and", "or", "nor"):
                    i -= 1  # ", and"
                continue
            if j == 0 or before in _PART_SEP:
                names.insert(0, (tokens[k][0], tokens[i - 1][1]))
                start = tokens[j][0]
            break  # a word before it ("wedged the stoneware, the glaze test is still open"): that's its object
        if names and start is not None:
            out.append((start, m.end(), names))
    return out


def _record_complaint(message: str, sentence: str, start: int, end: int, title: str, others: List[str]) -> bool:
    """The still-open is about the app's record: record words beside it, or a done said elsewhere in the message
    that names the item or points back to it ("the glaze test is still open, I finished it")."""
    from thirties_core.write_guard import _CLAUSE_RE, _about
    if _RECORD_RE.search(sentence[max(0, start - 40):end + 40]):
        return True
    text = message.replace(sentence, sentence[:start] + " ; " + sentence[end:])
    return any(_says("done", c) and (_about(c, title, others) or _POINTER_RE.search(c))
               for c in _CLAUSE_RE.split(text) if c and c.strip())


def left_out(message: Optional[str], title: str, others: Iterable[str] = (), kind: str = "done",
             answered: bool = False) -> str:
    """Why the user's words leave this item out of their done (or skip): it's named in an exception part of it, or
    said to be still open. A name another item shares just as much ("the dusting" beside Afternoon dusting and
    Evening dusting) names both. ``answered``: the words reply to the app's question about a done
    (said_for_it.py). "" when they don't."""
    if not message:
        return ""
    from thirties_core.write_guard import _about as _named_here, _fits_level
    others = [o for o in others if o and o != title]

    def _about(text: str, item: str, rest: List[str]) -> bool:
        return _named_here(text, item, rest) or _fits_level(text, item, rest)
    for _, sentence in _sentences(message):
        for a, end, b, names in _exception_spans(sentence, kind, answered):
            if names:
                hit = any(_about(sentence[x:y], title, others) for x, y in names)
            else:  # a clause: the items it names without a done of their own
                hit = _about(sentence[b:end], title, others) and not _says(kind, sentence[b:end])
            if hit:
                return _why(title, kind)
        if kind == "done":
            for a, b, names in _open_spans(sentence):
                if any(_about(sentence[x:y], title, others) for x, y in names) \
                        and not _record_complaint(message, sentence, a, b, title, others):
                    return _why(title, kind)
    return ""


def without_exceptions(message: Optional[str], kind: str = "done") -> Optional[str]:
    """The message with its exception parts and still-open stretches taken out: what the done is about is read
    from the rest."""
    if not message:
        return message
    cuts: List[Tuple[int, int]] = []
    for start, sentence in _sentences(message):
        cuts += [(start + a, start + end) for a, end, _, _ in _exception_spans(sentence, kind)]
        if kind == "done":
            cuts += [(start + a, start + b) for a, b, _ in _open_spans(sentence)]
    if not cuts:
        return message
    out, at = [], 0
    for a, b in sorted(cuts):
        a = max(a, at)
        if a < b:
            out.append(message[at:a])
            at = b
    out.append(message[at:])
    return " ".join(p.strip() for p in out if p.strip())


def _why(title: str, kind: str) -> str:
    if kind == "skip":
        return (f"The user's words leave {title} out of what they're skipping, so it isn't marked skipped. Leave it "
                "as it is.")
    return (f"The user's words name {title} as not done (left out of what they said is done, or still open), so it "
            "isn't marked done. Leave it open.")


__all__ = ["left_out", "without_exceptions"]
