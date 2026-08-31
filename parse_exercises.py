"""Load per-lesson exercise data into vocab.db.

The gradable content lives in hand-authored JSON (one file per lesson) under
exercises_data/, interpreted from each lesson's Exercises PDF. This script:

  * --load   (default)  read exercises_data/<n>.json -> upsert into exercise_items,
                         preserving any solved / self_done / typed-answer progress
  * --draft <n>          best-effort dump of the Exercises PDF into
                         exercises_data/<n>.draft.json to start authoring from
  * --status             show, per lesson, whether a PDF / JSON exists and counts

Re-running --load is safe: items are keyed by a hash of their prompt, so progress
survives edits elsewhere in the file, and rows dropped from the JSON are only
deleted when they carry no progress (same rule as parse_vocab.py).
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys

from grading import normalize

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "exercises_data")
# override points are handy when running from a git worktree, where the default
# sibling PatreonDownloads/ and vocab.db don't exist
DOWNLOADS_DIR = os.environ.get(
    "LSWK_DOWNLOADS_DIR", os.path.join(os.path.dirname(BASE_DIR), "PatreonDownloads")
)
DB_PATH = os.environ.get("LSWK_DB", os.path.join(BASE_DIR, "vocab.db"))

VALID_GRADERS = {"text", "set", "choice", "table"}
ANSWER_MARKER_RE = re.compile(
    r"correct answers? (below|&|and)|answers below|answers & examples", re.IGNORECASE
)


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
    return hashlib.sha1((normalize(section) + "||" + normalize(prompt)).encode("utf-8")).hexdigest()[:12]


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
        kind = it.get("kind")
        if kind not in ("graded", "open"):
            problems.append(f"{where}: kind must be 'graded' or 'open'")
            continue
        if not it.get("prompt"):
            problems.append(f"{where}: missing prompt")
        if kind == "graded":
            g = it.get("grader")
            if g not in VALID_GRADERS:
                problems.append(f"{where}: grader must be one of {sorted(VALID_GRADERS)}")
                continue
            spec = _build_spec(it)
            if g == "text" and not spec["blanks"]:
                problems.append(f"{where}: text grader needs at least one blank")
            if g == "text":
                for b in spec["blanks"]:
                    if not b["accept"]:
                        problems.append(f"{where}: a blank has no accepted answers")
            if g == "set" and (not spec["accept_pool"] or spec["n_required"] < 1):
                problems.append(f"{where}: set grader needs accept_pool and n_required >= 1")
            if g == "choice" and (not spec["options"] or not spec["correct"]):
                problems.append(f"{where}: choice grader needs options and correct")
            if g == "table" and not any(r["accept"] for r in spec["rows"]):
                problems.append(f"{where}: table grader needs at least one fillable row")
    return problems


def load_file(conn, path):
    data = json.load(open(path, encoding="utf-8"))
    lesson_no = data["lesson"]
    items = data.get("items", [])

    problems = _validate(lesson_no, items)
    if problems:
        raise ValueError("  " + "\n  ".join(problems))

    row = conn.execute("SELECT id FROM lessons WHERE number = ?", (lesson_no,)).fetchone()
    if not row:
        raise ValueError(f"lesson {lesson_no} not found in DB (run parse_vocab.py first)")
    lesson_id = row[0]

    fresh_keys = set()
    n_graded = n_open = new_rows = 0
    for order_index, it in enumerate(items):
        kind = it["kind"]
        grader = it.get("grader") if kind == "graded" else None
        spec = _build_spec(it) if kind == "graded" else {}
        reference = it.get("reference")
        key = item_key(it["prompt"], it.get("section", ""))
        fresh_keys.add(key)
        if kind == "graded":
            n_graded += 1
        else:
            n_open += 1

        existing = conn.execute(
            "SELECT id FROM exercise_items WHERE lesson_id = ? AND item_key = ?",
            (lesson_id, key),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE exercise_items SET order_index = ?, section = ?, kind = ?, "
                "grader = ?, prompt = ?, spec_json = ?, reference_json = ? WHERE id = ?",
                (order_index, it.get("section"), kind, grader, it["prompt"],
                 json.dumps(spec, ensure_ascii=False),
                 json.dumps(reference, ensure_ascii=False) if reference is not None else None,
                 existing[0]),
            )
        else:
            new_rows += 1
            conn.execute(
                "INSERT INTO exercise_items (lesson_id, item_key, order_index, section, "
                "kind, grader, prompt, spec_json, reference_json) VALUES (?,?,?,?,?,?,?,?,?)",
                (lesson_id, key, order_index, it.get("section"), kind, grader, it["prompt"],
                 json.dumps(spec, ensure_ascii=False),
                 json.dumps(reference, ensure_ascii=False) if reference is not None else None),
            )

    # drop rows no longer in the JSON, but never one the user has made progress on
    stale = conn.execute(
        "SELECT id, item_key, solved, self_done FROM exercise_items WHERE lesson_id = ?",
        (lesson_id,),
    ).fetchall()
    kept_with_progress = 0
    for row_id, key, solved, self_done in stale:
        if key in fresh_keys:
            continue
        if solved or self_done:
            kept_with_progress += 1
            continue
        conn.execute("DELETE FROM exercise_items WHERE id = ?", (row_id,))

    conn.commit()
    tail = f"  ({kept_with_progress} orphaned-with-progress kept)" if kept_with_progress else ""
    return f"{lesson_no:>3}. graded={n_graded:<3} open={n_open:<3} new={new_rows}{tail}"


def cmd_load(args):
    if not os.path.isdir(DATA_DIR):
        sys.exit(f"no {DATA_DIR}/ - nothing to load")
    conn = sqlite3.connect(DB_PATH)
    init_exercises_db(conn)
    files = sorted(
        f for f in os.listdir(DATA_DIR)
        if f.endswith(".json") and not f.endswith(".draft.json")
    )
    if not files:
        sys.exit(f"no authored *.json in {DATA_DIR}/")
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


# ---------- PDF -> draft JSON ----------

def _exercises_pdf(folder_path):
    cands = [f for f in os.listdir(folder_path) if re.search(r"exercise", f, re.IGNORECASE)]
    cands.sort()
    return os.path.join(folder_path, cands[0]) if cands else None


def _lesson_folder(lesson_no):
    for f in os.listdir(DOWNLOADS_DIR):
        m = re.match(r"^(\d+)\.", f)
        if m and int(m.group(1)) == lesson_no and os.path.isdir(os.path.join(DOWNLOADS_DIR, f)):
            return os.path.join(DOWNLOADS_DIR, f)
    return None


def _pdf_text(path):
    return subprocess.run(
        ["pdftotext", "-layout", path, "-"], capture_output=True, text=True
    ).stdout


NUM_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
PAIR_RE = re.compile(r"^(.*?)\s*[-–—→>]{1,2}\s*(.*)$")
BLANK_RE = re.compile(r"_{2,}")


def cmd_draft(args):
    lesson_no = args.lesson
    folder = _lesson_folder(lesson_no)
    if not folder:
        sys.exit(f"no lesson folder for {lesson_no}")
    pdf = _exercises_pdf(folder)
    if not pdf:
        sys.exit(f"no Exercises PDF in {folder}")
    text = _pdf_text(pdf).replace("\x0c", "\n")

    lines = [l.rstrip() for l in text.split("\n")]
    split_at = next((i for i, l in enumerate(lines) if ANSWER_MARKER_RE.search(l)), len(lines))
    prompt_lines = lines[:split_at]
    answer_lines = lines[split_at + 1:]

    # crude left-hand-side -> answer map from the solutions section
    ans_map = {}
    for l in answer_lines:
        l = NUM_RE.sub(r"\2", l.strip())
        m = PAIR_RE.match(l)
        if m and m.group(1).strip() and m.group(2).strip():
            ans_map[normalize(m.group(1))] = m.group(2).strip()

    items = []
    section = None
    for raw in prompt_lines:
        l = raw.strip()
        if not l:
            continue
        if l.endswith(":") and not BLANK_RE.search(l) and "_" not in l:
            section = l.rstrip(":").strip()
            continue
        body = NUM_RE.sub(r"\2", l)
        if not BLANK_RE.search(body):
            continue
        left = BLANK_RE.split(body)[0].strip(" -–—→>")
        guess = ans_map.get(normalize(left), "")
        items.append({
            "section": section or "Exercises",
            "kind": "graded" if guess else "open",
            "grader": "text" if guess else None,
            "prompt": BLANK_RE.sub("____", body).strip(),
            **({"blanks": [{"accept": [g.strip() for g in re.split(r'\s*/\s*', guess) if g.strip()]}]}
               if guess else {}),
            **({} if guess else {"reference": []}),
        })

    os.makedirs(DATA_DIR, exist_ok=True)
    out = os.path.join(DATA_DIR, f"{lesson_no}.draft.json")
    json.dump(
        {"lesson": lesson_no, "source_pdf": os.path.basename(pdf),
         "_note": "BEST-EFFORT DRAFT - verify every item against the PDF, then save as "
                  f"{lesson_no}.json",
         "_raw_solutions": answer_lines,
         "items": items},
        open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2,
    )
    print(f"wrote {out}  ({len(items)} candidate items, "
          f"{sum(1 for i in items if i['kind'] == 'graded')} pre-filled)")


# ---------- status ----------

def cmd_status(args):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    have_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='exercise_items'"
    ).fetchone()
    rows = []
    for f in sorted(os.listdir(DOWNLOADS_DIR)):
        m = re.match(r"^(\d+)\.", f)
        if not m or not os.path.isdir(os.path.join(DOWNLOADS_DIR, f)):
            continue
        n = int(m.group(1))
        pdf = _exercises_pdf(os.path.join(DOWNLOADS_DIR, f))
        js = os.path.join(DATA_DIR, f"{n}.json")
        in_db = (0, 0, 0)
        if have_table:
            r = conn.execute(
                "SELECT COUNT(*), SUM(kind='graded'), SUM(solved) FROM exercise_items ei "
                "JOIN lessons l ON l.id = ei.lesson_id WHERE l.number = ?", (n,)
            ).fetchone()
            in_db = (r[0] or 0, r[1] or 0, r[2] or 0)
        rows.append((n, bool(pdf), os.path.exists(js), in_db))
    conn.close()

    print(f"{'#':>3}  {'pdf':>3}  {'json':>4}  {'items':>5}  {'graded':>6}  {'solved':>6}")
    for n, has_pdf, has_json, (tot, grd, slv) in rows:
        print(f"{n:>3}  {'yes' if has_pdf else '-':>3}  {'yes' if has_json else '-':>4}  "
              f"{tot:>5}  {grd:>6}  {slv:>6}")
    print(f"\nauthored: {sum(1 for r in rows if r[2])} / "
          f"{sum(1 for r in rows if r[1])} lessons with an Exercises PDF")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("load")
    d = sub.add_parser("draft")
    d.add_argument("lesson", type=int)
    sub.add_parser("status")
    # allow "--load" / "--draft N" / "--status" spelling too
    argv = [a.lstrip("-") if a in ("--load", "--draft", "--status") else a for a in sys.argv[1:]]
    args = ap.parse_args(argv)

    if args.cmd == "draft":
        cmd_draft(args)
    elif args.cmd == "status":
        cmd_status(args)
    else:
        cmd_load(args)


if __name__ == "__main__":
    main()
