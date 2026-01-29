import makeWASocket, { DisconnectReason, useMultiFileAuthState } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import qrcode from 'qrcode-terminal';
import { readFileSync } from 'fs';

process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

async function connectToWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState("auth_info_baileys");

    const sock = makeWASocket({
        auth: state,
        printQRInTerminal: false
    });

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect } = update;
        if (connection === 'close') {
            const shouldRecconect = (lastDisconnect?.error as Boom)?.output?.statusCode !== DisconnectReason.loggedOut;
            console.log('connection closed due to ', lastDisconnect?.error, ', reconnecting ', shouldRecconect);
            // reconnect if not logged out
            if (shouldRecconect) {
                connectToWhatsApp();
            }
        } else if (connection === 'open') {
            console.log('opened connection');

            try {
                const mensagem = readFileSync('outbox.txt', 'utf-8');
                const groupId = '120363424073386097@g.us';
                await sock.sendMessage(groupId, { text: mensagem });
                console.log('Mensagem enviada com sucesso!');
            } catch (err) {
                console.error('Erro ao enviar mensagem: ', err);
            }
        }

        if (update.qr) {
            qrcode.generate(update.qr, { small: true });
        }
    });

    sock.ev.on('messages.upsert', async (m) => {
        for (let index = 0; index < m.messages.length; index++) {
            const message = m.messages[index];
            const content = message.message?.conversation || message.message?.extendedTextMessage?.text;

            if (content === 'Olá!') {
                // @ts-ignore
                await sock.sendMessage(message.key.remoteJid, { text: 'Olá! Como posso ajudar?' });
            }
        }
    });

    sock.ev.on('creds.update', saveCreds);
}

connectToWhatsApp();