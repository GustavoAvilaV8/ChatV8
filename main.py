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
from datetime import date, timedelta
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

SYSTEM_PROMPT = """Você é um atendente do Setor de Qualidade da V8 Digital, especializado em empréstimo consignado privado CLT.

IDENTIDADE:
- Você representa o Setor de Qualidade da V8 Digital
- Nunca diga que é robô, bot ou assistente virtual — aja como atendente humano
- Se perguntarem se é humano, diga apenas que é atendente do Setor de Qualidade da V8 Digital
- Use linguagem natural, próxima e profissional — como atendente real no WhatsApp
- Nunca use emojis
- Mensagens curtas — um assunto por vez
- Nunca use caixa alta
- Nunca use termos como "último aviso" ou "ação judicial"
- Nunca ameace ou constranja o cliente (CDC — Lei 8.078/1990, art. 42)
- Nunca compartilhe informações financeiras com terceiros (LGPD)

OBJETIVO:
Verificar parcelas em aberto do empréstimo consignado CLT, entender a situação do desconto em folha e orientar o cliente sobre regularização.

CONCEITO IMPORTANTE — DESCONTO EM FOLHA:
O desconto em folha pelo empregador NÃO garante que a parcela foi quitada. O desconto pode ter sido parcial (empresa atingiu o limite de 35% da margem consignável). Sempre verifique quantos descontos ocorreram e se foram parciais ou integrais.

REGRAS DE NEGÓCIO — ANÁLISE DAS PARCELAS:

CENÁRIO 1 — 1 parcela Parcial + 1 parcela Vencida, com apenas 1 desconto em folha (parcial):
- O cliente deve pagar o valor em aberto da parcela Parcial (diferença) E a parcela Vencida integralmente
- Motivo: só houve 1 desconto e foi insuficiente

CENÁRIO 2 — 2 parcelas Parciais, com 2 descontos em folha (ambos parciais):
- O cliente deve pagar a diferença de cada parcela parcial
- Motivo: houve desconto nas duas, mas ambos foram insuficientes

CENÁRIO 3 — 1 parcela Parcial + 1 parcela Vencida, com 2 descontos (1 parcial + 1 integral):
- O cliente é responsável apenas pelo valor em aberto da parcela Parcial
- Para a parcela Vencida com desconto integral: solicitar contato do RH da empresa
- Motivo: a empresa descontou integralmente mas o valor não chegou — problema no repasse

FLUXO DE ATENDIMENTO:
1. Chame o cliente pelo primeiro nome desde a primeira mensagem
2. Se identificar pelo telefone, vá direto ao ponto — não peça o contrato
3. Informe as parcelas pendentes de forma simples: quantas, valor total
4. Pergunte se a empresa descontou no holerite e quantas vezes
5. Com base na resposta, aplique o cenário correto
6. Informe o que o cliente deve pagar
7. Pergunte a data de vencimento desejada para o boleto — OBRIGATÓRIO antes de gerar
8. Após o cliente informar a data, diga apenas: "Certo, vou gerar o boleto agora." — NÃO mencione atendente, NÃO encaminhe para ninguém, o sistema gera automaticamente
9. Se tiver mais de 1 parcela vencida, emita uma por vez e informe que as demais continuam pendentes

REGRAS GERAIS:
- Sempre respeitoso — nunca pressione ou ameace
- Valores em R$ no formato brasileiro (ex: R$ 192,35)
- Nunca invente valores fora do contexto fornecido
- Refinanciamento NÃO está disponível — foque no boleto

REGRA DE TOM — OBRIGATÓRIA:
- Máximo 3 blocos de mensagem por resposta
- Linguagem direta e próxima, sem formalidade excessiva
- Pode usar asteriscos para destacar informações importantes (contrato, valores, datas)
- Pode usar bullets (·) para listar parcelas ou situação financeira
- NUNCA diga "encaminhar para atendente" ou "aguarde um momento"
- Tom como o exemplo aprovado:
  "Oi [Nome], tudo bem? Sou do Setor de Qualidade da V8 Digital. Localizei seu contrato MAG[numero]. A empresa realizou o desconto no seu holerite?"
  "Você possui *[X] parcelas vencidas* totalizando *R$ [valor]*. Posso emitir um boleto para regularizar. Qual data prefere para o vencimento?"
  "Certo, vou gerar o boleto de R$ [valor] para [data] agora."

SITUAÇÕES ESPECIAIS:

Cliente demitido:
Entenda com empatia. Informe que o pagamento passa a ser via boleto com valor integral.

Cliente com dificuldade financeira:
Demonstre compreensao. So emite boleto — sem acordos ou descontos.

Cliente agressivo:
Tom respeitoso. Diga: "Estou aqui para ajudar. Podemos focar na solucao?"

EXEMPLOS DE ABORDAGEM:
- Abertura: "Ola [Nome], tudo bem? Sou do Setor de Qualidade da V8 Digital. Identifiquei [X] parcelas em aberto no contrato [numero], totalizando R$ [valor]. A empresa realizou o desconto no seu holerite?"
- Sem desconto: "Entendi. Como nao houve o desconto, sera necessario regularizar. Posso emitir um boleto — qual data prefere para o vencimento?"
- Confirmacao: "Certo, vou gerar o boleto de R$ [valor] para [data] agora."
- Encerramento: "Assim que pagar, manda o comprovante para darmos baixa. Qualquer duvida estou a disposicao."

Quando tiver os dados do cliente no contexto, use-os para personalizar o atendimento com os valores e vencimentos corretos de cada parcela.

ATENÇÃO — SITUAÇÃO FINANCEIRA:
Use APENAS os dados de "SITUAÇÃO FINANCEIRA" do contexto. Se "Parcelas vencidas/pendentes" for maior que 0, há pendências. Se for 0, não há pendências. NUNCA deduza a situação por datas — use sempre os números calculados.

ATENÇÃO — CLIENTE NÃO ENCONTRADO:
Se o contexto indicar que o cliente não foi localizado na base, NÃO invente informações.
Diga honestamente: "Não localizei seu cadastro em nossa base. Poderia verificar se o CPF está correto, ou me informar o número do seu contrato?"
Nunca diga "localizei", "identifiquei pendência" ou apresente dados quando não tiver informações reais no contexto.
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

    # Detecta se é webhook do Meta (formato diferente da Vectax)
    if corpo.get("object") == "whatsapp_business_account":
        return await _processar_webhook_meta(corpo)

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

    # Extrai dados do cliente do contexto
    nome_cliente = ""
    cpf_cliente = ""
    contrato_cliente = ""
    if contexto:
        m = re.search(r'Nome:\s*(.+)', contexto)
        if m: nome_cliente = m.group(1).strip()
        m = re.search(r'CPF:\s*(\d+)', contexto)
        if m: cpf_cliente = m.group(1).strip()
        m = re.search(r'Contrato:\s*(\S+)', contexto)
        if m: contrato_cliente = m.group(1).strip()

    await _salvar_no_crm(numero_whatsapp, "user", mensagem, nome_cliente, cpf_cliente, contrato_cliente)
    await _salvar_no_crm(numero_whatsapp, "assistant", resposta, nome_cliente, cpf_cliente, contrato_cliente)

    # 6. Detecta se o cliente confirmou que quer o boleto e gera automaticamente
    boleto_gerado = await _tentar_gerar_boleto(
        mensagem=mensagem,
        resposta_claude=resposta,
        contexto=contexto,
        ticket_id=ticket_id_real,
        numero_whatsapp=numero_whatsapp,
        contact_id=contact_id,
        numero_vectax=numero_vectax,
    )

    if not boleto_gerado:
        await enviar_mensagem_vectax(ticket_id_real, numero_whatsapp, resposta, contact_id, numero_vectax or numero_whatsapp)


async def _tentar_gerar_boleto(
    mensagem: str, resposta_claude: str, contexto: str,
    ticket_id: str, numero_whatsapp: str, contact_id: str, numero_vectax: str
) -> bool:
    """
    Detecta se o cliente informou uma data para o boleto.
    Só gera quando a última resposta do bot estava pedindo a data de vencimento.
    Retorna True se o boleto foi gerado e enviado.
    """
    msg_lower = mensagem.lower().strip()

    # Detecta dias da semana e converte para data
    DIAS_SEMANA = {
        'segunda': 0, 'segunda-feira': 0, 'segunda feira': 0,
        'terca': 1, 'terça': 1, 'terca-feira': 1, 'terça-feira': 1, 'terca feira': 1, 'terça feira': 1,
        'quarta': 2, 'quarta-feira': 2, 'quarta feira': 2,
        'quinta': 3, 'quinta-feira': 3, 'quinta feira': 3,
        'sexta': 4, 'sexta-feira': 4, 'sexta feira': 4,
        'sabado': 5, 'sábado': 5,
        'domingo': 6,
    }

    def _proximo_dia_semana(alvo_weekday: int) -> date:
        """Retorna a próxima ocorrência do dia da semana (nunca hoje)."""
        hoje = date.today()
        dias = (alvo_weekday - hoje.weekday() + 7) % 7
        if dias == 0:
            dias = 7
        return hoje + timedelta(days=dias)

    data_por_dia_semana = None
    for nome_dia, weekday in DIAS_SEMANA.items():
        if nome_dia in msg_lower:
            data_por_dia_semana = _proximo_dia_semana(weekday)
            break

    # Só gera boleto quando a mensagem contiver uma data ou dia da semana
    m_data = re.search(r'\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?\b|\bdia\s+(\d{1,2})\b|\bpara\s+(\d{1,2})\b', msg_lower)
    if not m_data and not data_por_dia_semana:
        return False

    # Só age se a última mensagem do bot estava falando de boleto E pedindo a data
    historico = db.buscar_historico(conversa_id=ticket_id, limite=6)
    msgs_assistant = [h for h in historico if h["papel"] == "assistant"]
    if not msgs_assistant:
        return False

    ultima_resposta = msgs_assistant[-1]["conteudo"].lower()
    pediu_data = any(p in ultima_resposta for p in [
        "data", "vencimento", "qual data", "prefere", "dia prefere",
        "data prefere", "quando", "data deseja", "para qual data",
    ])
    fala_boleto = any(p in ultima_resposta for p in ["boleto", "providenciar", "emitir", "gerar"])

    if not pediu_data or not fala_boleto:
        return False

    # Extrai dados do contexto
    nome      = ""
    cpf       = ""
    contrato  = ""
    valor     = 0.0
    parcelas  = []
    provider  = ""

    if contexto:
        m = re.search(r'Nome:\s*(.+)', contexto)
        if m: nome = m.group(1).strip()
        m = re.search(r'CPF:\s*(\d+)', contexto)
        if m: cpf = m.group(1).strip()
        m = re.search(r'Contrato:\s*(\S+)', contexto)
        if m: contrato = m.group(1).strip().rstrip('(').strip()
        m = re.search(r'Contrato:\s*\S+\s*\((\w+)\)', contexto)
        if m: provider = m.group(1).upper()
        m = re.search(r'Total em aberto:\s*R\$\s*([\d.,]+)', contexto)
        if m:
            valor = float(m.group(1).replace('.', '').replace(',', '.'))
        nums = re.findall(r'Parcela\s+(\d+)', contexto)
        parcelas = [int(n) for n in nums[:1]]  # só a primeira (mais antiga)

    if not contrato:
        # Tenta achar o contrato no histórico da conversa
        historico_completo = db.buscar_historico(conversa_id=ticket_id, limite=20)
        for msg in reversed(historico_completo):
            m = re.search(r'Contrato:\s*(\S+)', msg.get('conteudo', ''))
            if m:
                contrato = m.group(1).strip().rstrip('(').strip()
                m2 = re.search(r'Contrato:\s*\S+\s*\((\w+)\)', msg.get('conteudo', ''))
                if m2 and not provider:
                    provider = m2.group(1).upper()
                log.info(f"Boleto: contrato {contrato} encontrado no histórico")
                break

    if not contrato:
        log.info("Boleto: contrato não encontrado no contexto nem no histórico")
        return False

    # Se não tem valor no contexto, busca os detalhes do contrato na API
    if valor <= 0:
        log.info(f"Boleto: buscando detalhes do contrato {contrato} na API")
        detalhe = await _consultar_api(f"/api/cobranca/contrato/{contrato}")
        if detalhe:
            pendentes = detalhe.get('parcelas_pendentes', [])
            if pendentes:
                # Pega a parcela mais antiga (primeira da lista)
                primeira = pendentes[0]
                valor    = primeira['em_aberto']
                parcelas = [primeira['numero']]
                if not provider:
                    provider = (detalhe.get('contrato', {}).get('provider') or '').upper()
                if not nome:
                    nome = detalhe.get('contrato', {}).get('nome', '')
                if not cpf:
                    cpf = detalhe.get('contrato', {}).get('cpf', '')
                log.info(f"Boleto: parcela {primeira['numero']} valor={valor} provider={provider}")

    if valor <= 0:
        log.info("Boleto: valor ainda 0 após busca na API — sem parcelas pendentes")
        return False

    # Extrai a data da mensagem do cliente
    vencimento_str = ""

    # Prioridade 1: dia da semana (ex: "segunda-feira", "quinta")
    if data_por_dia_semana:
        vencimento_str = data_por_dia_semana.strftime('%Y-%m-%d')

    # Prioridade 2: "dia 20" ou "para 20"
    if not vencimento_str:
        m_dia = re.search(r'\b(?:dia|para)\s+(\d{1,2})\b', msg_lower)
        if m_dia:
            dia = int(m_dia.group(1))
            hoje = date.today()
            mes = hoje.month if dia > hoje.day else (hoje.month % 12) + 1
            ano = hoje.year if mes >= hoje.month else hoje.year + 1
            try:
                vencimento_str = date(ano, mes, dia).strftime('%Y-%m-%d')
            except Exception:
                pass

    # Prioridade 3: "08/05" ou "08/05/2026" — formato brasileiro dia/mes
    if not vencimento_str:
        m_dt = re.search(r'\b(\d{1,2})[/\-](\d{1,2})(?:[/\-](\d{2,4}))?\b', mensagem)
        if m_dt:
            dia = int(m_dt.group(1))
            mes = int(m_dt.group(2))
            ano = int(m_dt.group(3)) if m_dt.group(3) else date.today().year
            if ano < 100:
                ano += 2000
            # Se a data já passou, avança para o próximo ano
            try:
                d = date(ano, mes, dia)
                if d < date.today():
                    d = date(ano + 1, mes, dia)
                vencimento_str = d.strftime('%Y-%m-%d')
            except Exception:
                pass

    # Se não conseguiu extrair a data, não gera
    if not vencimento_str:
        log.info("Boleto: data informada pelo cliente não reconhecida")
        return False

    log.info(f"💳 Gerando boleto: provider={provider} contrato={contrato} valor={valor} venc={vencimento_str}")

    # Chama a rota correta baseado no provider
    if provider == "QI":
        resultado = await _gerar_boleto_qi(
            numero_contrato=contrato,
            nome=nome,
            cpf=cpf,
            valor=valor,
            vencimento=vencimento_str,
            parcelas=parcelas,
        )
    else:
        # Celcoin ou qualquer outro → Asaas
        resultado = await _gerar_boleto_asaas(
            numero_contrato=contrato,
            nome=nome,
            cpf=cpf,
            valor=valor,
            vencimento=vencimento_str,
            parcelas=parcelas,
        )

    if not resultado or not resultado.get("ok"):
        erro = resultado.get("erro", "erro desconhecido") if resultado else "sem resposta"
        log.error(f"Falha ao gerar boleto: {erro}")
        # Envia resposta normal do Claude + aviso de falha
        await enviar_mensagem_vectax(ticket_id, numero_whatsapp, resposta_claude, contact_id, numero_vectax or numero_whatsapp)
        await enviar_mensagem_vectax(
            ticket_id, numero_whatsapp,
            "Tive uma dificuldade ao gerar o boleto agora. Um atendente vai te enviar em instantes.",
            contact_id, numero_vectax or numero_whatsapp
        )
        return True  # Retorna True para não enviar a resposta normal novamente

    # Monta mensagem com os dados do boleto
    linha = resultado.get("linha_digitavel", "")
    url   = resultado.get("url_boleto", "")
    pix   = resultado.get("pix_copia_cola", "")
    valor_fmt = f"R$ {resultado.get('valor', valor):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    venc_fmt  = vencimento_str[8:] + "/" + vencimento_str[5:7] + "/" + vencimento_str[:4]

    partes = [resposta_claude, ""]
    partes.append(f"Boleto gerado com sucesso!")
    partes.append(f"Valor: {valor_fmt}")
    partes.append(f"Vencimento: {venc_fmt}")

    if linha:
        partes.append(f"\nLinha digitavel:\n{linha}")
    if url:
        partes.append(f"\nLink do boleto:\n{url}")
    if pix:
        partes.append(f"\nPix copia e cola:\n{pix}")
    if resultado.get("pending") and not url and not linha:
        partes.append("\nO link do boleto sera disponibilizado em breve pela QI. Qualquer duvida, estou a disposicao.")

    partes.append("\nApos o pagamento, envie o comprovante para darmos baixa.")

    mensagem_final = "\n".join(partes)
    await enviar_mensagem_vectax(ticket_id, numero_whatsapp, mensagem_final, contact_id, numero_vectax or numero_whatsapp)
    log.info(f"✅ Boleto enviado para {numero_whatsapp}")
    return True


async def _gerar_boleto_asaas(
    numero_contrato: str, nome: str, cpf: str,
    valor: float, vencimento: str, parcelas: list
) -> Optional[dict]:
    """Gera boleto via Asaas (contratos Celcoin) pelo qualidadev8."""
    url = f"{QUALIDADE_API_URL}/chatbot/api/boleto/asaas"
    payload = {
        "numero_contrato": numero_contrato,
        "nome": nome,
        "cpf": cpf,
        "valor": valor,
        "vencimento": vencimento,
        "parcelas": parcelas,
    }
    headers = {"X-API-Key": QUALIDADE_API_SECRET, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
        log.info(f"💳 boleto Asaas status={resp.status_code}")
        return resp.json()
    except Exception as e:
        log.error(f"Erro ao chamar /chatbot/api/boleto/asaas: {e}")
        return None


async def _gerar_boleto_qi(
    numero_contrato: str, nome: str, cpf: str,
    valor: float, vencimento: str, parcelas: list
) -> Optional[dict]:
    """Gera boleto via API QI (Clickmassa) pelo qualidadev8."""
    url = f"{QUALIDADE_API_URL}/chatbot/api/boleto/qi"
    payload = {
        "numero_contrato": numero_contrato,
        "nome": nome,
        "cpf": cpf,
        "valor": valor,
        "vencimento": vencimento,
        "parcelas": parcelas,
    }
    headers = {"X-API-Key": QUALIDADE_API_SECRET, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            resp = await client.post(url, json=payload, headers=headers)
        log.info(f"💳 boleto QI status={resp.status_code}")
        return resp.json()
    except Exception as e:
        log.error(f"Erro ao chamar /chatbot/api/boleto/qi: {e}")
        return None


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

    # Múltiplos contratos — lista com detalhes completos
    nome_cliente = contratos[0]['nome'].split()[0] if contratos[0].get('nome') else ''
    linhas = [
        f"CONTEXTO DO CLIENTE:",
        f"Nome: {contratos[0]['nome']}",
        f"CPF: {contratos[0]['cpf']}",
        f"INSTRUCAO: chame o cliente pelo primeiro nome ({nome_cliente}) desde a primeira mensagem.",
        f"Este cliente possui {len(contratos)} contrato(s) — apresente todos abaixo sem pedir que ele informe o contrato:",
        "",
    ]
    for c in contratos:
        desembolso     = c.get('valor_desembolso') or c.get('valor_contrato') or 0
        primeira_parc  = c.get('data_primeiro_venc') or c.get('primeira_parcela') or ''
        linhas.append(
            f"- Contrato {c['numero_contrato']} | "
            f"Empresa: {c.get('empresa', '')} | "
            f"Valor desembolsado: R$ {float(desembolso):.2f} | "
            f"Primeira parcela: {primeira_parc} | "
            f"Parcela mensal: R$ {float(c.get('valor_parcela', 0)):.2f} | "
            f"Status: {c.get('status', '')}"
        )
    linhas.append("\nApresente todos os contratos e pergunte sobre qual deseja tratar.")
    return "\n".join(linhas)


def _formatar_contexto_contrato(detalhe: dict) -> str:
    """Formata o retorno da API de detalhes em texto para a Claude."""
    c = detalhe.get("contrato", {})
    r = detalhe.get("resumo", {})
    pendentes = detalhe.get("parcelas_pendentes", [])

    nome_completo = c.get('nome', '')
    primeiro_nome = nome_completo.split()[0] if nome_completo else ''

    linhas = [
        "CONTEXTO DO CLIENTE:",
        f"Nome: {nome_completo}",
        f"CPF: {c.get('cpf')}",
        f"INSTRUCAO: chame o cliente pelo primeiro nome ({primeiro_nome}) desde a primeira mensagem.",
        f"Empresa: {c.get('empresa')}",
        f"Contrato: {c.get('numero')} ({c.get('provider')})",
        f"Valor desembolsado: R$ {float(c.get('valor_desembolso') or c.get('valor_contrato') or 0):.2f}",
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
        linhas.append("\nPARCELAS PENDENTES (use estes valores exatos para o boleto):")
        for p in pendentes[:6]:
            linhas.append(
                f"  Parcela {p['numero']} | Venc original: {p['vencimento']} | "
                f"Valor do boleto: R$ {p['em_aberto']:.2f} | Status: {p['status']}"
            )
        linhas.append("\nIMPORTANTE: cada parcela tem seu proprio valor. Use 'Valor do boleto' de cada parcela individualmente — nunca divida o total.")

    return "\n".join(linhas)


async def chamar_claude(historico: list[dict], contexto: str) -> str:
    hoje = date.today().strftime('%d/%m/%Y')
    system = SYSTEM_PROMPT + f"\n\nDATA DE HOJE: {hoje}\n\n" + contexto
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
    # Meta usa o número completo com o 9 — não remove
    numero_meta = _normalizar_numero_br(numero)
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

async def _salvar_no_crm(numero: str, papel: str, conteudo: str, nome: str = "", cpf: str = "", contrato: str = ""):
    """Salva a mensagem no CRM do qualidadev8."""
    url = f"{QUALIDADE_API_URL}/chatbot/api/conversa"
    payload = {
        "numero": numero,
        "papel": papel,
        "conteudo": conteudo,
        "nome_cliente": nome,
        "cpf": cpf,
        "numero_contrato": contrato,
    }
    headers = {"X-API-Key": QUALIDADE_API_SECRET, "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=payload, headers=headers)
    except Exception as e:
        log.warning(f"Falha ao salvar no CRM: {e}")


async def _processar_webhook_meta(corpo: dict):
    """
    Processa webhook no formato da Meta WhatsApp Cloud API.
    Formato completamente diferente da Vectax.
    """
    try:
        entries = corpo.get("entry", [])
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                value = change.get("value", {})
                messages = value.get("messages", [])
                for msg in messages:
                    msg_type = msg.get("type")
                    if msg_type != "text":
                        continue

                    wamid  = msg.get("id", "")
                    numero = msg.get("from", "")
                    body   = msg.get("text", {}).get("body", "").strip()

                    if not body or not numero:
                        continue

                    # Evita duplicatas
                    if wamid in _mensagens_processadas:
                        log.info(f"⚠️ Meta msg duplicada ignorada: {wamid}")
                        continue
                    _mensagens_processadas.add(wamid)
                    if len(_mensagens_processadas) > 1000:
                        _mensagens_processadas.clear()

                    # Normaliza número
                    numero = _normalizar_numero_br(numero)
                    log.info(f"📱 Meta webhook: numero={numero} msg={body[:80]}")

                    # Processa como ticket_id=numero (sem Vectax)
                    await processar_mensagem(
                        ticket_id=numero,
                        contact_id=numero,
                        numero_whatsapp=numero,
                        mensagem=body,
                    )
    except Exception as e:
        log.error(f"Erro processando webhook Meta: {e}")

    return JSONResponse({"ok": True})


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
    # re já importado globalmente
    n = re.sub(r"\D", "", numero)
    if n.startswith("55") and len(n) == 12:
        n = n[:4] + "9" + n[4:]
    return n


def _remover_nono_digito(numero: str) -> str:
    """
    Remove o 9 dígito de todos os números brasileiros de 13 dígitos.
    A Vectax normaliza internamente para 12 dígitos (sem o 9) no canal WABA.
    """
    # re já importado globalmente
    n = re.sub(r"[^0-9]", "", numero)
    # Remove o 9 de qualquer número de 13 dígitos (55 + DDD + 9 + 8)
    if n.startswith("55") and len(n) == 13 and n[4] == "9":
        n = n[:4] + n[5:]
    return n
