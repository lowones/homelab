// Weather codes are WMO codes as returned by Open-Meteo.
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

function add(parent, tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;          // textContent only: the place name is user-set
  parent.appendChild(n);
  return n;
}

export default {
  render(el, data) {
    const w = data.weather;
    el.replaceChildren();
    add(el, 'div', 'wx-place', w.location);
    if (w.source === 'itinerary') add(el, 'div', 'wx-sub', 'From the trip schedule');
    if (!w.data) {
      add(el, 'div', 'wx-error', 'Weather unavailable');
      add(el, 'div', 'wx-sub', 'The Mac needs internet to fetch it. Trying again soon.');
      return;
    }
    const cur = describe(w.data.code);
    const now = add(el, 'div', 'wx-now');
    add(now, 'span', 'wx-icon', cur.icon);
    add(now, 'span', 'wx-temp', deg(w.data.temp));
    add(el, 'div', 'wx-desc', cur.text);
    add(el, 'div', 'wx-sub', 'Feels like ' + deg(w.data.feels));
    add(el, 'div', 'wx-sub', 'Wind ' + Math.round(w.data.wind) + ' mph');
    const list = add(el, 'ul', 'wx-days');
    w.data.days.forEach((d, i) => {
      const li = add(list, 'li');
      add(li, 'span', 'd', dayName(d.date, i));
      add(li, 'span', 'i', describe(d.code).icon);
      add(li, 'span', 'hi', deg(d.hi));
      add(li, 'span', 'lo', deg(d.lo));
    });
    if (w.error && w.updated) add(el, 'div', 'wx-sub', 'Showing data from ' + clock.format(new Date(w.updated * 1000)));
  },
};
