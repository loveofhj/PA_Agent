"use strict";
const byId = id => document.getElementById(id);
let selectedTask;
let registry = [];
let steps = {};
function node(tag, value, cls) {
  const el = document.createElement(tag);
  if (value !== undefined) el.textContent = value;
  if (cls) el.className = cls;
  return el;
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
const stateNames = {IDLE:"대기", PLANNING:"계획 작성 중", READY:"승인 대기", VALIDATING:"조건 확인 중",
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
  return activity(title,[when,event.StepCode,event.message].filter(Boolean).join(" · "),
    event.cause ? "확인 필요" : "",Boolean(event.cause),event);
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
  fill("recent", result.tasks, taskDetail);
  fill("alarms", result.events, eventDetail);
}
async function showTask(task) {
  selectedTask = task.TaskID;
  const plan = node("ol");
  for (const step of task.steps || []) {
    plan.append(node("li", step.StepCode + " " + step.name + " · " + step.skills.map(x => x.SkillID).join(", ")));
  }
  byId("task-result").replaceChildren(node("h2", (stateNames[task.state] || task.state) + (task.current_step ? " · " + task.current_step : "")),
    node("p", task.error || task.decision?.reason || ""), plan, taskDetail(task));
  byId("run").hidden = task.state !== "READY";
  const events = (await api("events")).filter(e => e.TaskID === selectedTask);
  fill("task-events",events,eventDetail);
}
async function showSkills() {
  const events = await api("events");
  fill("skill-list", registry, skill => {
    const latest = events.find(e => e.SkillID === skill.id && e.kind === "SKILL_RESULT");
    return detail(skill.id+" · "+skill.name+" · "+skill.steps+" · "+skill.version, {...skill, latest:latest || null});
  });
}
function episodeDetail(e) {
  return detail(e.quality.grade + " · " + e.source + " · " + e.step_code + " · " + e.skill_id +
    " · " + (e.quality.ready ? "승격 가능" : "승격 보류"),e);
}
async function datasetList() {
  const params = new URLSearchParams();
  for (const [k,v] of new FormData(byId("filters"))) if(v.trim()) params.set(k,v.trim());
  fill("datasets", await api("datasets?" + params), d => detail(
    d.dataset_id+" v"+d.version+" · "+d.source+" · "+d.episode_count+"개 · "+d.steps.join(", "),d));
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
const pages = {
  overview,
  task: async () => {},
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
  box.open = true; byId("episode-result").replaceChildren(box);
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
