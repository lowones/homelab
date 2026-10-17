// Satellite map, centred on the Mac's own location and about `miles` (layout.json, default 75)
// across its shorter side. Red pins are past hikes (display/pins.json, made by
// tools/make_pins.py); the amber line is the drive to the next stop. Leaflet is vendored under
// display/vendor/, so only the imagery tiles need internet: with the link down the map keeps
// its pins, route and position on a dark background and says so.
const BASE = '/display/static/';
const S = { map: null, here: null, targetMark: null, line: [], key: '', routeKey: '', miles: 75, rem: 24,
            loads: 0, errors: 0, note: null };

function loadCss(href) {
  const l = document.createElement('link');
  l.rel = 'stylesheet'; l.href = href;
  document.head.appendChild(l);
}
function loadScript(src) {
  return new Promise((ok, fail) => {
    const s = document.createElement('script');
    s.src = src; s.onload = ok; s.onerror = () => fail(new Error('could not load ' + src));
    document.head.appendChild(s);
  });
}

// Zoom level at which `miles` of ground span `px` pixels at this latitude (256 px tiles).
export function zoomFor(lat, px, miles) {
  const z = Math.log2(156543.03392 * Math.cos(lat * Math.PI / 180) * px / (miles * 1609.344));
  return Math.max(3, Math.min(17, z));
}

function pinIcon(rem) {
  const w = Math.round(rem * 1.5), h = Math.round(rem * 2.1);
  const svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 30 42" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M15 1C7.3 1 1 7.1 1 14.7 1 25.2 15 41 15 41s14-15.8 14-26.3C29 7.1 22.7 1 15 1z" fill="#e11d2e" stroke="#fff" stroke-width="2"/>' +
    '<circle cx="15" cy="14.5" r="5.2" fill="#fff"/></svg>';
  return L.divIcon({ className: 'hike-pin', html: svg, iconSize: [w, h], iconAnchor: [w / 2, h - 1] });
}

function say(text) {
  if (!S.note) return;
  S.note.textContent = text || '';
  S.note.hidden = !text;
}

async function addPins() {
  let pins = [];
  try { pins = await (await fetch(BASE + 'pins.json')).json(); } catch (e) { return; }   // no file: no pins
  const icon = pinIcon(S.rem);
  const group = L.markerClusterGroup({ maxClusterRadius: 50, showCoverageOnHover: false, spiderfyOnMaxZoom: false,
    zoomToBoundsOnClick: false, disableClusteringAtZoom: 15, chunkedLoading: true, animate: false });
  group.addLayers(pins.map(p => L.marker([p[0], p[1]], { icon, interactive: false, keyboard: false })));
  group.addTo(S.map);
}

function tiles() {
  const imagery = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    maxZoom: 19, attribution: 'Imagery &copy; Esri, Maxar, Earthstar Geographics, and the GIS User Community' });
  const labels = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}', {
    maxZoom: 19, opacity: 0.9 });
  [imagery, labels].forEach(l => {
    l.on('tileload', () => { S.loads++; if (S.loads === 1) say(''); });
    l.on('tileerror', e => {
      S.errors++;
      e.tile.style.visibility = 'hidden';
      if (S.loads === 0 && S.errors >= 4) say('Map imagery is offline (needs internet). Position, pins and route still show.');
    });
  });
  return [imagery, labels];
}

export default {
  async init(el, cfg) {
    S.miles = (cfg && cfg.miles) || 75;
    S.rem = parseFloat(getComputedStyle(document.documentElement).fontSize) || 24;
    const canvas = document.createElement('div'); canvas.className = 'map-canvas';
    S.note = document.createElement('div'); S.note.className = 'map-note'; S.note.hidden = true;
    el.append(canvas, S.note);

    ['leaflet.css', 'MarkerCluster.css', 'MarkerCluster.Default.css'].forEach(f => loadCss(BASE + 'vendor/' + f));
    await loadScript(BASE + 'vendor/leaflet.js');
    await loadScript(BASE + 'vendor/leaflet.markercluster.js');

    const map = L.map(canvas, { zoomControl: false, zoomSnap: 0, zoomAnimation: false, fadeAnimation: false,
      markerZoomAnimation: false, dragging: false, scrollWheelZoom: false, doubleClickZoom: false, boxZoom: false,
      keyboard: false, touchZoom: false, inertia: false });
    map.attributionControl.setPrefix(false);
    L.control.scale({ imperial: true, metric: false, position: 'bottomleft' }).addTo(map);
    tiles().forEach(l => l.addTo(map));
    map.setView([39, -98], 4);                       // until the first fix arrives
    S.map = map;

    const d = Math.round(S.rem * 2.4);
    S.here = L.marker([0, 0], { interactive: false, keyboard: false, zIndexOffset: 1000, opacity: 0,
      icon: L.divIcon({ className: 'rv-here', html: '<span class="pulse"></span><span class="dot"></span>',
                        iconSize: [d, d], iconAnchor: [d / 2, d / 2] }) }).addTo(map);
    addPins();
    window.addEventListener('resize', () => { map.invalidateSize(); S.key = ''; });
  },

  render(el, data) {
    const map = S.map;
    if (!map) return;                                // the library is still loading
    const nav = data.nav;
    if (!nav || !nav.here) { say('Waiting for a location fix'); return; }
    if (S.note && S.note.textContent.startsWith('Waiting')) say('');

    map.invalidateSize({ pan: false });
    const size = map.getSize();
    const h = nav.here;
    const key = [h.lat.toFixed(3), h.lon.toFixed(3), size.x, size.y, S.miles].join();
    if (key !== S.key && size.x > 0) {               // recentre only when something actually changed
      S.key = key;
      map.setView([h.lat, h.lon], zoomFor(h.lat, Math.min(size.x, size.y), S.miles), { animate: false });
    }
    S.here.setLatLng([h.lat, h.lon]).setOpacity(1);

    const r = nav.route, t = nav.target;
    const rk = r ? [r.segments.length, r.segments[0] && r.segments[0].length, r.miles, r.straight, t && t.lat, t && t.lon].join() : '';
    if (rk === S.routeKey) return;
    S.routeKey = rk;
    S.line.forEach(l => map.removeLayer(l));
    S.line = [];
    if (!r) return;
    const dash = r.straight ? Math.round(S.rem * .25) + ' ' + Math.round(S.rem * .7) : null;
    const base = { interactive: false, lineCap: 'round', lineJoin: 'round', dashArray: dash };
    S.line.push(L.polyline(r.segments, { ...base, color: '#0a0e18', weight: Math.round(S.rem * .62), opacity: .7 }).addTo(map));
    S.line.push(L.polyline(r.segments, { ...base, color: '#ffb02e', weight: Math.round(S.rem * .36), opacity: .97 }).addTo(map));
    if (t && t.lat != null) {
      const d = Math.round(S.rem * 2);
      S.line.push(L.marker([t.lat, t.lon], { interactive: false, keyboard: false, zIndexOffset: 900,
        icon: L.divIcon({ className: 'rv-target', html: '<span></span>', iconSize: [d, d], iconAnchor: [d / 2, d / 2] }) }).addTo(map));
    }
  },
};
