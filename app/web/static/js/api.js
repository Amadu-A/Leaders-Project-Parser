// app/web/static/js/api.js
/** Общий JSON-клиент, same-origin credentials и обязательный CSRF-заголовок. */
export async function api(path, method = "GET") {
  const response = await fetch(path, {method, credentials:"same-origin", headers:{"X-Requested-With":"LeadersParser"}});
  if (response.status === 204) return null;
  if (!response.ok) {
    let message = `Ошибка HTTP ${response.status}`;
    try { const data = await response.json(); if (typeof data.detail === "string") message = data.detail; } catch { /* Тело может отсутствовать у proxy/auth. */ }
    throw new Error(message);
  }
  return response.json();
}
