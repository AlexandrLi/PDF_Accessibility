#!/usr/bin/env python3
"""Render reports/ACCESSIBILITY_PROGRESS.md as a static HTML page.

The markdown checklist is the source of truth. This script only draws it:
progress bars per course, ids next to every row, done rows struck through.
Nothing on the page writes back; tick rows in the markdown file.

Usage:
    scripts/with-a11y-python.sh scripts/render_accessibility_progress.py \
        [--src reports/ACCESSIBILITY_PROGRESS.md] [--out reports/accessibility-progress.html]
"""
from __future__ import annotations

import argparse
import html
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ITEM_RE = re.compile(
    r"^(?P<indent>\s*)- \[(?P<done>[ xX])\] (?P<kind>ch|topic) `(?P<id>[^`]*)` row (?P<row>\d+) (?P<rest>.*)$"
)
PASS_RE = re.compile(r"^- \*\*(?P<title>Chapter [^*]+)\*\* \(chapter PDF passes\)$")
DONE_NOTE_RE = re.compile(r"\((done [^)]*)\)\s*$")


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def inline(s: str) -> str:
    """Escape, then convert the few inline markers the tracker uses."""
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[(fork|lambda|both)\]", r'<span class="tag tag-\1">\1</span>', s)
    s = s.replace("(in Aug 25 map)", '<span class="tag tag-map">Aug 25 map</span>')
    s = s.replace("&#x27;Aug 25 map&#x27;", '<span class="tag tag-map">Aug 25 map</span>')
    return s


def render_prose(block: str) -> str:
    """Minimal markdown for the sections above 'Per course': paragraphs, lists, tables."""
    out: list[str] = []
    lines = block.strip("\n").split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not ln.strip() or ln.startswith("Legend for the tag"):
            i += 1
            continue
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1
            head, body = rows[0], [r for r in rows[2:]]
            out.append('<div class="table-wrap"><table><thead><tr>' + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr></thead><tbody>")
            for r in body:
                out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            out.append("</tbody></table></div>")
            continue
        if re.match(r"^\d+\. ", ln):
            out.append("<ol>")
            while i < len(lines) and re.match(r"^\d+\. ", lines[i]):
                out.append(f"<li>{inline(re.sub(r'^\d+\. ', '', lines[i]))}</li>")
                i += 1
            out.append("</ol>")
            continue
        if ln.startswith("- "):
            out.append('<ul class="prose">')
            while i < len(lines) and lines[i].startswith("- "):
                out.append(f"<li>{inline(lines[i][2:])}</li>")
                i += 1
            out.append("</ul>")
            continue
        out.append(f"<p>{inline(ln)}</p>")
        i += 1
    return "\n".join(out)


def parse(md: str):
    head, _, per = md.partition("\n## Per course\n")
    title_line, _, head_rest = head.partition("\n")
    title = title_line.lstrip("# ").strip()
    sections = re.split(r"\n## ", "\n" + head_rest)
    intro = sections[0].strip()
    prose = [(s.split("\n", 1)[0].strip(), s.split("\n", 1)[1] if "\n" in s else "") for s in sections[1:]]
    courses = []
    for chunk in re.split(r"\n### `", per)[1:]:
        cid, _, body = chunk.partition("`\n")
        about = ""
        items = []
        stack: list[dict] = []
        for ln in body.split("\n"):
            m = ITEM_RE.match(ln)
            if m:
                rest = m.group("rest")
                note = DONE_NOTE_RE.search(rest)
                if note:
                    rest = rest[: note.start()].rstrip()
                item = {
                    "kind": m.group("kind"),
                    "id": m.group("id"),
                    "row": m.group("row"),
                    "done": m.group("done").lower() == "x",
                    "text": rest,
                    "note": note.group(1) if note else "",
                    "topics": [],
                }
                if m.group("indent"):
                    if stack:
                        stack[-1]["topics"].append(item)
                    else:
                        items.append(item)
                else:
                    items.append(item)
                    stack = [item]
                continue
            pm = PASS_RE.match(ln)
            if pm:
                item = {"kind": "passes", "text": pm.group("title"), "topics": [], "done": None}
                items.append(item)
                stack = [item]
                continue
            if ln.strip() and not about and not ln.startswith("-"):
                about = ln.strip()
        courses.append({"id": cid, "about": about, "items": items})
    return title, intro, prose, courses


def count(items):
    done = total = 0
    for it in items:
        if it["kind"] != "passes":
            total += 1
            done += it["done"]
        for t in it["topics"]:
            total += 1
            done += t["done"]
    return done, total


CSS = """
:root{--bg:#f4f6f3;--panel:#fff;--panel-2:#eef1ec;--ink:#1d2733;--ink-2:#4c5a68;--muted:#75838f;--line:#d7dcd5;
--accent:#1e6a72;--accent-soft:#dfeef0;--fail:#a8332a;--fail-soft:#f6e3e0;--pass:#2c6e49;--pass-soft:#e0efe6;
--lambda:#6b4fa0;--lambda-soft:#ebe4f5;--map:#7a6a2f;--map-soft:#f1ecd8;--done:#8a949e}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151a1f;--panel:#1d242b;--panel-2:#232b33;--ink:#e6e9ec;--ink-2:#b4bcc5;--muted:#8792a0;--line:#2f3941;
--accent:#6cc3cb;--accent-soft:#183237;--fail:#f08b82;--fail-soft:#3a201d;--pass:#7fcf9c;--pass-soft:#1d3527;--lambda:#b9a2e6;--lambda-soft:#2b2340;--map:#d9c46f;--map-soft:#33301d;--done:#6b7683}}
:root[data-theme="dark"]{--bg:#151a1f;--panel:#1d242b;--panel-2:#232b33;--ink:#e6e9ec;--ink-2:#b4bcc5;--muted:#8792a0;--line:#2f3941;
--accent:#6cc3cb;--accent-soft:#183237;--fail:#f08b82;--fail-soft:#3a201d;--pass:#7fcf9c;--pass-soft:#1d3527;--lambda:#b9a2e6;--lambda-soft:#2b2340;--map:#d9c46f;--map-soft:#33301d;--done:#6b7683}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font:15px/1.55 "IBM Plex Sans",system-ui,sans-serif;margin:0;padding-block:0 4rem;padding-inline:clamp(16px,4vw,40px)}
.mono,code{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace}
code{font-size:.85em;background:var(--accent-soft);padding:.05em .3em;border-radius:3px}
h1,h2{font-family:"Source Serif 4",Georgia,serif;font-weight:600;text-wrap:balance;line-height:1.2;margin:0}
h1{font-size:clamp(1.7rem,3.5vw,2.3rem)}
h2{font-size:1.3rem;margin-block:2.4rem .8rem;padding-top:1.2rem;border-top:1px solid var(--line)}
header{max-width:1200px;margin:0 auto;padding-block:2.2rem 1rem}
header p,main p{color:var(--ink-2);max-width:72ch;margin:.6rem 0 0}
.eyebrow{font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);font-weight:600;margin-bottom:.5rem}
.legend{display:flex;flex-wrap:wrap;gap:.4rem 1.2rem;font-size:.82rem;color:var(--ink-2);margin-top:.8rem}
.wrap{max-width:1200px;margin:0 auto;display:grid;grid-template-columns:minmax(0,1fr);gap:2rem}
@media(min-width:1000px){.wrap{grid-template-columns:minmax(0,1fr) 280px}}
main{min-width:0} main li{max-width:75ch}
ol,ul.prose{padding-left:1.2rem} ol li,ul.prose li{margin-block:.4rem}
aside{order:-1}
@media(min-width:1000px){aside{order:0;position:sticky;top:1rem;align-self:start;max-height:calc(100vh - 2rem);overflow:auto}}
aside h4{font-size:.72rem;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 .5rem;font-weight:600}
aside ul{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:2px}
@media(max-width:999px){aside ul{flex-direction:row;flex-wrap:wrap;gap:6px} aside a .bar{display:none}}
aside a{display:grid;grid-template-columns:1fr auto;gap:.1rem .6rem;padding:.35rem .5rem;border-radius:4px;color:var(--ink);text-decoration:none;font-size:.8rem}
aside a:hover,aside a:focus-visible{background:var(--accent-soft);outline:none}
aside a .bar{grid-column:1/-1}
aside a.complete .mono{color:var(--done);text-decoration:line-through}
.num{color:var(--muted);font-variant-numeric:tabular-nums;font-size:.8rem;white-space:nowrap}
.bar{display:inline-block;height:4px;width:100%;min-width:60px;background:var(--panel-2);border-radius:2px;overflow:hidden;vertical-align:middle}
.bar .fill{display:block;height:100%;background:var(--pass)}
.toolbar{display:flex;flex-wrap:wrap;gap:1rem;align-items:center;margin-block:.8rem 1rem;padding:.6rem .8rem;background:var(--panel);border:1px solid var(--line);border-radius:6px;font-size:.86rem}
.toolbar .bar{width:160px} .toolbar .src{color:var(--muted);margin-left:auto}
.table-wrap{overflow-x:auto;margin-block:.8rem 1.2rem;border:1px solid var(--line);border-radius:6px;background:var(--panel)}
table{border-collapse:collapse;width:100%;font-size:.86rem}
th,td{padding:.5rem .7rem;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
th{font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;background:var(--panel-2)}
tr:last-child td{border-bottom:0} td{font-variant-numeric:tabular-nums}
.tag{display:inline-block;font:500 .66rem/1 "IBM Plex Mono",monospace;letter-spacing:.03em;padding:.22em .4em;border-radius:3px;vertical-align:middle;margin-left:.3em}
.tag-fork{background:var(--accent-soft);color:var(--accent)} .tag-lambda{background:var(--lambda-soft);color:var(--lambda)}
.tag-both{background:var(--fail-soft);color:var(--fail)} .tag-map{background:var(--map-soft);color:var(--map)}
.lbl{font-size:.76rem;font-weight:600} .lbl-fail{color:var(--fail)} .lbl-pass{color:var(--pass)}
details.course{border:1px solid var(--line);border-radius:6px;background:var(--panel);margin-block:.6rem;padding:0 1rem}
details.course summary{cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:1rem;padding:.7rem 0;font-weight:500;list-style:none}
details.course summary::-webkit-details-marker{display:none}
details.course summary::before{content:"\\25B8";color:var(--muted);margin-right:.5rem}
details[open].course summary::before{content:"\\25BE"}
details.course summary .meta{display:flex;align-items:center;gap:.6rem} details.course summary .bar{width:110px}
details.course summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
details.course.complete summary .mono{color:var(--done)}
.about{color:var(--ink-2);font-size:.88rem;margin:.2rem 0 .8rem}
ul.items,ul.topics{list-style:none;margin:0;padding:0} ul.items{margin-bottom:1rem}
ul.items>li{border-top:1px solid var(--line);padding:.55rem 0}
ul.topics{margin:.3rem 0 0 1.6rem;border-left:2px solid var(--panel-2);padding-left:.8rem}
li.topic{padding:.3rem 0}
.item .head{display:flex;align-items:baseline;gap:.5rem;flex-wrap:wrap}
.item .box{font-family:"IBM Plex Mono",monospace;color:var(--muted);flex:none}
.item.done .box{color:var(--pass)}
.chapter .ttl{font-weight:600}
.item .row{color:var(--muted);font-size:.76rem;white-space:nowrap}
code.id{font-size:.72rem;color:var(--muted);background:var(--panel-2);user-select:all;white-space:nowrap}
.item .fails{margin:.15rem 0 0 1.45rem;line-height:1.7;font-size:.86rem;color:var(--ink-2)}
.item .note{margin-left:1.45rem;font-size:.78rem;color:var(--pass)}
.item.done .ttl{color:var(--done);text-decoration:line-through} .item.done .fails{opacity:.55}
"""


def render_item(it, cls):
    box = "[x]" if it["done"] else "[ ]"
    label, _, fails = it["text"].partition(": ") if it["kind"] == "topic" else it["text"].partition(" chapter PDF fails: ")
    fails_html = inline(fails)
    if it["kind"] == "ch":
        fails_html = '<span class="lbl lbl-fail">chapter PDF fails</span> ' + fails_html
    note = f'<div class="note">{esc(it["note"])}</div>' if it["note"] else ""
    return (
        f'<li class="item {cls}{" done" if it["done"] else ""}"><div class="head"><span class="box">{box}</span>'
        f'<span class="ttl">{inline(label)}</span><span class="row">row {it["row"]}</span>'
        f'<code class="id" title="{"chapter" if it["kind"] == "ch" else "topic"} id">{esc(it["id"])}</code></div>'
        f'<div class="fails">{fails_html}</div>{note}'
    )


def render(md: str, src_label: str) -> str:
    title, intro, prose, courses = parse(md)
    nav, body, grand_done, grand_total = [], [], 0, 0
    for c in courses:
        cid = c["id"]
        done, total = count(c["items"])
        grand_done += done
        grand_total += total
        pct = f"{100 * done / total if total else 0:.0f}%"
        complete = " complete" if total and done == total else ""
        nav.append(
            f'<li><a href="#c-{cid}" class="{complete.strip()}"><span class="mono">{esc(cid)}</span><span class="num">{done}/{total}</span>'
            f'<span class="bar"><span class="fill" style="width:{pct}"></span></span></a></li>'
        )
        rows = []
        for it in c["items"]:
            if it["kind"] == "passes":
                rows.append(f'<li class="item chapter passes"><div class="head"><span class="ttl">{inline(it["text"])}</span><span class="lbl lbl-pass">chapter PDF passes</span></div>')
            else:
                rows.append(render_item(it, "chapter"))
            if it["topics"]:
                rows.append('<ul class="topics">' + "".join(render_item(t, "topic") + "</li>" for t in it["topics"]) + "</ul>")
            rows.append("</li>")
        body.append(
            f'<details class="course{complete}" id="c-{cid}"><summary><span class="mono">{esc(cid)}</span><span class="meta"><span class="num">{done}/{total} done</span>'
            f'<span class="bar"><span class="fill" style="width:{pct}"></span></span></span></summary>'
            f'<p class="about">{inline(c["about"])}</p><ul class="items">{"".join(rows)}</ul></details>'
        )
    prose_html = "".join(f'<h2 id="{re.sub(r"[^a-z0-9]+", "-", h.lower()).strip("-")}">{esc(h)}</h2>{render_prose(b)}' for h, b in prose)
    gpct = f"{100 * grand_done / grand_total if grand_total else 0:.0f}%"
    return f"""<title>{esc(title)}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&family=Source+Serif+4:opsz,wght@8..60,500;8..60,600&display=swap">
<style>{CSS}</style>
<header>
 <div class="eyebrow">Rendered from {esc(src_label)}</div>
 <h1>{esc(title)}</h1>
 {render_prose(intro)}
 <div class="legend"><span><span class="tag tag-fork">fork</span> fix in PDF_Accessibility_fork</span><span><span class="tag tag-lambda">lambda</span> fix in generate-pdf-lambda</span><span><span class="tag tag-both">both</span> fork fix plus chapter re-wrap</span><span><span class="tag tag-map">Aug 25 map</span> already swept and pushed to dev, sheet still says FAIL</span></div>
</header>
<div class="wrap">
<main>
<div class="toolbar"><span>{grand_done} of {grand_total} rows done</span><span class="bar"><span class="fill" style="width:{gpct}"></span></span><span class="src">Read only. Tick rows in the markdown file and re-render.</span></div>
{prose_html}
<h2 id="per-course">Per course</h2>
<p class="about">Each course expands to its chapters. A chapter line lists the rules its merged chapter PDF fails, then the topics under it that fail. Chapter and topic ids sit at the end of each line.</p>
{"".join(body)}
</main>
<aside><h4>Courses, done / failing rows</h4><ul>{"".join(nav)}</ul></aside>
</div>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", default=str(ROOT / "reports" / "ACCESSIBILITY_PROGRESS.md"))
    ap.add_argument("--out", default=str(ROOT / "reports" / "accessibility-progress.html"))
    args = ap.parse_args()
    src = Path(args.src)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(src.read_text(), src.name))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
