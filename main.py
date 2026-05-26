"""
Servidor de integração: Vectax + Claude IA — Cobrança Crédito CLT
=================================================================
Recebe webhooks da Vectax, identifica o cliente pelo número de WhatsApp
ou CPF, consulta parcelas em aberto e responde via Claude.

Requisitos:
    pip install fastapi uvicorn anthropic httpx python-dotenv python-dateutil

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

app = FastAPI(title="Cobrança CLT — Claude IA", version="3.0.0")

claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
chatbot_client = httpx.AsyncClient(timeout=30)

VECTAX_API_URL = os.getenv("VECTAX_API_URL", "https://enterprise-369api.v8sistema.com")
VECTAX_API_ID  = os.getenv("VECTAX_API_ID")
VECTAX_TOKEN   = os.getenv("VECTAX_TOKEN")

BASE_CSV_PATH  = os.getenv("BASE_CSV_PATH", "base_contratos.csv")

db = ConversationDB("conversas.db")


# ---------------------------------------------------------------------------
# Prompt de sistema
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Você é um atendente da V8 Digital, especializado em consignado privado.

IDENTIDADE:
- Você representa a V8 Digital
- Nunca diga que é um robô, bot ou assistente virtual — aja como um atendente humano
- Se perguntarem se é humano ou robô, diga apenas que é um atendente da V8 Digital
- Use linguagem natural, simpática e próxima — como um atendente real faria no WhatsApp

OBJETIVO:
Ajudar clientes a entenderem sua situação de parcelas e negociar débitos em aberto de forma cordial e profissional.

REGRAS IMPORTANTES:
- Seja sempre respeitoso, empático e profissional
- Nunca pressione ou ameace o cliente
- Apresente os valores em R$ no formato brasileiro (ex: R$ 1.250,00)
- Se o cliente tiver múltiplos contratos, liste-os e pergunte sobre qual ele quer tratar
- Se não conseguir identificar o cliente, peça o CPF educadamente (somente números)
- Nunca invente informações que não estão no contexto fornecido
- Respostas curtas e objetivas — estamos no WhatsApp
- Não use saudações longas nem se apresente como assistente virtual

FLUXO DE ATENDIMENTO:
1. Identifique o cliente (pelo número ou CPF)
2. Apresente a situação das parcelas de forma clara
3. Ofereça opções: negociação, boleto, esclarecimentos
4. Encaminhe para atendente humano se necessário

Quando tiver os dados do cliente no contexto, use-os para personalizar o atendimento pelo nome.
"""

# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@app.get("/")
async def health_check():
    return {"status": "online", "servico": "Cobrança CLT — Claude IA"}


@app.post("/webhook")
async def receber_webhook(request: Request):
    try:
        corpo = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    log.info(f"Webhook COMPLETO: {json.dumps(corpo, ensure_ascii=False)}")

    evento = corpo.get("event")
    if evento != "NewMessage":
        return JSONResponse({"ok": True, "ignorado": f"evento {evento}"})

    msg        = corpo.get("message", {})
    ticket_id  = str(msg.get("ticketId", ""))
    contact_id = str(msg.get("contactId", ""))
    ticket_obj  = msg.get("ticket", {})
    contact_obj = ticket_obj.get("contact", {})
    raw_obj     = msg.get("raw", {})
    numero_raw  = str(
        contact_obj.get("number")
        or raw_obj.get("from")
        or contact_id
    )
    body       = msg.get("body", "").strip()
    from_me    = msg.get("fromMe", False)
    send_type  = msg.get("sendType", "")
    is_note    = msg.get("note", False)

    if from_me or send_type == "bot" or is_note or not body:
        return JSONResponse({"ok": True, "ignorado": "filtrado"})

    if not ticket_id or not contact_id:
        return JSONResponse({"ok": True, "ignorado": "sem identificadores"})

    log.info(f"✉ ticket={ticket_id} numero={numero_raw} | {body[:80]}")

    await processar_mensagem(
        ticket_id=ticket_id,
        contact_id=contact_id,
        numero_whatsapp=numero_raw,
        mensagem=body,
    )

    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Lógica principal
# ---------------------------------------------------------------------------

async def processar_mensagem(
    ticket_id: str,
    contact_id: str,
    numero_whatsapp: str,
    mensagem: str,
):
    # 1. Salva mensagem do cliente
    db.salvar_mensagem(
        conversa_id=ticket_id,
        papel="user",
        conteudo=mensagem,
        numero=contact_id,
    )

    # 2. Busca histórico
    historico = db.buscar_historico(conversa_id=ticket_id, limite=20)

    # 3. Chama Claude
    try:
        resposta = await chamar_claude(historico)
    except Exception as e:
        log.error(f"Erro Claude: {e}")
        resposta = "Desculpe, ocorreu uma instabilidade. Um atendente entrará em contato em breve!"

    log.info(f"✅ Resposta ticket={ticket_id}: {resposta[:100]}")

    # 4. Salva e envia resposta
    db.salvar_mensagem(
        conversa_id=ticket_id,
        papel="assistant",
        conteudo=resposta,
        numero=contact_id,
    )

    # Envia usando o número de WhatsApp real
    log.info(f"📱 Enviando para numero_whatsapp={numero_whatsapp} contact_id={contact_id}")
    await enviar_mensagem_vectax(ticket_id, numero_whatsapp, resposta)



async def chamar_claude(historico: list[dict]) -> str:
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
    url = f"{VECTAX_API_URL}/v1/api/external/{VECTAX_API_ID}"

    payload = {
        "body": mensagem,
        "number": contact_id,
        "externalKey": ticket_id,
    }
    headers = {
        "Authorization": f"Bearer {VECTAX_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        resp = await chatbot_client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        log.info(f"📤 Enviado ticket={ticket_id} status={resp.status_code}")
    except httpx.HTTPStatusError as e:
        log.error(f"Erro Vectax {e.response.status_code}: {e.response.text[:200]}")
    except Exception as e:
        log.error(f"Falha envio: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extrair_cpf(texto: str) -> Optional[str]:
    """
    Tenta extrair um CPF de um texto.
    Aceita formatos: 012.345.678-90 ou 01234567890
    """
    import re
    # Com formatação
    m = re.search(r"\d{3}[\.\s]?\d{3}[\.\s]?\d{3}[-\s]?\d{2}", texto)
    if m:
        return re.sub(r"\D", "", m.group())
    return None
