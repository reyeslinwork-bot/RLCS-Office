"""A small local web UI for searching the index (standard library only)."""

import html
import json
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote

from .search import search, stats

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Document Search</title>
<style>
  :root {{ --bg:#fafafa; --fg:#1d1d1f; --muted:#6b6b70; --card:#fff; --line:#e3e3e8; --hl:#ffe58a; --accent:#2457d6; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#141416; --fg:#ececf0; --muted:#9a9aa3; --card:#1d1d21; --line:#2e2e34; --hl:#6b5a12; --accent:#7ea2ff; }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:15px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }}
  main {{ max-width:860px; margin:0 auto; padding:32px 16px; }}
  h1 {{ font-size:22px; margin:0 0 4px; }}
  .muted {{ color:var(--muted); font-size:13px; }}
  form {{ display:flex; gap:8px; margin:20px 0; }}
  input[type=search] {{ flex:1; padding:10px 12px; font-size:16px; border:1px solid var(--line);
    border-radius:8px; background:var(--card); color:var(--fg); }}
  button {{ padding:10px 18px; font-size:15px; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }}
  .hit {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:10px; }}
  .hit a {{ color:var(--accent); font-weight:600; text-decoration:none; word-break:break-all; }}
  .hit p {{ margin:6px 0 0; }}
  mark {{ background:var(--hl); color:inherit; padding:0 2px; border-radius:3px; }}
  .tag {{ font-size:11px; border:1px solid var(--line); border-radius:4px; padding:1px 5px; margin-left:6px; color:var(--muted); }}
</style></head>
<body><main>
<h1>Document Search</h1>
<div class="muted">{stats}</div>
<form method="get" action="/"><input type="search" name="q" value="{q}" autofocus
  placeholder='Search… e.g. invoice 2023, "purchase order", tax OR vat'><button>Search</button></form>
{body}
</main></body></html>"""


def render_results(q, results):
    if not q:
        return '<p class="muted">Tip: use "quotes" for exact phrases, OR for alternatives, NOT to exclude.</p>'
    if not results:
        return f"<p>No matches for <b>{html.escape(q)}</b>.</p>"
    out = [f'<p class="muted">{len(results)} matching page(s)</p>']
    for r in results:
        snippet = html.escape(r["snippet"]).replace("[[", "<mark>").replace("]]", "</mark>")
        name = Path(r["path"]).name
        href = f'/pdf/{r["doc_id"]}#page={r["page_no"]}&amp;search={quote(q)}'
        out.append(
            f'<div class="hit"><a href="{href}" target="_blank">{html.escape(name)}</a>'
            f' <span class="muted">page {r["page_no"]}</span>'
            f'<span class="tag">{r["source"]}</span>'
            f'<div class="muted">{html.escape(r["path"])}</div><p>{snippet}</p></div>')
    return "\n".join(out)


def make_handler(db_path):
    class Handler(BaseHTTPRequestHandler):
        def _conn(self):
            return sqlite3.connect(db_path)

        def do_GET(self):
            url = urlparse(self.path)
            params = parse_qs(url.query)
            q = params.get("q", [""])[0].strip()
            try:
                if url.path == "/":
                    with self._conn() as conn:
                        results = search(conn, q, limit=100) if q else []
                        s = stats(conn)
                    info = f'{s["documents"]} documents · {s["pages"]} pages ({s["ocr_pages"]} OCR\'d)'
                    page = PAGE.format(stats=info, q=html.escape(q), body=render_results(q, results))
                    self._send(200, "text/html; charset=utf-8", page.encode())
                elif url.path == "/api/search":
                    with self._conn() as conn:
                        results = search(conn, q, limit=int(params.get("limit", ["50"])[0]))
                    self._send(200, "application/json", json.dumps(results, ensure_ascii=False).encode())
                elif url.path.startswith("/pdf/"):
                    doc_id = int(url.path.split("/")[2])
                    with self._conn() as conn:
                        row = conn.execute("SELECT path FROM documents WHERE id=?", (doc_id,)).fetchone()
                    if not row or not Path(row[0]).is_file():
                        return self._send(404, "text/plain", b"Not found")
                    # Only files registered in the index can be served.
                    self._send(200, "application/pdf", Path(row[0]).read_bytes())
                else:
                    self._send(404, "text/plain", b"Not found")
            except sqlite3.OperationalError as e:
                self._send(400, "text/plain; charset=utf-8", f"Bad query: {e}".encode())
            except ValueError:
                self._send(400, "text/plain", b"Bad request")

        def _send(self, code, ctype, body):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(db_path, host="127.0.0.1", port=8000):
    httpd = HTTPServer((host, port), make_handler(db_path))
    print(f"Serving search UI on http://{host}:{port}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
