const APPS = {
  'com.webos.app.hdmi1': 'HDMI 1', 'com.webos.app.hdmi2': 'HDMI 2', 'com.webos.app.hdmi3': 'HDMI 3',
  'netflix': 'Netflix', 'amazon': 'Prime Video', 'youtube.leanback.v4': 'YouTube',
};

function item(parent, text, dot) {
  const s = document.createElement('span');
  if (dot !== undefined) {
    const d = document.createElement('span');
    d.className = 'dot' + (dot ? ' on' : '');
    s.appendChild(d);
  }
  s.appendChild(document.createTextNode(text));
  parent.appendChild(s);
}

export default {
  render(el, data) {
    const tv = data.tv;
    el.replaceChildren();
    if (tv.power !== 'on') { item(el, 'TV is off', false); return; }
    item(el, 'TV on', true);
    if (tv.volume != null) item(el, tv.muted ? 'Muted' : 'Volume ' + tv.volume);
    if (tv.app) item(el, 'Showing ' + (APPS[tv.app] || tv.app));
  },
};
