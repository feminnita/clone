"""
Benchmark do preprocessador — mostra quanto o pipeline comprime o HTML.

Uso:
    python benchmark.py https://exemplo.com
    python benchmark.py https://exemplo.com https://outro.com
"""

import sys
import time

# Força UTF-8 no Windows para caracteres especiais
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from preprocessor import preprocess


def fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def run(url: str):
    print(f"\n{'─'*60}")
    print(f"URL: {url}")
    t0 = time.perf_counter()
    try:
        html, stats = preprocess(url, inline_styles=True)
    except Exception as e:
        print(f"  ERRO: {e}")
        return
    elapsed = time.perf_counter() - t0

    rb = stats.get("css_rules_before", 0)
    ra = stats.get("css_rules_after", 0)
    removed = rb - ra
    pct = int((removed / rb) * 100) if rb else 0
    chars = stats["char_count"]
    limit = 150_000

    spa = "  ← SPA (Playwright)" if stats.get("rendered") else ""
    print(f"  Tempo de preprocessamento : {elapsed:.1f}s{spa}")
    print(f"  CSS externo injetado      : {stats['css_inlined']} arquivo(s)")
    print(f"  Regras CSS antes do purge : {fmt(rb)}")
    print(f"  Regras CSS depois do purge: {fmt(ra)}  ({pct}% removidas)")
    print(f"  Tamanho final do HTML     : {fmt(chars)} chars")
    print(f"  Cabe no limite (150k)?    : {'✓ SIM' if chars <= limit else f'✗ NÃO  — trunca {fmt(chars-limit)} chars'}")

    # Salva para inspeção
    out = f"preprocessed_{url.split('//')[1].split('/')[0].replace('.','_')}.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html[:limit])
    print(f"  Salvo em                  : {out}")


if __name__ == "__main__":
    urls = sys.argv[1:] or ["https://example.com"]
    for url in urls:
        run(url)
    print()
