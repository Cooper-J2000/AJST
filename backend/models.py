"""
可扩展的数据库模型
所有业务表均有 metadata JSONB 字段，供未来扩展新属性而不改 schema。
预留 spectra/images/fitting_results/extinction_corrections 表。

时间约定：全项目所有时间字段一律为 naive UTC，不做任何时区转换。
"""
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Float, Boolean, Text, BigInteger,
    ForeignKey, DateTime, UniqueConstraint, Index
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


def utcnow():
    """naive UTC 当前时间（数据库列一律存 naive UTC）"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def distance_modulus(redshift):
    """由红移计算距离模数 μ (mag)，Planck18 宇宙学；无红移时返回 None。"""
    if redshift is None or redshift <= 0:
        return None
    from astropy.cosmology import Planck18
    return round(float(Planck18.distmod(redshift).value), 3)


class Base(DeclarativeBase):
    pass


# ---------- 用户账户 ----------
class User(Base):
    __tablename__ = 'users'

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    username      = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    role          = Column(String(16), nullable=False, default='user')  # admin / user
    created_at    = Column(DateTime, default=lambda: utcnow())

    def set_password(self, password):
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'role': self.role,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ---------- 暂现源 ----------
class Transient(Base):
    __tablename__ = 'transients'

    id              = Column(String(32), primary_key=True)  # e.g. EP251202a
    ra              = Column(Float, nullable=True)
    dec             = Column(Float, nullable=True)
    t0              = Column(DateTime, nullable=True)
    trigger_instrument = Column(String(64), nullable=True)
    redshift        = Column(Float, nullable=True)
    redshift_type   = Column(String(16), nullable=True)   # value / phot_z / upperlimit
    redshift_ref    = Column(Text, nullable=True)
    pos_error       = Column(Float, nullable=True)
    pos_error_unit  = Column(String(16), default='arcsec')
    pos_ref         = Column(Text, nullable=True)
    comment         = Column(Text, nullable=True)
    sub_tag         = Column(JSONB, default=list)          # ["L", "S", "X"]
    tags            = Column(JSONB, default=list)          # ["fxt"]
    aliases         = Column(JSONB, default=list)          # ["GRB251202A"]
    extra_data      = Column(JSONB, default=dict)          # 任意未来扩展字段

    created_at      = Column(DateTime, default=lambda: utcnow())
    updated_at      = Column(DateTime, default=lambda: utcnow(),
                             onupdate=lambda: utcnow())

    # 关系
    lightcurves    = relationship('Lightcurve', back_populates='transient',
                                  cascade='all, delete-orphan', lazy='dynamic')
    spectra        = relationship('Spectrum', back_populates='transient',
                                  cascade='all, delete-orphan', lazy='dynamic')
    images         = relationship('Image', back_populates='transient',
                                  cascade='all, delete-orphan', lazy='dynamic')
    fitting_results = relationship('FittingResult', back_populates='transient',
                                   cascade='all, delete-orphan', lazy='dynamic')
    extinction_corrections = relationship('ExtinctionCorrection', back_populates='transient',
                                          cascade='all, delete-orphan', lazy='dynamic')
    tags_rel       = relationship('TransientTag', back_populates='transient',
                                  cascade='all, delete-orphan')
    articles       = relationship('Article', back_populates='transient',
                                  cascade='all, delete-orphan', lazy='dynamic')

    def to_dict(self, include_relations=False):
        d = {
            'id': self.id,
            'ra': self.ra,
            'dec': self.dec,
            't0': self.t0.isoformat() if self.t0 else None,
            'trigger_instrument': self.trigger_instrument,
            'redshift': self.redshift,
            'redshift_type': self.redshift_type,
            'redshift_ref': self.redshift_ref,
            'pos_error': self.pos_error,
            'pos_error_unit': self.pos_error_unit,
            'pos_ref': self.pos_ref,
            'comment': self.comment,
            'sub_tag': self.sub_tag or [],
            'tags': self.tags or [],
            'aliases': self.aliases or [],
            'extra_data': self.extra_data or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'distmod': distance_modulus(self.redshift),
        }
        if include_relations:
            d['lc_count'] = self.lightcurves.count() if hasattr(self, 'lightcurves') else None
            d['spectra_count'] = self.spectra.count() if hasattr(self, 'spectra') else 0
        return d


# ---------- 光变数据 ----------
class Lightcurve(Base):
    __tablename__ = 'lightcurves'

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    transient_id  = Column(String(32), ForeignKey('transients.id', ondelete='CASCADE'),
                           nullable=False, index=True)

    time          = Column(Float, nullable=False)
    time_err      = Column(Float, nullable=True)
    time_unit     = Column(String(8), default='s')
    band          = Column(String(32), nullable=False)
    flux_density  = Column(Float, nullable=False)
    flux_density_err = Column(Float, nullable=True)
    flux_density_unit = Column(String(32), nullable=False)
    mag_system    = Column(String(8), nullable=True)
    gext_corr     = Column(Boolean, default=False)
    upperlimit    = Column(Boolean, default=False)
    host_subtracted = Column(Boolean, default=False)  # 是否已扣除宿主星系；NULL 表示未知
    gext_Alambda  = Column(Float, nullable=True)
    mag_gextcor            = Column(Float, nullable=True)   # 银消改正后 AB 星等
    mag_gextcor_err        = Column(Float, nullable=True)   # 银消改正后 AB 星等误差
    flux_density_gextcor     = Column(Float, nullable=True)
    flux_density_gextcor_err = Column(Float, nullable=True)
    flux_density_gextcor_unit = Column(String(32), nullable=True)
    weights       = Column(Float, default=1.0)
    discard       = Column(Boolean, default=False)
    telescope     = Column(String(128), nullable=True)
    instrument    = Column(String(128), nullable=True)
    reference     = Column(Text, nullable=True)
    comment       = Column(Text, nullable=True)
    source        = Column(String(128), nullable=True)  # 数据来源（提交账户名或导入渠道）
    extra_data      = Column(JSONB, default=dict)

    created_at    = Column(DateTime, default=lambda: utcnow())
    updated_at    = Column(DateTime, default=lambda: utcnow(),
                           onupdate=lambda: utcnow())

    transient     = relationship('Transient', back_populates='lightcurves')

    __table_args__ = (
        Index('idx_lc_transient_band', 'transient_id', 'band'),
        Index('idx_lc_transient_time', 'transient_id', 'time'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'transient_id': self.transient_id,
            'time': self.time,
            'time_err': self.time_err,
            'time_unit': self.time_unit,
            'band': self.band,
            'flux_density': self.flux_density,
            'flux_density_err': self.flux_density_err,
            'flux_density_unit': self.flux_density_unit,
            'mag_system': self.mag_system,
            'gext_corr': self.gext_corr,
            'upperlimit': self.upperlimit,
            'host_subtracted': self.host_subtracted,
            'gext_Alambda': self.gext_Alambda,
            'mag_gextcor': self.mag_gextcor,
            'mag_gextcor_err': self.mag_gextcor_err,
            'flux_density_gextcor': self.flux_density_gextcor,
            'flux_density_gextcor_err': self.flux_density_gextcor_err,
            'flux_density_gextcor_unit': self.flux_density_gextcor_unit,
            'weights': self.weights,
            'discard': self.discard,
            'telescope': self.telescope,
            'instrument': self.instrument,
            'reference': self.reference,
            'comment': self.comment,
            'source': self.source,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'extra_data': self.extra_data or {},
        }


# ---------- 相关研究文章 ----------
class Article(Base):
    __tablename__ = 'articles'

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    transient_id  = Column(String(32), ForeignKey('transients.id', ondelete='CASCADE'),
                           nullable=False, index=True)
    name          = Column(String(256), nullable=False)  # 简称，建议「第一作者+年份」
    url           = Column(Text, nullable=False)         # 文章网页链接
    title         = Column(Text, nullable=True)          # 文章标题（可含引号/冒号/空格）
    bibtex        = Column(Text, nullable=True)          # BibTeX 引用信息（前端不整段展示，仅复制）
    source        = Column(String(128), nullable=True)   # 录入账户（自动记录）
    created_at    = Column(DateTime, default=lambda: utcnow())
    updated_at    = Column(DateTime, default=lambda: utcnow(),
                           onupdate=lambda: utcnow())

    transient     = relationship('Transient', back_populates='articles')

    def to_dict(self):
        return {
            'id': self.id,
            'transient_id': self.transient_id,
            'name': self.name,
            'url': self.url,
            'title': self.title,
            'bibtex': self.bibtex,
            'source': self.source,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }


# ---------- 滤波器定义 ----------
class FilterDef(Base):
    __tablename__ = 'filters'

    id          = Column(String(32), primary_key=True)   # e.g. 'r', 'J', 'uvot-u'
    wavelength  = Column(Float, nullable=False)
    filter_type = Column(String(16), nullable=True)      # 'mean' / 'ref'
    vega2ab     = Column(Float, default=0.0)
    description = Column(Text, nullable=True)
    extra_data    = Column(JSONB, default=dict)

    def to_dict(self):
        return {
            'id': self.id,
            'wavelength': self.wavelength,
            'filter_type': self.filter_type,
            'vega2ab': self.vega2ab,
            'description': self.description,
            'extra_data': self.extra_data or {},
        }


# ---------- 标签（多对多） ----------
class Tag(Base):
    __tablename__ = 'tags'

    id          = Column(BigInteger, primary_key=True, autoincrement=True)
    name        = Column(String(64), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    color       = Column(String(16), nullable=True)     # 前端显示颜色
    created_at  = Column(DateTime, default=lambda: utcnow())

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'color': self.color,
        }


class TransientTag(Base):
    __tablename__ = 'transient_tags'

    transient_id = Column(String(32), ForeignKey('transients.id', ondelete='CASCADE'),
                          primary_key=True)
    tag_id       = Column(BigInteger, ForeignKey('tags.id', ondelete='CASCADE'),
                          primary_key=True)

    transient    = relationship('Transient', back_populates='tags_rel')
    tag          = relationship('Tag')


# ===================== 预留 / 未来扩展表 =====================

# ---------- 能谱 ----------
class Spectrum(Base):
    __tablename__ = 'spectra'

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    transient_id   = Column(String(32), ForeignKey('transients.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    filename       = Column(String(256), nullable=False)
    wavelength_min = Column(Float, nullable=True)
    wavelength_max = Column(Float, nullable=True)
    instrument     = Column(String(128), nullable=True)
    observation_date = Column(DateTime, nullable=True)
    file_path      = Column(Text, nullable=False)
    file_type      = Column(String(16), default='fits')  # fits / txt / csv / ecsv
    extra_data       = Column(JSONB, default=dict)
    created_at     = Column(DateTime, default=lambda: utcnow())

    transient      = relationship('Transient', back_populates='spectra')

    def to_dict(self):
        return {
            'id': self.id,
            'transient_id': self.transient_id,
            'filename': self.filename,
            'wavelength_min': self.wavelength_min,
            'wavelength_max': self.wavelength_max,
            'instrument': self.instrument,
            'observation_date': self.observation_date.isoformat() if self.observation_date else None,
            'file_path': self.file_path,
            'file_type': self.file_type,
            'extra_data': self.extra_data or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ---------- 图像 ----------
class Image(Base):
    __tablename__ = 'images'

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    transient_id   = Column(String(32), ForeignKey('transients.id', ondelete='CASCADE'),
                            nullable=False, index=True)
    filename       = Column(String(256), nullable=False)
    band           = Column(String(32), nullable=True)
    file_path      = Column(Text, nullable=False)
    file_type      = Column(String(16), default='fits')  # fits / png / jpg
    observation_date = Column(DateTime, nullable=True)
    extra_data       = Column(JSONB, default=dict)
    created_at     = Column(DateTime, default=lambda: utcnow())

    transient      = relationship('Transient', back_populates='images')

    def to_dict(self):
        return {
            'id': self.id,
            'transient_id': self.transient_id,
            'filename': self.filename,
            'band': self.band,
            'file_path': self.file_path,
            'file_type': self.file_type,
            'observation_date': self.observation_date.isoformat() if self.observation_date else None,
            'extra_data': self.extra_data or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ---------- 拟合结果 ----------
class FittingResult(Base):
    __tablename__ = 'fitting_results'

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    transient_id = Column(String(32), ForeignKey('transients.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    model_name   = Column(String(128), nullable=False)  # e.g. 'powerlaw', 'broken_powerlaw'
    parameters   = Column(JSONB, default=dict)          # 拟合参数
    chi_squared  = Column(Float, nullable=True)
    extra_data     = Column(JSONB, default=dict)
    created_at   = Column(DateTime, default=lambda: utcnow())

    transient    = relationship('Transient', back_populates='fitting_results')

    def to_dict(self):
        return {
            'id': self.id,
            'transient_id': self.transient_id,
            'model_name': self.model_name,
            'parameters': self.parameters or {},
            'chi_squared': self.chi_squared,
            'extra_data': self.extra_data or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


# ---------- 银河系消光改正 ----------
class ExtinctionCorrection(Base):
    __tablename__ = 'extinction_corrections'

    id           = Column(BigInteger, primary_key=True, autoincrement=True)
    transient_id = Column(String(32), ForeignKey('transients.id', ondelete='CASCADE'),
                          nullable=False, index=True)
    band         = Column(String(32), nullable=False)
    a_lambda     = Column(Float, nullable=False)        # A_lambda
    e_bv         = Column(Float, nullable=True)          # E(B-V)
    method       = Column(String(64), nullable=True)     # e.g. 'SF11', 'Planck'
    extra_data     = Column(JSONB, default=dict)
    created_at   = Column(DateTime, default=lambda: utcnow())

    transient    = relationship('Transient', back_populates='extinction_corrections')

    def to_dict(self):
        return {
            'id': self.id,
            'transient_id': self.transient_id,
            'band': self.band,
            'a_lambda': self.a_lambda,
            'e_bv': self.e_bv,
            'method': self.method,
            'extra_data': self.extra_data or {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
