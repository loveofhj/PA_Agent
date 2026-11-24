"use strict";
const byId = id => document.getElementById(id);
let selectedTask;
let registry = [];
let steps = {};
let stepDefinitions = [];
let selectedSkillStep;
function node(tag, value, cls) {
  const el = document.createElement(tag);
  if (value !== undefined) el.textContent = value;
  if (cls) el.className = cls;
  return el;
}
function flow(label, entries) {
  const list = node("ol", undefined, "flow");
  list.setAttribute("aria-label", label);
  list.setAttribute("role", "list");
  for (const {title, caption, tag, tone, action, current} of entries) {
    const item = node("li", undefined, "flow-item");
    const card = node(action ? "button" : "div", undefined, "flow-card" + (tone ? " " + tone : ""));
    if (action) {
      card.type = "button";
      card.addEventListener("click", action);
      if (current !== undefined) card.setAttribute("aria-pressed", String(current));
    }
    if (tag) card.append(node("span", tag, "flow-tag"));
    card.append(node("strong", title));
    if (caption) card.append(node("span", caption, "flow-caption"));
    item.append(card);
    list.append(item);
  }
  return list;
}
function openPage(page) {
  document.querySelector(`nav [data-page="${page}"]`).click();
}
function detail(title, data) {
  const box = node("details");
  box.append(node("summary", title), node("pre", JSON.stringify(data, null, 2)));
  return box;
}
function status(message, error=false) {
  byId("status").textContent = message;
  byId("status").classList.toggle("error", error);
}
async function api(path, data) {
  const opts = data === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(data)};
  const response = await fetch("/api/" + path, opts);
  const result = await response.json();
  if (!response.ok) throw Error(result.error || String(response.status));
  return result;
}
function fill(id, rows, render) {
  byId(id).replaceChildren(...(rows.length ? rows.map(render) : [node("p", "기록 없음")]));
}
const stateNames = {IDLE:"대기", PLANNING:"계획 작성 중", READY:"승인 대기", PREVIEW_ONLY:"배치 계획 미리보기", VALIDATING:"조건 확인 중",
  RUNNING:"실행 중", RECOVERING:"복구 중", DONE:"완료", HOLD:"보류", FAILED:"실패", "SAFE-STOP":"안전 정지"};
const eventNames = {INTERLOCK:"실행 조건 점검", STEP_DONE:"단계 완료", SKILL_RESULT:"실행 결과",
  DATA_QUALITY:"데이터 품질 검사", DATASET_PROMOTED:"데이터셋 등록"};
const causeNames = {"ERR-04":"실행 조건 불일치", "ERR-07":"최종 검사 불합격", "ERR-08":"설비 상태 갱신 필요",
  "ERR-09":"판정 신뢰도 부족", "ERR-11":"스크류 체결 이상", "ERR-13":"안전 허가 없음",
  LLM_PLAN_ERROR:"작업 계획 생성 오류", LLM_TIMEOUT:"Ollama 응답 시간 초과", NEEDS_CONFIRMATION:"작업지시 확인 필요"};
function activity(title, description, badge, alert, data) {
  const row = node("article",undefined,"activity");
  const line = node("div",undefined,"activity-line");
  line.append(node("strong",title));
  if (badge) line.append(node("span",badge,alert ? "badge alert" : "badge"));
  row.append(line);
  if (description) row.append(node("p",description,"muted"));
  row.append(detail("기술 세부정보",data));
  return row;
}
function eventDetail(event) {
  const title = causeNames[event.cause] || eventNames[event.kind] || stateNames[event.state] || "시스템 활동";
  const date = new Date(event.timestamp);
  const when = Number.isNaN(date.getTime()) ? "" : date.toLocaleString("ko-KR", {dateStyle:"short",timeStyle:"short"});
  const row = activity(title,[event.StepCode,event.message].filter(Boolean).join(" · "),
    event.cause ? "확인 필요" : "",Boolean(event.cause),event);
  row.prepend(node("time", when, "event-time"));
  row.firstChild.dateTime = event.timestamp;
  row.classList.toggle("event-alert", Boolean(event.cause));
  return row;
}
function taskDetail(task) {
  return activity(task.instruction,[task.current_step,task.error].filter(Boolean).join(" · "),
    stateNames[task.state],["FAILED","HOLD","SAFE-STOP"].includes(task.state),task);
}
async function health() {
  const result = await api("health");
  byId("health").textContent = result.provider === "mock" ? "Mock LLM · 재점검" :
    result.ready ? "Ollama 연결됨 · 재점검" : "Ollama 연결/모델 확인 필요 · 재점검";
  byId("chat-provider").textContent = result.provider === "mock" ? "Mock 시연 모드" :
    result.ready ? "로컬 Ollama 연결됨" : "로컬 Ollama 연결 확인 필요";
  status(result.message, !result.ready);
}
async function overview() {
  const result = await api("dashboard");
  byId("metrics").replaceChildren(...[
    ["완료",result.states.DONE], ["보류",result.states.HOLD], ["실패",result.states.FAILED],
    ["SAFE-STOP",result.states["SAFE-STOP"]], ["복구 이력 작업",result.recovered]
  ].map(([label,value]) => {
    const el = node("div", undefined, "metric");
    el.append(node("strong", value || 0), node("span",label)); return el;
  }));
  fill("recent", result.tasks, task => {
    const row = taskDetail(task);
    const button = node("button", "공정 흐름 보기", "open-task");
    button.addEventListener("click", async () => {
      try {
        await showTask(await api("tasks/" + task.TaskID));
        openPage("task");
        byId("task-result").scrollIntoView({block:"start"});
      } catch (error) { status(error.message,true); }
    });
    row.insertBefore(button, row.lastChild);
    return row;
  });
  fill("alarms", result.events, eventDetail);
}
async function showTask(task) {
  selectedTask = task.TaskID;
  const events = (await api("events")).filter(e => e.TaskID === task.TaskID);
  const completed = new Set(events.filter(e => e.kind === "STEP_DONE").map(e => e.StepCode));
  const plan = flow("S1~S8 조립 공정", (task.steps || []).map(step => {
    const isComplete = completed.has(step.StepCode);
    const active = !isComplete && task.current_step === step.StepCode;
    const technicalNames = step.skills.map(skill => registry.find(entry => entry.id === skill.SkillID)?.name || skill.SkillID);
    return {title: step.name, tag: step.StepCode + " · " + (isComplete ? "완료" :
      active ? stateNames[task.state] : "예정"), caption: technicalNames.join(" · "),
      tone: isComplete ? "flow-done" : active ? "flow-alert" : ""};
  }));
  byId("task-flow").replaceChildren(flow("작업 처리 순서", [
    {title: "작업지시", tone: "flow-done"},
    {title: "계획 검증", caption: task.steps ? "계획 생성됨" : stateNames[task.state],
      tone: task.steps ? "flow-done" : "flow-alert"},
    {title: "Mock 실행", caption: task.batch_preview ? "배치 미리보기" : stateNames[task.state],
      tone: task.state === "DONE" ? "flow-done" : ""}
  ]));
  const batch = task.batch_preview;
  const summary = batch ? [
    node("p", `${task.RecipeID} ${batch.quantity}개 · LOT ${batch.lot_count}개 (LOT당 최대 ${batch.lot_size}개)` +
      (batch.remainder ? ` · 마지막 LOT ${batch.remainder}개` : "")),
    node("p", "배치는 계획 확인용입니다. Mock 실행을 하려면 수량 1개로 새 작업을 생성하세요.","muted")
  ] : [];
  byId("task-result").replaceChildren(node("h2", (stateNames[task.state] || task.state) + (task.current_step ? " · " + task.current_step : "")),
    node("p", task.error || task.decision?.reason || ""), ...summary, plan, taskDetail(task));
  byId("run").hidden = task.state !== "READY";
  fill("task-events",events,eventDetail);
}
async function showSkills() {
  const events = await api("events");
  function skillsAt(code) {
    return registry.filter(skill => skill.steps === "S1~S8" || skill.steps.split(",").includes(code));
  }
  function render() {
    byId("skill-flow").replaceChildren(flow("기술을 확인할 공정 선택", stepDefinitions.map(([code, name]) => ({
      title: name, tag: code, caption: `${skillsAt(code).length}개 기술`,
      current: code === selectedSkillStep,
      action: () => {
        selectedSkillStep = code;
        render();
        byId("skill-flow").querySelector('[aria-pressed="true"]').focus();
      }
    }))));
    fill("skill-list", skillsAt(selectedSkillStep), skill => {
      const latest = events.find(e => e.SkillID === skill.id && e.StepCode === selectedSkillStep && e.kind === "SKILL_RESULT");
      const caption = latest ? latest.verified ? "최근 검증 통과" : "최근 검증 실패" : "실행 기록 없음";
      return activity(skill.name, "입력 · " + skill.input + " / 확인 결과 · " + skill.output,
        caption, Boolean(latest && !latest.verified), {...skill, latest:latest || null});
    });
  }
  render();
}
function episodeDetail(e) {
  const {grade, ready, defects} = e.quality;
  const row = node("article", undefined, "panel");
  row.append(node("h2", `${e.source} · ${e.step_code} · ${grade}등급`),
    flow("에피소드 처리 흐름", [
      {title:"데이터 수집", caption:e.source, tone:"flow-done"},
      {title:"품질 검사", caption:`${grade}등급 · 결함 ${defects.length}건`, tone:grade === "C" ? "flow-alert" : "flow-done"},
      {title:ready ? "승격 가능" : "승격 보류", tone:ready ? "flow-done" : "flow-alert"}
    ]), detail("수집 및 품질 세부정보",e));
  return row;
}
async function datasetList() {
  const params = new URLSearchParams();
  for (const [k,v] of new FormData(byId("filters"))) if(v.trim()) params.set(k,v.trim());
  fill("datasets", await api("datasets?" + params), d => {
    const row = node("article",undefined,"panel");
    const quality = Object.entries(d.quality).filter(([,count]) => count)
      .map(([grade,count]) => `${grade}등급 ${count}건`).join(" · ");
    row.append(node("h2", d.dataset_id + " · v" + d.version), flow("데이터셋 구성", [
      {title:"원본 에피소드", caption:`${d.source} · ${d.episode_count}건`},
      {title:"품질 확인", caption:quality},
      {title:"버전 등록", caption:`v${d.version} · ${d.steps.join(", ")}`, tone:"flow-done"}
    ]), detail("멤버와 데이터 계보", d));
    return row;
  });
}
async function members() {
  const episodes = (await api("episodes")).filter(e => e.quality.ready);
  fill("members", episodes, e => {
    const box = node("div", undefined, "member");
    const label = node("label", undefined, "check");
    const check = node("input"); check.type = "checkbox"; check.value = e.episode_id;
    label.append(check, node("span", e.episode_id+" · "+e.source+" · "+e.quality.grade));
    const select = node("select"); select.setAttribute("aria-label","데이터 분할");
    for (const split of ["train","validation","test"]) select.append(new Option(split));
    box.append(label,select); return box;
  });
}
let schedules = [];
const localTime = new Intl.DateTimeFormat("ko-KR",{dateStyle:"short",timeStyle:"medium"});
function renderSchedule(schedule) {
  const {request, slots, routes, unassigned} = schedule;
  const rows = [node("h2", `${request.plan_id} · 개정 ${request.revision}`)];
  const quantity = slots.reduce((sum, slot) => sum + slot.quantity, 0);
  rows.push(node("p", `배치 ${slots.length} LOT · ${quantity}개 · 납기 초과 예상 ${slots.filter(slot => slot.late).length} LOT`));
  for (const id of [...new Set(slots.map(slot => slot.cell_id))].sort()) {
    const cell = node("article",undefined,"panel");
    const table = node("table",undefined,"schedule-table");
    const head = table.createTHead().insertRow();
    for (const title of ["작업·LOT","레시피","수량","예상 시간","비고"]) {
      const column = node("th",title);
      column.scope = "col";
      head.append(column);
    }
    const body = table.createTBody();
    for (const slot of slots.filter(item => item.cell_id === id)) {
      const row = body.insertRow();
      const note = [slot.setup_sec ? `전환 ${slot.setup_sec}초` : "", slot.late ? "납기 초과 예상" : ""].filter(Boolean);
      for (const value of [`${slot.order_id} · LOT ${slot.lot_number}`, slot.recipe_id, slot.quantity,
        `${localTime.format(new Date(slot.start_at))} → ${localTime.format(new Date(slot.end_at))}`,
        note.join(" · ") || "—"]) row.insertCell().textContent = value;
      if (slot.late) row.classList.add("error");
    }
    const viewport = node("div",undefined,"schedule-table-wrap");
    viewport.append(table);
    cell.append(node("h3",id),viewport);
    rows.push(cell);
  }
  for (const order of unassigned) {
    rows.push(node("p", `${order.order_id} · ${order.recipe_id} ${order.quantity}개 배치 보류 · ${order.reason}`, "error"));
  }
  rows.push(node("h3","셀 공정 순서"),flow("LOT 공정 및 기술",routes.map(step => ({
    title:step.name,tag:step.step_code,caption:step.skills.map(skill => skill.name).join(" · ")
  }))));
  byId("schedule-result").replaceChildren(...rows);
}
async function scheduleHistory() {
  const select = byId("schedule-history");
  const current = select.value;
  const previous = current !== "" ? schedules[Number(current)]?.request : null;
  schedules = await api("schedules");
  select.replaceChildren(new Option("계획 선택",""), ...schedules.map((schedule,index) =>
    new Option(`${schedule.request.plan_id} · 개정 ${schedule.request.revision}`, String(index))));
  const index = schedules.findIndex(s => s.request.plan_id === previous?.plan_id && s.request.revision === previous.revision);
  if (index >= 0) select.value = String(index);
}
const pages = {
  overview,
  task: async () => {},
  schedule: scheduleHistory,
  skills: showSkills,
  episodes: async () => fill("episode-list",await api("episodes"),episodeDetail),
  catalog: async () => { await members(); await datasetList(); },
  events: async () => fill("event-list",await api("events"),eventDetail)
};
function handle(id, event, action) {
  byId(id).addEventListener(event, async e => {
    e.preventDefault();
    const button = e.submitter || (e.currentTarget.tagName === "BUTTON" ? e.currentTarget : null);
    if(button) button.disabled = true;
    try { await action(e.currentTarget); } catch(error) { status(error.message,true); }
    finally { if(button) button.disabled = false; }
  });
}
for (const button of document.querySelectorAll("nav button")) button.addEventListener("click", async () => {
  for (const other of document.querySelectorAll("nav button")) {
    other.removeAttribute("aria-current"); byId(other.dataset.page).hidden = other !== button;
  }
  button.setAttribute("aria-current","page");
  try { await pages[button.dataset.page](); } catch(error) { status(error.message,true); }
});
handle("health","click",health);
handle("plan","submit",async form => {
  status("계획 검증 중");
  selectedTask = undefined;
  byId("run").hidden = true;
  await showTask(await api("tasks",Object.fromEntries(new FormData(form))));
  status("계획 결과를 확인하세요.");
});
handle("run","submit",async form => {
  await showTask(await api("tasks/"+selectedTask+"/run",Object.fromEntries(new FormData(form))));
  await overview();
  status("Mock 실행 결과를 확인하세요.");
});
for (const key of ["order_id","task_id","lot_id","jig_id","product_id","session_id","step_id","recipe_id","tool_id",
                   "instruction","skill_version","model_version","rule_version","calibration_version","robot_id","asset_id"]) {
  const label = node("label",key);
  const input = node("input"); input.name = key; label.append(input); byId("metadata").append(label);
}
function updateSkills() {
  byId("episode-skill").replaceChildren(...steps[byId("episode-step").value].map(skill => new Option(skill)));
}
byId("episode-step").addEventListener("change",updateSkills);
byId("collect").elements.mode.addEventListener("change", e => {
  byId("upload-fields").hidden = e.target.value === "mock";
});
function asBase64(file) {
  return new Promise((resolve,reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result.split(",",2)[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}
handle("collect","submit",async form => {
  const payload = Object.fromEntries([...new FormData(form)].filter(([,value]) => typeof value === "string" && value.trim()));
  payload.label_confirmed = form.elements.label_confirmed.checked;
  if (payload.recovery) payload.recovery_history = [payload.recovery];
  delete payload.recovery;
  if (payload.mode === "upload") {
    const file = form.elements.file.files[0];
    const video = form.elements.video.files[0];
    if (!file) throw Error("CSV, JSON 또는 JSONL 파일을 선택하세요.");
    if(file.size + (video?.size || 0) > 20*1024*1024) throw Error("합계 20 MiB까지 업로드할 수 있습니다.");
    payload.format = file.name.split(".").pop().toLowerCase(); payload.content = await file.text();
    if(video) payload.video = await asBase64(video);
  }
  const episode = await api("episodes",payload);
  const box = episodeDetail(episode);
  byId("episode-result").replaceChildren(box);
  await pages.episodes(); status("수집과 품질 검사를 완료했습니다.");
});
handle("promote","submit",async form => {
  const payload = Object.fromEntries(new FormData(form));
  payload.members = [...document.querySelectorAll("#members .member")].filter(el => el.querySelector("input").checked)
    .map(el => ({episode_id:el.querySelector("input").value,split:el.querySelector("select").value}));
  const result = await api("datasets",payload);
  await datasetList(); status(result.dataset_id+" v"+result.version+" 등록 완료");
});
handle("filters","submit",datasetList);
handle("replay","click",async () => {
  const result = await api("outbox/replay",{}); await pages.events(); status("Outbox 재전송 "+result.replayed+"건");
});
Promise.all([api("skills").then(data => { registry=data; }), api("steps").then(data => {
  stepDefinitions = data;
  selectedSkillStep = data[0]?.[0];
  steps = Object.fromEntries(data.map(([code, , ids]) => [code, ids]));
  for (const step of Object.keys(steps)) byId("episode-step").append(new Option(step));
  byId("episode-step").value = "S7";
  updateSkills();
}), health(), overview()]).catch(error => status(error.message,true));


byId("new-task").addEventListener("click", () => {
  document.querySelector('[data-page="task"]').click();
  byId("plan").elements.instruction.focus();
});
const chatHistory = [];
function bubble(message, role) {
  const log = byId("chat-log");
  const item = node("p",message,"bubble "+role);
  log.append(item);
  log.scrollTop = log.scrollHeight;
  return item;
}
handle("chat-form","submit",async form => {
  const input = form.elements.question;
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  bubble(question,"user");
  const pending = bubble("운영 기록을 확인하고 있습니다…","assistant");
  try {
    const reply = await api("chat",{question,history:chatHistory.slice(-6)});
    pending.textContent = reply.answer;
    chatHistory.push({role:"user",content:question},{role:"assistant",content:reply.answer});
    if (chatHistory.length > 6) chatHistory.splice(0,chatHistory.length-6);
  } catch(error) {
    pending.textContent = error.message;
    pending.classList.add("error");
  }
  byId("chat-log").scrollTop = byId("chat-log").scrollHeight;
});
for (const suggestion of byId("chat-suggestions").querySelectorAll("button")) {
  suggestion.addEventListener("click", () => {
    byId("question").value = suggestion.textContent;
    byId("question").focus();
  });
}

byId("task-flow").append(flow("작업 처리 순서", [
  {title:"작업지시", caption:"레시피 및 수량 입력"},
  {title:"계획 검증", caption:"S1~S8 순서 확인"},
  {title:"Mock 실행", caption:"단품 작업 승인"}
]));
byId("operation-flow").append(flow("운영 화면 바로가기", [
  {title:"작업 관리", caption:"지시 → 계획", action:() => openPage("task")},
  {title:"실행 기술", caption:"공정 → 기술", action:() => openPage("skills")},
  {title:"데이터 수집", caption:"수집 → 품질", action:() => openPage("episodes")},
  {title:"데이터셋", caption:"선택 → 등록", action:() => openPage("catalog")},
  {title:"활동 기록", caption:"이벤트 → 원인", action:() => openPage("events")}
]));
byId("collection-flow").append(flow("수집 순서", [
  {title:"데이터 준비", caption:"파일 또는 Mock"},
  {title:"품질 검사", caption:"결함 확인 및 A/B/C 판정"},
  {title:"데이터셋 등록", caption:"승격 가능 에피소드 선택", action:() => openPage("catalog")}
]));
byId("catalog-flow").append(flow("데이터셋 등록 순서", [
  {title:"에피소드 선택", caption:"승격 가능한 기록"},
  {title:"분할 지정", caption:"학습 · 검증 · 테스트"},
  {title:"버전 등록", caption:"카탈로그에서 조회"}
]));

byId("schedule-example").addEventListener("click", () => {
  const stamp = new Date();
  stamp.setSeconds(0,0);
  byId("schedule-form").elements.plan.value = JSON.stringify({
    plan_id: "WP1-DEMO-" + stamp.toISOString().replace(/[:.]/g,"-"), revision: 1,
    start_at: stamp.toISOString(),
    orders: [
      {order_id:"LH-100", recipe_id:"LH", quantity:100, priority:50},
      {order_id:"RH-20", recipe_id:"RH", quantity:20, priority:80,
       due_at:new Date(stamp.getTime() + 30*60000).toISOString()}
    ]
  },null,2);
});
byId("schedule-history").addEventListener("change", event => {
  if (event.target.value === "") byId("schedule-result").replaceChildren();
  else renderSchedule(schedules[Number(event.target.value)]);
});
handle("schedule-form","submit",async form => {
  let plan;
  try { plan = JSON.parse(form.elements.plan.value); }
  catch { throw Error("WP1 작업계획의 JSON 형식을 확인하세요."); }
  const schedule = await api("schedules",plan);
  await scheduleHistory();
  const index = schedules.findIndex(s => s.request.plan_id === schedule.request.plan_id &&
    s.request.revision === schedule.request.revision);
  byId("schedule-history").value = String(index);
  renderSchedule(schedule);
  status("셀 스케줄을 저장했습니다.");
});
