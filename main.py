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
# Cliente HTTP criado por requisição para evitar conexões expiradas

# Vectax
VECTAX_API_URL = os.getenv("VECTAX_API_URL", "https://enterprise-369api.v8sistema.com")
VECTAX_API_ID  = os.getenv("VECTAX_API_ID")
VECTAX_TOKEN   = os.getenv("VECTAX_TOKEN")

# qualidadev8 — API interna de contratos
QUALIDADE_API_URL    = os.getenv("QUALIDADE_API_URL", "https://qualidadev8.onrender.com")
QUALIDADE_API_SECRET = os.getenv("QUALIDADE_API_SECRET", "dev-secret")

# Vectax Front API — token de login (expira em 3 dias, renovar automaticamente)
VECTAX_FRONT_TOKEN = os.getenv("VECTAX_FRONT_TOKEN", "")
VECTAX_FRONT_URL   = "https://enterprise-369api.v8sistema.com"
VECTAX_LOGIN_EMAIL = os.getenv("VECTAX_LOGIN_EMAIL", "")
VECTAX_LOGIN_PASS  = os.getenv("VECTAX_LOGIN_PASS", "")

db = ConversationDB("conversas.db")

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """Você é um atendente do Setor de Qualidade da V8 Digital, especializado em consignado privado CLT.

IDENTIDADE:
- Você representa o Setor de Qualidade da V8 Digital
- Nunca diga que é um robô, bot ou assistente virtual — aja como um atendente humano
- Se perguntarem se é humano ou robô, diga apenas que é um atendente do setor de qualidade da V8 Digital
- Use linguagem natural, próxima e profissional — como um atendente real faria no WhatsApp
- Nunca use emojis em nenhuma mensagem
- Sempre se apresente como do "Setor de Qualidade da V8 Digital"

OBJETIVO:
Ajudar clientes com parcelas em aberto do empréstimo consignado CLT, verificar a situação dos descontos em holerite e orientar corretamente sobre o que o cliente deve pagar.

CONCEITO IMPORTANTE — DESCONTO EM FOLHA:
O desconto em folha pelo empregador NÃO garante que a parcela foi quitada. O desconto pode ter sido parcial (empresa atingiu o limite de 35% da margem consignável). Sempre verifique quantos descontos ocorreram e se foram parciais ou integrais.

REGRAS DE NEGÓCIO — ANÁLISE DAS PARCELAS:
Use os dados do contexto (status "Parcial", "Vencido", "Pago") para identificar o cenário e orientar corretamente:

CENÁRIO 1 — 1 parcela Parcial + 1 parcela Vencida, com apenas 1 desconto em folha (parcial):
- O cliente deve pagar o valor em aberto da parcela Parcial (diferença) E a parcela Vencida integralmente
- Motivo: só houve 1 desconto e foi insuficiente

CENÁRIO 2 — 2 parcelas Parciais, com 2 descontos em folha (ambos parciais):
- O cliente deve pagar a diferença de cada parcela parcial
- Motivo: houve desconto nas duas, mas ambos foram insuficientes

CENÁRIO 3 — 1 parcela Parcial + 1 parcela Vencida, com 2 descontos (1 parcial + 1 integral):
- O cliente é responsável apenas pelo valor em aberto da parcela Parcial
- Para a parcela Vencida com desconto integral: solicitar o contato do RH da empresa para verificar o repasse que não foi localizado
- Motivo: a empresa descontou integralmente mas o valor não chegou — problema no repasse

FLUXO DE ATENDIMENTO:
1. Cumprimente e se apresente como Setor de Qualidade da V8 Digital
2. Informe as parcelas pendentes com valores e vencimentos
3. Pergunte se a empresa realizou o desconto no holerite e quantas vezes
4. Com base na resposta, aplique o cenário correto acima
5. Informe claramente o que o cliente deve pagar
6. Ofereça emissão de boleto e confirme a data de vencimento desejada
7. Encaminhe para atendente humano para finalizar a emissão

REGRAS GERAIS:
- Seja sempre respeitoso e profissional
- Nunca pressione ou ameace o cliente
- Apresente valores em R$ no formato brasileiro (ex: R$ 192,35)
- Chame o cliente pelo primeiro nome quando souber
- Se não identificar o cliente, peça o CPF (somente números)
- Nunca invente valores ou informações fora do contexto
- Respostas curtas e objetivas — estamos no WhatsApp
- Nunca use emojis

EXEMPLOS DE ABORDAGEM:
- Abertura: "Ola [Nome], tudo bem? Sou do Setor de Qualidade da V8 Digital. Verifiquei que voce tem parcelas do seu emprestimo CLT em aberto. Poderia me confirmar se a empresa realizou o desconto dessas parcelas no seu holerite?"
- Desconto parcial: "A empresa realizou o desconto, porem como atingiu o limite de 35% da margem consignavel, ficou um valor pendente a ser regularizado pelo senhor(a)."
- Sem desconto: "Como nao houve o desconto no holerite, sera necessario regularizar para evitar pendencias no contrato. Posso gerar um boleto. Qual data prefere para o vencimento?"
- Solicitar RH: "Para a parcela com desconto integral, vou precisar verificar com o RH da sua empresa o repasse que nao foi localizado. Poderia me passar o contato do responsavel pelo RH?"
- Encerramento: "Assim que realizar o pagamento, envie o comprovante para darmos baixa. Qualquer duvida estou a disposicao."

Quando tiver os dados do cliente no contexto, use-os para personalizar o atendimento e apresentar os valores corretos de cada parcela.
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

    # Captura o número exato quando um novo contato é criado
    if evento == "NewContact":
        contato = corpo.get("contact", {})
        numero_contato = contato.get("number", "")
        contact_id_novo = str(contato.get("id", ""))
        if numero_contato and contact_id_novo:
            db.salvar_numero_contato(contact_id_novo, numero_contato)
            log.info(f"👤 Novo contato id={contact_id_novo} number={numero_contato}")
        return JSONResponse({"ok": True})

    if evento != "NewMessage":
        return JSONResponse({"ok": True, "ignorado": f"evento {evento}"})

    msg        = corpo.get("message", {})
    ticket_id  = str(msg.get("ticketId", ""))
    contact_id = str(msg.get("contactId", ""))
    body       = msg.get("body", "").strip()
    from_me    = msg.get("fromMe", False)
    send_type  = msg.get("sendType", "")
    is_note    = msg.get("note", False)

    # Número real do WhatsApp — prioriza raw.from (sempre tem o número real)
    # contact.number pode ser ID interno quando contato não está cadastrado
    ticket_obj   = msg.get("ticket", {})
    contact_obj  = ticket_obj.get("contact", {})
    raw_obj      = msg.get("raw", {})
    # raw.from = número real do WhatsApp (com o 9)
    # contact.number = número cadastrado na Vectax (pode ser sem o 9)
    numero_raw       = str(raw_obj.get("from") or contact_obj.get("number") or contact_id)
    numero_vectax    = str(contact_obj.get("number") or numero_raw)  # como a Vectax cadastrou
    log.info(f"📞 numero={numero_raw} (raw={raw_obj.get('from')} contact={contact_obj.get('number')})")

    # Normaliza número brasileiro — garante o 9 no celular
    # Ex: 554784141181 (11 dígitos sem 9) → 5547984141181 (12 dígitos com 9)
    numero_raw = _normalizar_numero_br(numero_raw)
    log.info(f"📞 numero normalizado={numero_raw} raw_obj={raw_obj} raw_vazio={not raw_obj or raw_obj == {} or raw_obj == "{}"}")

    # raw=None indica webhook de ACK/confirmação de entrega — não é mensagem do cliente
    raw_vazio = not raw_obj or raw_obj == {} or raw_obj == "{}"
    if from_me or send_type in ("bot", "API") or is_note or not body or raw_vazio:
        return JSONResponse({"ok": True, "ignorado": "filtrado"})

    if not ticket_id or not contact_id:
        return JSONResponse({"ok": True, "ignorado": "sem identificadores"})

    log.info(f"✉ ticket={ticket_id} numero={numero_raw} contact_id={contact_id} fromMe={from_me} sendType={send_type} | {body[:80]}")

    await processar_mensagem(
        ticket_id=ticket_id,
        contact_id=contact_id,
        numero_whatsapp=numero_raw,
        numero_vectax=numero_vectax,
        mensagem=body,
    )

    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Lógica principal
# ---------------------------------------------------------------------------

async def processar_mensagem(ticket_id: str, contact_id: str, numero_whatsapp: str, mensagem: str, numero_vectax: str = ""):

    # 0. Busca o ticket correto via API do front da Vectax
    ticket_id_real = await _buscar_ticket_aberto(numero_whatsapp, ticket_id)
    if ticket_id_real != ticket_id:
        log.info(f"🎯 Usando ticket real={ticket_id_real} em vez de ticket={ticket_id}")

    # 1. Monta contexto do cliente consultando o qualidadev8
    contexto = await _montar_contexto(numero_whatsapp, mensagem, ticket_id_real)

    # 2. Salva mensagem do cliente
    db.salvar_mensagem(conversa_id=ticket_id_real, papel="user", conteudo=mensagem, numero=contact_id)

    # 3. Busca histórico
    historico = db.buscar_historico(conversa_id=ticket_id_real, limite=20)

    # 4. Chama Claude
    try:
        resposta = await chamar_claude(historico, contexto)
    except Exception as e:
        log.error(f"Erro Claude: {e}")
        resposta = "Desculpe, ocorreu uma instabilidade. Um atendente entrará em contato em breve!"

    log.info(f"✅ ticket={ticket_id_real}: {resposta[:100]}")

    # 5. Salva e envia
    db.salvar_mensagem(conversa_id=ticket_id_real, papel="assistant", conteudo=resposta, numero=contact_id)
    await enviar_mensagem_vectax(ticket_id_real, numero_whatsapp, resposta, contact_id, numero_vectax or numero_whatsapp)


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


async def enviar_mensagem_vectax(ticket_id: str, numero: str, mensagem: str, contact_id: str = "", numero_vectax: str = ""):
    """
    Envia mensagem via API externa da Vectax.
    Usa o número como externalKey para que nas próximas mensagens
    a Vectax encontre o mesmo ticket e não crie um novo.
    """
    url = f"{VECTAX_API_URL}/v1/api/external/{VECTAX_API_ID}"

    # Usa o ticket_id como externalKey — assim nas próximas mensagens
    # do mesmo ticket a Vectax encontra e não cria um novo
    # Busca o número exato como a Vectax cadastrou (via NewContact)
    numero_salvo = db.buscar_numero_contato(contact_id) if contact_id else ""
    if numero_salvo:
        numero_envio = numero_salvo
    else:
        # Fallback: usa o número do raw sem o 9 se tiver 13 dígitos
        numero_envio = _remover_nono_digito(numero)
    external_key = numero_envio
    log.info(f"📤 numero_envio={numero_envio} (salvo={numero_salvo} original={numero})")

    payload = {
        "body":        mensagem,
        "number":      numero_envio,
        "externalKey": external_key,
    }
    headers = {"Authorization": f"Bearer {VECTAX_TOKEN}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
        log.info(f"📤 Enviado ticket={ticket_id} numero={numero} externalKey={external_key} status={resp.status_code}")
    except Exception as e:
        log.error(f"Falha envio: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _setar_external_key_ticket(ticket_id: str, numero: str):
    """
    Busca o ticket aberto do cliente via API do front da Vectax.
    Se encontrar um ticket aberto com externalKey nulo, usa o ticketId
    do cliente para responder no ticket correto.
    Retorna o ticketId correto para usar no envio.
    """
    # Por enquanto mantém o ticket_id recebido
    # A busca via front API será implementada quando o token estiver configurado
    pass


async def _buscar_ticket_aberto(numero: str, ticket_id_atual: str) -> str:
    """
    Busca tickets abertos pelo número do cliente via API do front.
    Retorna o ticketId mais recente encontrado.
    """
    if not VECTAX_FRONT_TOKEN:
        return ticket_id_atual

    token = await _obter_token_front()
    if not token:
        return ticket_id_atual

    from datetime import datetime, timedelta
    hoje = datetime.now()
    ontem = hoje - timedelta(days=30)

    url = f"{VECTAX_FRONT_URL}/tickets-search"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": "https://chat.v8sistema.com",
        "Referer": "https://chat.v8sistema.com/",
    }
    payload = {
        "contactName": "",
        "contact": numero,
        "startDate": ontem.strftime("%Y-%m-%dT00:00:00"),
        "endDate": hoje.strftime("%Y-%m-%dT23:59:59"),
        "status": ["open", "pending"],
        "bots": [], "channels": [], "closingReasons": [],
        "isActiveDemand": None, "isCreatedDate": True,
        "isNotPagination": True, "notas": [], "pageNumber": 1,
        "protocolNumber": "", "queues": [], "tags": [], "users": [],
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
        log.info(f"🔍 tickets-search status={resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            tickets = data if isinstance(data, list) else data.get("tickets", [])
            if tickets:
                # Retorna o ticket mais recente
                ticket = sorted(tickets, key=lambda t: t.get("createdAt", ""), reverse=True)[0]
                tid = str(ticket.get("id", ticket_id_atual))
                log.info(f"🎯 Ticket encontrado: {tid} (atual: {ticket_id_atual})")
                return tid
    except Exception as e:
        log.warning(f"Falha na busca de ticket: {e}")

    return ticket_id_atual


async def _obter_token_front() -> str:
    """
    Obtém ou renova o token do front da Vectax.
    Usa o token configurado na variável de ambiente ou faz login.
    """
    # Se tiver token configurado, usa ele
    if VECTAX_FRONT_TOKEN:
        return VECTAX_FRONT_TOKEN

    # Senão, faz login para obter token
    if not VECTAX_LOGIN_EMAIL or not VECTAX_LOGIN_PASS:
        return ""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{VECTAX_FRONT_URL}/auth/login",
                json={"email": VECTAX_LOGIN_EMAIL, "password": VECTAX_LOGIN_PASS},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code == 200:
            return resp.json().get("token", "")
    except Exception as e:
        log.error(f"Falha no login Vectax front: {e}")

    return ""


async def _consultar_api(path: str) -> Optional[dict]:
    """Faz uma requisição autenticada para a API do qualidadev8."""
    url = f"{QUALIDADE_API_URL}{path}"
    headers = {"X-API-Key": QUALIDADE_API_SECRET}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
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


def _normalizar_numero_br(numero: str) -> str:
    """
    Garante que o número celular brasileiro tenha o 9 dígito.
    Formato esperado: 55 + DDD (2) + 9 + número (8) = 13 dígitos
    Sem o 9:          55 + DDD (2) + número (8)     = 12 dígitos
    """
    import re as _re
    n = _re.sub(r"\D", "", numero)
    if n.startswith("55") and len(n) == 12:
        n = n[:4] + "9" + n[4:]
    return n


def _remover_nono_digito(numero: str) -> str:
    """
    Remove o 9 dígito somente quando o número tem 9 dígitos no telefone.
    A Vectax cadastra o contato exatamente como recebe do WhatsApp.
    - 55 + DDD(2) + 9(1) + 8 dígitos = 13 dígitos → remove o 9 → 12 dígitos
    - 55 + DDD(2) + 8 dígitos = 12 dígitos → já está correto, não mexe
    """
    import re as _re
    n = _re.sub(r"[^0-9]", "", numero)
    # Só remove o 9 se o número tiver 13 dígitos (DDI+DDD+9+8)
    # Números com 12 dígitos (DDI+DDD+8) já estão no formato correto
    if n.startswith("55") and len(n) == 13 and n[4] == "9":
        n = n[:4] + n[5:]
    return n
