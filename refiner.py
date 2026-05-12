"""
Refinador de clones — compara screenshot do original vs clone e pede ao Claude
que corrija as diferenças visuais.

Requer: pip install playwright && playwright install chromium
"""

import base64
import re

import anthropic

REFINE_SYSTEM = """You are an expert web developer fixing a cloned website.

You will receive:
1. A screenshot of the ORIGINAL website
2. A screenshot of your previous CLONE attempt
3. The HTML source of your clone

Your job: produce an improved HTML clone that is visually closer to the original.

Focus ONLY on visible differences:
- Wrong colors, backgrounds, or gradients
- Layout shifts (columns, grid, flexbox issues)
- Missing sections or elements
- Font size / weight / family mismatches
- Broken or missing images (use the original's absolute URL)
- Spacing / padding / margin issues

Rules:
- Output ONLY the complete HTML file — no explanations, no markdown fences.
- Keep everything that already looks correct.
- The output must be a valid HTML5 document starting with <!DOCTYPE html>.
"""


def screenshot_url(url: str) -> bytes | None:
    """Captura screenshot de uma URL (requer playwright)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            page.goto(url, wait_until="networkidle", timeout=25_000)
            page.wait_for_timeout(1500)  # deixa animações CSS terminarem
            data = page.screenshot(full_page=False)  # viewport apenas (mais rápido)
        except Exception:
            data = None
        finally:
            browser.close()
    return data


def screenshot_html(html: str) -> bytes | None:
    """Captura screenshot de HTML inline (requer playwright)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        try:
            page.set_content(html, wait_until="networkidle", timeout=20_000)
            page.wait_for_timeout(1000)
            data = page.screenshot(full_page=False)
        except Exception:
            data = None
        finally:
            browser.close()
    return data


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode()


def refine(
    original_url: str,
    clone_html: str,
    original_screenshot: bytes | None = None,
    clone_screenshot: bytes | None = None,
) -> str:
    """
    Envia os dois screenshots + HTML clone ao Claude e retorna o HTML refinado.

    Se os screenshots não forem fornecidos, tenta capturá-los automaticamente
    (requer playwright). Se playwright não estiver disponível, refina só pelo HTML.
    """
    # Tira screenshots se não fornecidos
    if original_screenshot is None:
        original_screenshot = screenshot_url(original_url)
    if clone_screenshot is None:
        clone_screenshot = screenshot_html(clone_html)

    content: list[dict] = []

    if original_screenshot and clone_screenshot:
        content += [
            {"type": "text", "text": "ORIGINAL website screenshot:"},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": _b64(original_screenshot)
            }},
            {"type": "text", "text": "YOUR CLONE screenshot:"},
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/png",
                "data": _b64(clone_screenshot)
            }},
        ]
    else:
        content.append({
            "type": "text",
            "text": "(Screenshots unavailable — refining based on HTML structure only.)"
        })

    content.append({
        "type": "text",
        "text": (
            f"Original URL: {original_url}\n\n"
            f"Your previous clone HTML:\n```html\n{clone_html[:100_000]}\n```"
        ),
        "cache_control": {"type": "ephemeral"},
    })
    content.append({
        "type": "text",
        "text": "Now produce the improved HTML clone that fixes all visible differences.",
    })

    client = anthropic.Anthropic()
    parts: list[str] = []

    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=32_000,
        system=[{"type": "text", "text": REFINE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        extra_headers={
            "anthropic-beta": "prompt-caching-2024-07-31,output-128k-2025-02-19",
        },
    ) as stream:
        for text in stream.text_stream:
            parts.append(text)

    result = "".join(parts)

    # Remove cercas markdown externas residuais (```html ... ```)
    result = re.sub(r'^```[\w]*\n?', '', result)
    result = re.sub(r'\n?```$', '', result)

    return result.strip()
