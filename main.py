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

# Meta WhatsApp Cloud API — envio direto sem passar pela Vectax
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
META_ACCESS_TOKEN    = os.getenv("META_ACCESS_TOKEN", "")

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


@app.get("/webhook")
async def webhook_verify(request: Request):
    """Verificacao do webhook pelo Meta WhatsApp Cloud API."""
    params    = request.query_params
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == "chatv8":
        log.info("Webhook Meta verificado com sucesso")
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(challenge)
    return JSONResponse({"error": "forbidden"}, status_code=403)


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

    # Evita processar a mesma mensagem duas vezes
    msg_id = raw_obj.get("id", "") or f"{ticket_id}_{body[:20]}"
    if msg_id in _mensagens_processadas:
        log.info(f"⚠️ Mensagem duplicada ignorada: {msg_id}")
        return JSONResponse({"ok": True, "ignorado": "duplicada"})
    _mensagens_processadas.add(msg_id)
    # Limpa cache se ficar muito grande
    if len(_mensagens_processadas) > 1000:
        _mensagens_processadas.clear()

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
    Tenta enviar pela Meta Cloud API primeiro (sem Vectax — sem ticket duplicado).
    Se não configurado, tenta chat-flow-step.
    Se falhar, usa a API externa da Vectax como fallback.
    """
    # 1. Tenta Meta Cloud API — envio direto, sem criar ticket/contato duplicado
    if META_PHONE_NUMBER_ID and META_ACCESS_TOKEN:
        enviado = await _enviar_via_meta(numero, mensagem)
        if enviado:
            return

    # 2. Tenta chat-flow-step (só funciona com ChatFlow ativo)
    enviado = await _enviar_via_chat_flow_step(ticket_id, mensagem)
    if not enviado:
        # 3. Fallback: API externa da Vectax
        numero_salvo = db.buscar_numero_contato(contact_id) if contact_id else ""
        numero_envio = numero_salvo if numero_salvo else numero
        log.info(f"📤 Fallback Vectax numero_envio={numero_envio}")
        await _enviar_via_api_externa(ticket_id, numero_envio, mensagem)


async def _enviar_via_meta(numero: str, mensagem: str) -> bool:
    """
    Envia mensagem diretamente pela API do Meta (WhatsApp Cloud API).
    Não passa pela Vectax — elimina o problema do ticket/contato duplicado.
    """
    if not META_PHONE_NUMBER_ID or not META_ACCESS_TOKEN:
        return False

    # Usa o número normalizado (sem o 9 se tiver 13 dígitos)
    numero_meta = _remover_nono_digito(numero)
    # Remove o 55 do início se necessário — Meta usa formato internacional
    if numero_meta.startswith("55"):
        numero_meta = numero_meta  # mantém com 55

    url = f"https://graph.facebook.com/v19.0/{META_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": numero_meta,
        "type": "text",
        "text": {"body": mensagem}
    }
    headers = {
        "Authorization": f"Bearer {META_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
        log.info(f"📤 Meta API numero={numero_meta} status={resp.status_code} body={resp.text[:200]}")
        if resp.status_code == 200:
            return True
    except Exception as e:
        log.error(f"Falha Meta API: {e}")
    return False


async def _enviar_via_chat_flow_step(ticket_id: str, mensagem: str) -> bool:
    """
    Envia mensagem diretamente no ticket via chat-flow-step.
    Não precisa de number nem externalKey — usa o ticketId diretamente.
    Isso evita criação de ticket/contato duplicado.
    """
    url = f"{VECTAX_API_URL}/v1/api/external/{VECTAX_API_ID}/{ticket_id}/chat-flow-step"
    payload = {"body": mensagem}
    headers = {"Authorization": f"Bearer {VECTAX_TOKEN}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
        log.info(f"📤 chat-flow-step ticket={ticket_id} status={resp.status_code} body={resp.text[:200]}")
        if resp.status_code == 200:
            return True
    except Exception as e:
        log.error(f"Falha chat-flow-step: {e}")
    return False


async def _enviar_via_api_externa(ticket_id: str, numero: str, mensagem: str):
    """
    Fallback: API externa.
    Envia com o número exato do contato (como foi cadastrado pela Vectax via NewContact).
    """
    url = f"{VECTAX_API_URL}/v1/api/external/{VECTAX_API_ID}"
    payload = {
        "body":        mensagem,
        "number":      numero,
        "externalKey": numero,
    }
    headers = {"Authorization": f"Bearer {VECTAX_TOKEN}", "Content-Type": "application/json"}
    log.info(f"📤 API externa ticket={ticket_id} number={numero} externalKey={numero}")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
        log.info(f"📤 Enviado status={resp.status_code} body={resp.text[:200]}")
    except Exception as e:
        log.error(f"Falha envio API externa: {e}")

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
    token = await _obter_token_front()
    if not token:
        log.warning("Sem token Vectax front — usando ticket_id atual")
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


# Cache do token em memória
_token_cache: dict = {"token": "", "expiry": 0.0}

# Cache de mensagens já processadas (evita duplicatas)
_mensagens_processadas: set = set()


async def _obter_token_front() -> str:
    """
    Obtém ou renova o token do front da Vectax automaticamente.
    Faz login com email/senha e armazena em cache por 2 dias.
    """
    import time

    # Verifica cache
    if _token_cache["token"] and time.time() < _token_cache["expiry"]:
        return _token_cache["token"]

    # Token fixo (prioridade)
    if VECTAX_FRONT_TOKEN:
        _token_cache["token"] = VECTAX_FRONT_TOKEN
        _token_cache["expiry"] = time.time() + 172800
        return VECTAX_FRONT_TOKEN

    # Login automático
    email    = os.getenv("VECTAX_LOGIN_EMAIL", "")
    password = os.getenv("VECTAX_LOGIN_PASSWORD", "")

    if not email or not password:
        log.warning("VECTAX_LOGIN_EMAIL ou VECTAX_LOGIN_PASSWORD nao configurados")
        return ""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                "https://turbochatapi.v8sistema.com/auth/login/",
                json={"email": email, "password": password},
                headers={"Content-Type": "application/json"},
            )
        log.info(f"Login Vectax status={resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            token = (data.get("token") or data.get("access")
                     or data.get("access_token") or data.get("key", ""))
            if token:
                _token_cache["token"] = token
                _token_cache["expiry"] = time.time() + 172800
                log.info("Token Vectax renovado com sucesso")
                return token
            log.error(f"Token nao encontrado: {list(data.keys())}")
        else:
            log.error(f"Login falhou: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        log.error(f"Falha no login Vectax: {e}")

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
    Remove o 9 dígito de todos os números brasileiros de 13 dígitos.
    A Vectax normaliza internamente para 12 dígitos (sem o 9) no canal WABA.
    """
    import re as _re
    n = _re.sub(r"[^0-9]", "", numero)
    # Remove o 9 de qualquer número de 13 dígitos (55 + DDD + 9 + 8)
    if n.startswith("55") and len(n) == 13 and n[4] == "9":
        n = n[:4] + n[5:]
    return n
