// Satellite map, centred on the Mac's own location and about `data.map.miles` (a server setting: POST /display/map?miles=N, default 35;
// layout.json `miles` is only a fallback)
// across its shorter side. When a whole edge of the view is open water (the coast), the view is
// slid toward the land (fitLand) while the position dot stays at least 15% in from the edge. Pins in blue (post office), purple (library) and green (Costco) are the nearest few of each in view. Red pins are past hikes (display/pins.json, made by
// tools/make_pins.py); the amber line is the drive to the next stop. Leaflet is vendored under
// display/vendor/, so only the imagery tiles need internet: with the link down the map keeps
// its pins, route and position on a dark background and says so.
// Also drawn: interstates / motorways (thick, white) and trunk or US-numbered primary roads (thin) with
// route labels (data.roads, OpenStreetMap); yellow tent pins at every campground and trip-stop address
// (data.camps); white pins where the car sat parked 90 minutes or more (data.stops). Where pins overlap,
// campground beats hike beats tracked stop: a hike or stop within OVERLAP_M of a higher pin is not drawn.
const BASE = '/display/static/';
const S = { map: null, here: null, targetMark: null, line: [], key: '', routeKey: '', miles: 35, rem: 24, fitId: 0,
            loads: 0, errors: 0, note: null, poiData: null, poiLayer: null, poiKey: '', hereLL: null,
            pins: [], hikeGroup: null, hikeKey: '', campData: null, campLayer: null, campKey: '',
            stopData: null, stopLayer: null, stopKey: '',
            roadsData: null, roadV: null, roadWays: [], roadLayer: null, roadRenderer: null,
            roadLabelLayer: null, labelKey: '' };
const OVERLAP_M = 150;       // pins closer than this count as the same place

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

// ---- helpers (pure; exported so the smoke tests can run them in node) -----------------------------------
export function distM(a, b) {
  const R = 6371000, r = Math.PI / 180;
  const dla = (b.lat - a.lat) * r, dlo = (b.lon - a.lon) * r;
  const h = Math.sin(dla / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dlo / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

// Items ({lat, lon}) that are NOT within `m` metres of any blocker.
export function suppressNear(items, blockers, m) {
  if (!blockers || !blockers.length) return items;
  const dlat = m / 111000;
  return items.filter(it => !blockers.some(b => Math.abs(b.lat - it.lat) <= dlat && distM(it, b) < m));
}

// Google's encoded polyline (precision 1e-5), as the server sends the roads.
export function decodePoly(str) {
  const out = [];
  let i = 0, lat = 0, lon = 0;
  while (i < str.length) {
    for (let k = 0; k < 2; k++) {
      let b, shift = 0, res = 0;
      do { b = str.charCodeAt(i++) - 63; res |= (b & 0x1f) << shift; shift += 5; } while (b >= 0x20 && i < str.length + 1);
      const d = (res & 1) ? ~(res >> 1) : (res >> 1);
      if (k === 0) lat += d; else lon += d;
    }
    out.push([lat / 1e5, lon / 1e5]);
  }
  return out;
}

export function roadText(ref) {            // "I 5" -> "I-5", "US 101" -> "US-101"
  return String(ref || '').split(';')[0].trim().replace(/\s+/g, '-');
}

// Where to put route labels: per road number the longest stretch inside `bounds` ({s, w, n, e}), and a second
// and third one only when they are at least `gapM` from the ones already placed. At most `max` labels.
export function labelSpots(ways, bounds, gapM, max) {
  const inside = p => p[0] >= bounds.s && p[0] <= bounds.n && p[1] >= bounds.w && p[1] <= bounds.e;
  const cand = [];
  for (const w of ways) {
    if (!w.ref) continue;
    const pts = w.pts.filter(inside);
    if (pts.length < 2) continue;
    let len = 0;
    for (let i = 1; i < pts.length; i++) len += distM({ lat: pts[i - 1][0], lon: pts[i - 1][1] }, { lat: pts[i][0], lon: pts[i][1] });
    const mid = pts[Math.floor(pts.length / 2)];
    cand.push({ ref: w.ref, k: w.k, len, lat: mid[0], lon: mid[1] });
  }
  cand.sort((a, b) => b.len - a.len);
  const out = [], perRef = {};
  for (const c of cand) {
    if (out.length >= max) break;
    if ((perRef[c.ref] || 0) >= 3) continue;
    if (out.some(o => distM(o, c) < (o.ref === c.ref ? gapM : gapM / 3))) continue;
    perRef[c.ref] = (perRef[c.ref] || 0) + 1;
    out.push({ text: roadText(c.ref), k: c.k, lat: c.lat, lon: c.lon, ref: c.ref });
  }
  return out;
}

const PIN_PATH = '<path d="M15 1C7.3 1 1 7.1 1 14.7 1 25.2 15 41 15 41s14-15.8 14-26.3C29 7.1 22.7 1 15 1z"';
function glyphPin(cls, fill, stroke, glyph, rem) {
  const w = Math.round(rem * 1.6), h = Math.round(rem * 2.24);
  const svg = '<svg width="' + w + '" height="' + h + '" viewBox="0 0 30 42" xmlns="http://www.w3.org/2000/svg">' +
    PIN_PATH + ' fill="' + fill + '" stroke="' + stroke + '" stroke-width="2"/>' + glyph + '</svg>';
  return L.divIcon({ className: cls, html: svg, iconSize: [w, h], iconAnchor: [w / 2, h - 1] });
}
// yellow pin, tent: campgrounds and trip-stop addresses
function tentIcon(rem) {
  return glyphPin('poi-pin', '#ffd21f', '#fff',
    '<path d="M15 7.4 6.4 20.8h17.2z" fill="#3a2d00"/><path d="M15 12.6 11.8 20.8h6.4z" fill="#ffd21f"/>' +
    '<path d="M5.6 21.6h18.8" stroke="#3a2d00" stroke-width="1.7" stroke-linecap="round"/>', rem);
}
// white pin: somewhere the car sat parked for a while
function stopIcon(rem) {
  return glyphPin('poi-pin', '#ffffff', '#1c1c1c', '<circle cx="15" cy="14.6" r="4.8" fill="#1c1c1c"/>', rem);
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

// Nearest `count` of each kind among those inside the current view AND within `maxMiles` of the Mac
// (so a wide map still only shows what is close; 0 or missing = no distance limit).
export function pickPoi(items, bounds, here, count, maxMiles) {
  const out = [];
  const near = p => !(maxMiles > 0) || distM({ lat: here[0], lon: here[1] }, p) <= maxMiles * 1609.344;
  for (const kind of Object.keys(POI)) {
    const inView = items.filter(p => p.t === kind && bounds.contains([p.lat, p.lon]) && near(p))
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
  const picked = d && here && d.items ? pickPoi(d.items, map.getBounds(), here, d.count || 5, d.max_miles) : [];
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
  S.pins = pins.map(p => ({ lat: p[0], lon: p[1] }));
  buildHikes();
}

// Red hike pins, minus the ones that sit on a campground pin (campground wins).
function buildHikes() {
  if (!S.map || !S.pins.length) return;
  const camps = (S.campData && S.campData.items) || [];
  const keep = suppressNear(S.pins, camps, OVERLAP_M);
  const key = (S.campData ? S.campData.v : 0) + '|' + keep.length + '|' + S.rem;
  if (key === S.hikeKey) return;
  S.hikeKey = key;
  if (S.hikeGroup) S.map.removeLayer(S.hikeGroup);
  const icon = pinIcon(S.rem);
  const group = L.markerClusterGroup({ maxClusterRadius: 50, showCoverageOnHover: false, spiderfyOnMaxZoom: false,
    zoomToBoundsOnClick: false, disableClusteringAtZoom: 15, chunkedLoading: true, animate: false });
  group.addLayers(keep.map(p => L.marker([p.lat, p.lon], { icon, interactive: false, keyboard: false })));
  group.addTo(S.map);
  S.hikeGroup = group;
}

function drawCamps() {
  if (!S.map || !S.campLayer || !S.campData) return;
  const key = S.campData.v + '|' + S.campData.items.length + '|' + S.rem;
  if (key === S.campKey) return;
  S.campKey = key;
  S.campLayer.clearLayers();
  const icon = tentIcon(S.rem);
  S.campData.items.forEach(c => L.marker([c.lat, c.lon], { icon, interactive: false, keyboard: false, pane: 'campPane' }).addTo(S.campLayer));
  buildHikes();                                                  // hikes under a tent pin step aside
  S.stopKey = '';                                                // and so do stops
}

function drawStops() {
  if (!S.map || !S.stopLayer || !S.stopData) return;
  const camps = (S.campData && S.campData.items) || [];
  const shown = suppressNear(suppressNear(S.stopData.items, camps, OVERLAP_M), S.pins, OVERLAP_M);   // campground > hike > stop
  const key = shown.map(p => p.lat + ',' + p.lon).join('|') + '@' + S.rem;
  if (key === S.stopKey) return;
  S.stopKey = key;
  S.stopLayer.clearLayers();
  const icon = stopIcon(S.rem);
  shown.forEach(p => L.marker([p.lat, p.lon], { icon, interactive: false, keyboard: false, pane: 'stopPane' }).addTo(S.stopLayer));
}

// Highways: lines are rebuilt when the server's copy changes; labels when the view moves.
function drawRoads() {
  const map = S.map, d = S.roadsData;
  if (!map || !S.roadLayer || !d || !d.ways) return;
  const r = S.rem;
  if (d.v !== S.roadV) {
    S.roadV = d.v;
    S.roadWays = d.ways.map(w => ({ k: w[0], ref: w[1], pts: decodePoly(w[2]) }));
    S.roadLayer.clearLayers();
    const I = S.roadWays.filter(w => w.k === 'i').map(w => w.pts), H = S.roadWays.filter(w => w.k !== 'i').map(w => w.pts);
    const base = { interactive: false, lineCap: 'round', lineJoin: 'round', renderer: S.roadRenderer };
    [[H, '#0a0e18', .22, .55], [I, '#0a0e18', .30, .55], [H, '#f0f0f0', .10, .85], [I, '#ffffff', .17, .95]].forEach(a => {
      if (a[0].length) S.roadLayer.addLayer(L.polyline(a[0], { ...base, color: a[1], weight: Math.max(1.5, r * a[2]), opacity: a[3] }));
    });
    S.labelKey = '';
  }
  const b = map.getBounds();
  const key = S.roadV + '|' + [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()].map(x => x.toFixed(3)).join() + '|' + r;
  if (key === S.labelKey) return;
  S.labelKey = key;
  S.roadLabelLayer.clearLayers();
  const gapM = S.miles * 1609.344 * 0.35;
  labelSpots(S.roadWays, { s: b.getSouth(), w: b.getWest(), n: b.getNorth(), e: b.getEast() }, gapM, 14).forEach(sp => {
    const bg = sp.k === 'i' ? '#1f4eb4' : '#26272c';
    const html = '<span style="display:inline-block;transform:translate(-50%,-50%);padding:' + Math.round(r * .06) + 'px ' + Math.round(r * .3) +
      'px;background:' + bg + ';color:#fff;border:1.5px solid rgba(255,255,255,.85);border-radius:' + Math.round(r * .3) +
      'px;font:700 ' + Math.round(r * .52) + 'px/1.15 system-ui,sans-serif;white-space:nowrap;box-shadow:0 1px 3px rgba(0,0,0,.6)">' + sp.text + '</span>';
    L.marker([sp.lat, sp.lon], { interactive: false, keyboard: false, pane: 'roadlabels',
      icon: L.divIcon({ className: 'road-label', html, iconSize: [0, 0] }) }).addTo(S.roadLabelLayer);
  });
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
  drawRoads();                                                   // and re-place the road labels
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
    [['roads', 380], ['roadlabels', 390], ['stopPane', 590], ['campPane', 650], ['herePane', 700]].forEach(a => {
      map.createPane(a[0]).style.zIndex = a[1];
      map.getPane(a[0]).style.pointerEvents = 'none';
    });
    S.roadRenderer = L.canvas({ pane: 'roads', padding: 0.1 });
    L.control.scale({ imperial: true, metric: false, position: 'bottomleft' }).addTo(map);
    tiles().forEach(l => l.addTo(map));
    map.setView([39, -98], 4);                       // until the first fix arrives
    S.map = map;

    const d = Math.round(S.rem * 2.4);
    S.here = L.marker([0, 0], { interactive: false, keyboard: false, zIndexOffset: 1000, opacity: 0, pane: 'herePane',
      icon: L.divIcon({ className: 'rv-here', html: '<span class="pulse"></span><span class="dot"></span>',
                        iconSize: [d, d], iconAnchor: [d / 2, d / 2] }) }).addTo(map);
    S.poiLayer = L.layerGroup().addTo(map);
    S.roadLayer = L.layerGroup().addTo(map);
    S.roadLabelLayer = L.layerGroup().addTo(map);
    S.stopLayer = L.layerGroup().addTo(map);
    S.campLayer = L.layerGroup().addTo(map);
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
    S.roadsData = data.roads || S.roadsData;          // same for the layers below
    S.campData = data.camps || S.campData;
    S.stopData = data.stops || S.stopData;
    drawRoads();
    drawCamps();
    drawStops();

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
      S.line.push(L.marker([t.lat, t.lon], { interactive: false, keyboard: false, zIndexOffset: 900, pane: 'herePane',
        icon: L.divIcon({ className: 'rv-target', html: '<span></span>', iconSize: [d, d], iconAnchor: [d / 2, d / 2] }) }).addTo(map));
    }
  },
};
