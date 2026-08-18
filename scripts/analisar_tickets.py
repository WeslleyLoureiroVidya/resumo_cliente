import os
import time
import datetime
import smtplib
import requests
from email.message import EmailMessage
from google import genai
from google.genai import errors as genai_errors

MOVIDESK_BASE_URL = "https://api.movidesk.com/public/v1/tickets"

# Mapeia o status base do Movidesk para um emoji, deixando o relatório mais fácil de escanear
EMOJI_STATUS = {
    "New": "🆕",
    "InAttendance": "🔧",
    "Stopped": "⏸️",
    "Solved": "✅",
    "Closed": "✅",
    "Canceled": "❌",
    "Reopened": "🔁",
}


def escapar_odata(texto):
    """Escapa aspas simples para uso seguro dentro de literais string do OData."""
    return texto.replace("'", "''")


def buscar_tickets_movidesk(cliente_organizacao, token, data_inicio, data_fim):
    """
    Busca no Movidesk todos os tickets vinculados à ORGANIZAÇÃO do cliente
    (não à pessoa que abriu o ticket), dentro do período informado.
    """
    cliente_escapado = escapar_odata(cliente_organizacao)
    data_inicio_str = data_inicio.strftime("%Y-%m-%dT00:00:00")
    data_fim_str = (data_fim + datetime.timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")

    filtro = (
        f"createdDate ge {data_inicio_str} and createdDate lt {data_fim_str} "
        f"and clients/any(c: c/organization/businessName eq '{cliente_escapado}')"
    )

    params = {
        "token": token,
        "$select": "id,protocol,subject,category,urgency,status,baseStatus,createdDate",
        "$expand": (
            "clients($select=businessName;$expand=organization($select=businessName)),"
            "owner($select=businessName)"
        ),
        "$filter": filtro,
        "$orderby": "createdDate desc",
    }

    resposta = requests.get(MOVIDESK_BASE_URL, params=params, timeout=30)
    resposta.raise_for_status()
    return resposta.json()


def formatar_data_br(data_iso):
    try:
        return datetime.datetime.fromisoformat(data_iso.split(".")[0]).strftime("%d/%m/%Y %H:%M")
    except (ValueError, AttributeError):
        return data_iso or "-"


def nome_organizacao(ticket):
    for c in ticket.get("clients", []):
        org = c.get("organization")
        if org and org.get("businessName"):
            return org["businessName"]
    return "-"


def nome_solicitante(ticket):
    clientes = ticket.get("clients", [])
    if clientes:
        return clientes[0].get("businessName", "-")
    return "-"


def montar_tabela_tickets(tickets):
    """Monta uma tabela markdown organizada, com número do ticket, status, urgência, datas e solicitante."""
    if not tickets:
        return "_Nenhum ticket encontrado para esta organização no período._\n"

    linhas = [
        "| # Protocolo | Assunto | Categoria | Urgência | Status | Aberto em | Solicitante |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in tickets:
        emoji = EMOJI_STATUS.get(t.get("baseStatus"), "•")
        linhas.append(
            "| {protocolo} | {assunto} | {categoria} | {urgencia} | {emoji} {status} | {data} | {solicitante} |".format(
                protocolo=t.get("protocol") or t.get("id"),
                assunto=(t.get("subject") or "-").replace("|", "/"),
                categoria=t.get("category") or "-",
                urgencia=t.get("urgency") or "-",
                emoji=emoji,
                status=t.get("status") or "-",
                data=formatar_data_br(t.get("createdDate")),
                solicitante=nome_solicitante(t),
            )
        )
    return "\n".join(linhas) + "\n"


def montar_resumo_status(tickets):
    """Conta tickets por status base para dar uma visão geral rápida no topo do relatório."""
    contagem = {}
    for t in tickets:
        status = t.get("status") or "Desconhecido"
        contagem[status] = contagem.get(status, 0) + 1
    if not contagem:
        return "- Nenhum ticket no período.\n"
    linhas = [f"- **{status}:** {qtd} ticket(s)" for status, qtd in sorted(contagem.items(), key=lambda x: -x[1])]
    return "\n".join(linhas) + "\n"


def gerar_conteudo_com_retry(client, model, contents, max_tentativas=5, espera_inicial=10):
    """
    Chama o Gemini com retry e backoff exponencial.
    Trata especificamente erros 503 (modelo sobrecarregado) e 429 (rate limit).
    """
    espera = espera_inicial
    for tentativa in range(1, max_tentativas + 1):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except genai_errors.ServerError as e:
            if tentativa == max_tentativas:
                print(f"Falhou após {max_tentativas} tentativas. Desistindo.")
                raise
            print(f"Tentativa {tentativa}/{max_tentativas} falhou ({e}). "
                  f"Aguardando {espera}s antes de tentar novamente...")
            time.sleep(espera)
            espera *= 2  # backoff exponencial: 10s, 20s, 40s, 80s...
        except genai_errors.ClientError as e:
            # Erros 4xx (ex: chave inválida, modelo não encontrado) não adiantam retry
            print(f"Erro do cliente, não é recuperável com retry: {e}")
            raise


def enviar_email(destinatario, remetente, senha_app, assunto, corpo, caminho_anexo):
    """Envia o relatório por e-mail via Gmail SMTP, com o .md em anexo."""
    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = remetente
    msg["To"] = destinatario
    msg.set_content(corpo)

    with open(caminho_anexo, "rb") as f:
        conteudo_anexo = f.read()
    msg.add_attachment(
        conteudo_anexo,
        maintype="text",
        subtype="markdown",
        filename=os.path.basename(caminho_anexo),
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(remetente, senha_app)
        smtp.send_message(msg)


def main():
    hoje = datetime.date.today()
    primeiro_dia_mes = hoje.replace(day=1)
    cliente = os.environ.get("CLIENTE_NOME")

    movidesk_token = os.environ.get("MOVIDESK_TOKEN")
    if not movidesk_token:
        raise ValueError("A variável de ambiente MOVIDESK_TOKEN não foi configurada nos Secrets do repositório.")

    print(f"Buscando tickets da organização '{cliente}' no Movidesk, de {primeiro_dia_mes} até {hoje}...")
    tickets = buscar_tickets_movidesk(cliente, movidesk_token, primeiro_dia_mes, hoje)
    print(f"{len(tickets)} ticket(s) encontrado(s).")

    tabela_tickets_md = montar_tabela_tickets(tickets)
    resumo_status_md = montar_resumo_status(tickets)

    # Inicializa o cliente da API do Gemini
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("A variável de ambiente GEMINI_API_KEY não foi configurada nos Secrets do repositório.")
    client = genai.Client(api_key=api_key)

    # Dados enviados à IA: só os campos relevantes para análise (sem inflar o prompt)
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
Abaixo estão os tickets abertos pela organização '{cliente}' no período de {primeiro_dia_mes.strftime('%d/%m/%Y')} até {hoje.strftime('%d/%m/%Y')}, considerando TODOS os solicitantes dessa organização, independentemente de quem abriu cada ticket.

Dados dos tickets (JSON):
{dados_para_ia}

Elabore uma análise executiva objetiva, em markdown, para uma reunião de alinhamento, contendo:
1. **Principais Dores e Dificuldades:** o que mais tem gerado fricção para o cliente.
2. **Problemas Técnicos Recorrentes:** incidentes que merecem atenção da engenharia ou suporte técnico.
3. **Sugestões de Atuação:** onde nossa equipe deve agir proativamente para melhorar a experiência e retenção desse cliente.

Não repita a lista de tickets nem faça uma contagem geral — isso já será exibido separadamente no relatório. Vá direto para a análise.
Se não houver tickets suficientes para uma conclusão robusta, diga isso claramente em vez de especular.
"""

    print("Gerando análise executiva com o Gemini...")
    response = gerar_conteudo_com_retry(client=client, model="gemini-2.5-flash", contents=prompt)
    analise_ia = response.text

    # Monta o relatório final, combinando visão geral + tabela de tickets + análise da IA
    texto_relatorio = f"""# 📊 Resumo do Cliente: {cliente}

**Período analisado:** {primeiro_dia_mes.strftime('%d/%m/%Y')} a {hoje.strftime('%d/%m/%Y')}
**Total de tickets no período:** {len(tickets)}

---

## 🗂️ Visão Geral por Status

{resumo_status_md}
---

## 🎫 Tickets do Período

{tabela_tickets_md}
---

## 🔎 Análise Executiva

{analise_ia}
"""

    # Salvando o resultado em um arquivo Markdown
    nome_arquivo = f"relatorio_{cliente.lower().replace(' ', '_')}_{hoje.strftime('%Y%m%d')}.md"
    os.makedirs("relatorios", exist_ok=True)
    caminho_completo = os.path.join("relatorios", nome_arquivo)
    with open(caminho_completo, "w", encoding="utf-8") as f:
        f.write(texto_relatorio)
    print(f"Relatório gerado com sucesso em: {caminho_completo}")

    # Envio do e-mail
    email_user = os.environ.get("EMAIL_USER")
    email_password = os.environ.get("EMAIL_PASSWORD")
    email_to = os.environ.get("EMAIL_TO")
    if not all([email_user, email_password, email_to]):
        raise ValueError(
            "As variáveis EMAIL_USER, EMAIL_PASSWORD e EMAIL_TO precisam estar configuradas nos Secrets do repositório."
        )

    assunto_email = f"Resumo do Cliente {cliente} - Reunião de Alinhamento ({hoje.strftime('%d/%m/%Y')})"
    corpo_email = (
        f"Olá,\n\n"
        f"Segue em anexo o relatório executivo do cliente '{cliente}' "
        f"({len(tickets)} ticket(s) no período de {primeiro_dia_mes.strftime('%d/%m/%Y')} até {hoje.strftime('%d/%m/%Y')}).\n\n"
        f"O relatório completo, com a tabela de tickets e a análise executiva, está no anexo em Markdown.\n"
    )

    enviar_email(
        destinatario=email_to,
        remetente=email_user,
        senha_app=email_password,
        assunto=assunto_email,
        corpo=corpo_email,
        caminho_anexo=caminho_completo,
    )
    print(f"E-mail enviado com sucesso para: {email_to}")


if __name__ == "__main__":
    main()
