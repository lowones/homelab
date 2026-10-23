// Where the Mac is (primary), and where it is headed (under it). On a move day, meaning the
// trip sheet says today's stop is somewhere else than the Mac's own location fix, the Mac's
// location stays on top and the sheet's stop is shown as the target. A reminder from the
// sheet, if today has one, sits in the top right corner.
const SOURCES = { device: "From this Mac's location", itinerary: 'From the trip schedule', manual: 'Pinned by hand' };

function add(parent, tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;          // textContent only: place names come from a spreadsheet
  parent.appendChild(n);
  return n;
}
function fmtDate(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
}
function fmtDur(min) {
  const h = Math.floor(min / 60), m = Math.round(min % 60);
  return h ? h + ' h' + (m ? ' ' + m + ' min' : '') : m + ' min';
}

export default {
  render(el, data) {
    const nav = data.nav, w = data.weather || {};
    const here = nav && nav.here ? nav.here : { label: w.location || '', source: w.source };
    el.replaceChildren();
    const rem = data.reminder && data.reminder.text ? data.reminder : null;
    const head = add(el, 'div', 'loc-head' + (rem ? ' has-reminder' : ''));
    const main = add(head, 'div', 'loc-main');
    if (nav && nav.move_day) add(main, 'div', 'loc-badge', 'MOVE DAY');
    add(main, 'div', 'loc-label', here.label || 'Location unknown');
    if (SOURCES[here.source]) add(main, 'div', 'loc-src', SOURCES[here.source]);
    // Column G of today's row on the trip sheet (data.reminder): top right, opposite the place.
    if (rem) {
      const card = add(head, 'div', 'loc-reminder');
      add(card, 'span', 'tag', 'Reminder');
      add(card, 'div', 'txt', rem.text);             // textContent only: it comes from a spreadsheet
    }

    const t = nav && nav.target;
    if (!t) return;
    const row = add(el, 'div', 'loc-target');
    add(row, 'span', 'k', nav.move_day ? 'Target' : 'Next stop');
    add(row, 'span', 'v', t.label);
    const bits = [];
    if (!nav.move_day && t.date) bits.push(fmtDate(t.date));
    const r = nav.route;
    if (r) {
      bits.push(Math.round(r.miles) + ' mi' + (r.straight ? ' straight line' : ' by road'));
      if (r.minutes) bits.push('about ' + fmtDur(r.minutes));
    } else if (!nav.error) {
      bits.push('finding the route…');
    }
    if (bits.length) add(el, 'div', 'loc-meta', bits.join(' · '));
    if (t.precision === 'city') add(el, 'div', 'loc-note', 'Pin is the town centre: street address not found');
    else if (t.precision === 'street') add(el, 'div', 'loc-note', 'Pin is on the street: exact spot not found');
    if (nav.error) add(el, 'div', 'loc-note', 'Route: ' + nav.error);
  },
};
