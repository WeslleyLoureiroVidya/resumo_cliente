import os
import datetime
from google import genai
from google.genai import types
import requests

# Configurações de Datas (Do dia 1 até o dia atual)
hoje = datetime.date.today()
primeiro_dia_mes = hoje.replace(day=1)

cliente = os.environ.get("CLIENTE_NOME")
print(f"Buscando tickets para o cliente: {cliente} de {primeiro_dia_mes} até {hoje}")

# Exemplo: Requisição ao seu sistema de tickets (substitua pela API real que você utiliza)
# tickets_data = buscar_tickets_da_sua_api(cliente, primeiro_dia_mes, hoje)

# Mock de exemplo de dados vindos dos tickets para demonstração
tickets_exemplo = [
    {"id": 101, "data": "2026-08-03", "assunto": "Lentidão no módulo de relatórios", "status": "Resolvido"},
    {"id": 115, "data": "2026-08-10", "assunto": "Dúvida sobre exportação de dados em CSV", "status": "Fechado"},
    {"id": 142, "data": "2026-08-15", "assunto": "Erro 500 ao tentar cadastrar novos usuários", "status": "Em Aberto"}
]

# Inicializa o cliente da API do Gemini
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

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

# Chamada ao modelo Gemini (utilizando o modelo padrão recomendado)
response = client.models.generate_content(
    model='gemini-2.5-flash',
    contents=prompt,
)

# Salvando o resultado em um arquivo Markdown para consulta
nome_arquivo = f"relatorio_{cliente.lower().replace(' ', '_')}_{hoje.strftime('%Y%m%d')}.md"
os.makedirs("relatorios", exist_ok=True)
caminho_completo = os.path.join("relatorios", nome_arquivo)

with open(caminho_completo, "w", encoding="utf-8") as f:
    f.write(response.text)

print(f"Relatório gerado com sucesso em: {caminho_completo}")
