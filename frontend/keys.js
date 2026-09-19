(function () {
  const presets = {
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
      if (d.configured) {
        dotEl.classList.add('on');
        statusEl.textContent = `Active — ${d.provider} · ${d.model} · key ${d.masked_key}`;
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
    selectProvider('gemini');
    keyEl.value = '';
    msgEl.className = 'keys-msg';
    refreshStatus();
  }
  function closeModal() { overlay.classList.remove('open'); }

  if (navBtn) navBtn.addEventListener('click', openModal);
  closeBtn.addEventListener('click', closeModal);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });

  actBtn.addEventListener('click', async () => {
    const api_key = keyEl.value.trim();
    msgEl.className = 'keys-msg';
    if (!api_key) {
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
    actBtn.textContent = 'VERIFYING…';
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
})();
