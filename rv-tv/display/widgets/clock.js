const time = new Intl.DateTimeFormat([], { hour: 'numeric', minute: '2-digit' });
const date = new Intl.DateTimeFormat([], { weekday: 'long', month: 'long', day: 'numeric' });

export default {
  init(el) {
    el.innerHTML = '<div class="clock-date"></div>' +                    // "Friday, October 16", then the time, at the right edge
                   '<div class="clock-time"><span class="hm"></span><span class="ap"></span></div>';
    this.tick(el);
  },
  tick(el) {
    const parts = time.formatToParts(new Date());
    const hm = parts.filter(p => p.type !== 'dayPeriod').map(p => p.value).join('').replace(/[\s ]+$/, '');
    const ap = (parts.find(p => p.type === 'dayPeriod') || {}).value || '';
    const d = date.format(new Date());
    set(el.querySelector('.hm'), hm);
    set(el.querySelector('.ap'), ap);
    set(el.querySelector('.clock-date'), d);
  },
};

function set(node, text) { if (node.textContent !== text) node.textContent = text; }
