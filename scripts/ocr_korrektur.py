#!/usr/bin/env python3
"""Correct the training pages in the browser: the page image on the left, the draft (``<Seite>.entwurf.txt``) on the
right to edit, the page type to choose – "Speichern & weiter" writes ``<Seite>.soll.txt`` in the target form
(ocr_vorgabe.py). Runs on this computer only (127.0.0.1), standard library only; the pages stay here.

    python ocr_korrektur.py --ordner training            # then open http://127.0.0.1:8765
    python ocr_korrektur.py --ordner training --export training.jsonl

``--export`` writes the corrected pages as training data (one JSON line per page: the image, the prompt, the target
answer – the "messages" + "images" layout LLaMA-Factory and ms-swift read). Pages marked "nicht verwenden" are left out.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ocr_vergleich import MIME, pages_in  # noqa: E402
from ocr_vorgabe import PROMPT, TYPES, join, split  # noqa: E402

SKIPPED = ".nicht-verwenden"


def _side(page: Path, suffix: str) -> Path:
    return page.with_name(page.stem + suffix)


def state(page: Path) -> str:
    if _side(page, SKIPPED).exists():
        return "verworfen"
    return "korrigiert" if _side(page, ".soll.txt").exists() else "offen"


def current(page: Path) -> tuple[str, str]:
    """(type, text) to show: the correction if there is one, else the draft, else an empty text page."""
    for suffix in (".soll.txt", ".entwurf.txt"):
        f = _side(page, suffix)
        if f.exists():
            page_type, text = split(f.read_text(encoding="utf-8"))
            return page_type or "Text", text
    return "Text", ""


def save(page: Path, page_type: str, text: str) -> None:
    if page_type not in TYPES:
        raise ValueError(page_type)
    _side(page, SKIPPED).unlink(missing_ok=True)
    _side(page, ".soll.txt").write_text(join(page_type, text.replace("\r\n", "\n")), encoding="utf-8")


def discard(page: Path) -> None:
    _side(page, ".soll.txt").unlink(missing_ok=True)
    _side(page, SKIPPED).write_text("", encoding="utf-8")


def export(folder: Path, target: Path) -> dict:
    counts = {t: 0 for t in TYPES}
    with target.open("w", encoding="utf-8") as out:
        for page in pages_in(folder):
            if state(page) != "korrigiert":
                continue
            answer = _side(page, ".soll.txt").read_text(encoding="utf-8").strip()
            page_type, _text = split(answer)
            counts[page_type or "Text"] += 1
            out.write(json.dumps({"messages": [{"role": "user", "content": "<image>" + PROMPT},
                                               {"role": "assistant", "content": answer}],
                                  "images": [str(page.resolve())]}, ensure_ascii=False) + "\n")
    return counts


_STYLE = ("body{font:14px system-ui,sans-serif;margin:0}header{padding:.5rem 1rem;background:#f3f4f6;display:flex;"
          "gap:1rem;align-items:center;flex-wrap:wrap}main{display:grid;grid-template-columns:1fr 1fr;gap:1rem;"
          "padding:1rem}img{max-width:100%;border:1px solid #ccc}textarea{width:100%;height:70vh;font:13px "
          "ui-monospace,monospace}.typ label{margin-right:1rem}button{padding:.4rem .9rem;margin-right:.4rem}"
          ".offen{color:#888}.korrigiert{color:#176}.verworfen{color:#a40}details{margin-top:.6rem}"
          "pre{white-space:pre-wrap;max-height:20rem;overflow:auto;background:#fafafa;padding:.4rem}")


class Tool:
    def __init__(self, folder: Path):
        self.folder = folder
        self.pages = pages_in(folder)

    def next_open(self, after: int = -1) -> int:
        n = len(self.pages)
        for k in range(1, n + 1):
            i = (after + k) % n
            if state(self.pages[i]) == "offen":
                return i
        return min(after + 1, n - 1)

    def page_html(self, i: int) -> str:
        page = self.pages[i]
        page_type, text = current(page)
        done = sum(state(p) == "korrigiert" for p in self.pages)
        known = _side(page, ".tesseract.txt")
        radios = "".join(
            f"<label><input type='radio' name='typ' value='{t}' accesskey='{k}'{' checked' if t == page_type else ''}>"
            f" {t}</label>" for k, t in enumerate(TYPES, start=1))
        return (f"<!doctype html><meta charset='utf-8'><title>{i + 1}/{len(self.pages)} {html.escape(page.name)}</title>"
                f"<style>{_STYLE}</style><header><b>{done} von {len(self.pages)} korrigiert</b>"
                f"<span>Seite {i + 1}: {html.escape(page.name)} – <span class='{state(page)}'>{state(page)}</span></span>"
                f"<a href='/seite/{max(0, i - 1)}'>← zurück</a><a href='/seite/{min(len(self.pages) - 1, i + 1)}'>"
                f"weiter →</a><a href='/'>nächste offene</a></header><main><div><img src='/bild/{i}' alt=''></div>"
                f"<form method='post' action='/seite/{i}'><div class='typ'>Typ: {radios}</div>"
                f"<textarea name='text' autofocus>{html.escape(text)}</textarea><div>"
                f"<button name='aktion' value='speichern'>Speichern &amp; weiter (Strg+Enter)</button>"
                f"<button name='aktion' value='verwerfen' formnovalidate>nicht verwenden</button></div>"
                f"<details><summary>Text bisher (Textebene/Tesseract)</summary><pre>"
                f"{html.escape(known.read_text(encoding='utf-8') if known.exists() else '–')}</pre></details>"
                f"<details><summary>Regeln</summary><pre>{html.escape(PROMPT)}</pre></details></form></main>"
                "<script>document.addEventListener('keydown',e=>{if(e.ctrlKey&&e.key==='Enter'){"
                "document.querySelector(\"button[value=speichern]\").click()}})</script>")


def handler(tool: Tool):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _go(self, i: int) -> None:
            self.send_response(303)
            self.send_header("Location", f"/seite/{i}")
            self.end_headers()

        def _index(self, part: str) -> int | None:
            try:
                i = int(part)
            except ValueError:
                return None
            return i if 0 <= i < len(tool.pages) else None

        def do_GET(self):  # noqa: N802
            parts = self.path.split("?")[0].strip("/").split("/")
            if parts == [""]:
                return self._go(tool.next_open())
            if len(parts) == 2 and (i := self._index(parts[1])) is not None:
                if parts[0] == "seite":
                    return self._send(200, tool.page_html(i).encode("utf-8"))
                if parts[0] == "bild":
                    page = tool.pages[i]
                    return self._send(200, page.read_bytes(), MIME[page.suffix.lower()])
            self._send(404, b"nicht gefunden")

        def do_POST(self):  # noqa: N802
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "seite" or (i := self._index(parts[1])) is None:
                return self._send(404, b"nicht gefunden")
            form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8"),
                                         keep_blank_values=True)
            if (form.get("aktion") or [""])[0] == "verwerfen":
                discard(tool.pages[i])
            else:
                save(tool.pages[i], (form.get("typ") or ["Text"])[0], (form.get("text") or [""])[0])
            self._go(tool.next_open(i))

    return H


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Trainingsseiten im Browser korrigieren")
    ap.add_argument("--ordner", required=True)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--export", help="korrigierte Seiten als Trainingsdaten (JSONL) in diese Datei schreiben")
    args = ap.parse_args(argv)
    folder = Path(args.ordner)
    if args.export:
        counts = export(folder, Path(args.export))
        print(f"{sum(counts.values())} Seiten nach {args.export}: " + ", ".join(f"{t} {n}" for t, n in counts.items()))
        return 0
    tool = Tool(folder)
    if not tool.pages:
        raise SystemExit(f"Keine Seitenbilder in {folder}")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(tool))
    print(f"{len(tool.pages)} Seiten – im Browser öffnen: http://127.0.0.1:{args.port}  (beenden mit Strg+C)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
