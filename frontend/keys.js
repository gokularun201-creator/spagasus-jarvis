(function () {
  const presets = {
    gemma:  { model: "gemma4:12b", base: "http://127.0.0.1:11434/v1" },
    gemini: { model: "gemini-2.5-flash" },
    grok:   { model: "grok-4-fast" },
    openai: { model: "gpt-4o-mini" },
    wispr:  { model: "whisper-1" },
    custom: { model: "" },
  };
  let provider = "gemini";

  const overlay   = document.getElementById('keysOverlay');
  const navBtn    = document.getElementById('keysNavBtn');
  const closeBtn  = document.getElementById('keysClose');
  const provEls   = document.querySelectorAll('.keys-prov');
  const modelEl   = document.getElementById('keysModel');
  const baseEl    = document.getElementById('keysBaseUrl');
  const customRow = document.getElementById('keysCustomRow');
  const keyEl     = document.getElementById('keysApiKey');
  const toggleEl  = document.getElementById('keysToggle');
  const msgEl     = document.getElementById('keysMsg');
  const actBtn    = document.getElementById('keysActivateBtn');
  const dotEl     = document.getElementById('keysDot');
  const statusEl  = document.getElementById('keysStatusText');

  function selectProvider(p) {
    provider = p;
    provEls.forEach(e => e.classList.toggle('active', e.dataset.p === p));
    customRow.style.display = p === 'custom' ? 'block' : 'none';
    modelEl.placeholder = presets[p].model || 'model name';
    if (p === 'gemma') {
      if (!keyEl.value || keyEl.value === 'ollama-local') {
        keyEl.value = 'ollama-local';
      }
      keyEl.placeholder = 'Local engine (no API key needed)';
    } else {
      if (keyEl.value === 'ollama-local') keyEl.value = '';
      keyEl.placeholder = 'Paste your API key…';
    }
  }
  provEls.forEach(e => e.addEventListener('click', () => selectProvider(e.dataset.p)));

  toggleEl.addEventListener('click', () => {
    const t = keyEl.type === 'password' ? 'text' : 'password';
    keyEl.type = t;
    toggleEl.textContent = t === 'password' ? 'show' : 'hide';
  });

  async function refreshStatus() {
    try {
      const r = await fetch('/api/keys/status');
      const d = await r.json();
      const badge = document.getElementById('engineBadge');
      if (badge) {
        if (d.provider === 'gemma' || d.local_llm_enabled) {
          badge.textContent = '💎 GEMMA 4 LOCAL';
          badge.style.color = '#a855f7';
        } else {
          badge.textContent = '✨ ' + (d.provider || 'AI').toUpperCase();
          badge.style.color = '#38bdf8';
        }
      }
      if (d.configured) {
        dotEl.classList.add('on');
        if (d.provider === 'gemma') {
          statusEl.textContent = `Active — 💎 Gemma 4 Local (Ollama: ${d.ollama && d.ollama.running ? 'online' : 'ready'}) · ${d.model}`;
        } else {
          statusEl.textContent = `Active — ${d.provider} · ${d.model} · key ${d.masked_key}`;
        }
      } else {
        dotEl.classList.remove('on');
        statusEl.textContent = 'No key yet — JARVIS runs on built-in rules only.';
      }
    } catch (e) {
      statusEl.textContent = 'Could not reach JARVIS backend.';
    }
  }

  function openModal() {
    overlay.classList.add('open');
    selectProvider(provider || 'gemma');
    msgEl.className = 'keys-msg';
    refreshStatus();
  }
  function closeModal() { overlay.classList.remove('open'); }

  if (navBtn) navBtn.addEventListener('click', openModal);
  closeBtn.addEventListener('click', closeModal);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });

  actBtn.addEventListener('click', async () => {
    let api_key = keyEl.value.trim();
    if (provider === 'gemma' && !api_key) {
      api_key = 'ollama-local';
    }
    msgEl.className = 'keys-msg';
    if (!api_key && provider !== 'gemma') {
      msgEl.textContent = 'Enter an API key first.';
      msgEl.classList.add('err');
      return;
    }
    if (provider === 'custom' && !baseEl.value.trim()) {
      msgEl.textContent = 'Custom provider needs a base URL.';
      msgEl.classList.add('err');
      return;
    }
    actBtn.disabled = true;
    actBtn.textContent = 'ACTIVATING…';
    try {
      const r = await fetch('/api/keys/activate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider,
          api_key,
          model: modelEl.value.trim() || undefined,
          base_url: provider === 'custom' ? baseEl.value.trim() : undefined,
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'activation failed');
      msgEl.textContent = '✓ ' + d.message;
      msgEl.classList.add('ok');
      keyEl.value = '';
      actBtn.textContent = 'ACTIVATE';
      setTimeout(refreshStatus, 6000);
    } catch (e) {
      msgEl.textContent = '✕ ' + e.message;
      msgEl.classList.add('err');
      actBtn.textContent = 'ACTIVATE';
    } finally {
      actBtn.disabled = false;
    }
  });

  // Check and display active engine badge immediately on load
  refreshStatus();
})();
