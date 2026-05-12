"""
Site Cloner — Clona qualquer site usando a API do Claude.

Uso:
    python site_cloner.py https://exemplo.com
    python site_cloner.py https://exemplo.com --output meu_clone.html
    python site_cloner.py https://exemplo.com --screenshot  # requer playwright
    python site_cloner.py https://exemplo.com --refine      # refinamento visual (requer playwright)
"""

import argparse
import base64
import re
import sys
from pathlib import Path

import anthropic
from preprocessor import preprocess

# ─── Prompt do sistema ────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an expert web developer specializing in pixel-perfect website cloning.

Your job is to receive the HTML source and/or a screenshot of a website and produce a COMPLETE,
SELF-CONTAINED single HTML file that looks identical to the original.

Rules:
- Output ONLY the HTML file content — no explanations, no markdown code blocks, no preamble.
- The file must work offline: embed all CSS inline or in a <style> tag.
- Use Tailwind CSS via CDN (https://cdn.tailwindcss.com) for utility classes when helpful.
- Use Google Fonts via CDN if custom fonts are needed.
- Replicate exact colors (use the actual hex values from the source), spacing, layout, typography.
- Replicate ALL visible text content exactly as-is.
- For images: use the original src URL if available; otherwise use a placeholder with the same dimensions.
- Make the layout responsive (mobile-first).
- Preserve interactive elements (dropdowns, modals, tabs) with vanilla JavaScript.
- The output must be a valid HTML5 document starting with <!DOCTYPE html>.

Do NOT:
- Skip sections or simplify the layout
- Use "placeholder text" instead of real content
- Omit navigation, footer, or sidebar sections
- Add comments explaining what you did
"""

# ─── Helpers ──────────────────────────────────────────────────────────────────

def fetch_html(url: str, inline_styles: bool = True) -> tuple[str, dict]:
    """Busca e pré-processa o HTML da URL (inline CSS, resolve URLs)."""
    return preprocess(url, inline_styles=inline_styles)


def take_screenshot(url: str) -> bytes | None:
    """Captura screenshot do site (requer: pip install playwright && playwright install chromium)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("⚠  playwright não instalado. Usando apenas HTML.", file=sys.stderr)
        return None

    print("📸  Capturando screenshot...", file=sys.stderr)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(url, wait_until="networkidle", timeout=30_000)
        screenshot = page.screenshot(full_page=True)
        browser.close()
    return screenshot


def build_messages(url: str, html: str, screenshot: bytes | None) -> list[dict]:
    """Monta as mensagens para a API com cache_control no HTML fonte."""
    content: list[dict] = []

    # Screenshot primeiro (visão geral) se disponível
    if screenshot:
        b64 = base64.standard_b64encode(screenshot).decode()
        content.append({
            "type": "text",
            "text": f"Here is a screenshot of the website at {url}:"
        })
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": b64}
        })

    # HTML fonte — cacheável (grande e estável entre tentativas)
    content.append({
        "type": "text",
        "text": (
            f"Here is the HTML source of the website at {url}:\n\n"
            f"```html\n{html[:120_000]}\n```"
        ),
        "cache_control": {"type": "ephemeral"},
    })
    content.append({
        "type": "text",
        "text": "Now produce the complete, self-contained clone HTML file.",
    })

    return [{"role": "user", "content": content}]


# ─── Main ─────────────────────────────────────────────────────────────────────

def clone(url: str, use_screenshot: bool = False) -> str:
    """Clona o site e retorna o HTML resultante."""
    print(f"🌐  Pré-processando {url}...", file=sys.stderr)
    html, stats = fetch_html(url, inline_styles=True)
    spa_note = " · 🤖 SPA (DOM renderizado)" if stats.get("rendered") else ""
    print(
        f"✅  HTML pronto — {stats['char_count']:,} chars, "
        f"{stats['css_inlined']} folhas CSS injetadas{spa_note}",
        file=sys.stderr,
    )

    screenshot = take_screenshot(url) if use_screenshot else None

    client = anthropic.Anthropic()
    messages = build_messages(url, html, screenshot)

    print("🤖  Enviando para o Claude...", file=sys.stderr)

    # Streaming para não dar timeout em respostas longas
    result_parts: list[str] = []
    with client.messages.stream(
        model="claude-sonnet-4-6",
        max_tokens=32_000,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        extra_headers={
            "anthropic-beta": "prompt-caching-2024-07-31,output-128k-2025-02-19",
        },
    ) as stream:
        for text in stream.text_stream:
            result_parts.append(text)
            print(".", end="", flush=True, file=sys.stderr)

    print("\n✅  Clone gerado!", file=sys.stderr)
    return "".join(result_parts)


def main():
    parser = argparse.ArgumentParser(description="Clona um site usando Claude.")
    parser.add_argument("url", help="URL do site a clonar")
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Arquivo de saída (padrão: clone_<dominio>.html)"
    )
    parser.add_argument(
        "--screenshot", "-s",
        action="store_true",
        help="Capturar screenshot além do HTML (requer playwright)"
    )
    parser.add_argument(
        "--refine", "-r",
        action="store_true",
        help="Refinar clone com comparação visual de screenshots (requer playwright)"
    )
    args = parser.parse_args()

    html_output = clone(args.url, use_screenshot=args.screenshot)

    if args.refine:
        from refiner import refine as do_refine
        print("🔍  Refinando com comparação visual…", file=sys.stderr)
        html_output = do_refine(args.url, html_output)
        print("✅  Refinamento concluído!", file=sys.stderr)

    # Remove cercas markdown externas caso o modelo adicione ```html ... ```
    html_output = re.sub(r'^```[\w]*\n?', '', html_output)
    html_output = re.sub(r'\n?```$', '', html_output).strip()

    # Define nome do arquivo de saída
    if args.output:
        out_path = Path(args.output)
    else:
        from urllib.parse import urlparse
        domain = urlparse(args.url).netloc.replace(".", "_").replace("www_", "")
        out_path = Path(f"clone_{domain}.html")

    out_path.write_text(html_output, encoding="utf-8")
    print(f"💾  Salvo em: {out_path.resolve()}", file=sys.stderr)


if __name__ == "__main__":
    main()
