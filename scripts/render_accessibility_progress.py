#!/usr/bin/env python3
"""Render reports/ACCESSIBILITY_PROGRESS.md as a static HTML page.

The markdown checklist is the source of truth. This script only draws it:
a course board in the centre (courses by id, finished ones last; done / open
rows, progress bar, top rules),
each course expanding to its chapters and topics, and the reference sections
(next actions, key findings, rule ownership) at the bottom. Nothing on the
page writes back; tick rows in the markdown file and re-render.

Usage:
    scripts/with-a11y-python.sh scripts/render_accessibility_progress.py \\
        [--src reports/ACCESSIBILITY_PROGRESS.md] [--out reports/accessibility-progress.html]
"""
from __future__ import annotations

import argparse
import html
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ITEM_RE = re.compile(
    r"^(?P<indent>\s*)- \[(?P<done>[ xX])\] (?P<kind>ch|topic) `(?P<id>[^`]*)` row (?P<row>\d+) (?P<rest>.*)$"
)
PASS_RE = re.compile(r"^- \*\*(?P<title>Chapter [^*]+)\*\* \(chapter PDF passes\)$")
DONE_NOTE_RE = re.compile(r"\((done [^)]*)\)\s*$")
TRAIL_NOTE_RE = re.compile(r"\s\(([^()]*(?:\([^()]*\)[^()]*)*)\)\s*$")
DATE_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
# A row ticked because no file exists to fix, per the tracker legend.
NOFILE_RE = re.compile(r"no preview to fix", re.I)

# Reference sections in the order they appear at the bottom; the rest of the
# markdown headings are dropped from the page (the board replaces the Summary
# table).
BOTTOM_SECTIONS = ["Next actions", "Key findings", "Rule ownership and current fix status"]
OPEN_BY_DEFAULT = {"Next actions"}


def esc(s: str) -> str:
    return html.escape(s, quote=True)


def inline(s: str) -> str:
    """Escape, then convert the few inline markers the tracker uses."""
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    s = re.sub(r"\[(fork|lambda|both)\]", r'<span class="tag tag-\1">\1</span>', s)
    s = s.replace(" (in Aug 25 map)", "")
    s = s.replace("&#x27;Aug 25 map&#x27;", "Aug 25 map")
    return s


def render_prose(block: str) -> str:
    """Minimal markdown for the reference sections: paragraphs, lists, tables."""
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
    prose = {s.split("\n", 1)[0].strip(): (s.split("\n", 1)[1] if "\n" in s else "") for s in sections[1:]}
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
                    note_text = note.group(1)
                else:
                    trail = TRAIL_NOTE_RE.search(rest)
                    note_text = ""
                    if trail and DATE_RE.search(trail.group(1)):
                        rest = rest[: trail.start()].rstrip()
                        note_text = trail.group(1)
                item = {
                    "kind": m.group("kind"),
                    "id": m.group("id"),
                    "row": m.group("row"),
                    "done": m.group("done").lower() == "x",
                    "text": rest,
                    "note": note_text,
                    "nofile": bool(note_text and NOFILE_RE.search(note_text)),
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
                item = {"kind": "passes", "text": pm.group("title"), "topics": [], "done": None, "nofile": False}
                items.append(item)
                stack = [item]
                continue
            if ln.strip() and not about and not ln.startswith("-"):
                about = ln.strip()
        courses.append({"id": cid, "about": about, "items": items})
    return title, prose, courses


def summary_by_course(prose: dict) -> dict:
    """Course id -> (fail cells, top rules) from the Summary table."""
    out = {}
    for ln in prose.get("Summary", "").split("\n"):
        if not ln.startswith("|") or ln.startswith("|-") or ln.startswith("| Course"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) >= 6:
            out[cells[0].strip("`")] = (cells[4], cells[5])
    return out


def count(items):
    """(done, total, nofile): nofile rows are counted done, and also on their own."""
    done = total = nofile = 0
    for it in items:
        rows = [it] if it["kind"] != "passes" else []
        rows += it["topics"]
        for r in rows:
            total += 1
            done += r["done"]
            nofile += r["done"] and r["nofile"]
    return done, total, nofile


def last_event(about: str) -> str:
    """Latest absolute date in the course sentence, or empty."""
    dates = DATE_RE.findall(about)
    return max(dates) if dates else ""


CSS = """
:root{--bg:#f5f6f4;--panel:#fff;--panel-2:#eceeea;--ink:#1c2530;--ink-2:#4b5866;--muted:#7a8690;--line:#d9ddd6;
--accent:#1e6a72;--accent-soft:#e0eef0;--fail:#a8332a;--fail-soft:#f6e3e0;--pass:#2c6e49;--pass-soft:#e0efe6;
--lambda:#6b4fa0;--lambda-soft:#ebe4f5;--done:#98a1aa}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#151a1f;--panel:#1d242b;--panel-2:#232b33;--ink:#e6e9ec;--ink-2:#b4bcc5;--muted:#8792a0;--line:#2f3941;
--accent:#6cc3cb;--accent-soft:#183237;--fail:#f08b82;--fail-soft:#3a201d;--pass:#7fcf9c;--pass-soft:#1d3527;--lambda:#b9a2e6;--lambda-soft:#2b2340;--done:#6b7683}}
:root[data-theme="dark"]{--bg:#151a1f;--panel:#1d242b;--panel-2:#232b33;--ink:#e6e9ec;--ink-2:#b4bcc5;--muted:#8792a0;--line:#2f3941;
--accent:#6cc3cb;--accent-soft:#183237;--fail:#f08b82;--fail-soft:#3a201d;--pass:#7fcf9c;--pass-soft:#1d3527;--lambda:#b9a2e6;--lambda-soft:#2b2340;--done:#6b7683}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;margin:0;padding-block:0 4rem;padding-inline:clamp(16px,4vw,40px)}
.mono,code{font-family:ui-monospace,Menlo,Consolas,monospace}
code{font-size:.85em;background:var(--accent-soft);padding:.05em .3em;border-radius:3px}
h1,h2,h3{font-weight:600;line-height:1.2;margin:0;text-wrap:balance}
h1{font-size:clamp(1.4rem,3vw,1.9rem)}
h2{font-size:1.1rem;margin-block:2.2rem .6rem}
.page{max-width:1080px;margin:0 auto}
header{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:1rem 2rem;padding-block:2rem 1rem}
header .sub{color:var(--muted);font-size:.85rem;margin-top:.3rem}
.total{display:grid;grid-template-columns:auto 1fr;gap:.2rem .9rem;align-items:center;min-width:min(100%,360px)}
.total .big{font-size:2rem;font-weight:600;font-variant-numeric:tabular-nums;line-height:1;grid-row:span 2}
.total .cap{color:var(--muted);font-size:.8rem}
.bar{display:flex;height:6px;width:100%;background:var(--panel-2);border-radius:3px;overflow:hidden}
.bar .fill{display:block;height:100%;background:var(--pass)}
.bar .fill-na{display:block;height:100%;background:repeating-linear-gradient(135deg,var(--done) 0 2px,transparent 2px 5px)}
.controls{display:flex;flex-wrap:wrap;gap:.6rem 1.4rem;align-items:center;font-size:.85rem;color:var(--ink-2);margin-block:.4rem .8rem}
.controls label{display:inline-flex;gap:.4rem;align-items:center;cursor:pointer}
.controls .legend{display:inline-flex;flex-wrap:wrap;gap:.3rem .9rem;color:var(--muted);margin-left:auto}
.board{border:1px solid var(--line);border-radius:8px;background:var(--panel);overflow:hidden}
.board-head,.course>summary{display:grid;grid-template-columns:minmax(0,1fr) 92px minmax(120px,180px) minmax(0,1.3fr) 92px;gap:.8rem;align-items:center;padding:.6rem 1rem}
.board-head{font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;background:var(--panel-2);border-bottom:1px solid var(--line)}
.course{border-top:1px solid var(--line)} .course:first-of-type{border-top:0}
.course>summary{cursor:pointer;list-style:none;font-size:.9rem}
.course>summary::-webkit-details-marker{display:none}
.course>summary:hover{background:var(--accent-soft)}
.course>summary:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.course>summary .name{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.course>summary .name::before{content:"\\25B8";color:var(--muted);margin-right:.5rem;display:inline-block;width:.8em}
.course[open]>summary .name::before{content:"\\25BE"}
.num{color:var(--ink-2);font-variant-numeric:tabular-nums;white-space:nowrap;font-size:.85rem}
.rules{color:var(--muted);font-size:.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.when{color:var(--muted);font-size:.78rem;font-variant-numeric:tabular-nums;text-align:right}
.course.complete>summary .name{color:var(--done)} .course.complete .bar .fill{background:var(--done)}
.course-body{padding:.2rem 1rem 1rem 1rem;background:var(--bg)}
details.log{font-size:.83rem;color:var(--ink-2);margin:.4rem 0 .8rem}
details.log summary{cursor:pointer;color:var(--muted);font-size:.78rem;letter-spacing:.04em;text-transform:uppercase;font-weight:600}
details.log p{margin:.4rem 0 0;max-width:90ch}
ul.items,ul.topics{list-style:none;margin:0;padding:0}
ul.items>li{border-top:1px solid var(--line);padding:.5rem 0}
ul.items>li:first-child{border-top:0}
ul.topics{margin:.25rem 0 0 1.4rem;border-left:2px solid var(--line);padding-left:.8rem}
li.topic{padding:.25rem 0}
.item .head{display:flex;align-items:baseline;gap:.5rem;flex-wrap:wrap}
.item .box{font-family:ui-monospace,Menlo,monospace;color:var(--muted);flex:none;font-size:.85rem}
.item.done .box{color:var(--pass)}
.chapter .ttl{font-weight:600}
code.id{font-size:.72rem;color:var(--muted);background:var(--panel-2);user-select:all;white-space:nowrap}
.item .fails{margin:.1rem 0 0 1.5rem;line-height:1.7;font-size:.84rem;color:var(--ink-2)}
.item .note{margin-left:1.5rem;font-size:.76rem;color:var(--muted)}
.item.done .note{color:var(--pass)}
.item.done .ttl{color:var(--done);text-decoration:line-through} .item.done .fails{opacity:.5}
.item.nofile .box,.item.nofile .note{color:var(--muted)}
.item.nofile .ttl{text-decoration:none;font-style:italic}
.tag{display:inline-block;font:500 .64rem/1 ui-monospace,Menlo,monospace;letter-spacing:.03em;padding:.22em .4em;border-radius:3px;vertical-align:middle;margin-left:.3em}
.tag-fork{background:var(--accent-soft);color:var(--accent)} .tag-lambda{background:var(--lambda-soft);color:var(--lambda)}
.tag-both{background:var(--fail-soft);color:var(--fail)}
.lbl{font-size:.74rem;font-weight:600} .lbl-fail{color:var(--fail)} .lbl-pass{color:var(--pass)}
.lbl-nofile{color:var(--muted);font-weight:500;border:1px dashed var(--line);border-radius:3px;padding:.05em .35em}
body.hide-done ul.topics>li.done,body.hide-done ul.items>li>.item.done,body.hide-done .course.complete{display:none}
body.hide-done ul.items>li.all-done{display:none}
.ref{margin-top:2.5rem;border-top:1px solid var(--line);padding-top:.5rem}
.ref details{border:1px solid var(--line);border-radius:8px;background:var(--panel);margin-block:.7rem;padding:0 1.1rem}
.ref details summary{cursor:pointer;padding:.75rem 0;font-weight:600;list-style:none;display:flex;align-items:center;gap:.5rem}
.ref details summary::-webkit-details-marker{display:none}
.ref details summary::before{content:"\\25B8";color:var(--muted);display:inline-block;width:.8em}
.ref details[open] summary::before{content:"\\25BE"}
.ref .body{padding-bottom:1rem;color:var(--ink-2);font-size:.9rem}
.ref p,.ref li{max-width:85ch} .ref ol,.ref ul.prose{padding-left:1.2rem} .ref li{margin-block:.4rem}
.table-wrap{overflow-x:auto;margin-block:.6rem;border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%;font-size:.84rem}
th,td{padding:.5rem .7rem;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
th{font-size:.7rem;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);font-weight:600;background:var(--panel-2)}
tr:last-child td{border-bottom:0}
footer{color:var(--muted);font-size:.78rem;margin-top:2rem}
@media(max-width:760px){
 .board-head{display:none}
 .course>summary{grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"name num" "bar bar" "rules when"}
 .course>summary .name{grid-area:name} .course>summary .num{grid-area:num} .course>summary .bar{grid-area:bar}
 .course>summary .rules{grid-area:rules;white-space:normal} .course>summary .when{grid-area:when}
}
"""

JS = """
(function(){
 var key='a11y-progress-hide-done', box=document.getElementById('hide-done');
 function apply(){document.body.classList.toggle('hide-done',box.checked);try{localStorage.setItem(key,box.checked?'1':'0')}catch(e){}}
 try{box.checked=localStorage.getItem(key)==='1'}catch(e){}
 box.addEventListener('change',apply);apply();
 var open=document.getElementById('open-all'),close=document.getElementById('close-all');
 open.addEventListener('click',function(){document.querySelectorAll('details.course').forEach(function(d){d.open=true})});
 close.addEventListener('click',function(){document.querySelectorAll('details.course').forEach(function(d){d.open=false})});
})();
"""


def render_item(it, cls):
    box = "[-]" if it["nofile"] else ("[x]" if it["done"] else "[ ]")
    label, _, fails = it["text"].partition(": ") if it["kind"] == "topic" else it["text"].partition(" chapter PDF fails: ")
    fails_html = inline(fails)
    if it["kind"] == "ch":
        fails_html = '<span class="lbl lbl-fail">chapter PDF fails</span> ' + fails_html
    note = f'<div class="note">{esc(it["note"])}</div>' if it["note"] else ""
    chip = '<span class="lbl lbl-nofile" title="No file exists to fix, so the row counts as finished">no preview to fix</span>' if it["nofile"] else ""
    return (
        f'<div class="item {cls}{" done" if it["done"] else ""}{" nofile" if it["nofile"] else ""}">'
        f'<div class="head"><span class="box">{box}</span>'
        f'<span class="ttl">{inline(label)}</span>'
        f'<code class="id" title="{"chapter" if it["kind"] == "ch" else "topic"} id, workbook row {it["row"]}">{esc(it["id"])}</code>{chip}</div>'
        f'<div class="fails">{fails_html}</div>{note}</div>'
    )


def render_course(c, summary):
    cid = c["id"]
    done, total, nofile = count(c["items"])
    pct = f"{100 * (done - nofile) / total if total else 0:.0f}%"
    na_pct = f"{100 * nofile / total if total else 0:.0f}%"
    complete = " complete" if total and done == total else ""
    num_title = f' title="{nofile} of the {done} counted finished have no preview to fix"' if nofile else ""
    na_bar = f'<span class="fill-na" style="width:{na_pct}" title="no preview to fix"></span>' if nofile else ""
    fail_cells, top_rules = summary.get(cid, ("", ""))
    rows = []
    for it in c["items"]:
        all_done = (it["kind"] == "passes" or it["done"]) and all(t["done"] for t in it["topics"])
        rows.append(f'<li class="{"all-done" if all_done else ""}">')
        if it["kind"] == "passes":
            rows.append(f'<div class="item chapter"><div class="head"><span class="ttl">{inline(it["text"])}</span><span class="lbl lbl-pass">chapter PDF passes</span></div></div>')
        else:
            rows.append(render_item(it, "chapter"))
        if it["topics"]:
            rows.append('<ul class="topics">' + "".join(f'<li class="topic{" done" if t["done"] else ""}">' + render_item(t, "topic") + "</li>" for t in it["topics"]) + "</ul>")
        rows.append("</li>")
    log = f'<details class="log"><summary>Course log</summary><p>{inline(c["about"])}</p></details>' if c["about"] else ""
    return (
        f'<details class="course{complete}" id="c-{cid}"><summary>'
        f'<span class="name mono">{esc(cid)}</span>'
        f'<span class="num"{num_title}>{done} / {total}{" *" if nofile else ""}</span>'
        f'<span class="bar"><span class="fill" style="width:{pct}"></span>'
        f'{na_bar}</span>'
        f'<span class="rules" title="{esc(top_rules)}">{esc(top_rules)}</span>'
        f'<span class="when">{esc(last_event(c["about"]))}</span>'
        f'</summary><div class="course-body">{log}<ul class="items">{"".join(rows)}</ul></div></details>'
    )


def render(md: str, src_label: str) -> str:
    title, prose, courses = parse(md)
    summary = summary_by_course(prose)
    grand_done = grand_total = grand_nofile = 0
    scored = []
    for c in courses:
        done, total, nofile = count(c["items"])
        grand_done += done
        grand_total += total
        grand_nofile += nofile
        scored.append((total - done, c))
    # Courses in id order; finished courses sink to the bottom, also in id order.
    scored.sort(key=lambda x: (x[0] == 0, x[1]["id"]))
    board = "".join(render_course(c, summary) for _, c in scored)
    open_courses = sum(1 for n, _ in scored if n)
    gpct = f"{100 * grand_done / grand_total if grand_total else 0:.0f}%"
    na_cap = f" ({grand_nofile} with no preview to fix)" if grand_nofile else ""
    ref = []
    for name in BOTTOM_SECTIONS:
        if name not in prose:
            continue
        ref.append(
            f'<details{" open" if name in OPEN_BY_DEFAULT else ""}><summary>{esc(name)}</summary>'
            f'<div class="body">{render_prose(prose[name])}</div></details>'
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}</style></head><body>
<div class="page">
<header>
 <div><h1>{esc(title)}</h1><div class="sub">Rendered {date.today().isoformat()} from {esc(src_label)}. Read only: tick rows in the markdown and re-render.</div></div>
 <div class="total"><span class="big">{gpct}</span><span class="cap">{grand_done} of {grand_total} rows done{na_cap}, {open_courses} of {len(courses)} courses still open</span><span class="bar"><span class="fill" style="width:{gpct}"></span></span></div>
</header>
<div class="controls">
 <label><input type="checkbox" id="hide-done"> Hide done rows</label>
 <button type="button" id="open-all">Expand all</button><button type="button" id="close-all">Collapse all</button>
 <span class="legend"><span><span class="mono">[-]</span> no preview to fix, counted finished</span><span><span class="tag tag-fork">fork</span> topic fix</span><span><span class="tag tag-lambda">lambda</span> chapter merge fix</span><span><span class="tag tag-both">both</span> topic fix plus re-wrap</span></span>
</div>
<div class="board">
 <div class="board-head"><span>Course</span><span>Done / rows</span><span>Progress</span><span>Top failing rules</span><span style="text-align:right">Last event</span></div>
 {board}
</div>
<section class="ref">
{"".join(ref)}
</section>
<footer>Row counts are workbook rows with at least one FAIL. Ids are chapter or topic ids from the course JSON on dev; hover an id for its workbook row.</footer>
</div>
<script>{JS}</script>
</body></html>
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
