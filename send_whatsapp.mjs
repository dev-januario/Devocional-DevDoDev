import makeWASocket, { useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } from '@whiskeysockets/baileys'
import fs from 'fs'
import qrcode from 'qrcode-terminal'
import pino from 'pino'

// Ignorar erro de certificado SSL
process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0'

const [, , ...args] = process.argv
const MODE = args[0]

// Modo de autenticação
if (MODE === '--authenticate' || MODE === '--auth') {
    console.log('🔐 Iniciando autenticação do WhatsApp...')
    await authenticate()
    process.exit(0)
}

// Modo de envio de mensagem
const [groupId, filePath] = args

if (!groupId || !filePath) {
    console.error('Uso:')
    console.error('  Autenticar: node send_whatsapp.mjs --authenticate')
    console.error('  Enviar:     node send_whatsapp.mjs <GROUP_ID> <CAMINHO_ARQUIVO>')
    process.exit(1)
}

await sendMessage()

// ============= FUNÇÕES =============

async function authenticate() {
    let attempts = 0
    const maxAttempts = 3

    while (attempts < maxAttempts) {
        try {
            attempts++
            console.log(`\n🔄 Tentativa ${attempts}/${maxAttempts}\n`)

            const { state, saveCreds } = await useMultiFileAuthState('./auth')
            const { version } = await fetchLatestBaileysVersion()

            const sock = makeWASocket({
                auth: state,
                version,
                printQRInTerminal: false,
                connectTimeoutMs: 120000,
                defaultQueryTimeoutMs: 120000,
                retryRequestDelayMs: 250,
                maxMsgRetryCount: 5,
                logger: pino({ level: 'silent' })
            })

            sock.ev.on('creds.update', saveCreds)

            let qrGenerated = false

            await new Promise((resolve, reject) => {
                const timeout = setTimeout(() => {
                    if (!qrGenerated) {
                        reject(new Error('QR Code não foi gerado em 120 segundos'))
                    } else {
                        reject(new Error('Timeout: QR Code não foi escaneado em 120 segundos'))
                    }
                }, 120000)

                sock.ev.on('connection.update', (update) => {
                    const { connection, qr, lastDisconnect } = update

                    if (qr) {
                        qrGenerated = true
                        console.log('\n📱 Escaneie o QR Code abaixo:\n')
                        qrcode.generate(qr, { small: true })
                        console.log('\n⏳ Aguardando leitura do QR...\n')
                    }

                    if (connection === 'open') {
                        console.log('✅ Autenticação concluída com sucesso!')
                        console.log('✅ Credenciais salvas na pasta ./auth')
                        clearTimeout(timeout)
                        resolve()
                    }

                    if (connection === 'close') {
                        clearTimeout(timeout)
                        const statusCode = lastDisconnect?.error?.output?.statusCode
                        const reason = lastDisconnect?.error?.message || 'Desconhecido'

                        if (statusCode === DisconnectReason.loggedOut) {
                            reject(new Error('Você foi desconectado. Tente novamente.'))
                        } else {
                            reject(new Error(`Conexão fechada: ${reason}`))
                        }
                    }
                })
            })

            await sock.end()
            console.log('\n✅ Autenticação finalizada com sucesso!')
            return

        } catch (error) {
            console.error(`❌ Tentativa ${attempts} falhou:`, error.message)

            if (attempts < maxAttempts) {
                console.log('⏳ Aguardando 5 segundos antes de tentar novamente...\n')
                await new Promise(resolve => setTimeout(resolve, 5000))
            }
        }
    }

    console.error('\n❌ Todas as tentativas falharam.')
    console.log('\n💡 Possíveis soluções:')
    console.log('   1. Trocar de rede (use 4G do celular compartilhado)')
    console.log('   2. Desabilitar proxy/VPN corporativo')
    console.log('   3. Verificar firewall\n')
    process.exit(1)
}

async function sendMessage() {
    try {
        console.log('🔧 Iniciando conexão com WhatsApp...')

        const { state, saveCreds } = await useMultiFileAuthState('./auth')
        const { version } = await fetchLatestBaileysVersion()

        const sock = makeWASocket({
            auth: state,
            version,
            printQRInTerminal: false,
            connectTimeoutMs: 180000,
            defaultQueryTimeoutMs: 180000,
            retryRequestDelayMs: 500,
            maxMsgRetryCount: 10,
            logger: pino({ level: 'silent' })
        })

        sock.ev.on('creds.update', saveCreds)

        let connectionEstablished = false

        await new Promise((resolve, reject) => {
            const timeout = setTimeout(() => {
                reject(new Error('Timeout na conexão após 3 minutos'))
            }, 180000)

            sock.ev.on('connection.update', (update) => {
                const { connection, lastDisconnect } = update

                if (connection === 'connecting') {
                    console.log('🔄 Conectando...')
                }

                if (connection === 'open') {
                    console.log('✅ Conectado ao WhatsApp')
                    connectionEstablished = true
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

        console.log('📖 Lendo mensagem do arquivo...')
        const mensagem = fs.readFileSync(filePath, 'utf-8')

        if (!mensagem || mensagem.trim().length === 0) {
            throw new Error('Arquivo de mensagem está vazio')
        }

        console.log('📤 Enviando mensagem...')
        await sock.sendMessage(groupId, { text: mensagem })
        console.log('✅ Mensagem enviada com sucesso!')

        await new Promise(resolve => setTimeout(resolve, 2000))
        await sock.end()
        console.log('✅ Processo concluído')
        process.exit(0)

    } catch (error) {
        console.error('❌ Erro:', error.message)

        if (error.message.includes('Sessão expirou') || error.message.includes('loggedOut')) {
            console.log('\n💡 Execute: rm -rf auth/ && node send_whatsapp.mjs --authenticate\n')
        }

        process.exit(1)
    }
}