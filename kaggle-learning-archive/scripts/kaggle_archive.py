#!/usr/bin/env python3
"""Archive a Kaggle discussion or writeup into a local learning library.

Layout:
    <root>/<category>/<competition_slug>_<end_date>/<topic_id>-<title_slug>/
        README.md   - post body (author's raw markdown) + comments, images relinked locally
        meta.json   - machine-readable metadata (used to rebuild <root>/INDEX.md)
        images/     - every image referenced by the post and its comments

Usage:
    kaggle_archive.py config [--root <dir>]          # show / set where the library lives
    kaggle_archive.py info <url>                     # print metadata + suggested category as JSON
    kaggle_archive.py save <url> --category <name>   # fetch and write the archive folder
    kaggle_archive.py index                          # rebuild <root>/INDEX.md

The library root is resolved from --root, then $KAGGLE_LEARNING_ROOT, then the saved
config ($XDG_CONFIG_HOME/kaggle-learning-archive/config.json). If none is set, info/save/index
exit with code 4 so the caller can ask the user where the library should live and run
`config --root <dir>` once.

<url> is a Kaggle discussion URL (.../discussion/<id>), a writeup URL
(.../competitions/<slug>/writeups/<writeup-slug>), or a bare numeric topic id.

Only the Python standard library is used. Public posts need no credentials; if
KAGGLE_API_TOKEN is set it is sent as a bearer token (for restricted content).
"""

from __future__ import annotations

import argparse
import datetime as dt
import http.cookiejar
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KAGGLE = "https://www.kaggle.com"
API = f"{KAGGLE}/api/i"
CONFIG_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()
    / "kaggle-learning-archive" / "config.json"
)
EXIT_NOT_CONFIGURED = 4
USER_AGENT = "Mozilla/5.0 (kaggle-learning-archive)"

WRITEUP_RE = re.compile(r"kaggle\.com/competitions/(?P<comp>[^/?#]+)/writeups/(?P<slug>[^/?#]+)")
DISCUSSION_RE = re.compile(r"kaggle\.com/.*?discussions?/(?:[^/?#]+/)?(?P<id>\d+)")
# URLs may contain balanced parentheses, e.g. ".../image%20(3).png?generation=..." (CommonMark allows it).
MD_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<url>(?:[^()\s]|\([^()\s]*\))+)(?P<title>\s+\"[^\"]*\")?\)")
HTML_IMAGE_RE = re.compile(r"(<img\b[^>]*?\bsrc=)([\"'])(?P<url>[^\"']+)\2", re.IGNORECASE)


class KaggleSession:
    """Anonymous browser-style session for Kaggle's internal JSON web service."""

    def __init__(self, retries: int = 3):
        self.retries = retries
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.opener.addheaders = [("User-Agent", USER_AGENT)]
        token = os.environ.get("KAGGLE_API_TOKEN")
        if token:
            self.opener.addheaders.append(("Authorization", f"Bearer {token}"))
        self._open(urllib.request.Request(KAGGLE))  # seeds the XSRF-TOKEN cookie
        self.xsrf = next((c.value for c in self.cookies if c.name == "XSRF-TOKEN"), "")
        if not self.xsrf:
            raise RuntimeError("Could not obtain an XSRF token from kaggle.com")

    def _open(self, req: urllib.request.Request) -> bytes:
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                with self.opener.open(req, timeout=60) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if 400 <= exc.code < 500 and exc.code != 429:
                    raise
                last = exc
            except urllib.error.URLError as exc:
                last = exc
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"Request to {req.full_url} failed after {self.retries} tries: {last}")

    def call(self, method: str, body: dict) -> dict:
        req = urllib.request.Request(
            f"{API}/{method}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-XSRF-TOKEN": self.xsrf},
            method="POST",
        )
        try:
            return json.loads(self._open(req))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"{method} returned HTTP {exc.code}: {detail}") from exc

    def download(self, url: str) -> tuple[bytes, str]:
        with self.opener.open(urllib.request.Request(url), timeout=120) as resp:
            return resp.read(), resp.headers.get_content_type()


# ── Resolving the post ────────────────────────────────────────────────────


def resolve_topic(sess: KaggleSession, url: str) -> tuple[str, dict, dict | None]:
    """Return (kind, forumTopic, writeUp-or-None) for a discussion/writeup URL or topic id."""
    url = url.strip()
    if m := WRITEUP_RE.search(url):
        write_up = sess.call(
            "discussions.WriteUpsService/GetWriteUpBySlug",
            {"slug": m["slug"], "competitionName": m["comp"]},
        )
        topic_id = write_up.get("topicId")
        if not topic_id:
            raise RuntimeError(f"Writeup '{m['slug']}' has no forum topic id")
    elif url.isdigit():
        topic_id = int(url)
    elif m := DISCUSSION_RE.search(url):
        topic_id = int(m["id"])
    else:
        raise RuntimeError(f"Not a Kaggle discussion or writeup URL: {url}")

    topic = sess.call(
        "discussions.DiscussionsService/GetForumTopicById",
        {"forumTopicId": topic_id, "includeComments": True},
    ).get("forumTopic") or {}
    if not topic:
        raise RuntimeError(f"Topic {topic_id} not found (deleted, private, or wrong id)")
    write_up = topic.get("writeUp") or None
    return ("writeup" if write_up else "discussion"), topic, write_up


def competition_info(sess: KaggleSession, topic: dict, write_up: dict | None) -> dict | None:
    # Regular competition topics carry parentUrl=/competitions/<slug>. Hackathon-style
    # writeups have no parentUrl, but their own URL still names the competition.
    slug = None
    for url in (topic.get("parentUrl"), (write_up or {}).get("url")):
        if m := re.match(r"/competitions/([^/?#]+)", url or ""):
            slug = m[1]
            break
    if not slug:
        return None
    comp = sess.call("competitions.CompetitionService/GetCompetition", {"competitionName": slug})
    paths = [c.get("fullPath") for c in comp.get("categories") or [] if c.get("fullPath")]
    return {
        "slug": slug,
        "title": comp.get("title"),
        "brief_description": comp.get("briefDescription"),
        "deadline": comp.get("deadline"),
        "end_date": (comp.get("deadline") or "")[:10] or None,
        "simulation": bool(comp.get("simulation")),
        "task_tags": [p.split(">", 1)[1].strip() for p in paths if p.startswith("task >")],
        "subject_tags": [p.split(">")[-1].strip() for p in paths if p.startswith("subject >")],
        "url": f"{KAGGLE}/competitions/{slug}",
    }


# ── Naming ────────────────────────────────────────────────────────────────


def slugify(text: str, max_len: int = 60) -> str:
    text = text.replace("’", "").replace("'", "")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()  # Pokémon -> Pokemon
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    return cut.rsplit("-", 1)[0] if "-" in cut else cut


def snake(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def group_folder(topic: dict, comp: dict | None) -> str:
    if comp:
        return f"{comp['slug']}_{comp['end_date'] or 'no-deadline'}"
    forum = topic.get("parentName") or topic.get("forumName") or "forum"
    return f"forum-{slugify(forum)}"


def entry_folder(topic: dict, write_up: dict | None) -> str:
    title_slug = (write_up or {}).get("slug") or ""
    if not title_slug or re.fullmatch(r"new-writeup-\d+", title_slug):  # Kaggle's placeholder slug
        title_slug = slugify((write_up or {}).get("title") or topic.get("name") or "")
    return f"{topic['id']}-{title_slug}" if title_slug else str(topic["id"])


def existing_categories(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_")))


def find_existing(root: Path, topic_id: int) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob(f"*/*/{topic_id}-*") if p.is_dir()) + sorted(
        p for p in root.glob(f"*/*/{topic_id}") if p.is_dir()
    )


def suggest_category(comp: dict | None, existing: list[str]) -> tuple[str | None, str]:
    """Heuristic only — the agent makes the final call."""
    if comp:
        tags = [snake(t) for t in comp["task_tags"]]
        for t in tags:
            if t in existing:
                return t, f"competition task tag '{t}' matches an existing folder"
        if tags:
            return tags[0], f"from competition task tag '{comp['task_tags'][0]}'"
        if comp["simulation"]:
            name = "reinforcement_learning"
            return name, "simulation (agent-vs-agent) competition with no task tag"
    return None, "no task tag; decide from the competition title/description and post content"


# ── Markdown assembly ─────────────────────────────────────────────────────


def author_of(msg: dict) -> str:
    a = msg.get("author") or {}
    name = a.get("displayName") or "unknown"
    return f"{name} (kaggle.com{a['url']})" if a.get("url") else name


def comment_blocks(nodes: list[dict], depth: int = 1) -> list[str]:
    out: list[str] = []
    for c in nodes:
        votes = (c.get("votes") or {}).get("totalVotes", 0)
        head = f"**{(c.get('author') or {}).get('displayName', 'unknown')}** · {(c.get('postDate') or '')[:10]}"
        if votes:
            head += f" · ▲{votes}"
        body = (c.get("rawMarkdown") or c.get("content") or "").strip()
        prefix = "> " * depth
        lines = [head, ""] + body.splitlines()
        out.append("\n".join((prefix + ln).rstrip() for ln in lines))
        out.append("")
        if c.get("replies"):
            out.extend(comment_blocks(c["replies"], depth + 1))
    return out


def count_comments(nodes: list[dict]) -> int:
    return sum(1 + count_comments(c.get("replies") or []) for c in nodes)


def localize_images(sess: KaggleSession, markdown: str, img_dir: Path) -> tuple[str, list[str]]:
    """Download every image referenced in `markdown`, rewrite links to images/<name>."""
    cache: dict[str, str] = {}
    failures: list[str] = []
    used: set[str] = set()

    def local_name(url: str, content_type: str) -> str:
        path = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        base = re.split(r"[/]", path)[-1] or "image"
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[-80:]
        stem, ext = os.path.splitext(base)
        if not ext:
            ext = mimetypes.guess_extension(content_type) or ".png"
        name, i = f"{stem}{ext}", 2
        while name in used:
            name, i = f"{stem}-{i}{ext}", i + 1
        used.add(name)
        return name

    def fetch(url: str) -> str | None:
        if url in cache:
            return cache[url]
        absolute = urllib.parse.urljoin(KAGGLE, url)
        try:
            data, ctype = sess.download(absolute)
        except Exception as exc:  # noqa: BLE001 - reported, original link kept
            failures.append(f"{absolute} ({exc})")
            cache[url] = None
            return None
        img_dir.mkdir(parents=True, exist_ok=True)
        name = local_name(absolute, ctype)
        (img_dir / name).write_bytes(data)
        cache[url] = f"images/{name}"
        return cache[url]

    def md_sub(m: re.Match) -> str:
        local = fetch(m["url"])
        return f"![{m['alt']}]({local}{m['title'] or ''})" if local else m[0]

    def html_sub(m: re.Match) -> str:
        local = fetch(m["url"])
        return f"{m[1]}{m[2]}{local}{m[2]}" if local else m[0]

    markdown = MD_IMAGE_RE.sub(md_sub, markdown)
    markdown = HTML_IMAGE_RE.sub(html_sub, markdown)
    return markdown, failures


def starts_with_title(body: str, title: str) -> bool:
    """True if the post already opens with a level-1 heading that is (a form of) its title.

    Some authors repeat the title as the first heading, others open with "# Overview";
    only the former should suppress adding the title ourselves.
    """
    m = re.match(r"#\s+(.+)", body.lstrip())
    if not m:
        return False
    heading, full = slugify(m[1], max_len=10**6), slugify(title, max_len=10**6)
    return bool(heading) and (heading in full or full in heading)


def build_readme(kind: str, topic: dict, write_up: dict | None, comp: dict | None) -> tuple[str, dict]:
    first = topic.get("firstMessage") or {}
    message = (write_up or {}).get("message") or first
    body = (message.get("rawMarkdown") or message.get("content") or "").strip()
    title = ((write_up or {}).get("title") or topic.get("name") or "").strip() or f"Topic {topic['id']}"
    subtitle = ((write_up or {}).get("subtitle") or "").strip()
    source = KAGGLE + ((write_up or {}).get("url") or topic.get("url") or f"/discussion/{topic['id']}")

    if write_up and write_up.get("collaborators"):
        authors = ", ".join(
            f"{c.get('displayName')} (kaggle.com{c.get('url')})" for c in write_up["collaborators"]
        )
    else:
        authors = author_of(message)

    comments = topic.get("comments") or []
    n_comments = count_comments(comments)
    fetched = dt.date.today().isoformat()
    posted = ((write_up or {}).get("publishTime") or message.get("postDate") or topic.get("postDate") or "")[:10]
    rank = message.get("competitionRanking")

    header = [
        "<!--",
        f"Source: {source}",
        f"Type: {kind}",
        f"Title: {title}",
    ]
    if subtitle:
        header.append(f"Subtitle: {subtitle}")
    header.append(f"Author(s): {authors}" + (f", competition rank {rank} at time of fetch" if rank else ""))
    if comp:
        header.append(f"Competition: {comp['title']} ({comp['url']}), deadline {comp['end_date']}")
    header += [
        f"Posted: {posted or '?'}  |  Fetched: {fetched}  |  Votes: {topic.get('totalVotes', 0)}  |  Comments: {n_comments}",
        "Images downloaded locally to ./images/.",
        "-->",
        "",
    ]

    parts = ["\n".join(header)]
    if not starts_with_title(body, title):
        parts.append(f"# {title}\n")
        if subtitle:
            parts.append(f"*{subtitle}*\n")
    parts.append(body + "\n")
    if comments:
        parts.append(f"---\n\n## Comments ({n_comments})\n")
        parts.append("\n".join(comment_blocks(comments)))

    meta = {
        "topic_id": topic["id"],
        "kind": kind,
        "title": title,
        "subtitle": subtitle or None,
        "authors": authors,
        "source_url": source,
        "discussion_url": f"{KAGGLE}{topic['url']}" if "/discussion/" in (topic.get("url") or "") else None,
        "posted": posted or None,
        "fetched": fetched,
        "votes": topic.get("totalVotes", 0),
        "comments": n_comments,
        "competition_rank_at_fetch": rank,
        "competition": comp,
    }
    return "\n".join(parts).rstrip() + "\n", meta


# ── Index ─────────────────────────────────────────────────────────────────


def rebuild_index(root: Path) -> Path:
    lines = ["# Kaggle learning library", "", "Rebuilt automatically by the kaggle-learning-archive skill; do not edit.", ""]
    for cat in existing_categories(root):
        entries = []
        for meta_path in sorted((root / cat).glob("*/*/meta.json")):
            try:
                meta = json.loads(meta_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            entries.append((meta_path.parent, meta))
        if not entries:
            continue
        lines.append(f"## {cat}")
        current_group = None
        for folder, meta in entries:
            group = folder.parent.name
            if group != current_group:
                comp = meta.get("competition") or {}
                lines += ["", f"### {comp.get('title') or group}  ·  `{group}`", ""]
                current_group = group
            rel = folder.relative_to(root) / "README.md"
            lines.append(
                f"- [{meta['title']}]({rel.as_posix()}) — {meta['kind']}, {meta['authors'].split(' (')[0]}"
                f", {meta.get('posted') or '?'} · [source]({meta['source_url']})"
            )
        lines.append("")
    index = root / "INDEX.md"
    root.mkdir(parents=True, exist_ok=True)
    index.write_text("\n".join(lines).rstrip() + "\n")
    return index


# ── Commands ──────────────────────────────────────────────────────────────


def cmd_info(args) -> int:
    sess = KaggleSession()
    kind, topic, write_up = resolve_topic(sess, args.url)
    comp = competition_info(sess, topic, write_up)
    existing = existing_categories(args.root)
    suggestion, reason = suggest_category(comp, existing)
    body = ((write_up or {}).get("message") or topic.get("firstMessage") or {}).get("rawMarkdown") or ""
    print(json.dumps({
        "kind": kind,
        "topic_id": topic["id"],
        "title": (write_up or {}).get("title") or topic.get("name"),
        "subtitle": (write_up or {}).get("subtitle"),
        "author": topic.get("authorUserDisplayName"),
        "competition": comp,
        "forum": None if comp else (topic.get("parentName") or topic.get("forumName")),
        "root": str(args.root),
        "existing_categories": existing,
        "suggested_category": suggestion,
        "suggestion_reason": reason,
        "target_relative": f"<category>/{group_folder(topic, comp)}/{entry_folder(topic, write_up)}",
        "already_archived_at": [str(p) for p in find_existing(args.root, topic["id"])],
        "body_preview": body[:1500],
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_save(args) -> int:
    category = snake(args.category)
    if not category:
        print("error: --category must contain letters or digits", file=sys.stderr)
        return 2
    sess = KaggleSession()
    kind, topic, write_up = resolve_topic(sess, args.url)
    comp = competition_info(sess, topic, write_up)
    category_created = not (args.root / category).is_dir()
    target = args.root / category / group_folder(topic, comp) / entry_folder(topic, write_up)

    elsewhere = [p for p in find_existing(args.root, topic["id"]) if p != target]
    if elsewhere and not args.move:
        print(
            "error: this post is already archived at:\n  " + "\n  ".join(map(str, elsewhere))
            + "\nRe-run with that category to refresh it, or pass --move to relocate it here.",
            file=sys.stderr,
        )
        return 3
    for old in elsewhere:
        shutil.rmtree(old)
        if old.parent.is_dir() and not any(old.parent.iterdir()):
            old.parent.rmdir()

    readme, meta = build_readme(kind, topic, write_up, comp)
    if target.exists():
        shutil.rmtree(target)  # refresh: replace with the current version of the post
    target.mkdir(parents=True)
    readme, failures = localize_images(sess, readme, target / "images")
    (target / "README.md").write_text(readme)
    meta["category"] = category
    meta["image_failures"] = failures
    (target / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
    index = rebuild_index(args.root)

    n_images = len(list((target / "images").glob("*"))) if (target / "images").is_dir() else 0
    print(json.dumps({
        "saved": str(target / "README.md"),
        "kind": kind,
        "title": meta["title"],
        "category": category,
        "category_created": category_created,
        "images_downloaded": n_images,
        "image_failures": failures,
        "comments": meta["comments"],
        "moved_from": [str(p) for p in elsewhere],
        "index": str(index),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_index(args) -> int:
    print(rebuild_index(args.root))
    return 0


# ── Library location ──────────────────────────────────────────────────────


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def suggested_root() -> Path:
    kaggle_dir = Path("~/Kaggle").expanduser()
    return kaggle_dir / "learning" if kaggle_dir.is_dir() else Path("~/kaggle-learning").expanduser()


def resolve_root(flag: Path | None) -> tuple[Path | None, str]:
    if flag:
        return flag, "--root"
    if os.environ.get("KAGGLE_LEARNING_ROOT"):
        return Path(os.environ["KAGGLE_LEARNING_ROOT"]).expanduser(), "KAGGLE_LEARNING_ROOT"
    if load_config().get("root"):
        return Path(load_config()["root"]).expanduser(), str(CONFIG_PATH)
    return None, "unset"


def cmd_config(args) -> int:
    if args.root:
        root = args.root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({**load_config(), "root": str(root)}, indent=2) + "\n")
    root, source = resolve_root(None)
    print(json.dumps({
        "root": str(root) if root else None,
        "source": source,
        "config_file": str(CONFIG_PATH),
        "suggested_root": None if root else str(suggested_root()),
    }, indent=2))
    return 0


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=lambda p: Path(p).expanduser(), default=None,
                        help="library root for this run (overrides env and saved config)")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("config", parents=[common], help="show the library location, or save it with --root")
    p_info = sub.add_parser("info", parents=[common], help="print metadata and a suggested category")
    p_info.add_argument("url")
    p_save = sub.add_parser("save", parents=[common], help="fetch and write the archive folder")
    p_save.add_argument("url")
    p_save.add_argument("--category", required=True, help="problem-type folder, e.g. reinforcement_learning")
    p_save.add_argument("--move", action="store_true", help="relocate an existing copy filed elsewhere")
    sub.add_parser("index", parents=[common], help="rebuild INDEX.md from meta.json files")
    args = parser.parse_args()
    if args.cmd == "config":
        return cmd_config(args)
    args.root, _ = resolve_root(args.root)
    if args.root is None:
        print(
            "error: library location not configured.\n"
            f"Ask the user where to keep the library (suggested: {suggested_root()}), then run:\n"
            f"  {Path(__file__).resolve()} config --root <dir>",
            file=sys.stderr,
        )
        return EXIT_NOT_CONFIGURED
    try:
        return {"info": cmd_info, "save": cmd_save, "index": cmd_index}[args.cmd](args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
