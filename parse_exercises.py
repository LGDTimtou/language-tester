"""Exercise data for the trainer: PDF -> exercises_data/<n>.json -> vocab.db.

Two ways an <n>.json comes to exist:

  * hand-authored  (marked  "source": "hand"  - never overwritten by the parser)
  * auto-parsed    from the lesson's Exercises PDF by autoparse_lesson() below

`ensure_exercises()` runs on every app start (via run.sh / app.py): it
auto-parses any lesson that has an Exercises PDF but no JSON yet, then loads any
lesson whose JSON isn't in the DB. So dropping in a new lesson folder + running
parse_vocab.py is enough - its exercises appear on the next start.

CLI:
  python3 parse_exercises.py            ensure: parse-missing + load-missing (fast)
  python3 parse_exercises.py --load     full idempotent re-load of every JSON
                                        (use after hand-editing a file)
  python3 parse_exercises.py --regen N  re-autoparse lesson N (refuses "source":"hand")
  python3 parse_exercises.py --status   coverage table

Re-loading never destroys progress: items are keyed by a hash of their prompt,
and rows dropped from a file are only deleted when they carry no solved/done
state (same rule as parse_vocab.py).
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sqlite3
import sys

from grading import normalize

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "exercises_data")
DOWNLOADS_DIR = os.environ.get(
    "LSWK_DOWNLOADS_DIR", os.path.join(os.path.dirname(BASE_DIR), "PatreonDownloads")
)
DB_PATH = os.environ.get("LSWK_DB", os.path.join(BASE_DIR, "vocab.db"))

VALID_GRADERS = {"text", "set", "choice", "table"}


# ---------- schema ----------

def init_exercises_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS exercise_items (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        lesson_id        INTEGER NOT NULL REFERENCES lessons(id),
        item_key         TEXT NOT NULL,
        order_index      INTEGER NOT NULL,
        section          TEXT,
        kind             TEXT NOT NULL,
        grader           TEXT,
        prompt           TEXT NOT NULL,
        spec_json        TEXT NOT NULL DEFAULT '{}',
        reference_json   TEXT,
        solved           INTEGER NOT NULL DEFAULT 0,
        self_done        INTEGER NOT NULL DEFAULT 0,
        last_answer_json TEXT,
        UNIQUE(lesson_id, item_key)
    );
    """)
    conn.commit()


def item_key(prompt, section):
    raw = normalize(section) + "||" + normalize(prompt)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------- authored JSON -> DB ----------

def _build_spec(item):
    grader = item.get("grader")
    if grader == "text":
        blanks = item.get("blanks") or [{"accept": item.get("accept", [])}]
        return {"blanks": [
            {"accept": list(b.get("accept", [])),
             **({"article_optional": b["article_optional"]} if "article_optional" in b else {})}
            for b in blanks
        ]}
    if grader == "set":
        return {
            "n_required": item.get("n_required", len(item.get("accept_pool", []))),
            "distinct": item.get("distinct", True),
            "accept_pool": list(item.get("accept_pool", [])),
            **({"article_optional": item["article_optional"]} if "article_optional" in item else {}),
        }
    if grader == "choice":
        return {"options": list(item.get("options", [])), "correct": list(item.get("correct", []))}
    if grader == "table":
        return {"rows": [
            {"given": r.get("given", ""), "given_side": r.get("given_side", "sv"),
             "accept": list(r.get("accept", []))}
            for r in item.get("rows", [])
        ]}
    return {}


def _validate(lesson_no, items):
    problems = []
    for i, it in enumerate(items):
        where = f"lesson {lesson_no} item {i} ({it.get('prompt','')[:40]!r})"
        if it.get("kind") not in ("graded", "open"):
            problems.append(f"{where}: kind must be 'graded' or 'open'")
            continue
        if not it.get("prompt"):
            problems.append(f"{where}: missing prompt")
        if it["kind"] == "graded":
            g = it.get("grader")
            if g not in VALID_GRADERS:
                problems.append(f"{where}: grader must be one of {sorted(VALID_GRADERS)}")
                continue
            spec = _build_spec(it)
            if g == "text" and (not spec["blanks"] or any(not b["accept"] for b in spec["blanks"])):
                problems.append(f"{where}: text grader needs a non-empty accept list per blank")
            if g == "set" and (not spec["accept_pool"] or spec["n_required"] < 1):
                problems.append(f"{where}: set grader needs accept_pool and n_required >= 1")
            if g == "choice" and (not spec["options"] or not spec["correct"]):
                problems.append(f"{where}: choice grader needs options and correct")
            if g == "table" and not any(r["accept"] for r in spec["rows"]):
                problems.append(f"{where}: table grader needs at least one fillable row")
    return problems


def _lesson_id(conn, lesson_no):
    row = conn.execute("SELECT id FROM lessons WHERE number = ?", (lesson_no,)).fetchone()
    return row[0] if row else None


def load_file(conn, path):
    data = json.load(open(path, encoding="utf-8"))
    lesson_no = data["lesson"]
    items = data.get("items", [])

    problems = _validate(lesson_no, items)
    if problems:
        raise ValueError("  " + "\n  ".join(problems))

    lesson_id = _lesson_id(conn, lesson_no)
    if lesson_id is None:
        raise ValueError(f"lesson {lesson_no} not in DB (run parse_vocab.py first)")

    fresh_keys = set()
    n_graded = n_open = new_rows = 0
    for order_index, it in enumerate(items):
        kind = it["kind"]
        grader = it.get("grader") if kind == "graded" else None
        spec = _build_spec(it) if kind == "graded" else {}
        reference = it.get("reference")
        key = item_key(it["prompt"], it.get("section", ""))
        fresh_keys.add(key)
        n_graded += kind == "graded"
        n_open += kind == "open"

        existing = conn.execute(
            "SELECT id FROM exercise_items WHERE lesson_id = ? AND item_key = ?",
            (lesson_id, key),
        ).fetchone()
        ref_json = json.dumps(reference, ensure_ascii=False) if reference is not None else None
        if existing:
            conn.execute(
                "UPDATE exercise_items SET order_index = ?, section = ?, kind = ?, grader = ?, "
                "prompt = ?, spec_json = ?, reference_json = ? WHERE id = ?",
                (order_index, it.get("section"), kind, grader, it["prompt"],
                 json.dumps(spec, ensure_ascii=False), ref_json, existing[0]),
            )
        else:
            new_rows += 1
            conn.execute(
                "INSERT INTO exercise_items (lesson_id, item_key, order_index, section, kind, "
                "grader, prompt, spec_json, reference_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (lesson_id, key, order_index, it.get("section"), kind, grader, it["prompt"],
                 json.dumps(spec, ensure_ascii=False), ref_json),
            )

    stale = conn.execute(
        "SELECT id, item_key, solved, self_done FROM exercise_items WHERE lesson_id = ?",
        (lesson_id,),
    ).fetchall()
    kept = 0
    for row_id, key, solved, self_done in stale:
        if key in fresh_keys:
            continue
        if solved or self_done:
            kept += 1
            continue
        conn.execute("DELETE FROM exercise_items WHERE id = ?", (row_id,))

    conn.commit()
    src = data.get("source", "auto")
    tail = f"  ({kept} kept w/ progress)" if kept else ""
    return f"{lesson_no:>3}. [{src:<4}] graded={n_graded:<3} open={n_open:<3} new={new_rows}{tail}"


# ---------- PDF discovery ----------

def _lesson_folders():
    """{number: folder_path} for every 'NN. ...' dir under PatreonDownloads."""
    out = {}
    if not os.path.isdir(DOWNLOADS_DIR):
        return out
    for f in sorted(os.listdir(DOWNLOADS_DIR)):
        m = re.match(r"^(\d+)\.", f)
        p = os.path.join(DOWNLOADS_DIR, f)
        if m and os.path.isdir(p):
            out.setdefault(int(m.group(1)), p)
    return out


def _exercises_pdf(folder_path):
    cands = sorted(f for f in os.listdir(folder_path) if re.search(r"exercise", f, re.IGNORECASE))
    return os.path.join(folder_path, cands[0]) if cands else None


def _pdf_text(path):
    return subprocess.run(
        ["pdftotext", "-layout", path, "-"], capture_output=True, text=True
    ).stdout


# ---------- PDF -> structured items (best effort) ----------

_ANSWER_MARKER_RE = re.compile(
    r"correct answers?\s*(below|&|and)|answers?\s+below|answers?\s*&\s*examples", re.IGNORECASE
)
_PAGE_NOISE_RE = re.compile(r"^\s*\d+\.\s*(exercises|vocabulary|cheat sheet)\s*\d*\s*$", re.IGNORECASE)
_NUM_PREFIX_RE = re.compile(r"^\s*\d+[.)]\s+")
_BLANK_RE = re.compile(r"_{2,}")
_PAIR_RE = re.compile(r"^(.{1,80}?)\s*[-–—]{1,2}|→\s*")
_ARROW_SPLIT_RE = re.compile(r"\s*(?:–|—|-{1,2}|→)\s*")
_TICK_RE = re.compile(r"[✅✔️✓]|\bcorrect\b", re.IGNORECASE)
_MC_OPT_RE = re.compile(r"^\s*([a-eA-E])[).]\s+(.*)$")
_HINT_RE = re.compile(r"\s*\(([^)]*)\)\s*$")

_SET_HEADERS = ("write three", "write the days", "write the months", "write down the",
                "list ", "what two", "what three", "what are the two", "what are the three",
                "name three", "name the", "give three", "give me three")
_OPEN_HEADERS = ("what is the difference", "answer the following", "answer these",
                 "write your", "write a short", "short answers", "journaling",
                 "learn ", "reflect", "discuss", "in your own words", "write about")
_CHOICE_HEADERS = ("circle the", "choose the correct", "pick the correct", "underline the",
                   "select the correct")
_TABLE_HEADERS = ("complete the vocabulary table", "complete the table", "fill in the table",
                  "vocabulary table")


def _clean(text):
    text = text.replace("\x0c", "\n")
    lines = []
    for raw in text.split("\n"):
        s = raw.rstrip()
        if _PAGE_NOISE_RE.match(s.strip()):
            continue
        lines.append(s)
    return lines


def _split_solutions(lines):
    idx = next((i for i, l in enumerate(lines) if _ANSWER_MARKER_RE.search(l)), None)
    if idx is None:
        return lines, []
    return lines[:idx], lines[idx + 1:]


def _sections(lines):
    """[(header, [body lines]), ...]. A header is a shortish line ending ':' or
    matching a known kind, with no blank/underscore of its own."""
    known = _SET_HEADERS + _OPEN_HEADERS + _CHOICE_HEADERS + _TABLE_HEADERS + (
        "fill in the blank", "translate the english", "translate the swedish",
        "how would you say", "how do you say", "correct the mistake", "build the sentence",
        "put the words", "match ", "complete the sentence", "rewrite ", "conjugate ",
        "translate the sentence", "translate the word", "translate these",
    )
    out = []
    cur_head, cur_body = "Exercises", []
    for l in lines:
        s = l.strip()
        if not s:
            cur_body.append("")
            continue
        low = s.lower().rstrip(":").strip()
        is_head = (s.endswith(":") and len(s) <= 90 and not _BLANK_RE.search(s)) \
            or any(low.startswith(k) or k in low for k in known)
        if is_head and len(s) <= 100 and "_" not in s:
            if cur_body:
                out.append((cur_head, cur_body))
            cur_head, cur_body = s.rstrip(":").strip(), []
        else:
            cur_body.append(l)
    if cur_body:
        out.append((cur_head, cur_body))
    return out


def _accepts(rhs):
    rhs = _NUM_PREFIX_RE.sub("", rhs).strip().strip(".").strip()
    out = []
    for part in re.split(r"\s*/\s*|\s+eller\s+", rhs):
        p = part.strip().strip(".").strip()
        if not p or p.lower() in ("etc", "…", "..."):
            continue
        out.append(p)
        if "(" in p and ")" in p:
            out.append(re.sub(r"\s*\([^)]*\)", "", p).strip())   # drop the parenthetical
            out.append(re.sub(r"[()]", "", p).strip())            # keep its words
    seen, uniq = set(), []
    for x in out:
        k = normalize(x)
        if k and k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


def _lhs_of(line):
    """left-hand side of an 'X - ____' / 'X → ...' line, or None."""
    line = _NUM_PREFIX_RE.sub("", line).strip()
    m = re.split(r"\s*[-–—]{1,2}\s*|\s*→\s*", line, maxsplit=1)
    if len(m) == 2 and m[0].strip():
        return m[0].strip(), m[1].strip()
    return None


def _solution_pairs(sol_lines):
    pairs = {}
    for l in sol_lines:
        pr = _lhs_of(l)
        if pr and pr[1] and not _BLANK_RE.search(pr[1]):
            pairs.setdefault(normalize(pr[0]), pr[1])
    return pairs


def _norm_tok(t):
    return t.lower().strip(".,;:!?\"'()").strip()


def _diff_blanks(ex_sentence, sol_sentence):
    """Given 'Jag ___ glad.' + 'Jag är glad.' return ['är'] (one entry per blank).

    Matches the fixed words around each gap as whole tokens (punctuation-loose)
    and returns whatever the solution has in between.
    """
    ex = _NUM_PREFIX_RE.sub("", _HINT_RE.sub("", ex_sentence)).strip()
    sol = _NUM_PREFIX_RE.sub("", _HINT_RE.sub("", sol_sentence)).strip()
    # normalise the blank runs to a single sentinel token
    ex = _BLANK_RE.sub(" \x00 ", ex)
    ex_toks = [t for t in ex.split() if t]
    sol_toks = [t for t in sol.split() if t]
    if "\x00" not in ex_toks or not sol_toks:
        return None

    # segments of fixed tokens, split on the sentinel
    segs, cur = [], []
    for t in ex_toks:
        if t == "\x00":
            segs.append(cur)
            cur = []
        else:
            cur.append(t)
    segs.append(cur)                      # len(segs) == n_blanks + 1
    n_blanks = len(segs) - 1

    si = 0                                 # cursor into sol_toks
    vals = []
    for bi, seg in enumerate(segs):
        # the fixed segment must appear next in the solution
        for t in seg:
            if si >= len(sol_toks) or _norm_tok(sol_toks[si]) != _norm_tok(t):
                return None
            si += 1
        if bi == n_blanks:
            break
        # then capture up to the start of the next fixed segment
        nxt = segs[bi + 1]
        anchor = _norm_tok(nxt[0]) if nxt else None
        grabbed = []
        while si < len(sol_toks) and (anchor is None or _norm_tok(sol_toks[si]) != anchor):
            grabbed.append(sol_toks[si].strip(".,;:"))
            si += 1
        if not grabbed or len(grabbed) > 6:
            return None
        vals.append(" ".join(grabbed))
    # tolerate trailing punctuation-only tokens after the last fixed segment
    while si < len(sol_toks) and not _norm_tok(sol_toks[si]):
        si += 1
    return vals if si >= len(sol_toks) and len(vals) == n_blanks else None


def _int_word(s):
    for w, n in (("three", 3), ("3", 3), ("two", 2), ("2", 2), ("four", 4), ("five", 5), ("one", 1)):
        if w in s.lower():
            return n
    return None


def _mk_item(section, kind, prompt, **kw):
    it = {"section": section, "kind": kind, "prompt": prompt}
    it.update(kw)
    return it


def autoparse_lesson(number, folder_path):
    pdf = _exercises_pdf(folder_path)
    if not pdf:
        return None
    lines = _clean(_pdf_text(pdf))
    ex_lines, sol_lines = _split_solutions(lines)
    sol_pairs = _solution_pairs(sol_lines)
    sol_sections = {normalize(h): body for h, body in _sections(sol_lines)}
    sol_sentences = [l.strip() for l in sol_lines if l.strip() and not _lhs_of(l)]

    items = []
    for header, body in _sections(ex_lines):
        low = header.lower()
        sol_body = sol_sections.get(normalize(header), [])
        sol_body_lines = [l.strip() for l in sol_body if l.strip()]

        # ---- vocabulary table (named, or a bare English|Swedish grid) ----
        if any(k in low for k in _TABLE_HEADERS) or _looks_like_table(body):
            rows = _parse_table(body, sol_body_lines)
            if rows and any(r["accept"] for r in rows):
                items.append(_mk_item(header, "graded", header + " — fill the missing cell",
                                      grader="table", rows=rows))
            elif any(k in low for k in _TABLE_HEADERS):
                items.append(_mk_item(header, "open", header, reference=sol_body_lines or None))
            continue

        # ---- open-ended sections ----
        if any(low.startswith(k) or k in low for k in _OPEN_HEADERS):
            qs = _open_questions(body, sol_body_lines)
            items.extend(qs)
            continue

        # ---- multiple choice (a) b) c) with a ticked answer) ----
        mc = _parse_mc(header, body, sol_body_lines)
        if mc:
            items.extend(mc)
            continue

        # ---- circle / choose the correct form (X / Y) ----
        if any(k in low for k in _CHOICE_HEADERS):
            items.extend(_parse_circle(header, body, sol_body_lines))
            continue

        # ---- "write three ..." style sets ----
        if any(low.startswith(k) or k in low for k in _SET_HEADERS):
            n_blanks = sum(1 for l in body if _BLANK_RE.search(l)) or _int_word(header) or 3
            pool = [x for x in sol_body_lines if x and not _lhs_of(x)]
            pool = [_NUM_PREFIX_RE.sub("", x).strip() for x in pool]
            if pool:
                items.append(_mk_item(header, "graded", header, grader="set",
                                      n_required=min(n_blanks, len(pool)) or len(pool),
                                      distinct=True, accept_pool=pool))
            else:
                items.append(_mk_item(header, "open", header, reference=None))
            continue

        # ---- translate pairs  /  fill-in sentences ----
        items.extend(_parse_pairs_and_blanks(header, body, sol_pairs, sol_sentences, sol_body_lines))

    # de-dupe by (section, prompt)
    seen, uniq = set(), []
    for it in items:
        k = (normalize(it["section"]), normalize(it["prompt"]))
        if it["prompt"] and k not in seen:
            seen.add(k)
            uniq.append(it)
    return {"lesson": number, "source": "auto", "source_pdf": os.path.basename(pdf), "items": uniq}


_COL_WORDS = {"swedish", "english", "svenska", "engelska", "word", "translation", "meaning"}


def _looks_like_table(body):
    """True only when the body carries an explicit 'English   Swedish' (either
    order) column-header row - conservative on purpose so translate-pair lists
    aren't misread as grids."""
    for l in body:
        c = [x for x in re.split(r"\s{2,}", l.strip()) if x]
        if len(c) == 2 and {c[0].lower(), c[1].lower()} <= _COL_WORDS:
            return True
    return False


def _parse_table(body, sol_lines):
    def split_cols(l):
        return [c.strip() for c in re.split(r"\s{2,}", l.strip()) if c.strip()]
    sol_map = {}
    for l in sol_lines:
        cols = split_cols(l)
        if len(cols) == 2 and cols[0].lower() not in _COL_WORDS:
            sol_map[normalize(cols[0])] = cols[1]
            sol_map[normalize(cols[1])] = cols[0]
    rows = []
    for l in body:
        cols = split_cols(l)
        if not cols or cols[0].lower() in _COL_WORDS or _BLANK_RE.search(l):
            continue
        if len(cols) == 1:
            given = cols[0]
            # heuristic: a Swedish given tends to contain å/ä/ö or start with en/ett/att
            sv = bool(re.search(r"[åäö]", given.lower())) or given.lower().split()[0] in ("en", "ett", "att")
            other = sol_map.get(normalize(given), "")
            rows.append({"given": given, "given_side": "sv" if sv else "en",
                         "accept": _accepts(other) if other else []})
        elif len(cols) == 2:
            # both filled in the exercise already -> nothing to solve, skip
            continue
    return rows


def _open_questions(body, sol_lines):
    """Each 'question' = a run of prompt lines; reference = matching solution run."""
    chunks, cur = [], []
    for l in body:
        s = l.strip()
        if not s:
            if cur:
                chunks.append(cur)
                cur = []
        elif not _BLANK_RE.fullmatch(s.replace(" ", "")):
            if not set(s) <= set("_ "):
                cur.append(s)
    if cur:
        chunks.append(cur)

    sol_chunks, cur = [], []
    for l in sol_lines:
        s = l.strip()
        if not s:
            if cur:
                sol_chunks.append(" ".join(cur))
                cur = []
        else:
            cur.append(_NUM_PREFIX_RE.sub("", s))
    if cur:
        sol_chunks.append(" ".join(cur))

    out = []
    for i, ch in enumerate(chunks):
        prompt = " ".join(ch).strip()
        if not prompt or set(prompt) <= set("_ "):
            continue
        ref = None
        if i < len(sol_chunks):
            ref = [sol_chunks[i]]
        elif sol_chunks:
            ref = sol_chunks
        out.append(_mk_item(prompt[:120], "open", prompt, reference=ref))
    return out


def _parse_mc(header, body, sol_lines):
    items, stem, opts = [], None, []

    def flush():
        if stem and len(opts) >= 2:
            correct = _mc_correct(stem, opts, sol_lines)
            items.append(_mk_item(header, "graded", stem, grader="choice",
                                  options=[o for o, _ in opts],
                                  correct=correct or [opts[0][0]]))
    for l in body:
        s = l.strip()
        m = _MC_OPT_RE.match(s)
        if m:
            opts.append((m.group(2).strip(), m.group(1).lower()))
        elif _BLANK_RE.search(s):
            flush()
            stem, opts = _NUM_PREFIX_RE.sub("", s).strip(), []
        elif s and opts:
            flush()
            stem, opts = None, []
    flush()
    return items if any(len(x.get("options", [])) for x in items) else []


def _mc_correct(stem, opts, sol_lines):
    # find the stem in the solutions, then the ticked option after it
    idx = None
    for i, l in enumerate(sol_lines):
        if normalize(_NUM_PREFIX_RE.sub("", l)).startswith(normalize(stem)[:25]):
            idx = i
            break
    window = sol_lines[idx: idx + 2 + len(opts)] if idx is not None else sol_lines
    hits = []
    for l in window:
        if _TICK_RE.search(l):
            m = _MC_OPT_RE.match(l.strip())
            val = m.group(2).strip() if m else _TICK_RE.sub("", l).strip(" -–—")
            val = re.split(r"\s{2,}", val)[0].strip()
            if val:
                hits.append(val)
    return hits


def _parse_circle(header, body, sol_lines):
    out = []
    sol_tokens = [normalize(_NUM_PREFIX_RE.sub("", l)) for l in sol_lines]
    for l in body:
        s = _NUM_PREFIX_RE.sub("", l).strip()
        if "/" not in s or _BLANK_RE.search(s):
            continue
        opts = [o.strip() for o in s.split("/") if o.strip()]
        if len(opts) < 2:
            continue
        correct = []
        for i, sl in enumerate(sol_lines):
            st = _NUM_PREFIX_RE.sub("", sl).strip()
            if "/" in st:
                continue
            if any(normalize(st) == normalize(o) for o in opts):
                correct = [st]
                break
        if correct:
            out.append(_mk_item(header, "graded", s, grader="choice", options=opts, correct=correct))
        else:
            out.append(_mk_item(header, "open", s, reference=None))
    return out


def _parse_pairs_and_blanks(header, body, sol_pairs, sol_sentences, sol_body_lines):
    out = []
    sol_body_sentences = [l for l in sol_body_lines if not _lhs_of(l)]
    blank_i = 0
    for l in body:
        s = l.rstrip()
        if not s.strip() or set(s.strip()) <= set("_ "):
            continue
        pr = _lhs_of(s)
        has_blank = bool(_BLANK_RE.search(s))

        # 'English - ____'  ->  look the LHS up in the solutions
        if pr and (not pr[1] or set(pr[1]) <= set("_ ") or has_blank):
            lhs = pr[0].strip()
            rhs = sol_pairs.get(normalize(lhs))
            if rhs:
                out.append(_mk_item(header, "graded", lhs, grader="text",
                                    blanks=[{"accept": _accepts(rhs)}]))
            else:
                out.append(_mk_item(header, "open", lhs, reference=None))
            continue

        # a sentence with a gap  ->  diff against the matching solution sentence
        if has_blank:
            sol = None
            if blank_i < len(sol_body_sentences):
                sol = sol_body_sentences[blank_i]
            blank_i += 1
            prompt = _NUM_PREFIX_RE.sub("", s).strip()
            vals = _diff_blanks(prompt, sol) if sol else None
            if vals:
                out.append(_mk_item(header, "graded", _BLANK_RE.sub("____", prompt),
                                    grader="text", blanks=[{"accept": _accepts(v)} for v in vals]))
            elif sol:
                out.append(_mk_item(header, "open", _BLANK_RE.sub("____", prompt),
                                    reference=[sol.strip()]))
            else:
                out.append(_mk_item(header, "open", _BLANK_RE.sub("____", prompt), reference=None))
    return out


# ---------- ensure / load ----------

def _json_path(n):
    return os.path.join(DATA_DIR, f"{n}.json")


def ensure_exercises(db_path=None, verbose=True):
    """App-start hook: auto-parse lessons that have a PDF but no JSON, then load
    any lesson whose JSON isn't in the DB yet. Cheap once everything is parsed."""
    conn = sqlite3.connect(db_path or DB_PATH)
    try:
        init_exercises_db(conn)
        os.makedirs(DATA_DIR, exist_ok=True)
        folders = _lesson_folders()
        made = []
        for n, folder in folders.items():
            if os.path.exists(_json_path(n)) or not _exercises_pdf(folder):
                continue
            if _lesson_id(conn, n) is None:
                continue  # lesson row not created yet (parse_vocab.py hasn't seen it)
            data = autoparse_lesson(n, folder)
            if data and data["items"]:
                json.dump(data, open(_json_path(n), "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)
                made.append(n)
        if made and verbose:
            print(f"[exercises] auto-parsed new lesson(s): {', '.join(map(str, sorted(made)))}")

        loaded = []
        for f in sorted(os.listdir(DATA_DIR)) if os.path.isdir(DATA_DIR) else []:
            if not f.endswith(".json") or f.endswith(".draft.json"):
                continue
            try:
                n = json.load(open(os.path.join(DATA_DIR, f), encoding="utf-8"))["lesson"]
            except (ValueError, KeyError):
                continue
            lid = _lesson_id(conn, n)
            if lid is None:
                continue
            has_rows = conn.execute(
                "SELECT 1 FROM exercise_items WHERE lesson_id = ? LIMIT 1", (lid,)
            ).fetchone()
            if not has_rows:
                try:
                    load_file(conn, os.path.join(DATA_DIR, f))
                    loaded.append(n)
                except (ValueError, KeyError, json.JSONDecodeError) as e:
                    if verbose:
                        print(f"[exercises] skip {f}: {e}")
        if loaded and verbose:
            print(f"[exercises] loaded lesson(s): {', '.join(map(str, sorted(loaded)))}")
    finally:
        conn.close()


def cmd_load(_args):
    if not os.path.isdir(DATA_DIR):
        sys.exit(f"no {DATA_DIR}/")
    conn = sqlite3.connect(DB_PATH)
    init_exercises_db(conn)
    files = sorted(f for f in os.listdir(DATA_DIR)
                   if f.endswith(".json") and not f.endswith(".draft.json"))
    if not files:
        sys.exit(f"no *.json in {DATA_DIR}/")
    report, errors = [], []
    for f in files:
        try:
            report.append(load_file(conn, os.path.join(DATA_DIR, f)))
        except (ValueError, KeyError, json.JSONDecodeError) as e:
            errors.append(f"{f}:\n{e}")
    conn.close()
    print("\n".join(report))
    if errors:
        print("\nERRORS:\n" + "\n".join(errors))
        sys.exit(1)


def cmd_regen(args):
    n = args.lesson
    path = _json_path(n)
    if os.path.exists(path):
        try:
            src = json.load(open(path, encoding="utf-8")).get("source")
            if src in ("hand", "edited"):
                sys.exit(f"{path} has \"source\": \"{src}\" (manually corrected) - refusing to "
                         f"overwrite; delete it first if you really want to re-parse")
        except (ValueError, KeyError):
            pass
    folder = _lesson_folders().get(n)
    if not folder:
        sys.exit(f"no lesson folder for {n}")
    data = autoparse_lesson(n, folder)
    if not data or not data["items"]:
        sys.exit(f"parser found no exercises in {folder}")
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"wrote {path}  ({len(data['items'])} items, "
          f"{sum(1 for i in data['items'] if i['kind']=='graded')} graded)")
    conn = sqlite3.connect(DB_PATH)
    init_exercises_db(conn)
    print(load_file(conn, path))
    conn.close()


def cmd_status(_args):
    conn = sqlite3.connect(DB_PATH)
    have = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='exercise_items'"
    ).fetchone()
    print(f"{'#':>3}  {'pdf':>3}  {'json':>6}  {'items':>5}  {'graded':>6}  {'solved':>6}")
    folders = _lesson_folders()
    n_pdf = n_json = 0
    for n in sorted(folders):
        pdf = _exercises_pdf(folders[n])
        n_pdf += bool(pdf)
        src = "-"
        if os.path.exists(_json_path(n)):
            n_json += 1
            try:
                src = json.load(open(_json_path(n), encoding="utf-8")).get("source", "auto")
            except (ValueError, KeyError):
                src = "?"
        tot = grd = slv = 0
        if have:
            r = conn.execute(
                "SELECT COUNT(*), SUM(kind='graded'), SUM(solved) FROM exercise_items ei "
                "JOIN lessons l ON l.id = ei.lesson_id WHERE l.number = ?", (n,)
            ).fetchone()
            tot, grd, slv = (r[0] or 0), (r[1] or 0), (r[2] or 0)
        print(f"{n:>3}  {'yes' if pdf else '-':>3}  {src:>6}  {tot:>5}  {grd:>6}  {slv:>6}")
    conn.close()
    print(f"\n{n_json} / {n_pdf} lessons with an Exercises PDF have exercise data")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("load")
    sub.add_parser("status")
    rg = sub.add_parser("regen")
    rg.add_argument("lesson", type=int)

    argv = [a[2:] if a in ("--load", "--status", "--regen") else a for a in sys.argv[1:]]
    args = ap.parse_args(argv)

    if args.cmd == "load":
        cmd_load(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "regen":
        cmd_regen(args)
    else:
        ensure_exercises()


if __name__ == "__main__":
    main()
