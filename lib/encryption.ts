// Client-side encryption utility for API keys
// AES-256-GCM encryption before storing to Supabase

const ALGORITHM = 'AES-GCM';
const KEY_LENGTH = 256;
const IV_LENGTH = 12;

async function deriveKey(password: string, salt: Uint8Array): Promise<CryptoKey> {
  const encoder = new TextEncoder();
  const keyMaterial = await crypto.subtle.importKey(
    'raw',
    encoder.encode(password),
    { name: 'PBKDF2' },
    false,
    ['deriveKey']
  );
  return crypto.subtle.deriveKey(
    { name: 'PBKDF2', salt, iterations: 100000, hash: 'SHA-256' },
    keyMaterial,
    { name: ALGORITHM, length: KEY_LENGTH },
    false,
    ['encrypt', 'decrypt']
  );
}

export async function encryptApiKey(plaintext: string, userId: string): Promise<{
  encrypted: string;
  iv: string;
  salt: string;
}> {
  const encoder = new TextEncoder();
  const iv = crypto.getRandomValues(new Uint8Array(IV_LENGTH));
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const key = await deriveKey(userId, salt);

  const encrypted = await crypto.subtle.encrypt(
    { name: ALGORITHM, iv },
    key,
    encoder.encode(plaintext)
  );

  const encArr = new Uint8Array(encrypted);
  const ivArr = iv;
  const saltArr = salt;
  return {
    encrypted: btoa(Array.from(encArr).map(c => String.fromCharCode(c)).join('')),
    iv: btoa(Array.from(ivArr).map(c => String.fromCharCode(c)).join('')),
    salt: btoa(Array.from(saltArr).map(c => String.fromCharCode(c)).join('')),
  };
}

export async function decryptApiKey(
  encryptedBase64: string,
  ivBase64: string,
  saltBase64: string,
  userId: string
): Promise<string> {
  const encrypted = new Uint8Array(Array.from(atob(encryptedBase64)).map(c => c.charCodeAt(0)));
  const iv = new Uint8Array(Array.from(atob(ivBase64)).map(c => c.charCodeAt(0)));
  const salt = new Uint8Array(Array.from(atob(saltBase64)).map(c => c.charCodeAt(0)));
  const key = await deriveKey(userId, salt);

  const decrypted = await crypto.subtle.decrypt({ name: ALGORITHM, iv }, key, encrypted);
  return new TextDecoder().decode(decrypted);
}

export function maskApiKey(key: string): string {
  if (key.length <= 8) return '****';
  return key.slice(0, 4) + '****' + key.slice(-4);
}
