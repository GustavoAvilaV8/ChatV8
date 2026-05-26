"""
Banco de dados local (SQLite) para armazenar o histórico de conversas.

Cada conversa é identificada por um conversa_id (ticket_id ou número do cliente).
O histórico completo é enviado à Claude a cada nova mensagem para manter o contexto.
"""

import sqlite3
import logging
from datetime import datetime
from contextlib import contextmanager

log = logging.getLogger(__name__)


class ConversationDB:
    def __init__(self, caminho: str = "conversas.db"):
        self.caminho = caminho
        self._criar_tabelas()

    # ------------------------------------------------------------------
    # Gerenciamento de conexão
    # ------------------------------------------------------------------

    @contextmanager
    def _conn(self):
        """Context manager que garante commit/rollback e fechamento da conexão."""
        conn = sqlite3.connect(self.caminho)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Criação das tabelas
    # ------------------------------------------------------------------

    def _criar_tabelas(self):
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS mensagens (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversa_id  TEXT    NOT NULL,
                    numero       TEXT    NOT NULL,
                    papel        TEXT    NOT NULL CHECK(papel IN ('user', 'assistant')),
                    conteudo     TEXT    NOT NULL,
                    criado_em    TEXT    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_conversa_id
                    ON mensagens (conversa_id, criado_em);

                CREATE TABLE IF NOT EXISTS conversas (
                    conversa_id     TEXT PRIMARY KEY,
                    numero          TEXT NOT NULL,
                    total_mensagens INTEGER DEFAULT 0,
                    ultima_atividade TEXT,
                    criado_em        TEXT NOT NULL
                );
            """)
        log.info("Banco de dados pronto: %s", self.caminho)

    # ------------------------------------------------------------------
    # Operações principais
    # ------------------------------------------------------------------

    def salvar_mensagem(
        self,
        conversa_id: str,
        papel: str,          # "user" ou "assistant"
        conteudo: str,
        numero: str,
    ) -> int:
        """
        Salva uma mensagem e atualiza o registro da conversa.
        Retorna o ID da mensagem inserida.
        """
        agora = datetime.utcnow().isoformat()

        with self._conn() as conn:
            # Insere a mensagem
            cursor = conn.execute(
                """
                INSERT INTO mensagens (conversa_id, numero, papel, conteudo, criado_em)
                VALUES (?, ?, ?, ?, ?)
                """,
                (conversa_id, numero, papel, conteudo, agora),
            )
            msg_id = cursor.lastrowid

            # Cria ou atualiza o registro da conversa
            conn.execute(
                """
                INSERT INTO conversas (conversa_id, numero, total_mensagens, ultima_atividade, criado_em)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(conversa_id) DO UPDATE SET
                    total_mensagens  = total_mensagens + 1,
                    ultima_atividade = excluded.ultima_atividade
                """,
                (conversa_id, numero, agora, agora),
            )

        log.debug("Mensagem salva — conversa=%s papel=%s id=%s", conversa_id, papel, msg_id)
        return msg_id

    def buscar_historico(self, conversa_id: str, limite: int = 20) -> list[dict]:
        """
        Retorna as últimas `limite` mensagens da conversa em ordem cronológica.
        Formato: [{"papel": "user"|"assistant", "conteudo": "..."}]
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT papel, conteudo, criado_em
                FROM (
                    SELECT papel, conteudo, criado_em
                    FROM mensagens
                    WHERE conversa_id = ?
                    ORDER BY criado_em DESC
                    LIMIT ?
                )
                ORDER BY criado_em ASC
                """,
                (conversa_id, limite),
            ).fetchall()

        return [dict(row) for row in rows]

    def buscar_conversas_ativas(self, horas: int = 24) -> list[dict]:
        """
        Retorna conversas com atividade nas últimas `horas` horas.
        Útil para monitoramento ou relatórios.
        """
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT conversa_id, numero, total_mensagens, ultima_atividade
                FROM conversas
                WHERE ultima_atividade >= datetime('now', ? || ' hours')
                ORDER BY ultima_atividade DESC
                """,
                (f"-{horas}",),
            ).fetchall()

        return [dict(row) for row in rows]

    def total_mensagens(self, conversa_id: str) -> int:
        """Retorna o total de mensagens de uma conversa."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as total FROM mensagens WHERE conversa_id = ?",
                (conversa_id,),
            ).fetchone()
        return row["total"] if row else 0

    def limpar_conversa(self, conversa_id: str):
        """Remove todas as mensagens de uma conversa (uso administrativo)."""
        with self._conn() as conn:
            conn.execute("DELETE FROM mensagens WHERE conversa_id = ?", (conversa_id,))
            conn.execute("DELETE FROM conversas WHERE conversa_id = ?", (conversa_id,))
        log.info("Conversa %s limpa do banco", conversa_id)
