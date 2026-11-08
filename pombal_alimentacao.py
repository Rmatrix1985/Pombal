#!/usr/bin/env python3
"""Vigia a zona de alimentação pela câmara Reolink e regista cada visita dos pombos.

Corre num telemóvel Android antigo (Termux) ligado à mesma rede da câmara.
Avisa o script Google quando uma visita começa (a app mostra «a comer agora») e quando
acaba; o script guarda cada visita na folha «Pombal - Alimentação».
"""
from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PASTA = Path(__file__).resolve().parent
FICHEIRO_CONFIG = PASTA / "config.json"
FICHEIRO_PENDENTES = PASTA / "eventos-por-enviar.json"

INTERVALO_LEITURA_SEGUNDOS = 3
SEGUNDOS_SEM_MOVIMENTO_PARA_FECHAR = 60   # a visita só acaba depois de 1 min sem movimento
DURACAO_MINIMA_SEGUNDOS = 10              # ignora movimentos curtos (sombras, uma pena a cair)
TEMPO_LIMITE_PEDIDO_SEGUNDOS = 15
PAUSA_DEPOIS_DE_ERRO_SEGUNDOS = 30
MOVIMENTO_DETETADO = 1
MS_POR_SEGUNDO = 1000
# A câmara usa um certificado próprio em HTTPS; só falamos com ela dentro da rede do pombal
CONTEXTO_SSL_CAMARA = ssl._create_unverified_context()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
registo = logging.getLogger("comedouro")


class ErroCamara(Exception):
    """A câmara não respondeu ou respondeu com erro."""


@dataclass(frozen=True)
class Configuracao:
    ip_camara: str
    utilizador: str
    palavra_passe: str
    url_script: str
    chave: str
    protocolo: str = "http"

    @staticmethod
    def carregar(caminho: Path) -> "Configuracao":
        try:
            dados = json.loads(caminho.read_text(encoding="utf-8"))
        except FileNotFoundError as erro:
            raise SystemExit(f"Falta o ficheiro {caminho.name} na pasta do programa.") from erro
        except json.JSONDecodeError as erro:
            raise SystemExit(f"O ficheiro {caminho.name} tem um erro de formato: {erro}") from erro
        try:
            return Configuracao(**dados)
        except TypeError as erro:
            raise SystemExit(f"Faltam campos em {caminho.name}: {erro}") from erro


@dataclass(frozen=True)
class Visita:
    inicio: datetime
    fim: datetime

    @property
    def duracao_segundos(self) -> int:
        return round((self.fim - self.inicio).total_seconds())

    def evento_fim(self) -> dict[str, object]:
        return {"evento": "fim", "inicio": em_ms(self.inicio), "fim": em_ms(self.fim)}


@dataclass
class EstadoVisita:
    """Guarda a visita em curso enquanto há movimento."""
    inicio: datetime | None = None
    ultimo_movimento: datetime | None = None
    inicio_anunciado: bool = False
    pendentes: list[dict[str, object]] = field(default_factory=list)


def em_ms(instante: datetime) -> int:
    return int(instante.timestamp() * MS_POR_SEGUNDO)


def ler_json(pedido: urllib.request.Request | str, contexto: ssl.SSLContext | None = None) -> object:
    with urllib.request.urlopen(pedido, timeout=TEMPO_LIMITE_PEDIDO_SEGUNDOS, context=contexto) as resposta:
        return json.loads(resposta.read().decode("utf-8"))


def ha_movimento(config: Configuracao) -> bool:
    consulta = urllib.parse.urlencode({
        "cmd": "GetMdState", "channel": 0,
        "user": config.utilizador, "password": config.palavra_passe,
    })
    try:
        resposta = ler_json(f"{config.protocolo}://{config.ip_camara}/cgi-bin/api.cgi?{consulta}", CONTEXTO_SSL_CAMARA)
        primeiro = resposta[0]
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, IndexError, KeyError, TypeError) as erro:
        raise ErroCamara(f"sem resposta válida da câmara: {erro}") from erro
    if primeiro.get("code") != 0:
        raise ErroCamara(f"a câmara recusou o pedido: {primeiro.get('error', primeiro)}")
    return primeiro["value"]["state"] == MOVIMENTO_DETETADO


def enviar_evento(config: Configuracao, evento: dict[str, object]) -> bool:
    """Devolve True se o evento ficou tratado (aceite ou recusado); False para tentar mais tarde."""
    corpo = json.dumps({**evento, "chave": config.chave}).encode("utf-8")
    pedido = urllib.request.Request(config.url_script, data=corpo, headers={"Content-Type": "application/json"})
    try:
        resposta = ler_json(pedido)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as erro:
        registo.warning("Não consegui enviar (%s); fica guardado para depois.", erro)
        return False
    if not isinstance(resposta, dict) or not resposta.get("ok"):
        registo.warning("O script recusou o evento: %s", resposta)
    return True


def guardar_pendentes(pendentes: list[dict[str, object]]) -> None:
    FICHEIRO_PENDENTES.write_text(json.dumps(pendentes, ensure_ascii=False), encoding="utf-8")


def carregar_pendentes() -> list[dict[str, object]]:
    try:
        return json.loads(FICHEIRO_PENDENTES.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def enviar_pendentes(config: Configuracao, estado: EstadoVisita) -> None:
    if not estado.pendentes:
        return
    por_enviar = [p for p in estado.pendentes if not enviar_evento(config, p)]
    estado.pendentes = por_enviar
    guardar_pendentes(por_enviar)


def fechar_visita(estado: EstadoVisita) -> dict[str, object] | None:
    visita = Visita(estado.inicio, estado.ultimo_movimento)
    anunciada = estado.inicio_anunciado
    estado.inicio = estado.ultimo_movimento = None
    estado.inicio_anunciado = False
    if not anunciada:
        return None   # movimento curto: nunca chegou a contar como visita
    registo.info("Visita terminada: %s a %s (%d s).", visita.inicio.time(), visita.fim.time(), visita.duracao_segundos)
    return visita.evento_fim()


def anunciar_se_ja_conta(estado: EstadoVisita) -> dict[str, object] | None:
    if estado.inicio_anunciado:
        return None
    if (estado.ultimo_movimento - estado.inicio).total_seconds() < DURACAO_MINIMA_SEGUNDOS:
        return None
    estado.inicio_anunciado = True
    registo.info("Pombos na zona de alimentação desde %s.", estado.inicio.time())
    return {"evento": "inicio", "inicio": em_ms(estado.inicio)}


def atualizar_estado(estado: EstadoVisita, movimento: bool, agora: datetime) -> dict[str, object] | None:
    """Abre, prolonga ou fecha a visita. Devolve o evento a enviar, se houver."""
    if movimento:
        estado.inicio = estado.inicio or agora
        estado.ultimo_movimento = agora
        return anunciar_se_ja_conta(estado)
    if estado.inicio is None:
        return None
    if (agora - estado.ultimo_movimento).total_seconds() < SEGUNDOS_SEM_MOVIMENTO_PARA_FECHAR:
        return None
    return fechar_visita(estado)


def vigiar(config: Configuracao) -> None:
    estado = EstadoVisita(pendentes=carregar_pendentes())
    registo.info("A vigiar a zona de alimentação na câmara %s.", config.ip_camara)
    while True:
        try:
            movimento = ha_movimento(config)
        except ErroCamara as erro:
            registo.warning("%s. Volto a tentar daqui a %d s.", erro, PAUSA_DEPOIS_DE_ERRO_SEGUNDOS)
            time.sleep(PAUSA_DEPOIS_DE_ERRO_SEGUNDOS)
            continue
        evento = atualizar_estado(estado, movimento, datetime.now())
        if evento:
            estado.pendentes.append(evento)
            guardar_pendentes(estado.pendentes)
        enviar_pendentes(config, estado)
        time.sleep(INTERVALO_LEITURA_SEGUNDOS)


if __name__ == "__main__":
    vigiar(Configuracao.carregar(FICHEIRO_CONFIG))
