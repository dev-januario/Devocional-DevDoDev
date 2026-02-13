import makeWASocket, { DisconnectReason, useMultiFileAuthState, WAMessageStatus } from '@whiskeysockets/baileys'
import { Boom } from '@hapi/boom'
import qrcode from 'qrcode-terminal'
import { readFileSync, writeFileSync } from 'fs'

process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

interface SendStatus {
    success: boolean;
    timestamp: string;
    error?: string;
}

async function connectToWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState("auth_info_baileys");

    const sock = makeWASocket({
        auth: state,
        printQRInTerminal: true,
    });

    let resolved = false;

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update

        if (qr) {
            console.log('📲 Escaneia o QR abaixo com o WhatsApp:')
            qrcode.generate(qr, { small: true })
        }

        if (connection === 'close') {
            const shouldReconnect = (lastDisconnect?.error as Boom)?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log('Conexão fechada:', lastDisconnect?.error?.message);

            if (!resolved) {
                const status: SendStatus = {
                    success: false,
                    timestamp: new Date().toISOString(),
                    error: lastDisconnect?.error?.message || 'Conexão fechada antes do envio'
                };
                writeFileSync('send_status.json', JSON.stringify(status, null, 2));
            }

            if (shouldReconnect && !resolved) {
                console.log('Tentando reconectar...');
                setTimeout(() => connectToWhatsApp(), 5000);
            }
        }
        else if (connection === 'open') {
            console.log('✅ Conexão estabelecida com WhatsApp');

            try {
                const mensagem = readFileSync('outbox.txt', 'utf-8');
                const groupId = process.env.GROUP_ID || '120363424073386097@g.us';

                await sock.groupMetadata(groupId);
                const result = await sock.sendMessage(groupId, { text: mensagem });

                writeFileSync('send_status.json', JSON.stringify({
                    success: true,
                    timestamp: new Date().toISOString()
                }, null, 2));

                try {
                    await waitForDeliveredOrReceipt(sock, result?.key, 30000);
                    console.log('📬 Delivery/receipt confirmado.');
                    console.log('✅ Mensagem enviada com sucesso!');
                } catch (e) {
                    console.warn('⚠️ Não veio receipt a tempo (ok em grupo). Seguindo...');
                }

                resolved = true;
                console.log('Encerrando conexão...');
                await new Promise(r => setTimeout(r, 2000));
                process.exit(0);
            } catch (err) {
                console.error('❌ Erro ao enviar mensagem:', err);

                const status: SendStatus = {
                    success: false,
                    timestamp: new Date().toISOString(),
                    error: err instanceof Error ? err.message : String(err)
                };
                writeFileSync('send_status.json', JSON.stringify(status, null, 2));

                resolved = true;
                process.exit(1);
            }
        }
    });

    function waitForDeliveredOrReceipt(sock: any, msgKey: any, timeoutMs = 60000) {
        return new Promise<void>((resolve, reject) => {
            const done = () => {
                sock.ev.off('messages.update', onMsgUpdate)
                sock.ev.off('message-receipt.update', onReceiptUpdate)
                clearTimeout(t)
            }

            const t = setTimeout(() => {
                done()
                reject(new Error('Timeout esperando delivery/receipt'))
            }, timeoutMs)

            const onMsgUpdate = (updates: any[]) => {
                for (const u of updates) {
                    if (u.key?.id === msgKey?.id && u.key?.remoteJid === msgKey?.remoteJid) {
                        const st = u.update?.status
                        if (typeof st === 'number' && st >= WAMessageStatus.DELIVERY_ACK) {
                            done()
                            resolve()
                        }
                    }
                }
            }

            // Em grupos, o “recebido por quem” costuma vir aqui
            const onReceiptUpdate = (receipts: any[]) => {
                for (const r of receipts) {
                    if (r.key?.id === msgKey?.id) {
                        done()
                        resolve()
                    }
                }
            }

            sock.ev.on('messages.update', onMsgUpdate)
            sock.ev.on('message-receipt.update', onReceiptUpdate)
        })
    }

    sock.ev.on('creds.update', saveCreds);

    setTimeout(() => {
        if (!resolved) {
            console.error('❌ Timeout de conexão');

            const status: SendStatus = {
                success: false,
                timestamp: new Date().toISOString(),
                error: 'Timeout de conexão (30s)'
            };
            writeFileSync('send_status.json', JSON.stringify(status, null, 2));

            process.exit(1);
        }
    }, 30000);
}

connectToWhatsApp();