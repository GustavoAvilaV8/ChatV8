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
from consulta_contratos import (
    carregar_base,
    buscar_por_whatsapp,
    buscar_por_cpf,
    resumo_contrato,
)

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
# Carrega base de contratos na inicialização
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    carregar_base(BASE_CSV_PATH)


# ---------------------------------------------------------------------------
# Prompt de sistema
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Você é um assistente virtual de cobrança do Crédito CLT, especializado em consignado privado.

Seu objetivo é ajudar clientes a entenderem sua situação financeira e negociar parcelas em atraso de forma cordial e profissional.

REGRAS IMPORTANTES:
- Seja sempre respeitoso, empático e profissional
- Nunca pressione ou ameace o cliente
- Apresente os valores de forma clara (use R$ e formato brasileiro)
- Se o cliente tiver múltiplos contratos, liste-os e pergunte sobre qual ele quer tratar
- Se não conseguir identificar o cliente, peça o CPF educadamente
- Nunca invente informações que não estão no contexto fornecido
- Respostas curtas e objetivas — estamos no WhatsApp

FLUXO DE ATENDIMENTO:
1. Identifique o cliente (pelo número ou CPF)
2. Apresente a situação das parcelas
3. Ofereça opções: negociação, boleto, esclarecimentos
4. Encaminhe para atendente humano se necessário

Quando tiver os dados do cliente no contexto, use-os para personalizar o atendimento.
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
    numero_raw = str(msg.get("number") or contact_id)
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
    # 1. Tenta identificar o cliente pelo número de WhatsApp
    contexto_cliente = _montar_contexto_cliente(numero_whatsapp, mensagem)

    # 2. Salva mensagem do cliente
    db.salvar_mensagem(
        conversa_id=ticket_id,
        papel="user",
        conteudo=mensagem,
        numero=contact_id,
    )

    # 3. Busca histórico
    historico = db.buscar_historico(conversa_id=ticket_id, limite=20)

    # 4. Chama Claude com contexto do cliente
    try:
        resposta = await chamar_claude(historico, contexto_cliente)
    except Exception as e:
        log.error(f"Erro Claude: {e}")
        resposta = "Desculpe, ocorreu uma instabilidade. Um atendente entrará em contato em breve!"

    log.info(f"✅ Resposta ticket={ticket_id}: {resposta[:100]}")

    # 5. Salva e envia resposta
    db.salvar_mensagem(
        conversa_id=ticket_id,
        papel="assistant",
        conteudo=resposta,
        numero=contact_id,
    )

    await enviar_mensagem_vectax(ticket_id, contact_id, resposta)


def _montar_contexto_cliente(numero_whatsapp: str, mensagem: str) -> str:
    """
    Tenta identificar o cliente pelo WhatsApp.
    Se não encontrar, verifica se a mensagem contém um CPF.
    Retorna uma string de contexto para ser injetada no prompt.
    """

    # Tenta pelo número de WhatsApp
    contratos = buscar_por_whatsapp(numero_whatsapp)

    # Se não achou pelo WhatsApp, tenta extrair CPF da mensagem
    if not contratos:
        cpf = _extrair_cpf(mensagem)
        if cpf:
            contratos = buscar_por_cpf(cpf)

    if not contratos:
        return (
            "CONTEXTO DO CLIENTE:\n"
            "Número não encontrado na base de contratos.\n"
            "Solicite o CPF ao cliente para identificá-lo."
        )

    if len(contratos) == 1:
        resumo = resumo_contrato(contratos[0])
        return f"CONTEXTO DO CLIENTE:\n{json.dumps(resumo, ensure_ascii=False, indent=2)}"

    # Múltiplos contratos — passa lista resumida
    lista = []
    for c in contratos:
        r = resumo_contrato(c)
        lista.append({
            "contrato":         r["contrato"]["numero"],
            "valor_parcela":    r["contrato"]["valor_parcela"],
            "parcelas_atrasadas": r["situacao"]["parcelas_atrasadas"],
            "total_devido":     r["situacao"]["total_devido"],
        })

    return (
        f"CONTEXTO DO CLIENTE:\n"
        f"Nome: {contratos[0]['Nome']}\n"
        f"CPF: {contratos[0]['CPF']}\n"
        f"Este cliente possui {len(contratos)} contratos:\n"
        + json.dumps(lista, ensure_ascii=False, indent=2)
        + "\nPergunte ao cliente sobre qual contrato ele deseja tratar."
    )


async def chamar_claude(historico: list[dict], contexto_cliente: str) -> str:
    """
    Chama a Claude injetando o contexto do cliente no system prompt.
    """
    system = SYSTEM_PROMPT + "\n\n" + contexto_cliente

    mensagens = [
        {"role": msg["papel"], "content": msg["conteudo"]}
        for msg in historico
    ]

    response = claude_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=system,
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
