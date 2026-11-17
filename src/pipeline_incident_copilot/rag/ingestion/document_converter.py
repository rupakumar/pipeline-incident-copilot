"""Turn an uploaded company document into Markdown text, keeping its headings so the section chunker can use them.

PDF (pypdf; one "## Page N" section per page), Word .docx (python-docx; Heading styles → #), Markdown / text
(as is), HTML (headings kept, tags dropped). Anything else is refused with a reason a person can act on.
"""

import io
import re
from html.parser import HTMLParser

ALLOWED = {".md", ".markdown", ".txt", ".pdf", ".docx", ".html", ".htm"}
MAX_BYTES = 20 * 1024 * 1024


class UnsupportedDocument(ValueError):
    pass


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise UnsupportedDocument("the PDF is password-protected; upload an unlocked copy")
    pages = [(i, (p.extract_text() or "").strip()) for i, p in enumerate(reader.pages, 1)]
    text = "\n\n".join(f"## Page {i}\n\n{t}" for i, t in pages if t)
    if not text:
        raise UnsupportedDocument("no text found in the PDF (a scan?); upload a text PDF or the source document")
    return text


def _docx(data: bytes) -> str:
    import docx

    out = []
    for para in docx.Document(io.BytesIO(data)).paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower() if para.style else ""
        level = re.match(r"heading (\d)", style)
        if style == "title":
            out.append(f"# {text}")
        elif level:
            out.append(f"{'#' * min(int(level.group(1)) + 1, 3)} {text}")
        elif "list" in style:
            out.append(f"- {text}")
        else:
            out.append(text)
    return "\n\n".join(out)


class _HtmlText(HTMLParser):
    HEADINGS = {"h1": "#", "h2": "##", "h3": "###", "h4": "###"}
    SKIP = {"script", "style", "nav", "footer"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.HEADINGS:
            self.parts.append(f"\n\n{self.HEADINGS[tag]} ")
        elif tag in ("p", "li", "br", "tr", "div"):
            self.parts.append("\n\n" if tag != "li" else "\n- ")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.HEADINGS:
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def _html(data: bytes) -> str:
    parser = _HtmlText()
    parser.feed(data.decode("utf-8", errors="replace"))
    return re.sub(r"\n{3,}", "\n\n", "".join(parser.parts))


def to_markdown(filename: str, data: bytes) -> str:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in ALLOWED:
        raise UnsupportedDocument(f"{ext or 'this file type'} is not supported; use PDF, Word, Markdown, text or HTML")
    if len(data) > MAX_BYTES:
        raise UnsupportedDocument("the file is over 20 MB; split it or upload the relevant part")
    try:
        if ext == ".pdf":
            text = _pdf(data)
        elif ext == ".docx":
            text = _docx(data)
        elif ext in (".html", ".htm"):
            text = _html(data)
        else:
            text = data.decode("utf-8", errors="replace")
    except UnsupportedDocument:
        raise
    except Exception as e:  # noqa: BLE001 — a damaged file: say so instead of failing the page
        raise UnsupportedDocument(f"could not read the file ({e.__class__.__name__}); is it damaged?") from e
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    if len(text) < 40:
        raise UnsupportedDocument("the document has almost no text to learn from")
    return text
