import os
import sqlite3
import re
import json
import sys
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

# Permite também `python3 main.py TEST_MODE=1` além do padrão `TEST_MODE=1 python3 main.py`
for _arg in sys.argv[1:]:
    if "=" in _arg:
        _chave, _valor = _arg.split("=", 1)
        os.environ[_chave] = _valor

load_dotenv(override=False)

GROUP_ID = os.getenv("GROUP_ID")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
GEMINI_LOCATION = os.getenv("GEMINI_LOCATION", "global")
GEMINI_MODELS = os.getenv("GEMINI_MODELS", "gemini-3.5-flash,gemini-2.5-flash")
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database.db"
OUTBOX_PATH = BASE_DIR / "outbox.txt"
SEND_STATUS_PATH = BASE_DIR / "send_status.json"
NODE_SENDER_PATH = BASE_DIR / "index-send-message.ts"

def criar_cliente_genai() -> genai.Client:
    usar_vertex_env = os.getenv("GOOGLE_GENAI_USE_VERTEXAI")
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    em_github_actions = os.getenv("GITHUB_ACTIONS", "false").lower() == "true"

    if usar_vertex_env is None:
        # Em CI, evita depender de ADC por engano quando só o project foi definido.
        usar_vertex = bool(project) and not em_github_actions
    else:
        usar_vertex = usar_vertex_env == "1"

    if usar_vertex:
        project = project or require_env("GOOGLE_CLOUD_PROJECT")
        location = os.getenv("GOOGLE_CLOUD_LOCATION", GEMINI_LOCATION)
        print(f"ℹ️ Usando Vertex AI em location={location}")
        return genai.Client(vertexai=True, project=project, location=location)

    if project and em_github_actions:
        print("ℹ️ GOOGLE_CLOUD_PROJECT definido no CI sem GOOGLE_GENAI_USE_VERTEXAI=1; usando GEMINI_API_KEY")

    print("ℹ️ Usando Gemini Developer API (GEMINI_API_KEY)")
    api_key = require_env("GEMINI_API_KEY")
    return genai.Client(api_key=api_key)

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
    # Remove colchetes residuais caso o nome do livro venha como "[Gênesis]"
    livro = re.sub(r'^\[|\]$', '', livro).strip()
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

def listar_referencias_recentes(cursor: sqlite3.Cursor, limite: int = 60) -> list[str]:
    cursor.execute(
        """
        SELECT referencia
        FROM devocionais
        WHERE referencia IS NOT NULL AND TRIM(referencia) != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (limite,),
    )
    return [row[0] for row in cursor.fetchall() if row and row[0]]

# Formato do prompt: "📖 *Livro Cap:Vini-Vfim (VERSÃO)*" — emoji e negrito são opcionais pois o Gemini nem sempre inclui
_REF_EMOJI_PAT = re.compile(
    r"^(?:📖\s*)?\*?(?P<livro>.+?)\s+(?P<cap>\d+)\s*:\s*(?P<versos>\d+(?:\s*-\s*\d+)?)\s*\((?P<versao>[^)]+)\)\*?\s*$"
)

def extrair_referencia(texto: str) -> str:
    for line in texto.splitlines():
        m = _REF_EMOJI_PAT.match(line.strip())
        if m:
            livro = m.group("livro").strip()
            cap = m.group("cap").strip()
            versos = re.sub(r"\s*-\s*", "-", m.group("versos").strip())
            versao = m.group("versao").strip()
            return f"{livro} {cap}:{versos} ({versao})"

    raise RuntimeError("Não foi possível extrair a referência bíblica do texto.")

_REFLEXAO_PAT = re.compile(r"^(?:[📝🧠]\s*)?\*?Reflex[aã]o\*?\s*:?\s*$", flags=re.IGNORECASE)
_ORACAO_PAT = re.compile(r"^(?:🙏\s*)?\*?Ora[cç][aã]o\*?\s*:?\s*$", flags=re.IGNORECASE)

def validar_formato_devocional(texto: str) -> tuple[bool, str]:
    linhas = [line.strip() for line in texto.splitlines()]

    referencias_encontradas = [line for line in linhas if _eh_linha_referencia_biblica(line)]
    if len(referencias_encontradas) == 0:
        return False, "Nenhuma referência bíblica encontrada (linha \"Livro Cap:V-V (VERSÃO)\" ausente)"
    if len(referencias_encontradas) > 1:
        return False, f"Múltiplas referências detectadas! Encontrei {len(referencias_encontradas)} referências."

    if not any(_REFLEXAO_PAT.match(line) for line in linhas):
        return False, "Falta a seção Reflexão"

    if not any(_ORACAO_PAT.match(line) for line in linhas):
        return False, "Falta a seção Oração"

    return True, "OK"

def _eh_linha_referencia_biblica(line: str) -> bool:
    return bool(_REF_EMOJI_PAT.match(line.strip()))

def normalizar_formato(texto: str) -> str:
    # Remove indentação acidental (herdada do template do prompt) linha a linha
    linhas_normalizadas = [line.strip() for line in texto.splitlines()]
    return "\n".join(linhas_normalizadas)

def _erro_eh_quota_excedida(err: Exception) -> bool:
    msg = str(err).lower()
    return "429" in msg or "resource_exhausted" in msg or "quota exceeded" in msg

def gerar_devocional(client: genai.Client, cursor: sqlite3.Cursor, data: str) -> tuple[str, str]:
    modelos = [m.strip() for m in GEMINI_MODELS.split(",") if m.strip()]
    if not modelos:
        modelos = ["gemini-3.5-flash"]

    referencias_recentes = listar_referencias_recentes(cursor, limite=60)
    bloqueio_referencias = "\n".join(f"- {r}" for r in referencias_recentes)

    max_tentativas = 12
    tentativas_503 = 0
    modelos_sem_quota: set[str] = set()

    for tentativa in range(max_tentativas):
        modelos_disponiveis = [m for m in modelos if m not in modelos_sem_quota]
        if not modelos_disponiveis:
            raise RuntimeError("Sem quota disponível nos modelos configurados do Gemini. Ajuste GEMINI_MODELS ou cota/faturamento.")

        model = modelos_disponiveis[tentativa % len(modelos_disponiveis)]

        try:
            response = client.models.generate_content(
                model=model,
                contents=f"""
        Hoje é {data}.

        Você é um escritor cristão comprometido com a fidelidade bíblica. Escreva um devocional inédito, curto, acolhedor e edificante, baseado exclusivamente nas Escrituras.

        ## Objetivo
        Gerar um devocional que possa ser lido em aproximadamente 1 minuto, transmitindo uma única mensagem clara e prática.

        ## Escolha da passagem
        - Escolha uma única passagem bíblica coerente com o tema.
        - Utilize sempre a versão NVI.
        - O contexto da passagem deve ser respeitado; nunca utilize versículos fora do seu sentido original.
        - Alterne entre Antigo e Novo Testamento.
        - Diversifique os temas ao longo dos dias.

        ### Temas possíveis
        Escolha apenas um:
        - Autorreflexão
        - Consolo
        - Encorajamento
        - Sabedoria prática
        - Fé
        - Oração
        - Perseverança
        - Gratidão
        - Santidade
        - Propósito
        - Perdão
        - Esperança

        ### Referências proibidas
        Não utilize nenhuma destas referências:

        {bloqueio_referencias or "- Nenhuma"}

        ## Estilo
        - Linguagem simples, natural e acolhedora.
        - Escreva como quem conversa com um irmão na fé.
        - Evite clichês, frases de efeito e repetições.
        - Não faça promessas que a Bíblia não faz.
        - Toda aplicação deve nascer do texto bíblico.
        - Nunca invente informações sobre o contexto da passagem.

        ## Formato (SIGA EXATAMENTE)

        Olá, vamos à Palavra de hoje! 🙏

        📖 *[Livro] [Capítulo]:[V_inicial]-[V_final] (NVI)*

        > [número] "[texto do versículo]"
        > [número] "[texto do versículo]"
        (uma linha de citação por versículo, sempre precedida do seu número, para facilitar identificar qual versículo está sendo lido)

        🧠 *Reflexão*

        Escreva uma reflexão com no máximo 50 palavras.
        Explique a principal verdade da passagem e uma aplicação prática para hoje.

        🙏 *Oração*

        Escreva uma oração com no máximo 30 palavras.
        A oração deve estar relacionada diretamente à reflexão.
        Termine a oração com "Amém." seguido do emoji 🤍.

        ## Restrições
        - Não adicione títulos extras.
        - Não escreva "Contexto", "Para pensar", "Mensagem" ou similares.
        - Use apenas os emojis 🙏, 📖, 🧠 e 🤍, exatamente como no modelo acima.
        - Use negrito (*texto*) na referência bíblica e nos títulos "Reflexão" e "Oração".
        - Use "> " antes de cada linha de versículo (citação em bloco).
        - Não ultrapasse os limites de palavras.
        - A saída deve conter apenas o texto final do devocional.
        """.strip(),
            )

        except genai_errors.ServerError as e:
            tentativas_503 += 1
            wait = min(45, 8 * tentativas_503)
            print(f"⚠️ Servidor ocupado (503). Aguardando {wait}s para tentar novamente...")
            time.sleep(wait)
            continue
        except Exception as e:
            if _erro_eh_quota_excedida(e):
                modelos_sem_quota.add(model)
                print(f"⚠️ Quota esgotada para o modelo {model}. Tentando outro modelo...")
                continue

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

        text = normalizar_formato(text)
        valido, erro = validar_formato_devocional(text)
        if not valido:
            print(f"⚠️ Formato inválido: {erro}. Tentando novamente ({tentativa + 1}/{max_tentativas})...")
            continue

        referencia = extrair_referencia(text)

        if ha_sobreposicao(cursor, referencia):
            print(f"⚠️ Versículos com sobreposição: {referencia}. Tentando outro ({tentativa + 1}/{max_tentativas})...")
            continue

        hash_msg = hash_texto(text)
        if hash_ja_usado(cursor, hash_msg):
            print(f"⚠️ Texto/contexto repetido (hash). Tentando outro ({tentativa + 1}/{max_tentativas})...")
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

    client = criar_cliente_genai()
    hoje = datetime.now().strftime("%Y-%m-%d")
    # Em TEST_MODE, usa uma "data" sintética pra não colidir com o registro real de hoje (UNIQUE)
    data_registro = f"{hoje}-teste-{int(time.time())}" if TEST_MODE else hoje

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

        texto_final = f"""{devocional}""".strip()

        dados = parsear_referencia(referencia)
        hash_msg = hash_texto(devocional)

        if not dados:
            print("⚠️ Não consegui parsear referência. Salvando só o texto.")
            cursor.execute(
                """INSERT INTO devocionais (data, referencia, mensagem, hash_mensagem)
                VALUES (?, ?, ?, ?)""",
                (data_registro, referencia, texto_final, hash_msg),
            )
            conn.commit()
        else:
            livro_normalizado = normalizar_livro(dados['livro'])
            cursor.execute(
                """INSERT INTO devocionais
                (data, referencia, mensagem, hash_mensagem, livro, capitulo, verso_inicial, verso_final)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (data_registro, referencia, texto_final, hash_msg,
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
