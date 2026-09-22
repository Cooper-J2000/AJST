"""
波长 空气↔真空 转换（Å）。

正向（真空→空气）采用 Morton (1991, ApJS 77, 119) 折射率公式
（SDSS 光谱管线所用），σ = 1e4/λ_vac(Å) 为真空波数（µm⁻¹）：
    n(σ)   = 1 + 8.34254e-5 + 2.406147e-2/(130 − σ²) + 1.5997e-4/(38.9 − σ²)
    λ_air  = λ_vac / n(σ)

反向（空气→真空）无解析反演，用不动点迭代：
    初值 λ_vac = λ_air，迭代 λ_vac ← λ_air · n(σ(λ_vac)) 至收敛。
n ≈ 1.000276，3–5 次迭代即收敛到 1e-9 Å 量级，精度远优于 0.001 Å。

只对 ≥ 2000 Å 的光学/红外波长做转换：Morton 1991 公式在紫外以下精度
没有保证，且远紫外观测通常不区分空气/真空波长，范围外的波长原样保留。

转换口径（全项目统一）：光谱的读取与处理路径（API 返回、P92 消光改正）
一律先把波长列转成真空波长再使用；库存文件与下载接口保持原始波长不动。
wavelength_type 为 'vacuum' 时不转换；为 'air' 或 NULL（留空）时按空气
波长处理做转换（NULL 不自动回填为 'air'）。单位非 Å 时不转换（防御，
当前库内光谱全部为 Å）。
"""

# 有效转换窗口下限（Å）：低于此值的紫外波长不做空气/真空转换
CONVERT_MIN_A = 2000.0


def _is_angstrom(u_wavelengths):
    """波长单位是否按 Å 处理：缺省/缺失视为 Å（库内约定），明确非 Å 才不转换"""
    if u_wavelengths in (None, ''):
        return True
    return 'angstrom' in str(u_wavelengths).lower()


def _refraction_n(sigma):
    """Morton (1991) 空气折射率 n(σ)，σ 为真空波数（µm⁻¹）"""
    s2 = sigma * sigma
    return (1.0 + 8.34254e-5
            + 2.406147e-2 / (130.0 - s2)
            + 1.5997e-4 / (38.9 - s2))


def vacuum_to_air(wl_vac):
    """真空波长（Å）→ 空气波长（Å）；< 2000 Å 原样返回"""
    wl = float(wl_vac)
    if wl < CONVERT_MIN_A:
        return wl
    return wl / _refraction_n(1e4 / wl)


def air_to_vacuum(wl_air):
    """空气波长（Å）→ 真空波长（Å）；< 2000 Å 原样返回。

    迭代反解：λ_vac ← λ_air · n(1e4/λ_vac)，初值 λ_vac = λ_air。
    """
    wl = float(wl_air)
    if wl < CONVERT_MIN_A:
        return wl
    wl_vac = wl
    for _ in range(8):
        new = wl * _refraction_n(1e4 / wl_vac)
        if abs(new - wl_vac) < 1e-9:
            wl_vac = new
            break
        wl_vac = new
    return wl_vac


def to_vacuum_wavelengths(wavs, wavelength_type, u_wavelengths=None):
    """波长列表按 wavelength_type 转真空。

    wavelength_type == 'vacuum'、或单位非 Å 时不转换（返回原列表）。
    返回 (list, converted)；converted=False 时返回的是输入列表本身。
    """
    if wavelength_type == 'vacuum' or not _is_angstrom(u_wavelengths):
        return wavs, False
    return [air_to_vacuum(w) for w in wavs], True


def to_vacuum_points(points, wavelength_type, u_wavelengths=None):
    """光谱 data 行 [wl, flux, (err)] 的波长列按 wavelength_type 转真空
    （新列表，不改输入）。返回 (rows, converted)。"""
    if wavelength_type == 'vacuum' or not _is_angstrom(u_wavelengths):
        return points, False
    return [[air_to_vacuum(p[0])] + list(p[1:]) for p in points], True
