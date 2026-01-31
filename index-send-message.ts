import makeWASocket, { DisconnectReason, useMultiFileAuthState } from '@whiskeysockets/baileys';
import { Boom } from '@hapi/boom';
import { readFileSync } from 'fs';
import * as path from 'path';

process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

async function connectToWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState("auth_info_baileys");

    const sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
    });

    let resolved = false;

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect } = update;

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

        if (update.qr) {
            console.error('❌ QR Code detectado - Autenticação necessária');
            console.error('Execute localmente primeiro para gerar auth_info_baileys');
            process.exit(1);
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