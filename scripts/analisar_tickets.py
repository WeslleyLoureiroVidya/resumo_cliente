import os
import time
import datetime
import calendar
import html
import smtplib
import re
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
from google.genai import errors as genai_errors

MOVIDESK_BASE_URL = "https://api.movidesk.com/public/v1/tickets"
MOVIDESK_SURVEY_URL = "https://api.movidesk.com/public/v1/survey/responses"

TOP_N_CLIENTES = 5

MESES_PT = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]


# ---------------------------------------------------------------------------
# Utilidades de rate limit para a API do Movidesk
# ---------------------------------------------------------------------------
class RateLimiter:
    """Limita a taxa de requisições para evitar bloqueios (429) da API Movidesk."""

    def __init__(self, intervalo_minimo_segundos=0.7):
        self.intervalo_minimo = intervalo_minimo_segundos
        self._ultima_chamada = 0.0

    def aguardar(self):
        agora = time.monotonic()
        espera = self.intervalo_minimo - (agora - self._ultima_chamada)
        if espera > 0:
            time.sleep(espera)
        self._ultima_chamada = time.monotonic()


def requisitar_com_retry(url, params, rate_limiter, max_tentativas=5):
    """GET com respeito ao rate limit e novas tentativas em caso de 429/erro temporário."""
    for tentativa in range(1, max_tentativas + 1):
        rate_limiter.aguardar()
        resposta = requests.get(url, params=params, timeout=30)

        if resposta.status_code == 429:
            espera = int(resposta.headers.get("retry-after", 10))
            print(f"  Rate limit atingido, aguardando {espera}s...")
            time.sleep(espera)
            continue

        resposta.raise_for_status()
        return resposta

    resposta.raise_for_status()
    return resposta


# ---------------------------------------------------------------------------
# Período: mês anterior ao dia em que o script roda
# ---------------------------------------------------------------------------
def periodo_mes_anterior(referencia):
    primeiro_dia_mes_atual = referencia.replace(day=1)
    ultimo_dia_mes_anterior = primeiro_dia_mes_atual - datetime.timedelta(days=1)
    primeiro_dia_mes_anterior = ultimo_dia_mes_anterior.replace(day=1)
    return primeiro_dia_mes_anterior, ultimo_dia_mes_anterior


def nome_mes_ano(data):
    return f"{MESES_PT[data.month - 1]}/{data.year}"


# ---------------------------------------------------------------------------
# Busca de tickets no Movidesk (todas as organizações, com paginação)
# ---------------------------------------------------------------------------
def buscar_todos_tickets_periodo(token, data_inicio, data_fim, rate_limiter, pagina=500):
    query_start = data_inicio.strftime("%Y-%m-%d")
    query_end = data_fim.strftime("%Y-%m-%d")

    filtro = f"createdDate ge {query_start}T00:00:00.00z and createdDate le {query_end}T23:59:59.00z"

    select_campos = (
        "id,protocol,subject,category,urgency,status,baseStatus,"
        "createdDate,resolvedIn,closedIn"
    )

    todos_tickets = []
    skip = 0

    while True:
        params = {
            "token": token,
            "$select": select_campos,
            "$expand": "clients($expand=organization),owner",
            "$filter": filtro,
            "$orderby": "createdDate desc",
            "$top": pagina,
            "$skip": skip,
        }

        resposta = requisitar_com_retry(MOVIDESK_BASE_URL, params, rate_limiter)
        lote = resposta.json()

        if not isinstance(lote, list) or not lote:
            break

        todos_tickets.extend(lote)

        if len(lote) < pagina:
            break

        skip += pagina

    return todos_tickets


def nome_organizacao(ticket):
    clientes = ticket.get("clients", [])
    if isinstance(clientes, list) and clientes:
        primeiro_cliente = clientes[0]
        org = primeiro_cliente.get("organization")
        if isinstance(org, dict):
            nome = org.get("businessName") or org.get("name")
            if nome:
                return nome.strip()
        nome_cliente = primeiro_cliente.get("businessName")
        if nome_cliente:
            return nome_cliente.strip()
    return "Sem organização identificada"


def nome_solicitante(ticket):
    clientes = ticket.get("clients", [])
    if isinstance(clientes, list) and clientes:
        return clientes[0].get("businessName", "-")
    return "-"


def nome_responsavel(ticket):
    owner = ticket.get("owner")
    if isinstance(owner, dict):
        return owner.get("businessName") or "-"
    return "-"


def agrupar_por_organizacao(tickets):
    grupos = {}
    for t in tickets:
        org = nome_organizacao(t)
        grupos.setdefault(org, []).append(t)
    return grupos


def selecionar_top_organizacoes(grupos, quantidade):
    ordenado = sorted(grupos.items(), key=lambda item: len(item[1]), reverse=True)
    return ordenado[:quantidade]


# ---------------------------------------------------------------------------
# Pesquisa de satisfação (nota + comentário) — API dedicada, por ticket
# ---------------------------------------------------------------------------
def formatar_nota(tipo, valor):
    if valor is None:
        return "-"
    if tipo == 1:
        return "Positivo" if valor == 1 else "Negativo" if valor == 2 else str(valor)
    if tipo == 2:
        mapa = {1: "Muito insatisfeito", 2: "Insatisfeito", 3: "Neutro", 4: "Satisfeito", 5: "Muito satisfeito"}
        return mapa.get(valor, str(valor))
    if tipo == 3:
        return f"{valor}/10"
    return str(valor)


def buscar_nota_satisfacao(token, ticket_id, rate_limiter):
    params = {"token": token, "ticketId": ticket_id}
    try:
        resposta = requisitar_com_retry(MOVIDESK_SURVEY_URL, params, rate_limiter)
        dados = resposta.json()
    except requests.exceptions.RequestException:
        return "-", "-"

    itens = dados.get("items") if isinstance(dados, dict) else None
    if not itens:
        return "-", "-"

    primeira_resposta = itens[0]
    nota = formatar_nota(primeira_resposta.get("type"), primeira_resposta.get("value"))
    comentario = primeira_resposta.get("commentary") or "-"
    return nota, comentario


# ---------------------------------------------------------------------------
# Formatação
# ---------------------------------------------------------------------------
def formatar_data_br(data_iso):
    if not data_iso:
        return "-"
    try:
        return datetime.datetime.fromisoformat(data_iso.replace("Z", "").split(".")[0]).strftime("%d/%m/%Y %H:%M")
    except (ValueError, AttributeError):
        return data_iso or "-"


def status_class(base_status):
    val = (base_status or "").lower()
    if "solved" in val or "closed" in val:
        return "status-success"
    if "canceled" in val:
        return "status-danger"
    if "stopped" in val:
        return "status-warning"
    return "status-info"


def markdown_para_html(texto_md):
    """Converte o texto markdown da IA em HTML limpo e estilizado sem precisar de bibliotecas externas."""
    linhas = texto_md.split("\n")
    html_saida = []
    em_lista = False

    for linha in linhas:
        linha_limpa = linha.strip()

        if linha_limpa.startswith("### "):
            if em_lista:
                html_saida.append("</ul>")
                em_lista = False
            titulo = linha_limpa.replace("### ", "")
            html_saida.append(f"<h3>{titulo}</h3>")

        elif linha_limpa.startswith("* ") or linha_limpa.startswith("- "):
            if not em_lista:
                html_saida.append("<ul>")
                em_lista = True
            conteudo = linha_limpa[2:].strip()
            conteudo = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', conteudo)
            html_saida.append(f"<li>{conteudo}</li>")

        elif linha_limpa == "---":
            if em_lista:
                html_saida.append("</ul>")
                em_lista = False
            html_saida.append("<hr style='border:0; border-top:1px solid #ebdcf0; margin: 15px 0;'>")

        elif linha_limpa:
            if em_lista:
                html_saida.append("</ul>")
                em_lista = False
            texto = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', linha_limpa)
            html_saida.append(f"<p>{texto}</p>")

    if em_lista:
        html_saida.append("</ul>")

    return "\n".join(html_saida)


def gerar_conteudo_com_retry(client, model, contents, max_tentativas=5, espera_inicial=10):
    espera = espera_inicial
    for tentativa in range(1, max_tentativas + 1):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except genai_errors.ServerError:
            if tentativa == max_tentativas:
                raise
            time.sleep(espera)
            espera *= 2
        except genai_errors.ClientError:
            raise


# ---------------------------------------------------------------------------
# Montagem da seção HTML de um cliente
# ---------------------------------------------------------------------------
def montar_secao_cliente(nome_cliente, tickets, gemini_client, movidesk_token, rate_limiter,
                          data_inicio, data_fim):
    print(f"Processando cliente '{nome_cliente}' ({len(tickets)} ticket(s))...")

    total_tickets = len(tickets)
    resolvidos = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() in ["solved", "closed"])
    em_andamento = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() in ["new", "inattendance", "reopened"])
    parados = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() == "stopped")

    # Busca nota + comentário de satisfação para cada ticket (respeita rate limit)
    linhas_detalhe = []
    dados_para_ia = []
    for t in tickets:
        t_id = t.get("id")
        nota, comentario = buscar_nota_satisfacao(movidesk_token, t_id, rate_limiter)
        linhas_detalhe.append((t, nota, comentario))
        dados_para_ia.append({
            "protocolo": t.get("protocol") or t_id,
            "descricao": t.get("subject"),
            "solicitante": nome_solicitante(t),
            "urgencia": t.get("urgency"),
            "status": t.get("status"),
            "data_abertura": t.get("createdDate"),
            "data_fechamento": t.get("closedIn") or t.get("resolvedIn"),
            "responsavel": nome_responsavel(t),
            "nota_satisfacao": nota,
            "comentario_nota": comentario,
        })

    prompt = f"""
Você é um especialista em Sucesso do Cliente e Relacionamento.
Abaixo estão os tickets abertos pela organização '{nome_cliente}' no período de {data_inicio.strftime('%d/%m/%Y')} até {data_fim.strftime('%d/%m/%Y')}.

Dados dos tickets (JSON):
{dados_para_ia}

Elabore uma análise executiva estruturada e objetiva, contendo exatamente estas 3 seções com títulos em markdown (###):
### 1. Principais Dores e Dificuldades
### 2. Problemas Técnicos Recorrentes
### 3. Sugestões de Atuação

Use listas com marcadores (*) para os pontos de cada seção. Considere a nota e o comentário da pesquisa de satisfação como sinais relevantes quando existirem. Seja direto e profissional.
"""

    print(f"  Gerando análise executiva com o Gemini para '{nome_cliente}'...")
    response = gerar_conteudo_com_retry(client=gemini_client, model="gemini-3.6-flash", contents=prompt)
    analise_ia_html = markdown_para_html(response.text)

    linhas_tabela = ""
    for t, nota, comentario in linhas_detalhe:
        t_id = html.escape(str(t.get("protocol") or t.get("id")))
        descricao = html.escape(str(t.get("subject") or "-"))
        solicitante = html.escape(nome_solicitante(t))
        urgencia = html.escape(str(t.get("urgency") or "-"))
        status = html.escape(str(t.get("status") or "-"))
        st_class = status_class(t.get("baseStatus"))
        data_abertura = html.escape(formatar_data_br(t.get("createdDate")))
        data_fechamento = html.escape(formatar_data_br(t.get("closedIn") or t.get("resolvedIn")))
        responsavel = html.escape(nome_responsavel(t))
        nota_fmt = html.escape(str(nota))
        comentario_fmt = html.escape(str(comentario))

        linhas_tabela += f"""
        <tr>
            <td style="font-weight: bold; color: #374151;">#{t_id}</td>
            <td>{descricao}</td>
            <td>{solicitante}</td>
            <td>{urgencia}</td>
            <td><span class="status {st_class}">{status}</span></td>
            <td>{data_abertura}</td>
            <td>{data_fechamento}</td>
            <td>{responsavel}</td>
            <td>{nota_fmt}</td>
            <td>{comentario_fmt}</td>
        </tr>
        """

    secao_html = f"""
    <div class="client-section">
        <div class="client-header">
            <h2 class="client-title">{html.escape(nome_cliente)}</h2>
        </div>
        <div class="cards">
            <div class="card">
                <div class="card-label">Total de Tickets</div>
                <div class="card-value">{total_tickets}</div>
            </div>
            <div class="card">
                <div class="card-label">Resolvidos / Fechados</div>
                <div class="card-value" style="color: #18794e;">{resolvidos}</div>
            </div>
            <div class="card">
                <div class="card-label">Em Andamento</div>
                <div class="card-value" style="color: #2457a6;">{em_andamento}</div>
            </div>
            <div class="card">
                <div class="card-label">Parados</div>
                <div class="card-value" style="color: #a15c00;">{parados}</div>
            </div>
        </div>

        <div class="section-title">🔎 Análise Executiva & Insights (IA)</div>
        <div class="ai-box">
            {analise_ia_html}
        </div>

        <div class="section-title">🎫 Detalhamento dos Tickets</div>
        <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>Protocolo</th>
                        <th>Descrição</th>
                        <th>Solicitante</th>
                        <th>Urgência</th>
                        <th>Status</th>
                        <th>Aberto em</th>
                        <th>Fechado em</th>
                        <th>Responsável</th>
                        <th>Nota</th>
                        <th>Comentário da nota</th>
                    </tr>
                </thead>
                <tbody>
                    {linhas_tabela}
                </tbody>
            </table>
        </div>
    </div>
    """
    return secao_html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    hoje = datetime.date.today()
    data_inicio, data_fim = periodo_mes_anterior(hoje)

    movidesk_token = os.environ.get("MOVIDESK_TOKEN")
    if not movidesk_token:
        raise ValueError("A variável de ambiente MOVIDESK_TOKEN não foi configurada.")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("A variável de ambiente GEMINI_API_KEY não foi configurada.")
    gemini_client = genai.Client(api_key=api_key)

    rate_limiter = RateLimiter(intervalo_minimo_segundos=0.7)

    print(f"Buscando todos os tickets abertos entre {data_inicio.strftime('%d/%m/%Y')} e {data_fim.strftime('%d/%m/%Y')}...")
    todos_tickets = buscar_todos_tickets_periodo(movidesk_token, data_inicio, data_fim, rate_limiter)
    print(f"{len(todos_tickets)} ticket(s) encontrado(s) no período.")

    if not todos_tickets:
        print("Nenhum ticket encontrado no período. Encerrando sem enviar e-mail.")
        return

    grupos = agrupar_por_organizacao(todos_tickets)
    top_organizacoes = selecionar_top_organizacoes(grupos, TOP_N_CLIENTES)

    print(f"Top {len(top_organizacoes)} clientes por volume de tickets:")
    for nome, tickets in top_organizacoes:
        print(f"  - {nome}: {len(tickets)} ticket(s)")

    secoes_html = ""
    for nome_cliente, tickets_cliente in top_organizacoes:
        secoes_html += montar_secao_cliente(
            nome_cliente, tickets_cliente, gemini_client, movidesk_token, rate_limiter,
            data_inicio, data_fim,
        )

    periodo_titulo = nome_mes_ano(data_inicio)

    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
    <meta charset="UTF-8">
    <style>
        body {{ margin: 0; padding: 0; background-color: #f4f6f8; font-family: Arial, sans-serif; color: #202124; }}
        .wrapper {{ width: 100%; padding: 30px 0; }}
        .container {{ max-width: 900px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }}
        .header {{ background-color: #3b1443; padding: 30px; text-align: center; color: #ffffff; }}
        .logo {{ font-size: 14px; font-weight: bold; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 10px; opacity: 0.9; }}
        .title {{ margin: 0; font-size: 22px; font-weight: bold; }}
        .subtitle {{ margin: 8px 0 0; font-size: 13px; opacity: 0.8; }}
        .content {{ padding: 30px; }}
        .client-section {{ margin-bottom: 45px; padding-bottom: 30px; border-bottom: 2px solid #f3f4f6; }}
        .client-section:last-child {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}
        .client-header {{ margin-bottom: 15px; }}
        .client-title {{ font-size: 20px; font-weight: bold; color: #3b1443; margin: 0; padding: 10px 14px; background: #faf5fb; border-left: 5px solid #3b1443; border-radius: 4px; }}
        .cards {{ display: flex; gap: 15px; margin-bottom: 25px; justify-content: space-between; }}
        .card {{ flex: 1; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px; padding: 15px; text-align: center; }}
        .card-label {{ font-size: 11px; font-weight: bold; color: #6b7280; text-transform: uppercase; margin-bottom: 5px; }}
        .card-value {{ font-size: 22px; font-weight: bold; color: #111827; }}
        .section-title {{ font-size: 15px; font-weight: bold; color: #3b1443; margin: 20px 0 10px; }}

        .ai-box {{ background: #faf5fb; border: 1px solid #f3e8f5; border-left: 4px solid #3b1443; padding: 20px; border-radius: 6px; font-size: 13px; line-height: 1.6; color: #374151; margin-bottom: 25px; }}
        .ai-box h3 {{ font-size: 14px; color: #3b1443; margin-top: 18px; margin-bottom: 8px; border-bottom: 1px solid #ebdcf0; padding-bottom: 4px; }}
        .ai-box h3:first-child {{ margin-top: 0; }}
        .ai-box ul {{ margin: 0 0 10px 0; padding-left: 20px; }}
        .ai-box li {{ margin-bottom: 6px; }}
        .ai-box strong {{ color: #1f2937; }}

        .table-wrapper {{ width: 100%; overflow-x: auto; border: 1px solid #e5e7eb; border-radius: 8px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 11.5px; text-align: left; }}
        th {{ background: #f8fafc; color: #6b7280; font-weight: bold; padding: 10px 8px; border-bottom: 1px solid #e5e7eb; white-space: nowrap; }}
        td {{ padding: 10px 8px; border-bottom: 1px solid #f0f1f3; color: #374151; }}
        .status {{ display: inline-block; padding: 4px 8px; border-radius: 12px; font-size: 10px; font-weight: bold; white-space: nowrap; }}
        .status-success {{ background: #e9f7ef; color: #18794e; }}
        .status-danger {{ background: #fdecec; color: #b42318; }}
        .status-warning {{ background: #fff6df; color: #a15c00; }}
        .status-info {{ background: #edf4ff; color: #2457a6; }}
        .footer {{ padding: 20px; text-align: center; font-size: 11px; color: #9ca3af; background: #fafafa; border-top: 1px solid #e5e7eb; }}
    </style>
    </head>
    <body>
    <div class="wrapper">
        <div class="container">
            <div class="header">
                <div class="logo">VIDYA CODE</div>
                <h1 class="title">Top {len(top_organizacoes)} Clientes com Mais Tickets</h1>
                <p class="subtitle">Período: {periodo_titulo} ({data_inicio.strftime('%d/%m/%Y')} até {data_fim.strftime('%d/%m/%Y')})</p>
            </div>
            <div class="content">
                {secoes_html}
            </div>
            <div class="footer">
                Automação integrada • Movidesk & Gemini • Vidya Code
            </div>
        </div>
    </div>
    </body>
    </html>
    """

    email_user = os.environ.get("EMAIL_USER")
    email_password = os.environ.get("EMAIL_PASSWORD")
    email_to = os.environ.get("EMAIL_TO")

    if not all([email_user, email_password, email_to]):
        raise ValueError("Variáveis de e-mail não configuradas nos Secrets.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Resumo Executivo - Top {len(top_organizacoes)} Clientes ({periodo_titulo})"
    msg["From"] = email_user
    msg["To"] = email_to

    msg.attach(MIMEText(html_content, "html", "utf-8"))

    destinatarios = [e.strip() for e in email_to.split(",") if e.strip()]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(email_user, email_password)
        smtp.sendmail(email_user, destinatarios, msg.as_string())

    print(f"E-mail HTML enviado com sucesso para: {email_to}")


if __name__ == "__main__":
    main()
