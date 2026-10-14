"""Build and update a full-text index of (scanned) PDF documents.

Each page is read from the PDF's text layer when it has one; otherwise the
page is rendered to an image and OCR'd with Tesseract. Page text is stored in
a SQLite FTS5 table so searches return the document *and* page number.

Optionally, a "searchable" copy of each PDF is written: the original page
images are kept untouched and Tesseract's invisible text layer is laid over
them, so Ctrl+F / copy-paste work in any PDF viewer.
"""

import hashlib
import io
import os
import shutil
import sqlite3
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import pymupdf as fitz
import pytesseract
from PIL import Image

# A page whose text layer has fewer characters than this is treated as a scan.
MIN_TEXT_CHARS = 20

# On Windows, Tesseract is often installed but not on PATH; look in the usual places.
if os.name == "nt" and not shutil.which("tesseract"):
    for _base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                  os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                  os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        _exe = os.path.join(_base, "Tesseract-OCR", "tesseract.exe")
        if os.path.isfile(_exe):
            pytesseract.pytesseract.tesseract_cmd = _exe
            break


def check_tesseract(lang):
    """Fail early with a readable message instead of once per file."""
    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        raise SystemExit("Tesseract OCR was not found. Install it (see README.md) and try again.")
    missing = set(lang.split("+")) - set(pytesseract.get_languages())
    if missing:
        raise SystemExit(f"Tesseract language data missing: {', '.join(sorted(missing))}. "
                         "Re-run the Tesseract installer and tick these languages.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    sha256    TEXT NOT NULL,
    mtime     REAL NOT NULL,
    page_count INTEGER NOT NULL,
    searchable_path TEXT,      -- copy with an OCR text layer, if one was written
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
    cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "searchable_path" not in cols:  # databases created before this column existed
        conn.execute("ALTER TABLE documents ADD COLUMN searchable_path TEXT")
    return conn


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ocr_page(page, lang="eng", dpi=300):
    """OCR one page. Returns (text, text_layer) where text_layer is a one-page
    PDF holding only Tesseract's invisible text, sized to the rendered page."""
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    img = Image.open(io.BytesIO(pix.tobytes("png")))
    pdf = pytesseract.image_to_pdf_or_hocr(img, lang=lang, extension="pdf",
                                           config="-c textonly_pdf=1")
    layer = fitz.open("pdf", pdf)
    return layer[0].get_text().strip(), layer


def extract_pages(path, lang="eng", dpi=300, force_ocr=False, searchable_path=None):
    """Return a list of (page_no, text, source) for every page of the PDF.

    If searchable_path is given, also write a copy of the PDF there with the
    OCR text laid invisibly over each scanned page.
    """
    results = []
    with fitz.open(path) as doc:
        layers = []
        for i, page in enumerate(doc, start=1):
            existing = page.get_text().strip()
            if len(existing) >= MIN_TEXT_CHARS and not force_ocr:
                results.append((i, existing, "text"))
                continue
            text, layer = ocr_page(page, lang, dpi)
            results.append((i, text, "ocr"))
            # Don't stack a second text layer on a page that already has one.
            if len(existing) < MIN_TEXT_CHARS:
                layers.append((page, layer))

        if searchable_path:
            searchable_path = Path(searchable_path)
            searchable_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = searchable_path.with_name(searchable_path.name + ".tmp")
            if layers:
                for page, layer in layers:
                    # The layer matches the page as displayed; rotate it back
                    # into the page's own coordinate system.
                    page.show_pdf_page(page.cropbox, layer, 0, rotate=page.rotation, overlay=True)
                doc.save(tmp, garbage=3, deflate=True)
            else:
                shutil.copy2(path, tmp)  # nothing to add; keep the output folder complete
            os.replace(tmp, searchable_path)
    return results


def _extract_job(path, lang, dpi, force_ocr, searchable_path):
    try:
        return path, extract_pages(path, lang, dpi, force_ocr, searchable_path), None
    except Exception as e:  # keep going if one file is corrupt
        return path, None, f"{type(e).__name__}: {e}"


def find_pdfs(root, exclude=None):
    """Return (pdf_path, root) pairs; root is used to mirror the folder layout."""
    root = Path(root).resolve()
    if root.is_file():
        return [(root, root.parent)]
    found = []
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() != ".pdf" or not p.is_file():
            continue
        p = p.resolve()
        if exclude and p.is_relative_to(exclude):
            continue  # don't index our own searchable copies
        found.append((p, root))
    return found


def build_index(folders, db_path, lang="eng", dpi=300, workers=None,
                force_ocr=False, tokenizer="unicode61 remove_diacritics 2",
                prune=True, searchable_dir=None, log=print):
    check_tesseract(lang)
    conn = connect(db_path, tokenizer)
    if searchable_dir:
        searchable_dir = Path(searchable_dir).resolve()
    known = {row[0]: (row[1], row[2], row[3], row[4]) for row in
             conn.execute("SELECT path, id, sha256, mtime, searchable_path FROM documents")}

    # Never index our own searchable copies, even on a run without --searchable-dir.
    copies = {v[3] for v in known.values() if v[3]}
    pdfs = [(p, root) for folder in folders for p, root in find_pdfs(folder, exclude=searchable_dir)
            if str(p) not in copies]

    def out_path(p, root):
        if not searchable_dir:
            return None
        if len(folders) > 1:  # keep files from different folders apart
            return searchable_dir / root.name / p.relative_to(root)
        return searchable_dir / p.relative_to(root)

    # Work out which files are new or changed (mtime first, hash to confirm).
    # A file is also redone if its searchable copy was requested but is missing.
    todo = []
    for p, root in pdfs:
        key = str(p)
        out = out_path(p, root)
        out_ok = out is None or (key in known and known[key][3] == str(out) and out.is_file())
        mtime = p.stat().st_mtime
        if key in known and known[key][2] == mtime and out_ok and not force_ocr:
            continue
        sha = file_sha256(p)
        if key in known and known[key][1] == sha and out_ok and not force_ocr:
            conn.execute("UPDATE documents SET mtime=? WHERE path=?", (mtime, key))
            continue
        todo.append((p, sha, mtime, out))
    conn.commit()

    if prune:
        present = {str(p) for p, _ in pdfs}
        for path, (doc_id, _, _, _) in known.items():
            if path not in present:
                _delete_doc(conn, doc_id)
                log(f"removed  {path}")
        conn.commit()

    log(f"{len(pdfs)} PDF(s) found, {len(todo)} to index")
    if not todo:
        return conn

    meta = {str(p): (sha, mtime, out) for p, sha, mtime, out in todo}
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_extract_job, str(p), lang, dpi, force_ocr, out)
                   for p, _, _, out in todo]
        for fut in as_completed(futures):
            path, pages, err = fut.result()
            done += 1
            if err:
                log(f"[{done}/{len(todo)}] FAILED {path}: {err}")
                continue
            sha, mtime, out = meta[path]
            if path in known:
                _delete_doc(conn, known[path][0])
            cur = conn.execute(
                "INSERT INTO documents (path, sha256, mtime, page_count, searchable_path)"
                " VALUES (?, ?, ?, ?, ?)",
                (path, sha, mtime, len(pages), str(out) if out else None))
            doc_id = cur.lastrowid
            conn.executemany(
                "INSERT INTO pages (text, doc_id, page_no, source) VALUES (?, ?, ?, ?)",
                [(text, doc_id, n, src) for n, text, src in pages])
            conn.commit()
            n_ocr = sum(1 for _, _, src in pages if src == "ocr")
            log(f"[{done}/{len(todo)}] {path} ({len(pages)} pages, {n_ocr} OCR'd)"
                + (f" -> {out}" if out else ""))
    return conn


def _delete_doc(conn, doc_id):
    conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
