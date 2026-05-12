"""
Interface web para o Site Cloner.
Uso: python app.py  →  abre http://localhost:5000
"""

import json

import anthropic
from flask import Flask, Response, render_template_string, request, stream_with_context
from preprocessor import preprocess
from refiner import refine, screenshot_url, screenshot_html

# Detecta playwright disponível
try:
    from playwright.sync_api import sync_playwright as _pw
    PLAYWRIGHT_OK = True
except ImportError:
    PLAYWRIGHT_OK = False

app = Flask(__name__)

# ─── Prompt do sistema (mesmo do site_cloner.py) ─────────────────────────────

SYSTEM_PROMPT = """You are an expert web developer specializing in pixel-perfect website cloning.

Your job is to receive the HTML source of a website and produce a COMPLETE,
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

Do NOT skip sections, simplify layout, use placeholder text, or add comments.
"""

# ─── UI ───────────────────────────────────────────────────────────────────────

INDEX_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Site Cloner — powered by Claude</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap');
    * { box-sizing: border-box; }
    body { font-family: 'Inter', sans-serif; }
    .dot { animation: blink 1.2s step-start infinite; }
    @keyframes blink { 50% { opacity: 0; } }
    .split-view { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
    .split-view iframe { width: 100%; height: 520px; border-radius: 8px; border: 1px solid #374151; }
    .history-item:hover { background: #1f2937; }
  </style>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen p-6">

  <div class="max-w-5xl mx-auto space-y-6">

    <!-- Header -->
    <div class="text-center space-y-1 pt-4">
      <h1 class="text-3xl font-semibold tracking-tight">Site Cloner</h1>
      <p class="text-gray-400 text-sm">Cole uma URL — o Claude clona o site inteiro em HTML auto-contido</p>
    </div>

    <!-- Form -->
    <form id="form" class="flex gap-2">
      <input
        id="url-input"
        type="url"
        placeholder="https://exemplo.com"
        required
        class="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-4 py-3 text-sm
               focus:outline-none focus:ring-2 focus:ring-blue-500 placeholder-gray-500"
      />
      <button type="submit" id="btn"
        class="bg-blue-600 hover:bg-blue-500 text-white font-medium px-6 py-3 rounded-lg
               text-sm transition-colors disabled:opacity-40 disabled:cursor-not-allowed whitespace-nowrap">
        Clonar
      </button>
    </form>

    <!-- Warning banner (truncation, etc.) -->
    <div id="warning-banner" class="hidden text-xs text-amber-400 bg-amber-950 border border-amber-800 rounded-lg px-4 py-2"></div>

    <!-- Status -->
    <div id="status" class="hidden space-y-2">
      <div class="flex items-center justify-between text-sm">
        <div class="flex items-center gap-2 text-gray-400">
          <span id="status-text">Processando…</span><span class="dot">…</span>
        </div>
        <div class="flex items-center gap-3">
          <span id="char-count" class="text-xs text-gray-500"></span>
          <button id="btn-cancel" onclick="cancelClone()"
            class="text-xs px-2 py-1 rounded text-gray-400 hover:text-red-400 hover:bg-gray-800 transition-colors">
            ✕ Cancelar
          </button>
        </div>
      </div>
      <div class="h-1.5 bg-gray-800 rounded-full overflow-hidden">
        <div id="bar" class="h-full bg-blue-500 rounded-full transition-all duration-300" style="width:3%"></div>
      </div>
    </div>

    <!-- Result toolbar -->
    <div id="result" class="hidden space-y-4">
      <div class="flex items-center justify-between">
        <div class="flex items-center gap-3">
          <span class="text-green-400 text-sm font-medium">✓ Clone gerado</span>
          <span id="result-meta" class="text-xs text-gray-500"></span>
        </div>
        <div class="flex gap-2">
          <button id="btn-single" onclick="setView('single')"
            class="text-xs px-3 py-1.5 rounded-md bg-blue-700 transition-colors">
            Preview
          </button>
          <button id="btn-split" onclick="setView('split')"
            class="text-xs px-3 py-1.5 rounded-md bg-gray-800 hover:bg-gray-700 transition-colors">
            Comparar lado a lado
          </button>
          <button id="btn-refine" onclick="refineClone()"
            class="text-xs px-3 py-1.5 rounded-md bg-amber-700 hover:bg-amber-600 transition-colors">
            ✦ Refinar
          </button>
          <button onclick="copyClone(this)"
            class="text-xs px-3 py-1.5 rounded-md bg-gray-800 hover:bg-gray-700 transition-colors">
            ⎘ Copiar
          </button>
          <button onclick="downloadClone()"
            class="text-xs px-3 py-1.5 rounded-md bg-gray-800 hover:bg-gray-700 transition-colors">
            ↓ HTML
          </button>
        </div>
      </div>

      <!-- Single preview -->
      <div id="view-single">
        <iframe id="preview-clone" sandbox="allow-scripts allow-same-origin"
          class="w-full rounded-lg border border-gray-700" style="height:560px"></iframe>
      </div>

      <!-- Split: original | clone -->
      <div id="view-split" class="hidden">
        <div class="split-view">
          <div class="space-y-1">
            <p class="text-xs text-gray-500 text-center">Original</p>
            <iframe id="preview-original" class="rounded-lg border border-gray-700"
              style="width:100%;height:520px"></iframe>
          </div>
          <div class="space-y-1">
            <p class="text-xs text-gray-500 text-center">Clone (Claude)</p>
            <iframe id="preview-clone-split" sandbox="allow-scripts allow-same-origin"
              class="rounded-lg border border-gray-700" style="width:100%;height:520px"></iframe>
          </div>
        </div>
      </div>
    </div>

    <!-- Histórico -->
    <div id="history-section" class="hidden space-y-2 border-t border-gray-800 pt-4">
      <p class="text-xs text-gray-500 uppercase tracking-wider">Histórico</p>
      <div id="history-list" class="space-y-1"></div>
    </div>

  </div>

  <script>
    let clonedHtml = '';
    let currentUrl = '';
    let activeController = null;
    const HISTORY_KEY = 'site_cloner_history';

    function cancelClone() {
      if (activeController) {
        activeController.abort();
        activeController = null;
      }
    }

    const form      = document.getElementById('form');
    const btn       = document.getElementById('btn');
    const statusEl  = document.getElementById('status');
    const resultEl  = document.getElementById('result');
    const bar       = document.getElementById('bar');
    const charCount = document.getElementById('char-count');
    const statusTxt = document.getElementById('status-text');

    // ── Form submit ──────────────────────────────────────────────────────────
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      currentUrl = document.getElementById('url-input').value.trim();
      if (!currentUrl) return;

      btn.disabled = true;
      btn.textContent = 'Clonando…';
      statusEl.classList.remove('hidden');
      resultEl.classList.add('hidden');
      clonedHtml = '';
      bar.style.width = '4%';
      charCount.textContent = '';
      statusTxt.textContent = 'Iniciando…';
      document.getElementById('warning-banner').classList.add('hidden');

      let totalChars = 0;
      const t0 = Date.now();
      activeController = new AbortController();

      try {
        const resp = await fetch('/clone', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: currentUrl }),
          signal: activeController.signal,
        });

        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          alert('Erro: ' + (err.error || resp.statusText));
          return;
        }

        const reader  = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });

          // Consome linhas completas do buffer SSE
          const lines = buf.split('\n');
          buf = lines.pop(); // último elemento pode ser incompleto

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            let data;
            try { data = JSON.parse(line.slice(6)); } catch { continue; }

            if (data.type === 'status') {
              statusTxt.textContent = data.text;
              bar.style.width = data.progress + '%';

            } else if (data.type === 'warning') {
              const w = document.getElementById('warning-banner');
              w.textContent = '⚠ ' + data.text;
              w.classList.remove('hidden');

            } else if (data.type === 'token') {
              clonedHtml += data.text;
              totalChars += data.text.length;
              bar.style.width = Math.min(94, 32 + totalChars / 250) + '%';
              charCount.textContent = totalChars.toLocaleString('pt-BR') + ' chars';

            } else if (data.type === 'done') {
              // Remove cercas markdown residuais (```html ... ```)
              clonedHtml = clonedHtml.replace(/^```[\\w]*\n?/, '').replace(/\n?```$/, '').trim();
              const secs = ((Date.now() - t0) / 1000).toFixed(1);
              bar.style.width = '100%';
              statusEl.classList.add('hidden');
              resultEl.classList.remove('hidden');
              document.getElementById('result-meta').textContent =
                `${totalChars.toLocaleString('pt-BR')} chars · ${secs}s`;
              setView('single');
              saveHistory(currentUrl, clonedHtml, totalChars);
            }
          }
        }
      } catch (err) {
        if (err.name !== 'AbortError') alert('Erro de rede: ' + err.message);
      } finally {
        activeController = null;
        btn.disabled = false;
        btn.textContent = 'Clonar';
        statusEl.classList.add('hidden');
      }
    });

    // ── View toggle ──────────────────────────────────────────────────────────
    function setView(mode) {
      const single = document.getElementById('view-single');
      const split  = document.getElementById('view-split');
      document.getElementById('btn-single').className =
        'text-xs px-3 py-1.5 rounded-md transition-colors ' +
        (mode === 'single' ? 'bg-blue-700' : 'bg-gray-800 hover:bg-gray-700');
      document.getElementById('btn-split').className =
        'text-xs px-3 py-1.5 rounded-md transition-colors ' +
        (mode === 'split' ? 'bg-blue-700' : 'bg-gray-800 hover:bg-gray-700');

      if (mode === 'single') {
        single.classList.remove('hidden');
        split.classList.add('hidden');
        document.getElementById('preview-clone').srcdoc = clonedHtml;
      } else {
        single.classList.add('hidden');
        split.classList.remove('hidden');
        document.getElementById('preview-clone-split').srcdoc = clonedHtml;
        // Carrega o original em iframe (pode ser bloqueado por X-Frame-Options)
        const orig = document.getElementById('preview-original');
        orig.src = currentUrl;
        orig.onerror = () => {
          orig.srcdoc = '<body style="display:flex;align-items:center;justify-content:center;height:100%;font-family:sans-serif;color:#9ca3af;background:#111">' +
            '<p>Site bloqueou iframe (X-Frame-Options)</p></body>';
        };
      }
    }

    // ── Copy to clipboard ────────────────────────────────────────────────────
    function copyClone(btn) {
      if (!clonedHtml) return;
      navigator.clipboard.writeText(clonedHtml).then(() => {
        const orig = btn.textContent;
        btn.textContent = '✓ Copiado';
        setTimeout(() => { btn.textContent = orig; }, 1800);
      }).catch(() => alert('Falha ao copiar — tente pelo botão ↓ HTML'));
    }

    // ── Download ─────────────────────────────────────────────────────────────
    function downloadClone() {
      const domain = (() => { try { return new URL(currentUrl).hostname.replace(/\\./g,'_'); } catch { return 'clone'; } })();
      const blob = new Blob([clonedHtml], { type: 'text/html' });
      const a = Object.assign(document.createElement('a'), {
        href: URL.createObjectURL(blob),
        download: `clone_${domain}.html`,
      });
      a.click();
    }

    // ── Refinar ──────────────────────────────────────────────────────────────
    async function refineClone() {
      if (!clonedHtml || !currentUrl) return;
      const btn = document.getElementById('btn-refine');
      btn.disabled = true;
      btn.textContent = 'Refinando…';
      statusEl.classList.remove('hidden');
      bar.style.width = '5%';
      statusTxt.textContent = 'Capturando screenshots…';
      charCount.textContent = '';

      let totalChars = 0;
      const t0 = Date.now();
      let refined = '';
      activeController = new AbortController();

      try {
        const resp = await fetch('/refine', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: currentUrl, html: clonedHtml }),
          signal: activeController.signal,
        });

        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          alert('Erro ao refinar: ' + (err.error || resp.statusText));
          return;
        }

        const reader  = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop();

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            let data;
            try { data = JSON.parse(line.slice(6)); } catch { continue; }

            if (data.type === 'status') {
              statusTxt.textContent = data.text;
              bar.style.width = data.progress + '%';
            } else if (data.type === 'token') {
              refined += data.text;
              totalChars += data.text.length;
              bar.style.width = Math.min(94, 35 + totalChars / 250) + '%';
              charCount.textContent = totalChars.toLocaleString('pt-BR') + ' chars';
            } else if (data.type === 'done') {
              refined = refined.replace(/^```[\\w]*\n?/, '').replace(/\n?```$/, '').trim();
              clonedHtml = refined;
              const secs = ((Date.now() - t0) / 1000).toFixed(1);
              bar.style.width = '100%';
              statusEl.classList.add('hidden');
              document.getElementById('result-meta').textContent =
                `${totalChars.toLocaleString('pt-BR')} chars · refinado em ${secs}s`;
              setView('single');
              saveHistory(currentUrl, clonedHtml, totalChars);
            }
          }
        }
      } catch (err) {
        if (err.name !== 'AbortError') alert('Erro de rede: ' + err.message);
      } finally {
        activeController = null;
        btn.disabled = false;
        btn.textContent = '✦ Refinar';
        statusEl.classList.add('hidden');
      }
    }

    // ── Histórico (localStorage) ─────────────────────────────────────────────
    function saveHistory(url, html, chars) {
      const history = loadHistory();
      history.unshift({ url, html, chars, ts: Date.now() });
      const trimmed = history.slice(0, 20); // mantém últimos 20
      try { localStorage.setItem(HISTORY_KEY, JSON.stringify(trimmed)); } catch {}
      renderHistory(trimmed);
    }

    function loadHistory() {
      try { return JSON.parse(localStorage.getItem(HISTORY_KEY) || '[]'); } catch { return []; }
    }

    function renderHistory(history) {
      const section = document.getElementById('history-section');
      const list    = document.getElementById('history-list');
      if (!history.length) { section.classList.add('hidden'); return; }
      section.classList.remove('hidden');
      list.innerHTML = history.map((item, i) => {
        const d = new Date(item.ts).toLocaleString('pt-BR', { dateStyle:'short', timeStyle:'short' });
        const domain = (() => { try { return new URL(item.url).hostname; } catch { return item.url; } })();
        return `<div class="history-item flex items-center justify-between px-3 py-2 rounded-lg cursor-pointer"
                     onclick="loadHistoryItem(${i})">
          <div>
            <span class="text-sm text-gray-200">${domain}</span>
            <span class="text-xs text-gray-500 ml-2">${(item.chars/1000).toFixed(1)}k chars</span>
          </div>
          <span class="text-xs text-gray-600">${d}</span>
        </div>`;
      }).join('');
    }

    function loadHistoryItem(i) {
      const item = loadHistory()[i];
      if (!item) return;
      currentUrl = item.url;
      clonedHtml = item.html;
      document.getElementById('url-input').value = item.url;
      document.getElementById('result-meta').textContent =
        `${item.chars.toLocaleString('pt-BR')} chars · histórico`;
      resultEl.classList.remove('hidden');
      setView('single');
    }

    // Carrega histórico ao iniciar
    renderHistory(loadHistory());

    // Verifica playwright e ajusta tooltip do botão Refinar
    fetch('/playwright-status').then(r => r.json()).then(d => {
      const b = document.getElementById('btn-refine');
      if (!d.available) {
        b.title = 'Instale playwright para refinamento visual com screenshots:\npip install playwright && playwright install chromium';
        b.classList.add('opacity-60');
      }
    }).catch(() => {});
  </script>

</body>
</html>"""

# ─── Rota principal ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/clone", methods=["POST"])
def clone():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return {"error": "URL não informada"}, 400

    def generate():
        def sse(obj: dict) -> str:
            return f"data: {json.dumps(obj)}\n\n"

        # 1. Buscar e pré-processar HTML (inline CSS, resolver URLs)
        yield sse({"type": "status", "text": "Buscando HTML e injetando CSS…", "progress": 10})
        try:
            html_source, stats = preprocess(url, inline_styles=True)
            truncated = len(html_source) > 150_000
            html_source = html_source[:150_000]
            rb = stats.get("css_rules_before", 0)
            ra = stats.get("css_rules_after", 0)
            removed_pct = int((1 - ra / rb) * 100) if rb else 0
            css_msg = (
                f"{stats['css_inlined']} CSS injetados"
                + (f" · {removed_pct}% regras removidas ({rb}→{ra})" if rb else "")
            )
            if stats.get("rendered"):
                css_msg = "🤖 SPA detectada — DOM renderizado · " + css_msg
        except Exception as e:
            yield sse({"type": "status", "text": f"Erro: {e}", "progress": 0})
            return

        if truncated:
            yield sse({"type": "warning", "text": "HTML truncado em 150k chars — site muito grande, clone pode estar incompleto."})
        yield sse({"type": "status", "text": f"HTML pronto ({css_msg}). Gerando clone…", "progress": 28})

        # 2. Chamar Claude com streaming + prompt caching
        client = anthropic.Anthropic()
        messages = [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"Clone this website: {url}\n\nHTML source:\n```html\n{html_source}\n```",
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": "Output the complete self-contained HTML clone now.",
                },
            ],
        }]

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
                yield sse({"type": "token", "text": text})

        yield sse({"type": "done"})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/refine", methods=["POST"])
def refine_route():
    data = request.get_json()
    url       = data.get("url", "").strip()
    clone_html = data.get("html", "").strip()
    if not url or not clone_html:
        return {"error": "url e html são obrigatórios"}, 400

    def generate():
        def sse(obj: dict) -> str:
            return f"data: {json.dumps(obj)}\n\n"

        # 1. Screenshots (se playwright disponível)
        orig_ss = clone_ss = None
        if PLAYWRIGHT_OK:
            yield sse({"type": "status", "text": "Capturando screenshot do original…", "progress": 10})
            orig_ss = screenshot_url(url)
            yield sse({"type": "status", "text": "Capturando screenshot do clone…", "progress": 22})
            clone_ss = screenshot_html(clone_html)
            yield sse({"type": "status", "text": "Enviando comparação ao Claude…", "progress": 32})
        else:
            yield sse({"type": "status", "text": "Refinando pelo HTML (instale playwright para usar screenshots)…", "progress": 20})

        # 2. Chama refiner com streaming manual
        from refiner import REFINE_SYSTEM, _b64

        content: list[dict] = []
        if orig_ss and clone_ss:
            content += [
                {"type": "text", "text": "ORIGINAL website screenshot:"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(orig_ss)}},
                {"type": "text", "text": "YOUR CLONE screenshot:"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(clone_ss)}},
            ]
        else:
            content.append({"type": "text", "text": "(Screenshots indisponíveis — refinando pela estrutura HTML.)"})

        content.append({
            "type": "text",
            "text": (
                f"Original URL: {url}\n\n"
                f"Your previous clone HTML:\n```html\n{clone_html[:100_000]}\n```"
            ),
            "cache_control": {"type": "ephemeral"},
        })
        content.append({
            "type": "text",
            "text": "Produce the improved HTML clone fixing all visible differences.",
        })

        client = anthropic.Anthropic()
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
                yield sse({"type": "token", "text": text})

        yield sse({"type": "done"})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/playwright-status")
def playwright_status():
    return {"available": PLAYWRIGHT_OK}


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    status = "com screenshots" if PLAYWRIGHT_OK else "sem screenshots (instale playwright para ativar refinamento visual)"
    print(f"🚀  Site Cloner rodando em http://localhost:5000 — {status}")
    app.run(debug=False, port=5000, threaded=True)
