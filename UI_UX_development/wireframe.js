/* VORTEX wireframes — tabs + tweaks */
(function () {
  // ---- tabs ----
  var tabs = document.querySelectorAll('.tab');
  var panels = {
    a: document.getElementById('panel-a'),
    b: document.getElementById('panel-b'),
    c: document.getElementById('panel-c'),
    d: document.getElementById('panel-d')
  };
  function show(id, skipScroll) {
    tabs.forEach(function (t) { t.classList.toggle('active', t.dataset.tab === id); });
    Object.keys(panels).forEach(function (k) { panels[k].classList.toggle('active', k === id); });
    try { localStorage.setItem('vortex_wf_tab', id); } catch (e) {}
    if (!skipScroll) window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  tabs.forEach(function (t) { t.addEventListener('click', function () { show(t.dataset.tab); }); });
  // keyboard 1-4 / a-d
  document.addEventListener('keydown', function (e) {
    var map = { '1': 'a', '2': 'b', '3': 'c', '4': 'd', a: 'a', b: 'b', c: 'c', d: 'd' };
    if (e.target.tagName === 'INPUT') return;
    var id = map[e.key.toLowerCase()];
    if (id) show(id);
  });
  var saved = null;
  try { saved = localStorage.getItem('vortex_wf_tab'); } catch (e) {}
  if (saved && panels[saved]) show(saved, true);

  // ---- tweaks ----
  var btn = document.getElementById('tweakBtn');
  var panel = document.getElementById('tweakPanel');
  btn.addEventListener('click', function () {
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  });

  function toggleSetup(el, key, onChange) {
    el.addEventListener('click', function () {
      el.classList.toggle('on');
      var on = el.classList.contains('on');
      try { localStorage.setItem(key, on ? '1' : '0'); } catch (e) {}
      onChange(on);
    });
    var v = null;
    try { v = localStorage.getItem(key); } catch (e) {}
    if (v !== null) {
      var on = v === '1';
      el.classList.toggle('on', on);
      onChange(on);
    }
  }

  toggleSetup(document.getElementById('tw-sketch'), 'vortex_wf_sketch', function (on) {
    document.documentElement.style.setProperty('--sketch', on ? '1' : '0');
  });
  toggleSetup(document.getElementById('tw-notes'), 'vortex_wf_notes', function (on) {
    document.body.classList.toggle('nonotes', !on);
  });

  // accent swatches
  var sws = document.querySelectorAll('#tw-accent .sw');
  function setAccent(c) {
    document.documentElement.style.setProperty('--accent', c);
    sws.forEach(function (s) {
      s.style.boxShadow = s.dataset.c === c ? '0 0 0 2px var(--paper), 0 0 0 4px var(--line)' : 'none';
    });
    try { localStorage.setItem('vortex_wf_accent', c); } catch (e) {}
  }
  sws.forEach(function (s) { s.addEventListener('click', function () { setAccent(s.dataset.c); }); });
  var ac = null;
  try { ac = localStorage.getItem('vortex_wf_accent'); } catch (e) {}
  if (ac) setAccent(ac);
})();
