// app/web/static/js/api.js
/** JSON-клиент без авторизации с обязательным CSRF-заголовком. */
export async function api(path, method = "GET") {
  const response = await fetch(path, {method, headers:{"X-Requested-With":"LeadersParser"}});
  if (response.status === 204) return null;
  if (!response.ok) {
    let message = `Ошибка HTTP ${response.status}`;
    try { const data = await response.json(); if (typeof data.detail === "string") message = data.detail; } catch { /* Proxy может вернуть ответ без JSON. */ }
    throw new Error(message);
  }
  return response.json();
}
