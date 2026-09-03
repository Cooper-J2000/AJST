// === Create New Transient ===
import { app, showLoading, showError } from './layout.js';
import { createTransient, saveHost, showToast } from '../api.js';
import { parseRA, parseDec, attachCoordHint } from '../coords.js';

export async function render() {
  app.innerHTML = `
    <div class="mb-3">
      <a href="#/" class="text-secondary text-decoration-none small"><i class="bi bi-arrow-left"></i> 返回列表</a>
    </div>
    <div class="page-header"><h4><i class="bi bi-plus-circle"></i> 新建暂现源事件</h4></div>

    <div class="row">
      <div class="col-lg-8">
        <div class="card">
          <div class="card-header">基本信息 <small class="text-secondary">* 为必填</small></div>
          <div class="card-body">
            <form id="newTransientForm" onsubmit="return submitNewTransient()">
              <div class="row g-3">
                <div class="col-md-6">
                  <label class="form-label">事件 ID *</label>
                  <input type="text" class="form-control" id="fId" required pattern="^[A-Z0-9a-z]+$" placeholder="EP251202a">
                  <div class="form-text">唯一标识符，如 EP251202a</div>
                </div>
                <div class="col-md-6">
                  <label class="form-label">触发仪器</label>
                  <input type="text" class="form-control" id="fTrigger" placeholder="WXT">
                </div>
                <div class="col-md-4">
                  <label class="form-label">RA</label>
                  <input type="text" class="form-control" id="fRa" placeholder="122.1142 或 08h08m27.4s">
                </div>
                <div class="col-md-4">
                  <label class="form-label">Dec</label>
                  <input type="text" class="form-control" id="fDec" placeholder="40.61244 或 +40d36m44.8s">
                </div>
                <div class="col-md-4">
                  <label class="form-label">T0</label>
                  <input type="text" class="form-control" id="fT0" placeholder="2025-12-02T01:48:23Z">
                </div>
                <div class="col-md-3">
                  <label class="form-label">红移</label>
                  <input type="number" class="form-control" id="fZ" step="any" placeholder="2.785">
                </div>
                <div class="col-md-3">
                  <label class="form-label">红移类型</label>
                  <select class="form-select" id="fZType">
                    <option value="">-</option>
                    <option value="value">value</option>
                    <option value="phot_z">phot_z</option>
                    <option value="upperlimit">upperlimit</option>
                  </select>
                </div>
                <div class="col-md-6">
                  <label class="form-label">红移引用</label>
                  <input type="text" class="form-control" id="fZRef" placeholder="GCN 42939">
                </div>
                <div class="col-md-3">
                  <label class="form-label">位置误差</label>
                  <input type="number" class="form-control" id="fPosErr" step="any" placeholder="0.5">
                </div>
                <div class="col-md-3">
                  <label class="form-label">误差单位</label>
                  <select class="form-select" id="fPosErrUnit">
                    <option value="arcsec">arcsec</option>
                    <option value="deg">deg</option>
                    <option value="arcmin">arcmin</option>
                  </select>
                </div>
                <div class="col-md-6">
                  <label class="form-label">位置引用</label>
                  <input type="text" class="form-control" id="fPosRef" placeholder="GCN 42956">
                </div>
                <div class="col-md-6">
                  <label class="form-label">标签 (逗号分隔)</label>
                  <input type="text" class="form-control" id="fTags" placeholder="fxt, grb">
                </div>
                <div class="col-md-6">
                  <label class="form-label">别名 (逗号分隔)</label>
                  <input type="text" class="form-control" id="fAliases" placeholder="GRB251202A, AT2025xxxx">
                </div>
              </div>
              <!-- 宿主星系（可选，可后补） -->
              <div class="mt-3">
                <button class="btn btn-sm btn-outline-secondary" type="button" data-bs-toggle="collapse"
                        data-bs-target="#hostCollapse" aria-expanded="false" aria-controls="hostCollapse">
                  <i class="bi bi-houses"></i> 宿主星系（可选，可后补）
                </button>
                <div class="collapse mt-2" id="hostCollapse">
                  <div class="row g-3 border rounded p-2">
                    <div class="col-md-4">
                      <label class="form-label">宿主 RA</label>
                      <input type="text" class="form-control" id="fHostRa" placeholder="度 或 08h08m27.4s">
                    </div>
                    <div class="col-md-4">
                      <label class="form-label">宿主 Dec</label>
                      <input type="text" class="form-control" id="fHostDec" placeholder="度 或 +40d36m44.8s">
                    </div>
                    <div class="col-md-2">
                      <label class="form-label">宿主红移</label>
                      <input type="number" class="form-control" id="fHostZ" step="any">
                    </div>
                    <div class="col-md-2">
                      <label class="form-label">红移类型</label>
                      <select class="form-select" id="fHostZType">
                        <option value="">-</option>
                        <option value="spec">spec</option>
                        <option value="phot">phot</option>
                      </select>
                    </div>
                  </div>
                </div>
              </div>
              <div class="mt-4 d-flex gap-2">
                <button type="submit" class="btn btn-primary"><i class="bi bi-check-lg"></i> 创建事件</button>
                <a href="#/" class="btn btn-outline-secondary">取消</a>
              </div>
            </form>
          </div>
        </div>
      </div>
      <div class="col-lg-4">
        <div class="card">
          <div class="card-header">提示</div>
          <div class="card-body small text-secondary">
            <ul class="mb-0 ps-3">
              <li>事件 ID 是唯一标识，不可重复</li>
              <li>坐标支持十进制度或时分秒（如 122.1142 或 08h08m27.4s / +40d36m44.8s），入库统一转度</li>
              <li>T0 格式: ISO 8601</li>
              <li>创建后可在详情页添加光变数据</li>
              <li>所有字段均可后续编辑</li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  `;

  attachCoordHint(document.getElementById('fRa'), true);
  attachCoordHint(document.getElementById('fDec'), false);
  attachCoordHint(document.getElementById('fHostRa'), true);
  attachCoordHint(document.getElementById('fHostDec'), false);

  window.submitNewTransient = async () => {
    const getId = (id) => document.getElementById(id)?.value?.trim() || null;
    const coord = (id, isRA, label) => {
      const v = isRA ? parseRA(getId(id)) : parseDec(getId(id));
      if (typeof v === 'number' && isNaN(v)) {
        showToast(`${label} 格式无法解析（支持十进制度或时分秒）`, 'danger');
        return undefined;
      }
      return v;
    };

    const id = getId('fId');
    if (!id) { showToast('事件 ID 不能为空', 'danger'); return false; }

    const raV = coord('fRa', true, 'RA');
    if (raV === undefined) return false;
    const decV = coord('fDec', false, 'Dec');
    if (decV === undefined) return false;
    const hRa = coord('fHostRa', true, '宿主 RA');
    if (hRa === undefined) return false;
    const hDec = coord('fHostDec', false, '宿主 Dec');
    if (hDec === undefined) return false;

    const data = {
      id,
      trigger_instrument: getId('fTrigger'),
      ra: raV,
      dec: decV,
      t0: getId('fT0') || null,
      redshift: parseFloatOrNull(getId('fZ')),
      redshift_type: getId('fZType') || null,
      redshift_ref: getId('fZRef'),
      pos_error: parseFloatOrNull(getId('fPosErr')),
      pos_error_unit: getId('fPosErrUnit') || 'arcsec',
      pos_ref: getId('fPosRef'),
      tags: (getId('fTags') || '').split(',').map(s => s.trim()).filter(Boolean),
      aliases: (getId('fAliases') || '').split(',').map(s => s.trim()).filter(Boolean),
    };

    try {
      await createTransient(data);
      showToast(`事件 ${id} 创建成功！`, 'success');
      // 宿主星系（任一填写才写入；失败不影响源创建，仅警告）
      const hostData = {};
      const hZ = parseFloatOrNull(getId('fHostZ'));
      const hZType = getId('fHostZType');
      if (hRa != null) hostData.ra = hRa;
      if (hDec != null) hostData.dec = hDec;
      if (hZ != null) hostData.redshift = hZ;
      if (hZType) hostData.redshift_type = hZType;
      if (Object.keys(hostData).length) {
        try {
          await saveHost(id, hostData);
        } catch (e) {
          showToast(`宿主信息写入失败（事件已创建，可稍后补录）: ${e.message}`, 'warning');
        }
      }
      location.hash = `#/transient/${id}`;
    } catch (err) {
      showToast(`创建失败: ${err.message}`, 'danger');
    }
    return false;
  };
}

function parseFloatOrNull(val) {
  if (val === null || val === '') return null;
  const n = parseFloat(val);
  return isNaN(n) ? null : n;
}
