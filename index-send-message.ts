import makeWASocket, {
    DisconnectReason,
    useMultiFileAuthState,
    WAMessageStatus,
    proto,
    fetchLatestBaileysVersion,
} from '@whiskeysockets/baileys'
import { Boom } from '@hapi/boom'
import qrcode from 'qrcode-terminal'
import { readFileSync, writeFileSync, existsSync } from 'fs'

process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0'

interface SendStatus {
    success: boolean
    timestamp: string
    error?: string
    messageId?: string
    remoteJid?: string
}

const STATUS_FILE = 'send_status.json'
const OUTBOX_FILE = 'outbox.txt'
const AUTH_DIR = 'auth_info_baileys'

function writeStatus(status: SendStatus) {
    writeFileSync(STATUS_FILE, JSON.stringify(status, null, 2))
}

function sleep(ms: number) {
    return new Promise(res => setTimeout(res, ms))
}

async function main() {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)

    const groupId = process.env.GROUP_ID
    if (!groupId) {
        writeStatus({ success: false, timestamp: new Date().toISOString(), error: 'GROUP_ID ausente' })
        process.exit(1)
    }

    const mensagem = readFileSync(OUTBOX_FILE, 'utf-8')

    const maxAttempts = 5
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
        let sock: ReturnType<typeof makeWASocket> | null = null

        try {
            const { version } = await fetchLatestBaileysVersion()

            sock = makeWASocket({
                version,
                auth: state,
                printQRInTerminal: false,
                syncFullHistory: false,

                // 🔧 importantões
                connectTimeoutMs: 60_000,
                defaultQueryTimeoutMs: 60_000,
                keepAliveIntervalMs: 20_000,
                emitOwnEvents: true,
                markOnlineOnConnect: false,
            })

            sock.ev.on('creds.update', saveCreds)

            const opened = await new Promise<void>((resolve, reject) => {
                const t = setTimeout(() => reject(new Error('Timeout aguardando connection.open')), 60_000)

                sock!.ev.on('connection.update', (update) => {
                    const { connection, qr, lastDisconnect } = update

                    if (qr) {
                        console.log('📲 Escaneia o QR abaixo com o WhatsApp:')
                        qrcode.generate(qr, { small: true })
                    }

                    if (connection === 'open') {
                        clearTimeout(t)
                        resolve()
                    }

                    if (connection === 'close') {
                        const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode
                        const msg = lastDisconnect?.error?.message || 'Conexão fechada'
                        clearTimeout(t)

                        // Logged out: não adianta retry sem re-auth
                        if (statusCode === DisconnectReason.loggedOut) {
                            reject(new Error(`LOGGED_OUT: ${msg}`))
                        } else {
                            reject(new Error(msg))
                        }
                    }
                })
            })

            // conexão abriu
            await opened

            // dá uma respirada pro WhatsApp estabilizar a sessão
            await sleep(6000)

            // valida se o grupo existe/acessível
            await sock.groupMetadata(groupId)

            console.log('📤 Enviando mensagem...')
            const result = await sock.sendMessage(groupId, { text: mensagem })

            if (!result?.key) throw new Error('sendMessage retornou sem key')

            writeStatus({
                success: true,
                timestamp: new Date().toISOString(),
                messageId: result.key.id || undefined,
                remoteJid: result.key.remoteJid || groupId,
            })

            console.log('✅ Mensagem enviada com sucesso!')

            // receipt em grupo é instável; tenta, mas não falha o job
            try {
                await waitForDeliveredOrReceipt(sock, result.key, 10_000)
                console.log('📬 Delivery/receipt confirmado.')
            } catch {
                console.warn('⚠️ Sem receipt a tempo (normal em grupo). Seguindo…')
            }

            // fecha bonito e sai
            try { sock.end?.(undefined) } catch { }
            process.exit(0)
        } catch (err: any) {
            const msg = err?.message || String(err)
            console.error(`❌ Tentativa ${attempt}/${maxAttempts} falhou:`, msg)

            try { sock?.end?.(undefined) } catch { }

            if (attempt === maxAttempts) {
                writeStatus({ success: false, timestamp: new Date().toISOString(), error: msg })
                process.exit(1)
            }

            // backoff progressivo
            const backoff = Math.min(30_000, 2000 * attempt * attempt)
            console.log(`🔁 Retry em ${backoff}ms...`)
            await sleep(backoff)
        }
    }
}

function waitForDeliveredOrReceipt(sock: any, msgKey: any, timeoutMs = 7000) {
    return new Promise<void>((resolve, reject) => {
        const cleanup = () => {
            sock.ev.off('messages.update', onMsgUpdate)
            sock.ev.off('message-receipt.update', onReceiptUpdate)
            clearTimeout(t)
        }

        const t = setTimeout(() => {
            cleanup()
            reject(new Error('Timeout esperando delivery/receipt'))
        }, timeoutMs)

        const onMsgUpdate = (updates: any[]) => {
            for (const u of updates) {
                if (u.key?.id === msgKey?.id && u.key?.remoteJid === msgKey?.remoteJid) {
                    const st = u.update?.status
                    if (typeof st === 'number' && st >= WAMessageStatus.DELIVERY_ACK) {
                        cleanup()
                        resolve()
                        return
                    }
                }
            }
        }

        const onReceiptUpdate = (receipts: any[]) => {
            for (const r of receipts) {
                if (r.key?.id === msgKey?.id) {
                    cleanup()
                    resolve()
                    return
                }
            }
        }

        sock.ev.on('messages.update', onMsgUpdate)
        sock.ev.on('message-receipt.update', onReceiptUpdate)
    })
}

main().catch((e) => {
    writeStatus({ success: false, timestamp: new Date().toISOString(), error: String(e) })
    process.exit(1)
})
