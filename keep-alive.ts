import makeWASocket, { DisconnectReason, useMultiFileAuthState } from '@whiskeysockets/baileys'
import { Boom } from '@hapi/boom'

process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

async function keepAlive() {
    console.log('🔄 Iniciando keep-alive da sessão WhatsApp...');

    const { state, saveCreds } = await useMultiFileAuthState("auth_info_baileys");

    const sock = makeWASocket({
        auth: state,
        printQRInTerminal: false,
        connectTimeoutMs: 60000,
        defaultQueryTimeoutMs: 60000,
        keepAliveIntervalMs: 30000,
    });

    let resolved = false;
    let reconnectAttempts = 0;
    const MAX_RECONNECT_ATTEMPTS = 3;

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect } = update;

        if (connection === 'close') {
            const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
            const shouldReconnect = statusCode !== DisconnectReason.loggedOut;

            console.log('❌ Conexão fechada. Código:', statusCode);
            console.log('Erro:', lastDisconnect?.error?.message);

            if (shouldReconnect && !resolved && reconnectAttempts < MAX_RECONNECT_ATTEMPTS) {
                reconnectAttempts++;
                console.log(`🔄 Tentativa de reconexão ${reconnectAttempts}/${MAX_RECONNECT_ATTEMPTS}...`);
                setTimeout(() => keepAlive(), 5000);
            } else {
                console.error('❌ Falha definitiva no keep-alive');
                process.exit(1);
            }
        }
        else if (connection === 'open') {
            console.log('✅ Sessão WhatsApp renovada com sucesso!');
            console.log('📱 Conexão mantida ativa por 10 segundos para sincronização...');

            resolved = true;

            // Mantém conectado por 10 segundos para garantir sync completo
            setTimeout(() => {
                console.log('✅ Keep-alive concluído. Sessão renovada.');
                process.exit(0);
            }, 10000);
        }
    });

    sock.ev.on('creds.update', saveCreds);

    setTimeout(() => {
        if (!resolved) {
            console.error('❌ Timeout de conexão (60s)');
            process.exit(1);
        }
    }, 60000);
}

keepAlive();