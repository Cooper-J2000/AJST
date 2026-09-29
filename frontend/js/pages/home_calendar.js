// === 事件日历（GitHub 贡献日历风格，按事件 T0 逐日计数）===
// 布局照 GitHub：列=周（周日起），行=星期，顶部月份标签，Mon/Wed/Fri 行标签。
// 颜色分 5 档（0 / 1 / 2–3 / 4–7 / ≥8），色阶变量 --cal-0..4 随主题切换（style.css）。

const DAY_MS = 86400000;
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function level(n) {
  if (!n) return 0;
  if (n === 1) return 1;
  if (n <= 3) return 2;
  if (n <= 7) return 3;
  return 4;
}

function iso(d) {
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}-${String(d.getUTCDate()).padStart(2, '0')}`;
}

// meta items → { counts: Map('YYYY-MM-DD' -> n), years: [有记录的年，升序] }
export function buildCalendarData(items) {
  const counts = new Map();
  const years = new Set();
  for (const it of items || []) {
    if (!it || !it.t0) continue;
    const day = String(it.t0).slice(0, 10);  // t0 为 naive UTC，直接取日期段，不做时区换算
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) continue;
    counts.set(day, (counts.get(day) || 0) + 1);
    years.add(Number(day.slice(0, 4)));
  }
  return { counts, years: [...years].sort((a, b) => a - b) };
}

// 该年的周列网格：从 1/1 所在周的周日（UTC）起，铺满到覆盖 12/31
function yearWeeks(year) {
  const jan1 = new Date(Date.UTC(year, 0, 1));
  let cur = new Date(jan1.getTime() - jan1.getUTCDay() * DAY_MS);
  const end = new Date(Date.UTC(year, 11, 31));
  const weeks = [];
  while (cur <= end) {
    const week = [];
    for (let dow = 0; dow < 7; dow++) {
      week.push(new Date(cur));
      cur = new Date(cur.getTime() + DAY_MS);
    }
    weeks.push(week);
  }
  return weeks;
}

// 挂载到容器；data 为空或无记录时清空容器（不渲染卡片）
export function mountCalendar(el, data) {
  if (!el) return;
  if (!data || !data.years.length) {
    el.innerHTML = '';
    el.style.display = 'none';  // 有 meta 但全部源无 T0：整卡隐藏，不留空框
    return;
  }
  el.style.display = '';
  let idx = data.years.length - 1;  // 默认最新有记录的年

  function draw() {
    const year = data.years[idx];
    const weeks = yearWeeks(year);
    let yearTotal = 0;

    // 月份标签：以每周第一个落在本年内的日子的月份为准，与上一列不同才标
    // （1/1 所在周从上年 12 月起，若用周日月份会在首列叠出 "Dec""Jan" 两个标签）
    const monthSlots = weeks.map((w, i) => {
      const inYear = w.find(d => d.getUTCFullYear() === year);
      const m = (inYear || w[0]).getUTCMonth();
      const prev = i > 0 ? (weeks[i - 1].find(d => d.getUTCFullYear() === year) || weeks[i - 1][0]).getUTCMonth() : -1;
      return m !== prev ? MONTHS[m] : '';
    });

    const weeksHtml = weeks.map(week => `<div class="cal-week">${week.map(d => {
      if (d.getUTCFullYear() !== year) return '<span class="cal-day cal-empty"></span>';
      const key = iso(d);
      const n = data.counts.get(key) || 0;
      yearTotal += n;
      const tip = n ? `${key} · ${n} 个事件` : `${key} · 无事件`;
      return `<span class="cal-day cal-l${level(n)}" title="${tip}"></span>`;
    }).join('')}</div>`).join('');

    el.innerHTML = `
      <div class="cal-head">
        <div class="swiss-kicker">Event Calendar — 事件日历<small class="swiss-kicker-hint">（按 T0 逐日计数）</small></div>
        <div class="cal-pager">
          <button type="button" class="cal-pg" data-d="-1" ${idx === 0 ? 'disabled' : ''} aria-label="上一年">‹</button>
          <span class="cal-year">${year}</span>
          <button type="button" class="cal-pg" data-d="1" ${idx === data.years.length - 1 ? 'disabled' : ''} aria-label="下一年">›</button>
        </div>
      </div>
      <div class="cal-scroll">
        <div class="cal-grid">
          <span></span>
          <div class="cal-months">${monthSlots.map(m => `<span>${m}</span>`).join('')}</div>
          <div class="cal-dows"><span></span><span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span></div>
          <div class="cal-weeks">${weeksHtml}</div>
        </div>
      </div>
      <div class="cal-foot">
        <span class="text-secondary">${year} 年共 ${yearTotal} 个事件</span>
        <span class="cal-legend">Less ${[0, 1, 2, 3, 4].map(i => `<i class="cal-day cal-l${i}"></i>`).join('')} More</span>
      </div>`;

    el.querySelectorAll('.cal-pg').forEach(btn => {
      btn.addEventListener('click', () => {
        const d = Number(btn.dataset.d);
        const next = idx + d;
        if (next < 0 || next >= data.years.length) return;
        idx = next;
        draw();
      });
    });
  }

  draw();
}
