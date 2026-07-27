export class ApiError extends Error {
  constructor(message, status, details) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

export async function request(path, options = {}) {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("json") ? await response.json().catch(() => ({})) : await response.text();
  if (!response.ok) {
    if (response.status === 401 && !path.includes("/auth/")) window.dispatchEvent(new CustomEvent("auth:expired"));
    throw new ApiError(payload?.error?.message || payload?.detail || payload?.message || "Не удалось выполнить запрос", response.status, payload);
  }
  return payload;
}

export const api = {
  login: (password) => request("/api/auth/login", { method: "POST", body: JSON.stringify({ password }) }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  session: () => request("/api/auth/session"),
  config: () => request("/api/config"),
  folders: (path = "") => request(`/api/folders?path=${encodeURIComponent(path)}`),
  runs: () => request("/api/checks"),
  run: (id) => request(`/api/checks/${encodeURIComponent(id)}`),
  orders: (id) => request(`/api/checks/${encodeURIComponent(id)}/orders`),
  order: (runId, orderId) => request(`/api/checks/${encodeURIComponent(runId)}/orders/${encodeURIComponent(orderId)}`),
  start: (body) => request("/api/checks", { method: "POST", body: JSON.stringify(body) }),
  cancel: (id) => request(`/api/checks/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  correction: (runId, orderId, body) => request(`/api/checks/${encodeURIComponent(runId)}/orders/${encodeURIComponent(orderId)}/correction`, { method: "POST", body: JSON.stringify(body) }),
  preparePrint: (body) => request("/api/orders/prepare-print", { method: "POST", body: JSON.stringify(body) }),
  prepareReject: (body) => request("/api/orders/prepare-reject", { method: "POST", body: JSON.stringify(body) }),
  history: (params = {}) => request(`/api/order-history?${new URLSearchParams(params)}`),
};
