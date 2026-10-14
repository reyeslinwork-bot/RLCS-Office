# RLCS-Office — Scanned PDF Search

Search the text inside scanned PDF documents. Every page is OCR'd once with
Tesseract and stored in a local SQLite full-text index. After that, searches
are instant and show the file, the page number and a highlighted snippet.

- Pages that already have a text layer (digital PDFs, or scans that were OCR'd before) are read directly. Only image-only pages are OCR'd.
- Re-running `index` only processes new or changed files, and drops files that were deleted.
- You can search from the command line or from a local web page. Clicking a result opens the PDF at the matching page.

## Setup

1. Install Tesseract OCR, plus the language packs you need:
   - **Windows:** download the installer from https://github.com/UB-Mannheim/tesseract/wiki, select extra languages during the install, and add `C:\Program Files\Tesseract-OCR` to your `PATH`.
   - **macOS:** `brew install tesseract tesseract-lang`
   - **Ubuntu/Debian:** `sudo apt install tesseract-ocr tesseract-ocr-chi-sim` (and other `tesseract-ocr-<lang>` packages as needed)
2. Install the Python packages (Python 3.9+):
   ```
   pip install -r requirements.txt
   ```

## Usage

```bash
# 1. Build the index (OCR runs here; it takes a while the first time)
python -m pdf_search index "D:/Scans" "D:/Archive/2023"

# Documents not in English? Name the Tesseract language(s):
python -m pdf_search index "D:/Scans" --lang eng+chi_tra

# 2a. Search from the terminal
python -m pdf_search search invoice acme
python -m pdf_search search '"purchase order" 2023'

# 2b. Or open the web UI at http://127.0.0.1:8000
python -m pdf_search serve
```

Re-run the same `index` command whenever you add more scans.

### Search syntax

| Query                  | Matches pages containing                         |
|------------------------|--------------------------------------------------|
| `invoice acme`         | both words (word prefixes count: `invoic` finds `invoice`) |
| `"purchase order"`     | the exact phrase                                 |
| `tax OR vat`           | either word                                      |
| `invoice NOT draft`    | `invoice` but not `draft`                        |

### Options

| Option | What it does |
|--------|--------------|
| `--db FILE` | Where the index is stored (default `pdf_index.db`). Put it before the command, e.g. `python -m pdf_search --db office.db search ...` |
| `--lang` | Tesseract language codes, e.g. `eng`, `chi_sim`, `chi_tra`, `eng+chi_sim` |
| `--trigram` | Substring matching. Use it for Chinese/Japanese text, which has no spaces between words. It applies only when a new database is created. |
| `--dpi` | OCR resolution (default 300). Raise it for small print; lower it for speed. |
| `--workers` | Number of parallel OCR processes (default: CPU cores − 1) |
| `--force-ocr` | Re-OCR every page, even pages that already have text |

There is also a JSON API at `/api/search?q=...` if you want to connect the
index to another app.

## Tips for better OCR

- Scan at 300 DPI or more, in grayscale.
- Straight, high-contrast pages give the best results.
- OCR is never perfect. If an exact word finds nothing, try a shorter prefix or a different word from the same document.
