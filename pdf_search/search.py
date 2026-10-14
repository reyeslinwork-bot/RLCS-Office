"""Query the page index built by indexer.py."""

import re
import sqlite3


def to_fts_query(q):
    """Turn a plain user query into a safe FTS5 query.

    - "quoted phrases" are kept as phrases
    - OR / NOT / AND keywords are passed through
    - every other word is quoted and gets a prefix wildcard, so `invoic`
      matches `invoice`, and punctuation can't break the FTS5 syntax
    """
    parts = []
    for phrase, word in re.findall(r'"([^"]*)"|(\S+)', q):
        if phrase:
            phrase = phrase.replace('"', "").strip()
            if phrase:
                parts.append(f'"{phrase}"')
        elif word in ("OR", "AND", "NOT"):
            parts.append(word)
        else:
            word = word.replace('"', "")
            if word:
                parts.append(f'"{word}"*')
    # Drop dangling operators that would make FTS5 raise a syntax error.
    while parts and parts[0] in ("OR", "AND", "NOT"):
        parts.pop(0)
    while parts and parts[-1] in ("OR", "AND", "NOT"):
        parts.pop()
    return " ".join(parts)


def search(conn, query, limit=50, offset=0, raw=False):
    """Return matching pages, best first.

    Each result is a dict with path, page_no, source, snippet and score.
    Matched terms in the snippet are wrapped in [[ ]].
    """
    fts = query if raw else to_fts_query(query)
    if not fts:
        return []
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT d.id AS doc_id, d.path, p.page_no, p.source,
               snippet(pages, 0, '[[', ']]', ' … ', 24) AS snippet,
               bm25(pages) AS score
        FROM pages p JOIN documents d ON d.id = p.doc_id
        WHERE pages MATCH ?
        ORDER BY score
        LIMIT ? OFFSET ?
        """,
        (fts, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


def stats(conn):
    docs, = conn.execute("SELECT COUNT(*) FROM documents").fetchone()
    pages, = conn.execute("SELECT COUNT(*) FROM pages").fetchone()
    ocr, = conn.execute("SELECT COUNT(*) FROM pages WHERE source = 'ocr'").fetchone()
    return {"documents": docs, "pages": pages, "ocr_pages": ocr}
