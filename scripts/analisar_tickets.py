import os
import time
import datetime
import html
import smtplib
import re
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.genai import Client
from google.genai import errors as genai_errors

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
    if not isinstance(todos_tickets, list): return []
    
    tickets_filtrados = []
    cliente_busca = cliente_organizacao.strip().lower()
    for t in todos_tickets:
        for c in t.get("clients", []):
            org = c.get("organization")
            if isinstance(org, dict):
                nome_org = org.get("businessName") or org.get("name") or ""
                if cliente_busca in nome_org.strip().lower():
                    tickets_filtrados.append(t)
                    break
    return tickets_filtrados

def formatar_data_br(data_iso):
    try: return datetime.datetime.fromisoformat(data_iso.replace("Z", "").split(".")[0]).strftime("%d/%m/%Y %H:%M")
    except: return "-"

def status_class(base_status):
    val = (base_status or "").lower()
    if "solved" in val or "closed" in val: return "status-success"
    if "canceled" in val: return "status-danger"
    if "stopped" in val: return "status-warning"
    return "status-info"

def markdown_para_html(texto_md):
    linhas = texto_md.split("\n")
    html_saida = []
    em_lista = False
    for linha in linhas:
        l = linha.strip()
        if l.startswith("### "):
            if em_lista: html_saida.append("</ul>"); em_lista = False
            html_saida.append(f"<h3>{l.replace('### ', '')}</h3>")
        elif l.startswith("* ") or l.startswith("- "):
            if not em_lista: html_saida.append("<ul>"); em_lista = True
            conteudo = re.sub(r'\*\*(.*?)\*\*', r'<strong>\1</strong>', l[2:])
            html_saida.append(f"<li>{conteudo}</li>")
        elif l:
            if em_lista: html_saida.append("</ul>"); em_lista = False
            html_saida.append(f"<p>{re.sub(r'**(.*?**', r'<strong>\1</strong>', l)}</p>")
    if em_lista: html_saida.append("</ul>")
    return "\n".join(html_saida)

def processar_e_enviar(client, cliente, tickets, primeiro_dia_mes, hoje):
    # Cálculo de métricas
    total = len(tickets)
    resolvidos = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() in ["solved", "closed"])
    em_andamento = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() in ["new", "inattendance", "reopened"])
    parados = sum(1 for t in tickets if (t.get("baseStatus") or "").lower() == "stopped")

    # IA
    prompt = f"Analise estes tickets da organização '{cliente}' e faça um resumo executivo com 3 seções (Dores, Técnicos, Sugestões): {tickets}"
    response = client.models.generate_content(model="gemini-1.5-flash", contents=prompt)
    analise_html = markdown_para_html(response.text)

    # Montagem do HTML e Envio
    # (Inserir aqui o template HTML que você já possui, substituindo as variáveis)
    print(f"Relatório de {cliente} enviado com sucesso.")

def main():
    hoje = datetime.date.today()
    primeiro_dia_mes = hoje.replace(day=1)
    
    # Lista de clientes configurada via ambiente
    clientes_raw = os.environ.get("CLIENTES_LISTA", "")
    lista_clientes = [c.strip() for c in clientes_raw.split(",") if c.strip()]
    
    client = Client(api_key=os.environ.get("GEMINI_API_KEY"))
    token = os.environ.get("MOVIDESK_TOKEN")

    for cliente in lista_clientes:
        print(f"Processando {cliente}...")
        tickets = buscar_tickets_movidesk_por_organizacao(cliente, token, primeiro_dia_mes, hoje)
        processar_e_enviar(client, cliente, tickets, primeiro_dia_mes, hoje)

if __name__ == "__main__":
    main()
