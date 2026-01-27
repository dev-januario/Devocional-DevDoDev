import { existsSync } from 'fs'
import { join, dirname } from 'path'
import { fileURLToPath } from 'url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const authPath = join(__dirname, 'auth', 'creds.json')

if (existsSync(authPath)) {
    console.log('✅ Autenticação encontrada')
    process.exit(0)
} else {
    console.log('❌ Autenticação não encontrada')
    process.exit(1)
}