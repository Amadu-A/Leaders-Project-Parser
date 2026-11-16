// app/web/static/js/dashboard.js
/** Управляет polling, серверным поиском и действиями над durable заданием. */
import {api} from "./api.js";
import {actions,duration,statusNames,stageNames} from "./state.mjs";
import {element,renderTable} from "./table.js";

const find = selector => document.querySelector(selector);
let jobId = null, page = 1, search = "", errorsOnly = false, busy = false, tableVersion = 0;

/** Показывает ошибку связи, не заменяя последний достоверный прогресс. */
function notify(message = "") { const node=find("[data-notification]"); node.textContent=message; node.hidden=!message; }

/** Отображает серверные счётчики и разрешённые переходы текущего состояния. */
function render(job) {
  const allowed=actions(job);
  for (const button of document.querySelectorAll("[data-action]")) {
    const action=button.dataset.action, key=action === "retry-failed" ? "retry" : action;
    button.disabled=busy || !allowed[key];
    if (["delete","retry-failed"].includes(action)) button.hidden=!allowed[key];
  }
  if (!job) return;
  find("[data-status]").textContent=statusNames[job.status] || job.status;
  find("[data-status]").className=`badge${job.status === "completed" ? " badge--ok" : job.errors ? " badge--error" : " badge--active"}`;
  find("[data-stage]").textContent=stageNames[job.stage] || job.stage;
  find("[data-last-success]").textContent=job.last_success || "Ожидание worker";
  for (const metric of document.querySelectorAll("[data-metric]")) metric.textContent=job[metric.dataset.metric].toLocaleString("ru-RU");
  find("[data-average]").textContent=duration(job.average_seconds);
  const progress=find("[data-progress]");
  if (job.discovery_complete) progress.value=job.found ? (job.processed/job.found*100) : 0; else progress.removeAttribute("value");
  find("[data-progress-label]").textContent=job.discovery_complete ? `Обработано ${job.processed.toLocaleString("ru-RU")} из ${job.found.toLocaleString("ru-RU")}` : `Индексация: ${job.pages} страниц · найдено ${job.found.toLocaleString("ru-RU")}`;
  find("[data-eta]").textContent=job.eta_seconds == null ? "Оценка времени появится после первых 5 организаций" : `Примерно осталось: ${duration(job.eta_seconds)}`;
  const error=find("[data-job-error]"); error.textContent=job.last_error || ""; error.hidden=!job.last_error || !job.errors || job.status === "completed";
  const download=find("[data-download]"); download.hidden=!job.found; download.href=`/api/jobs/${job.id}/download`; download.textContent=job.status.startsWith("completed") ? "↓ Скачать Excel" : "↓ Скачать текущий Excel";
  find("[data-job-id]").textContent=`Задание ${job.id}`;
  const events=find("[data-events]"); events.replaceChildren();
  for (const event of job.events) { const li=element("li","","journal__event"); li.append(element("time",new Date(event.created_at).toLocaleTimeString("ru-RU"),"journal__time"),element("span",event.message)); events.append(li); }
}

/** Загружает текущую страницу; запоздавший ответ поиска не перезаписывает новый. */
async function loadTable() {
  if (!jobId) return;
  const version=++tableVersion;
  const params=new URLSearchParams({page,size:25,q:search,errors_only:String(errorsOnly)});
  const data=await api(`/api/jobs/${jobId}/companies?${params}`);
  if (version === tableVersion) renderTable(data);
}

/** Обновляет экран одним циклом без параллельных polling-запросов. */
async function refresh() {
  if (!jobId) { const latest=await api("/api/jobs/latest"); jobId=latest?.id || null; }
  if (!jobId) { render(null); return; }
  const job=await api(`/api/jobs/${jobId}`); render(job); await loadTable();
}

/** Выполняет идемпотентное действие и блокирует двойной клик до ответа API. */
async function perform(action) {
  if (busy) return;
  if (action === "delete" && !window.confirm("Удалить все данные этого завершённого задания?")) return;
  if (action === "cancel" && !window.confirm("Остановить задание? Собранные данные останутся доступны.")) return;
  busy=true; for (const node of document.querySelectorAll("[data-action]")) node.disabled=true;
  try {
    notify();
    if (action === "start") { const job=await api("/api/jobs","POST"); jobId=job.id; page=1; history.replaceState(null,"",`#${jobId}`); }
    else if (action === "delete") { await api(`/api/jobs/${jobId}`,"DELETE"); jobId=null; history.replaceState(null,"",location.pathname); location.reload(); return; }
    else await api(`/api/jobs/${jobId}/${action}`,"POST");
  } catch (error) { notify(error.message); }
  finally { busy=false; try { await refresh(); } catch(error) { notify(error.message); } }
}

/** Планирует следующий опрос после завершения предыдущего и сохраняет ошибки связи. */
async function poll() {
  if (!busy) { try { await refresh(); } catch(error) { notify(`Не удалось обновить данные: ${error.message}`); } }
  setTimeout(poll,Number(document.body.dataset.pollSeconds)*1000);
}

const saved=location.hash.slice(1); if (/^[0-9a-f-]{36}$/i.test(saved)) jobId=saved;
for (const button of document.querySelectorAll("[data-action]")) button.addEventListener("click",()=>perform(button.dataset.action));
find("[data-close-details]").addEventListener("click",()=>find("[data-details]").close());
find("[data-prev]").addEventListener("click",async()=>{ page=Math.max(1,page-1); try {await loadTable();}catch(error){notify(error.message);} });
find("[data-next]").addEventListener("click",async()=>{ page+=1; try {await loadTable();}catch(error){notify(error.message);} });
let searchTimer;
find("[data-search-form]").addEventListener("submit",event=>event.preventDefault());
find("#company-search").addEventListener("input",event=>{clearTimeout(searchTimer);search=event.target.value;page=1;searchTimer=setTimeout(()=>loadTable().catch(error=>notify(error.message)),350);});
find("[data-errors-only]").addEventListener("change",event=>{errorsOnly=event.target.checked;page=1;loadTable().catch(error=>notify(error.message));});
poll();
