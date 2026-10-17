// Satellite map, centred on the Mac's own location and about `data.map.miles` (a server setting: POST /display/map?miles=N, default 35;
// layout.json `miles` is only a fallback)
// across its shorter side. When a whole edge of the view is open water (the coast), the view is
// slid toward the land (fitLand) while the position dot stays at least 15% in from the edge. Pins in blue (post office), purple (library) and green (Costco) are the nearest few of each in view. Red pins are past hikes (display/pins.json, made by
// tools/make_pins.py); the amber line is the drive to the next stop. Leaflet is vendored under
// display/vendor/, so only the imagery tiles need internet: with the link down the map keeps
// its pins, route and position on a dark background and says so.
const BASE = '/display/static/';
const S = { map: null, here: null, targetMark: null, line: [], key: '', routeKey: '', miles: 35, rem: 24, fitId: 0,
            loads: 0, errors: 0, note: null, poiData: null, poiLayer: null, poiKey: '', hereLL: null };

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

// ---- post offices, libraries, Costco -----------------------------------------------------------------
// The server sends the nearest ~40 of each kind (OpenStreetMap); here only the nearest few that are
// inside the picture are drawn, one colour + glyph per kind. Pins only, no labels.
const POI = {
  post:    { color: '#2f6fed', glyph: '<rect x="7.5" y="9.5" width="15" height="10.5" rx="1.6" fill="#fff"/>' +
                                     '<path d="M8.4 10.6 15 15.8l6.6-5.2" fill="none" stroke="#2f6fed" stroke-width="1.7" stroke-linejoin="round"/>' },
  library: { color: '#8b3fd9', glyph: '<path d="M7 10.2c3-1.2 6-.9 8 1v8.6c-2-1.6-5-1.8-8-.7z" fill="#fff"/>' +
                                     '<path d="M23 10.2c-3-1.2-6-.9-8 1v8.6c2-1.6 5-1.8 8-.7z" fill="#fff"/>' },
  costco:  { color: '#16a04a', glyph: '<path d="M7 9.2h2.9l1.9 7.6h7.6l1.9-5.6H10.8" fill="none" stroke="#fff" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>' +
                                     '<circle cx="13" cy="20.2" r="1.5" fill="#fff"/><circle cx="19.2" cy="20.2" r="1.5" fill="#fff"/>' },
};

function poiIcon(kind, rem) {
  const w = Math.round(rem * 1.6), h = Math.round(rem * 2.24), c = POI[kind];
  const svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 30 42" xmlns="http://www.w3.org/2000/svg">' +
    '<path d="M15 1C7.3 1 1 7.1 1 14.7 1 25.2 15 41 15 41s14-15.8 14-26.3C29 7.1 22.7 1 15 1z" fill="' + c.color + '" stroke="#fff" stroke-width="2"/>' +
    c.glyph + '</svg>';
  return L.divIcon({ className: 'poi-pin', html: svg, iconSize: [w, h], iconAnchor: [w / 2, h - 1] });
}

// Nearest `count` of each kind among those inside the current view (to the Mac's position).
export function pickPoi(items, bounds, here, count) {
  const out = [];
  for (const kind of Object.keys(POI)) {
    const inView = items.filter(p => p.t === kind && bounds.contains([p.lat, p.lon]))
      .map(p => ({ p, d: Math.hypot(p.lat - here[0], (p.lon - here[1]) * Math.cos(here[0] * Math.PI / 180)) }))
      .sort((a, b) => a.d - b.d);
    inView.slice(0, count).forEach(x => out.push(x.p));
  }
  return out;
}

function drawPoi() {
  const map = S.map;
  if (!map || !S.poiLayer) return;
  const d = S.poiData, here = S.hereLL;
  const picked = d && here && d.items ? pickPoi(d.items, map.getBounds(), here, d.count || 5) : [];
  const key = picked.map(p => p.t + p.lat + ',' + p.lon).join('|') + '@' + S.rem;
  if (key === S.poiKey) return;                              // nothing changed: leave the markers alone
  S.poiKey = key;
  S.poiLayer.clearLayers();
  const icons = {};
  picked.forEach(p => {
    icons[p.t] = icons[p.t] || poiIcon(p.t, S.rem);
    L.marker([p.lat, p.lon], { icon: icons[p.t], interactive: false, keyboard: false, zIndexOffset: 500 }).addTo(S.poiLayer);
  });
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


// ---- keep open water off the edge ------------------------------------------------------------
// Looks at the same imagery the map shows (Esri allows reading its tiles), a couple of zoom levels
// out so that a handful of tiles cover the view, and labels each pixel water or land. Water is dark
// and blue-leaning; forest is green, desert and fields are red/brown, cloud and snow are grey.
// Returns how many view pixels to pan [x, y], or null when anything is unclear.
const IMG = 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/';
const EDGE_RUN = 0.12;      // water must reach this far in from the edge to count
const WATER_COL = 0.80;     // share of a column/row that must be water to count as water
const KEEP_IN = 0.15;       // the position dot stays at least this far from the edge

function loadTile(z, x, y) {
  return new Promise(ok => {
    const im = new Image();
    im.crossOrigin = 'anonymous';
    im.onload = () => ok(im); im.onerror = () => ok(null);
    im.src = IMG + z + '/' + y + '/' + x;
  });
}
function isWater(r, g, b) { return b >= g - 2 && b > r + 8 && (r + g + b) < 420; }

// leading run (in samples) of water lines from the start of `frac`
function run(frac, from, step) {
  let n = 0;
  for (let i = from; i >= 0 && i < frac.length && frac[i] >= WATER_COL; i += step) n++;
  return n;
}

export async function waterShift(map, size, zView) {
  const zs = Math.max(2, Math.min(15, Math.floor(zView) - 2));
  const b = map.getBounds();
  const nw = map.project(b.getNorthWest(), zs), se = map.project(b.getSouthEast(), zs);
  const x0 = Math.floor(nw.x), y0 = Math.floor(nw.y);
  const W = Math.max(8, Math.ceil(se.x - nw.x)), H = Math.max(8, Math.ceil(se.y - nw.y));
  const n = 1 << zs;
  const tx0 = Math.floor(nw.x / 256), tx1 = Math.floor((se.x - 1) / 256);
  const ty0 = Math.max(0, Math.floor(nw.y / 256)), ty1 = Math.min(n - 1, Math.floor((se.y - 1) / 256));
  if ((tx1 - tx0 + 1) * (ty1 - ty0 + 1) > 16) return null;
  const jobs = [];
  for (let ty = ty0; ty <= ty1; ty++) for (let tx = tx0; tx <= tx1; tx++)
    jobs.push(loadTile(zs, ((tx % n) + n) % n, ty).then(im => ({ im, tx, ty })));
  const got = await Promise.race([Promise.all(jobs), new Promise(r => setTimeout(() => r(null), 8000))]);
  if (!got || got.some(t => !t.im)) return null;
  const cv = document.createElement('canvas'); cv.width = W; cv.height = H;
  const cx = cv.getContext('2d', { willReadFrequently: true });
  got.forEach(t => cx.drawImage(t.im, t.tx * 256 - x0, t.ty * 256 - y0));
  let px;
  try { px = cx.getImageData(0, 0, W, H).data; } catch (e) { return null; }   // tainted: give up
  const col = new Float32Array(W), row = new Float32Array(H);
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const i = (y * W + x) * 4;
    if (isWater(px[i], px[i + 1], px[i + 2])) { col[x]++; row[y]++; }
  }
  for (let x = 0; x < W; x++) col[x] /= H;
  for (let y = 0; y < H; y++) row[y] /= W;

  // pixels of the sample -> pixels of the view
  const fx = size.x / W, fy = size.y / H;
  const axis = (frac, f, len, viewLen) => {
    const a = run(frac, 0, 1), z = run(frac, len - 1, -1);        // water at the start / the end
    const need = EDGE_RUN * len;
    if (a >= need && z < need) return Math.min(a * f, (0.5 - KEEP_IN) * viewLen);      // land lies ahead: pan forward
    if (z >= need && a < need) return -Math.min(z * f, (0.5 - KEEP_IN) * viewLen);
    return 0;
  };
  const dx = axis(col, fx, W, size.x), dy = axis(row, fy, H, size.y);
  return (dx || dy) ? [Math.round(dx), Math.round(dy)] : null;
}

async function fitLand(map, size, z, id) {
  let shift = null;
  try { shift = await waterShift(map, size, z); } catch (e) { shift = null; }
  if (!shift || id !== S.fitId) return;                          // nothing to do, or a newer fix arrived
  map.panBy(shift, { animate: false });
  drawPoi();                                                     // the picture moved: re-pick what is inside it
}

export default {
  async init(el, cfg) {
    S.miles = (cfg && cfg.miles) || 35;
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
    S.poiLayer = L.layerGroup().addTo(map);
    addPins();
    window.addEventListener('resize', () => { map.invalidateSize(); S.key = ''; });
  },

  render(el, data) {
    const map = S.map;
    if (!map) return;                                // the library is still loading
    const nav = data.nav;
    if (!nav || !nav.here) { say('Waiting for a location fix'); return; }
    if (S.note && S.note.textContent.startsWith('Waiting')) say('');

    const m = data.map && Number(data.map.miles);
    if (m > 0) S.miles = m;                          // the server's setting wins; changing it recentres below
    map.invalidateSize({ pan: false });
    const size = map.getSize();
    const h = nav.here;
    const key = [h.lat.toFixed(3), h.lon.toFixed(3), size.x, size.y, S.miles].join();
    if (key !== S.key && size.x > 0) {               // recentre only when something actually changed
      S.key = key;
      const z = zoomFor(h.lat, Math.min(size.x, size.y), S.miles);
      map.setView([h.lat, h.lon], z, { animate: false });
      fitLand(map, size, z, ++S.fitId);
    }
    S.here.setLatLng([h.lat, h.lon]).setOpacity(1);
    S.hereLL = [h.lat, h.lon];
    S.poiData = data.poi || S.poiData;                // keep the last good set if one poll comes back empty
    drawPoi();

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
