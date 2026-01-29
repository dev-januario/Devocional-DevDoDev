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

def gerar_devocional(client: genai.Client, data: str) -> str:
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"""
Hoje é {data}. Escreva um devocional inédito para este dia.

Você é um mentor cristão e escritor de devocionais, conhecido por sua sensibilidade, profundidade teológica e capacidade de traduzir verdades bíblicas para o coração de forma simples e emocionante.

# Instruções de Conteúdo
1. Base Bíblica: Mostre a versão utilizada para o versículo, preferências: KJA, NVI e NVI+.
2. Contextualização: Ao abordar um tema, não apresente apenas um versículo isolado. Se o texto fizer parte de uma narrativa ou ensinamento maior (ex: A Armadura de Deus, O Fruto do Espírito, As Bem-aventuranças), apresente o bloco de versículos completo para garantir a fidelidade ao contexto.
3. Linguagem: O tom deve ser acolhedor, cheio de paz, poético e acessível. Evite termos excessivamente técnicos; fale como um amigo sábio.
4. Impacto Emocional: Em seus comentários, busque tocar a alma. Use metáforas e reflexões que despertem sentimentos de esperança, consolo e a percepção do amor de Deus.
5. Concisão: O contexto deve ter no máximo 8 linhas. As perguntas devem ser objetivas e diretas, com no máximo 2 linhas cada.

# Estrutura do Devocional
1. [A PALAVRA]:
   - Referência bíblica
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

*[VERSÍCULOS]*

*[Referência Bíblica] (Versão)*

[número] - [versículo]
[número] - [versículo]

*[CONTEXTO]*

[texto do contexto - máximo 8 linhas]

*[PARA PENSAR]*

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

def job_diario() -> None:
    group_id = require_env("GROUP_ID")
    api_key = require_env("GEMINI_API_KEY")

    client = genai.Client(api_key=api_key)

    hoje = "2026-01-17"
    # hoje = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        init_db(conn)
        cursor = conn.cursor()

        if ja_enviado_hoje(cursor, hoje):
            print("⚠️ Devocional de hoje já enviado. Encerrando.")
            return

        devocional = gerar_devocional(client, hoje)

        texto_final = f"""Olá, irmãos e irmãs!🙏

Hoje preparei uma palavra de Deus pra você:

{devocional}

Reserve um momento pra meditar.
Deus é contigo.🤍
""".strip()

        OUTBOX_PATH.write_text(texto_final, encoding="utf-8")
        print("✅ Mensagem salva em outbox.txt. Envie pelo index-send-message.ts!")

        import time
        import subprocess
        import signal
        import psutil
        print("Abrindo terminal para enviar mensagem pelo bot...")
        proc = subprocess.Popen([
            "gnome-terminal",
            f"--working-directory=/home/dev_januario/Área de Trabalho/Estudo/devocional-bot",
            "--", "bash", "-c",
            "source $NVM_DIR/nvm.sh && nvm use 20 && npm run start:dev & sleep 20 && exit"
        ])
        time.sleep(20)
        proc.terminate()
        print("Terminal encerrado após envio da mensagem.")

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
