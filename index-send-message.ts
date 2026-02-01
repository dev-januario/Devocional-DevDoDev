import makeWASocket, { DisconnectReason, useMultiFileAuthState } from '@whiskeysockets/baileys'
import { Boom } from '@hapi/boom'
import qrcode from 'qrcode-terminal'
import { readFileSync } from 'fs'

process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

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

                await sock.sendMessage(groupId, { text: mensagem });
                console.log('✅ Mensagem enviada com sucesso!');

                resolved = true;

                setTimeout(() => {
                    console.log('Encerrando conexão...');
                    process.exit(0);
                }, 3000);

            } catch (err) {
                console.error('❌ Erro ao enviar mensagem:', err);
                process.exit(1);
            }
        }
    });

    sock.ev.on('creds.update', saveCreds);

    setTimeout(() => {
        if (!resolved) {
            console.error('❌ Timeout de conexão');
            process.exit(1);
        }
    }, 30000);
}

connectToWhatsApp();