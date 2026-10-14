"""Command line entry point: python -m pdf_search {index,search,serve,stats}"""

import argparse
import sys
from pathlib import Path

from . import indexer
from .search import search, stats


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pdf_search", description="OCR and search scanned PDF documents.")
    ap.add_argument("--db", default="pdf_index.db", help="index database file (default: pdf_index.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="OCR/index PDFs in one or more folders (re-run to pick up changes)")
    p.add_argument("folders", nargs="+")
    p.add_argument("--lang", default="eng",
                   help="Tesseract language(s), e.g. eng, chi_sim, eng+chi_tra (default: eng)")
    p.add_argument("--dpi", type=int, default=300, help="render resolution for OCR (default: 300)")
    p.add_argument("--workers", type=int, help="parallel OCR processes (default: CPU count - 1)")
    p.add_argument("--force-ocr", action="store_true",
                   help="OCR every page even if it already has a text layer, and re-index everything")
    p.add_argument("--trigram", action="store_true",
                   help="use the trigram tokenizer (substring matching; recommended for Chinese/Japanese). "
                        "Only takes effect when the database is first created.")
    p.add_argument("--searchable-dir", metavar="DIR",
                   help="also write a copy of each PDF with an invisible OCR text layer into DIR "
                        "(same sub-folder layout), so Ctrl+F works in any PDF viewer. "
                        "Originals are never modified.")
    p.add_argument("--no-prune", action="store_true", help="keep index entries for PDFs that no longer exist")

    p = sub.add_parser("search", help="search the index from the command line")
    p.add_argument("query", nargs="+")
    p.add_argument("-n", "--limit", type=int, default=20)
    p.add_argument("--raw", action="store_true", help="pass the query to SQLite FTS5 unchanged")

    p = sub.add_parser("serve", help="start the local web search UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    sub.add_parser("stats", help="show what is in the index")

    args = ap.parse_args(argv)

    if args.cmd == "index":
        tok = "trigram" if args.trigram else "unicode61 remove_diacritics 2"
        conn = indexer.build_index(args.folders, args.db, lang=args.lang, dpi=args.dpi,
                                   workers=args.workers, force_ocr=args.force_ocr,
                                   tokenizer=tok, prune=not args.no_prune,
                                   searchable_dir=args.searchable_dir)
        print(stats(conn))
        return 0

    if not Path(args.db).exists():
        print(f"No index at {args.db}. Run: python -m pdf_search index <folder>", file=sys.stderr)
        return 1
    conn = indexer.connect(args.db)

    if args.cmd == "search":
        q = " ".join(args.query)
        results = search(conn, q, limit=args.limit, raw=args.raw)
        if not results:
            print("No matches.")
        for r in results:
            print(f"{r['path']}  (page {r['page_no']}, {r['source']})")
            print(f"    {' '.join(r['snippet'].split())}\n")
    elif args.cmd == "serve":
        from .web import serve
        conn.close()
        serve(args.db, args.host, args.port)
    elif args.cmd == "stats":
        print(stats(conn))
    return 0


if __name__ == "__main__":
    sys.exit(main())
