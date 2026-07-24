import azure.functions as func
import logging
import os
import json
from openai import AzureOpenAI
from azure.data.tables import TableServiceClient

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

# prepara o cliente da tabela e garante que ela existe
tabela_service = TableServiceClient.from_connection_string(STORAGE_CONNECTION)
tabela_service.create_table_if_not_exists(TABELA)
tabela_client = tabela_service.get_table_client(TABELA)


def carregar_historico(session_id: str) -> list:
    # busca todas as mensagens dessa sessao, ordenadas pela ordem em que chegaram
    filtro = f"PartitionKey eq '{session_id}'"
    linhas = tabela_client.query_entities(filtro)
    mensagens = sorted(linhas, key=lambda x: x["RowKey"])
    return [{"role": m["role"], "content": m["content"]} for m in mensagens]


def salvar_mensagem(session_id: str, ordem: int, role: str, content: str):
    # cada mensagem vira uma linha na tabela; o RowKey preserva a ordem
    entidade = {
        "PartitionKey": session_id,
        "RowKey": f"{ordem:04d}",
        "role": role,
        "content": content,
    }
    tabela_client.create_entity(entidade)


@app.route(route="ask")
def ask(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('recebi uma requisicao no endpoint ask')

    try:
        corpo = req.get_json()
    except ValueError:
        corpo = {}

    pergunta = corpo.get('pergunta')
    session_id = corpo.get('session_id')

    # a pergunta continua obrigatoria
    if not pergunta:
        return func.HttpResponse(
            json.dumps({"erro": "envie uma pergunta no corpo json, campo 'pergunta'"}),
            status_code=400,
            mimetype="application/json",
        )

    # se nao veio session_id, funciona como antes: sem memoria
    if not session_id:
        historico = []
    else:
        historico = carregar_historico(session_id)

    # monta as mensagens: system + historico + pergunta atual
    mensagens = [{"role": "system", "content": "voce e um assistente util e responde em portugues."}]
    mensagens.extend(historico)
    mensagens.append({"role": "user", "content": pergunta})

    # chama o modelo
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

    # se tem sessao, salva a pergunta e a resposta pra manter a memoria
    if session_id:
        proxima_ordem = len(historico)
        salvar_mensagem(session_id, proxima_ordem, "user", pergunta)
        salvar_mensagem(session_id, proxima_ordem + 1, "assistant", resposta)

    return func.HttpResponse(
        json.dumps({"pergunta": pergunta, "resposta": resposta, "session_id": session_id}, ensure_ascii=False),
        status_code=200,
        mimetype="application/json",
    )