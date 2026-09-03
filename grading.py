"""Deterministic, offline grading for the per-lesson exercises.

No network / LLM at runtime: every gradable exercise carries an explicit list
of accepted answers (authored in exercises_data/<n>.json), and this module just
does normalized string comparison plus a few structural rules (sets, choices,
tables).

Kept separate from app.py's quiz checker so the quiz keeps its own behaviour.
"""
import re
import unicodedata

# Leading words that are treated as optional noise when `article_optional` is set
# (default for text / table graders). "att" is included so "att sova" also
# accepts "sova"; the en/ett lessons switch this off with `article_required`.
_ARTICLES = {"en", "ett", "att", "den", "det", "de", "the", "a", "an", "to"}


def normalize(s):
    """Lowercase, drop punctuation/symbols, collapse whitespace.

    Mirrors app.normalize_answer's technique (unicodedata category test) so the
    two stay conceptually in sync, but keeps its own copy. Letters - including
    a/ä/ö - are preserved; diacritics are NOT folded (learners should type them).
    """
    s = (s or "").lower().strip()
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] not in ("P", "S"))
    return re.sub(r"\s+", " ", s).strip()


def _strip_articles(norm):
    """Drop leading article-ish words from an already-normalized string."""
    parts = norm.split()
    while len(parts) > 1 and parts[0] in _ARTICLES:
        parts = parts[1:]
    return " ".join(parts)


def _variants(value, article_optional):
    """Normalized form(s) a single value may match as.

    Alternatives are always authored as separate list entries (never "a/b" in
    one string), so this deliberately does NOT split on '/' or ',' - that would
    wrongly let a fragment of a full-sentence answer match on its own.
    """
    out = {normalize(value)}
    if article_optional:
        out |= {_strip_articles(v) for v in list(out)}
    return {v for v in out if v}


def _blank_ok(accepted, given, article_optional=True):
    """True if `given` matches any accepted answer for one blank.

    `accepted` is a list of strings. Matching is symmetric on articles: the
    user's answer is also article-stripped before comparison.
    """
    given_forms = _variants(given, article_optional)
    for acc in accepted:
        if _variants(acc, article_optional) & given_forms:
            return True
    return False


def grade_text(spec, answer):
    """spec: {"blanks": [{"accept": [...], "article_optional": bool?}, ...]}
    answer: list of strings (one per blank) or a single string for 1 blank.
    """
    blanks = spec.get("blanks", [])
    if isinstance(answer, str):
        answer = [answer]
    answer = list(answer or [])
    if len(answer) != len(blanks):
        # tolerate a missing trailing blank as empty, but never a mismatch that
        # would misalign answers to blanks
        if len(answer) < len(blanks):
            answer += [""] * (len(blanks) - len(answer))
        else:
            return False
    for blank, given in zip(blanks, answer):
        if not given or not given.strip():
            return False
        art_opt = blank.get("article_optional", True)
        if not _blank_ok(blank.get("accept", []), given, art_opt):
            return False
    return True


def grade_set(spec, answer):
    """spec: {"n_required": int, "distinct": bool, "accept_pool": [...],
             "article_optional": bool?}
    answer: list of strings. Order does not matter. Each entry must be in the
    pool; `distinct` (default True) forbids using the same pool answer twice.
    """
    pool = spec.get("accept_pool", [])
    n_required = spec.get("n_required", len(pool))
    distinct = spec.get("distinct", True)
    art_opt = spec.get("article_optional", True)

    given = [a for a in (answer or []) if a and a.strip()]
    if len(given) < n_required:
        return False

    # canonical pool key for each pool entry, so "distinct" compares by meaning
    pool_forms = [(i, _variants(p, art_opt)) for i, p in enumerate(pool)]
    used = set()
    matched = 0
    for g in given:
        gforms = _variants(g, art_opt)
        hit = None
        for i, pforms in pool_forms:
            if i in used and distinct:
                continue
            if pforms & gforms:
                hit = i
                break
        if hit is None:
            return False  # an answer that isn't in the pool -> whole item wrong
        used.add(hit)
        matched += 1
    return matched >= n_required


def grade_translate(spec, answer):
    """spec: {"accept": [...], "article_optional": bool?}
    answer: a single string (or 1-element list). Matches any accepted answer.
    """
    if isinstance(answer, list):
        answer = answer[0] if answer else ""
    if not answer or not answer.strip():
        return False
    return _blank_ok(spec.get("accept", []), answer, spec.get("article_optional", True))


# a fill-in-the-blanks item grades exactly like the old multi-blank "text": one
# accepted list per placeholder, answers aligned to {0}, {1}, ... in order
grade_fill = grade_text


def grade_choice(spec, answer):
    """spec: {"options": [...], "correct": [...]}  (correct may hold >1 value,
    e.g. when either the letter or the word is acceptable / multiple right).
    answer: a single string (the chosen option, or its letter).
    """
    if isinstance(answer, list):
        answer = answer[0] if answer else ""
    if not answer or not answer.strip():
        return False
    correct = spec.get("correct", [])
    aforms = _variants(answer, False)
    for c in correct:
        if _variants(c, False) & aforms:
            return True
    return False


def grade_table(spec, answer):
    """spec: {"rows": [{"given": str, "given_side": "sv"|"en", "accept": [...]},
                       ...]}  - only rows with a non-empty `accept` are blanks
                              the user fills; `given` rows are shown read-only.
    answer: list aligned to the blank rows (in order), or a dict {row_index: val}.
    """
    rows = spec.get("rows", [])
    blank_rows = [(i, r) for i, r in enumerate(rows) if r.get("accept")]

    if isinstance(answer, dict):
        get = lambda i: answer.get(str(i), answer.get(i, ""))
        seq = [get(i) for i, _ in blank_rows]
    else:
        seq = list(answer or [])
        if len(seq) < len(blank_rows):
            seq += [""] * (len(blank_rows) - len(seq))

    for (_, row), given in zip(blank_rows, seq):
        if not given or not given.strip():
            return False
        if not _blank_ok(row.get("accept", []), given, article_optional=True):
            return False
    return True


_GRADERS = {
    "translate": grade_translate,
    "fill": grade_fill,
    "text": grade_text,          # legacy alias for pre-split data
    "set": grade_set,
    "choice": grade_choice,
    "table": grade_table,
}


def grade_item(grader, spec, answer):
    """Return True iff `answer` fully satisfies the exercise item.

    Unknown grader -> False (never silently 'pass').
    """
    fn = _GRADERS.get(grader)
    if fn is None:
        return False
    try:
        return bool(fn(spec or {}, answer))
    except (KeyError, TypeError, ValueError):
        return False
