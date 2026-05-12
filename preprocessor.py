"""
Preprocessador de HTML — prepara o site para clonagem fiel.

Pipeline:
  1. Busca HTML bruto
  2. Resolve URLs relativas → absolutas
  3. Remove scripts de tracking
  4. Inline + minifica CSS externo
  5. Purge CSS — remove regras cujos seletores não existem no HTML
  6. Retorna HTML enriquecido + stats
"""

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# Cliente compartilhado — reutiliza conexões TCP entre chamadas paralelas
_CLIENT = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=15)

# Domínios de tracking para remover (scripts inúteis para o clone)
SKIP_SCRIPT_DOMAINS = {
    "googletagmanager.com", "google-analytics.com", "hotjar.com",
    "facebook.net", "twitter.com", "doubleclick.net", "adsbygoogle",
    "clarity.ms", "segment.com", "intercom.io", "crisp.chat",
}


def fetch(url: str, timeout: int = 15) -> str | None:
    """Busca HTML de uma URL via httpx (sem executar JavaScript)."""
    try:
        r = _CLIENT.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception:
        return None


def fetch_rendered(url: str) -> str | None:
    """
    Busca o DOM totalmente renderizado via Playwright.
    Necessário para SPAs (React, Next.js, Vue, Angular).
    Retorna None se playwright não estiver instalado.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(
                viewport={"width": 1440, "height": 900},
                user_agent=HEADERS["User-Agent"],
            )
            page.goto(url, wait_until="networkidle", timeout=30_000)
            page.wait_for_timeout(2000)   # aguarda hidratação JS
            html = page.content()         # DOM serializado pós-JS
            browser.close()
        return html
    except Exception:
        return None


def is_spa(html: str) -> bool:
    """
    Detecta se o HTML bruto é de uma SPA (conteúdo real gerado por JS).
    Heurística: pouco texto visível + presença de root/app div.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Remove scripts e styles para contar texto real
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    has_root = bool(soup.find(id=re.compile(r'^(root|app|__next|__nuxt)$')))
    return has_root and len(text) < 500


def should_skip_script(src: str) -> bool:
    """Verifica se um script externo deve ser ignorado."""
    if not src:
        return False
    return any(domain in src for domain in SKIP_SCRIPT_DOMAINS)


def minify_css(css: str) -> str:
    """Minificação rápida de CSS — remove comentários e espaços redundantes."""
    # Remove comentários /* ... */
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.DOTALL)
    # Colapsa espaços em branco múltiplos
    css = re.sub(r'\s+', ' ', css)
    # Remove espaços ao redor de : ; { } , > ~
    # Não inclui + nem - pois calc() exige espaços: calc(100% + 2px)
    css = re.sub(r'\s*([:{};,>~])\s*', r'\1', css)
    # Remove ; antes de }
    css = re.sub(r';}', '}', css)
    return css.strip()


def inline_css(soup: BeautifulSoup, base_url: str) -> int:
    """
    Substitui <link rel="stylesheet"> por <style> inline.
    Busca todas as folhas em paralelo. Retorna o número injetadas.
    """
    links = [
        link for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in r)
        if link.get("href") and not link.get("href", "").startswith("data:")
    ]
    if not links:
        return 0

    # Busca em paralelo
    abs_urls = [urljoin(base_url, link["href"]) for link in links]
    results: dict[int, str | None] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch, url): i for i, url in enumerate(abs_urls)}
        for fut in as_completed(futures):
            try:
                results[futures[fut]] = fut.result()
            except Exception:
                results[futures[fut]] = None

    count = 0
    for i, link in enumerate(links):
        css_text = results.get(i)
        if css_text:
            css_text = _resolve_imports(css_text, abs_urls[i])
            css_text = resolve_css_urls(css_text, abs_urls[i])
            css_text = minify_css(css_text)
            style_tag = soup.new_tag("style")
            style_tag.string = css_text
            link.replace_with(style_tag)
            count += 1
        # Se não conseguiu buscar, deixa o link original (tentará via CDN)

    return count


def _resolve_imports(css: str, css_url: str, depth: int = 0) -> str:
    """
    Substitui @import por o conteúdo real da folha importada (até 3 níveis).
    Mantém @import com media queries como estão (não pode ser inlineado trivialmente).
    """
    if depth >= 3:
        return css

    # Captura @import "url" ou @import url(...), sem media query
    pattern = re.compile(
        r'''@import\s+(?:url\()?(['"]?)([^'"\)\s;]+)\1\)?\s*;''',
        re.IGNORECASE,
    )

    def replacer(m):
        raw_url = m.group(2)
        if not raw_url or raw_url.startswith("http") and "fonts.googleapis" in raw_url:
            return m.group(0)  # mantém Google Fonts (precisa de JS para carregar)
        abs_url = urljoin(css_url, raw_url)
        imported = fetch(abs_url)
        if not imported:
            return ""
        imported = resolve_css_urls(imported, abs_url)
        imported = _resolve_imports(imported, abs_url, depth + 1)
        return imported

    return pattern.sub(replacer, css)


def resolve_css_urls(css: str, css_url: str) -> str:
    """Converte url('../img/foo.png') em url('https://...') dentro de um CSS."""
    def replacer(m):
        inner = m.group(1)
        raw = inner.strip().strip("'\"")
        if not raw or raw.startswith("data:") or raw.startswith("http"):
            return m.group(0)
        abs_url = urljoin(css_url, raw)
        quote = inner[0] if inner and inner[0] in ("'", '"') else ""
        return f"url({quote}{abs_url}{quote})"

    return re.sub(r'url\(([^)]+)\)', replacer, css)


def resolve_html_urls(soup: BeautifulSoup, base_url: str):
    """Torna absolutos todos os src/href/srcset que sejam relativos."""
    skip = ("http", "data:", "#", "mailto:", "tel:", "javascript:")
    for tag in soup.find_all(True):
        for attr in ("src", "href", "action", "data-src", "data-lazy-src", "poster"):
            val = tag.get(attr)
            if val and not val.startswith(skip):
                tag[attr] = urljoin(base_url, val)

        # srcset / data-srcset — imagens responsivas e lazy loading
        srcset = tag.get("srcset") or tag.get("data-srcset")
        if srcset:
            parts = []
            for entry in srcset.split(","):
                entry = entry.strip()
                if not entry:
                    continue
                tokens = entry.split()
                url = tokens[0]
                descriptor = " ".join(tokens[1:])
                if url and not url.startswith(skip):
                    url = urljoin(base_url, url)
                parts.append(f"{url} {descriptor}".strip())
            resolved = ", ".join(parts)
            if tag.get("srcset"):
                tag["srcset"] = resolved
            if tag.get("data-srcset"):
                tag["data-srcset"] = resolved


def remove_tracking(soup: BeautifulSoup):
    """Remove scripts de analytics/tracking."""
    for script in soup.find_all("script"):
        src = script.get("src", "")
        if should_skip_script(src):
            script.decompose()
            continue
        # Remove scripts inline de tracking comum
        text = script.get_text()
        if any(kw in text for kw in ("gtag(", "fbq(", "hj(", "_hsq")):
            script.decompose()


def _extract_used_tokens(soup: BeautifulSoup) -> tuple[set[str], set[str], set[str]]:
    """
    Extrai classes, IDs e tags usados no HTML.
    Retorna (classes, ids, tags).
    """
    classes: set[str] = set()
    ids: set[str] = set()
    tags: set[str] = set()
    for tag in soup.find_all(True):
        tags.add(tag.name.lower())
        for cls in tag.get("class") or []:
            classes.add(cls)
        if tag.get("id"):
            ids.add(tag["id"])
    return classes, ids, tags


def _selector_is_used(selector: str, classes: set, ids: set, tags: set) -> bool:
    """
    Decide se um seletor CSS é relevante para o HTML atual.
    Estratégia conservadora: mantém se qualquer token do seletor bater.
    """
    # Sempre mantém seletores universais e de reset
    always_keep = re.compile(
        r'^(\*|:root|html|body|@|\[|::?(?:before|after|root|'
        r'placeholder|selection|scrollbar|marker|backdrop|'
        r'file-selector-button|spelling-error|grammar-error|'
        r'webkit|moz|ms))'
    )
    if always_keep.match(selector.strip()):
        return True

    # Mantém qualquer seletor com atributo — [type="text"], [data-bs-*], etc.
    # Conservador mas seguro: attribute selectors são específicos e não inflam muito
    if '[' in selector:
        return True

    # Extrai tokens: .classe, #id, tag
    tokens = re.findall(r'[.#]?[\w-]+', selector)
    for token in tokens:
        if token.startswith('.'):
            if token[1:] in classes:
                return True
        elif token.startswith('#'):
            if token[1:] in ids:
                return True
        elif token.lower() in tags:
            return True
        # pseudo-classes / pseudo-elements: mantém se a tag-base existir
        elif ':' in token:
            base = token.split(':')[0].lower()
            if not base or base in tags:
                return True
    return False


def purge_css(css: str, soup: BeautifulSoup) -> tuple[str, int, int]:
    """
    Remove regras CSS cujos seletores não são usados no HTML.

    Retorna (css_purgado, regras_antes, regras_depois).
    Preserva sempre: @media, @keyframes, @font-face, @import, :root, *.
    """
    classes, ids, tags = _extract_used_tokens(soup)

    kept: list[str] = []
    rules_before = 0
    rules_after  = 0

    # Divide o CSS em blocos de nível superior
    # Estratégia: tokeniza por { } respeitando blocos aninhados (@media)
    pos = 0
    text = css

    while pos < len(text):
        # Pula espaços
        while pos < len(text) and text[pos] in ' \t\n\r':
            pos += 1
        if pos >= len(text):
            break

        # Encontra o próximo {
        brace = text.find('{', pos)
        if brace == -1:
            break

        prelude = text[pos:brace].strip()

        # Encontra o } correspondente (respeitando aninhamento)
        depth = 0
        end = brace
        while end < len(text):
            if text[end] == '{':
                depth += 1
            elif text[end] == '}':
                depth -= 1
                if depth == 0:
                    break
            end += 1

        block = text[pos:end + 1]
        rules_before += 1

        # @-rules: mantém sempre (analisamos o conteúdo interno recursivamente
        # apenas para @media, para podar regras internas também)
        if prelude.startswith('@'):
            at_keyword = prelude.split()[0].lower() if prelude.split() else ''
            if at_keyword == '@media':
                # Purga internamente o bloco @media
                inner = text[brace + 1:end]
                inner_purged, rb, ra = purge_css(inner, soup)
                rules_before += rb - 1   # -1 pois já contamos o @media acima
                rules_after  += ra
                if inner_purged.strip():
                    kept.append(f"{prelude}{{{inner_purged}}}")
                    rules_after += 1
            else:
                # @font-face, @keyframes, @import, @charset, etc. — mantém
                kept.append(block)
                rules_after += 1
        else:
            # Regra normal — verifica cada seletor da lista (separados por ,)
            selectors = [s.strip() for s in prelude.split(',')]
            if any(_selector_is_used(s, classes, ids, tags) for s in selectors):
                kept.append(block)
                rules_after += 1

        pos = end + 1

    return ' '.join(kept), rules_before, rules_after


def preprocess(url: str, inline_styles: bool = True) -> tuple[str, dict]:
    """
    Busca e enriquece o HTML de uma URL.

    Retorna:
        (html_enriquecido, stats)
        stats = {"css_inlined", "css_rules_before", "css_rules_after",
                 "char_count", "rendered"}
    """
    rendered = False

    # 1. Tenta httpx primeiro (mais rápido)
    raw_html = fetch(url)
    if raw_html is None:
        raise RuntimeError(f"Não foi possível buscar {url}")

    # Detecta SPA e tenta obter DOM renderizado via Playwright
    if is_spa(raw_html):
        rendered_html = fetch_rendered(url)
        if rendered_html:
            raw_html = rendered_html
            rendered = True

    soup = BeautifulSoup(raw_html, "html.parser")

    # 2. Resolve URLs relativas
    resolve_html_urls(soup, url)

    # 3. Remove tracking (reduz ruído para o Claude)
    remove_tracking(soup)

    # 4. Inline + minifica CSS externo
    css_count = 0
    if inline_styles:
        css_count = inline_css(soup, url)
        for style_tag in soup.find_all("style"):
            if style_tag.string:
                css = _resolve_imports(style_tag.string, url)
                css = resolve_css_urls(css, url)
                style_tag.string = minify_css(css)

    # 4. Purge CSS — remove regras não utilizadas
    total_rules_before = total_rules_after = 0
    for style_tag in soup.find_all("style"):
        if not style_tag.string:
            continue
        purged, rb, ra = purge_css(style_tag.string, soup)
        style_tag.string = purged
        total_rules_before += rb
        total_rules_after  += ra

    enriched = str(soup)
    return enriched, {
        "css_inlined":      css_count,
        "css_rules_before": total_rules_before,
        "css_rules_after":  total_rules_after,
        "char_count":       len(enriched),
        "rendered":         rendered,
    }
