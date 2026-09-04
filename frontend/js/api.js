// === API Client ===
const API_BASE = '/api';

let _authed = false;
let _user = { username: null, role: null };

export function isAuthed() { return _authed; }
export function isAdmin() { return _authed && _user.role === 'admin'; }
export function currentUser() { return _user; }

export async function api(method, path, body = null) {
  const opts = {
    method,
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
    credentials: 'same-origin',
  };
  if (body) opts.body = JSON.stringify(body);
  const resp = await fetch(`${API_BASE}${path}`, opts);
  if (!resp.ok) {
    let msg = `${resp.status} ${resp.statusText}`;
    try {
      const err = await resp.json();
      if (err.message) msg = err.message;
      else if (err.error) msg = err.error;
    } catch {}
    if (resp.status === 401 || resp.status === 403) {
      if (resp.status === 401) { _authed = false; _user = { username: null, role: null }; }
    }
    throw new Error(msg);
  }
  const ct = resp.headers.get('content-type') || '';
  if (ct.includes('application/json')) return resp.json();
  return resp;
}

// Auth
export const checkAuth = () => api('GET', '/auth/status').then(r => {
  _authed = r.authenticated;
  _user = { username: r.username || null, role: r.role || null };
  return r;
});
export const login = (username, password) =>
  api('POST', '/auth/login', { username, password }).then(r => {
    _authed = true;
    _user = { username: r.username, role: r.role };
    return r;
  });
export const logout = () => api('POST', '/auth/logout').then(r => {
  _authed = false;
  _user = { username: null, role: null };
  return r;
});

// Transients
export const getTransients = (params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return api('GET', `/transients${qs ? '?' + qs : ''}`);
};
export const getTransient = (id) => api('GET', `/transients/${id}`);
// 轻量专用接口：统计/对比页全量元数据、列表页 tags 下拉（避免整表 per_page=10000）
export const getTransientMeta = () => api('GET', '/transients/meta');
export const getTransientTags = () => api('GET', '/transients/tags');
export const createTransient = (data) => api('POST', '/transients', data);
export const updateTransient = (id, data) => api('PUT', `/transients/${id}`, data);
export const deleteTransient = (id) => api('DELETE', `/transients/${id}`);

// Lightcurves
export const getLightcurves = (params = {}) => {
  const qs = new URLSearchParams(params).toString();
  return api('GET', `/lightcurves${qs ? '?' + qs : ''}`);
};
export const createLightcurves = (data) => api('POST', '/lightcurves/batch', data);
export const updateLightcurve = (id, data) => api('PUT', `/lightcurves/${id}`, data);
export const deleteLightcurve = (id) => api('DELETE', `/lightcurves/${id}`);
export const fitLightcurveModel = (payload) => api('POST', '/lightcurves/fit_model', payload);

// Filters
export const getFilters = () => api('GET', '/filters');

// Articles (相关研究文章)
export const getArticles = (tid) => api('GET', `/articles?transient_id=${encodeURIComponent(tid)}`);
export const createArticle = (data) => api('POST', '/articles', data);
export const updateArticle = (id, data) => api('PUT', `/articles/${id}`, data);
export const deleteArticle = (id) => api('DELETE', `/articles/${id}`);

// Spectra (光谱)
export const getSpectra = (tid) => api('GET', `/spectra?transient_id=${encodeURIComponent(tid)}`);
export const getSpectrum = (id) => api('GET', `/spectra/${id}`);
export const uploadSpectrum = (payload) => api('POST', '/spectra/upload', payload);
export const deleteSpectrumApi = (id) => api('DELETE', `/spectra/${id}`);

// Extinction (银河系消光改正)
export const getExtinctionStatus = () => api('GET', '/extinction/status');
export const runExtinction = (payload = {}) => api('POST', '/extinction/run', payload);
export const clearExtinction = (payload = {}) => api('POST', '/extinction/clear', payload);

// Fitting (余辉拟合)
export const getFittingEngines = () => api('GET', '/fitting/engines');
export const submitFittingJob = (payload) => api('POST', '/fitting/jobs', payload);
export const getFittingJobs = (tid) =>
  api('GET', `/fitting/jobs?transient_id=${encodeURIComponent(tid)}`);
export const getFittingJob = (id) => api('GET', `/fitting/jobs/${id}`);
export const getFittingJobFile = (id, kind) => api('GET', `/fitting/jobs/${id}/files/${kind}`);
export const deleteFittingJob = (id) => api('DELETE', `/fitting/jobs/${id}`);

// Hosts (宿主星系)
export const getHost = (tid) => api('GET', `/hosts/${encodeURIComponent(tid)}`);
export const saveHost = (tid, data) => api('PUT', `/hosts/${encodeURIComponent(tid)}`, data);
export const deleteHost = (tid) => api('DELETE', `/hosts/${encodeURIComponent(tid)}`);

// HostFit (宿主星系 SED 拟合)
export const getHostfitConfig = () => api('GET', '/hostfit/config');
export const submitHostfitJob = (payload) => api('POST', '/hostfit/jobs', payload);
export const getHostfitJobs = (tid) =>
  api('GET', `/hostfit/jobs?transient_id=${encodeURIComponent(tid)}`);
export const getHostfitJob = (id) => api('GET', `/hostfit/jobs/${id}`);
// 产物文件下载用裸 URL（<a href> / <img src> 直接引用）
export const hostfitJobFileUrl = (id, kind) => `${API_BASE}/hostfit/jobs/${id}/files/${kind}`;
export const deleteHostfitJob = (id) => api('DELETE', `/hostfit/jobs/${id}`);

// Stats
export const getOverview = () => api('GET', '/stats/overview');
export const getRedshiftDist = () => api('GET', '/stats/redshifts');
export const getBandCoverage = () => api('GET', '/stats/bands');
export const getHostStats = () => api('GET', '/stats/hosts');

// GCN (GCN 阅读工具)
export const getGcnIds = () => api('GET', '/gcn/ids');
export const getGcnCircular = (cid) => api('GET', `/gcn/${cid}`);
export const getGcnStatus = () => api('GET', '/gcn/status');
export const getGcnRelated = (cid) => api('GET', `/gcn/${cid}/related`);
export const updateGcnArchive = () => api('POST', '/gcn/update');

// Export
export const exportTransients = (fmt = 'csv') => {
  window.open(`${API_BASE}/export/transients?format=${fmt}`, '_blank');
};
export const exportLightcurves = (tid, fmt = 'csv') => {
  window.open(`${API_BASE}/export/lightcurves/${tid}?format=${fmt}`, '_blank');
};

// === Toast notification ===
export function showToast(msg, type = 'info') {
  const colors = {
    info: 'var(--accent-blue)', success: 'var(--accent-green)',
    danger: 'var(--accent-red)', warning: 'var(--accent-orange)',
  };
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = 'toast align-items-center show';
  toast.innerHTML = `
    <div class="d-flex">
      <div class="toast-body" style="color:${colors[type] || 'var(--text-secondary)'}">${msg}</div>
      <button type="button" class="btn-close me-2 m-auto" data-bs-dismiss="toast"></button>
    </div>`;
  container.appendChild(toast);
  setTimeout(() => toast.remove(), 5000);
}
