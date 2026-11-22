"""Cuentas: usuarios, claves del CLI, autorizaciones, auditorías y mediciones.

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

ID = sa.BigInteger().with_variant(sa.Integer, "sqlite")
DOC = sa.JSON().with_variant(JSONB, "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "usuarios",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("nombre", sa.String(200), nullable=False),
        sa.Column("creado_en", TS, nullable=False),
        sa.Column("visto_en", TS, nullable=False),
    )
    op.create_table(
        "claves",
        sa.Column("id", ID, primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.String(64),
                  sa.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False, unique=True),
        sa.Column("creada_en", TS, nullable=False),
        sa.Column("usada_en", TS, nullable=True),
        sa.Column("revocada_en", TS, nullable=True),
    )
    op.create_index("ix_claves_usuario_id", "claves", ["usuario_id"])
    op.create_table(
        "autorizaciones_cli",
        sa.Column("desafio", sa.String(64), primary_key=True),
        sa.Column("usuario_id", sa.String(64),
                  sa.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("expira_en", TS, nullable=False),
    )
    op.create_table(
        "auditorias",
        sa.Column("id", ID, primary_key=True, autoincrement=True),
        sa.Column("usuario_id", sa.String(64),
                  sa.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("modelo", sa.String(200), nullable=False),
        sa.Column("referencia", sa.String(200), nullable=False),
        sa.Column("juego", sa.String(20), nullable=False),
        sa.Column("semilla", sa.BigInteger, nullable=True),
        sa.Column("origen", sa.String(10), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("evidencia_sha256", sa.String(64), nullable=False),
        sa.Column("gasto_usd", sa.Float, nullable=False),
        sa.Column("segundos", sa.Integer, nullable=True),
        sa.Column("medida_en", TS, nullable=False),
        sa.Column("creada_en", TS, nullable=False),
        sa.Column("ficha", DOC, nullable=False),
        sa.UniqueConstraint("usuario_id", "evidencia_sha256",
                            name="uq_auditorias_usuario_evidencia"),
    )
    op.create_index("ix_auditorias_usuario_id", "auditorias", ["usuario_id"])
    op.create_table(
        "mediciones",
        sa.Column("id", ID, primary_key=True, autoincrement=True),
        sa.Column("auditoria_id", ID,
                  sa.ForeignKey("auditorias.id", ondelete="CASCADE"), nullable=False),
        sa.Column("usuario_id", sa.String(64),
                  sa.ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False),
        sa.Column("modelo", sa.String(200), nullable=False),
        sa.Column("endpoint", sa.String(200), nullable=False),
        sa.Column("proveedor", sa.String(120), nullable=False),
        sa.Column("via", sa.String(20), nullable=False),
        sa.Column("cuantizacion", sa.String(40), nullable=True),
        sa.Column("precio_salida", sa.Float, nullable=True),
        sa.Column("rol", sa.String(12), nullable=False),
        sa.Column("distinguible", sa.Boolean, nullable=False),
        sa.Column("pruebas", sa.Integer, nullable=False),
        sa.Column("errores", sa.Integer, nullable=False),
        sa.Column("latencia_p50_ms", sa.Integer, nullable=True),
        sa.Column("costo_usd", sa.Float, nullable=False),
        sa.Column("tareas_ok", sa.Integer, nullable=False),
        sa.Column("tareas_n", sa.Integer, nullable=False),
        sa.Column("iguales", sa.Integer, nullable=False),
        sa.Column("iguales_n", sa.Integer, nullable=False),
        sa.Column("libre_iguales", sa.Integer, nullable=False),
        sa.Column("libre_n", sa.Integer, nullable=False),
        sa.Column("tools_ok", sa.Integer, nullable=False),
        sa.Column("tools_n", sa.Integer, nullable=False),
        sa.Column("canarios_filtrados", sa.Integer, nullable=False),
        sa.Column("canarios_n", sa.Integer, nullable=False),
        sa.Column("tokens_ocultos", sa.Integer, nullable=True),
        sa.Column("motivos", DOC, nullable=False),
    )
    op.create_index("ix_mediciones_auditoria_id", "mediciones", ["auditoria_id"])
    op.create_index("ix_mediciones_usuario_id", "mediciones", ["usuario_id"])


def downgrade() -> None:
    for table in ("mediciones", "auditorias", "autorizaciones_cli", "claves", "usuarios"):
        op.drop_table(table)
