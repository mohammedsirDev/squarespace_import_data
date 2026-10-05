import html
import re
from typing import Any, Dict, List


def clean_html(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"</(p|li|ul|h[1-6]|div)>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{2,}", "\n", html.unescape(text)).strip()


def extract_clean_description(item: Dict[str, Any]) -> str:
    raw_html = item.get("excerpt") or item.get("body") or ""
    if not raw_html:
        return (item.get("seoData") or {}).get("seoDescription", "")

    without_ficha = re.split(
        r"<h[1-6][^>]*>\s*Ficha\s+t[eé]cnica\s*</h[1-6]>",
        raw_html,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]

    without_ul = re.sub(r"<ul[^>]*>.*?</ul>", "", without_ficha, flags=re.I | re.S)
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", without_ul, flags=re.I | re.S)
    narrative_paragraphs: List[str] = []
    for p in paragraphs:
        if re.match(r"^\s*<strong>[^<]{1,35}</strong>\s*:?\s*[^<]+$", p.strip(), flags=re.I):
            continue
        clean_p = clean_html(p)
        if clean_p:
            narrative_paragraphs.append(clean_p)

    if narrative_paragraphs:
        return "\n\n".join(narrative_paragraphs)

    return clean_html(without_ul) or clean_html(raw_html)