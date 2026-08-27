"""One-off / re-runnable importer: extracts vocabulary word pairs and verb
tense tables from each lesson's Vocabulary PDF and loads them into vocab.db.

Safe to re-run: existing words (matched by lesson+swedish+tense) keep their
correct/wrong/known progress; only new words are inserted, and words that
disappeared from the source PDF are left untouched (never deleted), so
re-parsing never destroys quiz history.
"""
import os
import re
import sqlite3
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(os.path.dirname(BASE_DIR), "PatreonDownloads")
DB_PATH = os.path.join(BASE_DIR, "vocab.db")

TENSE_KEYWORDS = [
    ("presens particip", "Presens particip"),
    ("perfekt particip", "Perfekt particip"),
    ("presens", "Presens"),
    ("preteritum", "Preteritum"),
    ("perfekt", "Perfekt"),
    ("supinum", "Supinum"),
    ("imperativ", "Imperativ"),
]


def classify_header(label):
    """Return ('swedish'|'english'|tense-name|'ignore') for a header cell."""
    norm = label.lower().strip()
    for needle, tense_name in TENSE_KEYWORDS:
        if needle in norm:
            return tense_name
    if "svenska" in norm or "infinitiv" in norm or "grundform" in norm or "swedish" in norm:
        return "swedish"
    if "english" in norm or "engelska" in norm or "meaning" in norm:
        return "english"
    return "ignore"


def split_cols(line):
    return [c.strip() for c in re.split(r"\s{2,}", line.strip()) if c.strip()]


def clean_tense_value(val):
    # strip trailing parentheticals like "(imperfekt)" from a cell value
    return re.sub(r"\s*\([^)]*\)\s*$", "", val).strip()


def pdf_to_text(path):
    result = subprocess.run(
        ["pdftotext", "-layout", path, "-"],
        capture_output=True, text=True,
    )
    return result.stdout


NOISE_LINE_RE = re.compile(r"^\d+\.\s*(Vocabulary|Cheat Sheet)\s*\d*$", re.IGNORECASE)


def extract_blocks(text):
    """Split pdftotext -layout output into table blocks (2+ blank lines = boundary)."""
    text = text.replace("\x0c", "\n\n")  # form feed = page break = force boundary
    lines = text.split("\n")
    blocks = []
    current = []
    blank_run = 0
    for raw in lines:
        line = raw.rstrip()
        if NOISE_LINE_RE.match(line.strip()):
            line = ""  # treat repeated page header/footer as blank
        if not line.strip():
            blank_run += 1
            continue
        if blank_run >= 2 and current:
            blocks.append(current)
            current = []
        blank_run = 0
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


ROW_TENSE_MAP = [
    ("presens particip", "Presens particip"),
    ("perfekt particip", "Perfekt particip"),
    ("infinitiv", "Infinitiv"),
    ("presens", "Presens"),
    ("preteritum", "Preteritum"),
    ("perfekt", "Perfekt"),
    ("supinum", "Supinum"),
    ("imperativ", "Imperativ"),
]


def row_label_to_tense(label):
    norm = label.lower().strip()
    for needle, name in ROW_TENSE_MAP:
        if needle in norm:
            return name
    return None


def parse_transposed_verb_block(block, verb_counter_start):
    """Handle tables like 'Form | Att tro | Att tycka | Att tänka' where rows
    are tenses and columns are individual verbs."""
    header_cols = split_cols(block[0])
    verb_names = header_cols[1:]
    verb_keys = [f"vt{verb_counter_start + j}" for j in range(len(verb_names))]
    verb_forms = []
    for j, name in enumerate(verb_names):
        verb_forms.append({"swedish": name, "english": "", "tense": "Infinitiv", "verb_key": verb_keys[j]})
    for line in block[1:]:
        cols = split_cols(line)
        if len(cols) < 2:
            continue
        tense_name = row_label_to_tense(cols[0])
        if not tense_name or tense_name == "Infinitiv":
            continue  # infinitive already captured from the header row
        for j, val in enumerate(cols[1:]):
            if j >= len(verb_names) or not val:
                continue
            verb_forms.append({"swedish": val, "english": "", "tense": tense_name, "verb_key": verb_keys[j]})
    return verb_forms, verb_counter_start + len(verb_names)


ARROW_RE = re.compile(r"[→>]{1,2}")
QUOTE_CHARS = "“”\"'‘’"


def parse_arrow_phrases(text):
    """Fallback for '“Phrase” → Translation' lists, including
    entries that word-wrap across multiple physical lines."""
    text = text.replace("\x0c", "\n\n")
    lines = [NOISE_LINE_RE.sub("", l.strip()) if NOISE_LINE_RE.match(l.strip()) else l.strip()
             for l in text.split("\n")]
    paragraphs = []
    current = []
    for line in lines:
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        has_arrow = "→" in line or "->" in line
        current_has_arrow = any("→" in l or "->" in l for l in current)
        if current and has_arrow and not current_has_arrow:
            # a heading/noise line (no arrow) got glued to this fresh entry
            # by a missing blank line; drop the noise, start a new paragraph
            current = []
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))

    phrases = []
    for para in paragraphs:
        if "→" not in para and "->" not in para:
            continue
        left, right = ARROW_RE.split(para, maxsplit=1)
        swedish = left.strip().strip(QUOTE_CHARS).strip()
        english = right.strip().strip(QUOTE_CHARS).strip()
        if swedish and english:
            phrases.append({"swedish": swedish, "english": english})
    return phrases


def parse_table_blocks(text):
    """Yield normal word dicts and verb-form dicts from the vocabulary text."""
    words = []          # {swedish, english}
    verb_forms = []      # {swedish, english, tense, verb_key}
    known_meanings = {}  # swedish -> english, learned as we go
    verb_counter = 0
    transposed_counter = 0

    for block in extract_blocks(text):
        header_cols = split_cols(block[0])
        if not header_cols:
            continue
        roles = [classify_header(c) for c in header_cols]
        if "swedish" not in roles and "english" not in roles:
            if header_cols[0].lower() == "form" and len(header_cols) > 1:
                forms, transposed_counter = parse_transposed_verb_block(block, transposed_counter)
                verb_forms.extend(forms)
            continue  # not a vocab table (prose, title, etc.)

        swedish_idx = roles.index("swedish") if "swedish" in roles else 0
        english_idx = roles.index("english") if "english" in roles else None
        tense_idxs = [(i, r) for i, r in enumerate(roles)
                      if r not in ("swedish", "english", "ignore")]
        is_verb_table = len(tense_idxs) > 0

        for line in block[1:]:
            cols = split_cols(line)
            if len(cols) < 1:
                continue
            # pad short rows so index access below never throws
            while len(cols) < len(header_cols):
                cols.append("")

            swedish_val = cols[swedish_idx] if swedish_idx < len(cols) else ""
            english_val = cols[english_idx] if english_idx is not None and english_idx < len(cols) else ""
            if not swedish_val:
                continue

            if is_verb_table:
                meaning = english_val or known_meanings.get(swedish_val, "")
                verb_counter += 1
                verb_key = f"v{verb_counter}"
                verb_forms.append({
                    "swedish": swedish_val, "english": meaning,
                    "tense": "Infinitiv", "verb_key": verb_key,
                })
                for idx, tense_name in tense_idxs:
                    val = cols[idx] if idx < len(cols) else ""
                    if not val:
                        continue
                    verb_forms.append({
                        "swedish": clean_tense_value(val), "english": meaning,
                        "tense": tense_name, "verb_key": verb_key,
                    })
                if meaning:
                    known_meanings[swedish_val] = meaning
            else:
                words.append({"swedish": swedish_val, "english": english_val})
                if english_val:
                    known_meanings[swedish_val] = english_val

    return words, verb_forms


NUMBERED_DASH_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s+[–—-]\s+(.+)$")


def parse_numbered_dash_fallback(text):
    """Fallback for prose-style lists like '3. Lagom - not too much...'."""
    words = []
    for raw in text.split("\n"):
        m = NUMBERED_DASH_RE.match(raw.strip())
        if m:
            words.append({"swedish": m.group(1).strip(), "english": m.group(2).strip()})
    return words


def find_vocab_pdf(folder_path):
    candidates = [f for f in os.listdir(folder_path) if re.search(r"vocabulary", f, re.IGNORECASE)]
    if not candidates:
        return None
    candidates.sort()
    return os.path.join(folder_path, candidates[0])


def parse_lesson_folder(folder_name):
    folder_path = os.path.join(DOWNLOADS_DIR, folder_name)
    m = re.match(r"^(\d+)\.(.*)$", folder_name)
    if not m:
        return None
    number = int(m.group(1))
    title = m.group(2).replace("_", " ").strip()

    pdf_path = find_vocab_pdf(folder_path)
    if not pdf_path:
        return {"number": number, "title": title, "folder": folder_name,
                "words": [], "verb_forms": [], "pdf": None}

    text = pdf_to_text(pdf_path)
    words, verb_forms = parse_table_blocks(text)

    existing_swedish = {w["swedish"] for w in words} | {vf["swedish"] for vf in verb_forms}
    for phrase in parse_arrow_phrases(text):
        if phrase["swedish"] not in existing_swedish:
            words.append(phrase)
            existing_swedish.add(phrase["swedish"])

    if not words and not verb_forms:
        words = parse_numbered_dash_fallback(text)

    # de-dupe: a verb's infinitive is already captured as a tense='Infinitiv'
    # entry in verb_forms, so drop any plain-word entry with the same text
    infinitives = {vf["swedish"] for vf in verb_forms if vf["tense"] == "Infinitiv"}
    words = [w for w in words if w["swedish"] not in infinitives]

    for w in words:
        w.setdefault("group", None)

    return {"number": number, "title": title, "folder": folder_name,
            "words": words, "verb_forms": verb_forms,
            "pdf": os.path.basename(pdf_path)}


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS lessons (
        id INTEGER PRIMARY KEY,
        number INTEGER NOT NULL,
        title TEXT NOT NULL,
        folder TEXT NOT NULL UNIQUE,
        best_score INTEGER,
        active_round_type TEXT
    );
    CREATE TABLE IF NOT EXISTS words (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lesson_id INTEGER NOT NULL REFERENCES lessons(id),
        swedish TEXT NOT NULL,
        english TEXT,
        tense TEXT,
        verb_group TEXT,
        correct_count INTEGER NOT NULL DEFAULT 0,
        wrong_count INTEGER NOT NULL DEFAULT 0,
        known INTEGER NOT NULL DEFAULT 0,
        order_index INTEGER NOT NULL,
        session_state TEXT,
        round_missed INTEGER NOT NULL DEFAULT 0,
        last_round_missed INTEGER,
        UNIQUE(lesson_id, swedish, tense, order_index)
    );
    """)
    word_cols = [r[1] for r in conn.execute("PRAGMA table_info(words)")]
    if "session_state" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN session_state TEXT")
    if "round_missed" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN round_missed INTEGER NOT NULL DEFAULT 0")
    if "last_round_missed" not in word_cols:
        conn.execute("ALTER TABLE words ADD COLUMN last_round_missed INTEGER")
    lesson_cols = [r[1] for r in conn.execute("PRAGMA table_info(lessons)")]
    if "best_score" not in lesson_cols:
        conn.execute("ALTER TABLE lessons ADD COLUMN best_score INTEGER")
    if "active_round_type" not in lesson_cols:
        conn.execute("ALTER TABLE lessons ADD COLUMN active_round_type TEXT")
    conn.commit()


def upsert_lesson(conn, folder, number, title):
    cur = conn.execute("SELECT id FROM lessons WHERE folder = ?", (folder,))
    row = cur.fetchone()
    if row:
        conn.execute("UPDATE lessons SET number = ?, title = ? WHERE id = ?", (number, title, row[0]))
        return row[0]
    cur = conn.execute("INSERT INTO lessons (number, title, folder) VALUES (?, ?, ?)", (number, title, folder))
    return cur.lastrowid


def upsert_word(conn, lesson_id, swedish, english, tense, verb_group, order_index):
    cur = conn.execute(
        "SELECT id FROM words WHERE lesson_id = ? AND swedish = ? AND IFNULL(tense,'') = IFNULL(?,'')",
        (lesson_id, swedish, tense),
    )
    row = cur.fetchone()
    if row:
        conn.execute("UPDATE words SET english = ?, verb_group = ? WHERE id = ?", (english, verb_group, row[0]))
        return 0
    conn.execute(
        "INSERT INTO words (lesson_id, swedish, english, tense, verb_group, order_index) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (lesson_id, swedish, english, tense, verb_group, order_index),
    )
    return 1


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    folders = sorted(
        [f for f in os.listdir(DOWNLOADS_DIR) if os.path.isdir(os.path.join(DOWNLOADS_DIR, f))],
        key=lambda f: (int(re.match(r"^(\d+)\.", f).group(1)), f) if re.match(r"^(\d+)\.", f) else (999, f),
    )

    report = []
    total_new = 0
    for folder in folders:
        data = parse_lesson_folder(folder)
        if data is None:
            continue
        lesson_id = upsert_lesson(conn, data["folder"], data["number"], data["title"])
        order_index = 0
        new_count = 0
        fresh_keys = set()
        for w in data["words"]:
            new_count += upsert_word(conn, lesson_id, w["swedish"], w["english"], None, w.get("group"), order_index)
            fresh_keys.add((w["swedish"], None))
            order_index += 1
        for vf in data["verb_forms"]:
            new_count += upsert_word(conn, lesson_id, vf["swedish"], vf["english"], vf["tense"], vf["verb_key"], order_index)
            fresh_keys.add((vf["swedish"], vf["tense"]))
            order_index += 1
        total_new += new_count

        # drop untouched DB rows that no longer match the freshly parsed
        # vocabulary (e.g. a de-duped duplicate from an earlier parser bug);
        # never touch a row the user has actually quizzed or marked known
        stale = conn.execute(
            "SELECT id, swedish, tense FROM words WHERE lesson_id = ? "
            "AND correct_count = 0 AND wrong_count = 0 AND known = 0",
            (lesson_id,),
        ).fetchall()
        for row_id, sw, tn in stale:
            if (sw, tn) not in fresh_keys:
                conn.execute("DELETE FROM words WHERE id = ?", (row_id,))
        n_words = len(data["words"])
        n_verbforms = len(data["verb_forms"])
        flag = "" if (n_words + n_verbforms) > 0 else "  <-- NO VOCAB FOUND"
        report.append(f"{data['number']:>3}. {data['title'][:45]:<45} pdf={data['pdf'] or '(none)':<20} "
                       f"words={n_words:<4} verb_forms={n_verbforms:<4} new={new_count}{flag}")

    conn.commit()
    conn.close()

    print("\n".join(report))
    print(f"\nTotal new word rows inserted: {total_new}")


if __name__ == "__main__":
    main()
