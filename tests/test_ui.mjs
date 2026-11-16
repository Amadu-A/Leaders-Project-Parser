// tests/test_ui.mjs
/** Проверяет правила действий UI и честное отображение неполных карточек. */
import test from "node:test";
import assert from "node:assert/strict";
import {actions,companyStatus,duration} from "../app/web/static/js/state.mjs";

test("paused job resumes, completed job permits a new run",()=>{
  assert.equal(actions({status:"paused",errors:0}).resume,true);
  assert.equal(actions({status:"paused",errors:0}).start,false);
  assert.equal(actions({status:"completed",errors:0}).delete,true);
  assert.equal(actions({status:"completed_with_errors",errors:2}).retry,true);
  assert.equal(actions({status:"cancelled",errors:2}).retry,false);
});
test("unchecked contacts never appear as complete",()=>{
  assert.deepEqual(companyStatus({detail_status:"ok",contacts_status:"pending"}),["Ожидание",""]);
  assert.deepEqual(companyStatus({detail_status:"ok",contacts_status:"missing"}),["Готово","ok"]);
  assert.deepEqual(companyStatus({detail_status:"ok",contacts_status:"error"}),["Ошибка","error"]);
  assert.deepEqual(companyStatus({detail_status:"pending",contacts_status:"pending",issues:[{kind:"access"}]}),["Ошибка","error"]);
  assert.equal(duration(null),"—");
});
