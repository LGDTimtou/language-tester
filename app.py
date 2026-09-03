import json
import os
import re
import sqlite3
import unicodedata

from flask import Flask, g, jsonify, render_template, request, abort

from grading import grade_item
from parse_exercises import (
    DATA_DIR, dump_lesson_json, ensure_exercises, init_exercises_db, item_key,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vocab.db")

app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        # make sure the exercises table exists even if parse_exercises.py has
        # never been run (the page just shows nothing until data is loaded)
        init_exercises_db(g.db)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def normalize_answer(s):
    """Case/punctuation-insensitive comparison, mirroring the frontend's rule."""
    s = (s or "").lower()
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] not in ("P", "S"))
    return re.sub(r"\s+", " ", s).strip()


def score_class(score):
    if score is None:
        return "none"
    if score >= 80:
        return "excellent"
    if score >= 50:
        return "fair"
    return "poor"


def word_stats_row(row):
    total = row["correct_count"] + row["wrong_count"]
    wrong_pct = round(100 * row["wrong_count"] / total) if total else None
    return {
        "id": row["id"],
        "swedish": row["swedish"],
        "english": row["english"],
        "tense": row["tense"],
        "verb_group": row["verb_group"],
        "correct_count": row["correct_count"],
        "wrong_count": row["wrong_count"],
        "total": total,
        "wrong_pct": wrong_pct,
        "known": bool(row["known"]),
        # tri-state: null = never in a completed round, true = missed at
        # least once last round, false = correct on the first try last round
        "last_round_missed": None if row["last_round_missed"] is None else bool(row["last_round_missed"]),
    }


# ---------- pages ----------

@app.route("/")
def index():
    db = get_db()
    rows = db.execute("""
        SELECT l.id, l.number, l.title, l.best_score,
               COUNT(w.id) AS word_count,
               SUM(w.known) AS known_count
        FROM lessons l
        LEFT JOIN words w ON w.lesson_id = l.id
        GROUP BY l.id
        ORDER BY l.number
    """).fetchall()

    # exercise progress per lesson: a lesson "counts as done" once every graded
    # item is solved (open/self-check items never hold it back)
    ex_rows = db.execute("""
        SELECT lesson_id,
               SUM(kind = 'graded') AS graded_total,
               SUM(kind = 'graded' AND solved = 1) AS graded_solved,
               COUNT(*) AS item_total
        FROM exercise_items
        GROUP BY lesson_id
    """).fetchall()
    ex_by_lesson = {r["lesson_id"]: r for r in ex_rows}

    lessons = []
    for r in rows:
        total = r["word_count"] or 0
        known = r["known_count"] or 0
        testable = total - known  # words actually in play for training (known ones are skipped)
        score = r["best_score"]
        correct_est = round(testable * score / 100) if (score is not None and testable > 0) else 0

        ex = ex_by_lesson.get(r["id"])
        ex_graded_total = (ex["graded_total"] or 0) if ex else 0
        ex_graded_solved = (ex["graded_solved"] or 0) if ex else 0
        lessons.append({
            "id": r["id"], "number": r["number"], "title": r["title"],
            "best_score": score, "word_count": total, "testable": testable,
            "correct_est": correct_est,
            "ex_has": bool(ex and ex["item_total"]),
            "ex_graded_total": ex_graded_total,
            "ex_graded_solved": ex_graded_solved,
            "ex_complete": ex_graded_total > 0 and ex_graded_solved == ex_graded_total,
        })

    return render_template("index.html", lessons=lessons, score_class=score_class)


@app.route("/lesson/<int:lesson_id>")
def lesson_page(lesson_id):
    db = get_db()
    lesson = db.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        abort(404)
    return render_template("lesson.html", lesson=lesson, score_class=score_class(lesson["best_score"]))


@app.route("/quiz/<int:lesson_id>")
def quiz_page(lesson_id):
    db = get_db()
    lesson = db.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        abort(404)
    return render_template("quiz.html", lesson=lesson)


@app.route("/exercises/<int:lesson_id>")
def exercises_page(lesson_id):
    db = get_db()
    lesson = db.execute("SELECT * FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        abort(404)
    return render_template("exercises.html", lesson=lesson)


# ---------- API ----------

@app.route("/api/lessons/<int:lesson_id>/words")
def api_lesson_words(lesson_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM words WHERE lesson_id = ? ORDER BY order_index", (lesson_id,)
    ).fetchall()
    return jsonify([word_stats_row(r) for r in rows])


@app.route("/api/lessons/<int:lesson_id>/words", methods=["POST"])
def api_create_word(lesson_id):
    db = get_db()
    if not db.execute("SELECT 1 FROM lessons WHERE id = ?", (lesson_id,)).fetchone():
        abort(404)
    body = request.get_json(force=True)
    swedish = (body.get("swedish") or "").strip()
    english = (body.get("english") or "").strip()
    if not swedish:
        return jsonify({"error": "Swedish text can't be empty"}), 400
    next_order = db.execute(
        "SELECT COALESCE(MAX(order_index), -1) + 1 FROM words WHERE lesson_id = ?", (lesson_id,)
    ).fetchone()[0]
    cur = db.execute(
        "INSERT INTO words (lesson_id, swedish, english, order_index) VALUES (?, ?, ?, ?)",
        (lesson_id, swedish, english, next_order),
    )
    db.commit()
    row = db.execute("SELECT * FROM words WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(word_stats_row(row)), 201


@app.route("/api/words/<int:word_id>", methods=["PATCH"])
def api_update_word(word_id):
    db = get_db()
    body = request.get_json(force=True)
    swedish = (body.get("swedish") or "").strip()
    english = (body.get("english") or "").strip()
    if not swedish:
        return jsonify({"error": "Swedish text can't be empty"}), 400
    cur = db.execute("UPDATE words SET swedish = ?, english = ? WHERE id = ?", (swedish, english, word_id))
    if cur.rowcount == 0:
        abort(404)
    db.commit()
    row = db.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    return jsonify(word_stats_row(row))


@app.route("/api/words/<int:word_id>", methods=["DELETE"])
def api_delete_word(word_id):
    db = get_db()
    cur = db.execute("DELETE FROM words WHERE id = ?", (word_id,))
    if cur.rowcount == 0:
        abort(404)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/words/<int:word_id>/known", methods=["POST"])
def api_set_known(word_id):
    db = get_db()
    known = 1 if request.get_json(force=True).get("known") else 0
    if known:
        # a known word is never part of an active quiz round, and shouldn't
        # carry a stale "missed last time" flag
        cur = db.execute(
            "UPDATE words SET known = 1, session_state = NULL, last_round_missed = NULL WHERE id = ?",
            (word_id,),
        )
    else:
        cur = db.execute("UPDATE words SET known = 0 WHERE id = ?", (word_id,))
    if cur.rowcount == 0:
        abort(404)
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/lessons/<int:lesson_id>/quiz-check", methods=["POST"])
def api_quiz_check(lesson_id):
    db = get_db()
    body = request.get_json(force=True)
    word_id = body.get("word_id")
    given = body.get("answer", "")
    direction = body.get("direction", "sv")

    current = db.execute(
        "SELECT * FROM words WHERE id = ? AND lesson_id = ?", (word_id, lesson_id)
    ).fetchone()
    if not current:
        abort(404)

    expected = current["swedish"] if direction == "sv" else current["english"]
    correct = normalize_answer(given) == normalize_answer(expected)

    if correct:
        db.execute("UPDATE words SET correct_count = correct_count + 1 WHERE id = ?", (word_id,))
        db.execute("UPDATE words SET session_state = 'done' WHERE id = ? AND session_state = 'pending'", (word_id,))
    else:
        db.execute("UPDATE words SET wrong_count = wrong_count + 1, round_missed = 1 WHERE id = ?", (word_id,))
    db.commit()

    return jsonify({"correct": correct, "correct_answer": expected})


@app.route("/api/lessons/<int:lesson_id>/quiz-typo-retry", methods=["POST"])
def api_quiz_typo_retry(lesson_id):
    """Retry after claiming the previous wrong answer was a typo. Get it
    right this time and the original miss is wiped - it counts as correct
    on the first try. Get it wrong again and the original miss just stands
    (not counted a second time)."""
    db = get_db()
    body = request.get_json(force=True)
    word_id = body.get("word_id")
    given = body.get("answer", "")
    direction = body.get("direction", "sv")

    current = db.execute(
        "SELECT * FROM words WHERE id = ? AND lesson_id = ?", (word_id, lesson_id)
    ).fetchone()
    if not current:
        abort(404)

    expected = current["swedish"] if direction == "sv" else current["english"]
    correct = normalize_answer(given) == normalize_answer(expected)

    if correct:
        db.execute(
            "UPDATE words SET wrong_count = MAX(wrong_count - 1, 0), correct_count = correct_count + 1, "
            "round_missed = 0 WHERE id = ?",
            (word_id,),
        )
        db.execute("UPDATE words SET session_state = 'done' WHERE id = ? AND session_state = 'pending'", (word_id,))
        db.commit()
    # if still wrong, the original wrong_count/round_missed already recorded
    # from the first attempt stand as-is - nothing further to change

    return jsonify({"correct": correct, "correct_answer": expected})


def _session_counts(db, lesson_id):
    row = db.execute("""
        SELECT SUM(session_state = 'pending') AS pending, SUM(session_state = 'done') AS done
        FROM words WHERE lesson_id = ?
    """, (lesson_id,)).fetchone()
    return (row["pending"] or 0), (row["done"] or 0)


@app.route("/api/lessons/<int:lesson_id>/quiz-status")
def api_quiz_status(lesson_id):
    """Read-only: never starts, resumes, or resets a round (unlike quiz-next)."""
    db = get_db()
    pending, done = _session_counts(db, lesson_id)
    mistake_count = db.execute(
        "SELECT COUNT(*) FROM words WHERE lesson_id = ? AND known = 0 AND last_round_missed = 1",
        (lesson_id,),
    ).fetchone()[0]
    return jsonify({
        "pending": pending, "done": done, "total": pending + done,
        "in_progress": pending > 0, "mistake_count": mistake_count,
    })


@app.route("/api/lessons/<int:lesson_id>/quiz-restart", methods=["POST"])
def api_quiz_restart(lesson_id):
    """Abandon the current round only - correct/wrong counts and known flags
    are untouched, so this doesn't lose any learning history."""
    db = get_db()
    db.execute(
        "UPDATE words SET session_state = NULL, round_missed = 0 WHERE lesson_id = ?", (lesson_id,)
    )
    db.execute("UPDATE lessons SET active_round_type = NULL WHERE id = ?", (lesson_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/lessons/<int:lesson_id>/quiz-next")
def api_quiz_next(lesson_id):
    db = get_db()
    mode = request.args.get("mode", "full")
    if mode not in ("full", "mistakes"):
        mode = "full"
    pending, done = _session_counts(db, lesson_id)

    if pending == 0 and done == 0:
        # no round in progress: start a fresh one
        if mode == "mistakes":
            where = "known = 0 AND last_round_missed = 1"
        else:
            where = "known = 0"
        db.execute(
            f"UPDATE words SET session_state = 'pending', round_missed = 0 WHERE lesson_id = ? AND {where}",
            (lesson_id,),
        )
        db.execute("UPDATE lessons SET active_round_type = ? WHERE id = ?", (mode, lesson_id))
        db.commit()
        pending, done = _session_counts(db, lesson_id)

    if pending == 0:
        # either nothing to quiz (done == 0) or the round just finished (done > 0);
        # score = % of this round's words gotten right on the first try
        score = None
        if done > 0:
            row = db.execute(
                "SELECT SUM(round_missed = 0) AS first_try FROM words WHERE lesson_id = ? AND session_state = 'done'",
                (lesson_id,),
            ).fetchone()
            score = round(100 * (row["first_try"] or 0) / done)

            lesson = db.execute(
                "SELECT best_score, active_round_type FROM lessons WHERE id = ?", (lesson_id,)
            ).fetchone()
            # only a full round updates what "missed last time" means and the
            # best score - a mistakes-practice round is a smaller, easier
            # subset, so its own result shouldn't overwrite either record.
            # This is what keeps the "exercise mistakes" button in place
            # (and doesn't reduce its count) after practicing: it only ever
            # changes on the next full round.
            if lesson["active_round_type"] != "mistakes":
                db.execute(
                    "UPDATE words SET last_round_missed = round_missed WHERE lesson_id = ? AND session_state = 'done'",
                    (lesson_id,),
                )
                if lesson["best_score"] is None or score > lesson["best_score"]:
                    db.execute("UPDATE lessons SET best_score = ? WHERE id = ?", (score, lesson_id))

        # reset so the *next* call starts a brand new round
        db.execute("UPDATE words SET session_state = NULL, round_missed = 0 WHERE lesson_id = ?", (lesson_id,))
        db.execute("UPDATE lessons SET active_round_type = NULL WHERE id = ?", (lesson_id,))
        db.commit()
        return jsonify({"word": None, "pending": 0, "done": done, "total": done, "score": score})

    row = db.execute("""
        SELECT *,
               (correct_count + wrong_count) AS total,
               CASE WHEN (correct_count + wrong_count) = 0 THEN -1.0
                    ELSE CAST(correct_count AS FLOAT) / (correct_count + wrong_count)
               END AS correct_pct
        FROM words
        WHERE lesson_id = ? AND session_state = 'pending'
        ORDER BY total ASC, correct_pct ASC, RANDOM()
        LIMIT 1
    """, (lesson_id,)).fetchone()

    word = word_stats_row(row)
    return jsonify({"word": word, "pending": pending, "done": done, "total": pending + done})


@app.route("/api/lessons/<int:lesson_id>/reset-progress", methods=["POST"])
def api_reset_progress(lesson_id):
    db = get_db()
    db.execute(
        "UPDATE words SET correct_count = 0, wrong_count = 0, known = 0, session_state = NULL, "
        "round_missed = 0, last_round_missed = NULL WHERE lesson_id = ?",
        (lesson_id,),
    )
    db.execute("UPDATE lessons SET best_score = NULL, active_round_type = NULL WHERE id = ?", (lesson_id,))
    db.commit()
    return jsonify({"ok": True})


# ---------- exercises ----------

def _render_spec(grader, spec):
    """Client-facing render hints - deliberately strips every accepted answer."""
    if grader == "translate":
        return {}
    if grader in ("fill", "text"):
        return {"n_blanks": len(spec.get("blanks", [])) or 1,
                "template": spec.get("template", "")}
    if grader == "set":
        return {"n_required": spec.get("n_required", 1)}
    if grader == "choice":
        return {"options": spec.get("options", [])}
    if grader == "table":
        return {"rows": [
            {"given": r.get("given", ""), "given_side": r.get("given_side", "sv"),
             "blank": bool(r.get("accept"))}
            for r in spec.get("rows", [])
        ]}
    return {}


def _exercise_rows(db, lesson_id):
    return db.execute(
        "SELECT * FROM exercise_items WHERE lesson_id = ? ORDER BY order_index", (lesson_id,)
    ).fetchall()


def _exercise_counts(rows):
    graded = [r for r in rows if r["kind"] == "graded"]
    solved = [r for r in graded if r["solved"]]
    return len(graded), len(solved)


@app.route("/api/lessons/<int:lesson_id>/exercises")
def api_exercises(lesson_id):
    db = get_db()
    lesson = db.execute("SELECT id, number, title FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        abort(404)
    rows = _exercise_rows(db, lesson_id)
    graded_total, graded_solved = _exercise_counts(rows)

    items = []
    for r in rows:
        spec = json.loads(r["spec_json"] or "{}")
        item = {
            "id": r["id"],
            "section": r["section"],
            "kind": r["kind"],
            "grader": r["grader"],
            "prompt": r["prompt"],
            "solved": bool(r["solved"]),
            "self_done": bool(r["self_done"]),
            "last_answer": json.loads(r["last_answer_json"]) if r["last_answer_json"] else None,
        }
        if r["kind"] == "graded":
            item["render"] = _render_spec(r["grader"], spec)
        else:
            item["reference"] = json.loads(r["reference_json"]) if r["reference_json"] else []
        items.append(item)

    return jsonify({
        "lesson": {"id": lesson["id"], "number": lesson["number"], "title": lesson["title"]},
        "graded_total": graded_total, "graded_solved": graded_solved,
        "items": items,
    })


@app.route("/api/lessons/<int:lesson_id>/exercises/check", methods=["POST"])
def api_exercises_check(lesson_id):
    db = get_db()
    answers = (request.get_json(force=True) or {}).get("answers", {})
    rows = _exercise_rows(db, lesson_id)

    results = {}
    for r in rows:
        if str(r["id"]) not in answers and r["id"] not in answers:
            continue
        given = answers.get(str(r["id"]), answers.get(r["id"]))
        # persist whatever was typed, for both graded and open items
        db.execute(
            "UPDATE exercise_items SET last_answer_json = ? WHERE id = ?",
            (json.dumps(given, ensure_ascii=False), r["id"]),
        )
        if r["kind"] != "graded":
            continue
        spec = json.loads(r["spec_json"] or "{}")
        ok = grade_item(r["grader"], spec, given)
        db.execute("UPDATE exercise_items SET solved = ? WHERE id = ?", (1 if ok else 0, r["id"]))
        results[r["id"]] = ok

    db.commit()
    rows = _exercise_rows(db, lesson_id)
    graded_total, graded_solved = _exercise_counts(rows)
    return jsonify({
        "results": results,
        "graded_total": graded_total, "graded_solved": graded_solved,
        "all_solved": graded_total > 0 and graded_solved == graded_total,
    })


@app.route("/api/lessons/<int:lesson_id>/exercises/save", methods=["POST"])
def api_exercises_save(lesson_id):
    """Autosave typed answers without grading, so a reload restores them."""
    db = get_db()
    answers = (request.get_json(force=True) or {}).get("answers", {})
    ids = {r["id"] for r in _exercise_rows(db, lesson_id)}
    for k, v in answers.items():
        try:
            item_id = int(k)
        except (TypeError, ValueError):
            continue
        if item_id in ids:
            db.execute(
                "UPDATE exercise_items SET last_answer_json = ? WHERE id = ?",
                (json.dumps(v, ensure_ascii=False), item_id),
            )
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/exercises/<int:item_id>/self-done", methods=["POST"])
def api_exercise_self_done(item_id):
    db = get_db()
    body = request.get_json(force=True) or {}
    done = 1 if body.get("done") else 0
    cur = db.execute("UPDATE exercise_items SET self_done = ? WHERE id = ?", (done, item_id))
    if cur.rowcount == 0:
        abort(404)
    if "answer" in body:
        db.execute(
            "UPDATE exercise_items SET last_answer_json = ? WHERE id = ?",
            (json.dumps(body["answer"], ensure_ascii=False), item_id),
        )
    db.commit()
    return jsonify({"ok": True, "self_done": bool(done)})


@app.route("/api/lessons/<int:lesson_id>/exercises/reset", methods=["POST"])
def api_exercises_reset(lesson_id):
    """Clears exercise progress only (typed answers, solved, self-done)."""
    db = get_db()
    db.execute(
        "UPDATE exercise_items SET solved = 0, self_done = 0, last_answer_json = NULL "
        "WHERE lesson_id = ?",
        (lesson_id,),
    )
    db.commit()
    return jsonify({"ok": True})


# ---- editing the accepted answers (parsed answers sometimes have small errors) ----

def _answer_editable(grader, spec):
    """The bits of a graded item's spec the user is allowed to correct."""
    if grader == "translate":
        return {"accept": list(spec.get("accept", []))}
    if grader in ("fill", "text"):
        return {"template": spec.get("template", ""),
                "blanks": [list(b.get("accept", [])) for b in spec.get("blanks", [])]}
    if grader == "set":
        return {"accept_pool": list(spec.get("accept_pool", [])),
                "n_required": spec.get("n_required", 1)}
    if grader == "choice":
        return {"options": list(spec.get("options", [])), "correct": list(spec.get("correct", []))}
    if grader == "table":
        return {"rows": [
            {"given": r.get("given", ""), "given_side": r.get("given_side", "sv"),
             "accept": list(r.get("accept", []))}
            for r in spec.get("rows", [])
        ]}
    return {}


def _clean_list(v):
    return [s.strip() for s in v if isinstance(s, str) and s.strip()]


def _apply_answer_edit(grader, spec, edit):
    """Return a new spec with the user's corrections merged in (empty edits ignored)."""
    spec = json.loads(json.dumps(spec))  # deep copy
    if grader == "translate":
        acc = _clean_list(edit.get("accept", []))
        if acc:
            spec["accept"] = acc
    elif grader in ("fill", "text"):
        if edit.get("template"):
            spec["template"] = edit["template"]
        for i, acc in enumerate(edit.get("blanks", [])):
            acc = _clean_list(acc)
            if acc and i < len(spec.get("blanks", [])):
                spec["blanks"][i]["accept"] = acc
    elif grader == "set":
        pool = _clean_list(edit.get("accept_pool", []))
        if pool:
            spec["accept_pool"] = pool
        try:
            n = int(edit.get("n_required", spec.get("n_required", 1)))
            spec["n_required"] = max(1, min(n, len(spec.get("accept_pool", [])) or n))
        except (TypeError, ValueError):
            pass
    elif grader == "choice":
        cor = _clean_list(edit.get("correct", []))
        if cor:
            spec["correct"] = cor
    elif grader == "table":
        for i, acc in enumerate(edit.get("rows", [])):
            acc = _clean_list(acc)
            if acc and i < len(spec.get("rows", [])):
                spec["rows"][i]["accept"] = acc
    return spec


def _spec_from_form(grader, form):
    """Build a fresh grader spec from the inline / bulk editor's fields."""
    form = form or {}
    if grader == "translate":
        return {"accept": _clean_list(form.get("accept", []))}
    if grader in ("fill", "text"):
        template = (form.get("template") or "").strip()
        nums = sorted({int(m) for m in re.findall(r"\{(\d+)\}", template)})
        for new_i, old in enumerate(nums):          # renumber to {0}..{k-1}
            template = template.replace("{%d}" % old, "\x00%d\x00" % new_i)
        template = re.sub(r"\x00(\d+)\x00", r"{\1}", template)
        blanks_in = form.get("blanks", [])
        blanks = [{"accept": _clean_list(blanks_in[j]) if j < len(blanks_in) else []}
                  for j in range(len(nums))] or [{"accept": []}]
        return {"template": template, "blanks": blanks}
    if grader == "set":
        pool = _clean_list(form.get("accept_pool", []))
        try:
            n = int(form.get("n_required", 1))
        except (TypeError, ValueError):
            n = 1
        return {"accept_pool": pool, "n_required": max(1, min(n, len(pool) or n)), "distinct": True}
    if grader == "choice":
        return {"options": _clean_list(form.get("options", [])),
                "correct": _clean_list(form.get("correct", []))}
    if grader == "table":
        rows = []
        for r in form.get("rows", []):
            acc = _clean_list(r.get("accept", []))
            given = (r.get("given") or "").strip()
            if given or acc:
                rows.append({"given": given,
                             "given_side": "en" if r.get("given_side") == "en" else "sv",
                             "accept": acc})
        return {"rows": rows}
    return {}


def _spec_problem(grader, spec):
    """Human-readable reason the spec is unusable, or None."""
    if grader == "translate":
        if not spec.get("accept"):
            return "add at least one accepted answer"
    elif grader in ("fill", "text"):
        if "{0}" not in spec.get("template", "") and "{1}" not in spec.get("template", ""):
            return "put a blank in the sentence with {0} (and {1}, {2}, … for more)"
        if not spec["blanks"] or any(not b["accept"] for b in spec["blanks"]):
            return "every blank needs at least one accepted answer"
    elif grader == "set":
        if not spec["accept_pool"]:
            return "the accepted pool is empty"
    elif grader == "choice":
        if not spec["options"] or not spec["correct"]:
            return "choice needs options and at least one correct value"
    elif grader == "table":
        if not any(r["accept"] for r in spec["rows"]):
            return "at least one table row needs an accepted answer"
    else:
        return f"unknown type {grader!r}"
    return None


@app.route("/api/lessons/<int:lesson_id>/exercises/answers")
def api_exercises_answers(lesson_id):
    """Current accepted answers for every graded item - only fetched when the
    user opens the answer editor."""
    db = get_db()
    if not db.execute("SELECT 1 FROM lessons WHERE id = ?", (lesson_id,)).fetchone():
        abort(404)
    out = []
    for r in _exercise_rows(db, lesson_id):
        if r["kind"] != "graded":
            continue
        spec = json.loads(r["spec_json"] or "{}")
        out.append({
            "id": r["id"], "section": r["section"], "prompt": r["prompt"],
            "grader": r["grader"], "editable": _answer_editable(r["grader"], spec),
        })
    return jsonify({"items": out})


@app.route("/api/lessons/<int:lesson_id>/exercises/answers", methods=["POST"])
def api_exercises_answers_save(lesson_id):
    db = get_db()
    lesson = db.execute("SELECT number FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson:
        abort(404)
    edits = (request.get_json(force=True) or {}).get("items", {})
    rows = {str(r["id"]): r for r in _exercise_rows(db, lesson_id)}

    updated = 0
    for sid, edit in edits.items():
        r = rows.get(str(sid))
        if not r or r["kind"] != "graded":
            continue
        old = json.loads(r["spec_json"] or "{}")
        new = _apply_answer_edit(r["grader"], old, edit or {})
        if json.dumps(new, sort_keys=True) == json.dumps(old, sort_keys=True):
            continue
        db.execute(
            "UPDATE exercise_items SET spec_json = ?, solved = 0 WHERE id = ?",
            (json.dumps(new, ensure_ascii=False), r["id"]),
        )
        updated += 1
    db.commit()

    wrote = False
    if updated:
        try:
            wrote = dump_lesson_json(db, lesson["number"])
        except OSError as e:
            print(f"[exercises] could not update JSON for lesson {lesson['number']}: {e}")

    return jsonify({"ok": True, "updated": updated, "json_updated": wrote})


# ---- one item at a time: reveal answer, edit it inline, change its type/heading ----

def _item_detail(r):
    spec = json.loads(r["spec_json"] or "{}")
    return {
        "id": r["id"], "uid": r["uid"], "order_index": r["order_index"],
        "section": r["section"], "kind": r["kind"], "grader": r["grader"],
        "prompt": r["prompt"], "solved": bool(r["solved"]),
        "editable": _answer_editable(r["grader"], spec) if r["kind"] == "graded" else {},
        "reference": json.loads(r["reference_json"]) if r["reference_json"] else [],
    }


@app.route("/api/exercises/<int:item_id>")
def api_exercise_get(item_id):
    db = get_db()
    r = db.execute("SELECT * FROM exercise_items WHERE id = ?", (item_id,)).fetchone()
    if not r:
        abort(404)
    return jsonify(_item_detail(r))


@app.route("/api/exercises/<int:item_id>", methods=["POST"])
def api_exercise_patch(item_id):
    """Edit one item: type (incl. graded <-> open), accepted answers / reference,
    prompt, and/or its heading. Body: {grader?, form?, prompt?, heading?}.
    grader == "open" makes it a self-check item. Regenerates the lesson JSON."""
    db = get_db()
    r = db.execute("SELECT * FROM exercise_items WHERE id = ?", (item_id,)).fetchone()
    if not r:
        abort(404)
    lesson = db.execute("SELECT number FROM lessons WHERE id = ?", (r["lesson_id"],)).fetchone()
    body = request.get_json(force=True) or {}
    form = body.get("form", {}) or {}

    req = body.get("grader")
    new_kind = "open" if req == "open" else "graded"
    prompt = body.get("prompt", r["prompt"])
    if prompt is None or not str(prompt).strip():
        return jsonify({"error": "prompt can't be empty"}), 400
    prompt = str(prompt).strip()

    if new_kind == "graded":
        grader = req or (r["grader"] if r["kind"] == "graded" else "translate")
        if "form" in body or req:
            spec = _spec_from_form(grader, form)
        else:
            spec = json.loads(r["spec_json"] or "{}")
        # a fill item's sentence IS its prompt - keep the two in sync
        if grader in ("fill", "text") and spec.get("template"):
            prompt = spec["template"]
        problem = _spec_problem(grader, spec)
        if problem:
            return jsonify({"error": problem}), 400
        db.execute(
            "UPDATE exercise_items SET kind = 'graded', grader = ?, prompt = ?, spec_json = ?, "
            "reference_json = NULL, item_key = ?, solved = 0, self_done = 0 WHERE id = ?",
            (grader, prompt, json.dumps(spec, ensure_ascii=False),
             item_key(prompt, r["section"] or ""), item_id),
        )
    else:  # open / self-check item
        ref = form.get("reference", body.get("reference"))
        if isinstance(ref, list):
            ref_list = _clean_list(ref)
        else:
            ref_list = json.loads(r["reference_json"]) if r["reference_json"] else []
        keep_done = r["self_done"] if r["kind"] == "open" else 0
        db.execute(
            "UPDATE exercise_items SET kind = 'open', grader = NULL, prompt = ?, spec_json = '{}', "
            "reference_json = ?, item_key = ?, solved = 0, self_done = ? WHERE id = ?",
            (prompt, json.dumps(ref_list, ensure_ascii=False) if ref_list else None,
             item_key(prompt, r["section"] or ""), keep_done, item_id),
        )

    if "heading" in body:
        _set_heading_run(db, r["lesson_id"], r["order_index"], (body["heading"] or "").strip() or None)

    db.commit()
    wrote = False
    try:
        wrote = dump_lesson_json(db, lesson["number"])
    except OSError as e:
        print(f"[exercises] JSON write failed for lesson {lesson['number']}: {e}")

    fresh = db.execute("SELECT * FROM exercise_items WHERE id = ?", (item_id,)).fetchone()
    return jsonify({"ok": True, "json_updated": wrote, "item": _item_detail(fresh)})


def _set_heading_run(db, lesson_id, order_index, new_section):
    """Rename the heading of the contiguous section-run that contains the item at
    order_index. If that item starts the run it's a plain rename; if it's in the
    middle the run is split there (that item + the ones after it get new_section)."""
    rows = db.execute(
        "SELECT id, order_index, section FROM exercise_items WHERE lesson_id = ? "
        "ORDER BY order_index, id", (lesson_id,),
    ).fetchall()
    idx = next((i for i, x in enumerate(rows) if x["order_index"] == order_index), None)
    if idx is None:
        return
    target = rows[idx]["section"]
    end = idx
    while end + 1 < len(rows) and rows[end + 1]["section"] == target:
        end += 1
    for x in rows[idx:end + 1]:
        db.execute(
            "UPDATE exercise_items SET section = ?, item_key = ? WHERE id = ?",
            (new_section, item_key(
                db.execute("SELECT prompt FROM exercise_items WHERE id = ?", (x["id"],)).fetchone()[0],
                new_section or ""), x["id"]),
        )


@app.route("/api/exercises/<int:item_id>/heading", methods=["POST"])
def api_exercise_heading(item_id):
    db = get_db()
    r = db.execute("SELECT * FROM exercise_items WHERE id = ?", (item_id,)).fetchone()
    if not r:
        abort(404)
    lesson = db.execute("SELECT number FROM lessons WHERE id = ?", (r["lesson_id"],)).fetchone()
    title = ((request.get_json(force=True) or {}).get("title") or "").strip() or None
    _set_heading_run(db, r["lesson_id"], r["order_index"], title)
    db.commit()
    try:
        dump_lesson_json(db, lesson["number"])
    except OSError:
        pass
    return jsonify({"ok": True})


if __name__ == "__main__":
    # self-heal on start: parse any lesson that has an Exercises PDF but no data
    # yet, and load anything not in the DB (new lesson folders "just work")
    try:
        ensure_exercises(DB_PATH)
    except Exception as e:  # never let exercise parsing block the server
        print(f"[exercises] ensure_exercises skipped: {e}")
    app.run(debug=True, port=5055)
