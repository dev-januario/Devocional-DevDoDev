import os
import sqlite3
import subprocess
import re
from datetime import datetime
from pathlib import Path

from google import genai
from dotenv import load_dotenv
import ssl
import time

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


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cur.fetchall()]  # row[1] = name
    return column in cols


def init_db(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()

    # Cria tabela base (se não existir)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS devocionais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT UNIQUE,
            mensagem TEXT
        )
    """)
    conn.commit()

    if not column_exists(conn, "devocionais", "referencia"):
        cursor.execute("ALTER TABLE devocionais ADD COLUMN referencia TEXT")
        conn.commit()

    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_devocionais_referencia_unique
        ON devocionais(referencia)
    """)
    conn.commit()

def versiculo_ja_usado(cursor: sqlite3.Cursor, referencia: str) -> bool:
    cursor.execute("SELECT 1 FROM devocionais WHERE referencia = ?", (referencia,))
    return cursor.fetchone() is not None

def ja_enviado_hoje(cursor: sqlite3.Cursor, hoje: str) -> bool:
    cursor.execute("SELECT 1 FROM devocionais WHERE data = ?", (hoje,))
    return cursor.fetchone() is not None

def extrair_referencia(texto: str) -> str:
    for line in texto.splitlines():
        line = line.strip()

        m = re.match(r"^\*(.+?)\s*\(([^)]+)\)\*$", line)
        if m:
            ref = m.group(1).strip()
            versao = m.group(2).strip()
            if re.search(r"\d+\s*:\s*\d+", ref):
                return f"{ref} ({versao})"

        m2 = re.match(r"^\*\[\s*(.+?)\s*\]\s*\(\s*([^)]+)\s*\)\*$", line)
        if m2:
            ref = m2.group(1).strip()
            versao = m2.group(2).strip()
            if re.search(r"\d+\s*:\s*\d+", ref):
                return f"{ref} ({versao})"

    raise RuntimeError("Não foi possível extrair a referência bíblica do texto.")

def gerar_devocional(client: genai.Client, cursor: sqlite3.Cursor, data: str) -> tuple[str, str]:
    for tentativa in range(8):
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=f"""
        Hoje é {data}. Escreva um devocional cristão inédito e completo para este dia.

        Você é um teólogo, pastor e escritor de devocionais profundamente sensível à voz do Espírito Santo. Seu dom é traduzir a verdade bíblica em reflexões que revigoram a alma, trazendo paz, esperança e uma clara percepção do amor e da fidelidade de Deus. Suas palavras são como água fresca para o sedento.

        # **OBJETIVO PRINCIPAL:**
        Criar um devocional que seja um verdadeiro encontro com Deus. Que o leitor termine a leitura sentindo-se mais leve, consolado, encorajado e com uma fé mais sólida. Foque nos frutos do Espírito: amor, alegria, paz, paciência, bondade, fidelidade, mansidão e domínio próprio.

        # **INSTRUÇÕES DE CONTEÚDO E ESTRUTURA (SIGA À RISCA):**

        1.  **BASE BÍBLICA - AGORA COM CONTEXTO:**
            *   **NÃO ESCOLHA APENAS UM VERSÍCULO ISOLADO.**
            *   Escolha uma **PASSAGEM COERENTE** (máx. 6 versículos) que forme uma unidade de pensamento completa. A passagem deve conter um ensinamento sólido, uma promessa ou uma verdade sobre o caráter de Deus.
            *   Para isso, **sempre considere o contexto imediato**. Por exemplo, em vez de Filipenses 4:13 sozinho, use Filipenses 4:10-13. Em vez de Mateus 18:20 sozinho, use Mateus 18:15-20.
            *   O objetivo é que a passagem escolhida, por si só, transmita a mensagem completa sem risco de má interpretação por falta de contexto.
            *   **Mostre a versão utilizada.** Preferências: KJA, NVI e NVI+.

        2.  **FORMATO DE SAÍDA (NÃO ADICIONE SAUDAÇÕES, TÍTULOS OU DESPEDIDAS):**
            *   Comece com: `*[VERSÍCULOS]*`
            *   Pule uma linha.
            *   Em seguida, a linha da referência **EXATAMENTE** assim:
                `*NOME_DO_LIVRO CAP:VERSO_INICIAL-VERSO_FINAL (VERSÃO)*`
                Exemplo: `*Filipenses 4:10-13 (NVI)*`
            *   Pule uma linha.
            *   Liste os versículos da passagem completa, cada um em uma linha, no formato:
                `número - texto do versículo`

            *   **AGORA, A SEÇÃO CRÍTICA:**
                Após os versículos, escreva **OBRIGATORIAMENTE**:
                `*[CONTEXTO]*`
                *   Pule uma linha.
                Em seguida, escreva o texto desta seção, que **DEVE**:
                - Ter entre **45 e 60 palavras** (conte rigorosamente). [Aumentei o limite para caber a análise do contexto]
                - Ser um **ÚNICO parágrafo contínuo**, sem quebras de linha, listas ou marcadores.
                - **Explicar, em uma ou duas frases, a situação ou o tema principal do capítulo ou episódio bíblico do qual a passagem faz parte.** Em seguida, fazer uma **reflexão teológica profunda** sobre a verdade central que a passagem completa revela.
                - Ter linguagem **poética, objetiva e direta ao coração**. Conduza o leitor a sentir a verdade, não apenas a entendê-la.
                - **NUNCA ultrapassar 60 palavras.**

            *   Finalize com: `*[PARA PENSAR]*`
            *   Pule uma linha.
            *   liste 3 perguntas curtas, íntimas e instigantes que ajudem o leitor a aplicar a verdade da **passagem completa** em sua vida interior.

        # **TOM E ABORDAGEM:**
        - **Teológico e Professor:** Seja didático sem ser acadêmico. Transmita a profundidade da Palavra com clareza. **A interpretação deve ser fiel ao contexto do livro e da passagem.**
        - **Acolhedor e Poético:** Use metáforas belas e imagens que toquem a alma (ex: "Deus é o oleiro que nos forma com cuidado", "Sua graça é como um rio que não seca").
        - **Foco no Amor de Deus:** A mensagem central deve sempre ser o caráter amoroso, fiel e presente de Deus. **Evite completamente tom de condenação ou culpa.**
        - **Revigorante:** As palavras devem trazer ânimo, como um respiro profundo de ar puro para o espírito.

        # **RESTRIÇÃO FINAL:**
        O devocional deve fluir como uma unidade: **Passagem Bíblica (com contexto) -> Explicação do Contexto Mais Ample -> Reflexão Teológica -> Perguntas para interiorização.** Cada parte deve se conectar perfeitamente, mostrando como a verdade emerge naturalmente do texto em seu ambiente original.
            """.strip(),
        )

        text = getattr(response, "text", None)
        if not text:
            raise RuntimeError("Gemini retornou resposta vazia.")

        text = text.strip()

        # Debug opcional: mostra início do texto
        print("=== TEXTO GERADO PELO GEMINI (INÍCIO) ===")
        print("\n".join(text.splitlines()[:25]))
        print("=== TEXTO GERADO PELO GEMINI (FIM) ===")

        referencia = extrair_referencia(text)

        if versiculo_ja_usado(cursor, referencia):
            print(f"⚠️ Versículo repetido: {referencia}. Tentando outro ({tentativa + 1}/8)...")
            continue

        return text, referencia

    raise RuntimeError("Não consegui gerar um devocional com referência inédita após várias tentativas.")

def job_diario() -> None:
    require_env("GROUP_ID")
    api_key = require_env("GEMINI_API_KEY")

    client = genai.Client(api_key=api_key)
    hoje = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(str(DB_PATH))
    try:
        init_db(conn)
        cursor = conn.cursor()

        if ja_enviado_hoje(cursor, hoje):
            print("⚠️ Devocional de hoje já enviado. Encerrando.")
            return

        devocional, referencia = gerar_devocional(client, cursor, hoje)

        texto_final = f"""Olá, irmãos e irmãs!🙏

Hoje preparei uma palavra de Deus pra você:

{devocional}

Reserve um momento pra meditar.
Deus é contigo.🤍
""".strip()

        OUTBOX_PATH.write_text(texto_final, encoding="utf-8")
        print("✅ Mensagem salva em outbox.txt")

        # **NOVIDADE: Execução direta do Node.js**
        print("Enviando mensagem pelo bot...")
        
        # Caminho absoluto para evitar problemas
        node_script = BASE_DIR / "index-send-message.js"
        
        # Executa o Node.js diretamente
        result = subprocess.run(
            ["node", str(node_script)],
            capture_output=True,
            text=True,
            timeout=60  # timeout de 60 segundos
        )
        
        if result.returncode == 0:
            print("✅ Mensagem enviada com sucesso!")
        else:
            print(f"❌ Erro ao enviar mensagem: {result.stderr}")
        
        # Salva no banco de dados
        cursor.execute(
            "INSERT INTO devocionais (data, referencia, mensagem) VALUES (?, ?, ?)",
            (hoje, referencia, texto_final)
        )
        conn.commit()

        print(f"✅ Devocional registrado com sucesso. Ref: {referencia}")

    finally:
        conn.close()

if __name__ == "__main__":
    job_diario()
