import makeWASocket, {
    useMultiFileAuthState,
    DisconnectReason,
    fetchLatestBaileysVersion,
    BufferJSON
} from '@whiskeysockets/baileys'
import fs from 'fs'
import qrcode from 'qrcode-terminal'
import pino from 'pino'
import path from 'path'
import { fileURLToPath } from 'url'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

const AUTH_DIR = path.join(__dirname, 'auth')
const CREDS_PATH = path.join(AUTH_DIR, 'creds.json')

// ===== util =====
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function ensureAuthDir() {
    fs.mkdirSync(AUTH_DIR, { recursive: true })
}

function safeReadJSON(p) {
    try {
        if (!fs.existsSync(p)) return null
        const raw = fs.readFileSync(p, 'utf-8')
        if (!raw || raw.trim().length < 2) return null
        return JSON.parse(raw)
    } catch {
        return null
    }
}

function isSessionReady(creds) {
    if (!creds) return false
    const hasMe = Boolean(creds?.me?.id)
    const reg = creds?.registered
    const registeredOk = (typeof reg === 'boolean') ? reg === true : true
    return hasMe && registeredOk
}

async function waitSessionReadyFromStateOrFile(state, { timeoutMs = 120000 } = {}) {
    const start = Date.now()
    while (Date.now() - start < timeoutMs) {
        if (isSessionReady(state?.creds)) return true
        const fileCreds = safeReadJSON(CREDS_PATH)
        if (isSessionReady(fileCreds)) return true
        await sleep(400)
    }
    return false
}

function hasValidCredsFile() {
    const json = safeReadJSON(CREDS_PATH)
    return isSessionReady(json)
}

// Persistência manual, atômica
function persistCredsManual(creds) {
    const tmp = CREDS_PATH + '.tmp'
    const payload = JSON.stringify(creds, BufferJSON.replacer, 2)
    fs.writeFileSync(tmp, payload, 'utf-8')
    fs.renameSync(tmp, CREDS_PATH)
}

// espera até o creds.json ficar "pronto" ou dar timeout
async function waitSessionReady({ timeoutMs = 120000 } = {}) {
    const start = Date.now()
    while (Date.now() - start < timeoutMs) {
        const creds = safeReadJSON(CREDS_PATH)
        if (isSessionReady(creds)) return true
        await sleep(350)
    }
    return false
}

// ===== main =====
const [, , ...args] = process.argv
const MODE = args[0]

ensureAuthDir()

if (MODE === '--authenticate' || MODE === '--auth') {
    console.log('🔐 Iniciando autenticação do WhatsApp...')
    await authenticate()
    await sleep(300)
    process.exit(0)
}

const [groupId, filePath] = args
if (!groupId || !filePath) {
    console.error('Uso:')
    console.error('  Autenticar: node send_whatsapp.mjs --authenticate')
    console.error('  Enviar:     node send_whatsapp.mjs <GROUP_ID> <CAMINHO_ARQUIVO>')
    process.exit(1)
}

await sendMessage({ groupId, filePath })
await sleep(300)
process.exit(0)

// ============= FUNÇÕES =============

async function authenticate() {
    const maxAttempts = 3

    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
        let sock = null

        try {
            console.log(`\n🔄 Tentativa ${attempt}/${maxAttempts}\n`)

            const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
            const { version } = await fetchLatestBaileysVersion()

            sock = makeWASocket({
                auth: state,
                version,
                printQRInTerminal: false,
                connectTimeoutMs: 240000,
                defaultQueryTimeoutMs: 240000,
                retryRequestDelayMs: 1000,
                maxMsgRetryCount: 10,
                keepAliveIntervalMs: 15000,
                logger: pino({ level: 'warn' })
            })

            let qrShown = false
            let opened = false

            sock.ev.on('creds.update', async () => {
                try { await saveCreds() } catch { }
                try { persistCredsManual(state.creds) } catch { }
            })

            const done = await new Promise((resolve, reject) => {
                const hardTimeout = setTimeout(() => {
                    reject(new Error('Timeout: não concluiu sincronização a tempo.'))
                }, 180000) // 3 min

                sock.ev.on('connection.update', (u) => {
                    const { connection, qr, lastDisconnect } = u

                    if (qr) {
                        qrShown = true
                        console.log('\n📱 Escaneie o QR Code abaixo:\n')
                        qrcode.generate(qr, { small: true })
                        console.log('\n⏳ Mantém o WhatsApp aberto até concluir a sincronização…\n')
                    }

                    if (connection === 'open') {
                        opened = true
                        console.log('✅ Conectou. Aguardando sincronização/chaves...')
                    }

                    if (connection === 'close') {
                        clearTimeout(hardTimeout)

                        const statusCode = lastDisconnect?.error?.output?.statusCode
                        const reasonNode = lastDisconnect?.error?.output?.payload?.reason || lastDisconnect?.error?.message
                        const reason = lastDisconnect?.error?.message || 'Desconhecido'

                        // conflitos comuns
                        if (reason?.includes('replaced') || reasonNode?.includes?.('replaced')) {
                            reject(new Error('Conflito: sessão foi substituída (replaced). Tem outro processo/instância conectada.'))
                            return
                        }

                        if (statusCode === DisconnectReason.loggedOut) {
                            reject(new Error('loggedOut/device_removed: sessão invalidada. Apaga auth/ e autentica de novo.'))
                            return
                        }

                        reject(new Error(`Conexão fechada: ${reason}`))
                    }
                })

                    ; (async () => {
                        // Espera ficar pronto DE VERDADE (state ou arquivo), mesmo que nem apareça QR nessa tentativa
                        const ok = await waitSessionReadyFromStateOrFile(state, { timeoutMs: 180000 })
                        clearTimeout(hardTimeout)
                        resolve(ok)
                    })().catch((e) => {
                        clearTimeout(hardTimeout)
                        reject(e)
                    })
            })

            if (!done) {
                throw new Error(
                    opened
                        ? 'Conectou, mas não concluiu sincronização (sem me.id).'
                        : (qrShown ? 'QR foi mostrado, mas não concluiu.' : 'Nem QR nem sync concluíram.')
                )
            }

            console.log('✅ Autenticação finalizada e sessão pronta!')
            await sock.end()
            await sleep(400)
            return

        } catch (error) {
            console.error(`❌ Tentativa ${attempt} falhou:`, error?.message || error)

            // sempre tenta encerrar o socket
            try { await sock?.end() } catch { }

            if (attempt < maxAttempts) {
                console.log('⏳ Aguardando 5 segundos antes de tentar novamente...\n')
                await sleep(5000)
            }
        }
    }

    console.error('\n❌ Todas as tentativas falharam.')
    console.log('\n💡 Pra resolver de vez (na moral):')
    console.log('   1) Mata qualquer node antigo rodando (replaced = isso)')
    console.log('   2) rm -rf auth/ e autentica de novo')
    console.log('   3) Faz o pareamento no hotspot 4G (515 = rede derrubando stream)\n')
    process.exit(1)
}

async function sendMessage({ groupId, filePath }) {
    try {
        if (!hasValidCredsFile()) {
            const c = safeReadJSON(CREDS_PATH)
            throw new Error(
                `Sem sessão válida. (me.id=${c?.me?.id || 'null'} registered=${c?.registered}) Rode: node send_whatsapp.mjs --authenticate`
            )
        }

        console.log('🔧 Iniciando conexão com WhatsApp...')

        const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
        const { version } = await fetchLatestBaileysVersion()

        const sock = makeWASocket({
            auth: state,
            version,
            printQRInTerminal: false,
            connectTimeoutMs: 240000,
            defaultQueryTimeoutMs: 240000,
            retryRequestDelayMs: 1000,
            maxMsgRetryCount: 10,
            keepAliveIntervalMs: 15000,
            logger: pino({ level: 'warn' })
        })

        sock.ev.on('creds.update', async () => {
            try { await saveCreds() } catch { }
            try { persistCredsManual(state.creds) } catch { }
        })

        await new Promise((resolve, reject) => {
            const timeout = setTimeout(() => reject(new Error('Timeout na conexão após 4 minutos')), 240000)

            sock.ev.on('connection.update', (update) => {
                const { connection, lastDisconnect, qr } = update

                if (qr) {
                    clearTimeout(timeout)
                    reject(new Error('Sessão inválida: pediu QR no modo envio. Rode --authenticate de novo.'))
                    return
                }

                if (connection === 'open') {
                    clearTimeout(timeout)
                    resolve()
                }

                if (connection === 'close') {
                    clearTimeout(timeout)
                    const statusCode = lastDisconnect?.error?.output?.statusCode
                    const reason = lastDisconnect?.error?.message || 'Desconhecido'

                    if (statusCode === DisconnectReason.loggedOut) {
                        reject(new Error('Sessão expirou. Execute: rm -rf auth/ && node send_whatsapp.mjs --authenticate'))
                    } else {
                        reject(new Error(`Erro de conexão: ${reason}`))
                    }
                }
            })
        })

        console.log('✅ Conectado ao WhatsApp')

        console.log('📖 Lendo mensagem do arquivo...')
        const mensagem = fs.readFileSync(filePath, 'utf-8')
        if (!mensagem || mensagem.trim().length === 0) throw new Error('Arquivo de mensagem está vazio')

        console.log('📤 Enviando mensagem...')
        await sock.sendMessage(groupId, { text: mensagem })
        console.log('✅ Mensagem enviada com sucesso!')

        try { await saveCreds() } catch { }
        try { persistCredsManual(state.creds) } catch { }
        await sleep(800)

        await sock.end()
        await sleep(300)
        console.log('✅ Processo concluído')
    } catch (error) {
        console.error('❌ Erro:', error?.message || error)
        process.exit(1)
    }
}
