import azure.functions as func
import logging
import os
import json
import re
import uuid
import base64
from datetime import datetime, timedelta, timezone
from openai import AzureOpenAI
from azure.data.tables import TableServiceClient
from azure.storage.blob import (
    BlobServiceClient,
    generate_blob_sas,
    BlobSasPermissions,
    ContentSettings,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

# cliente do azure openai
client = AzureOpenAI(
    api_key=os.environ["AZURE_OPENAI_KEY"],
    azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
    api_version="2024-10-21",
)

DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]
STORAGE_CONNECTION = os.environ["STORAGE_CONNECTION"]
TABELA = "conversas"
CONTAINER = "imagens"
DIAS_VALIDADE_SAS = 7

# tabela do historico
tabela_service = TableServiceClient.from_connection_string(STORAGE_CONNECTION)
tabela_service.create_table_if_not_exists(TABELA)
tabela_client = tabela_service.get_table_client(TABELA)

# blob das imagens
blob_service = BlobServiceClient.from_connection_string(STORAGE_CONNECTION)
try:
    blob_service.create_container(CONTAINER)
except Exception:
    pass  # container ja existe


def subir_imagem(data_uri: str) -> str:
    """Recebe a imagem em base64, sobe no blob e devolve uma url assinada."""
    match = re.match(r"data:(image/[\w+]+);base64,(.+)", data_uri, re.DOTALL)
    if not match:
        raise ValueError("formato de imagem invalido")

    content_type = match.group(1)
    dados = base64.b64decode(match.group(2))
    extensao = content_type.split("/")[1]

    nome = f"{uuid.uuid4().hex}.{extensao}"
    blob_client = blob_service.get_blob_client(container=CONTAINER, blob=nome)
    blob_client.upload_blob(
        dados,
        overwrite=True,
        content_settings=ContentSettings(content_type=content_type),
    )

    # url temporaria de leitura, para o modelo conseguir baixar
    sas = generate_blob_sas(
        account_name=blob_service.account_name,
        container_name=CONTAINER,
        blob_name=nome,
        account_key=blob_service.credential.account_key,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.now(timezone.utc) + timedelta(days=DIAS_VALIDADE_SAS),
    )
    return f"{blob_client.url}?{sas}"


def carregar_historico(session_id: str) -> list:
    """Monta o historico no formato que o modelo espera, reincluindo imagens."""
    filtro = f"PartitionKey eq '{session_id}'"
    linhas = tabela_client.query_entities(filtro)
    ordenadas = sorted(linhas, key=lambda x: x["RowKey"])

    mensagens = []
    for m in ordenadas:
        url_imagem = m.get("image_url")
        if url_imagem:
            mensagens.append({
                "role": m["role"],
                "content": [
                    {"type": "text", "text": m["content"]},
                    {"type": "image_url", "image_url": {"url": url_imagem}},
                ],
            })
        else:
            mensagens.append({"role": m["role"], "content": m["content"]})
    return mensagens


def salvar_mensagem(session_id: str, ordem: int, role: str, content: str, url_imagem: str = None):
    entidade = {
        "PartitionKey": session_id,
        "RowKey": f"{ordem:04d}",
        "role": role,
        "content": content[:30000],
    }
    if url_imagem:
        entidade["image_url"] = url_imagem
    tabela_client.create_entity(entidade)


@app.route(route="ask")
def ask(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('recebi uma requisicao no endpoint ask')

    try:
        corpo = req.get_json()
    except ValueError:
        corpo = {}

    pergunta = corpo.get('pergunta')
    imagem = corpo.get('imagem')          # data uri base64, opcional
    session_id = corpo.get('session_id')

    if not pergunta and not imagem:
        return func.HttpResponse(
            json.dumps({"erro": "envie uma pergunta e/ou uma imagem"}),
            status_code=400,
            mimetype="application/json",
        )

    if not pergunta:
        pergunta = "o que e essa imagem?"

    # sobe a imagem no blob e pega a url assinada
    url_imagem = None
    if imagem:
        try:
            url_imagem = subir_imagem(imagem)
            logging.info("imagem salva no blob")
        except Exception as e:
            logging.error(f"erro ao subir a imagem: {e}")
            return func.HttpResponse(
                json.dumps({"erro": "nao consegui processar a imagem"}),
                status_code=400,
                mimetype="application/json",
            )

    historico = carregar_historico(session_id) if session_id else []

    mensagens = [{"role": "system", "content": "voce e um assistente util e responde em portugues."}]
    mensagens.extend(historico)

    if url_imagem:
        conteudo = [
            {"type": "text", "text": pergunta},
            {"type": "image_url", "image_url": {"url": url_imagem}},
        ]
    else:
        conteudo = pergunta

    mensagens.append({"role": "user", "content": conteudo})

    try:
        resultado = client.chat.completions.create(
            model=DEPLOYMENT,
            messages=mensagens,
        )
        resposta = resultado.choices[0].message.content
    except Exception as e:
        logging.error(f"erro ao chamar o modelo: {e}")
        return func.HttpResponse(
            json.dumps({"erro": "falha ao gerar a resposta"}),
            status_code=500,
            mimetype="application/json",
        )

    # guarda a url da imagem no historico, nunca o base64
    if session_id:
        proxima_ordem = len(historico)
        salvar_mensagem(session_id, proxima_ordem, "user", pergunta, url_imagem)
        salvar_mensagem(session_id, proxima_ordem + 1, "assistant", resposta)

    return func.HttpResponse(
        json.dumps({"pergunta": pergunta, "resposta": resposta, "session_id": session_id}, ensure_ascii=False),
        status_code=200,
        mimetype="application/json",
    )