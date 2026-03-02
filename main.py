import os
import sqlite3
import re
import json
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from dotenv import load_dotenv
import ssl
import time
import hashlib
import random

ssl._create_default_https_context = ssl._create_unverified_context

load_dotenv(override=False)

GROUP_ID = os.getenv("GROUP_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.db"
OUTBOX_PATH = BASE_DIR / "outbox.txt"
SEND_STATUS_PATH = BASE_DIR / "send_status.json"
NODE_SENDER_PATH = BASE_DIR / "index-send-message.ts"

def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Variável de ambiente ausente: {name}")
    return value

def hash_texto(s: str) -> str:
    return hashlib.sha256(s.strip().encode("utf-8")).hexdigest()

def hash_ja_usado(cursor: sqlite3.Cursor, hash_msg: str) -> bool:
    cursor.execute("SELECT 1 FROM devocionais WHERE hash_mensagem = ?", (hash_msg,))
    return cursor.fetchone() is not None

def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cur.fetchall()]
    return column in cols

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

    if not column_exists(conn, "devocionais", "referencia"):
        cursor.execute("ALTER TABLE devocionais ADD COLUMN referencia TEXT")
        conn.commit()

    if not column_exists(conn, "devocionais", "hash_mensagem"):
        cursor.execute("ALTER TABLE devocionais ADD COLUMN hash_mensagem TEXT")
        conn.commit()

    if not column_exists(conn, "devocionais", "livro"):
        cursor.execute("ALTER TABLE devocionais ADD COLUMN livro TEXT")
        conn.commit()

    if not column_exists(conn, "devocionais", "capitulo"):
        cursor.execute("ALTER TABLE devocionais ADD COLUMN capitulo INTEGER")
        conn.commit()

    if not column_exists(conn, "devocionais", "verso_inicial"):
        cursor.execute("ALTER TABLE devocionais ADD COLUMN verso_inicial INTEGER")
        conn.commit()

    if not column_exists(conn, "devocionais", "verso_final"):
        cursor.execute("ALTER TABLE devocionais ADD COLUMN verso_final INTEGER")
        conn.commit()

    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_devocionais_hash_unique
        ON devocionais(hash_mensagem)
    """)
    conn.commit()

def parsear_referencia(ref: str) -> dict | None:
    ref_limpa = re.sub(r'\s*\([^)]+\)\s*$', '', ref).strip()
    padrao = r'^(.+?)\s+(\d+)\s*:\s*(\d+)(?:\s*-\s*(\d+))?'
    m = re.match(padrao, ref_limpa)
    if not m:
        return None

    livro = m.group(1).strip()
    capitulo = int(m.group(2))
    verso_inicial = int(m.group(3))
    verso_final = int(m.group(4)) if m.group(4) else verso_inicial

    return {
        'livro': livro,
        'capitulo': capitulo,
        'verso_inicial': verso_inicial,
        'verso_final': verso_final
    }

def ha_sobreposicao(cursor: sqlite3.Cursor, referencia: str) -> bool:
    dados = parsear_referencia(referencia)
    if not dados:
        return False

    cursor.execute("""
        SELECT verso_inicial, verso_final
        FROM devocionais
        WHERE livro = ? AND capitulo = ?
    """, (dados['livro'], dados['capitulo']))

    registros = cursor.fetchall()

    novo_ini = dados['verso_inicial']
    novo_fim = dados['verso_final']

    for v_ini, v_fim in registros:
        if v_ini is None or v_fim is None:
            continue
        if (v_ini <= novo_ini <= v_fim or
            v_ini <= novo_fim <= v_fim or
            (novo_ini <= v_ini and novo_fim >= v_fim)):
            return True

    return False

def ja_enviado_hoje(cursor: sqlite3.Cursor, hoje: str) -> bool:
    cursor.execute("SELECT 1 FROM devocionais WHERE data = ?", (hoje,))
    return cursor.fetchone() is not None

def extrair_referencia(texto: str) -> str:
    linhas = texto.splitlines()
    encontrou_versiculos = False

    for line in linhas:
        line = line.strip()

        if line in ("*[VERSÍCULOS]*", "[VERSÍCULOS]", "*[VERSICULOS]*", "[VERSICULOS]"):
            encontrou_versiculos = True
            continue

        if encontrou_versiculos:
            m = re.match(r"^\*(.+?)\s*\(([^)]+)\)\*$", line)
            if m:
                ref = m.group(1).strip()
                versao = m.group(2).strip()
                if re.search(r"\d+\s*:\s*\d+", ref):
                    return f"{ref} ({versao})"

            m2 = re.match(r"^(.+?)\s+(\d+)\s*:\s*(\d+(?:\s*-\s*\d+)?)\s*\(([^)]+)\)\s*$", line)
            if m2:
                livro = m2.group(1).strip()
                cap = m2.group(2).strip()
                versos = m2.group(3).strip()
                versao = m2.group(4).strip()
                return f"{livro} {cap}:{versos} ({versao})"

            m3 = re.match(r"^\*\[\s*(.+?)\s*\]\s*\(\s*([^)]+)\s*\)\*$", line)
            if m3:
                ref = m3.group(1).strip()
                versao = m3.group(2).strip()
                if re.search(r"\d+\s*:\s*\d+", ref):
                    return f"{ref} ({versao})"

            if re.match(r"^\d+\s*-\s*.+", line):
                continue

    raise RuntimeError("Não foi possível extrair a referência bíblica do texto.")

def validar_formato_devocional(texto: str) -> tuple[bool, str]:
    linhas = texto.splitlines()

    tem_versiculos = False
    for line in linhas[:5]:
        line_clean = line.strip()
        if line_clean in ("*[VERSÍCULOS]*", "[VERSÍCULOS]", "*[VERSICULOS]*", "[VERSICULOS]"):
            tem_versiculos = True
            break
    if not tem_versiculos:
        return False, "Falta a seção [VERSÍCULOS] no início"

    referencias_encontradas = []
    for line in linhas:
        line = line.strip()
        if re.match(r"^\*?[A-Za-zÀ-ú\s]+\d+:\d+(-\d+)?\s*\([^)]+\)\*?$", line):
            referencias_encontradas.append(line)

    if len(referencias_encontradas) == 0:
        return False, "Nenhuma referência bíblica encontrada"
    if len(referencias_encontradas) > 1:
        return False, f"Múltiplas traduções detectadas! Encontrei {len(referencias_encontradas)} referências."

    tem_contexto = any(line.strip() in ("*[CONTEXTO]*", "[CONTEXTO]") for line in linhas)
    if not tem_contexto:
        return False, "Falta a seção [CONTEXTO]"

    tem_pensar = any(line.strip() in ("*[PARA PENSAR]*", "[PARA PENSAR]") for line in linhas)
    if not tem_pensar:
        return False, "Falta a seção [PARA PENSAR]"

    return True, "OK"

def normalizar_formato(texto: str) -> str:
    linhas = texto.splitlines()
    linhas_normalizadas = []

    for line in linhas:
        line_stripped = line.strip()

        if line_stripped in ("[VERSÍCULOS]", "[VERSICULOS]"):
            linhas_normalizadas.append("*[VERSÍCULOS]*")
        elif line_stripped in ("[CONTEXTO]",):
            linhas_normalizadas.append("*[CONTEXTO]*")
        elif line_stripped in ("[PARA PENSAR]",):
            linhas_normalizadas.append("*[PARA PENSAR]*")
        elif re.match(r"^[A-Za-zÀ-ú\s]+\d+:\d+(-\d+)?\s*\([^)]+\)$", line_stripped):
            linhas_normalizadas.append(f"*{line_stripped}*")
        else:
            linhas_normalizadas.append(line)

    return "\n".join(linhas_normalizadas)

def gerar_devocional(client: genai.Client, cursor: sqlite3.Cursor, data: str) -> tuple[str, str]:
    modelos = [
        "gemini-3-pro-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.0-pro",
        "gemini-2.0-flash",
    ]

    for tentativa in range(8):
        model = modelos[min(tentativa, len(modelos) - 1)]

        try:
            response = client.models.generate_content(
                model=model,
                contents=f"""
                Hoje é {data}. Com base na data e no direcionamento abaixo, escreva um devocional cristão inédito, completo e transformador.

                SUA IDENTIDADE: Você é um mestre-teólogo, pastor e escritor com um dom dado pelo Espírito Santo para ensinar e exortar. Sua vocação é abrir o entendimento das pessoas para a verdade bíblica, mesmo quando ela é desafiadora. Você comunica com a firmeza de um profeta e a ternura de um pastor, sempre guiando para a graça, não parando na lei. Suas palavras têm o objetivo de convencer, instruir e corrigir, usando somente as Escrituras como base, sem opiniões pessoais.

                OBJETIVO DO DEVOCIONAL: Gerar uma reflexão que promova mudança interior e transformação de vida. O foco não é apenas em promessas de milagres e bênçãos, mas em ensinamentos sólidos, exortações amorosas e chamados à santidade. O leitor deve terminar a leitura sentindo-se desafiado a olhar para sua própria vida, confrontado pela verdade, mas também profundamente amado e capacitado pela graça de Deus para mudar.

                INSTRUÇÕES ESTRITAS DE ESTRUTURA E CONTEÚDO:
                1. ESCOLHA DA PASSAGEM:

                Escolha uma passagem coesa de inúmeros versículos (ou quantos achar necessário) que contenha um ensino claro, uma correção ou um princípio de vida que possa ser aplicado para exortação.

                CONTEXTO É TUDO. A passagem deve fazer sentido por si só. Evite versículos isolados que possam ser mal interpretados.

                DIVERSIFIQUE: Explore toda a Bíblia. Use passagens do Antigo e Novo Testamentos que tragam lições sobre caráter, relacionamento com Deus, santidade, humildade, perdão, etc.

                VERSÃO PADRÃO: Use sempre a NVI (Nova Versão Internacional) como base, devido à sua clareza e linguagem moderna.

                2. FORMATAÇÃO DE SAÍDA (SIGA EXATAMENTE ESTA ORDEM, SEM TÍTULOS EXTRA):

                [VERSÍCULOS]

                [Nome do Livro] [Cap]:[V_ini]-[V_fim] (NVI)

                [numero] - [texto do versículo]
                [numero] - [texto do versículo]
                ...

                [CONTEXTO]
                [Aqui, escreva um ÚNICO parágrafo de 50 a 100 palavras.
                Inicie contextualizando brevemente (quem fala, para quem, situação).
                Em seguida, faça a reflexão principal. Seja didático: explique o que a passagem realmente significa. Vá "além da curva": qual é a verdade profunda, o princípio eterno por trás das palavras? Como essa verdade confronta nosso comportamento natural e nos chama a um padrão mais alto? Conduza essa reflexão de forma lógica e clara.]

                [PARA PENSAR]

                [Pergunta pessoal e prática que ajude o leitor a examinar sua vida à luz do texto. Use palavras fáceis. - Resuma a pergunta, pois está muito longa e difícil de interpretar durante a leitura. O objetivo é que o leitor pense: "Como isso se aplica à minha vida? O que Deus quer me dizer? O que eu preciso mudar?" - Faça com poucas palavras, cerca de 20 a 30 palavras.]

                [Pergunta que incentive a mudança de atitude ou pensamento. - Resuma a pergunta, pois está muito longa e difícil de interpretar durante a leitura. O objetivo é que o leitor pense: "Como isso se aplica à minha vida? O que Deus quer me dizer? O que eu preciso mudar?" - Faça com poucas palavras, cerca de 20 a 30 palavras.]

                DIRETRIZES ESSENCIAIS DE TOM E CONTEÚDO (NÃO IGNORE):
                Seja Educado e Sábio: A verdade pode ser dura, mas a comunicação deve ser respeitosa. O alvo é restaurar, não esmagar.

                Seja Didático e Claro: Explique o texto como um mestre paciente. Garanta que o entendimento da mensagem central seja inevitável.

                Foque na Graça Transformadora: Não apresente a lei como fim, mas como espelho que nos leva à necessidade de Cristo. Sempre aponte para o perdão e o poder habilitador do Espírito Santo.

                Seja Amoroso, mas Direto: Evite rodeios. Fale a verdade com amor (Efésios 4:15), sem amenizar o seu peso.

                Linguagem Acessível: Use palavras do cotidiano. O objetivo é ser compreendido por todos.

                Seja um Mestre "Fora da Curva": Não se contente com a interpretação superficial. Pergunte-se: "Qual o princípio eterno aqui? Como isso se manifesta na vida moderna? Que área confortável da minha vida essa palavra desafia?".

                Exortação Baseada na Bíblia: Toda correção ou confronto deve fluir diretamente da explicação do texto bíblico. Nada de achismos. A autoridade é da Palavra.

                EXEMPLO DO ESPÍRITO DESEJADO (como você mencionou):
                Ao falar de Davi e seus testes em segredo (leões e ursos), não pare em "Deus treina heróis". Vá além: "Deus usa os desafios ocultos, aqueles que ninguém vê, para forjar em nós uma fé autêntica e uma força que será testemunho público no momento certo. Suas lutas secretas não são em vão; elas são o currículo de Deus para a sua próxima atribuição pública."

                PALAVRA FINAL: Gere um devocional que seja um encontro transformador com a Palavra. Que ele eduque a mente, convença o coração e mobilize a vontade em direção a uma vida que mais se assemelhe a Cristo.
                """.strip(),
            )

        except genai_errors.ServerError as e:
            # 503/5xx: backoff com jitter
            wait = min(60, 2 ** tentativa) + random.uniform(0, 1.5)
            print(f"⚠️ Gemini/ServerError ({getattr(e, 'status_code', '5xx')}): {e}. Retry em {wait:.1f}s...")
            time.sleep(wait)
            continue
        except Exception as e:
            # qualquer outro erro: também tenta mais uma vez, mas sem loop infinito
            wait = min(30, 2 ** tentativa) + random.uniform(0, 1.0)
            print(f"⚠️ Erro inesperado no Gemini: {e}. Retry em {wait:.1f}s...")
            time.sleep(wait)
            continue

        text = getattr(response, "text", None)
        if not text:
            print(f"⚠️ Resposta vazia do Gemini (modelo {model}). Tentando outro...")
            continue

        text = text.strip()

        print("=== TEXTO GERADO PELO GEMINI (INÍCIO) ===")
        print(text)
        print("=== TEXTO GERADO PELO GEMINI (FIM) ===")

        valido, erro = validar_formato_devocional(text)
        if not valido:
            print(f"⚠️ Formato inválido: {erro}. Tentando novamente ({tentativa + 1}/8)...")
            continue

        text = normalizar_formato(text)
        referencia = extrair_referencia(text)

        if ha_sobreposicao(cursor, referencia):
            print(f"⚠️ Versículos com sobreposição: {referencia}. Tentando outro ({tentativa + 1}/8)...")
            continue

        hash_msg = hash_texto(text)
        if hash_ja_usado(cursor, hash_msg):
            print(f"⚠️ Texto/contexto repetido (hash). Tentando outro ({tentativa + 1}/8)...")
            continue

        return text, referencia

    raise RuntimeError("Não consegui gerar um devocional com referência inédita após várias tentativas.")

def verificar_envio_bem_sucedido() -> bool:
    if not SEND_STATUS_PATH.exists():
        return False
    try:
        with open(SEND_STATUS_PATH, "r", encoding="utf-8") as f:
            status = json.load(f)
        return bool(status.get("success", False))
    except:
        return False

def read_send_status() -> dict | None:
    if not SEND_STATUS_PATH.exists():
        return None
    try:
        return json.loads(SEND_STATUS_PATH.read_text(encoding="utf-8"))
    except:
        return None

def job_diario() -> None:
    require_env("GROUP_ID")
    api_key = require_env("GEMINI_API_KEY")

    client = genai.Client(api_key=api_key)
    hoje = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")

    try:
        init_db(conn)
        cursor = conn.cursor()

        if (not TEST_MODE) and ja_enviado_hoje(cursor, hoje):
            print("⚠️ Devocional de hoje já enviado. Encerrando.")
            return

        devocional, referencia = gerar_devocional(client, cursor, hoje)

        texto_final = f"""Olá, irmãos e irmãs!🙏

Pare um instante — há uma Palavra de Deus para vocês hoje:

{devocional}

Reserve um momento pra meditar.
Deus é contigo.🤍
""".strip()

        OUTBOX_PATH.write_text(texto_final, encoding="utf-8")
        print("✅ Mensagem salva em outbox.txt")

        dados = parsear_referencia(referencia)
        hash_msg = hash_texto(devocional)

        if not dados:
            print("⚠️ Não consegui parsear referência. Salvando só o texto.")
            cursor.execute(
                """INSERT INTO devocionais (data, referencia, mensagem, hash_mensagem)
                VALUES (?, ?, ?, ?)""",
                (hoje, referencia, texto_final, hash_msg),
            )
            conn.commit()
            return

        try:
            cursor.execute(
                """INSERT INTO devocionais
                (data, referencia, mensagem, hash_mensagem, livro, capitulo, verso_inicial, verso_final)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (hoje, referencia, texto_final, hash_msg,
                 dados['livro'], dados['capitulo'], dados['verso_inicial'], dados['verso_final'])
            )
            conn.commit()
            print(f"✅ Devocional gerado e salvo. Ref: {referencia}")
        except sqlite3.IntegrityError:
            print("⚠️ Já existe registro para esta data/referência/hash.")
    finally:
        conn.close()

if __name__ == "__main__":
    job_diario()
