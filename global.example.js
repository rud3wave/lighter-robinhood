// ============================================================
//  GLOBAL CONFIG - Robinhood Chain Lighter ETH/USDG Bot
// ============================================================

// Telegram alerts. Leave empty/0 to disable.
export const TELEGRAM = {
  token: '',
  chatId: 0,
};

// Password used for AES-256-GCM vault encryption. The bot also accepts
// LIGHTER_ENCRYPTION_PASSWORD from the environment. The default value triggers
// an interactive prompt and is never accepted as a real password.
// Do not change this after the first successful encrypted setup.
export const ENCRYPTION_PASSWORD = 'change-me-before-first-run';
