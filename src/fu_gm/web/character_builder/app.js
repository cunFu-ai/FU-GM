const STORAGE_KEY = "fabula-ultima.character-workshop.v1";
const LEGACY_STORAGE_KEY = "fu-gm.character-builder.v1";
const PLACEHOLDER_PORTRAIT = "/characters/portrait-placeholder.webp";
const ATTRIBUTE_LABELS = {
  DEX: ["敏捷", "精确与反应"],
  INS: ["洞察", "观察与推理"],
  MIG: ["力量", "体魄与耐力"],
  WLP: ["意志", "决心与魅力"],
};
const SLOT_LABELS = {
  main_hand: "主手",
  off_hand: "副手",
  armor: "防具",
  shield: "盾牌",
};
const STEPS = [
  { label: "角色概念", short: "姓名、身份与主题", glyph: "Ⅰ" },
  { label: "职业组合", short: "2 至 3 个职业", glyph: "Ⅱ" },
  { label: "技能与法术", short: "分配 5 个技能等级", glyph: "Ⅲ" },
  { label: "核心属性", short: "选择并分配属性骰", glyph: "Ⅳ" },
  { label: "起始装备", short: "500 Z 采购预算", glyph: "Ⅴ" },
  { label: "羁绊与外貌", short: "联结角色与世界", glyph: "Ⅵ" },
  { label: "角色立绘", short: "Anima、Krea 与 LoRA", glyph: "Ⅶ" },
  { label: "完成角色卡", short: "命运骰与导入导出", glyph: "Ⅷ" },
];

const dom = {
  libraryView: document.querySelector("#libraryView"),
  editorView: document.querySelector("#editorView"),
  characterGrid: document.querySelector("#characterGrid"),
  characterEmpty: document.querySelector("#characterEmpty"),
  draftList: document.querySelector("#draftList"),
  draftEmpty: document.querySelector("#draftEmpty"),
  rosterCharacterCount: document.querySelector("#rosterCharacterCount"),
  characterCountLabel: document.querySelector("#characterCountLabel"),
  librarySectionTitle: document.querySelector("#librarySectionTitle"),
  draftCount: document.querySelector("#draftCount"),
  libraryNotice: document.querySelector("#libraryNotice"),
  saveIndicator: document.querySelector("#saveIndicator"),
  settingsButton: document.querySelector("#settingsButton"),
  settingsDialog: document.querySelector("#settingsDialog"),
  settingsComfyPort: document.querySelector("#settingsComfyPort"),
  settingsLlmBaseUrl: document.querySelector("#settingsLlmBaseUrl"),
  settingsLlmModel: document.querySelector("#settingsLlmModel"),
  settingsLlmApiKey: document.querySelector("#settingsLlmApiKey"),
  apiKeyState: document.querySelector("#apiKeyState"),
  comfyConnectionBadge: document.querySelector("#comfyConnectionBadge"),
  llmConnectionBadge: document.querySelector("#llmConnectionBadge"),
  workflowStatus: document.querySelector("#workflowStatus"),
  settingsNotice: document.querySelector("#settingsNotice"),
  saveSettingsButton: document.querySelector("#saveSettingsButton"),
  testComfyButton: document.querySelector("#testComfyButton"),
  testLlmButton: document.querySelector("#testLlmButton"),
  clearApiKeyButton: document.querySelector("#clearApiKeyButton"),
  editorTitle: document.querySelector("#editorTitle"),
  editorEyebrow: document.querySelector("#editorEyebrow"),
  stepNavigation: document.querySelector("#stepNavigation"),
  stepContent: document.querySelector("#stepContent"),
  characterSheet: document.querySelector("#characterSheet"),
  previousStepButton: document.querySelector("#previousStepButton"),
  nextStepButton: document.querySelector("#nextStepButton"),
  footerStatus: document.querySelector("#footerStatus"),
  editorLayout: document.querySelector("#editorLayout"),
  mobileStepNumber: document.querySelector("#mobileStepNumber"),
  mobileProgressBar: document.querySelector("#mobileProgressBar"),
  mobileStepLabel: document.querySelector("#mobileStepLabel"),
  fileInput: document.querySelector("#fileInput"),
  importDialog: document.querySelector("#importDialog"),
  importDialogTitle: document.querySelector("#importDialogTitle"),
  importDialogContent: document.querySelector("#importDialogContent"),
  importDialogActions: document.querySelector("#importDialogActions"),
  confirmDialog: document.querySelector("#confirmDialog"),
  confirmTitle: document.querySelector("#confirmTitle"),
  confirmContent: document.querySelector("#confirmContent"),
  confirmAcceptButton: document.querySelector("#confirmAcceptButton"),
  portraitViewer: document.querySelector("#portraitViewer"),
  portraitViewerImage: document.querySelector("#portraitViewerImage"),
  portraitViewerClose: document.querySelector("#portraitViewerClose"),
  toastRegion: document.querySelector("#toastRegion"),
};

const state = {
  catalog: null,
  settings: null,
  characters: [],
  drafts: {},
  activeDraftId: "",
  currentStep: 0,
  preview: null,
  previewError: "",
  equipmentTab: "weapons",
  equipmentSearch: "",
  portraitJobTimer: null,
  pendingConfirm: null,
};

class APIError extends Error {
  constructor(message, status, data) {
    super(message);
    this.name = "APIError";
    this.status = status;
    this.data = data;
  }
}

function node(tag, attributes = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") element.className = value;
    else if (key === "text") element.textContent = String(value);
    else if (key === "value") element.value = value;
    else if (key === "checked") element.checked = Boolean(value);
    else if (key === "disabled") element.disabled = Boolean(value);
    else if (key === "selected") element.selected = Boolean(value);
    else if (key === "dataset") Object.assign(element.dataset, value);
    else if (key === "style") Object.assign(element.style, value);
    else if (key.startsWith("on") && typeof value === "function") {
      element.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (key in element && !key.startsWith("aria")) {
      try { element[key] = value; } catch { element.setAttribute(key, value); }
    } else {
      element.setAttribute(key, value === true ? "" : String(value));
    }
  }
  const values = Array.isArray(children) ? children : [children];
  for (const child of values.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return element;
}

function clear(element) {
  element.replaceChildren();
  return element;
}

function deepClone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

function getPath(object, path) {
  return path.split(".").reduce((value, key) => value?.[key], object);
}

function setPath(object, path, value) {
  const parts = path.split(".");
  const final = parts.pop();
  let target = object;
  for (const part of parts) {
    if (!target[part] || typeof target[part] !== "object") target[part] = {};
    target = target[part];
  }
  target[final] = value;
}

function randomId() {
  return globalThis.crypto?.randomUUID?.() || `draft-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function randomD6() {
  if (globalThis.crypto?.getRandomValues) {
    const value = new Uint32Array(1);
    globalThis.crypto.getRandomValues(value);
    return (value[0] % 6) + 1;
  }
  return Math.floor(Math.random() * 6) + 1;
}

function defaultDraft() {
  const now = new Date().toISOString();
  return {
    id: randomId(),
    card_id: "",
    revision: 1,
    created_at: now,
    updated_at: now,
    build: {
      player_name: "",
      hero_name: "",
      identity: "",
      theme: "",
      origin: "",
      classes: {},
      attributes: { DEX: 10, INS: 8, MIG: 8, WLP: 6 },
      bonds: [],
      skills: {},
      skill_options: {},
      spells: [],
      bound_arcana: [],
      abilities: [],
      equipment: [],
      equipment_slots: { main_hand: "", off_hand: "", armor: "", shield: "" },
      notes: [],
      fate_roll: [],
    },
    presentation: {
      appearance: {
        species: "",
        age: "",
        gender_presentation: "",
        body: "",
        skin: "",
        hair: "",
        eyes: "",
        face: "",
        marks: "",
        outfit: "",
        armor: "",
        accessories: "",
        weapon: "",
        magic: "",
        pose: "自然站姿",
        expression: "",
        framing: "全身立绘，2:3 竖幅",
        palette: "",
        lighting: "柔和日光",
        background: "简洁、便于辨认角色的背景",
        style_notes: "原创 JRPG 角色设计",
        world_style: "",
        magic_tech_role: "",
      },
      portrait: {
        asset_url: "",
        model_profile: "anima",
        allow_creative_fill: true,
        positive_prompt: "",
        negative_prompt: "",
        style_notes: "",
        prompt_source: "",
        prompt_version: "",
        seed: "",
        job_id: "",
      },
    },
    extensions: {},
  };
}

function activeDraft() {
  return state.drafts[state.activeDraftId] || null;
}

function restoreLocalState() {
  try {
    const stored = JSON.parse(
      localStorage.getItem(STORAGE_KEY)
      || localStorage.getItem(LEGACY_STORAGE_KEY)
      || "{}",
    );
    if (stored && typeof stored === "object") {
      state.drafts = stored.drafts && typeof stored.drafts === "object" ? stored.drafts : {};
      state.activeDraftId = String(stored.activeDraftId || "");
    }
  } catch {
    state.drafts = {};
  }
  for (const [id, draft] of Object.entries(state.drafts)) {
    if (!draft || typeof draft !== "object" || !draft.build) delete state.drafts[id];
  }
}

function persistLocalState(label = "已保存") {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({
    drafts: state.drafts,
    activeDraftId: state.activeDraftId,
  }));
  localStorage.removeItem(LEGACY_STORAGE_KEY);
  dom.saveIndicator.textContent = label;
}

function touchDraft({ rerenderSheet = true, requestPreview = true } = {}) {
  const draft = activeDraft();
  if (!draft) return;
  draft.updated_at = new Date().toISOString();
  dom.saveIndicator.textContent = "保存中…";
  persistLocalState("已保存");
  updateEditorHeading();
  if (rerenderSheet) renderCharacterSheet();
  renderStepNavigation();
  updateFooter();
  if (requestPreview) schedulePreview();
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = typeof data === "object"
      ? data.error || data.errors?.[0] || `请求失败（${response.status}）`
      : `请求失败（${response.status}）`;
    throw new APIError(message, response.status, data);
  }
  return data;
}

function showToast(message, kind = "success", timeout = 3300) {
  const toast = node("div", { class: `toast ${kind}` }, [node("div", { text: message })]);
  dom.toastRegion.append(toast);
  window.setTimeout(() => toast.remove(), timeout);
}

function setConnectionBadge(element, message, kind = "") {
  element.textContent = message;
  element.className = `connection-badge ${kind}`.trim();
}

function renderWorkflowStatus(workflows = {}) {
  const labels = {
    anima: "Anima",
    krea2: "Krea 2",
    krea_lora: "Krea 2 + LoRA",
  };
  clear(dom.workflowStatus);
  for (const [profile, label] of Object.entries(labels)) {
    const ready = workflows?.[profile] === true;
    dom.workflowStatus.append(node("span", {
      class: `workflow-chip ${ready ? "ready" : ""}`.trim(),
      text: `${label}：${ready ? "工作流已就绪" : "缺少 JSON"}`,
    }));
  }
}

function renderSettings({ resetConnectionBadges = true } = {}) {
  if (!state.settings) return;
  dom.settingsComfyPort.value = state.settings.comfyui?.port || 8188;
  dom.settingsLlmBaseUrl.value = state.settings.llm?.api_base_url || "";
  dom.settingsLlmModel.value = state.settings.llm?.model || "";
  dom.settingsLlmApiKey.value = "";
  dom.apiKeyState.textContent = state.settings.llm?.api_key_configured
    ? "本次运行已设置"
    : "尚未填写";
  renderWorkflowStatus(state.settings.comfyui?.workflows || {});
  dom.settingsNotice.hidden = true;
  if (resetConnectionBadges) {
    setConnectionBadge(dom.comfyConnectionBadge, "未测试");
    setConnectionBadge(
      dom.llmConnectionBadge,
      state.settings.llm?.api_key_configured ? "Key 已设置" : "等待 Key",
    );
  }
}

function settingsRequestBody({ includeApiKey = true, clearApiKey = false } = {}) {
  const llm = {
    api_base_url: dom.settingsLlmBaseUrl.value.trim(),
    model: dom.settingsLlmModel.value.trim(),
  };
  const key = dom.settingsLlmApiKey.value.trim();
  if (includeApiKey && key) llm.api_key = key;
  if (clearApiKey) llm.clear_api_key = true;
  return {
    comfyui: { port: Number(dom.settingsComfyPort.value) },
    llm,
  };
}

async function loadWorkshopSettings() {
  if (!state.catalog?.capabilities?.connection_settings) {
    dom.settingsButton.hidden = true;
    return;
  }
  dom.settingsButton.hidden = false;
  state.settings = await api("/v1/workshop/settings");
  renderSettings();
}

async function saveWorkshopSettings({ announce = true } = {}) {
  const result = await api("/v1/workshop/settings", {
    method: "POST",
    body: JSON.stringify(settingsRequestBody()),
  });
  state.settings = result;
  renderSettings({ resetConnectionBadges: false });
  state.catalog = await api("/v1/character-builder/catalog");
  if (activeDraft()) {
    renderCurrentStep();
    renderStepNavigation();
    updateFooter();
  }
  if (announce) showToast("生成设置已保存。", "success");
  return result;
}

async function runSettingsTest(kind) {
  const button = kind === "comfyui" ? dom.testComfyButton : dom.testLlmButton;
  const badge = kind === "comfyui" ? dom.comfyConnectionBadge : dom.llmConnectionBadge;
  button.disabled = true;
  setConnectionBadge(badge, "正在连接…");
  try {
    await saveWorkshopSettings({ announce: false });
    const result = await api(`/v1/workshop/settings/test-${kind}`, {
      method: "POST",
      body: "{}",
    });
    const detail = kind === "comfyui" && result.version
      ? `已连接 ${result.version}`
      : "连接成功";
    setConnectionBadge(badge, detail, "success");
    if (kind === "comfyui") renderWorkflowStatus(result.workflows || {});
    showToast(result.message || "连接成功。", "success");
  } catch (error) {
    setConnectionBadge(badge, "连接失败", "error");
    dom.settingsNotice.hidden = false;
    dom.settingsNotice.textContent = error.message || "连接测试失败。";
  } finally {
    button.disabled = false;
  }
}

async function clearWorkshopApiKey() {
  try {
    state.settings = await api("/v1/workshop/settings", {
      method: "POST",
      body: JSON.stringify(settingsRequestBody({ includeApiKey: false, clearApiKey: true })),
    });
    renderSettings();
    showToast("本次运行中的 API Key 已清除。", "success");
  } catch (error) {
    showToast(error.message || "无法清除 API Key。", "error");
  }
}

async function openSettingsDialog() {
  try {
    if (!state.settings) await loadWorkshopSettings();
    renderSettings();
    dom.settingsDialog.showModal();
    dom.settingsDialog.querySelector(".dialog-content").scrollTop = 0;
  } catch (error) {
    showToast(error.message || "无法读取生成设置。", "error");
  }
}

function formatDate(value) {
  if (!value) return "刚刚";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "刚刚";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function safeFilename(name) {
  return String(name || "未命名角色").replace(/[\\/:*?"<>|]/g, "_").slice(0, 80);
}

function downloadJSON(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = node("a", { href: url, download: filename });
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function downloadText(content, filename) {
  const blob = new Blob(["\ufeff", String(content || "")], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = node("a", { href: url, download: filename });
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function inputField(labelText, path, options = {}) {
  const draft = activeDraft();
  const field = node("div", { class: `field ${options.full ? "full" : ""}` });
  const label = node("label", { htmlFor: options.id || path.replaceAll(".", "-") }, [
    node("span", { text: labelText }),
    options.hint ? node("small", { text: options.hint }) : null,
  ]);
  let control;
  const common = {
    id: options.id || path.replaceAll(".", "-"),
    name: path,
    placeholder: options.placeholder || "",
    value: getPath(draft, path) || "",
  };
  if (options.type === "textarea") {
    control = node("textarea", { ...common, rows: options.rows || 3, maxLength: options.maxLength || 1000 });
  } else if (options.options) {
    control = node("select", { id: common.id, name: path }, options.options.map((item) => {
      const value = typeof item === "object" ? item.value : item;
      const text = typeof item === "object" ? item.label : item;
      return node("option", { value, text, selected: String(value) === String(common.value) });
    }));
  } else {
    control = node("input", { ...common, type: options.type || "text", maxLength: options.maxLength || 200, autocomplete: options.autocomplete || "off" });
  }
  control.addEventListener("input", () => {
    setPath(draft, path, control.value);
    touchDraft();
  });
  field.append(label, control);
  if (options.help) field.append(node("p", { class: "field-help", text: options.help }));
  return field;
}

function stepIntro(title, description, glyph) {
  return node("div", { class: "step-intro" }, [
    node("div", { class: "step-intro-top" }, [
      node("span", { class: "step-glyph", text: glyph, "aria-hidden": "true" }),
      node("h2", { text: title }),
    ]),
    node("p", { text: description }),
  ]);
}

function counter(value, minimum, maximum, onChange) {
  const minus = node("button", { type: "button", text: "−", title: "减少", "aria-label": "减少", disabled: value <= minimum });
  const plus = node("button", { type: "button", text: "+", title: "增加", "aria-label": "增加", disabled: value >= maximum });
  minus.addEventListener("click", () => onChange(Math.max(minimum, value - 1)));
  plus.addEventListener("click", () => onChange(Math.min(maximum, value + 1)));
  return node("div", { class: "counter" }, [minus, node("output", { text: value }), plus]);
}

function rankTotal(map) {
  return Object.values(map || {}).reduce((sum, value) => sum + Number(value || 0), 0);
}

function selectedClassNames(draft = activeDraft()) {
  return Object.entries(draft?.build.classes || {}).filter(([, level]) => level > 0).map(([name]) => name);
}

function canonicalSpellName(name) {
  const clean = String(name || "").trim();
  return state.catalog?.spell_aliases?.[clean] || clean;
}

function playerFacingSkillText(value) {
  return String(value || "").replace(/\bSL\b/gi, "技能等级");
}

function openPortraitViewer(image, altText) {
  const source = image.currentSrc || image.src;
  if (!source) return;
  dom.portraitViewerImage.src = source;
  dom.portraitViewerImage.alt = altText || image.alt || "角色立绘大图";
  if (!dom.portraitViewer.open) dom.portraitViewer.showModal();
}

function enablePortraitViewer(image, altText) {
  image.classList.add("inspectable-portrait");
  image.tabIndex = 0;
  image.setAttribute("role", "button");
  image.setAttribute("aria-label", `${altText || image.alt || "角色立绘"}，双击或按回车查看大图`);
  image.title = "双击查看大图";
  image.addEventListener("dblclick", () => openPortraitViewer(image, altText));
  image.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openPortraitViewer(image, altText);
  });
}

function normalizeDraftSpells(draft, { filterBySkills = true } = {}) {
  if (!draft?.build) return;
  const allowedSchools = new Set();
  for (const [skillName, school] of Object.entries(state.catalog?.spell_granting_skills || {})) {
    if (Number(draft.build.skills?.[skillName] || 0) > 0) allowedSchools.add(school);
  }
  const normalized = [];
  for (const rawName of draft.build.spells || []) {
    const name = canonicalSpellName(rawName);
    const spell = state.catalog?.spells?.find((item) => item.name === name);
    if (!spell || (filterBySkills && !allowedSchools.has(spell.school))) continue;
    if (!normalized.includes(name)) normalized.push(name);
  }
  draft.build.spells = normalized;
}

function classAbilities(draft = activeDraft()) {
  const abilities = new Set(draft?.build.abilities || []);
  for (const className of selectedClassNames(draft)) {
    const entry = state.catalog?.classes?.find((item) => item.name === className);
    for (const ability of entry?.benefit?.abilities || []) abilities.add(ability);
  }
  return abilities;
}

function classBenefitText(classEntry) {
  const parts = [];
  if (classEntry.benefit.hp) parts.push(`HP +${classEntry.benefit.hp}`);
  if (classEntry.benefit.mp) parts.push(`MP +${classEntry.benefit.mp}`);
  if (classEntry.benefit.ip) parts.push(`IP +${classEntry.benefit.ip}`);
  return parts.length ? parts : ["职业能力"];
}

function renderConceptStep() {
  const fragment = document.createDocumentFragment();
  fragment.append(stepIntro(
    "写下英雄的第一句话",
    "身份、主题与故乡都是可在检定中援用的角色特质，也会成为故事持续回望的坐标。",
    "Ⅰ",
  ));
  const grid = node("div", { class: "form-grid" }, [
    inputField("角色名", "build.hero_name", { placeholder: "例如：伊莎贝尔", hint: "必填" }),
    inputField("玩家名", "build.player_name", { placeholder: "记录操控这位英雄的人" }),
    inputField("身份", "build.identity", { placeholder: "例如：失去王国的飞空骑士", hint: "一句话" }),
    inputField("主题", "build.theme", { placeholder: "例如：希望、愧疚、正义或野心" }),
    inputField("故乡", "build.origin", { placeholder: "例如：云海之上的索朗旧都", full: true }),
    inputField("角色笔记", "build.notes_text", {
      type: "textarea",
      placeholder: "重要誓言、习惯、待在故事中回答的问题……每行一项",
      full: true,
    }),
  ]);
  const notes = activeDraft().build.notes || [];
  const notesControl = grid.querySelector('[name="build.notes_text"]');
  notesControl.value = notes.join("\n");
  notesControl.addEventListener("input", () => {
    activeDraft().build.notes = notesControl.value.split("\n").map((item) => item.trim()).filter(Boolean);
  });
  fragment.append(grid);
  return fragment;
}

function renderClassesStep() {
  const draft = activeDraft();
  const fragment = document.createDocumentFragment();
  fragment.append(stepIntro(
    "组合你的职业道路",
    "起始英雄拥有 5 个职业等级，分布在 2 至 3 个职业中。每个职业等级都会对应一个技能等级。",
    "Ⅱ",
  ));
  const total = rankTotal(draft.build.classes);
  const count = selectedClassNames().length;
  fragment.append(node("div", { class: `allocation-bar ${total === 5 && count >= 2 && count <= 3 ? "" : "error"}` }, [
    node("span", { text: `${count} 个职业已激活` }),
    node("strong", { text: `${total} / 5 级` }),
  ]));
  const chooser = node("div", { class: "class-chooser" });
  for (const classEntry of state.catalog?.classes || []) {
    const value = Number(draft.build.classes[classEntry.name] || 0);
    const selected = value > 0;
    const card = node("article", { class: `class-choice ${selected ? "selected" : ""}` });
    const change = (next) => {
      const currentClasses = draft.build.classes;
      const selectedCount = selectedClassNames().length;
      if (next > 0 && value === 0 && selectedCount >= 3) {
        showToast("起始角色最多选择 3 个职业。", "warning");
        return;
      }
      const newTotal = total - value + next;
      if (newTotal > 5) {
        showToast("起始职业总等级不能超过 5。", "warning");
        return;
      }
      if (next <= 0) {
        delete currentClasses[classEntry.name];
        for (const skill of classEntry.skills || []) delete draft.build.skills[skill.name];
      } else {
        currentClasses[classEntry.name] = next;
      }
      pruneSkillSelections();
      touchDraft();
      renderCurrentStep();
    };
    card.append(node("div", { class: "class-choice-header" }, [
      node("h3", { text: classEntry.name }),
      counter(value, 0, 5, change),
    ]));
    card.append(node("div", { class: "benefit-dots" }, classBenefitText(classEntry).map((item) => node("span", { text: item }))));
    const ability = classEntry.benefit.abilities?.join("、") || `${classEntry.skills?.length || 0} 项职业技能`;
    card.append(node("p", { text: ability }));
    chooser.append(card);
  }
  fragment.append(chooser);
  return fragment;
}

function pruneSkillSelections() {
  const draft = activeDraft();
  const legalSkills = new Set();
  for (const className of selectedClassNames()) {
    const classEntry = state.catalog?.classes?.find((item) => item.name === className);
    for (const skill of classEntry?.skills || []) legalSkills.add(skill.name);
  }
  for (const skillName of Object.keys(draft.build.skills || {})) {
    if (!legalSkills.has(skillName) || draft.build.skills[skillName] <= 0) {
      delete draft.build.skills[skillName];
      delete draft.build.skill_options[skillName];
    }
  }
  normalizeDraftSpells(draft);
  if (!(draft.build.skills["契约与召唤"] > 0)) draft.build.bound_arcana = [];
}

function skillOptionsBlock(skillName, rank) {
  const draft = activeDraft();
  if (skillName === "便携装置") {
    const current = draft.build.skill_options[skillName] || [];
    const block = node("div", { class: "skill-options-panel" }, [
      node("div", { class: "group-label" }, [node("span", { text: "装置分配" }), node("small", { text: `${current.length} / ${rank}` })]),
    ]);
    for (let index = 0; index < rank; index += 1) {
      const field = node("div", { class: "field", style: { marginTop: index ? "8px" : "0" } });
      const select = node("select", {}, [
        node("option", { value: "", text: `第 ${index + 1} 次选择…`, selected: !current[index] }),
        ...(state.catalog?.portable_device_types || []).map((type) => node("option", { value: type, text: type, selected: current[index] === type })),
      ]);
      select.addEventListener("change", () => {
        const next = [...current];
        next[index] = select.value;
        draft.build.skill_options[skillName] = next.filter(Boolean);
        touchDraft();
        renderCurrentStep();
      });
      field.append(select);
      block.append(field);
    }
    return block;
  }
  if (["拟兽系仪式", "形意咒法"].includes(skillName)) {
    const value = draft.build.skill_options[skillName]?.[0] || "";
    const block = node("div", { class: "skill-options-panel" }, [
      node("div", { class: "group-label" }, [node("span", { text: "固定施法属性" }), node("small", { text: "选择一次" })]),
    ]);
    const choices = node("div", { class: "choice-grid" });
    for (const choice of ["洞察+意志", "力量+意志"]) {
      const button = node("button", { type: "button", class: `choice-card ${value === choice ? "selected" : ""}` }, [
        node("strong", { text: `【${choice}】` }),
        node("small", { text: "用于该技能的固定检定组合" }),
      ]);
      button.addEventListener("click", () => {
        draft.build.skill_options[skillName] = [choice];
        touchDraft();
        renderCurrentStep();
      });
      choices.append(button);
    }
    block.append(choices);
    return block;
  }
  return null;
}

function renderSpellSelections() {
  const draft = activeDraft();
  const containers = [];
  for (const [skillName, school] of Object.entries(state.catalog?.spell_granting_skills || {})) {
    const required = Number(draft.build.skills[skillName] || 0);
    if (!required) continue;
    const selected = (draft.build.spells || []).filter((name) => state.catalog.spells.find((spell) => spell.name === name)?.school === school);
    const section = node("section", { class: "skill-group" }, [
      node("div", { class: "skill-group-heading" }, [
        node("h3", { text: school }),
        node("span", { class: selected.length === required ? "soft-tag" : "warning-chip", text: `${selected.length} / ${required}` }),
      ]),
    ]);
    const grid = node("div", { class: "spell-grid" });
    for (const spell of state.catalog.spells.filter((item) => item.school === school)) {
      const checked = draft.build.spells.includes(spell.name);
      const checkbox = node("input", { type: "checkbox", checked });
      checkbox.addEventListener("change", () => {
        const selectedNow = (draft.build.spells || []).filter((name) => state.catalog.spells.find((item) => item.name === canonicalSpellName(name))?.school === school);
        if (checkbox.checked && selectedNow.length >= required) {
          checkbox.checked = false;
          showToast(`${school}只能选择 ${required} 个法术。`, "warning");
          return;
        }
        if (checkbox.checked) draft.build.spells = [...new Set([...(draft.build.spells || []).map(canonicalSpellName), spell.name])];
        else draft.build.spells = (draft.build.spells || []).map(canonicalSpellName).filter((name) => name !== spell.name);
        touchDraft();
        renderCurrentStep();
      });
      grid.append(node("label", { class: "check-choice" }, [
        checkbox,
        node("span", {}, [
          node("strong", { text: spell.name }),
          node("small", { text: `${spell.mp_cost} MP · ${spell.target} · ${spell.description}` }),
        ]),
      ]));
    }
    section.append(grid);
    containers.push(section);
  }
  if ((draft.build.skills["契约与召唤"] || 0) > 0) {
    const section = node("section", { class: "skill-group" }, [
      node("div", { class: "skill-group-heading" }, [node("h3", { text: "绑定奥灵" }), node("span", { class: "soft-tag", text: "至少一位" })]),
    ]);
    const grid = node("div", { class: "arcana-grid" });
    for (const arcana of state.catalog.arcana || []) {
      const checked = draft.build.bound_arcana.includes(arcana);
      const checkbox = node("input", { type: "checkbox", checked });
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) draft.build.bound_arcana.push(arcana);
        else draft.build.bound_arcana = draft.build.bound_arcana.filter((item) => item !== arcana);
        touchDraft();
        renderCurrentStep();
      });
      grid.append(node("label", { class: "check-choice" }, [checkbox, node("span", {}, node("strong", { text: arcana }))]));
    }
    section.append(grid);
    containers.push(section);
  }
  return containers;
}

function renderSkillsStep() {
  const draft = activeDraft();
  const fragment = document.createDocumentFragment();
  fragment.append(stepIntro(
    "把等级变成独特能力",
    "每个职业中的技能等级总数必须与该职业等级相同。可重复选择的技能不能超过其等级上限。",
    "Ⅲ",
  ));
  const groups = node("div", { class: "skill-groups" });
  if (!selectedClassNames().length) {
    groups.append(node("div", { class: "validation-panel invalid", text: "请先在上一步选择职业。" }));
  }
  for (const className of selectedClassNames()) {
    const classEntry = state.catalog.classes.find((item) => item.name === className);
    const required = Number(draft.build.classes[className] || 0);
    const chosen = (classEntry.skills || []).reduce((sum, skill) => sum + Number(draft.build.skills[skill.name] || 0), 0);
    const group = node("section", { class: "skill-group" }, [
      node("div", { class: "skill-group-heading" }, [
        node("h3", { text: `${className}技能` }),
        node("span", { class: chosen === required ? "soft-tag" : "warning-chip", text: `${chosen} / ${required}` }),
      ]),
    ]);
    const list = node("div", { class: "skill-list" });
    for (const skill of classEntry.skills || []) {
      const value = Number(draft.build.skills[skill.name] || 0);
      const row = node("div", { class: "skill-row" }, [
        node("div", { class: "skill-name" }, [
          node("strong", { text: skill.name }),
          node("small", { text: skill.max_ranks > 1 ? `最高 ${skill.max_ranks} 级` : "一次性技能" }),
        ]),
        node("div", { class: "skill-summary", text: playerFacingSkillText(skill.summary || "职业技能") }),
        counter(value, 0, skill.max_ranks, (next) => {
          const nextChosen = chosen - value + next;
          if (nextChosen > required) {
            showToast(`${className}只有 ${required} 个技能等级。`, "warning");
            return;
          }
          if (next > 0) draft.build.skills[skill.name] = next;
          else {
            delete draft.build.skills[skill.name];
            delete draft.build.skill_options[skill.name];
          }
          pruneSkillSelections();
          touchDraft();
          renderCurrentStep();
        }),
      ]);
      list.append(row);
      if (value > 0) {
        const options = skillOptionsBlock(skill.name, value);
        if (options) list.append(options);
      }
    }
    group.append(list);
    groups.append(group);
  }
  for (const extra of renderSpellSelections()) groups.append(extra);
  fragment.append(groups);
  return fragment;
}

function currentAttributePattern() {
  const values = Object.values(activeDraft()?.build.attributes || {}).map(Number).sort((a, b) => a - b);
  return state.catalog?.attribute_patterns?.find((pattern) => [...pattern.dice].map(Number).sort((a, b) => a - b).join(",") === values.join(",")) || null;
}

function applyAttributePattern(pattern) {
  const draft = activeDraft();
  const order = ["DEX", "INS", "MIG", "WLP"];
  order.forEach((attribute, index) => { draft.build.attributes[attribute] = Number(pattern.dice[index]); });
  touchDraft();
  renderCurrentStep();
}

function renderAttributesStep() {
  const draft = activeDraft();
  const fragment = document.createDocumentFragment();
  fragment.append(stepIntro(
    "为四项属性分配骰子",
    "选择一种规则书属性组合，再把骰子分配给敏捷、洞察、力量和意志。相同组合可以有不同排列。",
    "Ⅳ",
  ));
  const activePattern = currentAttributePattern();
  const tabs = node("div", { class: "pattern-tabs", role: "group", "aria-label": "属性组合" });
  for (const pattern of state.catalog?.attribute_patterns || []) {
    const button = node("button", {
      type: "button",
      class: activePattern?.name === pattern.name ? "active" : "",
      text: `${pattern.name} · ${pattern.dice.map((die) => `d${die}`).join("/")}`,
    });
    button.addEventListener("click", () => applyAttributePattern(pattern));
    tabs.append(button);
  }
  fragment.append(tabs);
  const grid = node("div", { class: "attribute-grid" });
  for (const [attribute, [label, summary]] of Object.entries(ATTRIBUTE_LABELS)) {
    const control = node("div", { class: "attribute-control", dataset: { attribute } }, [
      node("label", { htmlFor: `attribute-${attribute}`, text: label }),
      node("small", { text: summary }),
    ]);
    const select = node("select", { id: `attribute-${attribute}`, "aria-label": `${label}骰` }, [6, 8, 10, 12].map((die) => node("option", {
      value: die,
      text: `d${die}`,
      selected: Number(draft.build.attributes[attribute]) === die,
    })));
    select.addEventListener("change", () => {
      draft.build.attributes[attribute] = Number(select.value);
      touchDraft();
      renderCurrentStep();
    });
    control.append(select);
    grid.append(control);
  }
  fragment.append(grid);
  const selectedValues = Object.values(draft.build.attributes).map((value) => `d${value}`).join(" / ");
  fragment.append(node("div", {
    class: `validation-panel ${activePattern ? "valid" : "invalid"}`,
    text: activePattern ? `当前采用【${activePattern.name}】组合：${selectedValues}` : `当前骰组 ${selectedValues} 不属于起始属性组合。`,
  }));
  return fragment;
}

function allEquipment() {
  if (!state.catalog?.equipment) return [];
  return [
    ...(state.catalog.equipment.weapons || []).map((item) => ({ ...item, group: "weapons" })),
    ...(state.catalog.equipment.armor || []).map((item) => ({ ...item, group: "armor" })),
    ...(state.catalog.equipment.shields || []).map((item) => ({ ...item, group: "shields" })),
  ];
}

function equipmentByName(name) {
  return allEquipment().find((item) => item.name === name) || null;
}

function equipmentCost(draft = activeDraft()) {
  return (draft?.build.equipment || []).reduce((sum, name) => sum + Number(equipmentByName(name)?.price || 0), 0);
}

function equipmentQuantity(name, draft = activeDraft()) {
  return (draft?.build.equipment || []).filter((itemName) => itemName === name).length;
}

function equipmentDetail(item) {
  if (item.group === "weapons") {
    const accuracy = (item.accuracy_attributes || []).join("+");
    return `【${accuracy}】${Number(item.accuracy_modifier || 0) >= 0 ? "+" : ""}${item.accuracy_modifier || 0} · 高值+${item.damage_bonus || 0} · ${item.hands || 1} 手`;
  }
  if (item.group === "armor") {
    const physical = typeof item.physical_base === "string" ? `${item.physical_base}${item.physical_bonus ? `+${item.physical_bonus}` : ""}` : item.physical_base;
    const magic = typeof item.magic_base === "string" ? `${item.magic_base}${item.magic_bonus ? `+${item.magic_bonus}` : ""}` : item.magic_base;
    return `物防 ${physical} · 魔防 ${magic} · 先攻 ${item.initiative_modifier || 0}`;
  }
  return `物防 +${item.physical_bonus || 0} · 魔防 +${item.magic_bonus || 0}`;
}

function pruneEquipmentSlots() {
  const draft = activeDraft();
  const owned = new Set(draft.build.equipment || []);
  for (const slot of Object.keys(SLOT_LABELS)) {
    if (draft.build.equipment_slots[slot] && !owned.has(draft.build.equipment_slots[slot])) {
      draft.build.equipment_slots[slot] = "";
    }
  }
  const mainHand = draft.build.equipment_slots.main_hand;
  if (mainHand && draft.build.equipment_slots.off_hand === mainHand && equipmentQuantity(mainHand, draft) < 2) {
    draft.build.equipment_slots.off_hand = "";
  }
}

function setEquipmentQuantity(item, quantity) {
  const draft = activeDraft();
  const current = equipmentQuantity(item.name, draft);
  const next = Math.max(0, Number(quantity || 0));
  if (next > current && item.required_ability && !classAbilities().has(item.required_ability)) {
    showToast(`需要能力【${item.required_ability}】。`, "warning");
    return;
  }
  draft.build.equipment = [
    ...(draft.build.equipment || []).filter((name) => name !== item.name),
    ...Array.from({ length: next }, () => item.name),
  ];
  pruneEquipmentSlots();
  touchDraft();
  renderCurrentStep();
}

function renderEquipmentSlots() {
  const draft = activeDraft();
  const selected = [...new Set(draft.build.equipment || [])].map(equipmentByName).filter(Boolean);
  const candidates = {
    main_hand: selected.filter((item) => item.group === "weapons"),
    off_hand: selected.filter((item) => item.group === "weapons" && Number(item.hands || 1) === 1),
    armor: selected.filter((item) => item.group === "armor"),
    shield: selected.filter((item) => item.group === "shields"),
  };
  const grid = node("div", { class: "slots-grid" });
  for (const [slot, label] of Object.entries(SLOT_LABELS)) {
    const field = node("div", { class: "field" }, [node("label", { htmlFor: `slot-${slot}`, text: label })]);
    const select = node("select", { id: `slot-${slot}` }, [
      node("option", { value: "", text: slot === "main_hand" ? "自动选择" : "不装备", selected: !draft.build.equipment_slots[slot] }),
      ...candidates[slot].map((item) => node("option", {
        value: item.name,
        text: equipmentQuantity(item.name, draft) > 1 ? `${item.name} ×${equipmentQuantity(item.name, draft)}` : item.name,
        selected: draft.build.equipment_slots[slot] === item.name,
      })),
    ]);
    select.addEventListener("change", () => {
      const previous = draft.build.equipment_slots[slot] || "";
      draft.build.equipment_slots[slot] = select.value;
      const mainHand = draft.build.equipment_slots.main_hand || candidates.main_hand[0]?.name || "";
      const offHand = draft.build.equipment_slots.off_hand || "";
      if (mainHand && mainHand === offHand && equipmentQuantity(mainHand, draft) < 2) {
        draft.build.equipment_slots[slot] = previous;
        select.value = previous;
        showToast(`双持【${mainHand}】需要购买两件。`, "warning");
        return;
      }
      touchDraft();
    });
    field.append(select);
    grid.append(field);
  }
  return grid;
}

function renderEquipmentStep() {
  const draft = activeDraft();
  const fragment = document.createDocumentFragment();
  fragment.append(stepIntro(
    "为旅途准备合适的装备",
    "起始装备共用 500 Z 预算。职业武器、职业防具和职业盾牌会依据你的职业组合自动开放。",
    "Ⅴ",
  ));
  const spent = equipmentCost();
  const toolbar = node("div", { class: "equipment-toolbar" });
  const search = node("input", {
    class: "search-input",
    type: "search",
    value: state.equipmentSearch,
    placeholder: "搜索装备",
    "aria-label": "搜索装备",
  });
  search.addEventListener("input", () => {
    state.equipmentSearch = search.value;
    renderCurrentStep();
    const nextSearch = dom.stepContent.querySelector(".search-input");
    nextSearch?.focus();
    nextSearch?.setSelectionRange(state.equipmentSearch.length, state.equipmentSearch.length);
  });
  toolbar.append(search, node("div", { class: `budget-meter ${spent > 500 ? "over" : ""}` }, [
    node("strong", { text: `${spent} / 500 Z` }),
    node("small", { text: spent > 500 ? `超出 ${spent - 500} Z` : `剩余 ${500 - spent} Z` }),
  ]));
  fragment.append(toolbar);
  const tabs = node("div", { class: "equipment-tabs", role: "tablist" });
  for (const [group, label] of [["weapons", "武器"], ["armor", "防具"], ["shields", "盾牌"]]) {
    const button = node("button", { type: "button", class: state.equipmentTab === group ? "active" : "", text: label });
    button.addEventListener("click", () => {
      state.equipmentTab = group;
      renderCurrentStep();
    });
    tabs.append(button);
  }
  fragment.append(tabs);
  const list = node("div", { class: "equipment-list" });
  const abilities = classAbilities();
  const searchText = state.equipmentSearch.trim().toLowerCase();
  const items = (state.catalog?.equipment?.[state.equipmentTab] || []).filter((item) => !searchText || `${item.name} ${item.category || ""}`.toLowerCase().includes(searchText));
  for (const item of items) {
    const quantity = equipmentQuantity(item.name, draft);
    const selected = quantity > 0;
    const available = !item.required_ability || abilities.has(item.required_ability);
    const maximum = Number(item.price || 0) > 0 ? Math.max(1, Math.min(9, Math.floor(500 / Number(item.price)))) : 1;
    const quantityControl = counter(quantity, 0, maximum, (next) => setEquipmentQuantity({ ...item, group: state.equipmentTab }, next));
    quantityControl.classList.add("equipment-quantity");
    for (const button of quantityControl.querySelectorAll("button")) button.disabled = button.disabled || !available;
    const row = node("div", { class: `equipment-row ${selected ? "selected" : ""} ${available ? "" : "unavailable"}` }, [
      quantityControl,
      node("span", { class: "equipment-name" }, [
        node("strong", { text: item.name }),
        node("small", { text: available ? (item.category || (state.equipmentTab === "armor" ? "防具" : "盾牌")) : `需要 ${item.required_ability}` }),
      ]),
      node("span", { class: "equipment-detail" }, [
        node("strong", { text: equipmentDetail({ ...item, group: state.equipmentTab }) }),
        node("small", { text: item.range_type ? `${item.range_type} · ${item.category || ""}` : "起始装备" }),
      ]),
      node("span", { class: "equipment-price", text: `${item.price || 0} Z` }),
    ]);
    list.append(row);
  }
  if (!items.length) list.append(node("div", { class: "empty-drafts", text: "没有符合条件的装备" }));
  fragment.append(list, renderEquipmentSlots());
  return fragment;
}

function addBond() {
  const draft = activeDraft();
  if (draft.build.bonds.length >= 6) {
    showToast("一名角色最多同时建立 6 段羁绊。", "warning");
    return;
  }
  draft.build.bonds.push({ target: "", emotions: [] });
  touchDraft();
  renderCurrentStep();
}

function emotionPair(bond, first, second) {
  const container = node("div", { class: "emotion-pair" });
  for (const emotion of [first, second]) {
    const button = node("button", { type: "button", class: bond.emotions.includes(emotion) ? "selected" : "", text: emotion });
    button.addEventListener("click", () => {
      const current = bond.emotions || [];
      if (current.includes(emotion)) bond.emotions = current.filter((item) => item !== emotion);
      else bond.emotions = [...current.filter((item) => item !== first && item !== second), emotion];
      touchDraft();
      renderCurrentStep();
    });
    container.append(button);
  }
  return container;
}

function renderBondsAndAppearanceStep() {
  const draft = activeDraft();
  const fragment = document.createDocumentFragment();
  fragment.append(stepIntro(
    "让英雄与世界发生联结",
    "羁绊记录对角色、组织或信念的情感。每段羁绊可从三组对立情感中各选一种。",
    "Ⅵ",
  ));
  const title = node("div", { class: "section-title" }, [
    node("h3", { text: `羁绊 ${draft.build.bonds.length} / 6` }),
    node("button", { class: "button button-secondary button-small", type: "button", text: "＋ 添加羁绊", onclick: addBond }),
  ]);
  const list = node("div", { class: "bond-list" });
  draft.build.bonds.forEach((bond, index) => {
    const target = node("input", { class: "inline-input", type: "text", value: bond.target || "", placeholder: "对象或信念", maxLength: 200 });
    target.addEventListener("input", () => {
      bond.target = target.value;
      touchDraft({ requestPreview: false });
    });
    const remove = node("button", { class: "icon-button", type: "button", title: "移除羁绊", "aria-label": "移除羁绊", text: "×" });
    remove.addEventListener("click", () => {
      draft.build.bonds.splice(index, 1);
      touchDraft();
      renderCurrentStep();
    });
    list.append(node("div", { class: "bond-row" }, [
      target,
      node("div", { class: "emotion-pairs" }, [
        emotionPair(bond, "钦佩", "自卑"),
        emotionPair(bond, "信赖", "猜忌"),
        emotionPair(bond, "喜爱", "憎恨"),
      ]),
      remove,
    ]));
  });
  if (!draft.build.bonds.length) list.append(node("div", { class: "empty-drafts", text: "尚未记录羁绊" }));
  fragment.append(title, list);
  const appearance = node("section", { class: "appearance-section" }, [
    node("div", { class: "section-title" }, [node("h3", { text: "外貌与视觉设定" }), node("span", { text: "这些字段可用于生成立绘提示词" })]),
    node("div", { class: "form-grid" }, [
      inputField("种族或形体", "presentation.appearance.species", { placeholder: "人类、构装体、兽人……" }),
      inputField("年龄呈现", "presentation.appearance.age", { placeholder: "青年、年迈、年龄难辨……" }),
      inputField("性别呈现", "presentation.appearance.gender_presentation", { placeholder: "可留空" }),
      inputField("身形", "presentation.appearance.body", { placeholder: "高挑、结实、娇小……" }),
      inputField("发型与发色", "presentation.appearance.hair", { placeholder: "例如：银白短发，略显凌乱" }),
      inputField("眼睛", "presentation.appearance.eyes", { placeholder: "颜色、神态或特殊效果" }),
      inputField("服装", "presentation.appearance.outfit", { placeholder: "材质、剪裁、职业特征", full: true }),
      inputField("盔甲与饰品", "presentation.appearance.armor", { placeholder: "例如：不对称轻甲，旧王室徽章" }),
      inputField("武器", "presentation.appearance.weapon", { placeholder: "例如：折叠式魔导长枪" }),
      inputField("魔法表现", "presentation.appearance.magic", { placeholder: "元素、光效、奥灵或装置" }),
      inputField("标志特征", "presentation.appearance.marks", { placeholder: "伤痕、纹身、机械部件……" }),
      inputField("主色调", "presentation.appearance.palette", { placeholder: "例如：森林绿、旧金与珊瑚红" }),
      inputField("世界风格", "presentation.appearance.world_style", { placeholder: "史诗奇幻、自然奇幻、科技奇幻……" }),
      inputField("魔法与科技", "presentation.appearance.magic_tech_role", { placeholder: "角色如何使用或看待二者" }),
      inputField("表情", "presentation.appearance.expression", { placeholder: "沉静、坚毅、狡黠……" }),
      inputField("姿势", "presentation.appearance.pose", { placeholder: "自然站姿、拔剑瞬间……" }),
    ]),
  ]);
  fragment.append(appearance);
  return fragment;
}

function portraitSource(draft = activeDraft()) {
  return draft?.presentation?.portrait?.asset_url || PLACEHOLDER_PORTRAIT;
}

function portraitFeatureEnabled() {
  return state.catalog?.capabilities?.portrait_generation !== false;
}

function standaloneRosterReady() {
  return state.catalog?.storage === "standalone_roster";
}

function modelProfileTabs() {
  const draft = activeDraft();
  const current = draft.presentation.portrait.model_profile || "anima";
  const profiles = state.catalog?.portrait_profiles || [
    { id: "anima", label: "Anima", generation_ready: true },
    { id: "krea2", label: "Krea 2", generation_ready: false },
    { id: "krea_lora", label: "Krea 2 + LoRA", generation_ready: false },
  ];
  const tabs = node("div", { class: "model-tabs", role: "group", "aria-label": "生图模型" });
  for (const profile of profiles) {
    const value = profile.id;
    const button = node("button", {
      type: "button",
      class: current === value ? "active" : "",
      text: profile.label,
      title: profile.generation_ready ? `${profile.label} 工作流已就绪` : `${profile.label} 仅可整理提示词，尚未配置生图工作流`,
    });
    button.addEventListener("click", () => {
      draft.presentation.portrait.model_profile = value;
      draft.presentation.portrait.positive_prompt = "";
      draft.presentation.portrait.negative_prompt = "";
      draft.presentation.portrait.prompt_source = "";
      touchDraft({ requestPreview: false });
      renderCurrentStep();
    });
    tabs.append(button);
  }
  return tabs;
}

function activePortraitProfile() {
  const profileId = activeDraft()?.presentation?.portrait?.model_profile || "anima";
  return state.catalog?.portrait_profiles?.find((profile) => profile.id === profileId) || null;
}

function portraitPromptPayload() {
  const draft = activeDraft();
  return {
    build: deepClone(draft.build),
    presentation: deepClone(draft.presentation),
    model_profile: draft.presentation.portrait.model_profile || "anima",
  };
}

async function requestPortraitPrompt(allowCreativeFill) {
  const draft = activeDraft();
  const button = dom.stepContent.querySelector("[data-action='make-prompt']");
  if (button) {
    button.disabled = true;
    button.textContent = "整理中…";
  }
  try {
    const result = await api("/v1/portraits/prompt", {
      method: "POST",
      body: JSON.stringify({
        ...portraitPromptPayload(),
        allow_creative_fill: allowCreativeFill,
        require_llm: true,
      }),
    });
    Object.assign(draft.presentation.portrait, {
      model_profile: result.prompt.model_profile,
      positive_prompt: result.prompt.positive_prompt,
      negative_prompt: result.prompt.negative_prompt,
      style_notes: result.prompt.style_notes,
      prompt_source: result.prompt.source,
      prompt_version: result.prompt.prompt_version,
    });
    touchDraft({ requestPreview: false });
    renderCurrentStep();
    showToast(result.prompt.source === "llm" ? "提示词已由模型整理。" : "提示词已按角色资料整理。", "success");
  } catch (error) {
    showToast(error.message || "无法生成提示词。", "error");
    renderCurrentStep();
  }
}

async function startPortraitGeneration() {
  const draft = activeDraft();
  const portrait = draft.presentation.portrait;
  if (!portrait.positive_prompt || !portrait.negative_prompt) {
    showToast("请先生成或填写正向与负向提示词。", "warning");
    return;
  }
  if (portrait.prompt_source !== "llm") {
    showToast("请先让 LLM 根据当前角色资料重新整理提示词。", "warning");
    return;
  }
  portrait.job_error = "";
  try {
    const result = await api("/v1/portraits/generate", {
      method: "POST",
      body: JSON.stringify({
        card_id: draft.card_id,
        hero_name: draft.build.hero_name,
        seed: portrait.seed === "" ? null : Number(portrait.seed),
        require_llm: true,
        prompt: {
          model_profile: portrait.model_profile,
          positive_prompt: portrait.positive_prompt,
          negative_prompt: portrait.negative_prompt,
          style_notes: portrait.style_notes || "",
          source: portrait.prompt_source || "manual",
        },
      }),
    });
    const jobId = result.job.job_id || result.job.id;
    if (!jobId) throw new Error("立绘服务没有返回任务编号。");
    portrait.job_id = jobId;
    portrait.job_status = result.job.status;
    portrait.seed = result.job.seed ?? portrait.seed;
    touchDraft({ requestPreview: false });
    renderCurrentStep();
    pollPortraitJob(jobId);
  } catch (error) {
    const message = error.status === 503
      ? "ComfyUI 尚未启用，或当前模型的工作流还未配置。提示词已经保留。"
      : error.message || "立绘任务启动失败。";
    showToast(message, error.status === 503 ? "warning" : "error", 5000);
  }
}

async function pollPortraitJob(jobId) {
  window.clearTimeout(state.portraitJobTimer);
  state.portraitJobTimer = null;
  const draftId = state.activeDraftId;
  try {
    const result = await api(`/v1/portrait-jobs/${encodeURIComponent(jobId)}`);
    const draft = state.drafts[draftId];
    if (!draft) return;
    const portrait = draft.presentation.portrait;
    portrait.job_status = result.job.status;
    if (result.job.status === "completed") {
      portrait.asset_url = `${result.job.result.asset_url}&v=${Date.now()}`;
      portrait.seed = result.job.result.seed;
      portrait.job_error = "";
      touchDraft({ requestPreview: false });
      if (draftId === state.activeDraftId) renderCurrentStep();
      showToast("角色立绘已经生成。", "success");
      return;
    }
    if (result.job.status === "failed") {
      portrait.job_error = result.job.error || "生图任务失败";
      touchDraft({ requestPreview: false });
      if (draftId === state.activeDraftId) renderCurrentStep();
      showToast(portrait.job_error, "error", 5000);
      return;
    }
    if (draftId === state.activeDraftId) renderCurrentStep();
    state.portraitJobTimer = window.setTimeout(() => pollPortraitJob(jobId), 1400);
  } catch (error) {
    showToast(error.message || "无法读取立绘任务状态。", "error");
  }
}

function renderPortraitStep() {
  const draft = activeDraft();
  const portrait = draft.presentation.portrait;
  if (portrait.job_status === "completed") portrait.job_error = "";
  const fragment = document.createDocumentFragment();
  if (!portraitFeatureEnabled()) {
    fragment.append(stepIntro(
      "角色立绘暂未开放",
      "本地一键版不会连接 LLM 或 ComfyUI。外貌资料仍会正常保存在角色卡中，本步骤可以直接跳过。",
      "Ⅶ",
    ));
    const image = node("img", { src: portraitSource(), alt: `${draft.build.hero_name || "角色"}立绘占位图` });
    image.addEventListener("error", () => { image.src = PLACEHOLDER_PORTRAIT; });
    enablePortraitViewer(image, `${draft.build.hero_name || "角色"}立绘占位图`);
    fragment.append(node("div", { class: "portrait-layout" }, [
      node("div", { class: "portrait-stage" }, [node("div", { class: "portrait-frame" }, image)]),
      node("div", { class: "validation-panel valid" }, [
        node("strong", { text: "自动立绘已停用" }),
        node("p", { text: "第六步填写的外貌与视觉设定会随 JSON 角色卡一同导出。" }),
      ]),
    ]));
    return fragment;
  }
  fragment.append(stepIntro(
    "把角色的轮廓交给画面",
    "LLM 会综合角色设定、职业能力、装备与外貌，补全适合所选工作流的立绘提示词；确认后即可交给本机 ComfyUI。",
    "Ⅶ",
  ));
  const loading = ["queued", "running"].includes(portrait.job_status);
  const image = node("img", { src: portraitSource(), alt: `${draft.build.hero_name || "角色"}立绘` });
  image.addEventListener("error", () => { image.src = PLACEHOLDER_PORTRAIT; });
  enablePortraitViewer(image, `${draft.build.hero_name || "角色"}立绘`);
  const stage = node("div", { class: "portrait-stage" }, [
    node("div", { class: `portrait-frame ${loading ? "loading" : ""}` }, [
      image,
      loading ? node("div", { class: "portrait-loading" }, [node("i"), node("span", { text: portrait.job_status === "queued" ? "等待生成" : "正在绘制" })]) : null,
    ]),
    node("div", { class: "portrait-actions" }, [
      portrait.asset_url ? node("a", { class: "button button-secondary", href: portrait.asset_url, download: `${safeFilename(draft.build.hero_name)}-portrait`, text: "↓ 下载立绘" }) : null,
      portrait.asset_url ? node("button", {
        class: "button button-quiet",
        type: "button",
        text: "恢复占位图",
        onclick: () => {
          portrait.asset_url = "";
          touchDraft({ requestPreview: false });
          renderCurrentStep();
        },
      }) : null,
    ]),
  ]);
  const controls = node("div", {}, [
    node("div", { class: "group-label" }, [node("span", { text: "生图模型" }), node("small", { text: "对应不同 ComfyUI 工作流" })]),
    modelProfileTabs(),
  ]);
  if (state.settings && !state.settings.llm?.api_key_configured) {
    const setupButton = node("button", {
      class: "button button-secondary button-small",
      type: "button",
      text: "打开生成设置",
    });
    setupButton.addEventListener("click", openSettingsDialog);
    controls.append(node("div", { class: "validation-panel invalid" }, [
      node("strong", { text: "还需要填写 LLM API Key" }),
      node("p", { text: "Key 只在本次角色工房运行期间保留，不会写入角色卡或设置文件。" }),
      setupButton,
    ]));
  }
  const creative = node("input", { type: "checkbox", checked: portrait.allow_creative_fill !== false });
  creative.addEventListener("change", () => {
    portrait.allow_creative_fill = creative.checked;
    touchDraft({ requestPreview: false });
  });
  controls.append(node("label", { class: "check-choice", style: { marginTop: "14px" } }, [
    creative,
    node("span", {}, [node("strong", { text: "允许 LLM 补全美术细节" }), node("small", { text: "会参考职业、主题与世界风格，不会改写身份、剧情或规则" })]),
  ]));
  const makePrompt = node("button", {
    class: "button button-primary",
    type: "button",
    dataset: { action: "make-prompt" },
    text: portrait.positive_prompt ? "重新整理提示词" : "生成提示词",
  });
  makePrompt.addEventListener("click", () => requestPortraitPrompt(creative.checked));
  controls.append(node("div", { class: "portrait-actions" }, makePrompt));
  const promptEditor = node("div", { class: "prompt-editor" });
  const positive = inputField("正向提示词", "presentation.portrait.positive_prompt", { type: "textarea", rows: 6, full: true, maxLength: 8000 });
  const negative = inputField("负向提示词", "presentation.portrait.negative_prompt", { type: "textarea", rows: 4, full: true, maxLength: 4000 });
  promptEditor.append(positive, negative);
  const seedField = node("div", { class: "field" }, [node("label", { htmlFor: "portrait-seed", text: "随机种子" })]);
  const seedInput = node("input", { id: "portrait-seed", type: "number", min: 0, value: portrait.seed ?? "", placeholder: "留空则随机" });
  seedInput.addEventListener("input", () => {
    portrait.seed = seedInput.value;
    touchDraft({ requestPreview: false });
  });
  seedField.append(seedInput);
  const selectedProfile = activePortraitProfile();
  const workflowReady = selectedProfile?.generation_ready !== false;
  const generate = node("button", {
    class: "button button-primary",
    type: "button",
    text: loading ? "生成中…" : workflowReady ? "在 ComfyUI 生成" : "工作流未配置",
    disabled: loading || !workflowReady,
  });
  generate.addEventListener("click", startPortraitGeneration);
  promptEditor.append(node("div", { class: "seed-row" }, [seedField, generate]));
  if (!workflowReady) {
    promptEditor.append(node("div", {
      class: "validation-panel invalid",
      text: `${selectedProfile?.label || "当前模型"} 尚未配置 API-format 工作流，但仍可整理和导出提示词。`,
    }));
  }
  if (portrait.job_error) promptEditor.append(node("div", { class: "validation-panel invalid", text: portrait.job_error }));
  controls.append(promptEditor);
  fragment.append(node("div", { class: "portrait-layout" }, [stage, controls]));
  return fragment;
}

function stepValidation(index, draft = activeDraft()) {
  if (!draft) return { complete: false, message: "没有活动草稿" };
  const build = draft.build;
  if (index === 0) {
    const missing = [
      ["角色名", build.hero_name],
      ["身份", build.identity],
      ["主题", build.theme],
      ["故乡", build.origin],
    ].filter(([, value]) => !String(value || "").trim()).map(([label]) => label);
    return missing.length
      ? { complete: false, message: `还需填写：${missing.join("、")}` }
      : { complete: true, message: "角色概念已完成" };
  }
  if (index === 1) {
    const count = Object.keys(build.classes || {}).filter((name) => build.classes[name] > 0).length;
    const total = rankTotal(build.classes);
    const complete = count >= 2 && count <= 3 && total === 5;
    return { complete, message: complete ? "5 个职业等级已分配" : `当前 ${count} 个职业，共 ${total} / 5 级` };
  }
  if (index === 2) {
    const errors = [];
    for (const className of selectedClassNames(draft)) {
      const classEntry = state.catalog?.classes?.find((item) => item.name === className);
      const selected = (classEntry?.skills || []).reduce((sum, skill) => sum + Number(build.skills[skill.name] || 0), 0);
      if (selected !== Number(build.classes[className])) errors.push(`${className}技能 ${selected}/${build.classes[className]}`);
    }
    for (const [skillName, school] of Object.entries(state.catalog?.spell_granting_skills || {})) {
      const required = Number(build.skills[skillName] || 0);
      if (!required) continue;
      const selected = build.spells.filter((name) => state.catalog.spells.find((spell) => spell.name === name)?.school === school).length;
      if (selected !== required) errors.push(`${school} ${selected}/${required}`);
    }
    const portableRank = Number(build.skills["便携装置"] || 0);
    if (portableRank && (build.skill_options["便携装置"] || []).length !== portableRank) errors.push(`便携装置选择 ${(build.skill_options["便携装置"] || []).length}/${portableRank}`);
    for (const skillName of ["拟兽系仪式", "形意咒法"]) {
      if (build.skills[skillName] > 0 && (build.skill_options[skillName] || []).length !== 1) errors.push(`${skillName}尚未选择属性`);
    }
    if (build.skills["契约与召唤"] > 0 && !build.bound_arcana.length) errors.push("尚未绑定奥灵");
    return { complete: errors.length === 0 && stepValidation(1, draft).complete, message: errors.length ? errors.join("；") : "技能与附带选择已完成" };
  }
  if (index === 3) {
    const pattern = currentAttributePatternFor(draft);
    return { complete: Boolean(pattern), message: pattern ? `采用【${pattern.name}】属性组合` : "属性骰组合不符合起始规则" };
  }
  if (index === 4) {
    const spent = equipmentCost(draft);
    const missingAbilities = (build.equipment || []).filter((name) => {
      const item = equipmentByName(name);
      return item?.required_ability && !classAbilities(draft).has(item.required_ability);
    });
    const complete = spent <= Number(state.catalog?.equipment_budget || 500) && !missingAbilities.length;
    return { complete, message: complete ? `已使用 ${spent} Z，余下 ${500 - spent} Z` : missingAbilities.length ? "有装备不符合职业能力" : `装备超出预算 ${spent - 500} Z` };
  }
  if (index === 5) {
    const invalid = (build.bonds || []).filter((bond) => !String(bond.target || "").trim() || !bond.emotions?.length);
    return { complete: !invalid.length, message: invalid.length ? `${invalid.length} 段羁绊尚未完整` : "羁绊与外貌已记录" };
  }
  if (index === 6) {
    if (!portraitFeatureEnabled()) {
      return { complete: true, message: "本地版已跳过自动立绘" };
    }
    const portrait = draft.presentation.portrait;
    const appearance = draft.presentation.appearance;
    const hasVisual = Object.values(appearance || {}).some((value) => String(value || "").trim());
    return { complete: hasVisual, message: portrait.asset_url ? "立绘已生成" : hasVisual ? "视觉设定已记录" : "外貌设定可以稍后补充" };
  }
  const prerequisites = [0, 1, 2, 3, 4, 5].every((step) => stepValidation(step, draft).complete);
  return {
    complete: prerequisites && Boolean(state.preview?.valid) && build.fate_roll?.length === 2,
    message: prerequisites ? (state.preview?.valid ? "角色卡通过规则校验" : "等待规则校验") : "前面的必填步骤尚未完成",
  };
}

function currentAttributePatternFor(draft) {
  const values = Object.values(draft?.build.attributes || {}).map(Number).sort((a, b) => a - b);
  return state.catalog?.attribute_patterns?.find((pattern) => [...pattern.dice].map(Number).sort((a, b) => a - b).join(",") === values.join(",")) || null;
}

function localDerived(draft = activeDraft()) {
  const attributes = draft?.build.attributes || { DEX: 8, INS: 8, MIG: 8, WLP: 8 };
  let hpBonus = 0;
  let mpBonus = 0;
  let ipBonus = 0;
  for (const className of selectedClassNames(draft)) {
    const benefit = state.catalog?.classes?.find((item) => item.name === className)?.benefit || {};
    hpBonus += Number(benefit.hp || 0);
    mpBonus += Number(benefit.mp || 0);
    ipBonus += Number(benefit.ip || 0);
  }
  const maxHp = 5 + Number(attributes.MIG || 8) * 5 + hpBonus + Number(draft?.build.skills?.["铁壁"] || 0) * 3;
  const maxMp = 5 + Number(attributes.WLP || 8) * 5 + mpBonus + Number(draft?.build.skills?.["集中心智"] || 0) * 3;
  const spent = equipmentCost(draft);
  return {
    max_hp: maxHp,
    crisis_threshold: Math.floor(maxHp / 2),
    max_mp: maxMp,
    max_inventory_points: 6 + ipBonus,
    physical_defense: Number(attributes.DEX || 8),
    magic_defense: Number(attributes.INS || 8),
    initiative: 0,
    starting_zenit: draft?.build.fate_roll?.length === 2 ? 500 - spent + rankTotal(draft.build.fate_roll) * 10 : null,
  };
}

function draftPayload(draft = activeDraft()) {
  const build = deepClone(draft.build);
  delete build.notes_text;
  build.notes = (build.notes || []).map((item) => String(item).trim()).filter(Boolean);
  build.bonds = (build.bonds || []).filter((bond) => String(bond.target || "").trim()).map((bond) => ({
    target: String(bond.target).trim(),
    emotions: [...new Set(bond.emotions || [])],
  }));
  return {
    card_id: draft.card_id || "",
    revision: draft.revision || 1,
    build,
    presentation: deepClone(draft.presentation),
    extensions: deepClone(draft.extensions || {}),
  };
}

function schedulePreview() {
  window.clearTimeout(state.previewTimer);
  state.previewTimer = window.setTimeout(refreshPreview, 420);
}

async function refreshPreview() {
  const draft = activeDraft();
  if (!draft) return;
  const ready = [0, 1, 2, 3, 4, 5].every((step) => stepValidation(step, draft).complete);
  if (!ready) {
    state.preview = null;
    state.previewError = "";
    renderCharacterSheet();
    updateFooter();
    if (state.currentStep === 7) renderCurrentStep();
    return;
  }
  try {
    state.preview = await api("/v1/character-builder/preview", {
      method: "POST",
      body: JSON.stringify(draftPayload(draft)),
    });
    state.previewError = "";
  } catch (error) {
    state.preview = error.data || null;
    state.previewError = error.message || "角色卡校验失败";
  }
  renderCharacterSheet();
  updateFooter();
  if (state.currentStep === 7) renderCurrentStep();
}

function reviewLine(label, value) {
  return node("div", { class: "review-line" }, [node("span", { text: label }), node("strong", { text: value || "未填写" })]);
}

function rollFate() {
  const draft = activeDraft();
  draft.build.fate_roll = [randomD6(), randomD6()];
  touchDraft();
  renderCurrentStep();
  showToast(`命运骰：${draft.build.fate_roll.join(" + ")}`, "success");
}

async function buildPortableCard({ ensureFate = true } = {}) {
  const draft = activeDraft();
  if (ensureFate && draft.build.fate_roll?.length !== 2) rollFate();
  const result = await api("/v1/character-cards/build", {
    method: "POST",
    body: JSON.stringify(draftPayload(draft)),
  });
  draft.card_id = result.card.card.id;
  draft.revision = result.card.card.revision;
  touchDraft({ requestPreview: false });
  return result.card;
}

async function exportActiveDraft() {
  try {
    const card = await buildPortableCard();
    downloadJSON(card, `${safeFilename(card.build.hero_name)}.fu-character.json`);
    showToast("角色卡已导出。", "success");
  } catch (error) {
    showToast(error.message || "角色卡尚未通过校验。", "error", 5000);
    state.previewError = error.message || "角色卡校验失败";
    state.currentStep = 7;
    renderEditor();
  }
}

async function exportCardText(card, heroName) {
  const result = await api("/v1/character-cards/text", {
    method: "POST",
    body: JSON.stringify({ card }),
  });
  downloadText(result.text, `${safeFilename(heroName || result.hero_name)}.fu-character.txt`);
}

async function exportActiveDraftText() {
  try {
    const card = await buildPortableCard();
    await exportCardText(card, card.build.hero_name);
    showToast("纯文本角色卡已导出。", "success");
  } catch (error) {
    showToast(error.message || "角色卡尚未通过校验。", "error", 5000);
    state.previewError = error.message || "角色卡校验失败";
    state.currentStep = 7;
    renderEditor();
  }
}

async function saveActiveToRoster(conflict = "reject") {
  if (!standaloneRosterReady()) {
    showToast("当前连接的不是独立角色工房，请用“启动角色工房.cmd”重新打开。", "error", 6000);
    return;
  }
  try {
    const card = await buildPortableCard();
    const result = await api("/v1/character-cards/import", {
      method: "POST",
      body: JSON.stringify({ card, conflict }),
    });
    activeDraft().card_id = result.card.card.id;
    activeDraft().revision = result.card.card.revision;
    persistLocalState();
    await loadCharacters();
    showToast(`【${result.character.name}】已保存到本地名册。`, "success");
    renderCurrentStep();
  } catch (error) {
    if (error.status === 409 && conflict === "reject") {
      openSaveConflictDialog(error.message);
      return;
    }
    showToast(error.message || "无法保存到本地名册。", "error", 5000);
  }
}

function openSaveConflictDialog(message) {
  clear(dom.importDialogContent).append(node("div", { class: "conflict-box" }, [
    node("p", { text: message }),
    node("p", { text: "覆盖会用当前角色卡替换名册中的同名角色；副本会自动生成新名字。" }),
  ]));
  dom.importDialogTitle.textContent = "角色已经存在";
  clear(dom.importDialogActions);
  const cancel = node("button", { class: "button button-quiet", type: "button", text: "取消" });
  cancel.addEventListener("click", () => dom.importDialog.close());
  const copy = node("button", { class: "button button-secondary", type: "button", text: "导入副本" });
  copy.addEventListener("click", () => { dom.importDialog.close(); saveActiveToRoster("copy"); });
  const replace = node("button", { class: "button button-primary", type: "button", text: "覆盖现有角色" });
  replace.addEventListener("click", () => { dom.importDialog.close(); saveActiveToRoster("replace"); });
  dom.importDialogActions.append(cancel, copy, replace);
  dom.importDialog.showModal();
}

function renderReviewStep() {
  const draft = activeDraft();
  const derived = state.preview?.derived || localDerived(draft);
  const fragment = document.createDocumentFragment();
  fragment.append(stepIntro(
    "英雄即将踏上旅途",
    "确认角色资料，掷出两枚起始命运骰。通过规则校验后即可导出角色卡，或保存到这台设备的独立名册。",
    "Ⅷ",
  ));
  const classText = Object.entries(draft.build.classes).map(([name, level]) => `${name} ${level}`).join(" · ");
  const attributes = Object.entries(draft.build.attributes).map(([name, die]) => `${name} d${die}`).join(" · ");
  const buildSummary = [
    node("h3", { text: "职业与属性" }),
    reviewLine("职业", classText),
    reviewLine("属性", attributes),
    reviewLine("技能等级", String(rankTotal(draft.build.skills))),
  ];
  if (draft.build.spells.length > 0) {
    buildSummary.push(reviewLine("法术", draft.build.spells.join("、")));
  }
  const grid = node("div", { class: "review-grid" }, [
    node("section", { class: "review-section accent" }, [
      node("h3", { text: "角色概念" }),
      reviewLine("角色名", draft.build.hero_name),
      reviewLine("身份", draft.build.identity),
      reviewLine("主题", draft.build.theme),
      reviewLine("故乡", draft.build.origin),
    ]),
    node("section", { class: "review-section" }, buildSummary),
    node("section", { class: "review-section" }, [
      node("h3", { text: "资源" }),
      reviewLine("最大 HP", String(derived.max_hp)),
      reviewLine("最大 MP", String(derived.max_mp)),
      reviewLine("最大 IP", String(derived.max_inventory_points)),
      reviewLine("危机值", String(derived.crisis_threshold)),
    ]),
    node("section", { class: "review-section" }, [
      node("h3", { text: "装备与联结" }),
      reviewLine("装备", draft.build.equipment.join("、") || "无购买"),
      reviewLine("装备花费", `${equipmentCost(draft)} Z`),
      reviewLine("羁绊", `${draft.build.bonds.length} 段`),
      reviewLine("立绘", draft.presentation.portrait.asset_url ? "已生成" : portraitFeatureEnabled() ? "使用占位图" : "本地版未启用"),
    ]),
  ]);
  fragment.append(grid);
  const dice = draft.build.fate_roll || [];
  const fatePanel = node("section", { class: "fate-panel" }, [
    node("div", { class: "fate-copy" }, [
      node("h3", { text: "起始命运骰" }),
      node("p", { text: dice.length === 2 ? `起始金币：${500 - equipmentCost(draft) + rankTotal(dice) * 10} Z` : "两枚 d6 的总点数 × 10 Z 会加入剩余起始金币。" }),
    ]),
    dice.length === 2
      ? node("div", { class: "fate-dice" }, [node("span", { class: "die-face", text: dice[0] }), node("span", { text: "+" }), node("span", { class: "die-face", text: dice[1] }), node("button", { class: "icon-button", type: "button", text: "↻", title: "重新掷骰", "aria-label": "重新掷骰", onclick: rollFate })])
      : node("button", { class: "button button-primary", type: "button", text: "掷 2d6", onclick: rollFate }),
  ]);
  fragment.append(fatePanel);
  const validation = state.preview?.valid
    ? node("div", { class: "validation-panel valid" }, [node("strong", { text: "规则校验通过" }), ...(state.preview.warnings || []).map((warning) => node("p", { text: warning }))])
    : node("div", { class: `validation-panel ${state.previewError ? "invalid" : ""}` }, [
      node("strong", { text: state.previewError ? "还不能完成角色卡" : "等待完整资料" }),
      ...(state.preview?.errors || (state.previewError ? [state.previewError] : [stepValidation(7, draft).message])).map((error) => node("p", { text: error })),
    ]);
  fragment.append(validation);
  const actions = node("div", { class: "completion-actions" }, [
    node("button", { class: "button button-secondary", type: "button", text: "↓ 导出角色卡 JSON", onclick: exportActiveDraft }),
    node("button", { class: "button button-secondary", type: "button", text: "↓ 导出纯文本 TXT", onclick: exportActiveDraftText }),
    node("button", { class: "button button-primary", type: "button", text: "保存到本地名册", onclick: () => saveActiveToRoster("reject") }),
    node("button", { class: "button button-quiet", type: "button", text: "⎙ 打印角色卡", onclick: () => window.print() }),
    node("button", { class: "button button-quiet", type: "button", text: "返回角色库", onclick: showLibrary }),
  ]);
  fragment.append(actions);
  return fragment;
}

function sheetSection(title, rows, emptyText) {
  const section = node("section", { class: "sheet-section" }, [node("div", { class: "sheet-section-title", text: title })]);
  if (!rows.length) section.append(node("div", { class: "sheet-empty", text: emptyText }));
  else section.append(...rows);
  return section;
}

function renderCharacterSheet() {
  const draft = activeDraft();
  clear(dom.characterSheet);
  if (!draft) return;
  const derived = state.preview?.derived || localDerived(draft);
  const portrait = node("img", { src: portraitSource(draft), alt: "角色立绘" });
  portrait.addEventListener("error", () => { portrait.src = PLACEHOLDER_PORTRAIT; });
  enablePortraitViewer(portrait, `${draft.build.hero_name || "角色"}立绘`);
  const classRows = Object.entries(draft.build.classes || {}).filter(([, level]) => level > 0).map(([name, level]) => node("div", { class: "sheet-class-row" }, [node("span", { text: name }), node("strong", { text: `Lv. ${level}` })]));
  const skillRows = Object.entries(draft.build.skills || {}).filter(([, rank]) => rank > 0).map(([name, rank]) => node("div", { class: "sheet-skill-row" }, [node("span", { text: name }), node("strong", { text: `Lv${rank}` })]));
  const spellRows = (draft.build.spells || []).map((name) => node("div", { class: "sheet-skill-row" }, [node("span", { text: name }), node("strong", { text: "Lv1" })]));
  const equipmentRows = (draft.build.equipment || []).map((name) => node("div", { class: "sheet-equipment-row" }, [node("span", { text: name }), node("strong", { text: `${equipmentByName(name)?.price || 0} Z` })]));
  const bondRows = (draft.build.bonds || []).filter((bond) => bond.target).map((bond) => node("div", { class: "sheet-bond-row" }, [node("span", { text: bond.target }), node("strong", { text: (bond.emotions || []).join(" · ") || "未定" })]));
  const sheet = node("article", { class: "character-sheet" }, [
    node("div", { class: "sheet-band" }),
    node("header", { class: "sheet-heading" }, [
      node("span", { class: "sheet-label", text: `LEVEL 5 · ${draft.build.player_name || "PLAYER"}` }),
      node("h2", { class: "sheet-name", text: draft.build.hero_name || "未命名的英雄" }),
      node("p", { class: "sheet-identity", text: draft.build.identity || "身份尚未写下" }),
      node("div", { class: "sheet-origin", text: `${draft.build.theme || "未定主题"} · ${draft.build.origin || "未知故乡"}` }),
      node("div", { class: "sheet-portrait" }, portrait),
    ]),
    node("div", { class: "sheet-body" }, [
      node("div", { class: "sheet-resource-grid" }, [
        node("div", { class: "sheet-resource" }, [node("span", { text: "HP / 危机" }), node("strong", { text: `${derived.max_hp} / ${derived.crisis_threshold}` })]),
        node("div", { class: "sheet-resource" }, [node("span", { text: "MP" }), node("strong", { text: derived.max_mp })]),
        node("div", { class: "sheet-resource" }, [node("span", { text: "IP" }), node("strong", { text: derived.max_inventory_points })]),
      ]),
      node("div", { class: "sheet-attributes" }, Object.entries(ATTRIBUTE_LABELS).map(([key, [label]]) => node("div", { class: "sheet-attribute" }, [node("span", { text: label }), node("strong", { text: `d${draft.build.attributes[key] || 6}` })]))),
      node("div", { class: "sheet-defenses" }, [
        node("div", {}, [node("span", { text: "物防" }), node("strong", { text: derived.physical_defense })]),
        node("div", {}, [node("span", { text: "魔防" }), node("strong", { text: derived.magic_defense })]),
        node("div", {}, [node("span", { text: "先攻" }), node("strong", { text: Number(derived.initiative || 0) >= 0 ? `+${derived.initiative || 0}` : derived.initiative })]),
      ]),
      sheetSection("职业", classRows.length ? [node("div", { class: "sheet-class-list" }, classRows)] : [], "尚未选择职业"),
      sheetSection("技能与法术", skillRows.length || spellRows.length ? [node("div", { class: "sheet-skill-list" }, [...skillRows, ...spellRows])] : [], "尚未选择技能"),
      sheetSection("装备", equipmentRows.length ? [node("div", { class: "sheet-equipment-list" }, equipmentRows)] : [], "徒手与旅行衣装"),
      sheetSection("羁绊", bondRows.length ? [node("div", { class: "sheet-bond-list" }, bondRows)] : [], "尚未建立羁绊"),
      node("div", { class: "sheet-footer-mark", text: `FABULA POINTS 3 · ${derived.starting_zenit ?? "—"} Z · FABULA ULTIMA` }),
    ]),
  ]);
  dom.characterSheet.append(sheet);
}

function renderStepNavigation() {
  const draft = activeDraft();
  clear(dom.stepNavigation);
  STEPS.forEach((step, index) => {
    const validation = stepValidation(index, draft);
    const button = node("button", { type: "button", class: `step-button ${index === state.currentStep ? "active" : ""} ${validation.complete ? "complete" : ""}` }, [
      node("span", { class: "step-index", text: validation.complete && index !== state.currentStep ? "✓" : index + 1 }),
      node("span", { class: "step-label" }, [node("strong", { text: step.label }), node("small", { text: step.short })]),
    ]);
    button.addEventListener("click", () => goToStep(index));
    dom.stepNavigation.append(button);
  });
  dom.mobileStepNumber.textContent = String(state.currentStep + 1);
  dom.mobileStepLabel.textContent = STEPS[state.currentStep].label;
  dom.mobileProgressBar.style.width = `${((state.currentStep + 1) / STEPS.length) * 100}%`;
}

function updateEditorHeading() {
  const draft = activeDraft();
  if (!draft) return;
  dom.editorTitle.textContent = draft.build.hero_name || "未命名的英雄";
  dom.editorEyebrow.textContent = draft.card_id ? "角色卡草稿" : "新角色";
}

function updateFooter() {
  const validation = stepValidation(state.currentStep);
  dom.footerStatus.textContent = validation.message;
  dom.previousStepButton.disabled = state.currentStep === 0;
  dom.nextStepButton.disabled = state.currentStep === STEPS.length - 1;
  dom.nextStepButton.replaceChildren(document.createTextNode(state.currentStep === STEPS.length - 2 ? "检查角色卡 " : "下一步 "), node("span", { text: "→", "aria-hidden": "true" }));
}

function renderCurrentStep() {
  if (!activeDraft() || !state.catalog) return;
  clear(dom.stepContent);
  const renderers = [
    renderConceptStep,
    renderClassesStep,
    renderSkillsStep,
    renderAttributesStep,
    renderEquipmentStep,
    renderBondsAndAppearanceStep,
    renderPortraitStep,
    renderReviewStep,
  ];
  dom.stepContent.append(renderers[state.currentStep]());
  renderStepNavigation();
  updateEditorHeading();
  updateFooter();
  renderCharacterSheet();
}

function renderEditor() {
  dom.libraryView.hidden = true;
  dom.editorView.hidden = false;
  renderCurrentStep();
  schedulePreview();
}

function goToStep(index) {
  state.currentStep = Math.max(0, Math.min(STEPS.length - 1, Number(index)));
  renderCurrentStep();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function openEditor(draftId, step = 0) {
  if (!state.drafts[draftId]) return;
  state.activeDraftId = draftId;
  state.currentStep = step;
  state.preview = null;
  state.previewError = "";
  persistLocalState();
  renderEditor();
  const portrait = state.drafts[draftId].presentation?.portrait;
  if (portraitFeatureEnabled() && ["queued", "running"].includes(portrait?.job_status)) {
    if (portrait.job_id) {
      pollPortraitJob(portrait.job_id);
    } else {
      portrait.job_status = "";
      portrait.job_error = "上一次立绘任务缺少任务编号，请重新生成。";
      touchDraft({ requestPreview: false });
      renderCurrentStep();
    }
  }
}

function createDraftAndOpen() {
  if (!state.catalog) {
    showToast("规则目录仍在载入，请稍候。", "warning");
    return;
  }
  const draft = defaultDraft();
  state.drafts[draft.id] = draft;
  openEditor(draft.id, 0);
}

function showLibrary() {
  window.clearTimeout(state.portraitJobTimer);
  state.portraitJobTimer = null;
  dom.editorView.hidden = true;
  dom.libraryView.hidden = false;
  persistLocalState();
  renderLibrary();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function portraitFromCardData(portrait) {
  return portrait?.asset_url || portrait?.url || PLACEHOLDER_PORTRAIT;
}

function characterTile(character) {
  const image = node("img", { src: portraitFromCardData(character.portrait), alt: `${character.name}立绘` });
  image.addEventListener("error", () => { image.src = PLACEHOLDER_PORTRAIT; });
  const visual = node("div", { class: "character-tile-visual" }, [image, node("span", { class: "character-level", text: `Lv. ${character.level}` })]);
  const classTags = Object.entries(character.classes || {}).map(([name, level]) => node("span", { class: "class-tag", text: `${name} ${level}` }));
  const exportButton = node("button", { class: "button button-secondary button-small", type: "button", text: "↓ JSON" });
  exportButton.addEventListener("click", () => exportExistingCharacter(character.name));
  const textButton = node("button", { class: "button button-secondary button-small", type: "button", text: "↓ TXT" });
  textButton.addEventListener("click", () => exportExistingCharacterText(character.name));
  const copyButton = node("button", { class: "button button-quiet button-small", type: "button", text: "制作副本" });
  copyButton.addEventListener("click", () => copyExistingCharacter(character.name));
  return node("article", { class: "character-tile" }, [
    visual,
    node("div", { class: "character-tile-body" }, [
      node("h3", { text: character.name }),
      node("p", { text: `${character.identity || "未记录身份"} · ${character.theme || "未记录主题"}` }),
      node("div", { class: "class-line" }, classTags),
      node("div", { class: "tile-actions" }, [copyButton, exportButton, textButton]),
    ]),
  ]);
}

function draftRow(draft) {
  const classText = Object.entries(draft.build.classes || {}).map(([name, level]) => `${name} ${level}`).join(" · ") || "尚未选择职业";
  const continueButton = node("button", { class: "button button-secondary button-small", type: "button", text: "继续" });
  continueButton.addEventListener("click", () => openEditor(draft.id, firstIncompleteStep(draft)));
  const removeButton = node("button", { class: "icon-button", type: "button", title: "删除草稿", "aria-label": "删除草稿", text: "×" });
  removeButton.addEventListener("click", () => confirmAction("删除草稿", `删除【${draft.build.hero_name || "未命名的英雄"}】的本地草稿？`, () => {
    delete state.drafts[draft.id];
    if (state.activeDraftId === draft.id) state.activeDraftId = "";
    persistLocalState();
    renderLibrary();
  }));
  return node("div", { class: "draft-row" }, [
    node("div", { class: "draft-name" }, [node("strong", { text: draft.build.hero_name || "未命名的英雄" }), node("span", { text: draft.build.identity || "身份尚未填写" })]),
    node("div", { class: "draft-meta", text: classText }),
    node("div", { class: "draft-updated", text: formatDate(draft.updated_at) }),
    node("div", { class: "draft-actions" }, [continueButton, removeButton]),
  ]);
}

function firstIncompleteStep(draft) {
  for (let index = 0; index < 7; index += 1) {
    if (!stepValidation(index, draft).complete) return index;
  }
  return 7;
}

function renderLibrary() {
  dom.characterCountLabel.textContent = "名册角色";
  dom.librarySectionTitle.textContent = "最终物语中的英雄";
  dom.rosterCharacterCount.textContent = String(state.characters.length);
  dom.draftCount.textContent = String(Object.keys(state.drafts).length);
  clear(dom.characterGrid);
  state.characters.forEach((character) => dom.characterGrid.append(characterTile(character)));
  dom.characterEmpty.hidden = state.characters.length > 0;
  clear(dom.draftList);
  const drafts = Object.values(state.drafts).sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  drafts.forEach((draft) => dom.draftList.append(draftRow(draft)));
  dom.draftEmpty.hidden = drafts.length > 0;
}

function renderLibraryLoading() {
  clear(dom.characterGrid);
  for (let index = 0; index < 4; index += 1) dom.characterGrid.append(node("div", { class: "skeleton" }));
  dom.characterEmpty.hidden = true;
}

async function loadCatalog() {
  state.catalog = await api("/v1/character-builder/catalog");
  if (!standaloneRosterReady()) {
    throw new Error("当前端口仍是旧版 FU-GM 服务，请通过“启动角色工房.cmd”打开独立角色工房。");
  }
  if (!portraitFeatureEnabled()) STEPS[6].short = "本地版暂未开放";
  for (const draft of Object.values(state.drafts)) normalizeDraftSpells(draft);
  persistLocalState();
}

async function loadCharacters() {
  const result = await api("/v1/character-cards");
  state.characters = result.characters || [];
  renderLibrary();
}

async function exportExistingCharacter(name) {
  try {
    const result = await api(`/v1/character-cards/export?hero_name=${encodeURIComponent(name)}`);
    downloadJSON(result.card, `${safeFilename(name)}.fu-character.json`);
    showToast(`【${name}】的角色卡已导出。`, "success");
  } catch (error) {
    showToast(error.message || "角色卡导出失败。", "error");
  }
}

async function exportExistingCharacterText(name) {
  try {
    const result = await api(`/v1/character-cards/export?hero_name=${encodeURIComponent(name)}`);
    await exportCardText(result.card, name);
    showToast(`【${name}】的纯文本角色卡已导出。`, "success");
  } catch (error) {
    showToast(error.message || "纯文本角色卡导出失败。", "error");
  }
}

async function copyExistingCharacter(name) {
  try {
    const result = await api(`/v1/character-cards/export?hero_name=${encodeURIComponent(name)}`);
    const draft = draftFromCard(result.card, { asCopy: true });
    state.drafts[draft.id] = draft;
    openEditor(draft.id, 0);
    showToast("已创建独立的 5 级角色草稿副本。", "success");
  } catch (error) {
    showToast(error.message || "无法读取角色卡。", "error");
  }
}

function draftFromCard(card, { asCopy = false } = {}) {
  const base = defaultDraft();
  const build = card?.build && typeof card.build === "object" ? deepClone(card.build) : {};
  const presentation = card?.presentation && typeof card.presentation === "object" ? deepClone(card.presentation) : {};
  base.card_id = asCopy ? "" : String(card?.card?.id || "");
  base.revision = asCopy ? 1 : Number(card?.card?.revision || 1);
  base.source_level = Number(card?.state?.level || 5);
  base.build = {
    ...base.build,
    ...build,
    classes: { ...(build.classes || {}) },
    attributes: { ...base.build.attributes, ...(build.attributes || {}) },
    bonds: deepClone(build.bonds || []),
    skills: { ...(build.skills || {}) },
    skill_options: deepClone(build.skill_options || {}),
    spells: [...(build.spells || [])],
    bound_arcana: [...(build.bound_arcana || [])],
    abilities: [...(build.abilities || [])],
    equipment: [...(build.equipment || [])],
    equipment_slots: { ...base.build.equipment_slots, ...(build.equipment_slots || {}) },
    notes: [...(build.notes || [])],
    fate_roll: asCopy ? [] : [...(build.fate_roll || [])],
  };
  if (asCopy) {
    base.build.hero_name = build.hero_name ? `${build.hero_name}（副本）` : "";
  }
  base.presentation = {
    appearance: { ...base.presentation.appearance, ...(presentation.appearance || {}) },
    portrait: { ...base.presentation.portrait, ...(presentation.portrait || {}) },
  };
  base.extensions = deepClone(card?.extensions || {});
  if (asCopy && base.source_level > 5) resetAdvancedDraftToStartingLevel(base);
  return base;
}

function resetAdvancedDraftToStartingLevel(draft) {
  const ranked = Object.entries(draft.build.classes || {})
    .filter(([, level]) => Number(level) > 0)
    .sort((left, right) => Number(right[1]) - Number(left[1]))
    .slice(0, 3);
  const chosen = ranked.length >= 2 ? ranked : (state.catalog?.classes || []).slice(0, 2).map((item) => [item.name, 1]);
  const classes = Object.fromEntries(chosen.map(([name]) => [name, 1]));
  let remaining = 5 - chosen.length;
  while (remaining > 0) {
    const target = chosen
      .map(([name, original]) => ({ name, gap: Number(original) - Number(classes[name] || 0) }))
      .sort((left, right) => right.gap - left.gap)[0];
    classes[target.name] += 1;
    remaining -= 1;
  }
  draft.build.classes = classes;
  draft.build.skills = {};
  draft.build.skill_options = {};
  draft.build.spells = [];
  draft.build.bound_arcana = [];
  draft.build.abilities = [];
  draft.build.equipment = [];
  draft.build.equipment_slots = { main_hand: "", off_hand: "", armor: "", shield: "" };
  draft.build.fate_roll = [];
  draft.build.notes = [...(draft.build.notes || []), "由进阶角色卡建立 5 级重构草稿；技能与起始装备需要重新选择。"];
}

function importSummaryNode(card, preview) {
  const character = preview?.character || {};
  const build = card?.build || {};
  const image = node("img", { src: portraitFromCardData(card?.presentation?.portrait), alt: "导入角色立绘" });
  image.addEventListener("error", () => { image.src = PLACEHOLDER_PORTRAIT; });
  const classes = Object.entries(character.classes || build.classes || {}).map(([name, level]) => `${name} ${level}`).join(" · ");
  return node("div", { class: "import-summary" }, [
    image,
    node("div", {}, [
      node("h3", { text: character.name || build.hero_name || "未命名角色" }),
      node("p", { text: `${character.identity || build.identity || "未记录身份"} · ${character.theme || build.theme || "未记录主题"}` }),
      node("div", { class: "tag-line" }, [
        node("span", { class: "class-tag", text: `Lv. ${character.level || card?.state?.level || 5}` }),
        ...(classes ? classes.split(" · ").map((item) => node("span", { class: "soft-tag", text: item })) : []),
      ]),
      node("p", { text: `规则：${card?.ruleset?.id || "未知"} · 卡片版本 ${card?.schema_version ?? "未知"}` }),
    ]),
  ]);
}

function showInvalidImport(error, card) {
  dom.importDialogTitle.textContent = "角色卡无法导入";
  clear(dom.importDialogContent).append(node("div", { class: "validation-panel invalid" }, [
    node("strong", { text: error.message || "格式不正确" }),
    ...((error.data?.errors || []).map((message) => node("p", { text: message }))),
  ]));
  if (card && typeof card === "object") {
    dom.importDialogContent.prepend(node("p", { text: "文件已经读到，但没有通过 Fabula Ultima 角色卡校验。原文件没有被修改。" }));
  }
  clear(dom.importDialogActions).append(node("button", { class: "button button-primary", type: "button", text: "关闭", onclick: () => dom.importDialog.close() }));
  dom.importDialog.showModal();
}

async function previewImportedCard(card) {
  if (!standaloneRosterReady()) {
    showInvalidImport(new Error("当前连接的不是独立角色工房，请用“启动角色工房.cmd”重新打开。"), null);
    return;
  }
  try {
    const preview = await api("/v1/character-cards/import/preview", {
      method: "POST",
      body: JSON.stringify({ card }),
    });
    openImportPreviewDialog(card, preview);
  } catch (error) {
    showInvalidImport(error, card);
  }
}

function openImportPreviewDialog(card, preview) {
  const level = Number(card?.state?.level || 5);
  dom.importDialogTitle.textContent = "检查角色卡";
  clear(dom.importDialogContent).append(importSummaryNode(card, preview));
  if (level > 5) {
    dom.importDialogContent.append(node("div", { class: "warning-box" }, [
      node("p", { text: `这是 ${level} 级进阶角色卡。加入本地名册会保留完整进度；载入网页编辑时会建立独立的 5 级副本。` }),
    ]));
  }
  if (preview.warnings?.length) {
    dom.importDialogContent.append(node("div", { class: "warning-box" }, preview.warnings.map((message) => node("p", { text: message }))));
  }
  if (preview.conflicts?.length) {
    dom.importDialogContent.append(node("div", { class: "conflict-box" }, preview.conflicts.map((conflict) => node("p", { text: conflict.message }))));
  }
  clear(dom.importDialogActions);
  const cancel = node("button", { class: "button button-quiet", type: "button", text: "取消", onclick: () => dom.importDialog.close() });
  const edit = node("button", { class: "button button-secondary", type: "button", text: level > 5 ? "创建 5 级副本" : "载入编辑" });
  edit.addEventListener("click", () => {
    const draft = draftFromCard(card, { asCopy: level > 5 });
    state.drafts[draft.id] = draft;
    dom.importDialog.close();
    openEditor(draft.id, 0);
  });
  const mode = node("select", { class: "inline-input", style: { width: "auto", minWidth: "118px" }, "aria-label": "冲突处理方式" }, [
    node("option", { value: "reject", text: "遇冲突则停止", selected: !preview.conflicts?.length }),
    node("option", { value: "replace", text: "覆盖现有角色", selected: Boolean(preview.conflicts?.length) }),
    node("option", { value: "copy", text: "导入为副本" }),
  ]);
  const importButton = node("button", { class: "button button-primary", type: "button", text: "加入本地名册" });
  importButton.addEventListener("click", async () => {
    importButton.disabled = true;
    importButton.textContent = "正在导入…";
    try {
      const result = await api("/v1/character-cards/import", {
        method: "POST",
        body: JSON.stringify({ card, conflict: mode.value }),
      });
      dom.importDialog.close();
      await loadCharacters();
      showToast(`【${result.character.name}】已加入本地名册。`, "success");
    } catch (error) {
      importButton.disabled = false;
      importButton.textContent = "加入本地名册";
      showToast(error.message || "导入失败。", "error", 5000);
    }
  });
  dom.importDialogActions.append(cancel, edit, mode, importButton);
  dom.importDialog.showModal();
}

async function handleFileSelection(file) {
  if (!file) return;
  if (file.size > 4 * 1024 * 1024) {
    showToast("角色卡文件不能超过 4 MB。", "error");
    return;
  }
  try {
    const card = JSON.parse(await file.text());
    if (!card || typeof card !== "object" || Array.isArray(card)) throw new Error("角色卡必须是 JSON 对象。");
    await previewImportedCard(card);
  } catch (error) {
    showInvalidImport(error, null);
  } finally {
    dom.fileInput.value = "";
  }
}

function confirmAction(title, message, action) {
  dom.confirmTitle.textContent = title;
  clear(dom.confirmContent).append(node("p", { text: message }));
  state.pendingConfirm = action;
  dom.confirmDialog.showModal();
}

function bindGlobalEvents() {
  document.querySelector("#newCharacterButton").addEventListener("click", createDraftAndOpen);
  document.querySelector("#emptyNewButton").addEventListener("click", createDraftAndOpen);
  document.querySelector("#importButton").addEventListener("click", () => dom.fileInput.click());
  document.querySelector("#brandButton").addEventListener("click", showLibrary);
  document.querySelector("#backButton").addEventListener("click", showLibrary);
  document.querySelector("#printButton").addEventListener("click", () => window.print());
  document.querySelector("#exportDraftButton").addEventListener("click", exportActiveDraft);
  document.querySelector("#exportTextDraftButton").addEventListener("click", exportActiveDraftText);
  dom.settingsButton.addEventListener("click", openSettingsDialog);
  dom.saveSettingsButton.addEventListener("click", async () => {
    dom.saveSettingsButton.disabled = true;
    try {
      await saveWorkshopSettings();
      dom.settingsDialog.close();
    } catch (error) {
      dom.settingsNotice.hidden = false;
      dom.settingsNotice.textContent = error.message || "无法保存设置。";
    } finally {
      dom.saveSettingsButton.disabled = false;
    }
  });
  dom.testComfyButton.addEventListener("click", () => runSettingsTest("comfyui"));
  dom.testLlmButton.addEventListener("click", () => runSettingsTest("llm"));
  dom.clearApiKeyButton.addEventListener("click", clearWorkshopApiKey);
  dom.fileInput.addEventListener("change", () => handleFileSelection(dom.fileInput.files?.[0]));
  dom.previousStepButton.addEventListener("click", () => goToStep(state.currentStep - 1));
  dom.nextStepButton.addEventListener("click", () => goToStep(state.currentStep + 1));
  document.querySelectorAll(".mobile-pane-switch [data-mobile-pane]").forEach((button) => {
    button.addEventListener("click", () => {
      const pane = button.dataset.mobilePane;
      dom.editorLayout.dataset.mobilePane = pane;
      document.querySelectorAll(".mobile-pane-switch [data-mobile-pane]").forEach((item) => item.classList.toggle("active", item.dataset.mobilePane === pane));
    });
  });
  dom.confirmAcceptButton.addEventListener("click", () => {
    const action = state.pendingConfirm;
    state.pendingConfirm = null;
    if (typeof action === "function") action();
  });
  dom.confirmDialog.addEventListener("close", () => { state.pendingConfirm = null; });
  dom.portraitViewerClose.addEventListener("click", () => dom.portraitViewer.close());
  dom.portraitViewer.addEventListener("click", (event) => {
    if (event.target === dom.portraitViewer) dom.portraitViewer.close();
  });
  dom.portraitViewer.addEventListener("close", () => {
    dom.portraitViewerImage.removeAttribute("src");
    dom.portraitViewerImage.alt = "";
  });
  window.addEventListener("beforeunload", () => persistLocalState());
}

async function initialize() {
  restoreLocalState();
  bindGlobalEvents();
  renderLibraryLoading();
  renderLibrary();
  try {
    await loadCatalog();
    await loadWorkshopSettings();
    await loadCharacters();
    renderLibrary();
  } catch (error) {
    dom.libraryNotice.hidden = false;
    dom.libraryNotice.textContent = `无法连接角色工房：${error.message || "服务未响应"}`;
    clear(dom.characterGrid);
    renderLibrary();
  }
}

initialize();
