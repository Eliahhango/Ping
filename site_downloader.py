#!/usr/bin/env python3
"""
Simple website downloader for testing your own site.

Usage:
  python site_downloader.py https://www.elitechwiz.site -o downloaded_site --max-pages 2000 --delay 0.1 --include-related-hosts
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import pathlib
import posixpath
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from typing import Iterable
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ASSET_ATTRS = {
    "img": ["src", "srcset"],
    "script": ["src"],
    "source": ["src", "srcset"],
    "video": ["src", "poster"],
    "audio": ["src"],
    "iframe": ["src"],
}

SKIP_SCHEMES = ("mailto:", "tel:", "javascript:", "data:")
TRACKING_QUERY_PREFIXES = ("utm_", "fbclid", "gclid", "mc_")

CSS_URL_RE = re.compile(r"url\(([^)]+)\)", re.IGNORECASE)
CSS_IMPORT_RE = re.compile(r"@import\s+(?:url\()?['\"]?([^'\"\)\s]+)", re.IGNORECASE)


def normalize_url(raw_url: str) -> str:
    clean, _frag = urldefrag(raw_url)
    return clean.strip()


def is_http(url: str) -> bool:
    p = urlparse(url)
    return p.scheme in ("http", "https")


def normalize_netloc(parsed) -> str:
    host = parsed.netloc.lower()
    if host.endswith(":80") and parsed.scheme == "http":
        host = host[:-3]
    elif host.endswith(":443") and parsed.scheme == "https":
        host = host[:-4]
    return host


def simplify_path(path: str) -> str:
    if not path:
        return "/"
    normalized = posixpath.normpath(path)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def drop_tracking_query(query: str) -> str:
    if not query:
        return ""
    kept = []
    for part in query.split("&"):
        key = part.split("=", 1)[0].lower()
        if any(key.startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES):
            continue
        kept.append(part)
    return "&".join(kept)


def canonicalize_url(raw_url: str, keep_query: bool) -> str:
    raw_url = normalize_url(raw_url)
    p = urlparse(raw_url)
    scheme = p.scheme.lower()
    if not scheme or not p.netloc:
        return raw_url
    netloc = normalize_netloc(p)
    path = simplify_path(p.path)
    query = drop_tracking_query(p.query) if keep_query else ""
    return f"{scheme}://{netloc}{path}" + (f"?{query}" if query else "")


def root_domain(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def in_page_scope(url: str, root_host: str, include_related_hosts: bool, allow_external_pages: bool) -> bool:
    if allow_external_pages:
        return is_http(url)
    host = normalize_netloc(urlparse(url))
    if not host:
        return False
    if host == root_host or host == f"www.{root_host}" or root_host == f"www.{host}":
        return True
    if include_related_hosts:
        return root_domain(host) == root_domain(root_host)
    return False


def in_asset_scope(url: str, root_host: str, include_related_hosts: bool, allow_external_assets: bool) -> bool:
    if allow_external_assets:
        return is_http(url)
    return in_page_scope(url, root_host, include_related_hosts, allow_external_pages=False)


def normalize_srcset(srcset: str) -> Iterable[str]:
    for part in srcset.split(","):
        candidate = part.strip().split(" ")[0]
        if candidate:
            yield candidate


def shorten_component(component: str, max_len: int = 120) -> str:
    if len(component) <= max_len:
        return component
    digest = hashlib.sha1(component.encode("utf-8", errors="ignore")).hexdigest()[:12]
    keep = max_len - 15
    return f"{component[:keep]}_{digest}"


def to_local_path(url: str, output_dir: pathlib.Path) -> pathlib.Path:
    p = urlparse(url)
    netloc = normalize_netloc(p)
    path = p.path or "/"

    if path.endswith("/"):
        path = path + "index.html"

    filename = posixpath.basename(path)
    if "." not in filename:
        path = path + "/index.html"

    safe_path = re.sub(r"[<>:\\|?*]", "_", path.lstrip("/"))
    safe_parts = [shorten_component(part) for part in safe_path.split("/") if part]
    safe_path = "/".join(safe_parts) if safe_parts else "index.html"
    local_path = output_dir / netloc / safe_path

    if p.query:
        query_safe = re.sub(r"[^A-Za-z0-9._-]", "_", p.query)
        query_hash = hashlib.sha1(p.query.encode("utf-8", errors="ignore")).hexdigest()[:12]
        query_short = shorten_component(query_safe, max_len=80)
        local_path = local_path.with_name(local_path.name + "__q_" + query_short + "_" + query_hash)

    if len(str(local_path)) > 240:
        digest = hashlib.sha1(str(local_path).encode("utf-8", errors="ignore")).hexdigest()[:16]
        stem = shorten_component(local_path.stem, max_len=48)
        local_path = local_path.with_name(f"{stem}_{digest}{local_path.suffix}")

    return local_path


def save_content(resp: requests.Response, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as f:
        f.write(resp.content)


def is_cloudflare_challenge(resp: requests.Response) -> bool:
    server = (resp.headers.get("Server") or "").lower()
    if "cloudflare" not in server:
        return False
    body = resp.text.lower()
    return "challenge-platform" in body or "just a moment" in body


def log(message: str) -> None:
    try:
        print(message)
    except OSError:
        fallback = message.encode("ascii", errors="backslashreplace").decode("ascii")
        print(fallback)


def extract_asset_links(soup: BeautifulSoup, base_url: str) -> set[str]:
    assets: set[str] = set()
    for tag, attrs in ASSET_ATTRS.items():
        for node in soup.find_all(tag):
            for attr in attrs:
                value = node.get(attr)
                if not value:
                    continue
                if attr == "srcset":
                    for u in normalize_srcset(value):
                        assets.add(urljoin(base_url, u))
                else:
                    assets.add(urljoin(base_url, value))

    allowed_link_rels = {
        "stylesheet",
        "icon",
        "shortcut",
        "apple-touch-icon",
        "apple-touch-startup-image",
        "mask-icon",
        "manifest",
        "preload",
        "modulepreload",
        "prefetch",
    }
    for node in soup.find_all("link", href=True):
        rel_tokens = {token.lower() for token in (node.get("rel") or [])}
        if not rel_tokens:
            continue
        if rel_tokens.intersection(allowed_link_rels):
            assets.add(urljoin(base_url, node["href"]))

    return assets


def extract_css_links(css_text: str, base_url: str) -> set[str]:
    assets: set[str] = set()

    for match in CSS_URL_RE.finditer(css_text):
        value = match.group(1).strip().strip("'\"")
        if not value or value.startswith(SKIP_SCHEMES):
            continue
        assets.add(urljoin(base_url, value))

    for match in CSS_IMPORT_RE.finditer(css_text):
        value = match.group(1).strip().strip("'\"")
        if not value or value.startswith(SKIP_SCHEMES):
            continue
        assets.add(urljoin(base_url, value))

    return assets


def extract_page_links(soup: BeautifulSoup, base_url: str) -> set[str]:
    links: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(SKIP_SCHEMES):
            continue
        links.add(urljoin(base_url, href))

    for form in soup.find_all("form", action=True):
        action = form["action"].strip()
        if action and not action.startswith(SKIP_SCHEMES):
            links.add(urljoin(base_url, action))

    return links


def parse_sitemap_xml(xml_text: str) -> set[str]:
    found: set[str] = set()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return found
    for elem in root.iter():
        if elem.tag.endswith("loc") and elem.text:
            found.add(elem.text.strip())
    return found


def discover_sitemaps(start_url: str, session: requests.Session, timeout: int) -> set[str]:
    p = urlparse(start_url)
    base = f"{p.scheme}://{normalize_netloc(p)}"
    candidates = {
        f"{base}/sitemap.xml",
        f"{base}/sitemap_index.xml",
    }
    robots_url = f"{base}/robots.txt"

    try:
        resp = session.get(robots_url, timeout=timeout)
        if resp.ok:
            for line in resp.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_url = line.split(":", 1)[1].strip()
                    if sitemap_url:
                        candidates.add(sitemap_url)
    except requests.RequestException:
        pass

    return candidates


def collect_urls_from_sitemaps(
    sitemap_urls: set[str],
    session: requests.Session,
    timeout: int,
    root_host: str,
    include_related_hosts: bool,
    allow_external_pages: bool,
) -> set[str]:
    pending = collections.deque(sorted(sitemap_urls))
    visited: set[str] = set()
    page_urls: set[str] = set()
    max_sitemap_docs = 100

    while pending and len(visited) < max_sitemap_docs:
        sitemap_url = pending.popleft()
        sitemap_url = canonicalize_url(sitemap_url, keep_query=False)

        if sitemap_url in visited or not is_http(sitemap_url):
            continue

        visited.add(sitemap_url)

        try:
            resp = session.get(sitemap_url, timeout=timeout)
        except requests.RequestException:
            continue

        if not resp.ok:
            continue

        found = parse_sitemap_xml(resp.text)

        for u in found:
            cu = canonicalize_url(u, keep_query=False)
            if not cu:
                continue
            if cu.endswith(".xml") and "sitemap" in cu:
                if cu not in visited:
                    pending.append(cu)
            elif in_page_scope(cu, root_host, include_related_hosts, allow_external_pages):
                page_urls.add(cu)

    return page_urls


def local_link(from_path: pathlib.Path, to_url: str, output_dir: pathlib.Path) -> str:
    parsed = urlparse(to_url)
    fragment = parsed.fragment
    clean_url = urldefrag(to_url)[0]
    target_path = to_local_path(clean_url, output_dir)
    rel_path = os.path.relpath(target_path, start=from_path.parent).replace("\\", "/")
    if fragment:
        return f"{rel_path}#{fragment}"
    return rel_path


def rewrite_css_to_local(
    css_text: str,
    source_url: str,
    source_path: pathlib.Path,
    output_dir: pathlib.Path,
    root_host: str,
    include_related_hosts: bool,
    allow_external_assets: bool,
) -> tuple[str, set[str]]:
    discovered: set[str] = set()

    def replace_url_match(match: re.Match[str]) -> str:
        raw = match.group(1).strip()
        quote = ""
        if (raw.startswith("'") and raw.endswith("'")) or (raw.startswith('"') and raw.endswith('"')):
            quote = raw[0]
            raw = raw[1:-1]

        value = raw.strip()
        if not value or value.startswith(SKIP_SCHEMES):
            return match.group(0)

        absolute = urljoin(source_url, value)
        candidate = canonicalize_url(absolute, keep_query=True)
        if not candidate or not in_asset_scope(candidate, root_host, include_related_hosts, allow_external_assets):
            return match.group(0)

        discovered.add(candidate)
        rewritten = local_link(source_path, absolute, output_dir)
        return f"url({quote}{rewritten}{quote})"

    def replace_import_match(match: re.Match[str]) -> str:
        value = match.group(1).strip().strip("'\"")
        if not value or value.startswith(SKIP_SCHEMES):
            return match.group(0)

        absolute = urljoin(source_url, value)
        candidate = canonicalize_url(absolute, keep_query=True)
        if not candidate or not in_asset_scope(candidate, root_host, include_related_hosts, allow_external_assets):
            return match.group(0)

        discovered.add(candidate)
        rewritten = local_link(source_path, absolute, output_dir)
        return match.group(0).replace(match.group(1), rewritten)

    rewritten = CSS_URL_RE.sub(replace_url_match, css_text)
    rewritten = CSS_IMPORT_RE.sub(replace_import_match, rewritten)
    return rewritten, discovered


def rewrite_html_to_local(
    soup: BeautifulSoup,
    page_url: str,
    page_path: pathlib.Path,
    output_dir: pathlib.Path,
    root_host: str,
    include_related_hosts: bool,
    keep_query_pages: bool,
    allow_external_pages: bool,
    allow_external_assets: bool,
) -> set[str]:
    discovered_assets: set[str] = set()
    for node in soup.find_all("a", href=True):
        href = node["href"].strip()
        if not href or href.startswith(SKIP_SCHEMES):
            continue
        absolute = urljoin(page_url, href)
        page_candidate = canonicalize_url(absolute, keep_query=keep_query_pages)
        if page_candidate and in_page_scope(page_candidate, root_host, include_related_hosts, allow_external_pages):
            node["href"] = local_link(page_path, absolute, output_dir)

    for node in soup.find_all("form", action=True):
        action = node["action"].strip()
        if not action or action.startswith(SKIP_SCHEMES):
            continue
        absolute = urljoin(page_url, action)
        page_candidate = canonicalize_url(absolute, keep_query=keep_query_pages)
        if page_candidate and in_page_scope(page_candidate, root_host, include_related_hosts, allow_external_pages):
            node["action"] = local_link(page_path, absolute, output_dir)

    for tag, attrs in ASSET_ATTRS.items():
        for node in soup.find_all(tag):
            for attr in attrs:
                value = node.get(attr)
                if not value:
                    continue

                if attr == "srcset":
                    rewritten_parts = []
                    for part in value.split(","):
                        raw = part.strip()
                        if not raw:
                            continue
                        pieces = raw.split()
                        candidate = pieces[0]
                        descriptor = " ".join(pieces[1:])
                        if candidate.startswith(SKIP_SCHEMES):
                            rewritten_parts.append(raw)
                            continue
                        absolute = urljoin(page_url, candidate)
                        asset_candidate = canonicalize_url(absolute, keep_query=True)
                        if asset_candidate and in_asset_scope(asset_candidate, root_host, include_related_hosts, allow_external_assets):
                            local = local_link(page_path, absolute, output_dir)
                            rewritten_parts.append(f"{local} {descriptor}".strip())
                        else:
                            rewritten_parts.append(raw)
                    if rewritten_parts:
                        node[attr] = ", ".join(rewritten_parts)
                    continue

                if value.startswith(SKIP_SCHEMES):
                    continue
                absolute = urljoin(page_url, value)
                asset_candidate = canonicalize_url(absolute, keep_query=True)
                if asset_candidate and in_asset_scope(asset_candidate, root_host, include_related_hosts, allow_external_assets):
                    node[attr] = local_link(page_path, absolute, output_dir)
                    discovered_assets.add(asset_candidate)

    allowed_link_rels = {
        "stylesheet",
        "icon",
        "shortcut",
        "apple-touch-icon",
        "apple-touch-startup-image",
        "mask-icon",
        "manifest",
        "preload",
        "modulepreload",
        "prefetch",
    }
    for node in soup.find_all("link", href=True):
        rel_tokens = {token.lower() for token in (node.get("rel") or [])}
        if not rel_tokens:
            continue
        if not rel_tokens.intersection(allowed_link_rels):
            continue
        href = node["href"].strip()
        if not href or href.startswith(SKIP_SCHEMES):
            continue
        absolute = urljoin(page_url, href)
        asset_candidate = canonicalize_url(absolute, keep_query=True)
        if asset_candidate and in_asset_scope(asset_candidate, root_host, include_related_hosts, allow_external_assets):
            node["href"] = local_link(page_path, absolute, output_dir)
            discovered_assets.add(asset_candidate)

    for node in soup.find_all(style=True):
        rewritten_style, style_assets = rewrite_css_to_local(
            css_text=node["style"],
            source_url=page_url,
            source_path=page_path,
            output_dir=output_dir,
            root_host=root_host,
            include_related_hosts=include_related_hosts,
            allow_external_assets=allow_external_assets,
        )
        node["style"] = rewritten_style
        discovered_assets.update(style_assets)

    for node in soup.find_all("style"):
        css_text = node.string
        if not css_text:
            continue
        rewritten_css, style_assets = rewrite_css_to_local(
            css_text=css_text,
            source_url=page_url,
            source_path=page_path,
            output_dir=output_dir,
            root_host=root_host,
            include_related_hosts=include_related_hosts,
            allow_external_assets=allow_external_assets,
        )
        node.string.replace_with(rewritten_css)
        discovered_assets.update(style_assets)

    return discovered_assets


def prepare_vercel_bundle(output_dir: pathlib.Path, root_host: str) -> pathlib.Path:
    source_root = output_dir / root_host
    if not source_root.exists() or not source_root.is_dir():
        raise RuntimeError(f"Primary host folder not found: {source_root}")

    deploy_dir = output_dir / "_vercel_deploy"
    if deploy_dir.exists():
        shutil.rmtree(deploy_dir)
    deploy_dir.mkdir(parents=True, exist_ok=True)

    # Flatten the primary host to deployment root so / maps directly to the site.
    for child in source_root.iterdir():
        target = deploy_dir / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)

    # Keep downloaded external host assets (CDN mirrors) available at the same paths.
    for child in output_dir.iterdir():
        if child.name in {root_host, "_vercel_deploy"}:
            continue
        target = deploy_dir / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        elif child.is_file():
            shutil.copy2(child, target)

    # Create .html aliases so routes like /about can map cleanly to /about.html on static hosts.
    for index_file in deploy_dir.rglob("index.html"):
        rel_parent = index_file.parent.relative_to(deploy_dir)
        if str(rel_parent) == ".":
            continue
        alias_file = deploy_dir / (rel_parent.as_posix() + ".html")
        alias_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(index_file, alias_file)

    vercel_config = {
        "version": 2,
        "cleanUrls": False,
        "trailingSlash": False,
        "routes": [
            {"handle": "filesystem"},
            {"src": "^/(.+)/?$", "dest": "/$1.html"},
            {"src": "^/$", "dest": "/index.html"},
        ],
    }
    (deploy_dir / "vercel.json").write_text(json.dumps(vercel_config, indent=2), encoding="utf-8")

    return deploy_dir


def run_checked(command: list[str], cwd: pathlib.Path) -> None:
    subprocess.run(command, cwd=str(cwd), check=True)


def auto_publish_bundle(
    deploy_dir: pathlib.Path,
    github_repo_url: str,
    git_branch: str,
    git_commit_message: str,
    deploy_vercel: bool,
) -> None:
    git_dir = deploy_dir / ".git"
    if not git_dir.exists():
        run_checked(["git", "init", "-b", git_branch], cwd=deploy_dir)

    run_checked(["git", "add", "-A"], cwd=deploy_dir)

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(deploy_dir),
        capture_output=True,
        text=True,
        check=True,
    )
    if status.stdout.strip():
        run_checked(["git", "commit", "-m", git_commit_message], cwd=deploy_dir)

    existing_remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(deploy_dir),
        capture_output=True,
        text=True,
    )
    if existing_remote.returncode == 0:
        run_checked(["git", "remote", "set-url", "origin", github_repo_url], cwd=deploy_dir)
    else:
        run_checked(["git", "remote", "add", "origin", github_repo_url], cwd=deploy_dir)

    run_checked(["git", "push", "-u", "origin", git_branch], cwd=deploy_dir)

    if deploy_vercel:
        run_checked(["vercel", "--prod", "--yes"], cwd=deploy_dir)


def download_site(
    start_url: str,
    output_dir: pathlib.Path,
    max_pages: int,
    delay: float,
    timeout: int,
    user_agent: str,
    include_related_hosts: bool,
    keep_query_pages: bool,
    allow_external_pages: bool,
    allow_external_assets: bool,
    prepare_vercel: bool,
    auto_publish: bool,
    github_repo_url: str | None,
    git_branch: str,
    git_commit_message: str,
    deploy_vercel: bool,
) -> None:
    start_url = canonicalize_url(start_url, keep_query=keep_query_pages)
    parsed_start = urlparse(start_url)

    if not is_http(start_url):
        raise ValueError("Start URL must be HTTP/HTTPS.")

    root_host = normalize_netloc(parsed_start)
    output_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})

    queue = collections.deque([start_url])
    queued: set[str] = {start_url}
    asset_queue: collections.deque[str] = collections.deque()
    queued_assets: set[str] = set()
    seen_pages: set[str] = set()
    downloaded_assets: set[str] = set()

    def enqueue_asset(asset_url: str) -> None:
        candidate = canonicalize_url(asset_url, keep_query=True)
        if (
            not candidate
            or candidate in downloaded_assets
            or candidate in queued_assets
            or not is_http(candidate)
            or not in_asset_scope(candidate, root_host, include_related_hosts, allow_external_assets)
        ):
            return
        asset_queue.append(candidate)
        queued_assets.add(candidate)

    sitemap_urls = discover_sitemaps(start_url, session, timeout)
    seeded_pages = collect_urls_from_sitemaps(
        sitemap_urls=sitemap_urls,
        session=session,
        timeout=timeout,
        root_host=root_host,
        include_related_hosts=include_related_hosts,
        allow_external_pages=allow_external_pages,
    )

    for page in sorted(seeded_pages):
        if page not in queued:
            queue.append(page)
            queued.add(page)

    while queue and len(seen_pages) < max_pages:
        url = queue.popleft()
        url = canonicalize_url(url, keep_query=keep_query_pages)

        if not url or url in seen_pages:
            continue

        if not is_http(url) or not in_page_scope(url, root_host, include_related_hosts, allow_external_pages):
            continue

        try:
            resp = session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            log(f"[WARN] Failed page {url}: {exc}")
            continue

        if not resp.ok:
            log(f"[WARN] Skipping page {url}: HTTP {resp.status_code}")
            continue

        if is_cloudflare_challenge(resp):
            log(f"[WARN] Cloudflare challenge detected at {url}. Automated crawling is blocked for this host.")
            seen_pages.add(url)
            continue

        ct = resp.headers.get("Content-Type", "")
        if "text/html" not in ct:
            if url not in downloaded_assets:
                path = to_local_path(url, output_dir)
                save_content(resp, path)
                downloaded_assets.add(url)
                log(f"[ASSET] {url} -> {path}")
            continue

        seen_pages.add(url)
        page_path = to_local_path(url, output_dir)
        soup = BeautifulSoup(resp.text, "html.parser")

        for link in extract_page_links(soup, url):
            link = canonicalize_url(link, keep_query=keep_query_pages)
            if (
                link
                and in_page_scope(link, root_host, include_related_hosts, allow_external_pages)
                and link not in seen_pages
                and link not in queued
            ):
                queue.append(link)
                queued.add(link)

        for asset_url in extract_asset_links(soup, url):
            enqueue_asset(asset_url)

        html_inline_assets = rewrite_html_to_local(
            soup=soup,
            page_url=url,
            page_path=page_path,
            output_dir=output_dir,
            root_host=root_host,
            include_related_hosts=include_related_hosts,
            keep_query_pages=keep_query_pages,
            allow_external_pages=allow_external_pages,
            allow_external_assets=allow_external_assets,
        )
        for asset_url in html_inline_assets:
            enqueue_asset(asset_url)

        page_path.parent.mkdir(parents=True, exist_ok=True)
        page_path.write_text(str(soup), encoding="utf-8")
        log(f"[PAGE] {url} -> {page_path}")

        while asset_queue:
            asset_url = asset_queue.popleft()

            try:
                asset_resp = session.get(asset_url, timeout=timeout)
                if not asset_resp.ok:
                    log(f"[WARN] Skipping asset {asset_url}: HTTP {asset_resp.status_code}")
                    continue

                asset_ct = (asset_resp.headers.get("Content-Type", "") or "").lower()
                if "text/html" in asset_ct:
                    page_candidate = canonicalize_url(asset_url, keep_query=keep_query_pages)
                    if (
                        page_candidate
                        and page_candidate not in seen_pages
                        and page_candidate not in queued
                        and in_page_scope(page_candidate, root_host, include_related_hosts, allow_external_pages)
                    ):
                        queue.append(page_candidate)
                        queued.add(page_candidate)
                    continue

                asset_path = to_local_path(asset_url, output_dir)
                is_css = "text/css" in asset_ct or urlparse(asset_url).path.lower().endswith(".css")
                if is_css:
                    rewritten_css, nested_assets = rewrite_css_to_local(
                        css_text=asset_resp.text,
                        source_url=asset_url,
                        source_path=asset_path,
                        output_dir=output_dir,
                        root_host=root_host,
                        include_related_hosts=include_related_hosts,
                        allow_external_assets=allow_external_assets,
                    )
                    asset_path.parent.mkdir(parents=True, exist_ok=True)
                    asset_path.write_text(rewritten_css, encoding="utf-8")
                    for nested in nested_assets:
                        enqueue_asset(nested)
                else:
                    save_content(asset_resp, asset_path)

                downloaded_assets.add(asset_url)
                log(f"[ASSET] {asset_url} -> {asset_path}")
            except requests.RequestException as exc:
                log(f"[WARN] Failed asset {asset_url}: {exc}")

        if delay > 0:
            time.sleep(delay)

    log("\nDone.")
    log(f"Pages downloaded: {len(seen_pages)}")
    log(f"Assets downloaded: {len(downloaded_assets)}")
    log(f"Saved to: {output_dir.resolve()}")

    if prepare_vercel:
        try:
            deploy_dir = prepare_vercel_bundle(output_dir=output_dir, root_host=root_host)
            log(f"Vercel bundle ready: {deploy_dir.resolve()}")
            log("Deploy with: vercel --cwd \"" + str(deploy_dir.resolve()) + "\"")

            if auto_publish:
                if not github_repo_url:
                    log("[WARN] Auto-publish enabled but --github-repo-url was not provided. Skipping publish.")
                else:
                    try:
                        auto_publish_bundle(
                            deploy_dir=deploy_dir,
                            github_repo_url=github_repo_url,
                            git_branch=git_branch,
                            git_commit_message=git_commit_message,
                            deploy_vercel=deploy_vercel,
                        )
                        log("Auto-publish completed (GitHub push" + (" + Vercel deploy" if deploy_vercel else "") + ").")
                    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                        log(f"[WARN] Auto-publish failed: {exc}")
        except Exception as exc:
            log(f"[WARN] Vercel bundle preparation failed: {exc}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download pages and assets from a website you own or are authorized to test.")
    parser.add_argument("url", help="Start URL, e.g. https://www.elitechwiz.site")
    parser.add_argument("-o", "--output", default="downloaded_site", help="Output directory (default: downloaded_site)")
    parser.add_argument("--max-pages", type=int, default=2000, help="Maximum HTML pages to crawl (default: 2000)")
    parser.add_argument("--delay", type=float, default=0.1, help="Delay between page requests in seconds (default: 0.1)")
    parser.add_argument("--timeout", type=int, default=20, help="HTTP timeout seconds (default: 20)")
    parser.add_argument(
        "--include-related-hosts",
        action="store_true",
        help="Also crawl hosts under the same root domain (e.g. preview.example.com + www.example.com).",
    )
    parser.add_argument(
        "--keep-query-pages",
        action="store_true",
        help="Treat query-string page URLs as unique pages. Off by default to avoid duplicate crawl loops.",
    )
    parser.add_argument(
        "--allow-external-pages",
        action="store_true",
        help="Allow crawling pages/assets on other hosts discovered from the start page (use carefully).",
    )
    parser.add_argument(
        "--allow-external-assets",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Allow downloading non-HTML assets from other hosts (CDNs). Default: enabled. Use --no-allow-external-assets to disable.",
    )
    parser.add_argument(
        "--prepare-vercel",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Build a deployment-ready static bundle in output/_vercel_deploy (default: enabled).",
    )
    parser.add_argument(
        "--auto-publish",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="After preparing _vercel_deploy, commit/push to GitHub automatically.",
    )
    parser.add_argument(
        "--github-repo-url",
        default=None,
        help="GitHub remote URL used by --auto-publish, e.g. https://github.com/<user>/<repo>.git",
    )
    parser.add_argument(
        "--git-branch",
        default="main",
        help="Git branch used by --auto-publish (default: main).",
    )
    parser.add_argument(
        "--git-commit-message",
        default="Update mirrored site",
        help="Commit message used by --auto-publish.",
    )
    parser.add_argument(
        "--deploy-vercel",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="When auto-publishing, also run 'vercel --prod --yes' in _vercel_deploy.",
    )
    parser.add_argument(
        "--user-agent",
        default="Mozilla/5.0 (compatible; SiteDownloader/1.0)",
        help="User-Agent string",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    download_site(
        start_url=args.url,
        output_dir=pathlib.Path(args.output),
        max_pages=args.max_pages,
        delay=args.delay,
        timeout=args.timeout,
        user_agent=args.user_agent,
        include_related_hosts=args.include_related_hosts,
        keep_query_pages=args.keep_query_pages,
        allow_external_pages=args.allow_external_pages,
        allow_external_assets=args.allow_external_assets,
        prepare_vercel=args.prepare_vercel,
        auto_publish=args.auto_publish,
        github_repo_url=args.github_repo_url,
        git_branch=args.git_branch,
        git_commit_message=args.git_commit_message,
        deploy_vercel=args.deploy_vercel,
    )


if __name__ == "__main__":
    main()
