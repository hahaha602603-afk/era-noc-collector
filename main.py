import os
import time
import logging
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

ERA_API_BASE_URL = os.getenv("ERA_API_BASE_URL", "").rstrip("/")
ERA_API_PATH = os.getenv("ERA_API_PATH", "/api/v1").strip("/")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

POLL_INTERVAL_SECONDS = int(
    os.getenv("POLL_INTERVAL_SECONDS", "300")
)

HTTP_TIMEOUT = 20

if not all([
    ERA_API_BASE_URL,
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
]):
    raise RuntimeError(
        "Configure ERA_API_BASE_URL, SUPABASE_URL e "
        "SUPABASE_SERVICE_ROLE_KEY nas variáveis de ambiente."
    )

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
)

session = requests.Session()

# Guarda os tokens temporariamente na memória.
tokens = {}


def api_url(endpoint):
    return (
        f"{ERA_API_BASE_URL}/{ERA_API_PATH}/{endpoint.lstrip('/')}"
    )


def get_token(organization_id):
    """Reutiliza o token até próximo do vencimento."""

    cached = tokens.get(organization_id)
    now = time.time()

    if cached and cached["expires_at"] > now + 30:
        return cached["token"]

    response = session.post(
        api_url("token/generate"),
        json={"organization_id": organization_id},
        timeout=HTTP_TIMEOUT,
    )

    response.raise_for_status()
    result = response.json()

    if not result.get("success") or not result.get("token"):
        raise RuntimeError(
            f"ERA não forneceu token para {organization_id}: "
            f"{result.get('message', 'resposta inválida')}"
        )

    expires_in = int(result.get("expires_in", 300))

    tokens[organization_id] = {
        "token": result["token"],
        "expires_at": now + expires_in,
    }

    return result["token"]


def get_extensions(organization_id, domain):
    """Consulta os ramais. Erros são propagados, nunca tratados como offline."""

    token = get_token(organization_id)

    response = session.get(
        api_url("register/extensionsStatus"),
        params={
            "organization_id": organization_id,
            "organization_name": domain,
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=HTTP_TIMEOUT,
    )

    if response.status_code == 401:
        tokens.pop(organization_id, None)
        raise RuntimeError("Token recusado pela API ERA.")

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "60")
        raise RuntimeError(
            f"Limite da API atingido. Aguarde {retry_after} segundos."
        )

    response.raise_for_status()
    result = response.json()

    if not result.get("success"):
        raise RuntimeError(
            result.get("message", "Falha ao consultar ramais.")
        )

    data = result.get("data")

    if not isinstance(data, list):
        raise RuntimeError("A API retornou uma lista de ramais inválida.")

    return data


def sync_extension(org, extension):
    """Sincroniza cadastro e status de um ramal."""

    org_id = org["id"]
    number = str(extension.get("extension", "")).strip()

    if not number:
        logging.warning("Ramal sem número ignorado.")
        return

    enabled = str(extension.get("enabled", "false")).lower() == "true"
    registered = str(extension.get("register", "false")).lower() == "true"

    details = extension.get("detail") or []
    detail = details[0] if details else {}

    ip = detail.get("network-ip") or None
    port = detail.get("network-port")
    agent = detail.get("agent") or None

    now = datetime.now(timezone.utc).isoformat()

    # Cria o ramal ou atualiza o cadastro existente.
    ramal_result = (
        supabase.table("ramais")
        .upsert(
            {
                "organizacao_id": org_id,
                "numero": number,
                "descricao": extension.get("description") or None,
                "habilitado": enabled,
            },
            on_conflict="organizacao_id,numero",
        )
        .execute()
    )

    if not ramal_result.data:
        raise RuntimeError(
            f"Não foi possível salvar o cadastro do ramal {number}."
        )

    # Busca o ID interno do ramal.
    ramal_result = (
        supabase.table("ramais")
        .select("id")
        .eq("organizacao_id", org_id)
        .eq("numero", number)
        .limit(1)
        .execute()
    )

    if not ramal_result.data:
        raise RuntimeError(f"Ramal {number} não encontrado no Supabase.")

    ramal_id = ramal_result.data[0]["id"]

    # Lê o estado anterior para detectar mudanças.
    previous_result = (
        supabase.table("status_ramais")
        .select("registrado,ip_atual")
        .eq("ramal_id", ramal_id)
        .limit(1)
        .execute()
    )

    previous = (
        previous_result.data[0]
        if previous_result.data
        else None
    )

    previous_registered = (
        previous["registrado"] if previous else None
    )
    previous_ip = previous.get("ip_atual") if previous else None

    status_data = {
        "ramal_id": ramal_id,
        "registrado": registered,
        "ip_atual": ip,
        "porta": int(port) if str(port or "").isdigit() else None,
        "user_agent": agent,
        "ultima_verificacao": now,
        "atualizado_em": now,
    }

    if registered and previous_registered is not True:
        status_data["ultima_conexao"] = now

    if not registered and previous_registered is True:
        status_data["ultima_desconexao"] = now

    # Só atualiza o estado depois de uma consulta bem-sucedida.
    (
        supabase.table("status_ramais")
        .upsert(status_data, on_conflict="ramal_id")
        .execute()
    )

    # Registra mudanças de conexão.
    if previous_registered is not None and previous_registered != registered:
        event_type = "CONECTADO" if registered else "DESCONECTADO"

        (
            supabase.table("eventos_conexao")
            .insert({
                "ramal_id": ramal_id,
                "tipo_evento": event_type,
                "ip_publico": ip,
                "descricao": (
                    "Ramal registrado na ERA Cloud."
                    if registered
                    else "Ramal deixou de estar registrado na ERA Cloud."
                ),
                "ocorrido_em": now,
            })
            .execute()
        )

    # Registra alteração de IP quando o ramal continua conectado.
    if (
        registered
        and previous_registered is True
        and previous_ip
        and ip
        and str(previous_ip) != str(ip)
    ):
        (
            supabase.table("historico_ips")
            .insert({
                "ramal_id": ramal_id,
                "ip_anterior": previous_ip,
                "ip_novo": ip,
                "detectado_em": now,
            })
            .execute()
        )

        (
            supabase.table("eventos_conexao")
            .insert({
                "ramal_id": ramal_id,
                "tipo_evento": "ALTERACAO_IP",
                "ip_publico": ip,
                "descricao": f"IP alterado de {previous_ip} para {ip}.",
                "ocorrido_em": now,
            })
            .execute()
        )


def process_organization(org):
    org_id = org.get("era_org_id")
    domain = org.get("dominio")

    if not org_id or not domain:
        logging.warning(
            "Organização ignorada: identificador ou domínio ausente."
        )
        return

    logging.info("Consultando organização: %s", org.get("nome", org_id))

    # Se a consulta falhar, nenhum ramal é marcado como offline.
    extensions = get_extensions(org_id, domain)

    for extension in extensions:
        sync_extension(org, extension)

    logging.info(
        "Organização %s: %s ramais processados.",
        org.get("nome", org_id),
        len(extensions),
    )


def run_cycle():
    result = (
        supabase.table("organizacoes")
        .select("id,era_org_id,nome,dominio")
        .eq("ativo", True)
        .execute()
    )

    organizations = result.data or []

    logging.info(
        "Iniciando ciclo para %s organizações.",
        len(organizations),
    )

    errors = 0

    for index, org in enumerate(organizations):
        try:
            process_organization(org)
        except Exception as exc:
            errors += 1
            logging.error(
                "Falha na organização %s: %s",
                org.get("nome", org.get("era_org_id")),
                exc,
            )

        # Distribui as chamadas ao longo do ciclo.
        if index < len(organizations) - 1:
            time.sleep(1.5)

    logging.info(
        "Ciclo concluído. Organizações: %s. Falhas: %s.",
        len(organizations),
        errors,
    )


def main():
    logging.info("Coletor NOC ERA Cloud iniciado.")
    logging.info(
        "Intervalo entre ciclos: %s segundos.",
        POLL_INTERVAL_SECONDS,
    )

    while True:
        started = time.time()

        try:
            run_cycle()
        except Exception:
            logging.exception("Falha geral no ciclo de coleta.")

        elapsed = time.time() - started
        wait_time = max(1, POLL_INTERVAL_SECONDS - elapsed)

        time.sleep(wait_time)


if __name__ == "__main__":
    main()
