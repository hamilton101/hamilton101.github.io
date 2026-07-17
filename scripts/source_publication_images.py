from __future__ import annotations

import argparse
import hashlib
import html
import os
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup
from PIL import Image, ImageOps, UnidentifiedImageError


ROOT = Path(__file__).resolve().parents[1]
IMAGE_DIR = ROOT / "images" / "publications"
OUTPUT = ROOT / "_data" / "publication_images.yaml"
CITATIONS = ROOT / "_data" / "citations.yaml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def safe_name(value: str) -> str:
    value = value.split(":", 1)[-1]
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value)
    return value.strip("-") or hashlib.sha1(value.encode()).hexdigest()[:12]


def clean_text(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def load_citations(limit: int | None = None) -> list[dict]:
    citations = yaml.safe_load(CITATIONS.read_text(encoding="utf-8"))
    if limit:
        citations = citations[:limit]
    return citations


def request(session: requests.Session, url: str, *, stream: bool = False, timeout: int = 25):
    return session.get(url, headers=HEADERS, stream=stream, timeout=timeout, allow_redirects=True)


def scholar_detail(session: requests.Session, citation: dict) -> dict:
    link = citation.get("link") or ""
    if not link:
        return {}
    try:
        response = request(session, link)
    except requests.RequestException as exc:
        return {"status": "missing", "reason": f"scholar detail fetch failed: {exc}"}
    if response.status_code >= 400:
        return {"status": "missing", "reason": f"scholar detail status {response.status_code}"}

    soup = BeautifulSoup(response.text, "html.parser")
    title_link = soup.select_one("a.gsc_oci_title_link")
    fields = {}
    for row in soup.select("#gsc_oci_table .gs_scl"):
        key = row.select_one(".gsc_oci_field")
        value = row.select_one(".gsc_oci_value")
        if key and value:
            fields[clean_text(key.get_text(" "))] = clean_text(value.get_text(" "))
    return {
        "official_url": urljoin("https://scholar.google.com", title_link.get("href", "")) if title_link else "",
        "scholar_fields": fields,
    }


def candidate_sciencedirect_figures(url: str) -> list[str]:
    match = re.search(r"/pii/(S\d+)", url)
    if not match:
        return []
    pii = match.group(1)
    base = f"https://ars.els-cdn.com/content/image/1-s2.0-{pii}"
    candidates = [f"{base}-fx1_lrg.jpg"]
    candidates.extend(f"{base}-gr{i}_lrg.jpg" for i in range(1, 11))
    return candidates


def candidate_mdpi_assets(url: str) -> tuple[list[str], list[str]]:
    parsed = urlparse(url)
    if "mdpi.com" not in parsed.netloc:
        return [], []
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        return [], []

    journal_by_issn = {
        "2072-4292": "remotesensing",
    }
    journal = journal_by_issn.get(parts[0])
    if not journal:
        return [], []
    volume = parts[1]
    article = parts[3]
    article_padded = article.zfill(5)
    base = f"https://pub.mdpi-res.com/{journal}/{journal}-{volume}-{article_padded}/article_deploy/html/images/{journal}-{volume}-{article_padded}"
    images = [f"{base}-g{i:03d}.png" for i in range(1, 11)]
    pdfs = [f"https://www.mdpi.com/{parts[0]}/{parts[1]}/{parts[2]}/{parts[3]}/pdf?download=1"]
    return images, pdfs


def provider_pdf_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    candidates: list[str] = []

    if "ieeexplore.ieee.org" in parsed.netloc:
        match = re.search(r"/document/(\d+)", parsed.path)
        if match:
            candidates.append(f"https://ieeexplore.ieee.org/stamp/stamp.jsp?tp=&arnumber={match.group(1)}")

    if "ascelibrary.org" in parsed.netloc and "/doi/abs/" in parsed.path:
        candidates.append(url.replace("/doi/abs/", "/doi/pdf/"))

    if "onepetro.org" in parsed.netloc:
        candidates.append(url.replace("/conference-paper/", "/conference-paper-pdf/"))

    return candidates


def likely_article_image(src: str) -> bool:
    lower = src.lower()
    if any(term in lower for term in ["logo", "icon", "sprite", "avatar", "profile", "facebook", "twitter", "loading", "spinner", "placeholder"]):
        return False
    return any(
        term in lower
        for term in [
            "figure",
            "fig",
            "media",
            "image",
            "article",
            "gr",
            "fx",
            "thumb",
            "patent",
            "download",
            "cms",
            "static",
        ]
    )


def html_candidates(session: requests.Session, url: str) -> tuple[list[str], list[str]]:
    try:
        response = request(session, url)
    except requests.RequestException:
        return [], []
    content_type = response.headers.get("content-type", "")
    if "pdf" in content_type.lower() or response.content[:4] == b"%PDF":
        return [], [response.url]
    if response.status_code >= 400:
        return [], []

    soup = BeautifulSoup(response.text, "html.parser")
    image_urls: list[str] = []
    pdf_urls: list[str] = []

    for selector in [
        'meta[name="citation_pdf_url"]',
        'meta[property="citation_pdf_url"]',
        'meta[name="dc.identifier"][content$=".pdf"]',
    ]:
        for meta in soup.select(selector):
            content = meta.get("content")
            if content:
                pdf_urls.append(urljoin(response.url, content))

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        text = clean_text(anchor.get_text(" "))
        if ".pdf" in href.lower() or "pdf" in text.lower():
            pdf_urls.append(urljoin(response.url, href))

    for selector in [
        'meta[property="og:image"]',
        'meta[name="twitter:image"]',
        'meta[property="twitter:image"]',
        'meta[itemprop="image"]',
    ]:
        for meta in soup.select(selector):
            content = meta.get("content")
            if content and likely_article_image(content):
                image_urls.append(urljoin(response.url, content))

    for img in soup.find_all("img"):
        src = img.get("data-src") or img.get("data-original") or img.get("src")
        if not src:
            srcset = img.get("srcset") or ""
            if srcset:
                src = srcset.split(",")[-1].strip().split(" ")[0]
        alt = clean_text(img.get("alt", ""))
        cls = " ".join(img.get("class", []))
        combined = " ".join([src or "", alt, cls])
        if src and likely_article_image(combined):
            image_urls.append(urljoin(response.url, src))

    return list(dict.fromkeys(image_urls)), list(dict.fromkeys(pdf_urls))


def download_image(session: requests.Session, url: str, destination: Path) -> bool:
    try:
        response = request(session, url, stream=True)
    except requests.RequestException:
        return False
    if response.status_code >= 400:
        return False
    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type:
        return False
    raw = destination.with_suffix(".download")
    with raw.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=65536):
            if chunk:
                handle.write(chunk)
    try:
        with Image.open(raw) as img:
            if img.width < 120 or img.height < 90:
                raw.unlink(missing_ok=True)
                return False
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, "white")
                background.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
                img = background
            else:
                img = img.convert("RGB")
            img.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
            img.save(destination, "JPEG", quality=88, optimize=True)
    except (UnidentifiedImageError, OSError):
        raw.unlink(missing_ok=True)
        return False
    raw.unlink(missing_ok=True)
    return True


def download_pdf_thumbnail(session: requests.Session, url: str, destination: Path) -> bool:
    try:
        response = request(session, url, stream=True, timeout=35)
    except requests.RequestException:
        return False
    if response.status_code >= 400:
        return False

    with tempfile.TemporaryDirectory(dir=IMAGE_DIR) as tmp:
        tmpdir = Path(tmp)
        pdf = tmpdir / "paper.pdf"
        with pdf.open("wb") as handle:
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                if chunk:
                    total += len(chunk)
                    if total > 40_000_000:
                        return False
                    handle.write(chunk)
        if pdf.read_bytes()[:4] != b"%PDF":
            return False
        prefix = tmpdir / "page"
        pdftoppm = pdftoppm_command()
        if not pdftoppm:
            return False
        try:
            subprocess.run(
                [pdftoppm, "-f", "1", "-singlefile", "-jpeg", "-r", "144", str(pdf), str(prefix)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            return False
        page = prefix.with_suffix(".jpg")
        if not page.exists():
            return False
        with Image.open(page) as img:
            img = ImageOps.exif_transpose(img).convert("RGB")
            img.thumbnail((1400, 1400), Image.Resampling.LANCZOS)
            img.save(destination, "JPEG", quality=88, optimize=True)
    return True


def pdftoppm_command() -> str:
    configured = os.environ.get("PDFTOPPM")
    candidates = [configured] if configured else []
    runtime = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
    candidates.append(str(runtime))
    discovered = shutil.which("pdftoppm")
    if discovered:
        candidates.append(discovered)

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return ""


def resolve_thumbnail(session: requests.Session, citation: dict, detail: dict) -> dict:
    official_url = detail.get("official_url") or ""
    scholar_id = citation.get("scholar_id") or safe_name(citation.get("id", "paper"))
    filename = f"{safe_name(scholar_id)}.jpg"
    destination = IMAGE_DIR / filename

    image_candidates: list[str] = []
    pdf_candidates: list[str] = []
    if official_url:
        image_candidates.extend(candidate_sciencedirect_figures(official_url))
        mdpi_images, mdpi_pdfs = candidate_mdpi_assets(official_url)
        image_candidates.extend(mdpi_images)
        pdf_candidates.extend(mdpi_pdfs)
        pdf_candidates.extend(provider_pdf_candidates(official_url))
        more_images, more_pdfs = html_candidates(session, official_url)
        image_candidates.extend(more_images)
        pdf_candidates.extend(more_pdfs)

    for image_url in list(dict.fromkeys(image_candidates)):
        if download_image(session, image_url, destination):
            return {
                "image": f"images/publications/{filename}",
                "source_url": image_url,
                "official_url": official_url,
                "source_type": "paper-image",
                "status": "found",
            }

    for pdf_url in list(dict.fromkeys(pdf_candidates)):
        if download_pdf_thumbnail(session, pdf_url, destination):
            return {
                "image": f"images/publications/{filename}",
                "source_url": pdf_url,
                "official_url": official_url,
                "source_type": "paper-pdf-first-page",
                "status": "found",
            }

    return {
        "official_url": official_url,
        "source_type": "unavailable",
        "status": "missing",
        "reason": "no accessible official image or PDF thumbnail found",
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--delay", type=float, default=0.25)
    args = parser.parse_args()

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    records = []
    existing_by_id = {}
    if OUTPUT.exists():
        existing_records = yaml.safe_load(OUTPUT.read_text(encoding="utf-8")) or []
        existing_by_id = {record.get("scholar_id"): record for record in existing_records}

    for index, citation in enumerate(load_citations(args.limit), start=1):
        scholar_id = citation.get("scholar_id") or ""
        print(f"[{index:02d}] {citation.get('title', '')[:90]}")
        existing = existing_by_id.get(scholar_id, {})
        detail = scholar_detail(session, citation)
        if not detail.get("official_url") and existing.get("official_url"):
            detail["official_url"] = existing["official_url"]
        result = resolve_thumbnail(session, citation, detail)
        existing_image = ROOT / existing.get("image", "")
        if result.get("status") != "found" and existing.get("status") == "found" and existing_image.exists():
            result = {key: value for key, value in existing.items() if key not in ["scholar_id", "title"]}
        records.append(
            {
                "scholar_id": scholar_id,
                "title": citation.get("title", ""),
                **result,
            }
        )
        print(f"     {result.get('status')} {result.get('source_type')} {result.get('source_url', '')}")
        time.sleep(args.delay)

    header = (
        "# Paper-derived thumbnails for Google Scholar-synced publications.\n"
        "# Generated by scripts/source_publication_images.py from official paper, patent, or PDF sources.\n"
    )
    OUTPUT.write_text(
        header + yaml.safe_dump(records, sort_keys=False, allow_unicode=True, width=1200),
        encoding="utf-8",
        newline="\n",
    )
    found = sum(1 for record in records if record.get("status") == "found")
    print(f"Wrote {OUTPUT} with {found}/{len(records)} sourced thumbnails.")


if __name__ == "__main__":
    main()
