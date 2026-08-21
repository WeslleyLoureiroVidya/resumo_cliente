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
MAX_TICKETS_TABELA_EMAIL = 5  # Limite por cliente para não estourar 102KB no Gmail

MESES_PT = [
    "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
    "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro",
]


# ---------------------------------------------------------------------------
# Rate Limiter
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
# Período
# ---------------------------------------------------------------------------
def periodo_mes_anterior(referencia):
    primeiro_dia_mes_atual = referencia.replace(day=1)
    ultimo_dia_mes_anterior = primeiro_dia_mes_atual - datetime.timedelta(days=1)
    primeiro_dia_mes_anterior = ultimo_dia_mes_anterior.replace(day=1)
    return primeiro_dia_mes_anterior, ultimo_dia_mes_anterior


def nome_mes_ano(data):
    return f"{MESES_PT[data.month - 1]}/{data.year}"


# ---------------------------------------------------------------------------
# Busca de Tickets Movidesk
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
# Pesquisa de Satisfação
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
# Formatação e Auxiliares
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
            html_saida.append("<hr style='border:0; border-top:1px solid #ebdcf0; margin: 12px 0;'>")

        elif linha_limpa:
            if em_lista:
                html_saida.append("</ul>")
                em_lista = False
            texto = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', linha_limpa)
            html_saida.append(f"<p>{texto}</p>")

    if em_lista:
        html_saida.append("</ul>")

    return "\n".join(html_saida)


def gerar_conteudo_com_retry(client, model, prompt, max_tentativas=5, espera_inicial=10):
    """Gera conteúdo via sessão de Chat para evitar o aviso de AFC da SDK do Gemini."""
    espera = espera_inicial
    for tentativa in range(1, max_tentativas + 1):
        try:
            chat = client.chats.create(model=model)
            return chat.send_message(prompt)
        except genai_errors.ServerError:
            if tentativa == max_tentativas:
                raise
            time.sleep(espera)
            espera *= 2
        except genai_errors.ClientError:
            raise


# ---------------------------------------------------------------------------
# Montagem da Seção do Cliente
# ---------------------------------------------------------------------------
def montar_secao_cliente(idx, nome_cliente, tickets, gemini_client, movidesk_token, rate_limiter, data_inicio, data_fim):
    print(f"Processando cliente [{idx}] '{nome_cliente}' ({len(tickets)} ticket(s))...")

    total_tickets = len(tickets)
    resolvidos = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() in ["solved", "closed"])
    em_andamento = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() in ["new", "inattendance", "reopened"])
    parados = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() == "stopped")

    # Ordena para priorizar tickets parados/abertos na amostragem da tabela
    tickets_priorizados = sorted(
        tickets,
        key=lambda x: 0 if (x.get("baseStatus") or "").lower() == "stopped" else (1 if (x.get("baseStatus") or "").lower() in ["new", "inattendance"] else 2)
    )

    linhas_detalhe = []
    dados_para_ia = []

    for t in tickets:
        dados_para_ia.append({
            "protocolo": t.get("protocol") or t.get("id"),
            "descricao": t.get("subject"),
            "solicitante": nome_solicitante(t),
            "urgencia": t.get("urgency"),
            "status": t.get("status"),
            "data_abertura": t.get("createdDate"),
            "data_fechamento": t.get("closedIn") or t.get("resolvedIn"),
            "responsavel": nome_responsavel(t),
        })

    # Busca nota de satisfação para a amostragem exibida no e-mail
    for t in tickets_priorizados[:MAX_TICKETS_TABELA_EMAIL]:
        t_id = t.get("id")
        nota, comentario = buscar_nota_satisfacao(movidesk_token, t_id, rate_limiter)
        linhas_detalhe.append((t, nota, comentario))

    prompt = f"""
Você é um especialista em Sucesso do Cliente e Relacionamento.
Abaixo estão os tickets abertos pela organização '{nome_cliente}' no período de {data_inicio.strftime('%d/%m/%Y')} até {data_fim.strftime('%d/%m/%Y')}.

Dados dos tickets (JSON):
{dados_para_ia}

Elabore uma análise executiva estruturada e objetiva, contendo exatamente estas 3 seções com títulos em markdown (###):
### 1. Principais Dores e Dificuldades
### 2. Problemas Técnicos Recorrentes
### 3. Sugestões de Atuação

Use listas com marcadores (*) para os pontos de cada seção. Seja direto e resumido.
"""

    print(f"  Gerando análise com Gemini para '{nome_cliente}'...")
    response = gerar_conteudo_com_retry(client=gemini_client, model="gemini-2.5-flash", prompt=prompt)
    analise_ia_html = markdown_para_html(response.text)

    linhas_tabela = ""
    for t, nota, comentario in linhas_detalhe:
        t_id = html.escape(str(t.get("protocol") or t.get("id")))
        descricao = html.escape(str(t.get("subject") or "-"))
        urgencia = html.escape(str(t.get("urgency") or "-"))
        status = html.escape(str(t.get("status") or "-"))
        st_class = status_class(t.get("baseStatus"))
        data_abertura = html.escape(formatar_data_br(t.get("createdDate")))
        responsavel = html.escape(nome_responsavel(t))

        linhas_tabela += f"""
        <tr>
            <td style="font-weight: bold; color: #374151;">#{t_id}</td>
            <td>{descricao}</td>
            <td>{urgencia}</td>
            <td><span class="status {st_class}">{status}</span></td>
            <td>{data_abertura}</td>
            <td>{responsavel}</td>
        </tr>
        """

    secao_html = f"""
    <div id="cliente-{idx}" class="client-section">
        <div class="client-header">
            <h2 class="client-title">{idx}. {html.escape(nome_cliente)}</h2>
        </div>
        <div class="cards">
            <div class="card">
                <div class="card-label">Total</div>
                <div class="card-value">{total_tickets}</div>
            </div>
            <div class="card">
                <div class="card-label">Resolvidos</div>
                <div class="card-value" style="color: #18794e;">{resolvidos}</div>
            </div>
            <div class="card">
                <div class="card-label">Andamento</div>
                <div class="card-value" style="color: #2457a6;">{em_andamento}</div>
            </div>
            <div class="card">
                <div class="card-label">Parados</div>
                <div class="card-value" style="color: #a15c00;">{parados}</div>
            </div>
        </div>

        <div class="section-title">🔎 Insights IA</div>
        <div class="ai-box">
            {analise_ia_html}
        </div>

        <div class="section-title">🎫 Amostragem de Tickets Pendentes / Recentes</div>
        <div class="table-wrapper">
            <table>
                <thead>
                    <tr>
                        <th>Protocolo</th>
                        <th>Descrição</th>
                        <th>Urgência</th>
                        <th>Status</th>
                        <th>Aberto em</th>
                        <th>Responsável</th>
                    </tr>
                </thead>
                <tbody>
                    {linhas_tabela}
                </tbody>
            </table>
        </div>
    </div>
    """
    return secao_html, (nome_cliente, total_tickets, resolvidos, em_andamento, parados)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    hoje = datetime.date.today()
    data_inicio, data_fim = periodo_mes_anterior(hoje)

    movidesk_token = os.environ.get("MOVIDESK_TOKEN")
    if not movidesk_token:
        raise ValueError("A variável MOVIDESK_TOKEN não foi configurada.")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("A variável GEMINI_API_KEY não foi configurada.")
    gemini_client = genai.Client(api_key=api_key)

    rate_limiter = RateLimiter(intervalo_minimo_segundos=0.7)

    print(f"Buscando tickets entre {data_inicio.strftime('%d/%m/%Y')} e {data_fim.strftime('%d/%m/%Y')}...")
    todos_tickets = buscar_todos_tickets_periodo(movidesk_token, data_inicio, data_fim, rate_limiter)
    print(f"{len(todos_tickets)} ticket(s) encontrado(s).")

    if not todos_tickets:
        print("Nenhum ticket encontrado. Encerrando.")
        return

    grupos = agrupar_por_organizacao(todos_tickets)
    top_organizacoes = selecionar_top_organizacoes(grupos, TOP_N_CLIENTES)

    secoes_html = ""
    resumo_clientes = []

    for idx, (nome_cliente, tickets_cliente) in enumerate(top_organizacoes, 1):
        html_sec, métricas = montar_secao_cliente(
            idx, nome_cliente, tickets_cliente, gemini_client, movidesk_token, rate_limiter,
            data_inicio, data_fim
        )
        secoes_html += html_sec
        resumo_clientes.append(métricas)

    # Métricas Globais dos 5 clientes
    total_g = sum(m[1] for m in resumo_clientes)
    resolvidos_g = sum(m[2] for m in resumo_clientes)
    andamento_g = sum(m[3] for m in resumo_clientes)
    parados_g = sum(m[4] for m in resumo_clientes)

    # Links de navegação âncora
     links_ancora = " &bull; ".join([f'<a href="#cliente-{i+1}" style="color:#6b2d70; text-decoration:none;">[{i+1}. {html.escape(m[0])}]</a>' for i, m in enumerate(resumo_clientes)])

    # Linhas da Tabela Comparativa de Alto Nível
    linhas_resumo_topo = ""
    for i, m in enumerate(resumo_clientes, 1):
        status_tag = '<span style="color:#137333; font-weight:bold;">🟢 Estável</span>' if m[4] == 0 else f'<span style="color:#b06000; font-weight:bold;">⚠️ {m[4]} Parados</span>'
        linhas_resumo_topo += f"""
        <tr>
            <td><a href="#cliente-{i}" style="color:#321635; font-weight:bold; text-decoration:none;">{i}. {html.escape(m[0])}</a></td>
            <td>{m[1]}</td>
            <td><span class="status status-success">{m[2]}</span></td>
            <td><span class="status status-info">{m[3]}</span></td>
            <td><span class="status status-warning">{m[4]}</span></td>
            <td>{status_tag}</td>
        </tr>
        """

    periodo_titulo = nome_mes_ano(data_inicio)

    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
    <meta charset="UTF-8">
    <style>
        body {{ margin: 0; padding: 0; background-color: #f4f6f8; font-family: Arial, sans-serif; color: #202124; }}
        .wrapper {{ width: 100%; padding: 20px 0; }}
        .container {{ max-width: 850px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; border: 1px solid #e0d8e8; }}
        .header {{ background-color: #321635; padding: 25px; text-align: center; color: #ffffff; }}
        .logo {{ font-size: 11px; font-weight: bold; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 6px; color: #d8c3e0; }}
        .title {{ margin: 0; font-size: 20px; font-weight: bold; }}
        .subtitle {{ margin: 6px 0 0; font-size: 12px; color: #e5d5ec; }}
        
        /* Master Digest Topo */
        .global-kpi {{ background: #f3edf7; padding: 15px; border-bottom: 1px solid #e0d8e8; text-align: center; font-size: 12px; }}
        .nav-bar {{ background: #faf8fc; padding: 8px 15px; border-bottom: 1px solid #eee; font-size: 11px; text-align: center; color: #6b2d70; }}
        
        .content {{ padding: 25px; }}
        .client-section {{ margin-bottom: 35px; padding-bottom: 25px; border-bottom: 2px solid #f3f4f6; }}
        .client-section:last-child {{ border-bottom: none; margin-bottom: 0; padding-bottom: 0; }}
        .client-title {{ font-size: 17px; font-weight: bold; color: #321635; margin: 0; padding: 8px 12px; background: #faf5fb; border-left: 4px solid #6b2d70; border-radius: 4px; }}
        
        .cards {{ display: flex; gap: 10px; margin: 15px 0; justify-content: space-between; }}
        .card {{ flex: 1; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 6px; padding: 10px; text-align: center; }}
        .card-label {{ font-size: 10px; font-weight: bold; color: #6b7280; text-transform: uppercase; margin-bottom: 4px; }}
        .card-value {{ font-size: 18px; font-weight: bold; color: #111827; }}
        .section-title {{ font-size: 13px; font-weight: bold; color: #321635; margin: 15px 0 8px; }}

        .ai-box {{ background: #faf5fb; border: 1px solid #f3e8f5; border-left: 3px solid #6b2d70; padding: 12px; border-radius: 4px; font-size: 12px; line-height: 1.5; color: #374151; margin-bottom: 15px; }}
        .ai-box h3 {{ font-size: 12px; color: #321635; margin-top: 10px; margin-bottom: 4px; border-bottom: 1px solid #ebdcf0; padding-bottom: 2px; }}
        .ai-box h3:first-child {{ margin-top: 0; }}
        .ai-box ul {{ margin: 0 0 8px 0; padding-left: 16px; }}
        .ai-box li {{ margin-bottom: 4px; }}

        .table-wrapper {{ width: 100%; overflow-x: auto; border: 1px solid #e5e7eb; border-radius: 6px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 11px; text-align: left; }}
        th {{ background: #f8fafc; color: #6b7280; font-weight: bold; padding: 8px; border-bottom: 1px solid #e5e7eb; white-space: nowrap; }}
        td {{ padding: 8px; border-bottom: 1px solid #f0f1f3; color: #374151; }}
        
        .status {{ display: inline-block; padding: 2px 6px; border-radius: 10px; font-size: 9.5px; font-weight: bold; white-space: nowrap; }}
        .status-success {{ background: #e9f7ef; color: #18794e; }}
        .status-danger {{ background: #fdecec; color: #b42318; }}
        .status-warning {{ background: #fff6df; color: #a15c00; }}
        .status-info {{ background: #edf4ff; color: #2457a6; }}
        
        .footer {{ padding: 15px; text-align: center; font-size: 10.5px; color: #9ca3af; background: #fafafa; border-top: 1px solid #e5e7eb; }}
    </style>
    </head>
    <body>
    <div class="wrapper">
        <div class="container">
            <div class="header">
                <div class="logo">VIDYA CODE</div>
                <h1 class="title">Relatório Executivo Consolidado</h1>
                <p class="subtitle">Período: {periodo_titulo} ({data_inicio.strftime('%d/%m/%Y')} até {data_fim.strftime('%d/%m/%Y')}) &bull; Top 5 Clientes</p>
            </div>
            
            <!-- VISÃO GERAL ACUMULADA -->
            <div class="global-kpi">
                <strong>📊 VISÃO GERAL DOS 5 CLIENTES:</strong> 
                &nbsp;|&nbsp; <strong>Total:</strong> {total_g}
                &nbsp;|&nbsp; <span style="color:#18794e;"><strong>Resolvidos:</strong> {resolvidos_g}</span>
                &nbsp;|&nbsp; <span style="color:#2457a6;"><strong>Andamento:</strong> {andamento_g}</span>
                &nbsp;|&nbsp; <span style="color:#a15c00;"><strong>Parados:</strong> {parados_g}</span>
            </div>

            <!-- MENU NAVEGAÇÃO -->
            <div class="nav-bar">
                <strong>Navegação Rápida:</strong> {links_ancora}
            </div>

            <div class="content">
                <!-- TABELA COMPARATIVA DE TOPO -->
                <div class="section-title" style="margin-top:0;">📋 Resumo Comparativo de Saúde</div>
                <div class="table-wrapper" style="margin-bottom:25px;">
                    <table>
                        <thead>
                            <tr>
                                <th>Cliente</th>
                                <th>Total</th>
                                <th>Resolvidos</th>
                                <th>Andamento</th>
                                <th>Parados</th>
                                <th>Status Rápido</th>
                            </tr>
                        </thead>
                        <tbody>
                            {linhas_resumo_topo}
                        </tbody>
                    </table>
                </div>

                {secoes_html}
            </div>

            <div class="footer">
                Automação integrada &bull; Movidesk & Gemini &bull; Vidya Code
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
    msg["Subject"] = f"Resumo Executivo Consolidado - Top 5 Clientes ({periodo_titulo})"
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
