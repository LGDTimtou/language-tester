"""Lenient matching for spoken quiz answers.

Browser speech recognition (webkitSpeechRecognition, Chrome/Edge - free, no key)
sends us the transcript plus a few alternative guesses. Isolated Swedish words
are its weakest case, so we don't demand an exact hit: we already know the one
answer we're listening for, so we just ask "does anything it heard sound close
enough to that?".

Only used when the answer arrives by voice; typed answers stay strict.
"""
import re
import unicodedata
from difflib import SequenceMatcher

_ACCEPT_THRESHOLD = 0.82   # phonetic-key similarity needed to accept


def _normalize(s):
    s = (s or "").lower().strip()
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] not in ("P", "S"))
    return re.sub(r"\s+", " ", s).strip()


# Fold Swedish (and the recogniser's English-ish spelling of it) down to roughly
# how it sounds, so the classic confusions collapse together:
#   sju / skjuta / stjärna / sked / "shoo"  -> the "sje" sound
#   tjock / kika / kex / "chock"            -> the "tje" sound
#   gilla / hjul / djur / ljud / "yood"     -> a "j" (y) sound
#   å~o, ä~e, ö~o, y~i, u~o ; ck~k ; w~v ; z~s ; doubled letters ~ single ;
#   a word-initial h the recogniser invented ("har" for "år") is dropped
_SJE, _TJE = "\x01", "\x02"

# consonant rules run while the string still has å/ä/ö/y/u
_RULES = [
    (re.compile(r"ck"), "k"),
    (re.compile(r"sch|stj|skj|ssj|sj|sh|zh"), _SJE),
    (re.compile(r"sk(?=[eiyäöéè])"), _SJE),
    (re.compile(r"tch|tj|kj|ch"), _TJE),
    (re.compile(r"k(?=[eiyäöéè])"), _TJE),
    (re.compile(r"gj|dj|lj|hj"), "j"),
    (re.compile(r"g(?=[eiyäöéè])"), "j"),
    (re.compile(r"hv"), "v"),
    (re.compile(r"ph"), "f"),
    (re.compile(r"x"), "ks"),
    (re.compile(r"c(?=[eiy])"), "s"),
    (re.compile(r"[cq]"), "k"),
    (re.compile(r"w"), "v"),
    (re.compile(r"z"), "s"),
    (re.compile(r"h(?=[bcdfgjklmnpqrstvxz])"), ""),     # near-silent h before a consonant
]
_VOWELS = str.maketrans({"å": "o", "ä": "e", "ö": "o", "y": "i", "u": "o",
                         "é": "e", "è": "e", "à": "a", "ü": "i"})


def phonetic_key(s):
    s = _normalize(s)
    for rx, rep in _RULES:
        s = rx.sub(rep, s)
    s = s.translate(_VOWELS)
    s = s.replace(_SJE, "x").replace(_TJE, "c").replace("j", "i")
    s = re.sub(r"(.)\1+", r"\1", s)      # collapse doubled letters
    return s.strip()


def _ratio(a, b):
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _lev(a, b):
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _one_ok(expected, heard):
    if not heard.strip():
        return 0.0
    if _normalize(heard) == _normalize(expected):
        return 1.0
    ne, nh = _normalize(expected), _normalize(heard)
    pe, ph = phonetic_key(expected), phonetic_key(heard)
    scores = [_ratio(ne, nh), _ratio(pe, ph)]

    # short words: one substituted sound is still "close", but only when the
    # words start the same (so "katt" != "hat", "att" != "at"... stays strict)
    if (3 <= max(len(pe), len(ph)) <= 5 and _lev(pe, ph) <= 1
            and pe[:1] == ph[:1]):
        scores.append(0.9)

    # phrase answers: every expected word should have a close match somewhere
    ew, hw = pe.split(), ph.split()
    if len(ew) > 1 and hw:
        scores.append(sum(max(_ratio(e, h) for h in hw) for e in ew) / len(ew))
    return max(scores)


def close_enough(expected, transcripts, threshold=_ACCEPT_THRESHOLD):
    """expected: the single correct answer string.
    transcripts: list of what the recogniser heard (top guess + alternatives).
    Returns (accepted: bool, best_score: float, best_transcript: str).
    """
    best_score, best_t = 0.0, ""
    for t in transcripts or []:
        sc = _one_ok(expected, t)
        if sc > best_score:
            best_score, best_t = sc, t
    return best_score >= threshold, round(best_score, 3), best_t
