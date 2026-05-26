"""
Modelos de dados (Pydantic) usados pelo servidor.
"""

from typing import Optional
from pydantic import BaseModel


class WebhookPayload(BaseModel):
    """
    Payload recebido da plataforma de chatbot via webhook.
    Ajuste os campos conforme o formato real da sua plataforma.
    """
    number:     Optional[str] = None       # Número do cliente
    body:       Optional[str] = None       # Texto da mensagem
    ticketId:   Optional[str] = None       # ID do ticket/conversa
    externalKey: Optional[str] = None      # Chave externa
    fromMe:     Optional[bool] = False     # True = mensagem enviada pelo bot
    direction:  Optional[str] = None       # "inbound" ou "outbound"


class ChatbotMessage(BaseModel):
    """
    Payload enviado para a API da plataforma ao responder o cliente.
    Corresponde ao schema Message da documentação.
    """
    body:        str
    number:      str
    externalKey: str
    mediaUrl:    Optional[str] = None
    onlyNote:    Optional[bool] = None
    userId:      Optional[int] = None
    forceTicketToUser: Optional[bool] = None


class Oportunidade(BaseModel):
    """
    Payload para criação de oportunidade no CRM da plataforma.
    """
    name:           str
    description:    Optional[str] = None
    value:          Optional[float] = None
    contactId:      int
    responsibleId:  str
    pipelineStepId: int
    userId:         str
