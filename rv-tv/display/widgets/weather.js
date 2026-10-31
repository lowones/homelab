// Weather codes are WMO codes as returned by Open-Meteo. Sunrise and sunset come from the same
// call; tides come from the nearest NOAA prediction station (data.tides, absent inland).
// The moon phase (data.moon) is worked out on the server and shown at the right of the top row.
const CODES = [
  [[0], 'Clear', '☀️'], [[1], 'Mostly clear', '🌤️'], [[2], 'Partly cloudy', '⛅'], [[3], 'Overcast', '☁️'],
  [[45, 48], 'Fog', '🌫️'], [[51, 53, 55, 56, 57], 'Drizzle', '🌦️'], [[61, 63, 65, 66, 67], 'Rain', '🌧️'],
  [[71, 73, 75, 77], 'Snow', '❄️'], [[80, 81, 82], 'Rain showers', '🌦️'], [[85, 86], 'Snow showers', '🌨️'],
  [[95, 96, 99], 'Thunderstorm', '⛈️'],
];
function describe(code) {
  const hit = CODES.find(c => c[0].includes(code));
  return hit ? { text: hit[1], icon: hit[2] } : { text: 'Unknown', icon: '·' };
}
const deg = n => Math.round(n) + '°';
const dayName = (iso, i) => {
  if (i === 0) return 'Today';
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString([], { weekday: 'long' });
};
const clock = new Intl.DateTimeFormat([], { hour: 'numeric', minute: '2-digit' });
const pad = n => String(n).padStart(2, '0');
const ymd = d => d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());

// '2026-10-16T07:34' (Open-Meteo, already local to the place) -> '7:34 AM'
function sunTime(s) {
  const m = /T(\d\d):(\d\d)/.exec(s || '');
  return m ? clock.format(new Date(2000, 0, 1, +m[1], +m[2])) : null;
}
// '2026-10-16 03:12' (NOAA, local station time) -> Date
function tideDate(s) {
  const m = /^(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d)$/.exec(s || '');
  return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5]) : null;
}

function add(parent, tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;          // textContent only
  parent.appendChild(n);
  return n;
}

// Today's high and low tides; once they have all passed, tomorrow's.
function tidePlan(t, now) {
  const ev = (t.events || []).map(e => ({ ...e, d: tideDate(e.t) })).filter(e => e.d);
  const today = ymd(now);
  const todays = ev.filter(e => ymd(e.d) === today);
  const upcoming = todays.some(e => e.d > now);
  const rows = (upcoming ? todays : ev.filter(e => ymd(e.d) !== today)).slice(0, 4);
  return { label: upcoming ? 'Tides today' : 'Tides tomorrow', rows, nextIdx: rows.findIndex(e => e.d > now) };
}

// A second, fixed place (data.weather_extra, e.g. Dundee, MI): one compact line, lilac.
function drawExtra(el, x) {
  if (!x || !x.data || x.data.temp == null) return;
  const d = x.data, c = describe(d.code);
  const box = add(el, 'div', 'wx-extra');
  add(box, 'span', 'xl', x.label);
  add(box, 'span', 'xi', c.icon);
  add(box, 'span', 'xt', deg(d.temp));
  add(box, 'span', 'xd', c.text);
  if (d.hi != null && d.lo != null) {
    add(box, 'span', 'xh', deg(d.hi));
    add(box, 'span', 'xlo', deg(d.lo));
  }
}

function drawTides(el, t, now) {
  if (!t || !t.station || !t.events || !t.events.length) return;          // inland, or not fetched yet
  const plan = tidePlan(t, now);
  if (!plan.rows.length) return;
  const box = add(el, 'div', 'wx-tides');
  const head = add(box, 'h3');
  add(head, 'span', 'l', plan.label);
  add(head, 'span', 'r', t.station + (t.miles != null ? ' · ' + t.miles + ' mi' : ''));
  const list = add(box, 'div', 'tide-list');
  plan.rows.forEach((e, i) => {
    const cls = 'tide' + (e.d <= now ? ' past' : '') + (i === plan.nextIdx ? ' next' : '');
    const row = add(list, 'div', cls);
    add(row, 'span', 'ar', e.type === 'H' ? '▲' : '▼');
    add(row, 'span', 'when', clock.format(e.d));
    add(row, 'span', 'ht', e.v.toFixed(1) + ' ft');
  });
}

export default {
  render(el, data) {
    const w = data.weather || {};
    el.replaceChildren();
    if (!w.data) {
      add(el, 'div', 'wx-error', 'Weather unavailable');
      add(el, 'div', 'wx-sub', 'The Mac needs internet to fetch it. Trying again soon.');
      drawExtra(el, data.weather_extra);
      drawTides(el, data.tides, new Date());
      return;
    }
    const cur = describe(w.data.code);
    const top = add(el, 'div', 'wx-top');
    add(top, 'span', 'wx-icon', cur.icon);
    add(top, 'span', 'wx-temp', deg(w.data.temp));
    const side = add(top, 'div', 'wx-side');
    add(side, 'div', 'wx-desc', cur.text);
    add(side, 'div', 'wx-sub', 'Feels like ' + deg(w.data.feels) + ' · Wind ' + Math.round(w.data.wind) + ' mph');
    const moon = data.moon;                          // computed on the server (data.moon): no network needed
    if (moon && moon.name) {
      const mo = add(top, 'div', 'wx-moon');
      add(mo, 'div', 'mi', moon.icon);
      add(mo, 'div', 'mn', moon.name);
      add(mo, 'div', 'mp', moon.percent + '% lit');
    }
    const today = w.data.days[0] || {};
    const rise = sunTime(today.rise), set = sunTime(today.set);
    if (rise || set) {
      const sun = add(side, 'div', 'wx-sun');
      if (rise) { const g = add(sun, 'span', 'sg'); add(g, 'span', 'k', '🌅'); g.appendChild(document.createTextNode(rise)); }
      if (rise && set) add(sun, 'span', 'gap');
      if (set) { const g = add(sun, 'span', 'sg'); add(g, 'span', 'k', '🌇'); g.appendChild(document.createTextNode(set)); }
    }
    const list = add(el, 'ul', 'wx-days');
    const y = w.data.yesterday;                      // sent separately; days[0] is still today
    // Wind (the day's top speed) and humidity (its mean) follow the low. An older server sends neither: no columns.
    const extra = [y, ...w.data.days].some(d => d && (d.wind != null || d.hum != null));
    if (extra) list.classList.add('cols');
    const wxCols = (li, d) => {
      if (!extra) return;
      add(li, 'span', 'wd', d.wind != null ? '💨 ' + d.wind + ' mph' : '');
      add(li, 'span', 'hm', d.hum != null ? '💧 ' + d.hum + '%' : '');
    };
    if (y && y.hi != null && y.lo != null) {
      const li = add(list, 'li', 'wx-yday');
      li.style.opacity = '0.62';                     // quieter than today so Today stays the focus
      add(li, 'span', 'd', 'Yesterday');
      add(li, 'span', 'i', describe(y.code).icon);
      add(li, 'span', 'hi', deg(y.hi));
      add(li, 'span', 'lo', deg(y.lo));
      wxCols(li, y);
    }
    w.data.days.forEach((d, i) => {
      const li = add(list, 'li');
      add(li, 'span', 'd', dayName(d.date, i));
      add(li, 'span', 'i', describe(d.code).icon);
      add(li, 'span', 'hi', deg(d.hi));
      add(li, 'span', 'lo', deg(d.lo));
      wxCols(li, d);
    });
    drawExtra(el, data.weather_extra);
    drawTides(el, data.tides, new Date());
    if (w.error && w.updated) add(el, 'div', 'wx-sub', 'Showing data from ' + clock.format(new Date(w.updated * 1000)));
  },
};
