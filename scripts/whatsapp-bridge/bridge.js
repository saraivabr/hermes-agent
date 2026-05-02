#!/usr/bin/env node
/**
 * EMPRESA.IA WhatsApp Bridge
 *
 * Standalone Node.js process that connects to WhatsApp via Baileys
 * and exposes HTTP endpoints for the Python gateway adapter.
 *
 * Endpoints (matches gateway/platforms/whatsapp.py expectations):
 *   GET  /messages       - Long-poll for new incoming messages
 *   POST /send           - Send a message { chatId, message, replyTo? }
 *   POST /edit           - Edit a sent message { chatId, messageId, message }
 *   POST /send-media     - Send media natively { chatId, filePath, mediaType?, caption?, fileName? }
 *   POST /typing         - Send typing indicator { chatId }
 *   POST /presence       - Send presence { chatId, presence }
 *   POST /read           - Mark message/chat read { chatId, messageId?, participant? }
 *   POST /react          - React to a message { chatId, messageId, emoji, participant? }
 *   POST /chat/modify    - Archive/mute/mark unread/star { chatId, action, ... }
 *   GET  /profile/:id    - Fetch profile/status/business info
 *   GET  /chat/:id       - Get chat info
 *   POST /groups/create  - Create group { subject, participants[] }
 *   POST /groups/subject - Change group name { chatId, subject }
 *   POST /groups/photo   - Change group photo { chatId, filePath }
 *   POST /groups/participants - Add/remove/promote/demote { chatId, participants[], action }
 *   POST /groups/description - Change group description { chatId, description }
 *   POST /groups/settings - Change group settings { chatId, setting }
 *   POST /groups/invite  - Get/revoke invite link { chatId, revoke? }
 *   GET  /health         - Health check
 *
 * Usage:
 *   node bridge.js --port 3000 --session ~/.hermes/whatsapp/session
 */

import { makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, downloadMediaMessage } from '@whiskeysockets/baileys';
import express from 'express';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import path from 'path';
import { mkdirSync, readFileSync, writeFileSync, appendFileSync, existsSync, readdirSync } from 'fs';
import { randomBytes } from 'crypto';
import qrcode from 'qrcode-terminal';
import { matchesAllowedUser, parseAllowedUsers } from './allowlist.js';

// Parse CLI args
const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : defaultVal;
}

const WHATSAPP_DEBUG =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_DEBUG === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_DEBUG.toLowerCase());

const PORT = parseInt(getArg('port', '3000'), 10);
const SESSION_DIR = getArg('session', path.join(process.env.HOME || '~', '.hermes', 'whatsapp', 'session'));
const IMAGE_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'image_cache');
const DOCUMENT_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'document_cache');
const AUDIO_CACHE_DIR = path.join(process.env.HOME || '~', '.hermes', 'audio_cache');
const HISTORY_DIR = getArg('history', process.env.WHATSAPP_HISTORY_DIR || path.join(process.env.HOME || '~', '.hermes', 'whatsapp', 'history'));
const HISTORY_FILE = path.join(HISTORY_DIR, 'messages.jsonl');
const PAIR_ONLY = args.includes('--pair-only');
const WHATSAPP_MODE = getArg('mode', process.env.WHATSAPP_MODE || 'self-chat'); // "bot" or "self-chat"
const READ_ONLY = ['1', 'true', 'yes', 'on'].includes(String(process.env.WHATSAPP_READ_ONLY || '').toLowerCase());
const ALLOWED_USERS = parseAllowedUsers(process.env.WHATSAPP_ALLOWED_USERS || '');
const DEFAULT_REPLY_PREFIX = '⚕ *EMPRESA.IA*\n────────────\n';
const REPLY_PREFIX = process.env.WHATSAPP_REPLY_PREFIX === undefined
  ? DEFAULT_REPLY_PREFIX
  : process.env.WHATSAPP_REPLY_PREFIX.replace(/\\n/g, '\n');

function formatOutgoingMessage(message) {
  // In bot mode, messages come from a different number so the prefix is
  // redundant — the sender identity is already clear.  Only prepend in
  // self-chat mode where bot and user share the same number.
  if (WHATSAPP_MODE !== 'self-chat') return message;
  return REPLY_PREFIX ? `${REPLY_PREFIX}${message}` : message;
}

function normalizeWhatsAppId(value) {
  if (!value) return '';
  return String(value).replace(':', '@');
}

function normalizeParticipantJid(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  if (raw.endsWith('@s.whatsapp.net') || raw.endsWith('@lid')) return raw;
  if (raw.includes('@')) return raw;
  const digits = raw.replace(/\D/g, '');
  return digits ? `${digits}@s.whatsapp.net` : '';
}

function buildMessageKey(chatId, messageId, participant, fromMe = false) {
  if (!chatId || !messageId) return null;
  const key = { remoteJid: chatId, id: String(messageId), fromMe: !!fromMe };
  if (participant) key.participant = normalizeWhatsAppId(participant);
  return key;
}

function buildQuotedMessage(chatId, replyTo, participant) {
  if (!replyTo) return null;
  if (typeof replyTo === 'object') {
    const key = replyTo.key || buildMessageKey(
      replyTo.chatId || chatId,
      replyTo.messageId || replyTo.id,
      replyTo.participant || participant,
      !!replyTo.fromMe,
    );
    if (!key?.id) return null;
    return {
      key,
      message: replyTo.message || { conversation: replyTo.text || '' },
    };
  }
  const key = buildMessageKey(chatId, replyTo, participant, false);
  if (!key?.id) return null;
  return { key, message: { conversation: '' } };
}

function normalizeParticipantList(values) {
  if (!Array.isArray(values)) return [];
  return [...new Set(values.map(normalizeParticipantJid).filter(Boolean))];
}

function requireConnected(res) {
  if (!sock || connectionState !== 'connected') {
    res.status(503).json({ error: 'Not connected to WhatsApp' });
    return false;
  }
  return true;
}

function requireGroupChatId(chatId, res) {
  if (!chatId || !String(chatId).endsWith('@g.us')) {
    res.status(400).json({ error: 'Valid WhatsApp group chatId ending in @g.us is required' });
    return false;
  }
  return true;
}

function requireWritable(res) {
  if (READ_ONLY) {
    res.status(403).json({ error: 'Bridge is read-only' });
    return false;
  }
  return true;
}

function getMessageContent(msg) {
  const content = msg?.message || {};
  if (content.ephemeralMessage?.message) return content.ephemeralMessage.message;
  if (content.viewOnceMessage?.message) return content.viewOnceMessage.message;
  if (content.viewOnceMessageV2?.message) return content.viewOnceMessageV2.message;
  if (content.documentWithCaptionMessage?.message) return content.documentWithCaptionMessage.message;
  if (content.templateMessage?.hydratedTemplate) return content.templateMessage.hydratedTemplate;
  if (content.buttonsMessage) return content.buttonsMessage;
  if (content.listMessage) return content.listMessage;
  return content;
}

function getContextInfo(messageContent) {
  if (!messageContent || typeof messageContent !== 'object') return {};
  for (const value of Object.values(messageContent)) {
    if (value && typeof value === 'object' && value.contextInfo) {
      return value.contextInfo;
    }
  }
  return {};
}

mkdirSync(SESSION_DIR, { recursive: true });
mkdirSync(HISTORY_DIR, { recursive: true });

function loadStoredMessageIds() {
  const ids = new Set();
  if (!existsSync(HISTORY_FILE)) return ids;
  try {
    const raw = readFileSync(HISTORY_FILE, 'utf8');
    for (const line of raw.split('\n')) {
      if (!line.trim()) continue;
      try {
        const rec = JSON.parse(line);
        if (rec?.messageId) ids.add(String(rec.messageId));
      } catch {}
    }
  } catch (err) {
    console.error('[bridge] Failed to load WhatsApp history index:', err.message);
  }
  return ids;
}

const storedMessageIds = loadStoredMessageIds();

function normalizeTimestamp(ts) {
  if (!ts) return Math.floor(Date.now() / 1000);
  if (typeof ts === 'number') return ts;
  if (typeof ts === 'string') {
    const n = Number(ts);
    return Number.isFinite(n) ? n : Math.floor(Date.now() / 1000);
  }
  if (typeof ts === 'object') {
    if (typeof ts.low === 'number') return ts.low;
    if (typeof ts.toNumber === 'function') {
      try { return ts.toNumber(); } catch {}
    }
  }
  return Math.floor(Date.now() / 1000);
}

function storeHistoryEvent(event) {
  if (!event?.messageId) return;
  if (storedMessageIds.has(String(event.messageId))) return;
  const record = {
    schemaVersion: 1,
    capturedAt: new Date().toISOString(),
    messageId: event.messageId,
    chatId: event.chatId,
    chatName: event.chatName,
    isGroup: !!event.isGroup,
    senderId: event.senderId,
    senderName: event.senderName,
    body: event.body || '',
    hasMedia: !!event.hasMedia,
    mediaType: event.mediaType || '',
    mediaUrls: event.mediaUrls || [],
    mentionedIds: event.mentionedIds || [],
    quotedParticipant: event.quotedParticipant || '',
    fromMe: !!event.fromMe,
    timestamp: normalizeTimestamp(event.timestamp),
  };
  try {
    appendFileSync(HISTORY_FILE, `${JSON.stringify(record)}\n`, 'utf8');
    storedMessageIds.add(String(event.messageId));
  } catch (err) {
    console.error('[bridge] Failed to store WhatsApp history event:', err.message);
  }
}

function readHistoryRecords({ chatId, q, limit = 100, before, after, isGroup } = {}) {
  if (!existsSync(HISTORY_FILE)) return [];
  const max = Math.max(1, Math.min(parseInt(limit || 100, 10) || 100, 1000));
  const query = q ? String(q).toLowerCase() : '';
  const beforeTs = before ? Number(before) : null;
  const afterTs = after ? Number(after) : null;
  const out = [];
  try {
    const lines = readFileSync(HISTORY_FILE, 'utf8').split('\n');
    for (let i = lines.length - 1; i >= 0; i--) {
      const line = lines[i]?.trim();
      if (!line) continue;
      let rec;
      try { rec = JSON.parse(line); } catch { continue; }
      if (chatId && rec.chatId !== chatId) continue;
      if (isGroup !== undefined && String(rec.isGroup) !== String(isGroup)) continue;
      if (query) {
        const haystack = `${rec.body || ''} ${rec.senderName || ''} ${rec.chatName || ''}`.toLowerCase();
        if (!haystack.includes(query)) continue;
      }
      if (Number.isFinite(beforeTs) && !(Number(rec.timestamp) < beforeTs)) continue;
      if (Number.isFinite(afterTs) && !(Number(rec.timestamp) > afterTs)) continue;
      out.push(rec);
      if (out.length >= max) break;
    }
  } catch (err) {
    console.error('[bridge] Failed to read WhatsApp history:', err.message);
  }
  return out.reverse();
}

function listHistoryChats() {
  const byChat = new Map();
  for (const rec of readHistoryRecords({ limit: 1000 })) {
    const prev = byChat.get(rec.chatId) || { chatId: rec.chatId, chatName: rec.chatName, isGroup: rec.isGroup, messageCount: 0, lastTimestamp: 0 };
    prev.chatName = rec.chatName || prev.chatName;
    prev.isGroup = rec.isGroup;
    prev.messageCount += 1;
    prev.lastTimestamp = Math.max(prev.lastTimestamp || 0, Number(rec.timestamp) || 0);
    byChat.set(rec.chatId, prev);
  }
  return Array.from(byChat.values()).sort((a, b) => (b.lastTimestamp || 0) - (a.lastTimestamp || 0));
}

// Build LID → phone reverse map from session files (lid-mapping-{phone}.json)
function buildLidMap() {
  const map = {};
  try {
    for (const f of readdirSync(SESSION_DIR)) {
      const m = f.match(/^lid-mapping-(\d+)\.json$/);
      if (!m) continue;
      const phone = m[1];
      const lid = JSON.parse(readFileSync(path.join(SESSION_DIR, f), 'utf8'));
      if (lid) map[String(lid)] = phone;
    }
  } catch {}
  return map;
}
let lidToPhone = buildLidMap();

const logger = pino({
  level: process.env.WHATSAPP_BAILEYS_LOG_LEVEL || (WHATSAPP_DEBUG ? 'debug' : 'error'),
  redact: {
    paths: [
      '*.auth',
      '*.creds',
      '*.state',
      '*.currentRatchet',
      '*.ephemeralKeyPair',
      '*.pendingPreKey',
      '*.indexInfo',
      '*.rootKey',
      '*.privKey',
      '*.baseKey',
      '*.remoteIdentityKey',
      'fullErrorNode',
      '*.fullErrorNode',
    ],
    censor: '[REDACTED]',
  },
});

// Message queue for polling
const messageQueue = [];
const MAX_QUEUE_SIZE = 100;

// Track recently sent message IDs to prevent echo-back loops with media
const recentlySentIds = new Set();
const MAX_RECENT_IDS = 50;

let sock = null;
let connectionState = 'disconnected';
const reconnectCounts = {};
let lastDisconnectReason = null;

async function startSocket() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: ['EMPRESA.IA', 'Chrome', '120.0'],
    // Best-effort backfill: ask WhatsApp/Baileys for full history when the
    // linked-device session allows it. WhatsApp may still return only a subset.
    syncFullHistory: process.env.WHATSAPP_SYNC_FULL_HISTORY === 'false' ? false : true,
    markOnlineOnConnect: false,
    // Required for Baileys 7.x: without this, incoming messages that need
    // E2EE session re-establishment are silently dropped (msg.message === null)
    getMessage: async (key) => {
      // We don't maintain a message store, so return a placeholder.
      // This is enough for Baileys to complete the retry handshake.
      return { conversation: '' };
    },
  });

  sock.ev.on('creds.update', () => { saveCreds(); lidToPhone = buildLidMap(); });

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      console.log('\n📱 Scan this QR code with WhatsApp on your phone:\n');
      qrcode.generate(qr, { small: true });
      console.log('\nWaiting for scan...\n');
    }

    if (connection === 'close') {
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      connectionState = 'disconnected';
      lastDisconnectReason = reason || 'unknown';
      reconnectCounts[lastDisconnectReason] = (reconnectCounts[lastDisconnectReason] || 0) + 1;

      if (reason === DisconnectReason.loggedOut) {
        console.log('❌ Logged out. Delete session and restart to re-authenticate.');
        process.exit(1);
      } else {
        // 515 = restart requested (common after pairing). Always reconnect.
        if (reason === 515) {
          console.log('↻ WhatsApp requested restart (code 515). Reconnecting...');
        } else {
          console.log(`⚠️  Connection closed (reason: ${reason}). Reconnecting in 3s...`);
        }
        setTimeout(startSocket, reason === 515 ? 1000 : 3000);
      }
    } else if (connection === 'open') {
      connectionState = 'connected';
      console.log('✅ WhatsApp connected!');
      if (PAIR_ONLY) {
        console.log('✅ Pairing complete. Credentials saved.');
        // Give Baileys a moment to flush creds, then exit cleanly
        setTimeout(() => process.exit(0), 2000);
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    // In self-chat mode, your own messages commonly arrive as 'append' rather
    // than 'notify'. Accept both and filter agent echo-backs below.
    if (type !== 'notify' && type !== 'append') return;

    const botIds = Array.from(new Set([
      normalizeWhatsAppId(sock.user?.id),
      normalizeWhatsAppId(sock.user?.lid),
    ].filter(Boolean)));

    for (const msg of messages) {
      if (!msg.message) continue;

      const chatId = msg.key.remoteJid;
      if (WHATSAPP_DEBUG) {
        try {
          console.log(JSON.stringify({
            event: 'upsert', type,
            fromMe: !!msg.key.fromMe, chatId,
            senderId: msg.key.participant || chatId,
            messageKeys: Object.keys(msg.message || {}),
          }));
        } catch {}
      }
      const senderId = msg.key.participant || chatId;
      const isGroup = chatId.endsWith('@g.us');
      const senderNumber = senderId.replace(/@.*/, '');

      // Handle fromMe messages based on mode
      if (msg.key.fromMe) {
        if (isGroup || chatId.includes('status')) continue;

        if (WHATSAPP_MODE === 'bot') {
          // Bot mode: separate number. ALL fromMe are echo-backs of our own replies — skip.
          continue;
        }

        // Self-chat mode: only allow messages in the user's own self-chat
        // WhatsApp now uses LID (Linked Identity Device) format: 67427329167522@lid
        // AND classic format: 34652029134@s.whatsapp.net
        // sock.user has both: { id: "number:10@s.whatsapp.net", lid: "lid_number:10@lid" }
        const myNumber = (sock.user?.id || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const myLid = (sock.user?.lid || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const chatNumber = chatId.replace(/@.*/, '');
        const isSelfChat = (myNumber && chatNumber === myNumber) || (myLid && chatNumber === myLid);
        if (!isSelfChat) continue;
      }

      // Check allowlist for messages from others (resolve LID ↔ phone aliases)
      if (!msg.key.fromMe && !matchesAllowedUser(senderId, ALLOWED_USERS, SESSION_DIR)) {
        try {
          console.log(JSON.stringify({
            event: 'ignored',
            reason: 'allowlist_mismatch',
            chatId,
            senderId,
          }));
        } catch {}
        continue;
      }

      const messageContent = getMessageContent(msg);
      const contextInfo = getContextInfo(messageContent);
      const mentionedIds = Array.from(new Set((contextInfo?.mentionedJid || []).map(normalizeWhatsAppId).filter(Boolean)));
      const quotedParticipant = normalizeWhatsAppId(contextInfo?.participant || contextInfo?.remoteJid || '');

      // Extract message body
      let body = '';
      let hasMedia = false;
      let mediaType = '';
      const mediaUrls = [];

      if (messageContent.conversation) {
        body = messageContent.conversation;
      } else if (messageContent.extendedTextMessage?.text) {
        body = messageContent.extendedTextMessage.text;
      } else if (messageContent.imageMessage) {
        body = messageContent.imageMessage.caption || '';
        hasMedia = true;
        mediaType = 'image';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = messageContent.imageMessage.mimetype || 'image/jpeg';
          const extMap = { 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif' };
          const ext = extMap[mime] || '.jpg';
          mkdirSync(IMAGE_CACHE_DIR, { recursive: true });
          const filePath = path.join(IMAGE_CACHE_DIR, `img_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download image:', err.message);
        }
      } else if (messageContent.videoMessage) {
        body = messageContent.videoMessage.caption || '';
        hasMedia = true;
        mediaType = 'video';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = messageContent.videoMessage.mimetype || 'video/mp4';
          const ext = mime.includes('mp4') ? '.mp4' : '.mkv';
          mkdirSync(DOCUMENT_CACHE_DIR, { recursive: true });
          const filePath = path.join(DOCUMENT_CACHE_DIR, `vid_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download video:', err.message);
        }
      } else if (messageContent.audioMessage || messageContent.pttMessage) {
        hasMedia = true;
        mediaType = messageContent.pttMessage ? 'ptt' : 'audio';
        try {
          const audioMsg = messageContent.pttMessage || messageContent.audioMessage;
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = audioMsg.mimetype || 'audio/ogg';
          const ext = mime.includes('ogg') ? '.ogg' : mime.includes('mp4') ? '.m4a' : '.ogg';
          mkdirSync(AUDIO_CACHE_DIR, { recursive: true });
          const filePath = path.join(AUDIO_CACHE_DIR, `aud_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download audio:', err.message);
        }
      } else if (messageContent.documentMessage) {
        body = messageContent.documentMessage.caption || '';
        hasMedia = true;
        mediaType = 'document';
        const fileName = messageContent.documentMessage.fileName || 'document';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          mkdirSync(DOCUMENT_CACHE_DIR, { recursive: true });
          const safeFileName = path.basename(fileName).replace(/[^a-zA-Z0-9._-]/g, '_');
          const filePath = path.join(DOCUMENT_CACHE_DIR, `doc_${randomBytes(6).toString('hex')}_${safeFileName}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download document:', err.message);
        }
      }

      // For media without caption, use a placeholder so the API message is never empty
      if (hasMedia && !body) {
        body = `[${mediaType} received]`;
      }

      // Ignore our own reply messages in self-chat mode to avoid loops.
      if (msg.key.fromMe && ((REPLY_PREFIX && body.startsWith(REPLY_PREFIX)) || recentlySentIds.has(msg.key.id))) {
        if (WHATSAPP_DEBUG) {
          try { console.log(JSON.stringify({ event: 'ignored', reason: 'agent_echo', chatId, messageId: msg.key.id })); } catch {}
        }
        continue;
      }

      // Skip empty messages
      if (!body && !hasMedia) {
        if (WHATSAPP_DEBUG) {
          try { 
            console.log(JSON.stringify({ event: 'ignored', reason: 'empty', chatId, messageKeys: Object.keys(msg.message || {}) })); 
          } catch (err) {
            console.error('Failed to log empty message event:', err);
          }
        }
        continue;
      }

      const event = {
        messageId: msg.key.id,
        chatId,
        senderId,
        senderName: msg.pushName || senderNumber,
        chatName: isGroup ? (chatId.split('@')[0]) : (msg.pushName || senderNumber),
        isGroup,
        fromMe: !!msg.key.fromMe,
        body,
        hasMedia,
        mediaType,
        mediaUrls,
        mentionedIds,
        quotedParticipant,
        botIds,
        timestamp: msg.messageTimestamp,
      };

      storeHistoryEvent(event);

      messageQueue.push(event);
      if (messageQueue.length > MAX_QUEUE_SIZE) {
        messageQueue.shift();
      }
    }
  });
}

// HTTP server
const app = express();
app.use(express.json());

// Host-header validation — defends against DNS rebinding.
// The bridge binds loopback-only (127.0.0.1) but a victim browser on
// the same machine could be tricked into fetching from an attacker
// hostname that TTL-flips to 127.0.0.1. Reject any request whose Host
// header doesn't resolve to a loopback alias.
// See GHSA-ppp5-vxwm-4cf7.
const _ACCEPTED_HOST_VALUES = new Set([
  'localhost',
  '127.0.0.1',
  '[::1]',
  '::1',
]);

app.use((req, res, next) => {
  const raw = (req.headers.host || '').trim();
  if (!raw) {
    return res.status(400).json({ error: 'Missing Host header' });
  }
  // Strip port suffix: "localhost:3000" → "localhost"
  const hostOnly = (raw.includes(':')
    ? raw.substring(0, raw.lastIndexOf(':'))
    : raw
  ).replace(/^\[|\]$/g, '').toLowerCase();
  if (!_ACCEPTED_HOST_VALUES.has(hostOnly)) {
    return res.status(400).json({
      error: 'Invalid Host header. Bridge accepts loopback hosts only.',
    });
  }
  next();
});

// Poll for new messages (long-poll style)
app.get('/messages', (req, res) => {
  const msgs = messageQueue.splice(0, messageQueue.length);
  res.json(msgs);
});

// List chats observed in the local WhatsApp history store.
app.get('/history/chats', (req, res) => {
  res.json({ chats: listHistoryChats(), historyFile: HISTORY_FILE });
});

// Read locally stored WhatsApp history. Query params:
//   chatId, q, limit, before, after, isGroup
app.get('/history', (req, res) => {
  const { chatId, q, limit, before, after, isGroup } = req.query || {};
  const records = readHistoryRecords({
    chatId: chatId ? String(chatId) : undefined,
    q: q ? String(q) : undefined,
    limit: limit ? Number(limit) : 100,
    before: before ? Number(before) : undefined,
    after: after ? Number(after) : undefined,
    isGroup: isGroup === undefined ? undefined : String(isGroup) === 'true',
  });
  res.json({ count: records.length, messages: records, historyFile: HISTORY_FILE });
});

// Convenience alias for text search.
app.get('/search', (req, res) => {
  const { chatId, q, limit, before, after, isGroup } = req.query || {};
  const records = readHistoryRecords({
    chatId: chatId ? String(chatId) : undefined,
    q: q ? String(q) : '',
    limit: limit ? Number(limit) : 100,
    before: before ? Number(before) : undefined,
    after: after ? Number(after) : undefined,
    isGroup: isGroup === undefined ? undefined : String(isGroup) === 'true',
  });
  res.json({ count: records.length, messages: records, historyFile: HISTORY_FILE });
});

// Send a message
app.post('/send', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, message, replyTo, replyToParticipant } = req.body;
  if (!chatId || !message) {
    return res.status(400).json({ error: 'chatId and message are required' });
  }

  try {
    const options = {};
    const quoted = buildQuotedMessage(chatId, replyTo, replyToParticipant);
    if (quoted) options.quoted = quoted;
    const sent = await sock.sendMessage(chatId, { text: formatOutgoingMessage(message) }, options);

    // Track sent message ID to prevent echo-back loops
    if (sent?.key?.id) {
      recentlySentIds.add(sent.key.id);
      storeHistoryEvent({
        messageId: sent.key.id,
        chatId,
        senderId: normalizeWhatsAppId(sock.user?.id) || 'EMPRESA.IA',
        senderName: 'EMPRESA.IA',
        chatName: chatId,
        isGroup: String(chatId).endsWith('@g.us'),
        fromMe: true,
        body: message,
        hasMedia: false,
        mediaType: '',
        mediaUrls: [],
        mentionedIds: [],
        quotedParticipant: replyToParticipant || '',
        timestamp: Math.floor(Date.now() / 1000),
      });
      if (recentlySentIds.size > MAX_RECENT_IDS) {
        recentlySentIds.delete(recentlySentIds.values().next().value);
      }
    }

    res.json({ success: true, messageId: sent?.key?.id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Edit a previously sent message
app.post('/edit', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, messageId, message } = req.body;
  if (!chatId || !messageId || !message) {
    return res.status(400).json({ error: 'chatId, messageId, and message are required' });
  }

  try {
    const key = { id: messageId, fromMe: true, remoteJid: chatId };
    await sock.sendMessage(chatId, { text: formatOutgoingMessage(message), edit: key });
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// MIME type map and media type inference for /send-media
const MIME_MAP = {
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
  webp: 'image/webp', gif: 'image/gif',
  mp4: 'video/mp4', mov: 'video/quicktime', avi: 'video/x-msvideo',
  mkv: 'video/x-matroska', '3gp': 'video/3gpp',
  pdf: 'application/pdf',
  doc: 'application/msword',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
};

function inferMediaType(ext) {
  if (['jpg', 'jpeg', 'png', 'webp', 'gif'].includes(ext)) return 'image';
  if (['mp4', 'mov', 'avi', 'mkv', '3gp'].includes(ext)) return 'video';
  if (['ogg', 'opus', 'mp3', 'wav', 'm4a'].includes(ext)) return 'audio';
  return 'document';
}

// Send media (image, video, document) natively
app.post('/send-media', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, filePath, mediaType, caption, fileName, ptt, replyTo, replyToParticipant } = req.body;
  if (!chatId || !filePath) {
    return res.status(400).json({ error: 'chatId and filePath are required' });
  }

  try {
    if (!existsSync(filePath)) {
      return res.status(404).json({ error: `File not found: ${filePath}` });
    }

    const buffer = readFileSync(filePath);
    const ext = filePath.toLowerCase().split('.').pop();
    const type = mediaType || inferMediaType(ext);
    let msgPayload;

    switch (type) {
      case 'image':
        msgPayload = { image: buffer, caption: caption || undefined, mimetype: MIME_MAP[ext] || 'image/jpeg' };
        break;
      case 'video':
        msgPayload = { video: buffer, caption: caption || undefined, mimetype: MIME_MAP[ext] || 'video/mp4' };
        break;
      case 'audio': {
        const audioMime = (ext === 'ogg' || ext === 'opus') ? 'audio/ogg; codecs=opus' : 'audio/mpeg';
        msgPayload = { audio: buffer, mimetype: audioMime, ptt: ptt === undefined ? (ext === 'ogg' || ext === 'opus') : !!ptt };
        break;
      }
      case 'document':
      default:
        msgPayload = {
          document: buffer,
          fileName: fileName || path.basename(filePath),
          caption: caption || undefined,
          mimetype: MIME_MAP[ext] || 'application/octet-stream',
        };
        break;
    }

    const options = {};
    const quoted = buildQuotedMessage(chatId, replyTo, replyToParticipant);
    if (quoted) options.quoted = quoted;
    const sent = await sock.sendMessage(chatId, msgPayload, options);

    // Track sent message ID to prevent echo-back loops
    if (sent?.key?.id) {
      recentlySentIds.add(sent.key.id);
      storeHistoryEvent({
        messageId: sent.key.id,
        chatId,
        senderId: normalizeWhatsAppId(sock.user?.id) || 'EMPRESA.IA',
        senderName: 'EMPRESA.IA',
        chatName: chatId,
        isGroup: String(chatId).endsWith('@g.us'),
        fromMe: true,
        body: caption || `[${type} sent]`,
        hasMedia: true,
        mediaType: type,
        mediaUrls: [],
        mentionedIds: [],
        quotedParticipant: replyToParticipant || '',
        timestamp: Math.floor(Date.now() / 1000),
      });
      if (recentlySentIds.size > MAX_RECENT_IDS) {
        recentlySentIds.delete(recentlySentIds.values().next().value);
      }
    }

    res.json({ success: true, messageId: sent?.key?.id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Typing indicator
app.post('/typing', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected' });
  }

  const { chatId } = req.body;
  if (!chatId) return res.status(400).json({ error: 'chatId required' });

  try {
    await sock.sendPresenceUpdate('composing', chatId);
    res.json({ success: true });
  } catch (err) {
    res.json({ success: false });
  }
});

app.post('/presence', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;

  const { chatId, presence } = req.body || {};
  if (!chatId) return res.status(400).json({ error: 'chatId required' });
  const allowed = new Set(['available', 'unavailable', 'composing', 'recording', 'paused']);
  const value = String(presence || 'composing').toLowerCase();
  if (!allowed.has(value)) {
    return res.status(400).json({ error: 'presence must be one of: available, unavailable, composing, recording, paused' });
  }

  try {
    await sock.sendPresenceUpdate(value, chatId);
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/read', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;

  const { chatId, messageId, participant } = req.body || {};
  if (!chatId) return res.status(400).json({ error: 'chatId required' });

  try {
    if (messageId) {
      const key = buildMessageKey(chatId, messageId, participant, false);
      await sock.readMessages([key]);
    } else {
      await sock.sendReceipt(chatId, participant || undefined, undefined, 'read');
    }
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/react', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;

  const { chatId, messageId, participant, emoji } = req.body || {};
  if (!chatId || !messageId || emoji === undefined) {
    return res.status(400).json({ error: 'chatId, messageId, and emoji are required' });
  }

  try {
    const key = buildMessageKey(chatId, messageId, participant, false);
    const sent = await sock.sendMessage(chatId, { react: { text: String(emoji || ''), key } });
    res.json({ success: true, messageId: sent?.key?.id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/chat/modify', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;

  const { chatId, action, durationSeconds, messageId, participant } = req.body || {};
  if (!chatId || !action) return res.status(400).json({ error: 'chatId and action are required' });
  const normalizedAction = String(action).toLowerCase();

  try {
    if (normalizedAction === 'archive') {
      await sock.chatModify({ archive: true }, chatId);
    } else if (normalizedAction === 'unarchive') {
      await sock.chatModify({ archive: false }, chatId);
    } else if (normalizedAction === 'mute') {
      const muteEndTime = Math.floor(Date.now() / 1000) + Number(durationSeconds || 8 * 60 * 60);
      await sock.chatModify({ mute: muteEndTime }, chatId);
    } else if (normalizedAction === 'unmute') {
      await sock.chatModify({ mute: null }, chatId);
    } else if (normalizedAction === 'mark_unread') {
      await sock.chatModify({ markRead: false }, chatId);
    } else if (normalizedAction === 'mark_read') {
      await sock.chatModify({ markRead: true }, chatId);
    } else if (normalizedAction === 'star' || normalizedAction === 'unstar') {
      if (!messageId) return res.status(400).json({ error: 'messageId is required for star/unstar' });
      await sock.chatModify({
        star: {
          messages: [buildMessageKey(chatId, messageId, participant, false)],
          star: normalizedAction === 'star',
        },
      }, chatId);
    } else {
      return res.status(400).json({ error: 'unsupported chat modify action' });
    }
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Chat info
app.get('/chat/:id', async (req, res) => {
  const chatId = req.params.id;
  const isGroup = chatId.endsWith('@g.us');

  if (isGroup && sock) {
    try {
      const metadata = await sock.groupMetadata(chatId);
      return res.json({
        name: metadata.subject,
        isGroup: true,
        participants: metadata.participants.map(p => p.id),
      });
    } catch {
      // Fall through to default
    }
  }

  res.json({
    name: chatId.replace(/@.*/, ''),
    isGroup,
    participants: [],
  });
});

app.get('/profile/:id', async (req, res) => {
  if (!requireConnected(res)) return;

  const jid = req.params.id;
  try {
    const [status, pictureUrl, businessProfile] = await Promise.allSettled([
      sock.fetchStatus(jid),
      sock.profilePictureUrl(jid, 'image'),
      sock.getBusinessProfile(jid),
    ]);
    res.json({
      jid,
      status: status.status === 'fulfilled' ? status.value : null,
      pictureUrl: pictureUrl.status === 'fulfilled' ? pictureUrl.value : null,
      businessProfile: businessProfile.status === 'fulfilled' ? businessProfile.value : null,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Group/admin operations. These mutate WhatsApp state; callers should apply
// user-level policy before hitting them.
app.get('/groups/metadata/:id', async (req, res) => {
  if (!requireConnected(res)) return;
  const chatId = req.params.id;
  if (!requireGroupChatId(chatId, res)) return;

  try {
    const metadata = await sock.groupMetadata(chatId);
    res.json({ success: true, metadata });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/groups/join-approval', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;
  const { chatId, participants, action } = req.body || {};
  if (!requireGroupChatId(chatId, res)) return;
  const normalizedParticipants = normalizeParticipantList(participants);
  const normalizedAction = String(action || 'approve').toLowerCase();
  if (!['approve', 'reject'].includes(normalizedAction)) {
    return res.status(400).json({ error: 'action must be approve or reject' });
  }
  if (normalizedParticipants.length === 0) {
    return res.status(400).json({ error: 'participants[] is required' });
  }

  try {
    if (typeof sock.groupRequestParticipantsUpdate !== 'function') {
      return res.status(501).json({ error: 'groupRequestParticipantsUpdate is not available in this Baileys build' });
    }
    const result = await sock.groupRequestParticipantsUpdate(chatId, normalizedParticipants, normalizedAction);
    res.json({ success: true, result });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/groups/create', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;

  const { subject, participants } = req.body || {};
  const normalizedParticipants = normalizeParticipantList(participants);
  if (!subject || normalizedParticipants.length === 0) {
    return res.status(400).json({ error: 'subject and participants[] are required' });
  }

  try {
    const group = await sock.groupCreate(String(subject), normalizedParticipants);
    res.json({ success: true, group });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/groups/subject', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;

  const { chatId, subject } = req.body || {};
  if (!requireGroupChatId(chatId, res)) return;
  if (!subject) return res.status(400).json({ error: 'subject is required' });

  try {
    await sock.groupUpdateSubject(chatId, String(subject));
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/groups/description', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;

  const { chatId, description } = req.body || {};
  if (!requireGroupChatId(chatId, res)) return;

  try {
    await sock.groupUpdateDescription(chatId, description ? String(description) : undefined);
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/groups/photo', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;

  const { chatId, filePath } = req.body || {};
  if (!requireGroupChatId(chatId, res)) return;
  if (!filePath) return res.status(400).json({ error: 'filePath is required' });

  try {
    if (!existsSync(filePath)) {
      return res.status(404).json({ error: `File not found: ${filePath}` });
    }
    const buffer = readFileSync(filePath);
    await sock.updateProfilePicture(chatId, buffer);
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/groups/participants', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;

  const { chatId, participants, action } = req.body || {};
  if (!requireGroupChatId(chatId, res)) return;
  const normalizedParticipants = normalizeParticipantList(participants);
  const normalizedAction = String(action || 'add').toLowerCase();
  const allowedActions = new Set(['add', 'remove', 'promote', 'demote']);
  if (!allowedActions.has(normalizedAction)) {
    return res.status(400).json({ error: 'action must be one of: add, remove, promote, demote' });
  }
  if (normalizedParticipants.length === 0) {
    return res.status(400).json({ error: 'participants[] is required' });
  }

  try {
    const result = await sock.groupParticipantsUpdate(chatId, normalizedParticipants, normalizedAction);
    res.json({ success: true, result });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/groups/settings', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;

  const { chatId, setting } = req.body || {};
  if (!requireGroupChatId(chatId, res)) return;
  const normalizedSetting = String(setting || '').toLowerCase();
  const allowedSettings = new Set(['announcement', 'not_announcement', 'locked', 'unlocked']);
  if (!allowedSettings.has(normalizedSetting)) {
    return res.status(400).json({ error: 'setting must be one of: announcement, not_announcement, locked, unlocked' });
  }

  try {
    await sock.groupSettingUpdate(chatId, normalizedSetting);
    res.json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/groups/invite', async (req, res) => {
  if (!requireWritable(res)) return;
  if (!requireConnected(res)) return;

  const { chatId, revoke } = req.body || {};
  if (!requireGroupChatId(chatId, res)) return;

  try {
    const code = revoke ? await sock.groupRevokeInvite(chatId) : await sock.groupInviteCode(chatId);
    res.json({ success: true, code, inviteUrl: `https://chat.whatsapp.com/${code}` });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Health check
app.get('/health', (req, res) => {
  res.json({
    status: connectionState,
    queueLength: messageQueue.length,
    uptime: process.uptime(),
    readOnly: READ_ONLY,
    reconnects: reconnectCounts,
    lastDisconnectReason,
  });
});

// Start
if (PAIR_ONLY) {
  // Pair-only mode: just connect, show QR, save creds, exit. No HTTP server.
  console.log('📱 WhatsApp pairing mode');
  console.log(`📁 Session: ${SESSION_DIR}`);
  console.log();
  startSocket();
} else {
  app.listen(PORT, '127.0.0.1', () => {
    console.log(`🌉 WhatsApp bridge listening on port ${PORT} (mode: ${WHATSAPP_MODE})`);
    console.log(`📁 Session stored in: ${SESSION_DIR}`);
    if (ALLOWED_USERS.size > 0) {
      console.log(`🔒 Allowed users: ${Array.from(ALLOWED_USERS).join(', ')}`);
    } else {
      console.log(`⚠️  No WHATSAPP_ALLOWED_USERS set — all messages will be processed`);
    }
    console.log();
    startSocket();
  });
}
