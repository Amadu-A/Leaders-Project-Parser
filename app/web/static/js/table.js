// app/web/static/js/table.js
/** Безопасное построение ограниченной страницы таблицы и диалога контактов. */
import {companyStatus,fieldStatusNames,stageNames,revenue} from "./state.mjs";

/** Создаёт DOM-узел; недоверенные строки вставляются только через textContent. */
export function element(tag, text = "", className = "") {
  const node = document.createElement(tag); node.textContent = text;
  if (className) node.className = className;
  return node;
}

/** Открывает все контакты и ошибки выбранной компании в доступном dialog. */
function details(company) {
  const dialog = document.querySelector("[data-details]");
  document.querySelector("[data-details-title]").textContent = company.name;
  const body = document.querySelector("[data-details-content]"); body.replaceChildren();
  for (const [label, value] of [["ИНН",company.inn],["КПП",company.kpp_status === "pending" ? "Ожидает дозаполнения" : company.kpp || "Не указан"],["Выручка (млн ₽)",company.revenue_status === "pending" ? "Ожидает дозаполнения" : revenue(company.revenue_mln)],["Руководитель",company.director || "Не указан"],["Телефоны",company.phones.join("\n")],["Электронная почта",company.emails.join("\n")],["Сайты",company.websites.join("\n")]]) {
    body.append(element("h3",label,"details__label"),element("p",value || "Нет данных","details__value"));
  }
  body.append(element("p",`Карточка: ${fieldStatusNames[company.detail_status] || "Не проверено"} · Контакты: ${fieldStatusNames[company.contacts_status] || "Не проверено"}`,"details__label"));
  for (const issue of company.issues || []) body.append(element("p",`${stageNames[issue.stage] || "Проверка"}: ${issue.message}`,"details__error"));
  dialog.showModal();
}

/** Перерисовывает только текущую серверную страницу, никогда весь каталог. */
export function renderTable(data) {
  const tbody = document.querySelector("[data-companies]"); tbody.replaceChildren();
  for (const company of data.items) {
    const row = element("tr");
    for (const value of [company.ordinal,company.name,company.inn,company.kpp || "—",revenue(company.revenue_mln),company.director || "—"]) row.append(element("td",value));
    const statusCell = element("td"), [label, modifier] = companyStatus(company);
    statusCell.append(element("span",label,`badge${modifier ? ` badge--${modifier}` : ""}`));
    const contactCell = element("td"), button = element("button","Подробнее ↗","table__link");
    button.type = "button"; button.setAttribute("aria-label",`Контакты и ошибки: ${company.name}`);
    button.addEventListener("click",() => details(company)); contactCell.append(button); row.append(statusCell,contactCell); tbody.append(row);
  }
  if (!data.items.length) { const row = element("tr"), cell = element("td","Организации не найдены","table__empty"); cell.colSpan=8; row.append(cell); tbody.append(row); }
  const pages = Math.max(1,Math.ceil(data.total/data.size));
  document.querySelector("[data-table-count]").textContent = data.total.toLocaleString("ru-RU");
  document.querySelector("[data-page-label]").textContent = `Страница ${data.page} из ${pages}`;
  document.querySelector("[data-prev]").disabled = data.page <= 1;
  document.querySelector("[data-next]").disabled = data.page >= pages;
}
