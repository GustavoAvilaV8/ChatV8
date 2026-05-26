"""
Servidor de integração: Vectax + Claude IA — Cobrança V8 Digital
=================================================================
Recebe webhooks da Vectax, consulta contratos no qualidadev8,
e responde via Claude com dados reais das parcelas.

Requisitos:
    pip install fastapi uvicorn anthropic httpx python-dotenv

Uso:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""

import os
import re
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

app = FastAPI(title="Cobrança V8 Digital — Claude IA", version="4.0.0")

claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
chatbot_client = httpx.AsyncClient(timeout=30)

# Vectax
VECTAX_API_URL = os.getenv("VECTAX_API_URL", "https://enterprise-369api.v8sistema.com")
VECTAX_API_ID  = os.getenv("VECTAX_API_ID")
VECTAX_TOKEN   = os.getenv("VECTAX_TOKEN")

# qualidadev8 — API interna de contratos
QUALIDADE_API_URL    = os.getenv("QUALIDADE_API_URL", "https://qualidadev8.onrender.com")
QUALIDADE_API_SECRET = os.getenv("QUALIDADE_API_SECRET", "dev-secret")

db = ConversationDB("conversas.db")

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Você é um atendente do Setor de Qualidade da V8 Digital, especializado em consignado privado CLT.

IDENTIDADE:
- Você representa o Setor de Qualidade da V8 Digital
- Nunca diga que é um robô, bot ou assistente virtual — aja como um atendente humano
- Se perguntarem se é humano ou robô, diga apenas que é um atendente do setor de qualidade da V8 Digital
- Use linguagem natural, simpática e próxima — como um atendente real faria no WhatsApp
- Sempre se apresente como do "Setor de Qualidade da V8 Digital"

OBJETIVO:
Ajudar clientes com parcelas em aberto do empréstimo consignado CLT, verificar situação do desconto em holerite e, quando necessário, encaminhar para emissão de boleto.

FLUXO DE ATENDIMENTO:
1. Cumprimente cordialmente e se apresente como Setor de Qualidade da V8 Digital
2. Informe que o cliente tem parcela(s) em aberto e confirme se a empresa fez o desconto no holerite
3. Se a empresa fez o desconto → agradeça e informe que está tudo certo, mas verifique se há valor parcial pendente
4. Se a empresa NÃO fez o desconto → informe que será necessário regularizar e ofereça emissão de boleto
5. Para emitir boleto → confirme a data de vencimento desejada pelo cliente
6. Encaminhe para atendente humano para emissão do boleto e finalização

REGRAS IMPORTANTES:
- Seja sempre respeitoso, empático e profissional
- Nunca pressione ou ameace o cliente
- Apresente os valores em R$ no formato brasileiro (ex: R$ 899,37)
- Chame o cliente sempre pelo primeiro nome quando souber
- Se o cliente tiver múltiplos contratos, liste-os e pergunte sobre qual ele quer tratar
- Se não conseguir identificar o cliente, peça o CPF educadamente (somente números)
- Nunca invente valores ou informações que não estão no contexto fornecido
- Respostas curtas e objetivas — estamos no WhatsApp
- Quando o cliente pedir boleto, confirme a data e avise que um atendente vai finalizar

EXEMPLOS DE ABORDAGEM:
- Abertura: "Olá [Nome], tudo bem? Somos do Setor de Qualidade da V8 Digital. Você tem uma parcela do seu empréstimo CLT em aberto. Poderia me confirmar se a empresa fez o desconto no seu holerite?"
- Se não houve desconto: "Como não houve o desconto direto no holerite, será necessário regularizar para evitar pendências no contrato. Posso gerar um boleto para você. Qual data prefere para vencimento?"
- Se houve desconto parcial: "A empresa realizou o desconto, porém como atingiu o limite de 35% da margem, ficou um valor pendente de R$ [valor] a ser regularizado pela(o) sr(a). Posso gerar um boleto para essa diferença?"
- Encerramento após boleto: "Assim que realizar o pagamento, envie o comprovante para darmos baixa. Qualquer dúvida estou à disposição! 😊"

Quando tiver os dados do cliente no contexto, use-os para personalizar o atendimento pelo nome e apresentar os valores corretos das parcelas.
"""

# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

@app.get("/")
async def health_check():
    return {"status": "online", "servico": "Cobrança V8 Digital — Claude IA"}


@app.post("/webhook")
async def receber_webhook(request: Request):
    try:
        corpo = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON inválido")

    evento = corpo.get("event")
    if evento != "NewMessage":
        return JSONResponse({"ok": True, "ignorado": f"evento {evento}"})

    msg        = corpo.get("message", {})
    ticket_id  = str(msg.get("ticketId", ""))
    contact_id = str(msg.get("contactId", ""))
    body       = msg.get("body", "").strip()
    from_me    = msg.get("fromMe", False)
    send_type  = msg.get("sendType", "")
    is_note    = msg.get("note", False)

    # Número real do WhatsApp está em message.ticket.contact.number
    ticket_obj  = msg.get("ticket", {})
    contact_obj = ticket_obj.get("contact", {})
    raw_obj     = msg.get("raw", {})
    numero_raw  = str(
        contact_obj.get("number")
        or raw_obj.get("from")
        or contact_id
    )

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

async def processar_mensagem(ticket_id: str, contact_id: str, numero_whatsapp: str, mensagem: str):

    # 1. Monta contexto do cliente consultando o qualidadev8
    contexto = await _montar_contexto(numero_whatsapp, mensagem, ticket_id)

    # 2. Salva mensagem do cliente
    db.salvar_mensagem(conversa_id=ticket_id, papel="user", conteudo=mensagem, numero=contact_id)

    # 3. Busca histórico
    historico = db.buscar_historico(conversa_id=ticket_id, limite=20)

    # 4. Chama Claude
    try:
        resposta = await chamar_claude(historico, contexto)
    except Exception as e:
        log.error(f"Erro Claude: {e}")
        resposta = "Desculpe, ocorreu uma instabilidade. Um atendente entrará em contato em breve!"

    log.info(f"✅ ticket={ticket_id}: {resposta[:100]}")

    # 5. Salva e envia
    db.salvar_mensagem(conversa_id=ticket_id, papel="assistant", conteudo=resposta, numero=contact_id)
    await enviar_mensagem_vectax(ticket_id, numero_whatsapp, resposta)


async def _montar_contexto(numero_whatsapp: str, mensagem: str, ticket_id: str) -> str:
    """
    Tenta identificar o cliente pelo WhatsApp ou CPF extraído da mensagem.
    Consulta o qualidadev8 e retorna contexto formatado para a Claude.
    """

    # Tenta pelo número de WhatsApp
    dados = await _consultar_api(f"/api/cobranca/whatsapp/{numero_whatsapp}")

    # Se não achou, tenta extrair CPF da mensagem
    if not dados or not dados.get("encontrado"):
        cpf = _extrair_cpf(mensagem)
        if cpf:
            dados = await _consultar_api(f"/api/cobranca/cpf/{cpf}")

    if not dados or not dados.get("encontrado"):
        return (
            "CONTEXTO DO CLIENTE:\n"
            "Cliente não encontrado na base pelo número de WhatsApp.\n"
            "Peça o CPF ao cliente educadamente para localizar os contratos."
        )

    contratos = dados.get("contratos", [])

    if len(contratos) == 1:
        # Busca detalhes completos do contrato
        num = contratos[0]["numero_contrato"]
        detalhe = await _consultar_api(f"/api/cobranca/contrato/{num}")
        if detalhe:
            return _formatar_contexto_contrato(detalhe)

    # Múltiplos contratos — lista resumida
    linhas = [
        f"CONTEXTO DO CLIENTE:\n"
        f"Nome: {contratos[0]['nome']}\n"
        f"CPF: {contratos[0]['cpf']}\n"
        f"Este cliente possui {len(contratos)} contrato(s):\n"
    ]
    for c in contratos:
        linhas.append(
            f"- Contrato {c['numero_contrato']} | "
            f"Empresa: {c['empresa']} | "
            f"Parcela: R$ {c['valor_parcela']:.2f} | "
            f"Status: {c['status']}"
        )
    linhas.append("\nPergunte ao cliente sobre qual contrato deseja tratar.")
    return "\n".join(linhas)


def _formatar_contexto_contrato(detalhe: dict) -> str:
    """Formata o retorno da API de detalhes em texto para a Claude."""
    c = detalhe.get("contrato", {})
    r = detalhe.get("resumo", {})
    pendentes = detalhe.get("parcelas_pendentes", [])

    linhas = [
        "CONTEXTO DO CLIENTE:",
        f"Nome: {c.get('nome')}",
        f"CPF: {c.get('cpf')}",
        f"Empresa: {c.get('empresa')}",
        f"Contrato: {c.get('numero')} ({c.get('provider')})",
        f"Valor da parcela: R$ {c.get('valor_parcela', 0):.2f}",
        f"Total de parcelas: {c.get('n_parcelas')}",
        "",
        "SITUAÇÃO FINANCEIRA:",
        f"Parcelas vencidas/pendentes: {r.get('parcelas_vencidas', 0)}",
        f"Parcelas pagas: {r.get('parcelas_pagas', 0)}",
        f"Total em aberto: R$ {r.get('total_aberto', 0):.2f}",
        f"Valor presente (VP) total: R$ {r.get('total_vp', 0):.2f}",
    ]

    if pendentes:
        linhas.append("\nPARCELAS PENDENTES:")
        for p in pendentes[:6]:  # máx 6 para não lotar o contexto
            linhas.append(
                f"  Parcela {p['numero']} | Venc: {p['vencimento']} | "
                f"Em aberto: R$ {p['em_aberto']:.2f} | Status: {p['status']}"
            )

    return "\n".join(linhas)


async def chamar_claude(historico: list[dict], contexto: str) -> str:
    system = SYSTEM_PROMPT + "\n\n" + contexto
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


async def enviar_mensagem_vectax(ticket_id: str, numero: str, mensagem: str):
    url = f"{VECTAX_API_URL}/v1/api/external/{VECTAX_API_ID}"
    payload = {"body": mensagem, "number": numero, "externalKey": ticket_id}
    headers = {"Authorization": f"Bearer {VECTAX_TOKEN}", "Content-Type": "application/json"}
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

async def _consultar_api(path: str) -> Optional[dict]:
    """Faz uma requisição autenticada para a API do qualidadev8."""
    url = f"{QUALIDADE_API_URL}{path}"
    headers = {"X-API-Key": QUALIDADE_API_SECRET}
    try:
        resp = await chatbot_client.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        log.warning(f"API qualidadev8 {resp.status_code}: {path}")
        return None
    except Exception as e:
        log.error(f"Erro ao consultar qualidadev8 ({path}): {e}")
        return None


def _extrair_cpf(texto: str) -> Optional[str]:
    m = re.search(r"\d{3}[\.\s]?\d{3}[\.\s]?\d{3}[-\s]?\d{2}", texto)
    if m:
        return re.sub(r"\D", "", m.group())
    return None
