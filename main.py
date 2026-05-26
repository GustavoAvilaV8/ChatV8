"""
Servidor de integração: Vectax + Claude IA
==========================================
Recebe webhooks da Vectax (evento NewMessage),
processa com a Claude e responde automaticamente.

Requisitos:
    pip install fastapi uvicorn anthropic httpx python-dotenv

Uso:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import json
import logging
from typing import Optional

import httpx
import anthropic
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from database import ConversationDB

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app = FastAPI(title="Vectax + Claude IA", version="2.0.0")

# Clientes externos
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
chatbot_client = httpx.AsyncClient(timeout=30)

# Configurações da Vectax
VECTAX_API_URL = os.getenv("VECTAX_API_URL", "https://enterprise-369api.v8sistema.com")
VECTAX_API_ID  = os.getenv("VECTAX_API_ID")   # ID da API (ex: 7f82649a-2083-489d-a0ad-b10586c57768)
VECTAX_TOKEN   = os.getenv("VECTAX_TOKEN")     # Token JWT da Vectax

# Números permitidos para teste — deixe vazio [] para atender todos
# Exemplo: NUMEROS_TESTE = ["5511999999999", "5521988888888"]
NUMEROS_TESTE: list[str] = os.getenv("NUMEROS_TESTE", "").split(",") if os.getenv("NUMEROS_TESTE") else []

# Banco de conversas (SQLite local)
db = ConversationDB("conversas.db")

# ---------------------------------------------------------------------------
# Prompt de sistema da IA — personalize com informações do seu negócio
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Você é um assistente virtual inteligente e cordial de atendimento ao cliente.

Suas responsabilidades:
- Responder dúvidas sobre produtos e serviços com clareza e objetividade
- Identificar oportunidades de venda e registrá-las quando adequado
- Escalar para um atendente humano quando a situação exigir
- Manter tom amigável, profissional e empático em todas as interações

Diretrizes importantes:
- Seja conciso: respostas curtas e diretas são preferidas no WhatsApp
- Nunca invente informações que não possui
- Se não souber algo, diga honestamente e ofereça alternativas
- Use linguagem natural, sem excessos de formalidade

Quando identificar interesse claro de compra, mencione que vai registrar a oportunidade.
"""

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def health_check():
    return {"status": "online", "servico": "Vectax + Claude IA"}


@app.post("/webhook")
async def receber_webhook(request: Request):
    """
    Recebe o evento NewMessage da Vectax.

    Payload esperado:
    {
      "event": "NewMessage",
      "tenantId": 1,
      "message": {
        "id": "...",
        "ticketId": 9902,
        "contactId": 17256,
        "body": "Texto da mensagem",
        "fromMe": false,
        "sendType": "chat",   // "bot" = mensagem automática, ignorar
        "note": false,
        "mediaType": "chat",
        "mediaUrl": null,
        ...
      }
    }
    """
    try:
        corpo = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    log.info(f"Webhook recebido: {json.dumps(corpo, ensure_ascii=False)[:300]}")

    # Valida que é o evento correto
    evento = corpo.get("event")
    if evento != "NewMessage":
        log.info(f"Evento '{evento}' ignorado — só processa NewMessage")
        return JSONResponse({"ok": True, "ignorado": f"evento {evento}"})

    msg = corpo.get("message", {})

    # Extrai campos do payload da Vectax
    ticket_id  = str(msg.get("ticketId", ""))
    contact_id = str(msg.get("contactId", ""))
    body       = msg.get("body", "").strip()
    from_me    = msg.get("fromMe", False)
    send_type  = msg.get("sendType", "")   # "chat", "bot", "api"
    is_note    = msg.get("note", False)
    media_type = msg.get("mediaType", "chat")

    # --- Filtros: ignora mensagens que não devem ser processadas ---

    # Ignora mensagens enviadas pelo próprio sistema (evita loop infinito)
    if from_me:
        log.info("Mensagem fromMe=true — ignorada para evitar loop")
        return JSONResponse({"ok": True, "ignorado": "fromMe"})

    # Ignora mensagens automáticas de bot
    if send_type == "bot":
        log.info("Mensagem sendType=bot — ignorada")
        return JSONResponse({"ok": True, "ignorado": "sendType bot"})

    # Ignora notas internas
    if is_note:
        log.info("Nota interna — ignorada")
        return JSONResponse({"ok": True, "ignorado": "note"})

    # Ignora mensagens sem texto (áudio, imagem, etc. sem legenda)
    if not body:
        log.info(f"Mensagem sem texto (mediaType={media_type}) — ignorada")
        return JSONResponse({"ok": True, "ignorado": "sem texto"})

    if not ticket_id or not contact_id:
        log.warning("Webhook sem ticketId ou contactId — ignorado")
        return JSONResponse({"ok": True, "ignorado": "sem identificadores"})

    # --- Filtro de teste: responde só para números específicos ---
    if NUMEROS_TESTE and contact_id not in NUMEROS_TESTE:
        log.info(f"Contato {contact_id} fora da lista de teste — ignorado")
        return JSONResponse({"ok": True, "ignorado": "fora da lista de teste"})

    log.info(f"✉ Nova mensagem | ticket={ticket_id} contato={contact_id} | {body[:80]}")

    # Processa e responde
    await processar_e_responder(
        ticket_id=ticket_id,
        contact_id=contact_id,
        mensagem=body,
    )

    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Lógica principal
# ---------------------------------------------------------------------------

async def processar_e_responder(ticket_id: str, contact_id: str, mensagem: str):
    """
    Fluxo completo:
    1. Salva mensagem do cliente
    2. Busca histórico da conversa (contexto para a IA)
    3. Chama a Claude com o histórico completo
    4. Salva resposta da IA
    5. Envia resposta ao cliente via API da Vectax
    6. (Opcional) Registra oportunidade se detectar interesse de compra
    """

    # 1. Salva mensagem do cliente
    db.salvar_mensagem(
        conversa_id=ticket_id,
        papel="user",
        conteudo=mensagem,
        numero=contact_id,
    )

    # 2. Busca histórico (últimas 20 mensagens = contexto da conversa)
    historico = db.buscar_historico(conversa_id=ticket_id, limite=20)

    # 3. Gera resposta com a Claude
    try:
        resposta_ia = await chamar_claude(historico)
    except Exception as e:
        log.error(f"Erro ao chamar Claude: {e}")
        resposta_ia = (
            "Desculpe, estou com uma instabilidade no momento. "
            "Um atendente vai entrar em contato em breve! 🙏"
        )

    log.info(f"✅ Resposta gerada para ticket {ticket_id}: {resposta_ia[:100]}")

    # 4. Salva resposta da IA no histórico
    db.salvar_mensagem(
        conversa_id=ticket_id,
        papel="assistant",
        conteudo=resposta_ia,
        numero=contact_id,
    )

    # 5. Envia resposta ao cliente pela Vectax
    await enviar_mensagem_vectax(
        ticket_id=ticket_id,
        contact_id=contact_id,
        mensagem=resposta_ia,
    )

    # 6. Registra oportunidade se detectar interesse de compra
    if detectar_intencao_compra(mensagem):
        log.info(f"💰 Intenção de compra detectada — criando oportunidade")
        await registrar_oportunidade(contact_id, mensagem, ticket_id)


async def chamar_claude(historico: list[dict]) -> str:
    """
    Envia o histórico completo da conversa para a Claude.
    Cada item do histórico tem 'papel' (user/assistant) e 'conteudo'.
    """
    mensagens = [
        {"role": msg["papel"], "content": msg["conteudo"]}
        for msg in historico
    ]

    response = claude_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=mensagens,
    )

    return response.content[0].text


async def enviar_mensagem_vectax(ticket_id: str, contact_id: str, mensagem: str):
    """
    Envia a resposta da IA para o cliente via endpoint da Vectax:
    POST /v1/api/external/{apiId}
    """
    url = f"{VECTAX_API_URL}/v1/api/external/{VECTAX_API_ID}"

    payload = {
        "body": mensagem,
        "number": contact_id,         # ID do contato na Vectax
        "externalKey": ticket_id,     # ID do ticket — vincula ao atendimento certo
    }

    headers = {
        "Authorization": f"Bearer {VECTAX_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        resp = await chatbot_client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        log.info(f"📤 Mensagem enviada | ticket={ticket_id} status={resp.status_code}")
    except httpx.HTTPStatusError as e:
        log.error(f"Erro Vectax {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        log.error(f"Falha ao enviar mensagem: {e}")


# ---------------------------------------------------------------------------
# Detecção de intenção de compra
# ---------------------------------------------------------------------------

PALAVRAS_COMPRA = [
    "quero comprar", "quero contratar", "quanto custa", "qual o preço",
    "tem disponível", "como faço para comprar", "quero adquirir",
    "interesse em", "me interessa", "gostaria de comprar", "quero fechar",
    "vou querer", "pode me passar o valor", "qual o investimento",
]

def detectar_intencao_compra(mensagem: str) -> bool:
    texto = mensagem.lower()
    return any(palavra in texto for palavra in PALAVRAS_COMPRA)


async def registrar_oportunidade(contact_id: str, mensagem: str, ticket_id: str):
    """
    Cria uma oportunidade no CRM da Vectax automaticamente.
    Configure os IDs padrão no .env conforme sua conta.
    """
    url = f"{VECTAX_API_URL}/v1/api/external/{VECTAX_API_ID}/opportunities"

    payload = {
        "name": f"Lead via chatbot — contato {contact_id}",
        "description": f"Interesse detectado automaticamente.\nMensagem: {mensagem[:300]}",
        "contactId": int(os.getenv("DEFAULT_CONTACT_ID", "1")),
        "responsibleId": os.getenv("DEFAULT_RESPONSIBLE_ID", "1"),
        "pipelineStepId": int(os.getenv("DEFAULT_PIPELINE_STEP_ID", "1")),
        "userId": os.getenv("DEFAULT_USER_ID", "1"),
    }

    headers = {
        "Authorization": f"Bearer {VECTAX_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        resp = await chatbot_client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        log.info(f"🎯 Oportunidade criada para contato {contact_id}")
    except Exception as e:
        log.error(f"Erro ao criar oportunidade: {e}")
