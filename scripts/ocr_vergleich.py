#!/usr/bin/env python3
"""Compare OCR / vision models on exported page images (python -m projektakte.ocr_testseiten) – standalone, standard
library only, runs on any computer with Python 3.10+ (Windows too).

    python ocr_vergleich.py --ordner ocr-test ^
        --modell "GLM-OCR|http://localhost:8000/v1|zai-org/GLM-OCR" ^
        --modell "TeleOCR|http://localhost:8001/v1|XingChen-AGI/TeleOCR" --parallel 8

Each model is an OpenAI-compatible server (vLLM, llama.cpp, LM Studio, Ollama) on this computer. A model spec is
``Name|URL|Modell-ID[|Prompt]``; without a prompt the German OCR prompt below is used. Every page goes to every model;
written are ``ergebnis/<Name>/<Seite>.txt``, ``ergebnis/zusammenfassung.csv`` and ``ergebnis/vergleich.html`` (page
image, Tesseract's text and every model's text side by side). The summary printed at the end – time, answer tokens,
characters, loops, cut-off answers per model – holds no page text and can be passed on; the texts are real project data.
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROMPT = ("Lies den gesamten Text dieser Seite und gib ihn vollständig und wörtlich wieder, in der Reihenfolge der "
          "Seite; Tabellen als Markdown-Tabellen. Erfinde nichts, fasse nichts zusammen, beschreibe nichts – nur der "
          "Text, der auf der Seite steht.")
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".tif": "image/tiff", ".tiff": "image/tiff",
        ".webp": "image/webp"}
_WORD = re.compile(r"\S+")


def looped(text: str, repeats: int = 6, min_chars: int = 400, max_period: int = 400) -> bool:
    """A repetition loop (as projektakte.ai.repetition: a unit of words repeated in a row – six times, or three times
    for a unit of 200 characters or more – over at least 400 characters) or more than ten "[Stempel:"."""
    if len(re.findall(r"\[Stempel:", text, re.I)) > 10:
        return True
    words = [(m.start(), m.end(), m.group().lower()) for m in _WORD.finditer(text)]
    toks = [w[2] for w in words]
    n = len(toks)
    for p in range(1, min(max_period, n // 3) + 1):
        run = 0
        for i in range(p, n):
            if toks[i] != toks[i - p]:
                run = 0
                continue
            run += 1
            reps = (run + p) // p
            if reps < 3:
                continue
            span = words[i][1] - words[i - run - p + 1][0]
            if span >= min_chars and (reps >= repeats or span / reps >= 200):
                return True
    return False


def ask(url: str, model: str, prompt: str, image: bytes, mime: str, max_tokens: int, timeout: float,
        extra: dict | None = None) -> dict:
    payload = {"model": model, "max_tokens": max_tokens, "temperature": 0.0, **(extra or {}),
               "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}}]}]}
    req = urllib.request.Request(url.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"text": "", "error": str(exc)[:200], "seconds": time.monotonic() - started}
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    if isinstance(content, list):
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()
    usage = data.get("usage") or {}
    reasoning = (choice.get("message") or {}).get("reasoning_content") or (choice.get("message") or {}).get("reasoning")
    return {"text": content, "thinking": bool(reasoning), "seconds": time.monotonic() - started, "finish": choice.get("finish_reason"),
            "prompt_tokens": usage.get("prompt_tokens"), "tokens": usage.get("completion_tokens"), "error": None}


def parse_model(spec: str) -> dict:
    parts = spec.split("|")
    if len(parts) < 3:
        raise SystemExit(f"--modell „{spec}“: erwartet Name|URL|Modell-ID[|Prompt]")
    return {"name": parts[0].strip(), "url": parts[1].strip(), "model": parts[2].strip(),
            "prompt": "|".join(parts[3:]).strip() or PROMPT}


def pages_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in MIME)


def run(folder: Path, models: list[dict], *, parallel: int, max_tokens: int, timeout: float, out: Path,
        extra: dict | None = None) -> list[dict]:
    pages = pages_in(folder)
    if not pages:
        raise SystemExit(f"Keine Seitenbilder in {folder}")
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for m in models:
        target = out / re.sub(r"[^A-Za-z0-9._-]+", "_", m["name"])
        target.mkdir(parents=True, exist_ok=True)
        print(f"{m['name']}: {len(pages)} Seite(n), {parallel} gleichzeitig …", flush=True)
        started = time.monotonic()

        def one(page: Path, m=m, target=target) -> dict:
            res = ask(m["url"], m["model"], m["prompt"], page.read_bytes(), MIME[page.suffix.lower()], max_tokens,
                      timeout, extra)
            (target / f"{page.stem}.txt").write_text(res["text"], encoding="utf-8")
            return {"modell": m["name"], "seite": page.name, "sekunden": round(res["seconds"], 1),
                    "antwort_tokens": res.get("tokens"), "prompt_tokens": res.get("prompt_tokens"),
                    "zeichen": len(res["text"]), "abgeschnitten": res.get("finish") == "length",
                    "schleife": looped(res["text"]), "denkt": bool(res.get("thinking")), "fehler": res["error"] or ""}

        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            done = list(pool.map(one, pages))
        wall = time.monotonic() - started
        if any(r["denkt"] for r in done):
            print(f"  Achtung: {m['name']} denkt vor der Antwort (Reasoning) – die Antwort-Tokens zählen das mit. Wie in "
                  f"der App ausschalten, z. B. --extra-body '{{\"chat_template_kwargs\":{{\"enable_thinking\":false}}}}'",
                  flush=True)
        for r in done:
            r["laufzeit_gesamt"] = round(wall, 1)
        rows += done
    _write_csv(out / "zusammenfassung.csv", rows)
    _write_html(out / "vergleich.html", folder, pages, models, rows, out)
    return rows


def summary(rows: list[dict]) -> list[str]:
    lines = ["Modell | Seiten | Seiten/Min. | Ø s je Seite | Ø Antwort-Tokens | Ø Zeichen | Schleifen | abgeschnitten | "
             "Fehler"]
    for name in dict.fromkeys(r["modell"] for r in rows):
        rs = [r for r in rows if r["modell"] == name]
        ok = [r for r in rs if not r["fehler"]]
        toks = [r["antwort_tokens"] for r in ok if r["antwort_tokens"] is not None]
        wall = rs[0]["laufzeit_gesamt"] or 1
        lines.append(" | ".join([
            name, str(len(rs)), f"{60 * len(ok) / wall:.1f}",
            f"{sum(r['sekunden'] for r in ok) / max(1, len(ok)):.1f}",
            f"{sum(toks) / len(toks):.0f}" if toks else "–",
            f"{sum(r['zeichen'] for r in ok) / max(1, len(ok)):.0f}",
            str(sum(r["schleife"] for r in rs)), str(sum(r["abgeschnitten"] for r in rs)),
            str(sum(bool(r["fehler"]) for r in rs))]))
    return lines


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), delimiter=";")
        w.writeheader()
        w.writerows(rows)


def _write_html(path: Path, folder: Path, pages: list[Path], models: list[dict], rows: list[dict], out: Path) -> None:
    by = {(r["modell"], r["seite"]): r for r in rows}
    parts = ["<!doctype html><meta charset='utf-8'><title>OCR-Vergleich</title><style>"
             "body{font:14px system-ui,sans-serif;margin:1rem}table{border-collapse:collapse;width:100%}"
             "td,th{border:1px solid #ccc;vertical-align:top;padding:.4rem}pre{white-space:pre-wrap;margin:0;"
             "max-height:40rem;overflow:auto;font:12px ui-monospace,monospace}img{max-width:28rem}"
             ".warn{color:#a40}</style><h1>OCR-Vergleich</h1><pre>",
             html.escape("\n".join(summary(rows))), "</pre>"]
    rel = Path("..") / folder.name if folder.parent == out.parent else folder.resolve()
    for page in pages:
        parts.append(f"<h2>{html.escape(page.name)}</h2><table><tr><th>Seite</th><th>Tesseract</th>")
        parts += [f"<th>{html.escape(m['name'])}</th>" for m in models]
        tess = page.with_name(page.stem + ".tesseract.txt")
        cells = [f"<td><img src='{html.escape((rel / page.name).as_posix())}' alt=''></td>",
                 f"<td><pre>{html.escape(tess.read_text(encoding='utf-8') if tess.exists() else '–')}</pre></td>"]
        for m in models:
            r = by.get((m["name"], page.name), {})
            text = (out / re.sub(r"[^A-Za-z0-9._-]+", "_", m["name"]) / f"{page.stem}.txt")
            flags = [f for f, on in (("Schleife", r.get("schleife")), ("abgeschnitten", r.get("abgeschnitten")),
                                     (r.get("fehler"), r.get("fehler"))) if on]
            head = f"{r.get('sekunden', '–')} s · {r.get('antwort_tokens') or '–'} Tokens · {r.get('zeichen', 0)} Zeichen"
            warn = f"<div class='warn'>{html.escape(', '.join(flags))}</div>" if flags else ""
            body = html.escape(text.read_text(encoding="utf-8")) if text.exists() else ""
            cells.append(f"<td><div>{html.escape(head)}</div>{warn}<pre>{body}</pre></td>")
        parts.append("</tr><tr>" + "".join(cells) + "</tr></table>")
    path.write_text("".join(parts), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OCR-Modelle an exportierten Seitenbildern vergleichen")
    ap.add_argument("--ordner", required=True, help="Ordner mit den Seitenbildern (aus projektakte.ocr_testseiten)")
    ap.add_argument("--modell", action="append", required=True, help="Name|URL|Modell-ID[|Prompt], mehrfach")
    ap.add_argument("--parallel", type=int, default=8, help="gleichzeitige Anfragen je Modell (Voreinstellung: 8)")
    ap.add_argument("--max-tokens", type=int, default=4000, help="höchstens so viele Antwort-Tokens (4000)")
    ap.add_argument("--timeout", type=float, default=600, help="Sekunden je Seite (600)")
    ap.add_argument("--ausgabe", default=None, help="Ergebnisordner (Voreinstellung: ergebnis neben dem Ordner)")
    ap.add_argument("--extra-body", default="", help="weitere Felder der Anfrage als JSON, wie LLM_EXTRA_BODY der App, "
                    'z. B. \'{"chat_template_kwargs":{"enable_thinking":false}}\'')
    args = ap.parse_args(argv)
    folder = Path(args.ordner)
    out = Path(args.ausgabe) if args.ausgabe else folder.resolve().parent / "ergebnis"
    try:
        extra = json.loads(args.extra_body) if args.extra_body.strip() else None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--extra-body ist kein gültiges JSON: {exc}")
    rows = run(folder, [parse_model(s) for s in args.modell], parallel=args.parallel, max_tokens=args.max_tokens,
               timeout=args.timeout, out=out, extra=extra)
    print("\n".join(summary(rows)))
    print(f"\nVergleich: {out / 'vergleich.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
