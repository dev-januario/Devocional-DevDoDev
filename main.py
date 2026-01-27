import os
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from google import genai
from dotenv import load_dotenv
import ssl

ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv()

GROUP_ID = os.getenv("GROUP_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.db"
OUTBOX_PATH = BASE_DIR / "outbox.txt"
NODE_SENDER_PATH = BASE_DIR / "send_whatsapp.mjs"

def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Variável de ambiente ausente: {name}")
    return value

def init_db(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS devocionais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT UNIQUE,
            mensagem TEXT
        )
    """)
    conn.commit()

def ja_enviado_hoje(cursor: sqlite3.Cursor, hoje: str) -> bool:
    cursor.execute("SELECT 1 FROM devocionais WHERE data = ?", (hoje,))
    return cursor.fetchone() is not None

def gerar_devocional(client: genai.Client) -> str:
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents="""
Você é um mentor cristão e escritor de devocionais, conhecido por sua sensibilidade, profundidade teológica e capacidade de traduzir verdades bíblicas para o coração de forma simples e emocionante.

# Instruções de Conteúdo
1. Base Bíblica: Mostre a versão utilizada para o versículo, preferências: KJA, NVI e NVI+.
2. Contextualização: Ao abordar um tema, não apresente apenas um versículo isolado. Se o texto fizer parte de uma narrativa ou ensinamento maior (ex: A Armadura de Deus, O Fruto do Espírito, As Bem-aventuranças), apresente o bloco de versículos completo para garantir a fidelidade ao contexto.
3. Linguagem: O tom deve ser acolhedor, cheio de paz, poético e acessível. Evite termos excessivamente técnicos; fale como um amigo sábio.
4. Impacto Emocional: Em seus comentários, busque tocar a alma. Use metáforas e reflexões que despertem sentimentos de esperança, consolo e a percepção do amor de Deus.
5. Concisão: O contexto deve ter no máximo 8 linhas. As perguntas devem ser objetivas e diretas, com no máximo 2 linhas cada.

# Estrutura do Devocional
1. [A PALAVRA]:
   - Referência bíblica (ex: *Filipenses 4:6-7 (KJA)*)
   - Versículos numerados no formato:
     6 - [texto do versículo]
     7 - [texto do versículo]

2. [CONTEXTO]:
   - Explicação histórica e espiritual do texto
   - MÁXIMO 8 linhas
   - Tom acolhedor e poético

3. [PARA PENSAR]:
   - 3 perguntas reflexivas
   - Cada pergunta com NO MÁXIMO 1 linha
   - Diretas e impactantes

# Formato de Saída
NÃO inclua saudações ou despedidas. Apenas o conteúdo estruturado:

[A PALAVRA]

**[Referência Bíblica] (Versão)**

[número] - [versículo]
[número] - [versículo]

[CONTEXTO]

[texto do contexto - máximo 8 linhas]

[PARA PENSAR]

1. [pergunta curta e direta - máximo 2 linhas]
2. [pergunta curta e direta - máximo 2 linhas]
3. [pergunta curta e direta - máximo 2 linhas]

# Restrição Importante
O foco nunca deve ser a condenação, mas sim o arrependimento gerado pelo amor e o desejo de ser mais parecido com Cristo.
        """.strip()
    )

    text = getattr(response, "text", None)
    if not text:
        raise RuntimeError("Gemini retornou resposta vazia.")
    return text.strip()

def enviar_whatsapp_via_node(mensagem: str, group_id: str) -> None:
    if not NODE_SENDER_PATH.exists():
        raise RuntimeError(f"Sender Node não encontrado em: {NODE_SENDER_PATH}")

    OUTBOX_PATH.write_text(mensagem, encoding="utf-8")
    
    env = os.environ.copy()
    env['NODE_TLS_REJECT_UNAUTHORIZED'] = '0'

    result = subprocess.run(
        ["node", str(NODE_SENDER_PATH), group_id, str(OUTBOX_PATH)],
        capture_output=True,
        text=True,
        env=env,
        timeout=200  # 3 minutos + margem
    )

    if result.returncode != 0:
        # Se falhar por problema de rede, apenas loga e continua
        if "Timeout" in result.stderr or "Erro de conexão" in result.stderr:
            print("⚠️ Falha no envio (problema de rede). Mensagem salva em outbox.txt")
            print(f"stderr: {result.stderr}")
            return
        
        # Outros erros, lança exceção
        raise RuntimeError(
            "Falha ao enviar via Node.\n"
            f"stdout:\n{result.stdout}\n\nstderr:\n{result.stderr}"
        )
    else:
        print("✅ Mensagem enviada via Node.js")
    
def verificar_autenticacao_node() -> bool:
    result = subprocess.run(
        ["node", str(BASE_DIR / "check_auth.js")],
        capture_output=True,
        text=True
    )
    return result.returncode == 0

def job_diario() -> None:
    group_id = require_env("GROUP_ID")
    api_key = require_env("GEMINI_API_KEY")

    if not verificar_autenticacao_node():
        print("❌ Autenticação do WhatsApp não configurada.")
        print("Execute: node send_whatsapp.mjs --authenticate")
        return

    client = genai.Client(api_key=api_key)

    hoje = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        init_db(conn)
        cursor = conn.cursor()

        if ja_enviado_hoje(cursor, hoje):
            print("⚠️ Devocional de hoje já enviado. Encerrando.")
            return

        devocional = gerar_devocional(client)

        texto_final = f"""Olá, Alysson 🙏

Hoje preparei uma palavra de Deus pra você:

{devocional}

Reserve um momento pra meditar.
Deus é contigo. 🤍
""".strip()

        enviar_whatsapp_via_node(texto_final, group_id)

        cursor.execute(
            "INSERT INTO devocionais (data, mensagem) VALUES (?, ?)",
            (hoje, texto_final)
        )
        conn.commit()

        print("✅ Devocional enviado com sucesso.")
    finally:
        conn.close()

if __name__ == "__main__":
    job_diario()
