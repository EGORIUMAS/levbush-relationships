// Levbush Relationships — API сайта/мини-приложения (контракт: docs/api.md).
// Авторизация — подпись Telegram: Mini App (initData) или Login Widget. Пускает участников группы и админа.
// Ключи проверки подписи бот кладёт в таблицу app_secrets (производные от токена, сам токен сюда не попадает).
import { createClient } from "npm:@supabase/supabase-js@2";

const db = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!, {
  auth: { persistSession: false },
});

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, content-type, x-client-info, apikey",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Max-Age": "86400",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

// ---------------------------------------------------------------- ключи

type Secrets = { login_key: string; webapp_key: string; admin_id: number; login_max_age: number; webapp_max_age: number };
let secrets: Secrets | null = null;
let secretsAt = 0;

async function getSecrets(): Promise<Secrets | null> {
  if (secrets && Date.now() - secretsAt < 300_000) return secrets;
  const { data, error } = await db.from("app_secrets").select("name, value");
  if (error || !data) return secrets;
  const m = Object.fromEntries(data.map((r: { name: string; value: string }) => [r.name, r.value]));
  if (!m.login_key || !m.webapp_key) return null;
  secrets = {
    login_key: m.login_key,
    webapp_key: m.webapp_key,
    admin_id: Number(m.admin_id || 0),
    login_max_age: Number(m.login_max_age || 30 * 86400),
    webapp_max_age: Number(m.webapp_max_age || 86400),
  };
  secretsAt = Date.now();
  return secrets;
}

// ---------------------------------------------------------------- подпись

const enc = new TextEncoder();

function fromHex(hex: string): Uint8Array {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

function toHex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmacHex(keyHex: string, data: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", fromHex(keyHex), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return toHex(await crypto.subtle.sign("HMAC", key, enc.encode(data)));
}

function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function checkString(fields: Record<string, string>): string {
  return Object.keys(fields).filter((k) => k !== "hash").sort().map((k) => `${k}=${fields[k]}`).join("\n");
}

// Возвращает id пользователя или null
async function authUser(header: string | null, s: Secrets): Promise<number | null> {
  if (!header) return null;
  const sp = header.indexOf(" ");
  if (sp < 0) return null;
  const scheme = header.slice(0, sp).toLowerCase();
  const payload = header.slice(sp + 1).trim();
  const now = Math.floor(Date.now() / 1000);

  if (scheme === "tma") {
    // https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    const params = new URLSearchParams(payload);
    const fields: Record<string, string> = {};
    params.forEach((v, k) => (fields[k] = v));
    if (!fields.hash || !fields.user) return null;
    const hash = await hmacHex(s.webapp_key, checkString(fields));
    if (!safeEqual(hash, fields.hash)) return null;
    if (now - Number(fields.auth_date || 0) > s.webapp_max_age) return null;
    try {
      return Number(JSON.parse(fields.user).id) || null;
    } catch {
      return null;
    }
  }

  if (scheme === "tglogin") {
    // https://core.telegram.org/widgets/login#checking-authorization
    let obj: Record<string, unknown>;
    try {
      const bytes = Uint8Array.from(atob(payload), (c) => c.charCodeAt(0));
      obj = JSON.parse(new TextDecoder().decode(bytes));
    } catch {
      return null;
    }
    const fields: Record<string, string> = {};
    for (const [k, v] of Object.entries(obj)) if (v !== null && v !== undefined) fields[k] = String(v);
    if (!fields.hash || !fields.id) return null;
    const hash = await hmacHex(s.login_key, checkString(fields));
    if (!safeEqual(hash, fields.hash)) return null;
    if (now - Number(fields.auth_date || 0) > s.login_max_age) return null;
    return Number(fields.id) || null;
  }
  return null;
}

const memberCache = new Map<number, { ok: boolean; at: number }>();

async function isMember(uid: number, s: Secrets): Promise<boolean> {
  if (uid === s.admin_id) return true;
  const hit = memberCache.get(uid);
  if (hit && Date.now() - hit.at < 60_000) return hit.ok;
  const { data } = await db.from("people").select("is_member").eq("id", uid).maybeSingle();
  const ok = !!data?.is_member;
  memberCache.set(uid, { ok, at: Date.now() });
  return ok;
}

// ---------------------------------------------------------------- запросы

function int(v: string | null): number | null {
  if (v === null || !/^-?\d+$/.test(v)) return null;
  return Number(v);
}

async function rpc(fn: string, args: Record<string, unknown>): Promise<Response> {
  const { data, error } = await db.rpc(fn, args);
  if (error) {
    console.error(fn, error);
    return json({ error: "db" }, 500);
  }
  if (data === null) return json({ error: "not_found" }, 404);
  return json(data);
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });
  if (req.method !== "GET") return json({ error: "method" }, 405);

  const s = await getSecrets();
  if (!s) return json({ error: "not_configured" }, 503);
  const uid = await authUser(req.headers.get("authorization"), s);
  if (!uid) return json({ error: "auth" }, 401);
  if (!(await isMember(uid, s))) return json({ error: "not_member" }, 403);

  const url = new URL(req.url);
  const q = url.searchParams.get("q");
  const id = int(url.searchParams.get("id"));
  const a = int(url.searchParams.get("a"));
  const b = int(url.searchParams.get("b"));

  switch (q) {
    case "graph": {
      const { data, error } = await db.rpc("api_graph");
      if (error) return json({ error: "db" }, 500);
      return json({ ...data, me: uid });
    }
    case "person":
      return id === null ? json({ error: "args" }, 400) : rpc("api_person", { uid: id });
    case "relation":
      return a === null || b === null ? json({ error: "args" }, 400) : rpc("api_relation", { x: a, y: b });
    default:
      return json({ error: "unknown_query" }, 400);
  }
});
