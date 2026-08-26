// === TNS 风格谱线对比标记 ===
// 谱线组定义逐字取自 wis-tns.org 对象页（drupal-settings objectFlot.*.params.markings）：
// 组名/颜色/静止系波长与 TNS 一致；自定义 1–4 为自由波长输入；Tellurics 为波段（不做 z/v 偏移）
// 线位置换算（同 TNS 面板语义）：λ_显示 = λ0·(1+z)·(1 − v_exp/c)，v_exp>0 表示抛射蓝移

const C_KMS = 299792.458;

export const SPEC_LINE_GROUPS = [
  // ── 元素离子（z + v_exp） ──
  { key: 'h',      name: 'H',       color: '#0066cc', hasZ: 1, hasV: 1, lines: [3970, 4102, 4341, 4861, 6563, 10052, 10941, 12822, 18756],
    title: 'H: [Balmer] 3970,4102,4341,4861,6563, [Paschen] 10052,10941,12822,18756' },
  { key: 'he',     name: 'He I',    color: '#cc0000', hasZ: 1, hasV: 1, lines: [3889, 4471, 5876, 6678, 7065], title: 'HeI: 3889,4471, 5876,6678,7065' },
  { key: 'he_ii',  name: 'He II',   color: '#ff8000', hasZ: 1, hasV: 1, lines: [3203, 4686, 5411, 6560, 6683, 6891, 8237, 10124],
    title: 'HeII: 3203,4686,5411,6560,6683,6891,8237,10124' },
  { key: 'c_ii',   name: 'C II',    color: '#cccc00', hasZ: 1, hasV: 1, lines: [3919, 3921, 4267, 5145, 5890, 6578, 7231, 7236, 9234, 9891],
    title: 'CII: 3919,3921,4267,5145,5890,6578,7231,7236,9234,9891' },
  { key: 'c_iii',  name: 'C III',   color: '#4c9900', hasZ: 1, hasV: 1, lines: [4647, 4650, 5696, 6742, 8500, 8665, 9711],
    title: 'CIII: 4647,4650,5696,6742,8500,8665,9711' },
  { key: 'c_iv',   name: 'C IV',    color: '#0066cc', hasZ: 1, hasV: 1, lines: [4658, 5801, 5812, 7061, 7726, 8859], title: 'CIV: 4658,5801,5812,7061,7726,8859' },
  { key: 'n_ii',   name: 'N II',    color: '#ff8000', hasZ: 1, hasV: 1, lines: [3995, 4631, 5005, 5680, 5942, 6482, 6611],
    title: 'NII: 3995,4631,5005,5680,5942,6482,6611' },
  { key: 'n_iii',  name: 'N III',   color: '#4c9900', hasZ: 1, hasV: 1, lines: [4634, 4641, 4687, 5321, 5327, 6467], title: 'NIII: 4634,4641,4687,5321,5327,6467' },
  { key: 'n_iv',   name: 'N IV',    color: '#0066cc', hasZ: 1, hasV: 1, lines: [3479, 3483, 3485, 4058, 6381, 7115], title: 'NIV: 3479,3483,3485,4058,6381,7115' },
  { key: 'n_v',    name: 'N V',     color: '#6600cc', hasZ: 1, hasV: 1, lines: [4604, 4620, 4945], title: 'NV: 4604,4620,4945' },
  { key: 'o',      name: 'O I',     color: '#cc0000', hasZ: 1, hasV: 1, lines: [6158, 7772, 7774, 7775, 8446, 9263], title: 'OI: 6158,7772,7774,7775,8446,9263' },
  { key: 'o_f',    name: '[O I]',   color: '#ff8000', hasZ: 1, hasV: 1, lines: [5577, 6300, 6363], title: '[OI]: 5577,6300,6363' },
  { key: 'o_ii',   name: 'O II',    color: '#cccc00', hasZ: 1, hasV: 1, lines: [3390, 3377, 4416, 6641, 6721, 3738, 3960, 4115, 4358, 4651],
    title: 'OII: 3390,3377,4416,6641,6721; [SLSN-I blends] 3738,3960,4115,4358,4651' },
  { key: 'o_iif',  name: '[O II]',  color: '#4c9900', hasZ: 1, hasV: 1, lines: [3726, 3729], title: '[OII]: 3726,3729' },
  { key: 'o_iiif', name: '[O III]', color: '#0066cc', hasZ: 1, hasV: 1, lines: [4363, 4959, 5007], title: '[OIII]: 4363, 4959,5007' },
  { key: 'o_v',    name: 'O V',     color: '#6600cc', hasZ: 1, hasV: 1, lines: [3145, 4124, 4930, 5598, 6500], title: 'OV: 3145,4124,4930,5598,6500' },
  { key: 'o_vi',   name: 'O VI',    color: '#cc0066', hasZ: 1, hasV: 1, lines: [3811, 3834], title: 'OVI: 3811,3834' },
  { key: 'na',     name: 'Na I',    color: '#666600', hasZ: 1, hasV: 1, lines: [5890, 5896, 8183, 8195], title: 'NaI: 5890,5896, 8183,8195' },
  { key: 'mg',     name: 'Mg I',    color: '#cc0000', hasZ: 1, hasV: 1, lines: [3829, 3832, 3838, 4571, 4703, 5167, 5173, 5184, 5528, 8807],
    title: 'MgI: 3829,3832,3838, 4571,4703, 5167,5173,5184, 5528,8807' },
  { key: 'mg_ii',  name: 'Mg II',   color: '#cccc00', hasZ: 1, hasV: 1, lines: [2796, 2798, 2803, 4481, 7877, 7896, 8214, 8235, 9218, 9244, 9632],
    title: 'MgII: 2796,2798,2803, 4481,7877,7896,8214,8235,9218,9244,9632' },
  { key: 'si_ii',  name: 'Si II',   color: '#006666', hasZ: 1, hasV: 1, lines: [4128, 4131, 5958, 5979, 6347, 6371], title: 'SiII: 4128,4131,5958,5979,6347,6371' },
  { key: 's_ii',   name: 'S II',    color: '#003366', hasZ: 1, hasV: 1, lines: [5433, 5454, 5606, 5640, 5647, 6715, 13529, 14501],
    title: 'SII: 5433,5454,5606,5640,5647,6715, 13529,14501' },
  { key: 'ca_ii',  name: 'Ca II',   color: '#660066', hasZ: 1, hasV: 1, lines: [3159, 3180, 3706, 3737, 3934, 3969, 8498, 8542, 8662],
    title: 'CaII: 3159,3180,3706,3737, [H&K] 3934,3969, [IR-trip] 8498,8542,8662' },
  { key: 'ca_iif', name: '[Ca II]', color: '#990099', hasZ: 1, hasV: 1, lines: [7292, 7324], title: '[CaII]: 7292,7324' },
  { key: 'fe_ii',  name: 'Fe II',   color: '#660033', hasZ: 1, hasV: 1, lines: [4303, 4352, 4515, 4549, 4924, 5018, 5169, 5198, 5235, 5363],
    title: 'FeII: 4303,4352,4515,4549,4924,5018,5169,5198,5235,5363' },
  { key: 'fe_iii', name: 'Fe III',  color: '#99004C', hasZ: 1, hasV: 1, lines: [4397, 4421, 4432, 5129, 5158], title: 'FeIII: 4397,4421,4432,5129,5158' },
  // ── 自定义波长（z + v_exp，输入静止系波长 Å） ──
  { key: 'custom',  name: '自定义 1', color: '#980000', hasZ: 1, hasV: 1, custom: 1, title: '' },
  { key: 'custom2', name: '自定义 2', color: '#a77f03', hasZ: 1, hasV: 1, custom: 1, title: '' },
  { key: 'custom3', name: '自定义 3', color: '#0c8900', hasZ: 1, hasV: 1, custom: 1, title: '' },
  { key: 'custom4', name: '自定义 4', color: '#004b83', hasZ: 1, hasV: 1, custom: 1, title: '' },
  // ── 特殊组 ──
  { key: 'tell', name: 'Tellurics', color: '#A0A0A0', hasZ: 0, hasV: 0, bands: [[6867, 6884], [7594, 7621]],
    title: 'Tellurics: 6867-6884, 7594-7621' },
  { key: 'gal',  name: 'Galaxy lines', color: '#404040', hasZ: 1, hasV: 0,
    lines: [4341, 4861, 6563, 6548, 6583, 3727, 4959, 5007, 5890, 5896, 2798, 6717, 6731, 3969, 3934,
            2025, 2056, 2062, 2066, 2249, 2260, 2343, 2374, 2382, 2586, 2599, 2576, 2594, 2852],
    title: 'Galaxy lines: H 4341,4861,6563; NII 6548,6583; [OII] 3727; [OIII] 4959,5007; NaI 5890,5896; MgII 2798; SII 6717,6731; CaII H&K 3969,3934; ZnII 2025; CrII 2056,2062,2066; FeII 2249,2260,2343,2374,2382,2586,2599; MnII 2576,2594; MgI 2852' },
  { key: 'WR_WN', name: 'WR-WN',   color: '#6600cc', hasZ: 1, hasV: 1,
    lines: [4341, 4861, 6563, 4686, 5412, 10124, 5801, 4640, 4058, 4537, 7109, 7123, 4604, 4946],
    title: 'WR_WN lines: H: 4341,4861,6563; HeII: 4686,5412,10124; CIV: 5801; NIII: 4640; NIV: 4058,4537,7109,7123; NV: 4604,4946' },
  { key: 'WR_WC', name: 'WR-WC/O', color: '#cc0066', hasZ: 1, hasV: 1,
    lines: [4341, 4861, 6563, 7065, 6678, 5876, 4472, 3886, 4686, 7236, 4647, 5696, 6742, 9711, 5801, 7726, 5598, 3811, 3834],
    title: 'WR_WC/O lines: H: 4341,4861,6563; HeI: 7065,6678,5876,4472,3886; HeII: 4686; CII: 7236; CIII: 4647,5696,6742,9711; CIV: 5801,7726; OV: 5598; OVI: 3811,3834' },
];

// 面板分栏（仿 TNS：左两栏元素离子，右栏 custom / tellurics+galaxy+WR）
export const SPEC_MARKING_COLUMNS = [
  ['h', 'he', 'he_ii', 'c_ii', 'c_iii', 'c_iv', 'n_ii', 'n_iii', 'n_iv'],
  ['n_v', 'o', 'o_f', 'o_ii', 'o_iif', 'o_iiif', 'o_v', 'o_vi', 'na'],
  ['mg', 'mg_ii', 'si_ii', 's_ii', 'ca_ii', 'ca_iif', 'fe_ii', 'fe_iii'],
  ['custom', 'custom2', 'custom3', 'custom4', 'tell', 'gal', 'WR_WN', 'WR_WC'],
];

// Chart.js 插件工厂：getState() 返回 {组key: {on, z, v, wl}}
export function createSpecLinesPlugin(getState) {
  return {
    id: 'specLines',
    afterDraw(chart) {
      const state = getState();
      if (!state) return;
      const x = chart.scales.x;
      const area = chart.chartArea;
      if (!x || !area) return;
      const ctx = chart.ctx;
      const xmin = x.min, xmax = x.max;
      ctx.save();
      ctx.font = '9px sans-serif';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      for (const g of SPEC_LINE_GROUPS) {
        const st = state[g.key];
        if (!st || !st.on) continue;
        if (g.bands) {
          // Tellurics：观测系波段，灰色半透明竖带
          ctx.fillStyle = g.color + '44';
          for (const [a, b] of g.bands) {
            if (b < xmin || a > xmax) continue;
            const px0 = x.getPixelForValue(Math.max(a, xmin));
            const px1 = x.getPixelForValue(Math.min(b, xmax));
            ctx.fillRect(px0, area.top, px1 - px0, area.bottom - area.top);
          }
          continue;
        }
        const f = (1 + (st.z || 0)) * (1 - (st.v || 0) / C_KMS);
        const lams = g.custom ? (isFinite(st.wl) ? [st.wl] : []) : g.lines;
        ctx.strokeStyle = g.color;
        ctx.fillStyle = g.color;
        ctx.setLineDash([5, 3]);
        ctx.lineWidth = 1;
        let li = 0;
        for (const l0 of lams) {
          const lam = l0 * f;
          if (lam < xmin || lam > xmax) continue;
          const px = x.getPixelForValue(lam);
          ctx.beginPath();
          ctx.moveTo(px, area.top);
          ctx.lineTo(px, area.bottom);
          ctx.stroke();
          // 组名标签（交替两级高度防重叠）
          ctx.fillText(g.custom ? `${Math.round(l0)}` : g.name, px + 2, area.top + 2 + (li % 2) * 10);
          li++;
        }
        ctx.setLineDash([]);
      }
      ctx.restore();
    },
  };
}

// TNS 风格标记面板 HTML；zDefault 为该源红移
export function buildMarkingsPanelHTML(zDefault) {
  const z = (zDefault != null && isFinite(zDefault)) ? zDefault : 0;
  const rowHTML = (g) => {
    const zIn = g.hasZ ? ` z=<input type="number" class="spec-mk-z" step="0.01" value="${z}"
        style="width:64px" onchange="specMarkingSet('${g.key}','z',this.value)">` : '';
    const vIn = g.hasV ? ` v<sub>exp</sub>=<input type="number" class="spec-mk-v" step="1000" value="0"
        style="width:70px" onchange="specMarkingSet('${g.key}','v',this.value)"> km/s` : '';
    const wlIn = g.custom ? `<input type="text" class="spec-mk-wl" size="6" placeholder="λ₀"
        style="width:64px" onchange="specMarkingSet('${g.key}','wl',this.value)"> Å` : '';
    return `<div class="text-nowrap small" title="${g.title.replace(/"/g, '&quot;')}">
      <input type="checkbox" onchange="specMarkingToggle('${g.key}', this.checked)">
      <span style="color:${g.color}">▪</span> ${g.custom ? wlIn : g.name}${zIn}${vIn}
    </div>`;
  };
  const cols = SPEC_MARKING_COLUMNS.map(keys =>
    `<div class="d-flex flex-column gap-1 me-3">${keys.map(k => rowHTML(SPEC_LINE_GROUPS.find(g => g.key === k))).join('')}</div>`
  ).join('');
  return `<div class="d-flex flex-wrap">${cols}</div>
    <div class="small text-secondary mt-1">
      z-step: <input type="text" size="4" value="0.01" onchange="specMarkingStep('z', this.value)">
      &nbsp; v-step: <input type="text" size="4" value="1000" onchange="specMarkingStep('v', this.value)">
      &nbsp;（λ = λ₀·(1+z)·(1−v/c)，v<sub>exp</sub>&gt;0 为蓝移；悬停各组可查看谱线波长清单）
    </div>`;
}
