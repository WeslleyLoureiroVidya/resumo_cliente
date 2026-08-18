import os
import time
import datetime
import smtplib
from email.message import EmailMessage
from google import genai
from google.genai import errors as genai_errors

# Configurações de Datas (Do dia 1 até o dia atual)
hoje = datetime.date.today()
primeiro_dia_mes = hoje.replace(day=1)
cliente = os.environ.get("CLIENTE_NOME")

print(f"Buscando tickets para o cliente: {cliente} de {primeiro_dia_mes} até {hoje}")

# Mock de exemplo de dados vindos dos tickets (substitua pela chamada real à sua API de tickets)
tickets_exemplo = [
    {"id": 101, "data": "2026-08-03", "assunto": "Lentidão no módulo de relatórios", "status": "Resolvido"},
    {"id": 115, "data": "2026-08-10", "assunto": "Dúvida sobre exportação de dados em CSV", "status": "Fechado"},
    {"id": 142, "data": "2026-08-15", "assunto": "Erro 500 ao tentar cadastrar novos usuários", "status": "Em Aberto"}
]

# Inicializa o cliente da API do Gemini utilizando o segredo configurado no repositório
api_key = os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("A variável de ambiente GEMINI_API_KEY não foi configurada nos Secrets do repositório.")

client = genai.Client(api_key=api_key)

# Prompt estruturado para a IA analisar os dados
prompt = f"""
Você é um especialista em Sucesso do Cliente e Relacionamento. 
Abaixo estão os tickets abertos pelo cliente '{cliente}' no período de {primeiro_dia_mes.strftime('%d/%m/%Y')} até {hoje.strftime('%d/%m/%Y')}.

Dados dos tickets:
{tickets_exemplo}

Por favor, elabore um relatório executivo para a nossa reunião de alinhamento contendo:
1. **Visão Geral:** Quantidade de tickets abertos no período e status geral.
2. **Principais Dores e Dificuldades:** O que mais tem gerado fricção para o cliente.
3. **Problemas Técnicos Recorrentes:** Incidentes que merecem atenção da engenharia ou suporte técnico.
4. **Sugestões de Atuação:** Onde a nossa equipe deve agir proativamente para melhorar a experiência e retenção desse cliente.
"""

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


# Chamada ao modelo Gemini (com retry automático em caso de indisponibilidade)
response = gerar_conteudo_com_retry(
    client=client,
    model='gemini-3.6-flash',
    contents=prompt,
)

texto_relatorio = response.text

# Salvando o resultado em um arquivo Markdown para consulta
nome_arquivo = f"relatorio_{cliente.lower().replace(' ', '_')}_{hoje.strftime('%Y%m%d')}.md"
os.makedirs("relatorios", exist_ok=True)
caminho_completo = os.path.join("relatorios", nome_arquivo)

with open(caminho_completo, "w", encoding="utf-8") as f:
    f.write(texto_relatorio)

print(f"Relatório gerado com sucesso em: {caminho_completo}")


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
    f"Segue em anexo o relatório executivo do cliente '{cliente}', "
    f"referente ao período de {primeiro_dia_mes.strftime('%d/%m/%Y')} até {hoje.strftime('%d/%m/%Y')}.\n\n"
    f"Resumo gerado automaticamente:\n\n"
    f"{texto_relatorio}\n"
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
