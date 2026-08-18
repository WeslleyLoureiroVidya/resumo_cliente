import os
import time
import datetime
import html
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
from google.genai import errors as genai_errors
import markdown  # Biblioteca padrão para converter markdown em html formatado

MOVIDESK_BASE_URL = "https://api.movidesk.com/public/v1/tickets"


def buscar_tickets_movidesk_por_organizacao(cliente_organizacao, token, data_inicio, data_fim):
    query_start = data_inicio.strftime("%Y-%m-%d")
    query_end = data_fim.strftime("%Y-%m-%d")

    filtro = f"createdDate ge {query_start}T00:00:00.00z and createdDate le {query_end}T23:59:59.00z"

    params = {
        "token": token,
        "$select": "id,protocol,subject,category,urgency,status,baseStatus,createdDate,clients",
        "$expand": "clients($expand=organization)",
        "$filter": filtro,
        "$orderby": "createdDate desc",
    }

    resposta = requests.get(MOVIDESK_BASE_URL, params=params, timeout=30)
    resposta.raise_for_status()
    todos_tickets = resposta.json()

    if not isinstance(todos_tickets, list):
        return []

    tickets_filtrados = []
    cliente_busca = cliente_organizacao.strip().lower()

    for t in todos_tickets:
        org_encontrada = False
        for c in t.get("clients", []):
            org = c.get("organization")
            if isinstance(org, dict):
                nome_org = org.get("businessName") or org.get("name") or ""
                if cliente_busca in nome_org.strip().lower():
                    org_encontrada = True
                    break
        if org_encontrada:
            tickets_filtrados.append(t)

    return tickets_filtrados


def formatar_data_br(data_iso):
    try:
        return datetime.datetime.fromisoformat(data_iso.replace("Z", "").split(".")[0]).strftime("%d/%m/%Y %H:%M")
    except (ValueError, AttributeError):
        return data_iso or "-"


def nome_solicitante(ticket):
    clientes = ticket.get("clients", [])
    if isinstance(clientes, list) and clientes:
        return clientes[0].get("businessName", "-")
    return "-"


def status_class(base_status):
    val = (base_status or "").lower()
    if "solved" in val or "closed" in val:
        return "status-success"
    if "canceled" in val:
        return "status-danger"
    if "stopped" in val:
        return "status-warning"
    return "status-info"


def gerar_conteudo_com_retry(client, model, contents, max_tentativas=5, espera_inicial=10):
    espera = espera_inicial
    for tentativa in range(1, max_tentativas + 1):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except genai_errors.ServerError as e:
            if tentativa == max_tentativas:
                raise
            time.sleep(espera)
            espera *= 2
        except genai_errors.ClientError:
            raise


def main():
    hoje = datetime.date.today()
    primeiro_dia_mes = hoje.replace(day=1)
    cliente = os.environ.get("CLIENTE_NOME")

    if not cliente:
        raise ValueError("A variável de ambiente CLIENTE_NOME não foi informada.")

    movidesk_token = os.environ.get("MOVIDESK_TOKEN")
    if not movidesk_token:
        raise ValueError("A variável de ambiente MOVIDESK_TOKEN não foi configurada.")

    print(f"Buscando tickets da organização '{cliente}' no Movidesk...")
    tickets = buscar_tickets_movidesk_por_organizacao(cliente, movidesk_token, primeiro_dia_mes, hoje)
    print(f"{len(tickets)} ticket(s) encontrado(s).")

    # Contagem de status para os cards de resumo
    total_tickets = len(tickets)
    resolvidos = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() in ["solved", "closed"])
    em_andamento = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() in ["new", "inattendance", "reopened"])
    parados = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() == "stopped")

    # Inicializa o Gemini
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("A variável de ambiente GEMINI_API_KEY não foi configurada.")
    client = genai.Client(api_key=api_key)

    dados_para_ia = [
        {
            "protocolo": t.get("protocol") or t.get("id"),
            "assunto": t.get("subject"),
            "categoria": t.get("category"),
            "urgencia": t.get("urgency"),
            "status": t.get("status"),
            "data_abertura": t.get("createdDate"),
            "solicitante": nome_solicitante(t),
        }
        for t in tickets
    ]

    prompt = f"""
Você é um especialista em Sucesso do Cliente e Relacionamento.
Abaixo estão os tickets abertos pela organização '{cliente}' no período de {primeiro_dia_mes.strftime('%d/%m/%Y')} até {hoje.strftime('%d/%m/%Y')}.

Dados dos tickets (JSON):
{dados_para_ia}

Elabore uma análise executiva estruturada e objetiva, contendo exatamente estas 3 seções com títulos em markdown (###):
### 1. Principais Dores e Dificuldades
### 2. Problemas Técnicos Recorrentes
### 3. Sugestões de Atuação

Use listas com marcadores (*) para os pontos de cada seção. Seja direto e profissional.
"""

    print("Gerando análise executiva com o Gemini...")
    response = gerar_conteudo_com_retry(client=client, model="gemini-3.6-flash", contents=prompt)
    
    # Converte o Markdown da IA em HTML limpo e estilizado
    analise_ia_html = markdown.markdown(response.text)

    # Montagem das linhas da tabela HTML
    linhas_tabela = ""
    if not tickets:
        linhas_tabela = '<tr><td colspan="6" style="text-align: center; padding: 25px; color: #6b7280;">Nenhum ticket encontrado no período.</td></tr>'
    else:
        for t in tickets:
            t_id = html.escape(str(t.get("protocol") or t.get("id")))
            assunto = html.escape(str(t.get("subject") or "-"))
            categoria = html.escape(str(t.get("category") or "-"))
            urgencia = html.escape(str(t.get("urgency") or "-"))
            status = html.escape(str(t.get("status") or "-"))
            st_class = status_class(t.get("baseStatus"))
            data_abertura = html.escape(formatar_data_br(t.get("createdDate")))

            linhas_tabela += f"""
            <tr>
                <td style="font-weight: bold; color: #374151;">#{t_id}</td>
                <td>{assunto}</td>
                <td>{categoria}</td>
                <td>{urgencia}</td>
                <td><span class="status {st_class}">{status}</span></td>
                <td>{data_abertura}</td>
            </tr>
            """

    # HTML com estilos refinados para a área de insights da IA
    html_content = f"""
    <!DOCTYPE html>
    <html lang="pt-BR">
    <head>
    <meta charset="UTF-8">
    <style>
        body {{ margin: 0; padding: 0; background-color: #f4f6f8; font-family: Arial, sans-serif; color: #202124; }}
        .wrapper {{ width: 100%; padding: 30px 0; }}
        .container {{ max-width: 800px; margin: 0 auto; background: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 16px rgba(0,0,0,0.06); }}
        .header {{ background-color: #3b1443; padding: 30px; text-align: center; color: #ffffff; }}
        .logo {{ font-size: 14px; font-weight: bold; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 10px; opacity: 0.9; }}
        .title {{ margin: 0; font-size: 22px; font-weight: bold; }}
        .subtitle {{ margin: 8px 0 0; font-size: 13px; opacity: 0.8; }}
        .content {{ padding: 30px; }}
        .cards {{ display: flex; gap: 15px; margin-bottom: 30px; justify-content: space-between; }}
        .card {{ flex: 1; background: #f8fafc; border: 1px solid #e5e7eb; border-radius: 8px; padding: 15px; text-align: center; }}
        .card-label {{ font-size: 11px; font-weight: bold; color: #6b7280; text-transform: uppercase; margin-bottom: 5px; }}
        .card-value {{ font-size: 22px; font-weight: bold; color: #111827; }}
        .section-title {{ font-size: 16px; font-weight: bold; color: #3b1443; margin: 25px 0 12px; border-bottom: 2px solid #f3f4f6; padding-bottom: 6px; }}
        
        /* Estilização refinada para os insights da IA */
        .ai-box {{ background: #faf5fb; border: 1px solid #f3e8f5; border-left: 4px solid #3b1443; padding: 20px; border-radius: 6px; font-size: 13px; line-height: 1.6; color: #374151; margin-bottom: 30px; }}
        .ai-box h3 {{ font-size: 14px; color: #3b1443; margin-top: 16px; margin-bottom: 8px; border-bottom: 1px solid #ebdcf0; padding-bottom: 4px; }}
        .ai-box h3:first-child {{ margin-top: 0; }}
        .ai-box ul {{ margin: 0 0 10px 0; padding-left: 20px; }}
        .ai-box li {{ margin-bottom: 6px; }}
        .ai-box strong {{ color: #1f2937; }}

        .table-wrapper {{ width: 100%; overflow-x: auto; border: 1px solid #e5e7eb; border-radius: 8px; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 12px; text-align: left; }}
        th {{ background: #f8fafc; color: #6b7280; font-weight: bold; padding: 12px 10px; border-bottom: 1px solid #e5e7eb; }}
        td {{ padding: 12px 10px; border-bottom: 1px solid #f0f1f3; color: #374151; }}
        .status {{ display: inline-block; padding: 4px 8px; border-radius: 12px; font-size: 10px; font-weight: bold; }}
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
                <h1 class="title">Resumo do Cliente: {html.escape(cliente)}</h1>
                <p class="subtitle">Período: {primeiro_dia_mes.strftime('%d/%m/%Y')} até {hoje.strftime('%d/%m/%Y')}</p>
            </div>
            <div class="content">
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
                                <th>Assunto</th>
                                <th>Categoria</th>
                                <th>Urgência</th>
                                <th>Status</th>
                                <th>Aberto em</th>
                            </tr>
                        </thead>
                        <tbody>
                            {linhas_tabela}
                        </tbody>
                    </table>
                </div>
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
    msg["Subject"] = f"Resumo Executivo do Cliente: {cliente} ({hoje.strftime('%d/%m/%Y')})"
    msg["From"] = email_user
    msg["To"] = email_to

    msg.attach(MIMEText(html_content, "html", "utf-8"))

    destinatarios = [e.strip() for e in email_to.split(",") if e.strip()]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(email_user, email_password)
        smtp.sendmail(email_user, destinatarios, msg.as_string())

    print(f"E-mail HTML formatado enviado com sucesso para: {email_to}")


if __name__ == "__main__":
    main()
