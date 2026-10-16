// Dashboard shell. Widgets are plain modules in widgets/<id>.js listed in layout.json:
//   export default { init(el, cfg), render(el, data), tick(el) }   (all optional)
// init runs once, render runs whenever new data arrives (every 15 s), tick runs every second.
const token = location.hash.slice(1);
const grid = document.getElementById('grid');
const banner = document.getElementById('banner');
const widgets = [];

function say(text) { banner.textContent = text || ''; banner.hidden = !text; }

// The sky follows the clock: four palettes, switched by hour.
const SKIES = {
  night: ['#0d1220', '#1d2745'],
  dawn:  ['#2b3558', '#a35a55'],
  day:   ['#173f66', '#2f6c98'],
  dusk:  ['#3a2a5c', '#b5503f'],
};
function paintSky() {
  const h = new Date().getHours();
  const phase = h >= 5 && h < 7 ? 'dawn' : h >= 7 && h < 17 ? 'day' : h >= 17 && h < 20 ? 'dusk' : 'night';
  const [top, bottom] = SKIES[phase];
  document.documentElement.style.setProperty('--sky-top', top);
  document.documentElement.style.setProperty('--sky-bottom', bottom);
}

async function start() {
  let layout;
  try {
    layout = await (await fetch('/display/static/layout.json', { cache: 'no-store' })).json();
  } catch (e) { say('Could not load the layout'); return; }
  for (const cfg of layout.widgets) {
    const el = document.createElement('section');
    el.className = 'widget widget-' + cfg.id;
    el.style.gridColumn = 'span ' + (cfg.span || 4);
    grid.appendChild(el);
    try {
      const mod = (await import('/display/static/widgets/' + cfg.id + '.js')).default;
      if (mod.init) mod.init(el, cfg);
      widgets.push({ mod, el });
    } catch (e) {
      el.classList.add('broken');
      el.textContent = 'Widget "' + cfg.id + '" failed: ' + e.message;
    }
  }
  paintSky(); poll();
  setInterval(poll, 15000);
  setInterval(() => { widgets.forEach(w => w.mod.tick && safe(() => w.mod.tick(w.el))); }, 1000);
  setInterval(paintSky, 60000);
}

function safe(fn) { try { fn(); } catch (e) { console.error(e); } }

async function poll() {
  let data = null;
  try {
    const r = await fetch('/display/data', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' });
    if (r.status === 401) { say('This display session has ended. Press Show dashboard again.'); return; }
    if (!r.ok) throw new Error(r.status);
    data = await r.json();
    say('');
  } catch (e) { say('Lost contact with the TV box. Retrying...'); }
  if (data) widgets.forEach(w => w.mod.render && safe(() => w.mod.render(w.el, data)));
}

start();
