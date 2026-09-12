// === Aladin Lite 全天图（详情页概览标签） ===
// 模块级实例引用：容器随 DOM 重建销毁，render 时必须经 resetAladin() 清空旧引用，
// 否则 SPA 内切源后第二个源的全天图永久空白（initAladin 见非空即返回）。
let aladinInstance = null;

export function resetAladin() {
  aladinInstance = null;
}

export function initAladin(ra, dec, id) {
  if (aladinInstance) return;
  const container = document.getElementById('aladinContainer');
  if (!container) return;
  try {
    aladinInstance = A.aladin('#aladinContainer', {
      target: `${ra} ${dec}`,
      fov: 0.1,
      survey: 'P/DSS2/color',
      showReticle: true,
      showSimbadPointer: false,
      showCooGrid: true,
    });
    // Aladin Lite v3：marker 需挂在 catalog 图层上（v2 的 addMarker 已移除）；
    // id 为 null 表示无坐标源（图示为银河系中心占位），不放置源标记
    if (id != null) {
      const cat = A.catalog({ name: id });
      aladinInstance.addCatalog(cat);
      cat.addSources([
        A.marker(ra, dec, { popupTitle: id, popupDesc: `RA=${ra.toFixed(4)}°, Dec=${dec.toFixed(4)}°` })
      ]);
    }
  } catch (err) {
    console.error('Aladin init error:', err);
  }
}
