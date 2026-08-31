import json
import os
import re
import sqlite3
import unicodedata

from flask import Flask, g, jsonify, render_template, request, abort

from grading import grade_item
from parse_exercises import init_exercises_db

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
    if grader == "text":
        return {"n_blanks": len(spec.get("blanks", [])) or 1}
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


if __name__ == "__main__":
    app.run(debug=True, port=5055)
