"""Build and update a full-text index of (scanned) PDF documents.

Each page is read from the PDF's text layer when it has one; otherwise the
page is rendered to an image and OCR'd with Tesseract. Page text is stored in
a SQLite FTS5 table so searches return the document *and* page number.
"""

import hashlib
import io
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pymupdf as fitz
import pytesseract
from PIL import Image

# A page whose text layer has fewer characters than this is treated as a scan.
MIN_TEXT_CHARS = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    sha256    TEXT NOT NULL,
    mtime     REAL NOT NULL,
    page_count INTEGER NOT NULL,
    indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5(
    text,
    doc_id UNINDEXED,
    page_no UNINDEXED,
    source UNINDEXED,          -- 'text' (embedded text layer) or 'ocr'
    tokenize = '{tokenizer}'
);
"""


def connect(db_path, tokenizer="unicode61 remove_diacritics 2"):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA.format(tokenizer=tokenizer))
    return conn


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def extract_pages(path, lang="eng", dpi=300, force_ocr=False):
    """Return a list of (page_no, text, source) for every page of the PDF."""
    results = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, start=1):
            text = "" if force_ocr else page.get_text().strip()
            if len(text) >= MIN_TEXT_CHARS:
                results.append((i, text, "text"))
                continue
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            text = pytesseract.image_to_string(img, lang=lang).strip()
            results.append((i, text, "ocr"))
    return results


def _extract_job(path, lang, dpi, force_ocr):
    try:
        return path, extract_pages(path, lang, dpi, force_ocr), None
    except Exception as e:  # keep going if one file is corrupt
        return path, None, f"{type(e).__name__}: {e}"


def find_pdfs(root):
    root = Path(root)
    if root.is_file():
        return [root.resolve()]
    return sorted(p.resolve() for p in root.rglob("*") if p.suffix.lower() == ".pdf" and p.is_file())


def build_index(folders, db_path, lang="eng", dpi=300, workers=None,
                force_ocr=False, tokenizer="unicode61 remove_diacritics 2",
                prune=True, log=print):
    conn = connect(db_path, tokenizer)
    pdfs = [p for folder in folders for p in find_pdfs(folder)]

    known = {row[0]: (row[1], row[2], row[3]) for row in
             conn.execute("SELECT path, id, sha256, mtime FROM documents")}

    # Work out which files are new or changed (mtime first, hash to confirm).
    todo = []
    for p in pdfs:
        key = str(p)
        mtime = p.stat().st_mtime
        if key in known and known[key][2] == mtime and not force_ocr:
            continue
        sha = file_sha256(p)
        if key in known and known[key][1] == sha and not force_ocr:
            conn.execute("UPDATE documents SET mtime=? WHERE path=?", (mtime, key))
            continue
        todo.append((p, sha, mtime))
    conn.commit()

    if prune:
        present = {str(p) for p in pdfs}
        for path, (doc_id, _, _) in known.items():
            if path not in present:
                _delete_doc(conn, doc_id)
                log(f"removed  {path}")
        conn.commit()

    log(f"{len(pdfs)} PDF(s) found, {len(todo)} to index")
    if not todo:
        return conn

    meta = {str(p): (sha, mtime) for p, sha, mtime in todo}
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_extract_job, str(p), lang, dpi, force_ocr) for p, _, _ in todo]
        for fut in as_completed(futures):
            path, pages, err = fut.result()
            done += 1
            if err:
                log(f"[{done}/{len(todo)}] FAILED {path}: {err}")
                continue
            sha, mtime = meta[path]
            if path in known:
                _delete_doc(conn, known[path][0])
            cur = conn.execute(
                "INSERT INTO documents (path, sha256, mtime, page_count) VALUES (?, ?, ?, ?)",
                (path, sha, mtime, len(pages)))
            doc_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO pages (text, doc_id, page_no, source) VALUES (?, ?, ?, ?)",
                [(text, doc_id, n, src) for n, text, src in pages])
            conn.commit()
            n_ocr = sum(1 for _, _, src in pages if src == "ocr")
            log(f"[{done}/{len(todo)}] {path} ({len(pages)} pages, {n_ocr} OCR'd)")
    return conn


def _delete_doc(conn, doc_id):
    conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
