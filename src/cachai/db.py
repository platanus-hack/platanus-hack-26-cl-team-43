"""Las tablas de las cuentas: quién audita, desde qué equipo y qué midió.

El identificador de un usuario es el `sub` de Clerk. Cada auditoría subida guarda la ficha
completa y una fila por endpoint (mediciones), que es lo que suma el panel.
"""
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String,
                        UniqueConstraint, create_engine)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

ID = BigInteger().with_variant(Integer, "sqlite")  # SQLite solo autoincrementa INTEGER
DOC = JSON().with_variant(JSONB, "postgresql")
TS = DateTime(timezone=True)


def now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Usuario(Base):
    __tablename__ = "usuarios"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # el `sub` de Clerk
    # correo y nombre los manda la página para mostrarlos; la identidad es el id
    email: Mapped[str] = mapped_column(String(320), default="")
    nombre: Mapped[str] = mapped_column(String(200), default="")
    creado_en: Mapped[datetime] = mapped_column(TS, default=now)
    visto_en: Mapped[datetime] = mapped_column(TS, default=now)


class Clave(Base):
    """Una clave del CLI. Se guarda solo su SHA-256: la clave se muestra una vez."""
    __tablename__ = "claves"
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    usuario_id: Mapped[str] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"),
                                            index=True)
    nombre: Mapped[str] = mapped_column(String(120), default="")
    hash: Mapped[str] = mapped_column(String(64), unique=True)
    creada_en: Mapped[datetime] = mapped_column(TS, default=now)
    usada_en: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    revocada_en: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class AutorizacionCli(Base):
    """Un `cachai login` aprobado en el navegador y todavía sin canjear por el CLI."""
    __tablename__ = "autorizaciones_cli"
    desafio: Mapped[str] = mapped_column(String(64), primary_key=True)  # SHA-256 del verificador
    usuario_id: Mapped[str] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"))
    nombre: Mapped[str] = mapped_column(String(120), default="")
    expira_en: Mapped[datetime] = mapped_column(TS)


class Auditoria(Base):
    __tablename__ = "auditorias"
    __table_args__ = (UniqueConstraint("usuario_id", "evidencia_sha256",
                                       name="uq_auditorias_usuario_evidencia"),)
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    usuario_id: Mapped[str] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"),
                                            index=True)
    modelo: Mapped[str] = mapped_column(String(200))
    referencia: Mapped[str] = mapped_column(String(200))
    juego: Mapped[str] = mapped_column(String(20))
    semilla: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    origen: Mapped[str] = mapped_column(String(10))  # cli o web
    version: Mapped[str] = mapped_column(String(40), default="")
    evidencia_sha256: Mapped[str] = mapped_column(String(64))
    gasto_usd: Mapped[float] = mapped_column(Float, default=0.0)
    segundos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    medida_en: Mapped[datetime] = mapped_column(TS)
    creada_en: Mapped[datetime] = mapped_column(TS, default=now)
    ficha: Mapped[dict] = mapped_column(DOC)


class Medicion(Base):
    """Lo medido en un endpoint dentro de una auditoría."""
    __tablename__ = "mediciones"
    id: Mapped[int] = mapped_column(ID, primary_key=True, autoincrement=True)
    auditoria_id: Mapped[int] = mapped_column(ForeignKey("auditorias.id", ondelete="CASCADE"),
                                              index=True)
    usuario_id: Mapped[str] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"),
                                            index=True)
    modelo: Mapped[str] = mapped_column(String(200))
    endpoint: Mapped[str] = mapped_column(String(200))
    proveedor: Mapped[str] = mapped_column(String(120))
    via: Mapped[str] = mapped_column(String(20), default="")
    cuantizacion: Mapped[str | None] = mapped_column(String(40), nullable=True)
    precio_salida: Mapped[float | None] = mapped_column(Float, nullable=True)
    rol: Mapped[str] = mapped_column(String(12))  # referencia, piso o auditado
    distinguible: Mapped[bool] = mapped_column(Boolean, default=False)
    pruebas: Mapped[int] = mapped_column(Integer, default=0)
    errores: Mapped[int] = mapped_column(Integer, default=0)
    latencia_p50_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    costo_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tareas_ok: Mapped[int] = mapped_column(Integer, default=0)
    tareas_n: Mapped[int] = mapped_column(Integer, default=0)
    iguales: Mapped[int] = mapped_column(Integer, default=0)
    iguales_n: Mapped[int] = mapped_column(Integer, default=0)
    libre_iguales: Mapped[int] = mapped_column(Integer, default=0)
    libre_n: Mapped[int] = mapped_column(Integer, default=0)
    tools_ok: Mapped[int] = mapped_column(Integer, default=0)
    tools_n: Mapped[int] = mapped_column(Integer, default=0)
    canarios_filtrados: Mapped[int] = mapped_column(Integer, default=0)
    canarios_n: Mapped[int] = mapped_column(Integer, default=0)
    tokens_ocultos: Mapped[int | None] = mapped_column(Integer, nullable=True)
    motivos: Mapped[list] = mapped_column(DOC, default=list)


def normalize(url: str) -> str:
    """Railway entrega postgres:// o postgresql://; el driver instalado es psycopg 3."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def connect(url: str):
    """El motor y la fábrica de sesiones."""
    url = normalize(url)
    args = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    engine = create_engine(url, pool_pre_ping=True, **args)
    return engine, sessionmaker(engine, expire_on_commit=False)


def upgrade(url: str) -> None:
    """Lleva la base a la última migración (Alembic, en src/cachai/migrations)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", normalize(url).replace("%", "%%"))
    command.upgrade(cfg, "head")
