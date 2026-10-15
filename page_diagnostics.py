"""Diagnostic public de page pour les echecs de detection des marches."""

import json
import logging
from pathlib import Path
import re

from playwright.sync_api import Error as PlaywrightError


def capture_market_diagnostic(page, bookmaker, match_id, marker, output_dir="diagnostics"):
    diagnostic = page.evaluate("""marker => ({
        url: location.href,
        title: document.title,
        readyState: document.readyState,
        hasMarketText: (document.body?.innerText || '').includes(marker),
        headings: Array.from(document.querySelectorAll('h1,h2,h3'))
            .map(el => el.innerText.trim()).filter(Boolean).slice(0, 80),
        textStart: (document.body?.innerText || '').slice(0, 2500),
        textEnd: (document.body?.innerText || '').slice(-2500)
    })""", marker)
    safe_match_id = re.sub(r"[^A-Za-z0-9_-]+", "-", str(match_id)).strip("-") or "unknown"
    output = Path(output_dir) / f"{bookmaker.lower()}-match-{safe_match_id}"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.with_suffix(".json").write_text(
            json.dumps(diagnostic, ensure_ascii=True, indent=2), encoding="utf-8",
        )
    except OSError:
        logging.exception("[%s] Impossible d'enregistrer le diagnostic du marche", bookmaker)
    try:
        page.screenshot(path=str(output.with_suffix(".png")), full_page=True, timeout=15000)
    except (OSError, PlaywrightError):
        logging.exception("[%s] Impossible d'enregistrer la capture du marche", bookmaker)
    logging.warning("[%s] Diagnostic page marche : %s", bookmaker,
                    json.dumps(diagnostic, ensure_ascii=True))
    return diagnostic
