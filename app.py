import os
import re
import sqlite3
import unicodedata

from flask import Flask, g, jsonify, render_template, request, abort

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "vocab.db")

app = Flask(__name__)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
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
    return render_template("index.html", lessons=rows, score_class=score_class)


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


# ---------- API ----------

@app.route("/api/lessons/<int:lesson_id>/words")
def api_lesson_words(lesson_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM words WHERE lesson_id = ? ORDER BY order_index", (lesson_id,)
    ).fetchall()
    return jsonify([word_stats_row(r) for r in rows])


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


@app.route("/api/words/<int:word_id>/known", methods=["POST"])
def api_set_known(word_id):
    db = get_db()
    known = 1 if request.get_json(force=True).get("known") else 0
    if known:
        # a known word is never part of an active quiz round
        cur = db.execute("UPDATE words SET known = 1, session_state = NULL WHERE id = ?", (word_id,))
    else:
        cur = db.execute("UPDATE words SET known = 0 WHERE id = ?", (word_id,))
    if cur.rowcount == 0:
        abort(404)
    db.commit()
    return jsonify({"ok": True})


def _synonym_siblings(db, lesson_id, word_row):
    """Other words in the same lesson sharing this word's meaning group
    (only plain words are grouped this way; verb tense forms never are)."""
    if word_row["tense"] is not None or not word_row["verb_group"]:
        return []
    rows = db.execute(
        "SELECT * FROM words WHERE lesson_id = ? AND verb_group = ? AND tense IS NULL AND id != ?",
        (lesson_id, word_row["verb_group"], word_row["id"]),
    ).fetchall()
    return rows


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

    norm_given = normalize_answer(given)
    siblings = _synonym_siblings(db, lesson_id, current) if direction == "sv" else []

    credited_id = None
    if direction == "sv":
        # accept the current word's own answer, or any not-yet-used sibling answer
        candidates = [current] + [s for s in siblings if s["session_state"] == "pending"]
        for cand in candidates:
            if normalize_answer(cand["swedish"]) == norm_given:
                credited_id = cand["id"]
                break
    else:
        if normalize_answer(current["english"]) == norm_given:
            credited_id = current["id"]

    correct = credited_id is not None

    if correct:
        db.execute("UPDATE words SET correct_count = correct_count + 1 WHERE id = ?", (credited_id,))
        db.execute("UPDATE words SET session_state = 'done' WHERE id = ? AND session_state = 'pending'", (credited_id,))
    else:
        db.execute("UPDATE words SET wrong_count = wrong_count + 1, round_missed = 1 WHERE id = ?", (word_id,))
    db.commit()

    correct_answer = current["swedish"] if direction == "sv" else current["english"]
    return jsonify({"correct": correct, "correct_answer": correct_answer})


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
    return jsonify({"pending": pending, "done": done, "total": pending + done, "in_progress": pending > 0})


@app.route("/api/lessons/<int:lesson_id>/quiz-restart", methods=["POST"])
def api_quiz_restart(lesson_id):
    """Abandon the current round only - correct/wrong counts and known flags
    are untouched, so this doesn't lose any learning history."""
    db = get_db()
    db.execute("UPDATE words SET session_state = NULL, round_missed = 0 WHERE lesson_id = ?", (lesson_id,))
    db.commit()
    return jsonify({"ok": True})


@app.route("/api/lessons/<int:lesson_id>/quiz-next")
def api_quiz_next(lesson_id):
    db = get_db()
    pending, done = _session_counts(db, lesson_id)

    if pending == 0 and done == 0:
        # no round in progress: start a fresh one covering every not-known word
        db.execute(
            "UPDATE words SET session_state = 'pending', round_missed = 0 WHERE lesson_id = ? AND known = 0",
            (lesson_id,),
        )
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
            lesson = db.execute("SELECT best_score FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
            if lesson["best_score"] is None or score > lesson["best_score"]:
                db.execute("UPDATE lessons SET best_score = ? WHERE id = ?", (score, lesson_id))

        # reset so the *next* call starts a brand new round
        db.execute("UPDATE words SET session_state = NULL, round_missed = 0 WHERE lesson_id = ?", (lesson_id,))
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
    siblings = _synonym_siblings(db, lesson_id, row)
    if siblings:
        already_used = any(s["session_state"] == "done" for s in siblings)
        word["group_hint"] = (
            "This word has more than one correct answer — give a different one than last time."
            if already_used else
            "This word has more than one correct answer — either is fine."
        )
    else:
        word["group_hint"] = None

    return jsonify({"word": word, "pending": pending, "done": done, "total": pending + done})


@app.route("/api/lessons/<int:lesson_id>/reset-progress", methods=["POST"])
def api_reset_progress(lesson_id):
    db = get_db()
    db.execute(
        "UPDATE words SET correct_count = 0, wrong_count = 0, known = 0, session_state = NULL, round_missed = 0 "
        "WHERE lesson_id = ?",
        (lesson_id,),
    )
    db.execute("UPDATE lessons SET best_score = NULL WHERE id = ?", (lesson_id,))
    db.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5055)
