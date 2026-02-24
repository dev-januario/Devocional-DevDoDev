import makeWASocket, { DisconnectReason, useMultiFileAuthState, WAMessageStatus, proto, fetchLatestBaileysVersion } from '@whiskeysockets/baileys'
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

function safeReadStatus(): SendStatus | null {
    try {
        if (!existsSync(STATUS_FILE)) return null
        return JSON.parse(readFileSync(STATUS_FILE, 'utf-8'))
    } catch {
        return null
    }
}

async function connectToWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
    const { version } = await fetchLatestBaileysVersion()
    const sock = makeWASocket({
        auth: state,
        version,
        syncFullHistory: false
    })

    let finished = false
    let sent = false
    let lastMessageKey: proto.IMessageKey | undefined

    const overallTimeoutMs = 180_000
    const receiptTimeoutMs = 10_000

    const overallTimer = setTimeout(() => {
        if (!finished && !sent) {
            writeStatus({
                success: false,
                timestamp: new Date().toISOString(),
                error: `Timeout geral(${overallTimeoutMs / 1000}s)`,
            })
            finished = true
            process.exit(1)
        }
    }, overallTimeoutMs)

    const doneExit = (code: number) => {
        if (finished) return
        finished = true

        clearTimeout(overallTimer)
        setTimeout(() => process.exit(code), 200)
    }

    sock.ev.on('creds.update', saveCreds)

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update

        if (qr) {
            console.log('📲 Escaneia o QR abaixo com o WhatsApp:')
            qrcode.generate(qr, { small: true })
        }

        if (connection === 'close') {
            const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode
            const message = lastDisconnect?.error?.message || 'Conexão fechada'

            console.log('Conexão fechada:', message)

            if (!sent) {
                const previous = safeReadStatus()
                if (!previous?.success) {
                    writeStatus({
                        success: false,
                        timestamp: new Date().toISOString(),
                        error: message,
                    })
                }
            }

            const shouldReconnect = statusCode !== DisconnectReason.loggedOut

            if (!sent && shouldReconnect && !finished) {
                console.log('Tentando reconectar em 5s...')
                setTimeout(() => connectToWhatsApp(), 5000)
            } else {
                doneExit(sent ? 0 : 1)
            }
        }

        if (connection === 'open') {
            console.log('✅ Conexão estabelecida com WhatsApp')
            await new Promise(resolve => setTimeout(resolve, 5000))

            try {
                const mensagem = readFileSync(OUTBOX_FILE, 'utf-8')
                const groupId = process.env.GROUP_ID || '120363424073386097@g.us'

                let groupMetadata
                try {
                    groupMetadata = await sock.groupMetadata(groupId)
                    console.log(`📱 Grupo encontrado: ${groupMetadata.subject}`)
                } catch (metaError: any) {
                    throw new Error(`Grupo não encontrado ou inacessível: ${metaError?.message || metaError}`)
                }

                console.log('📤 Enviando mensagem...')
                const result = await sock.sendMessage(groupId, { text: mensagem })

                if (!result?.key) {
                    throw new Error('Resposta inválida do sendMessage (sem key)')
                }

                lastMessageKey = result.key
                sent = true

                writeStatus({
                    success: true,
                    timestamp: new Date().toISOString(),
                    messageId: lastMessageKey.id || undefined,
                    remoteJid: lastMessageKey.remoteJid || groupId,
                })

                console.log('✅ Mensagem enviada com sucesso!')

                try {
                    await waitForDeliveredOrReceipt(sock, lastMessageKey, receiptTimeoutMs)
                    console.log('📬 Delivery/receipt confirmado.')
                } catch {
                    console.warn('⚠️ Sem receipt a tempo (normal em grupo). Seguindo…')
                }

                console.log('🏁 Encerrando...')
                doneExit(0)
            } catch (err: any) {
                console.error('❌ Erro ao enviar mensagem:', err?.message || err)
                console.error('Stack trace:', err?.stack)

                writeStatus({
                    success: false,
                    timestamp: new Date().toISOString(),
                    error: err instanceof Error ? err.message : String(err),
                })

                doneExit(1)
            }
        }
    })

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
}

connectToWhatsApp().catch((e) => {
    console.error('Falha fatal ao iniciar:', e)
    writeFileSync(
        'send_status.json',
        JSON.stringify({
            success: false,
            timestamp: new Date().toISOString(),
            error: String(e)
        },
            null,
            2,
        ),
    )
    process.exit(1)
})