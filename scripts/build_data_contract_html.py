#!/usr/bin/env python3
"""Build docs/data-contract.html from docs/data-contract.md.

Static, dependency-free renderer for the "Data Contract & Schema Specification"
document (Nobitex smart trading bot). The output is a single self-contained
HTML file -- no server, no packages, no build tooling -- suitable for opening
directly in the Freebuff Preview tab via htmlPath.

Features:
  * sticky header with document metadata + section search
  * sidebar table of contents (all numbered sections, subsections indented)
  * client-side filtering of sections by text match
  * copy-to-clipboard button on every code block
  * JSON syntax highlighting, RTL-aware paragraphs (Persian + English)
  * footer with source sha256 short hash and rebuild command

Re-run after every edit to docs/data-contract.md:
    python scripts/build_data_contract_html.py
"""

from __future__ import annotations

import hashlib
import html
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "data-contract.md"
OUT = ROOT / "docs" / "data-contract.html"

EXPECTED_SECTIONS = 102  # contract sections #1 .. #102

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
NUM_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)?)(?:[.:\s]|$)")


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

def slugify(text: str, used: set) -> str:
    m = NUM_PREFIX_RE.match(text)
    if m:
        base = "sec-" + m.group(1).replace(".", "-")
    else:
        base = re.sub(r"[^\w\u0600-\u06FF-]+", "-", text.strip().lower()).strip("-") or "sec"
    cand, i = base, 2
    while cand in used:
        cand = "%s-%d" % (base, i)
        i += 1
    used.add(cand)
    return cand


def parse(md: str):
    """Return a flat list of blocks: heading / p / ul / ol / code / hr."""
    used: set = set()
    blocks = []
    lines = md.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("<!--"):
            while i < n and "-->" not in lines[i]:
                i += 1
            i += 1  # skip HTML comments (insertion markers) entirely
            continue
        if line.startswith("```"):
            lang = line[3:].strip().lower()
            i += 1
            code = []
            while i < n and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # closing fence
            blocks.append({"kind": "code", "lang": lang or "text", "code": code})
            continue
        m = HEADING_RE.match(line)
        if m:
            blocks.append({
                "kind": "heading",
                "level": len(m.group(1)),
                "text": m.group(2).strip(),
                "id": slugify(m.group(2).strip(), used),
            })
            i += 1
            continue
        if re.fullmatch(r"-{3,}", line.strip()):
            blocks.append({"kind": "hr"})
            i += 1
            continue
        if re.match(r"^\*\s+", line):
            items = []
            while i < n and re.match(r"^\*\s+", lines[i]):
                items.append(re.sub(r"^\*\s+", "", lines[i]))
                i += 1
            blocks.append({"kind": "ul", "items": items})
            continue
        if re.match(r"^\d+\.\s+", line):
            items = []
            while i < n and re.match(r"^\d+\.\s+", lines[i]):
                items.append(re.sub(r"^\d+\.\s+", "", lines[i]))
                i += 1
            blocks.append({"kind": "ol", "items": items})
            continue
        if not line.strip():
            i += 1
            continue
        para = [line]
        i += 1
        while (i < n and lines[i].strip()
               and not lines[i].startswith("```")
               and not lines[i].startswith("<!--")
               and not HEADING_RE.match(lines[i])
               and not re.fullmatch(r"-{3,}", lines[i].strip())
               and not re.match(r"^\*\s+", lines[i])
               and not re.match(r"^\d+\.\s+", lines[i])):
            para.append(lines[i])
            i += 1
        blocks.append({"kind": "p", "lines": para})
    return blocks


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

JSON_TOKEN_RE = re.compile(
    r'("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)'
)


def highlight_json(escaped: str) -> str:
    def repl(m):
        if m.group(1):
            if m.group(2):
                return '<span class="tk-key">%s</span>%s' % (m.group(1), m.group(2))
            return '<span class="tk-str">%s</span>' % m.group(1)
        if m.group(3):
            return '<span class="tk-bool">%s</span>' % m.group(3)
        if m.group(4):
            return '<span class="tk-num">%s</span>' % m.group(4)
        return m.group(0)
    return JSON_TOKEN_RE.sub(repl, escaped)


PERSIAN_RE = re.compile(r"[\u0600-\u06FF]")


def detect_rtl(items) -> bool:
    """True if a list is Persian-dominant enough to deserve RTL bullet layout."""
    text = " ".join(items)
    fa = len(PERSIAN_RE.findall(text))
    la = len(re.findall(r"[A-Za-z]", text))
    return fa > 0 and fa >= 0.25 * (fa + la)


def inline(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               r'<a href="\2" target="_blank" rel="noopener">\1</a>', t)
    return t


def render_code(block) -> str:
    lang = block["lang"]
    raw = html.escape("\n".join(block["code"]), quote=False)
    body = highlight_json(raw) if lang == "json" else raw
    return (
        '<div class="code">'
        '<div class="code-bar"><span class="lang">%s</span>'
        '<button class="copy" type="button">Copy</button></div>'
        '<pre dir="ltr"><code>%s</code></pre></div>'
    ) % (html.escape(lang), body)


def render_blocks(blocks) -> str:
    out = []
    for b in blocks:
        k = b["kind"]
        if k == "heading":
            lvl = b["level"]
            out.append('<h%d id="%s" dir="auto">%s</h%d>'
                       % (lvl, b["id"], inline(b["text"]), lvl))
        elif k == "p":
            out.append('<p dir="auto">%s</p>' % "<br>".join(inline(x) for x in b["lines"]))
        elif k == "ul":
            d = "rtl" if detect_rtl(b["items"]) else "ltr"
            out.append('<ul dir="%s">' % d
                       + "".join("<li>%s</li>" % inline(x) for x in b["items"]) + "</ul>")
        elif k == "ol":
            d = "rtl" if detect_rtl(b["items"]) else "ltr"
            out.append('<ol dir="%s">' % d
                       + "".join("<li>%s</li>" % inline(x) for x in b["items"]) + "</ol>")
        elif k == "code":
            out.append(render_code(b))
        elif k == "hr":
            out.append("<hr>")
    return "\n".join(out)


def build_sections(blocks):
    """Group blocks into <section> chunks at each h1 boundary."""
    sections = []
    cur = {"id": "sec-top", "title": None, "parts": []}
    first_h1 = True
    for b in blocks:
        if b["kind"] == "heading" and b["level"] == 1:
            if first_h1:
                first_h1 = False  # document title stays in the prologue section
            else:
                sections.append(cur)
                cur = {"id": b["id"], "title": b["text"], "parts": []}
        cur["parts"].append(b)
    sections.append(cur)
    return sections


# --------------------------------------------------------------------------
# page template
# --------------------------------------------------------------------------

CSS = """
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
--accent:#58a6ff;--green:#7ee787;--gold:#f2cc60;--blue:#a5d6ff;--red:#ff7b72}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--text);
font-family:"Vazirmatn","Segoe UI",system-ui,-apple-system,Tahoma,sans-serif;
font-size:15px;line-height:1.75}
.topbar{position:sticky;top:0;z-index:20;background:rgba(13,17,23,.92);
backdrop-filter:blur(6px);border-bottom:1px solid var(--border);
padding:10px 20px;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.brand{font-weight:700;font-size:15px;white-space:nowrap}
.brand small{color:var(--muted);font-weight:400}
.meta{color:var(--muted);font-size:12px;white-space:nowrap}
#count{color:var(--gold);font-size:12px}
.spacer{flex:1}
#search{background:var(--panel);border:1px solid var(--border);color:var(--text);
border-radius:6px;padding:6px 10px;font-size:13px;width:220px;outline:none}
#search:focus{border-color:var(--accent)}
.layout{display:flex;gap:0;align-items:flex-start}
#toc{width:300px;flex:0 0 300px;position:sticky;top:57px;max-height:calc(100vh - 57px);
overflow:auto;border-right:1px solid var(--border);padding:14px 8px 30px 12px;font-size:13px}
#toc .toc-h{color:var(--muted);font-size:11px;letter-spacing:.12em;text-transform:uppercase;
margin:6px 6px 8px}
#toc a{display:block;color:var(--text);text-decoration:none;padding:3px 8px;border-radius:5px;
direction:ltr;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#toc a:hover{background:var(--panel);color:var(--accent)}
#toc a.sub{padding-left:22px;color:var(--muted);font-size:12px}
#toc a.dim{opacity:.25}
main{flex:1;min-width:0;max-width:980px;margin:0 auto;padding:26px 34px 80px}
section.hidden{display:none}
h1{font-size:24px;border-bottom:1px solid var(--border);padding-bottom:8px;margin:44px 0 14px}
main>section:first-child h1{margin-top:6px}
h2{font-size:18px;margin:28px 0 8px;color:var(--accent)}
h3{font-size:15px;margin:20px 0 6px}
p,li{overflow-wrap:break-word}
ul,ol{margin:8px 0;padding-inline-start:24px}
li{margin:3px 0;unicode-bidi:plaintext}
hr{border:0;border-top:1px solid var(--border);margin:22px 0}
code{font-family:Consolas,"Cascadia Mono","Courier New",monospace;background:var(--panel);
border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:.9em}
.code{background:#010409;border:1px solid var(--border);border-radius:8px;margin:14px 0}
.code-bar{display:flex;justify-content:space-between;align-items:center;
padding:3px 10px;border-bottom:1px solid #21262d;font-size:11px;color:var(--muted)}
.code-bar .lang{text-transform:uppercase;letter-spacing:.08em}
.copy{background:transparent;border:1px solid var(--border);color:var(--muted);
border-radius:4px;font-size:11px;padding:2px 8px;cursor:pointer}
.copy:hover{color:var(--text);border-color:var(--accent)}
pre{margin:0;padding:12px 14px;overflow:auto;direction:ltr;text-align:left;
font-family:Consolas,"Cascadia Mono","Courier New",monospace;font-size:13px;line-height:1.55}
pre code{background:none;border:0;padding:0}
.tk-key{color:var(--green)}.tk-str{color:var(--blue)}.tk-num{color:var(--gold)}
.tk-bool{color:var(--red)}
footer{margin-top:60px;border-top:1px solid var(--border);color:var(--muted);
font-size:12px;padding-top:14px}
footer code{font-size:11px}
.toc-toggle{display:none;background:var(--panel);border:1px solid var(--border);
color:var(--text);border-radius:6px;padding:5px 10px;font-size:13px;cursor:pointer}
.toc-toggle:hover{border-color:var(--accent)}
.backdrop{position:fixed;top:57px;left:0;right:0;bottom:0;background:rgba(0,0,0,.5);
z-index:25;display:none}
body.toc-open .backdrop{display:block}
@media (max-width:1080px){
#toc{position:fixed;top:57px;left:0;bottom:0;width:290px;max-height:none;background:var(--bg);
z-index:30;transform:translateX(-105%);transition:transform .18s ease,visibility .18s;
visibility:hidden;box-shadow:4px 0 18px rgba(0,0,0,.45)}
body.toc-open #toc{transform:none;visibility:visible}
.toc-toggle{display:inline-block}
.meta{display:none}
}
@media print{.topbar,#toc,.copy{display:none}body{background:#fff;color:#111}}
"""

JS = """
(function(){
  var input=document.getElementById('search');
  var count=document.getElementById('count');
  var sections=Array.prototype.slice.call(document.querySelectorAll('main section'));
  var links=Array.prototype.slice.call(document.querySelectorAll('#toc a'));
  function apply(){
    var q=input.value.trim().toLowerCase(),shown=0;
    sections.forEach(function(s){
      var hit=!q||s.textContent.toLowerCase().indexOf(q)!==-1;
      s.classList.toggle('hidden',!hit);if(hit)shown++;
    });
    links.forEach(function(a){
      var t=document.getElementById(a.getAttribute('href').slice(1));
      a.classList.toggle('dim',!!q&&t&&t.classList.contains('hidden'));
    });
    count.textContent=q?(shown+' / '+sections.length+' sections'):'';
  }
  if(input){input.addEventListener('input',apply);}
  var toggle=document.querySelector('.toc-toggle');
  var backdrop=document.getElementById('backdrop');
  function closeToc(){document.body.classList.remove('toc-open');}
  if(toggle){toggle.addEventListener('click',function(){document.body.classList.toggle('toc-open');});}
  if(backdrop){backdrop.addEventListener('click',closeToc);}
  links.forEach(function(a){a.addEventListener('click',closeToc);});
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape'){input.value='';apply();input.blur();closeToc();}
  });
  Array.prototype.forEach.call(document.querySelectorAll('.copy'),function(b){
    b.addEventListener('click',function(){
      var pre=b.closest('.code').querySelector('pre');
      function done(){b.textContent='Copied';setTimeout(function(){b.textContent='Copy';},1200);}
      if(navigator.clipboard&&navigator.clipboard.writeText){
        navigator.clipboard.writeText(pre.innerText).then(done,function(){b.textContent='Ctrl+C';});
      }else{b.textContent='Ctrl+C';}
    });
  });
})();
"""


def main() -> int:
    if not SRC.exists():
        print("source not found: %s" % SRC)
        return 1
    raw = SRC.read_bytes()
    text = raw.decode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    blocks = parse(text)

    h1s = [b for b in blocks if b["kind"] == "heading" and b["level"] == 1]
    n_sections = len(h1s) - 1  # minus document title
    if n_sections != EXPECTED_SECTIONS:
        print("warning: expected %d contract sections, found %d"
              % (EXPECTED_SECTIONS, n_sections))

    sections = build_sections(blocks)

    toc = ['<div class="toc-h">Contents</div>']
    first_h1_seen = False
    for b in blocks:
        if b["kind"] != "heading" or b["level"] > 2:
            continue
        if b["level"] == 1:
            if not first_h1_seen:
                first_h1_seen = True  # skip document title itself
                continue
            toc.append('<a href="#%s" title="%s">%s</a>'
                       % (b["id"], html.escape(b["text"], quote=True),
                          html.escape(b["text"], quote=False)))
        else:
            toc.append('<a class="sub" href="#%s" title="%s">%s</a>'
                       % (b["id"], html.escape(b["text"], quote=True),
                          html.escape(b["text"], quote=False)))

    body = []
    for s in sections:
        body.append('<section id="%s">' % s["id"])
        body.append(render_blocks(s["parts"]))
        body.append("</section>")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    footer = (
        "Generated from <code>docs/data-contract.md</code> &middot; "
        "sha256:<code>%s</code> &middot; %d contract sections &middot; %s<br>"
        "Rebuild after editing the markdown: <code>python scripts/build_data_contract_html.py</code>"
    ) % (digest[:12], n_sections, now)

    page = []
    page.append("<!DOCTYPE html>")
    page.append('<html lang="en">')
    page.append("<head><meta charset=\"utf-8\">")
    page.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    page.append("<title>Data Contract &amp; Schema Specification — Nobitex Trading Bot</title>")
    page.append("<style>%s</style></head>" % CSS)
    page.append("<body>")
    page.append('<header class="topbar">')
    page.append('<button class="toc-toggle" type="button" aria-label="Toggle table of contents">'
                '&#9776; Contents</button>')
    page.append('<span class="brand">Nobitex Trading Bot &middot; Data Contract <small>v1.0</small></span>')
    page.append('<span class="meta">%d sections &middot; sha256 %s</span>' % (n_sections, digest[:12]))
    page.append('<span id="count"></span><span class="spacer"></span>')
    page.append('<input id="search" type="search" placeholder="Search sections…" '
                'aria-label="Filter sections">')
    page.append("</header>")
    page.append('<div class="layout">')
    page.append('<nav id="toc" aria-label="Table of contents">%s</nav>' % "\n".join(toc))
    page.append("<main>%s</main>" % "\n".join(body))
    page.append("</div>")
    page.append("<footer><div class=\"inner\">%s</div></footer>" % footer)
    page.append('<div class="backdrop" id="backdrop"></div>')
    page.append("<script>%s</script></body></html>" % JS)

    OUT.write_text("\n".join(page), encoding="utf-8")
    print("wrote %s (%d bytes) from %s (%d lines, sha256 %s)"
          % (OUT, OUT.stat().st_size, SRC, len(text.splitlines()), digest[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
