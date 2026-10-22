# /// script
# requires-python = ">=3.9"
# dependencies = ["markdown==3.7"]
# ///
"""Render the public Sentinel docs into static pages under docs/.

Usage (from the website root):
    uv run scripts/build_docs.py [path/to/sentinel/docs]

Reads every published page of the Sentinel docs (skips _internal/ and drafts),
renders it into the site layout, copies the images it references, and rewrites
sitemap.xml. Run it again whenever the Sentinel docs change.
"""
import html
import json
import posixpath
import re
import shutil
import sys
from datetime import date
from pathlib import Path

import markdown

SITE = Path(__file__).resolve().parent.parent
SRC = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else (SITE.parent / "sentinel" / "docs").resolve()
OUT = SITE / "docs"
SITE_URL = "https://styxlabs.xyz"
REPO_URL = "https://github.com/StyxLabs-Inc/sentinel"

# The old docs site moved to styxlabs.xyz. Matches only full URLs, so the
# Kubernetes API group agent.sentinel.dev is left alone.
OLD_SITE = re.compile(r"https?://(?:www\.)?sentinel\.dev")
ADMONITION_TITLES = {"note": "Note", "tip": "Tip", "info": "Info", "warning": "Warning", "caution": "Caution", "danger": "Danger"}


def parse_page(path):
    text = OLD_SITE.sub(SITE_URL, path.read_text(encoding="utf-8"))
    meta = {}
    if text.startswith("---\n"):
        end = text.index("\n---", 4)
        for line in text[4:end].splitlines():
            key, sep, value = line.partition(":")
            if not sep:
                continue
            value = value.strip()
            if value[:1] in "\"'":
                value = value[1:value.index(value[0], 1)]
            else:
                value = value.split(" #")[0].strip()
            meta[key.strip()] = value
        text = text[end + 4:].lstrip("\n")
    if "title" not in meta:
        m = re.match(r"#\s+(.+)\n", text)
        if m:
            meta["title"] = m.group(1).strip()
            text = text[m.end():]
        else:
            meta["title"] = path.stem.replace("-", " ").title()
    return meta, text


def convert_admonitions(text):
    out, fence = [], None
    for line in text.split("\n"):
        stripped = line.strip()
        fm = re.match(r"(`{3,}|~{3,})", stripped)
        if fm and (fence is None or stripped.startswith(fence)):
            fence = None if fence else fm.group(1)
        if fence is None:
            m = re.match(r":::(\w+)(?:\[(.*)\]|\s+(.*))?$", stripped)
            if m and m.group(1) in ADMONITION_TITLES:
                kind = m.group(1)
                title = m.group(2) or m.group(3) or ADMONITION_TITLES[kind]
                out += [f'<div class="admonition {kind}" markdown="1">',
                        f'<p class="admonition-title">{html.escape(title)}</p>', ""]
                continue
            if stripped == ":::":
                out += ["", "</div>"]
                continue
        out.append(line)
    return "\n".join(out)


def out_path(rel):
    return rel[:-3] + ".html"


def collect():
    pages = {}
    for path in sorted(SRC.rglob("*.md")):
        rel = path.relative_to(SRC).as_posix()
        if any(part.startswith(("_", ".")) for part in rel.split("/")):
            continue
        meta, body = parse_page(path)
        if meta.get("draft") == "true":
            continue
        pages[rel] = {"meta": meta, "body": body, "url": out_path(rel)}
    return pages


def build_nav(pages):
    """A Docusaurus-like tree from _category_.json files and sidebar_position."""
    def category(dir_rel):
        cfg_file = SRC / dir_rel / "_category_.json"
        cfg = json.loads(cfg_file.read_text()) if cfg_file.exists() else {}
        link = (cfg.get("link") or {}).get("id")
        name = posixpath.basename(dir_rel)
        return {
            "label": cfg.get("label") or ("HyperShift" if name == "hypershift" else name.replace("-", " ").capitalize()),
            "pos": cfg.get("position", 99),
            "link": link + ".md" if link else None,
            "children": [],
        }

    root = {"children": []}
    cats = {"": root}

    def get_cat(dir_rel):
        if dir_rel not in cats:
            cats[dir_rel] = category(dir_rel)
            get_cat(posixpath.dirname(dir_rel))["children"].append(cats[dir_rel])
        return cats[dir_rel]

    for rel, page in pages.items():
        cat = get_cat(posixpath.dirname(rel))
        if cat.get("link") == rel:
            continue
        meta = page["meta"]
        cat["children"].append({
            "label": meta.get("sidebar_label") or meta["title"],
            "pos": int(meta.get("sidebar_position", 99)),
            "doc": rel,
        })

    def sort(node):
        node["children"].sort(key=lambda c: (c["pos"], c["label"].lower()))
        for c in node["children"]:
            if "children" in c:
                sort(c)
    sort(root)
    return root


def flatten(node, acc):
    for c in node["children"]:
        if "doc" in c:
            acc.append(c["doc"])
        else:
            if c["link"]:
                acc.append(c["link"])
            flatten(c, acc)
    return acc


def rel_url(from_url, to_url):
    return posixpath.relpath(to_url, posixpath.dirname(from_url) or ".")


def render_nav(node, pages, current, depth=0):
    items = []
    for c in node["children"]:
        if "doc" in c:
            cls = ' class="active" aria-current="page"' if c["doc"] == current else ""
            href = rel_url(pages[current]["url"], pages[c["doc"]]["url"])
            items.append(f'<li><a href="{href}"{cls}>{html.escape(c["label"])}</a></li>')
            continue
        docs = flatten(c, [])
        open_attr = " open" if current in docs or current == c["link"] else ""
        if c["link"]:
            cls = ' class="active" aria-current="page"' if c["link"] == current else ""
            href = rel_url(pages[current]["url"], pages[c["link"]]["url"])
            summary = f'<a href="{href}"{cls}>{html.escape(c["label"])}</a>'
        else:
            summary = f'<span>{html.escape(c["label"])}</span>'
        items.append(f'<li><details{open_attr}><summary>{summary}</summary>'
                     f'{render_nav(c, pages, current, depth + 1)}</details></li>')
    return f'<ul class="docs-nav-list">{"".join(items)}</ul>'


def rewrite_links(body_html, rel, pages, assets):
    src_dir = posixpath.dirname(rel)
    page_url = pages[rel]["url"]

    def fix(m):
        attr, target = m.group(1), html.unescape(m.group(2))
        if re.match(r"^([a-z][a-z0-9+.-]*:|#|/)", target, re.I):
            return m.group(0)
        path, _, frag = target.partition("#")
        resolved = posixpath.normpath(posixpath.join(src_dir, path))
        frag = "#" + frag if frag else ""
        if resolved in pages:
            new = rel_url(page_url, pages[resolved]["url"]) + frag
        elif not resolved.startswith(("..", "_")) and "/_" not in resolved and (SRC / resolved).is_file():
            assets.add(resolved)
            new = rel_url(page_url, resolved) + frag
        else:
            kind = "tree" if (SRC / resolved).is_dir() else "blob"
            repo_path = posixpath.normpath(posixpath.join("docs", resolved))
            new = f"{REPO_URL}/{kind}/main/{repo_path}{frag}"
            if not (SRC / resolved).exists():
                print(f"  warning: {rel} links to missing {target}")
        return f'{attr}="{html.escape(new, quote=True)}"'

    return re.sub(r'\b(href|src)="([^"]*)"', fix, body_html)


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} | Sentinel Docs | StyxLabs</title>
  <meta name="description" content="{description}">
  <link rel="canonical" href="{canonical}">
  <meta property="og:title" content="{title} — Sentinel Docs">
  <meta property="og:description" content="{description}">
  <meta property="og:url" content="{canonical}">
  <meta property="og:site_name" content="StyxLabs">
  <meta property="og:image" content="{site}/assets/og-image.png">
  <meta property="og:type" content="article">
  <link rel="icon" type="image/png" href="{root}assets/styxlabs-mark.png">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="{root}css/styles.css">
  <link rel="stylesheet" href="{root}css/docs.css">
</head>
<body class="docs-page">
  <a class="skip-link" href="#main">Skip to content</a>

  <header class="site-header scrolled" id="top">
    <div class="container nav docs-container">
      <a class="brand" href="{root}index.html" aria-label="StyxLabs home">
        <img src="{root}assets/styxlabs-mark.png" alt="" width="29" height="40">
        <span class="wordmark"><strong>STYX</strong>Labs</span>
        <span class="docs-badge">Sentinel Docs</span>
      </a>
      <button class="nav-toggle" aria-expanded="false" aria-controls="nav-menu" aria-label="Open menu">
        <span></span><span></span><span></span>
      </button>
      <nav id="nav-menu" class="nav-menu" aria-label="Primary">
        <a href="{root}index.html#platform">Platform</a>
        <a href="{docs_home}" aria-current="page">Docs</a>
        <a href="{repo}">GitHub</a>
        <a class="btn btn-primary btn-sm" href="{root}index.html#contact">Schedule a Demo</a>
      </nav>
    </div>
  </header>

  <div class="container docs-container docs-layout">
    <aside class="docs-sidebar" aria-label="Documentation">
      <details class="docs-sidebar-toggle" open>
        <summary>Documentation menu</summary>
        <nav>{nav}</nav>
      </details>
    </aside>

    <main id="main" class="docs-main">
      <article class="docs-article">
        <p class="eyebrow">{section}</p>
        <h1>{title}</h1>
        {lede}
        {body}
      </article>
      <nav class="docs-pager" aria-label="Previous and next page">{pager}</nav>
      <p class="docs-edit"><a href="{edit_url}">Edit this page on GitHub</a></p>
    </main>
  </div>

  <footer class="site-footer docs-footer">
    <div class="container docs-container legal">
      <p>© <span id="year">2026</span> StyxLabs Inc. Sentinel is open source under the Apache 2.0 license.</p>
      <p><a href="{root}index.html">styxlabs.xyz</a> · <a href="{repo}">GitHub</a> · <a href="mailto:hello@styxlabs.xyz">hello@styxlabs.xyz</a></p>
    </div>
  </footer>

  <script src="{root}js/main.js"></script>
</body>
</html>
"""


def section_label(nav, rel):
    for c in nav["children"]:
        if c.get("doc") == rel:
            return "Sentinel"
        if "children" in c and (c["link"] == rel or rel in flatten(c, [])):
            return c["label"]
    return "Sentinel"


def main():
    if not SRC.is_dir():
        sys.exit(f"Sentinel docs not found at {SRC}")
    pages = collect()
    nav = build_nav(pages)
    order = flatten(nav, [])
    assets = set()

    if OUT.exists():
        shutil.rmtree(OUT)

    for rel, page in pages.items():
        meta = page["meta"]
        md = markdown.Markdown(extensions=["tables", "fenced_code", "md_in_html", "sane_lists", "attr_list",
                                           "toc"], extension_configs={"toc": {"permalink": "#"}})
        body = rewrite_links(md.convert(convert_admonitions(page["body"])), rel, pages, assets)
        body = body.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")

        url = page["url"]
        root = "../" * url.count("/")
        idx = order.index(rel) if rel in order else -1
        pager = []
        if idx > 0:
            prev = order[idx - 1]
            pager.append(f'<a class="prev" href="{rel_url(url, pages[prev]["url"])}"><span>Previous</span>'
                         f'{html.escape(pages[prev]["meta"]["title"])}</a>')
        if 0 <= idx < len(order) - 1:
            nxt = order[idx + 1]
            pager.append(f'<a class="next" href="{rel_url(url, pages[nxt]["url"])}"><span>Next</span>'
                         f'{html.escape(pages[nxt]["meta"]["title"])}</a>')

        description = meta.get("description", "")
        target = OUT / url
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(TEMPLATE.format(
            title=html.escape(meta["title"]),
            description=html.escape(description or f"{meta['title']} — Sentinel documentation."),
            lede=f'<p class="docs-lede">{html.escape(description)}</p>' if description else "",
            canonical=f"{SITE_URL}/docs/{url}",
            site=SITE_URL,
            root=root + "../",
            docs_home=rel_url(url, "index.html"),
            repo=REPO_URL,
            section=html.escape(section_label(nav, rel)),
            nav=render_nav(nav, pages, rel),
            body=body,
            pager="".join(pager),
            edit_url=f"{REPO_URL}/edit/main/docs/{rel}",
        ), encoding="utf-8")

    for asset in sorted(assets):
        dest = OUT / asset
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SRC / asset, dest)

    today = date.today().isoformat()
    urls = [f"{SITE_URL}/"] + [f"{SITE_URL}/docs/{pages[r]['url']}" for r in order]
    (SITE / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url>\n    <loc>{u}</loc>\n    <lastmod>{today}</lastmod>\n  </url>\n" for u in urls)
        + "</urlset>\n", encoding="utf-8")

    print(f"Built {len(pages)} pages and {len(assets)} assets into {OUT.relative_to(SITE)}/")


if __name__ == "__main__":
    main()
