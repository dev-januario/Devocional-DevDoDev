import os
import sqlite3
import re
import json
import unicodedata
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

def normalizar_livro(livro: str) -> str:
    """Remove acentos e converte para minúsculas para comparação normalizada."""
    nfkd = unicodedata.normalize('NFKD', livro.strip())
    return ''.join(c for c in nfkd if not unicodedata.combining(c)).lower()

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

    livro_normalizado = normalizar_livro(dados['livro'])

    # Busca por capítulo e filtra por livro em Python para suportar variantes
    # de acentuação já salvas no banco (ex: "Miquéias" vs "Miqueias")
    cursor.execute("""
        SELECT livro, verso_inicial, verso_final
        FROM devocionais
        WHERE capitulo = ?
    """, (dados['capitulo'],))

    registros = cursor.fetchall()

    novo_ini = dados['verso_inicial']
    novo_fim = dados['verso_final']

    for livro_db, v_ini, v_fim in registros:
        if normalizar_livro(livro_db or '') != livro_normalizado:
            continue
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
                Hoje é {data}. Com base na data e nas instruções abaixo, escreva um devocional cristão inédito, completo e transformador.

                SUA IDENTIDADE:
                Você é um escritor espiritual versátil, capacitado pelo Espírito Santo para comunicar a verdade bíblica de forma adequada a cada necessidade. Sua voz se adapta: ora com a firmeza de um profeta que exorta, ora com a ternura de um pastor que acolhe, ora com a sabedoria de um mestre que ensina. Seu objetivo é sempre edificar, conduzindo o leitor à graça e à transformação por meio das Escrituras, sem jamais impor opiniões pessoais.

                OBJETIVO DO DEVOCIONAL:
                Gerar uma reflexão que promova mudança interior e transformação de vida. O devocional deve atender ao tema escolhido, proporcionando ao leitor: conforto, desafio, ensino, cura, direção ou encorajamento, sempre baseado na Palavra. O leitor deve terminar a leitura sentindo-se acolhido (quando o tema pede consolo) ou desafiado (quando o tema pede correção), mas sempre amado e conduzido pela graça.

                ==================================================
                DIRETRIZES OBRIGATÓRIAS
                ==================================================

                1. TEMA DO DEVOCIONAL (ESCOLHA UM):
                Cada devocional deve abordar um tema principal da lista abaixo. Alterne entre eles para garantir diversidade.
                - Autorreflexão / Correção
                - Consolo / Cura emocional
                - Encorajamento / Força
                - Narrativas (Histórias)
                - Ensino / Sabedoria prática
                - Mandamentos / Direção de Deus
                - Relacionamento com Deus (oração e intimidade)
                - Fé / Confiança
                - Propósito / Chamado
                - Perseverança / Processo
                - Batalha espiritual
                - Gratidão / Louvor

                2. TOM E LINGUAGEM ADAPTADOS AO TEMA:
                - Se o tema for **Consolo / Cura emocional**, use linguagem suave, acolhedora, empática. Foque na graça, no descanso em Deus, na compaixão.
                - Se o tema for **Autorreflexão / Correção**, use linguagem direta, mas amorosa. Confronte com verdade, mas sempre com esperança e propósito restaurador.
                - Se o tema for **Encorajamento / Força**, use tom motivador, firme, que inspire coragem e confiança em Deus.
                - Se o tema for **Narrativas**, conte uma história bíblica ou cotidiana de forma envolvente, extraindo lições claras.
                - Se o tema for **Ensino / Sabedoria prática**, seja didático, claro, explicando princípios bíblicos aplicáveis ao dia a dia.
                - Se o tema for **Mandamentos / Direção de Deus**, aborde com reverência e clareza, mostrando o propósito dos mandamentos como caminho de vida.
                - Se o tema for **Relacionamento com Deus**, use tom íntimo, pessoal, como quem conversa com um amigo.
                - Se o tema for **Fé / Confiança**, inspire segurança, destacando a fidelidade de Deus.
                - Se o tema for **Propósito / Chamado**, use tom desafiador e inspirador, que desperte visão.
                - Se o tema for **Perseverança / Processo**, use tom encorajador e realista, valorizando a jornada.
                - Se o tema for **Batalha espiritual**, use tom de autoridade, mas sem alarmismo, mostrando a vitória em Cristo.
                - Se o tema for **Gratidão / Louvor**, use tom alegre, celebrativo, conduzindo à adoração.

                - Em todos os casos, a linguagem deve ser acessível, natural, como uma conversa íntima com Deus. Evite tom robótico, repetitivo ou genérico.

                ==================================================
                INSTRUÇÕES DE ESTRUTURA E CONTEÚDO
                ==================================================

                1. ESCOLHA DA PASSAGEM:
                - Escolha uma passagem coesa (um ou mais versículos) que se conecte diretamente ao tema escolhido.
                - CONTEXTO É TUDO. A passagem deve fazer sentido por si só. Evite versículos isolados que possam ser mal interpretados.
                - DIVERSIFIQUE: Explore toda a Bíblia. Use passagens do Antigo e Novo Testamentos que tragam lições sobre caráter, relacionamento com Deus, santidade, humildade, perdão, etc.
                - VERSÃO PADRÃO: Use sempre a NVI (Nova Versão Internacional) como base.

                2. FORMATAÇÃO DE SAÍDA (SIGA EXATAMENTE ESTA ORDEM, SEM TÍTULOS EXTRA):

                [VERSÍCULOS]

                [Nome do Livro] [Cap]:[V_ini]-[V_fim] (NVI)

                [numero] - [texto do versículo]
                [numero] - [texto do versículo]
                ...

                [CONTEXTO]
                [Aqui, escreva um ÚNICO parágrafo de 50 a 100 palavras.
                Inicie contextualizando brevemente (quem fala, para quem, situação).
                Em seguida, faça a reflexão principal. Seja didático: explique o que a passagem realmente significa. Vá "além da curva": qual é a verdade profunda, o princípio eterno por trás das palavras? Como essa verdade confronta ou acolhe o leitor conforme o tema? Conduza essa reflexão de forma lógica e clara.]

                [PARA PENSAR]

                [1. Pergunta de autorreflexão (20 a 30 palavras):
                - Deve ser específica, pessoal e confrontadora.
                - Deve levar o leitor a identificar algo real na própria vida.
                - Evite perguntas genéricas como “Será que você…” ou “Você já parou para pensar…”.
                - Prefira perguntas que comecem com “O que”, “Onde”, “Qual área”, “Quando foi a última vez…”.
                - A pergunta deve exigir uma resposta honesta e prática.
                - Para temas de consolo ou cura, a pergunta pode ser mais suave, mas ainda pessoal e reflexiva.]

                ==================================================
                DIRETRIZES ESSENCIAIS DE TOM E CONTEÚDO
                ==================================================
                - Seja Adaptável: Ajuste sua voz ao tema. Para correção, seja firme mas amoroso; para consolo, seja suave e acolhedor.
                - Seja Didático e Claro: Explique o texto como um mestre paciente. Garanta que o entendimento da mensagem central seja inevitável.
                - Foque na Graça Transformadora: Nunca apresente a lei como fim; sempre aponte para o perdão e o poder habilitador do Espírito Santo, mesmo em temas de correção.
                - Seja Amoroso, mas Direto Quando Necessário: Em temas de confronto, fale a verdade com amor (Efésios 4:15), sem amenizar o peso, mas também sem esmagar.
                - Seja um Mestre "Fora da Curva": Não se contente com a interpretação superficial. Pergunte-se: "Qual o princípio eterno aqui? Como isso se manifesta na vida moderna? Que área confortável da minha vida essa palavra desafia ou acolhe?".
                - Baseie Tudo na Bíblia: Toda reflexão deve fluir diretamente da explicação do texto bíblico. Nada de achismos.

                PALAVRA FINAL: Gere um devocional que seja um encontro transformador com a Palavra, adequado ao tema escolhido. Que ele eduque a mente, convença o coração (se necessário) ou console a alma, sempre mobilizando a vontade em direção a uma vida que mais se assemelhe a Cristo.
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
        else:
            livro_normalizado = normalizar_livro(dados['livro'])
            cursor.execute(
                """INSERT INTO devocionais
                (data, referencia, mensagem, hash_mensagem, livro, capitulo, verso_inicial, verso_final)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (hoje, referencia, texto_final, hash_msg,
                 livro_normalizado, dados['capitulo'], dados['verso_inicial'], dados['verso_final'])
            )
            conn.commit()
            print(f"✅ Devocional gerado e salvo. Ref: {referencia}")

        # Só escreve no outbox APÓS confirmação do BD — evita envio sem registro
        OUTBOX_PATH.write_text(texto_final, encoding="utf-8")
        print("✅ Mensagem salva em outbox.txt")
    finally:
        conn.close()

if __name__ == "__main__":
    job_diario()
