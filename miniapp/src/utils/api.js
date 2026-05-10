const API_BASE = "";

async function ensureToken() {
  const token = localStorage.getItem("whisper_token");
  if (token) return token;

  // Try Telegram initData auto-auth
  const initData = window.Telegram?.WebApp?.initData;
  if (initData) {
    const res = await fetch(`${API_BASE}/api/auth/telegram`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ init_data: initData }),
    });
    const data = await res.json();
    if (data.success && data.token) {
      localStorage.setItem("whisper_token", data.token);
      return data.token;
    }
  }

  // No token and no Telegram context — caller must handle login
  const err = new Error("need_login");
  err.code = "NEED_LOGIN";
  throw err;
}

export async function apiFetch(path, options = {}) {
  let token = await ensureToken();
  const url = `${API_BASE}${path}`;

  const doFetch = (t) =>
    fetch(url, {
      ...options,
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${t}`,
        ...options.headers,
      },
      body: options.body ? JSON.stringify(options.body) : undefined,
    });

  let res = await doFetch(token);

  if (res.status === 401) {
    localStorage.removeItem("whisper_token");
    token = await ensureToken();
    res = await doFetch(token);
  }

  if (res.status === 204) return null;
  if (!res.ok) {
    const error = await res.json().catch(() => ({}));
    throw new Error(error.detail || `请求失败 (${res.status})`);
  }
  return res.json();
}

export async function loginWithPassword(password, totpCode) {
  const body = { password };
  if (totpCode) body.totp_code = totpCode;
  const res = await fetch(`${API_BASE}/api/auth/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json();
  if (data.success && data.token) {
    localStorage.setItem("whisper_token", data.token);
    return data;
  }
  throw new Error(data.detail || "密码错误");
}

export async function checkIpStatus() {
  const res = await fetch(`${API_BASE}/api/auth/check-ip`);
  return res.json();
}
