import sqlite3
import shutil
import argparse
from pathlib import Path
from datetime import datetime


DB_PATH = Path("database.db")
BACKUP_DIR = Path("backups")


def criar_backup() -> Path:
    """Cria backup do banco de dados com timestamp."""
    if not DB_PATH.exists():
        print("⚠️ Nenhum banco de dados encontrado para fazer backup.")
        return None
    
    BACKUP_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"database_backup_{timestamp}.db"
    
    shutil.copy2(DB_PATH, backup_path)
    tamanho = backup_path.stat().st_size
    print(f"✅ Backup criado: {backup_path}")
    print(f"   Tamanho: {tamanho:,} bytes")
    
    return backup_path


def mostrar_estatisticas():
    """Mostra estatísticas do banco de dados atual."""
    if not DB_PATH.exists():
        print("❌ Banco de dados não encontrado.")
        return
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    try:
        # Total de registros
        cursor.execute("SELECT COUNT(*) FROM devocionais")
        total = cursor.fetchone()[0]
        
        print("\n" + "="*50)
        print("📊 ESTATÍSTICAS DO BANCO DE DADOS")
        print("="*50)
        print(f"\n📝 Total de devocionais: {total}")
        
        if total > 0:
            # Primeiro e último devocional
            cursor.execute("SELECT MIN(data), MAX(data) FROM devocionais")
            primeira, ultima = cursor.fetchone()
            print(f"📅 Primeiro devocional: {primeira}")
            print(f"📅 Último devocional: {ultima}")
            
            # Livros mais usados
            cursor.execute("""
                SELECT livro, COUNT(*) as vezes 
                FROM devocionais 
                WHERE livro IS NOT NULL
                GROUP BY livro 
                ORDER BY vezes DESC 
                LIMIT 10
            """)
            
            livros = cursor.fetchall()
            if livros:
                print("\n📖 Top 10 livros mais usados:")
                for i, (livro, vezes) in enumerate(livros, 1):
                    print(f"   {i}. {livro}: {vezes}x")
            
            # Distribuição AT vs NT (simplificado)
            at_livros = [
                "Gênesis", "Êxodo", "Levítico", "Números", "Deuteronômio",
                "Josué", "Juízes", "Rute", "1 Samuel", "2 Samuel",
                "1 Reis", "2 Reis", "1 Crônicas", "2 Crônicas",
                "Esdras", "Neemias", "Ester", "Jó", "Salmos", "Provérbios",
                "Eclesiastes", "Cantares", "Isaías", "Jeremias", "Lamentações",
                "Ezequiel", "Daniel", "Oséias", "Joel", "Amós", "Obadias",
                "Jonas", "Miquéias", "Naum", "Habacuque", "Sofonias",
                "Ageu", "Zacarias", "Malaquias"
            ]
            
            cursor.execute("""
                SELECT livro FROM devocionais WHERE livro IS NOT NULL
            """)
            todos_livros = [row[0] for row in cursor.fetchall()]
            
            at_count = sum(1 for livro in todos_livros if livro in at_livros)
            nt_count = len(todos_livros) - at_count
            
            print(f"\n📊 Distribuição:")
            print(f"   Antigo Testamento: {at_count} ({at_count/total*100:.1f}%)")
            print(f"   Novo Testamento: {nt_count} ({nt_count/total*100:.1f}%)")
        
        # Tamanho do arquivo
        tamanho = DB_PATH.stat().st_size
        print(f"\n💾 Tamanho do arquivo: {tamanho:,} bytes ({tamanho/1024:.2f} KB)")
        
        print("="*50 + "\n")
        
    finally:
        conn.close()


def criar_banco_vazio():
    """Cria um novo banco de dados vazio com a estrutura correta."""
    if DB_PATH.exists():
        DB_PATH.unlink()
        print("🗑️ Banco de dados antigo removido.")
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # Cria tabela
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS devocionais (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT UNIQUE,
            mensagem TEXT,
            referencia TEXT,
            hash_mensagem TEXT,
            livro TEXT,
            capitulo INTEGER,
            verso_inicial INTEGER,
            verso_final INTEGER
        )
    """)
    
    # Cria índice
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_devocionais_hash_unique
        ON devocionais(hash_mensagem)
    """)
    
    conn.commit()
    conn.close()
    
    print("✅ Novo banco de dados criado com sucesso!")
    print(f"📊 Tamanho: {DB_PATH.stat().st_size} bytes")


def confirmar_reset() -> bool:
    """Pede confirmação do usuário."""
    print("\n" + "!"*50)
    print("⚠️  ATENÇÃO: OPERAÇÃO IRREVERSÍVEL")
    print("!"*50)
    print("\nVocê está prestes a RESETAR o banco de dados.")
    print("Todos os devocionais enviados serão APAGADOS.")
    print("\nUm backup será criado automaticamente antes do reset.")
    
    resposta = input("\nDigite 'CONFIRMO' para continuar: ").strip()
    
    return resposta == "CONFIRMO"


def main():
    parser = argparse.ArgumentParser(description="Resetar banco de dados do devocional")
    parser.add_argument(
        "--force", 
        action="store_true", 
        help="Resetar sem pedir confirmação"
    )
    parser.add_argument(
        "--backup-only", 
        action="store_true", 
        help="Apenas criar backup sem resetar"
    )
    parser.add_argument(
        "--show-stats", 
        action="store_true", 
        help="Mostrar estatísticas do banco atual"
    )
    
    args = parser.parse_args()
    
    # Apenas mostrar estatísticas
    if args.show_stats:
        mostrar_estatisticas()
        return
    
    # Apenas backup
    if args.backup_only:
        print("\n📦 Criando backup do banco de dados...")
        backup = criar_backup()
        if backup:
            print(f"\n✅ Backup concluído: {backup}")
        return
    
    # Reset completo
    print("\n🗑️ RESET DO BANCO DE DADOS\n")
    
    # Mostra estatísticas atuais
    if DB_PATH.exists():
        mostrar_estatisticas()
    
    # Pede confirmação (se não for --force)
    if not args.force:
        if not confirmar_reset():
            print("\n❌ Operação cancelada pelo usuário.")
            return
    
    print("\n📦 Criando backup antes do reset...")
    criar_backup()
    
    print("\n🗑️ Resetando banco de dados...")
    criar_banco_vazio()
    
    print("\n" + "="*50)
    print("✅ RESET CONCLUÍDO COM SUCESSO!")
    print("="*50)
    print("\n📋 Próximos passos:")
    print("  1. O próximo devocional será o primeiro registro")
    print("  2. Não haverá verificação de versículos repetidos")
    print("  3. O histórico foi salvo em 'backups/'")
    print("\n💡 Dica: Use --show-stats para ver estatísticas a qualquer momento")
    print("="*50 + "\n")


if __name__ == "__main__":
    main()