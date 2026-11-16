// app/web/static/js/state.mjs
/** Чистые правила UI-состояния и форматирования, независимые от DOM и fetch. */
export const terminal = new Set(["completed", "completed_with_errors", "failed", "cancelled"]);
export const statusNames = {queued:"В очереди",discovering:"Индексация",processing:"Сбор контактов",paused:"На паузе",cooldown:"Ожидание источника",completed:"Завершено",completed_with_errors:"Завершено с ошибками",failed:"Ошибка",cancelled:"Остановлено"};
export const stageNames = {preflight:"Проверка доступа к источнику",discovering:"Собираем полный список организаций",processing:"Обрабатываем карточки и контакты",detail:"Карточка",contacts:"Контакты",backfill_revenue:"Дозаполняем выручку ранее найденных компаний",backfill_kpp:"Дозаполняем КПП без повторной обработки контактов",kpp:"КПП"};
export const fieldStatusNames = {pending:"Не проверено",unchecked:"Не проверено",ok:"Проверено",missing:"Данные отсутствуют",error:"Ошибка проверки",not_found:"Страница не найдена"};
/** Возвращает доступные действия, сохраняя distinction ошибки/успеха. */
export function actions(job) {
  const ended = job && terminal.has(job.status);
  return {start:!job || ended,pause:!!job && !ended && job.status !== "paused",resume:job?.status === "paused",cancel:!!job && !ended,retry:!!job && ended && job.errors > 0 && job.status !== "cancelled",delete:ended,"backfill-fields":!!ended && job.status !== "cancelled" && job.discovery_complete && job.fields_pending > 0};
}
/** Различает pending, непроверенные контакты и полностью успешную карточку. */
export function companyStatus(company) {
  if (company.inn_conflict || company.issues?.length || [company.detail_status,company.contacts_status,company.kpp_status,company.revenue_status].some(s => ["error","not_found"].includes(s))) return ["Ошибка", "error"];
  if ([company.detail_status,company.contacts_status].every(s => ["ok","missing"].includes(s)) && [company.kpp_status ?? "missing",company.revenue_status ?? "missing"].some(s => s === "pending")) return ["Дозаполнение", ""];
  if ([company.detail_status,company.contacts_status].every(s => ["ok","missing"].includes(s))) return ["Готово", "ok"];
  return ["Ожидание", ""];
}
/** Сохраняет нулевую выручку, миллионы и десятичную точность при отображении. */
export function revenue(value) { return value == null ? "—" : Number(value).toLocaleString("ru-RU",{maximumFractionDigits:8}); }
/** Форматирует оценку длительности, сохраняя отсутствие статистики. */
export function duration(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.ceil(seconds)} с`;
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} мин`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)} ч`;
  return `${(seconds / 86400).toFixed(1)} дн`;
}
