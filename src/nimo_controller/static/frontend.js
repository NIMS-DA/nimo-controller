/**
 * NIMO Controller Frontend
 * Blockly workflow editor + Chat for block generation
 */
(() => {
  const $ = (id) => document.getElementById(id);

  /** Create a small label element used inside tool cards. */
  function makeToolSectionLabel(text) {
    const el = document.createElement("div");
    el.className = "tool-section-label";
    el.textContent = text;
    return el;
  }

  // =========================================================================
  // Log UI
  // =========================================================================
  const logEl = () => $("log");
  const statusEl = () => $("logStatus");

  function clearLogPlaceholder() {
    const el = logEl();
    if (!el) return;
    if (el.classList.contains("log-placeholder")) {
      el.classList.remove("log-placeholder");
      el.innerHTML = "";
    }
  }

  function setLogPlaceholder(text) {
    const el = logEl();
    if (!el) return;
    el.classList.add("log-placeholder");
    el.innerHTML = '<div class="chat-placeholder"></div>';
    const ph = el.querySelector(".chat-placeholder");
    if (ph) ph.textContent = text || "";
  }

  /** Append a generic row to the log panel. */
  function appendRow(role, className, text) {
    const el = logEl();
    if (!el) return;
    clearLogPlaceholder();
    const row = document.createElement("div");
    row.className = `chat-row ${role}`;
    const box = document.createElement("div");
    box.className = className;
    box.textContent = String(text ?? "");
    row.appendChild(box);
    el.appendChild(row);
    el.scrollTop = el.scrollHeight;
  }

  /** Append a chat bubble to the log panel. */
  function appendBubble(role, text) {
    const el = logEl();
    if (!el) return;
    clearLogPlaceholder();
    const row = document.createElement("div");
    row.className = `chat-row ${role}`;
    const bubble = document.createElement("div");
    bubble.className = role === "user" ? "user-bubble" : "assistant-bubble";
    bubble.textContent = String(text ?? "");
    row.appendChild(bubble);
    el.appendChild(row);
    el.scrollTop = el.scrollHeight;
  }

  const logSystem = (t) => appendRow("system", "system-log", String(t ?? "").replace(/\n{3,}/g, "\n\n"));
  const logUser = (t) => appendBubble("user", t);
  const logAssistant = (t) => appendBubble("assistant", t);

  /**
   * Append a card-style log message to the log panel.
   * @param {"error"|"warn"|"info"|"success"|"loop"|"if"} level
   * @param {string} title - Short label shown in the badge.
   * @param {string} [message] - Detail text shown in the card body.
   * @param {Object} [opts]
   * @param {string|string[]} [opts.code] - One or more code blocks to render.
   * @param {string} [opts.suffix] - Small label appended after badge in header.
   * @param {boolean} [opts.resultBadge] - If set, show a true/false badge after codes.
   * @returns {HTMLElement} The card element.
   */
  function logCard(level, title, message, opts) {
    const log = logEl();
    if (!log) return null;
    clearLogPlaceholder();
    const row = document.createElement("div"); row.className = "chat-row system";
    const card = document.createElement("div"); card.className = `log-card log-card--${level}`;
    const head = document.createElement("div"); head.className = "log-card__head";
    const badge = document.createElement("span"); badge.className = `log-card__badge log-card__badge--${level}`;
    badge.textContent = title;
    head.appendChild(badge);
    if (opts?.suffix) {
      const suf = document.createElement("span"); suf.className = "log-card__suffix";
      suf.textContent = opts.suffix;
      head.appendChild(suf);
    }
    card.appendChild(head);
    if (message) {
      const body = document.createElement("div"); body.className = "log-card__body";
      body.textContent = String(message);
      card.appendChild(body);
    }
    const codes = opts?.code;
    if (codes || opts?.resultBadge != null) {
      const line = document.createElement("div"); line.className = "log-card__codeline";
      if (codes) {
        const list = Array.isArray(codes) ? codes : [codes];
        for (const c of list) {
          const el = document.createElement("code"); el.className = "log-card__code";
          el.textContent = String(c);
          line.appendChild(el);
        }
      }
      if (opts?.resultBadge != null) {
        const rb = document.createElement("span");
        rb.className = opts.resultBadge ? "log-card__result log-card__result--true" : "log-card__result log-card__result--false";
        rb.textContent = opts.resultBadge ? "true" : "false";
        line.appendChild(rb);
      }
      card.appendChild(line);
    }
    row.appendChild(card); log.appendChild(row); log.scrollTop = log.scrollHeight;
    return card;
  }

  const logError = (title, msg) => logCard("error", title, msg);
  const logWarn  = (title, msg) => logCard("warn", title, msg);
  const logInfo  = (title, msg) => logCard("info", title, msg);
  const logOk    = (title, msg) => logCard("success", title, msg);

  function safeJson(obj) {
    try { return JSON.stringify(obj, null, 2); } catch { return String(obj); }
  }

  /** Format current time as HH:MM:SS. */
  function timeStamp() {
    const d = new Date();
    return [d.getHours(), d.getMinutes(), d.getSeconds()].map(n => String(n).padStart(2, "0")).join(":");
  }

  /** Classify a backend text/if log and render it as a styled card. */
  function renderTextLog(payload) {
    const text = typeof payload === "string" ? payload : (payload?.text || "");
    if (!text) return;

    // "▶ Repeat x5" — skip, rounds show the loop context
    if (text.startsWith("▶ Repeat")) return;

    // "-- Round 1/5 --" — show as a loop card
    if (text.startsWith("-- Round")) {
      const label = text.replace(/^-+\s*|\s*-+$/g, "").trim();
      logCard("loop", "Loop", label);
      return;
    }

    // "▶ If ..." — [If] i = 3 on header, then  3 < 5  true  on next line
    if (text.startsWith("▶ If")) {
      const v   = typeof payload === "object" ? payload : {};
      const name = v.counter_var, val = v.counter_value;
      const op = v.op, tgt = v.target;
      const res = v.result === true || v.result === "True";

      if (name != null && val != null) {
        logCard("if", "If", null, {
          suffix: `${name} = ${val}`,
          code: `${val} ${op} ${tgt}`,
          resultBadge: res,
        });
      } else {
        logCard("if", "If", null, { code: text.slice(2).trim() });
      }
      return;
    }
    logSystem(text);
  }

  // =========================================================================
  // Busy indicator
  // =========================================================================
  const busy = { timer: null, dots: 0, running: false, text: "" };

  function setStatus(text) {
    const st = statusEl();
    if (!st) return;
    busy.text = String(text ?? "");
    if (!busy.running || !busy.text) {
      st.style.display = "none";
      st.textContent = "";
      return;
    }
    st.style.display = "block";
    st.textContent = busy.text;
  }

  function busyStart(base = "⏳") {
    busyStop();
    busy.running = true;
    busy.dots = 0;
    setStatus(base);
    busy.timer = setInterval(() => {
      busy.dots = (busy.dots + 1) % 4;
      setStatus(`${base}${".".repeat(busy.dots)}`);
    }, 1000);
  }

  function busyStop() {
    if (busy.timer) clearInterval(busy.timer);
    busy.timer = null;
    busy.dots = 0;
    busy.running = false;
    setStatus("");
  }

  // =========================================================================
  // API helper
  // =========================================================================

  /** Simple fetch wrapper for API calls. */
  async function apiFetch(url, opts = {}) {
    return fetch(url, opts);
  }

  // =========================================================================
  // Blockly — constants & helpers
  // =========================================================================
  const NIMO_VAR_KEY = "__nimo_var__";
  const LAST_FLOAT_KEY = "__last_float__";
  const LOOP_COUNTER_KEY = "__loop_counter__";

  const safeSlug = (s) => String(s).trim().replace(/\s+/g, "_").replace(/[^A-Za-z0-9_-]/g, "_");
  const hueFromString = (s) => { let h = 0; for (let i = 0; i < s.length; i++) h = s.charCodeAt(i) + ((h << 5) - h); return Math.abs(h) % 360; };
  const toolStmtType = (sid, name) => `mcp__${safeSlug(sid)}__${safeSlug(name)}__stmt`;
  const nimoVarType = (name) => `nimo_var__${safeSlug(name)}`;

  // Block colors (Blockly hue values)
  const COLOR_REPEAT = 120;    // green  — repeat blocks
  const COLOR_IF     = 38;     // bright orange — if block
  const COLOR_NIMO   = 230;    // dark blue — NIMO selection/update
  const COLOR_VARS   = 50;     // yellow — variables, counter ref
  const COLOR_TOOL   = 200;    // bright blue — MCP tools
  const NIMO_SEL_TYPE = "nimo_selection", NIMO_UPD_TYPE = "nimo_update";

  // =========================================================================
  // Blockly — block definitions
  // =========================================================================
  Blockly.Blocks["repeat_n_with_index"] = { init() {
    this.appendDummyInput().appendField("repeat").appendField(new Blockly.FieldNumber(1, 1, 1000, 1), "TIMES")
      .appendField("times  counter").appendField(new Blockly.FieldTextInput("i"), "COUNTER_VAR");
    this.appendStatementInput("DO").setCheck(null);
    this.setColour(COLOR_REPEAT); this.setPreviousStatement(true); this.setNextStatement(true);
    this.setTooltip("Repeat N times with a named counter variable (0-indexed).");
  }};

  Blockly.Blocks["loop_counter_ref"] = { init() {
    this.appendDummyInput().appendField("counter").appendField(new Blockly.FieldTextInput("i"), "COUNTER_VAR");
    this.setColour(COLOR_VARS); this.setOutput(true, null);
    this.setTooltip("Reference the current value of a loop counter variable.");
  }};

  Blockly.Blocks["if_counter"] = { init() {
    this.appendDummyInput()
      .appendField("if counter")
      .appendField(new Blockly.FieldTextInput("i"), "COUNTER_VAR")
      .appendField(new Blockly.FieldDropdown([["<","<"],[">",">"],["=","=="],["≤","<="],["≥",">="]]), "OP")
      .appendField(new Blockly.FieldNumber(1, -Infinity, Infinity, 1), "VALUE");
    this.appendStatementInput("THEN").appendField("then");
    this.appendStatementInput("ELSE").appendField("else");
    this.setColour(COLOR_IF);
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setTooltip("Execute 'then' or 'else' blocks based on a counter comparison.");
  }};

  Blockly.Blocks[NIMO_UPD_TYPE] = { init() {
    this.appendDummyInput().appendField("update");
    this.setPreviousStatement(true); this.setNextStatement(true); this.setColour(COLOR_NIMO);
  }};

  function defineNimoSelectionBlock(methods) {
    const opts = (Array.isArray(methods) && methods.length ? methods : ["PHYSBO", "RE", "PDC"]).map((v) => [String(v), String(v)]);
    Blockly.Blocks[NIMO_SEL_TYPE] = { init() {
      this.appendDummyInput().appendField("selection").appendField("method").appendField(new Blockly.FieldDropdown(opts), "METHOD");
      this.setPreviousStatement(true); this.setNextStatement(true); this.setColour(COLOR_NIMO);
    }};
  }

  function defineNimoVarBlock(name) {
    const type = nimoVarType(name);
    if (Blockly.Blocks[type]) return type;
    Blockly.Blocks[type] = { init() { this.appendDummyInput().appendField(name, "VARNAME"); this.setColour(COLOR_VARS); this.setOutput(true, null); }};
    return type;
  }

  // =========================================================================
  // Blockly — workspace init
  // =========================================================================
  /** Build initial toolbox XML with just Core blocks. */
  function makeInitialToolbox() {
    const xml = document.createElement("xml");
    const core = document.createElement("category");
    core.setAttribute("name", "Core"); core.setAttribute("colour", COLOR_REPEAT);
    for (const t of ["repeat_n_with_index", "loop_counter_ref", "if_counter"]) {
      const b = document.createElement("block"); b.setAttribute("type", t); core.appendChild(b);
    }
    xml.appendChild(core);
    const nimo = document.createElement("category");
    nimo.setAttribute("name", "nimo"); nimo.setAttribute("colour", COLOR_NIMO);
    xml.appendChild(nimo);
    return xml;
  }

  const workspace = Blockly.inject("workspace", { toolbox: makeInitialToolbox() });

  // XML helpers
  function exportWorkspaceXml() { return (Blockly.utils?.xml?.domToText || Blockly.Xml.domToText)(Blockly.Xml.workspaceToDom(workspace)); }
  function loadWorkspaceXml(xmlText) {
    if (!xmlText) return;
    try {
      const dom = (Blockly.utils?.xml?.textToDom || Blockly.Xml.textToDom)(xmlText);
      workspace.clear();
      Blockly.Xml.domToWorkspace(dom, workspace);
    } catch (e) {
      logWarn("Workspace", `Failed to restore: ${e}`); 
      workspace.clear();
    }
  }

  // =========================================================================
  // AST JSON → Blockly blocks (import)
  // =========================================================================

  /**
   * Import a workflow AST into the workspace, placing new blocks below existing ones.
   * @param {Object} ast - Object with a `body` array of workflow nodes.
   */
  function importAstToWorkspace(ast) {
    if (!ast || !Array.isArray(ast.body)) { logError("Generate", "Invalid AST: missing body"); return; }
    let maxY = 20;
    for (const b of workspace.getTopBlocks(false)) {
      const pos = b.getRelativeToSurfaceXY();
      const h = b.getHeightWidth ? b.getHeightWidth().height : 60;
      maxY = Math.max(maxY, pos.y + h + 40);
    }
    try {
      const top = createChain(ast.body);
      if (top) top.moveBy(20, maxY);
      logOk("Generate", "Blocks created.");
    } catch (e) { logError("Generate", String(e)); }
  }

  /** Create a connected chain of statement blocks from an array of AST nodes. */
  function createChain(nodes) {
    if (!Array.isArray(nodes) || !nodes.length) return null;
    let first = null, prev = null;
    for (const node of nodes) {
      const block = createNode(node);
      if (!block) continue;
      if (!first) first = block;
      if (prev?.nextConnection && block.previousConnection) prev.nextConnection.connect(block.previousConnection);
      prev = block;
    }
    return first;
  }

  /** Create a single Blockly block from a workflow AST node. */
  function createNode(node) {
    if (!node) return null;
    if (node.kind === "repeat") return createRepeatBlock(node);
    if (node.kind === "if") return createIfBlock(node);
    if (node.kind === "tool") return createToolBlock(node);
    logWarn("Generate", `Unknown node kind: ${node.kind}`);
    return null;
  }

  function createRepeatBlock(node) {
    const block = workspace.newBlock("repeat_n_with_index");
    block.setFieldValue(String(Number(node.times) || 1), "TIMES");
    block.setFieldValue(node.counter_var, "COUNTER_VAR");
    block.initSvg(); block.render();
    if (Array.isArray(node.body) && node.body.length) {
      const inner = createChain(node.body);
      const input = block.getInput("DO");
      if (inner && input?.connection && inner.previousConnection) input.connection.connect(inner.previousConnection);
    }
    return block;
  }

  function createIfBlock(node) {
    const block = workspace.newBlock("if_counter");
    block.setFieldValue(String(node.counter_var || "i"), "COUNTER_VAR");
    block.setFieldValue(String(node.op || "=="), "OP");
    block.setFieldValue(String(Number(node.value) || 0), "VALUE");
    block.initSvg(); block.render();
    if (Array.isArray(node.then) && node.then.length) {
      const inner = createChain(node.then);
      const inp = block.getInput("THEN");
      if (inner && inp?.connection && inner.previousConnection) inp.connection.connect(inner.previousConnection);
    }
    if (Array.isArray(node.else) && node.else.length) {
      const inner = createChain(node.else);
      const inp = block.getInput("ELSE");
      if (inner && inp?.connection && inner.previousConnection) inp.connection.connect(inner.previousConnection);
    }
    return block;
  }

  function createToolBlock(node) {
    const { server_id: sid, tool: name, args = {} } = node;
    // Special NIMO blocks
    if (sid === "nimo" && name === "selection") {
      const b = workspace.newBlock(NIMO_SEL_TYPE);
      if (args.method) b.setFieldValue(String(args.method), "METHOD");
      b.initSvg(); b.render(); return b;
    }
    if (sid === "nimo" && name === "update") {
      const b = workspace.newBlock(NIMO_UPD_TYPE); b.initSvg(); b.render(); return b;
    }
    // General MCP tool
    const type = toolStmtType(sid, name);
    if (!Blockly.Blocks[type]) { logWarn("Generate", `Unknown tool: ${sid}/${name}`); return null; }
    const block = workspace.newBlock(type);
    block.initSvg(); block.render();
    const schema = toolKeyToSchema.get(`${sid}::${name}`) || {};
    const props = schema?.properties || {};
    for (const [key, value] of Object.entries(args)) {
      const ps = props[key];
      if (schemaType(ps) === "enum" || Array.isArray(ps?.enum)) { try { block.setFieldValue(String(value), key); } catch {} continue; }
      const vb = createValueBlock(value); if (!vb) continue;
      const inp = block.getInput(key);
      if (inp?.connection && vb.outputConnection) inp.connection.connect(vb.outputConnection);
    }
    return block;
  }

  /** Create a value (output) block for a literal, nimo var, or counter ref. */
  function createValueBlock(value) {
    if (value == null) return null;
    if (typeof value === "object" && value[NIMO_VAR_KEY]) {
      const type = defineNimoVarBlock(String(value[NIMO_VAR_KEY]));
      const b = workspace.newBlock(type); b.initSvg(); b.render(); return b;
    }
    if (typeof value === "object" && value[LOOP_COUNTER_KEY]) {
      const b = workspace.newBlock("loop_counter_ref"); b.setFieldValue(String(value[LOOP_COUNTER_KEY]), "COUNTER_VAR"); b.initSvg(); b.render(); return b;
    }
    if (typeof value === "number") { const b = workspace.newBlock("math_number"); b.setFieldValue(String(value), "NUM"); b.initSvg(); b.render(); return b; }
    if (typeof value === "boolean") { const b = workspace.newBlock("logic_boolean"); b.setFieldValue(value ? "TRUE" : "FALSE", "BOOL"); b.initSvg(); b.render(); return b; }
    if (typeof value === "string") { const b = workspace.newBlock("text"); b.setFieldValue(value, "TEXT"); b.initSvg(); b.render(); return b; }
    return null;
  }

  // =========================================================================
  // Toolbox build (from /tools + /nimo/parameters)
  // =========================================================================
  const blockTypeInfo = new Map();   // stmtType → {serverId, toolName}
  const toolKeyToSchema = new Map(); // "sid::name" → JSON schema

  function schemaType(s) { return s?.type || (Array.isArray(s?.enum) ? "enum" : null); }

  function buildInputForProp(block, key, ps) {
    const t = schemaType(ps);
    if (t === "enum" || Array.isArray(ps?.enum)) {
      block.appendDummyInput().appendField(key).appendField(new Blockly.FieldDropdown(ps.enum.map((v) => [String(v), String(v)])), key);
      return;
    }
    const inp = block.appendValueInput(key).appendField(key);
    if (t === "integer" || t === "number") inp.setCheck("Number");
    else if (t === "boolean") inp.setCheck("Boolean");
    else if (t === "string") inp.setCheck("String");
    else inp.setCheck(null);
  }

  function makeShadowXml(key, ps) {
    const t = schemaType(ps);
    if (t === "enum" || Array.isArray(ps?.enum)) return null;
    const val = document.createElement("value"); val.setAttribute("name", key);
    const shadow = document.createElement("shadow");
    const field = document.createElement("field");
    if (t === "boolean") { shadow.setAttribute("type", "logic_boolean"); field.setAttribute("name", "BOOL"); field.textContent = ps?.default === true ? "TRUE" : "FALSE"; }
    else if (t === "number" || t === "integer") { shadow.setAttribute("type", "math_number"); field.setAttribute("name", "NUM"); field.textContent = String(Number.isFinite(ps?.default) ? ps.default : 0); }
    else { shadow.setAttribute("type", "text"); field.setAttribute("name", "TEXT"); field.textContent = typeof ps?.default === "string" ? ps.default : ""; }
    shadow.appendChild(field); val.appendChild(shadow);
    return val;
  }

  function extractSelectionMethods(tools) {
    const t = tools.find((x) => x.server_id === "nimo" && x.name === "selection");
    if (!t) return ["PHYSBO", "RE", "PDC"];
    const m = (t.input_schema?.properties?.method) || {};
    if (Array.isArray(m.enum) && m.enum.length) return m.enum.map(String);
    const ref = m.$ref;
    if (typeof ref === "string") {
      const d = (t.input_schema.$defs || {})[ref.split("/").pop()];
      if (d?.enum?.length) return d.enum.map(String);
    }
    return ["PHYSBO", "RE", "PDC"];
  }

  /** Fetch tools from backend and rebuild the Blockly toolbox. */
  async function buildToolbox() {
    const tools = await (await apiFetch("/tools")).json();
    defineNimoSelectionBlock(extractSelectionMethods(tools));

    const xml = document.createElement("xml");

    // Core category
    const core = document.createElement("category"); core.setAttribute("name", "Core"); core.setAttribute("colour", COLOR_REPEAT);
    for (const t of ["repeat_n_with_index", "loop_counter_ref", "if_counter"]) {
      const b = document.createElement("block"); b.setAttribute("type", t); core.appendChild(b);
    }
    xml.appendChild(core);

    // NIMO category
    const nimo = document.createElement("category"); nimo.setAttribute("name", "nimo"); nimo.setAttribute("colour", COLOR_NIMO);
    for (const t of [NIMO_SEL_TYPE, NIMO_UPD_TYPE]) { const b = document.createElement("block"); b.setAttribute("type", t); nimo.appendChild(b); }
    try {
      const pRes = await (await apiFetch("/nimo/parameters")).json();
      if (pRes.ok) for (const p of pRes.parameters) { const b = document.createElement("block"); b.setAttribute("type", defineNimoVarBlock(p)); nimo.appendChild(b); }
    } catch (e) { logWarn("NIMO", `Parameters: ${e}`); }
    xml.appendChild(nimo);

    // Server categories (including non-hardcoded nimo tools)
    const NIMO_HARDCODED = new Set(["selection", "update", "get_parameter_names", "get_proposal", "reinitialize"]);
    const cats = new Map();
    for (const tool of tools) {
      const sid = tool.server_id;
      if (sid === "nimo" && NIMO_HARDCODED.has(tool.name)) continue;
      const cat = sid === "nimo" ? nimo : (() => {
        if (!cats.has(sid)) { const c = document.createElement("category"); c.setAttribute("name", sid); c.setAttribute("colour", hueFromString(sid)); cats.set(sid, c); xml.appendChild(c); }
        return cats.get(sid);
      })();
      const schema = tool.input_schema || {};
      toolKeyToSchema.set(`${sid}::${tool.name}`, schema);
      const type = toolStmtType(sid, tool.name);
      blockTypeInfo.set(type, { serverId: sid, toolName: tool.name });
      Blockly.Blocks[type] = { init() {
        this.appendDummyInput().appendField(tool.name);
        for (const [k, ps] of Object.entries(schema?.properties || {})) buildInputForProp(this, k, ps);
        this.setColour(sid === "nimo" ? COLOR_NIMO : COLOR_TOOL); this.setPreviousStatement(true); this.setNextStatement(true); this.setTooltip(tool.description || "");
      }};
      const bx = document.createElement("block"); bx.setAttribute("type", type);
      for (const [k, ps] of Object.entries(schema?.properties || {})) { const s = makeShadowXml(k, ps); if (s) bx.appendChild(s); }
      cat.appendChild(bx);
    }

    workspace.updateToolbox(xml);
  }

  // =========================================================================
  // Workflow export (Blockly → AST JSON)
  // =========================================================================
  function exportValueExpr(block) {
    if (block?.type?.startsWith("nimo_var__")) return { [NIMO_VAR_KEY]: block.getFieldValue("VARNAME") };
    if (block?.type === "loop_counter_ref") return { [LOOP_COUNTER_KEY]: String(block.getFieldValue("COUNTER_VAR") || "i") };
    if (block?.type === "math_number") { const v = Number(block.getFieldValue("NUM")); return Number.isFinite(v) ? v : 0; }
    if (block?.type === "text") return String(block.getFieldValue("TEXT") ?? "");
    if (block?.type === "logic_boolean") return block.getFieldValue("BOOL") === "TRUE";
    return null;
  }

  function exportToolArgs(block, sid, name) {
    const schema = toolKeyToSchema.get(`${sid}::${name}`) || {};
    const args = {};
    for (const [key, ps] of Object.entries(schema?.properties || {})) {
      if (schemaType(ps) === "enum" || Array.isArray(ps?.enum)) args[key] = String(block.getFieldValue(key));
      else args[key] = exportValueExpr(block.getInputTargetBlock(key));
    }
    return args;
  }

  function exportChain(block) {
    const out = [];
    while (block) {
      if (block.type === "repeat_n_with_index") {
        out.push({ kind: "repeat", times: Number(block.getFieldValue("TIMES")) || 1, counter_var: String(block.getFieldValue("COUNTER_VAR") || "i"), body: exportChain(block.getInputTargetBlock("DO")) || [], block_id: block.id });
      } else if (block.type === "if_counter") {
        out.push({
          kind: "if", counter_var: String(block.getFieldValue("COUNTER_VAR") || "i"),
          op: String(block.getFieldValue("OP")), value: Number(block.getFieldValue("VALUE")) || 0,
          then: exportChain(block.getInputTargetBlock("THEN")) || [],
          else: exportChain(block.getInputTargetBlock("ELSE")) || [],
          block_id: block.id,
        });
      } else if (block.type === NIMO_SEL_TYPE) {
        out.push({ kind: "tool", server_id: "nimo", tool: "selection", args: { method: String(block.getFieldValue("METHOD")) }, block_id: block.id });
      } else if (block.type === NIMO_UPD_TYPE) {
        out.push({ kind: "tool", server_id: "nimo", tool: "update", args: { objs: { [LAST_FLOAT_KEY]: true } }, block_id: block.id });
      } else {
        const info = blockTypeInfo.get(block.type);
        if (!info) throw new Error(`Unknown block: ${block.type}`);
        out.push({ kind: "tool", server_id: info.serverId, tool: info.toolName, args: exportToolArgs(block, info.serverId, info.toolName), block_id: block.id });
      }
      block = block.getNextBlock();
    }
    return out;
  }

  /** Export the entire workspace as a workflow AST. */
  function exportWorkflowAst() {
    const tops = workspace.getTopBlocks(false);
    if (!tops.length) throw new Error("Place blocks before running");
    const sorted = tops.slice().sort((a, b) => { const pa = a.getRelativeToSurfaceXY(), pb = b.getRelativeToSurfaceXY(); return pa.y - pb.y || pa.x - pb.x; });
    const body = [];
    for (const b of sorted) body.push(...exportChain(b));
    const ast = { body };
    validateCounterScopes(ast.body, new Set());
    return ast;
  }

  /**
   * Recursively validate that every counter variable reference (in if_counter
   * blocks and loop_counter_ref values) is defined by an enclosing
   * repeat_n_with_index block.
   *
   * @param {Array} nodes - Array of AST nodes to validate.
   * @param {Set<string>} scope - Counter variable names currently in scope.
   * @throws {Error} If a reference to an undefined counter is found.
   */
  function validateCounterScopes(nodes, scope) {
    if (!Array.isArray(nodes)) return;
    for (const node of nodes) {
      if (node.kind === "repeat") {
        const inner = new Set(scope);
        if (node.counter_var) inner.add(node.counter_var);
        validateCounterScopes(node.body, inner);
      } else if (node.kind === "if") {
        if (!scope.has(node.counter_var)) {
          throw new Error(
            `if block references counter "${node.counter_var}" which is not defined by any enclosing repeat block. ` +
            `Wrap it inside a "repeat … counter ${node.counter_var}" block.`
          );
        }
        validateCounterScopes(node.then, scope);
        validateCounterScopes(node.else, scope);
      } else if (node.kind === "tool") {
        validateCounterInArgs(node.args, scope);
      }
    }
  }

  /** Check that any __loop_counter__ references in tool args are in scope. */
  function validateCounterInArgs(obj, scope) {
    if (!obj || typeof obj !== "object") return;
    if (obj[LOOP_COUNTER_KEY]) {
      const name = obj[LOOP_COUNTER_KEY];
      if (!scope.has(name)) {
        throw new Error(
          `Tool argument references counter "${name}" which is not defined by any enclosing repeat block. ` +
          `Wrap it inside a "repeat … counter ${name}" block.`
        );
      }
      return;
    }
    for (const v of Object.values(obj)) {
      if (v && typeof v === "object") validateCounterInArgs(v, scope);
    }
  }

  // =========================================================================
  // Tool card UI (shared by workflow + chat logs)
  // =========================================================================
  const toolCallIdToCard = new Map();
  const toolCallIdToName = new Map();

  /** Convert a Blockly hue (0-360) to an HSL color string. */
  function hueToColor(hue, s = 55, l = 50) {
    return `hsl(${hue}, ${s}%, ${l}%)`;
  }

  /**
   * Render a value as a readable DOM element.
   * Objects/arrays with flat keys → key-value table.
   * Primitives → inline text.
   */
  function renderValue(val) {
    if (val == null) { const s = document.createElement("span"); s.className = "tool-val tool-val--null"; s.textContent = "null"; return s; }
    if (typeof val === "string") { const s = document.createElement("span"); s.className = "tool-val tool-val--str"; s.textContent = `"${val}"`; return s; }
    if (typeof val === "number" || typeof val === "boolean") { const s = document.createElement("span"); s.className = "tool-val tool-val--num"; s.textContent = String(val); return s; }
    if (Array.isArray(val)) {
      if (val.length === 0) { const s = document.createElement("span"); s.className = "tool-val tool-val--null"; s.textContent = "[]"; return s; }
      if (val.every(v => typeof v !== "object" || v === null)) {
        const s = document.createElement("span"); s.className = "tool-val";
        s.textContent = "[" + val.map(v => v === null ? "null" : typeof v === "string" ? `"${v}"` : String(v)).join(", ") + "]";
        return s;
      }
      const pre = document.createElement("pre"); pre.className = "tool-pre tool-pre--nested"; pre.textContent = safeJson(val); return pre;
    }
    if (typeof val === "object") {
      const entries = Object.entries(val);
      if (entries.length === 0) { const s = document.createElement("span"); s.className = "tool-val tool-val--null"; s.textContent = "{}"; return s; }
      // If all values are flat, render as key-value rows
      const allFlat = entries.every(([, v]) => v === null || typeof v !== "object");
      if (allFlat) {
        const tbl = document.createElement("div"); tbl.className = "tool-kv";
        for (const [k, v] of entries) {
          const row = document.createElement("div"); row.className = "tool-kv__row";
          const key = document.createElement("span"); key.className = "tool-kv__key"; key.textContent = k;
          const valEl = renderValue(v);
          row.append(key, valEl);
          tbl.appendChild(row);
        }
        return tbl;
      }
      // Mixed: render flat keys inline, nested keys as sub-blocks
      const wrap = document.createElement("div"); wrap.className = "tool-kv";
      for (const [k, v] of entries) {
        const row = document.createElement("div"); row.className = "tool-kv__row";
        const key = document.createElement("span"); key.className = "tool-kv__key"; key.textContent = k;
        row.appendChild(key);
        row.appendChild(renderValue(v));
        wrap.appendChild(row);
      }
      return wrap;
    }
    const s = document.createElement("span"); s.textContent = String(val); return s;
  }

  /** Create and append a tool-call card to the log panel. Returns the card element. */
  function appendToolCard({ server, tool, args }) {
    const log = logEl();
    if (!log) return null;
    clearLogPlaceholder();
    const row = document.createElement("div"); row.className = "chat-row system";
    const card = document.createElement("div"); card.className = "tool-card";
    // Header — badge shows tool name with block color
    const head = document.createElement("div"); head.className = "tool-head";
    const badge = document.createElement("span"); badge.className = "tool-badge";
    badge.textContent = tool || "(tool)";
    const sid = server || "";
    if (sid === "nimo") badge.style.background = hueToColor(COLOR_NIMO);
    else if (sid) badge.style.background = hueToColor(COLOR_TOOL);
    head.appendChild(badge);
    // Args
    const argsBlock = document.createElement("div"); argsBlock.className = "tool-pre";
    argsBlock.appendChild(renderValue(args ?? {}));
    // Output
    const outBlock = document.createElement("div"); outBlock.className = "tool-pre"; outBlock.dataset.role = "output";
    const outSpinner = document.createElement("span"); outSpinner.className = "tool-val tool-val--null"; outSpinner.textContent = "⏳ running…";
    outBlock.appendChild(outSpinner);
    // Images
    const imgBox = document.createElement("div"); imgBox.className = "tool-images"; imgBox.dataset.role = "images";
    card.append(head, makeToolSectionLabel("args"), argsBlock, makeToolSectionLabel("output"), outBlock, imgBox);
    row.appendChild(card); log.appendChild(row); log.scrollTop = log.scrollHeight;
    return card;
  }

  /** Update a tool card's output section. */
  function updateCardOutput(card, output, images) {
    if (!card) return;
    const outBox = card.querySelector('[data-role="output"]');
    if (!outBox) return;
    let display = output, imgs = images;
    if (output && typeof output === "object" && !Array.isArray(output) && "data" in output) {
      display = output.data;
      if (!imgs && Array.isArray(output.images)) imgs = output.images;
    }
    outBox.innerHTML = "";
    outBox.appendChild(renderValue(display));
    if (Array.isArray(imgs) && imgs.length) renderCardImages(card, imgs);
  }

  function renderCardImages(card, images) {
    const box = card.querySelector('[data-role="images"]');
    if (!box) return;
    box.innerHTML = "";
    for (const img of images) {
      if (!img?.data) continue;
      const el = document.createElement("img"); el.className = "tool-image";
      el.src = `data:${img.mimeType || "image/png"};base64,${img.data}`;
      el.alt = "Tool output";
      el.addEventListener("click", () => openLightbox(el.src));
      box.appendChild(el);
    }
    if (box.children.length) { const l = logEl(); if (l) l.scrollTop = l.scrollHeight; }
  }

  function openLightbox(src) {
    let ov = $("imageLightbox");
    if (!ov) {
      ov = document.createElement("div"); ov.id = "imageLightbox"; ov.className = "image-lightbox";
      ov.innerHTML = '<img class="image-lightbox-img" />'; ov.addEventListener("click", () => { ov.style.display = "none"; });
      document.body.appendChild(ov);
    }
    ov.querySelector("img").src = src; ov.style.display = "flex";
  }

  // =========================================================================
  // Chat API
  // =========================================================================

  async function apiChatRun(message) {
    const r = await apiFetch("/chat/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (!r.ok) throw new Error(`chat/run ${r.status}`);
    return r.json();
  }

  /** Render an array of chat log entries, returning true if any assistant message was shown. */
  function renderChatLogs(logs) {
    let sawMsg = false;
    if (!Array.isArray(logs)) return sawMsg;
    for (const it of logs) {
      if (!it) continue;
      if (it.type === "tool_call") {
        const cid = it.call_id || "";
        if (!cid || toolCallIdToCard.has(cid)) continue;
        const server = it.server_id || "", tool = it.tool || "(tool)", args = it.arguments ?? {};
        toolCallIdToCard.set(cid, appendToolCard({ server, tool, args }));
        toolCallIdToName.set(cid, tool);
      } else if (it.type === "tool_output") {
        const cid = it.call_id || "", out = it.output, imgs = Array.isArray(it.images) ? it.images : undefined;
        const card = cid ? toolCallIdToCard.get(cid) : null;
        if (card) updateCardOutput(card, out, imgs);
        else { const fb = appendToolCard({ server: "", tool: "", args: {} }); updateCardOutput(fb, out, imgs); }
        // Detect generate_blocks → import to workspace
        const calledName = cid ? toolCallIdToName.get(cid) : null;
        if (calledName === "generate_blocks") {
          try { let d = out; if (typeof d === "string") d = JSON.parse(d); if (d?.ok && d.ast) importAstToWorkspace(d.ast); else if (d?.error) logError("Generate", d.error); } catch (e) { logError("Generate", String(e)); }
        }
      } else if (it.type === "message" && it.text) { sawMsg = true; logAssistant(it.text); }
    }
    return sawMsg;
  }

  // =========================================================================
  // Workflow execution + SSE
  // =========================================================================
  const execState = { running: false, workflowId: null, es: null, lastCard: null };

  function setRunningUI(on) {
    execState.running = on;
    const btn = $("runBtn"); if (!btn) return;
    btn.classList.toggle("btn-danger", on); btn.classList.toggle("btn-primary", !on);
    btn.textContent = on ? "■ Cancel" : "▶ Run";
    // Lock / unlock the Blockly workspace during execution
    const wsEl = $("workspace");
    if (wsEl) wsEl.classList.toggle("workspace-locked", on);
    const chatBox = $("chatBox");
    if (chatBox) chatBox.classList.toggle("chat-disabled", on);
  }
  function closeSSE() { if (execState.es) { try { execState.es.close(); } catch {} execState.es = null; } }

  /**
   * Show a workflow-finished info card in the log panel.
   * Includes a download link for the execution log.
   */
  function logWorkflowFinished(status, wfId) {
    const labels = { done: "Completed", error: "Failed", canceled: "Canceled" };
    const levels = { done: "info", error: "error", canceled: "warn" };
    const card = logCard(levels[status] || "info", "Workflow", `${labels[status] || status} at ${timeStamp()}`);
    if (!card || !wfId) return;
    const link = document.createElement("a");
    link.className = "log-card__download";
    link.href = `/workflow/${wfId}/log.md`;
    link.download = "";
    link.textContent = "⬇ Download log";
    card.appendChild(link);
  }

  function connectSSE(wfId) {
    closeSSE();
    const es = new EventSource(`/workflow/${wfId}/events`);
    execState.es = es;
    const finish = (status) => {
      busyStop(); setRunningUI(false);
      const id = execState.workflowId;
      localStorage.removeItem("workflow_id"); execState.workflowId = null;
      try { workspace.highlightBlock(null); } catch {}
      closeSSE();
      logWorkflowFinished(status, id);
    };
    es.addEventListener("log", (e) => {
      try {
        const p = JSON.parse(e.data);
        if (p.kind === "tool_call") { execState.lastCard = appendToolCard({ server: p.server_id ?? "", tool: p.tool ?? "", args: p.args ?? {} }); }
        else if (p.kind === "tool_output") { if (execState.lastCard) updateCardOutput(execState.lastCard, p.output); else updateCardOutput(appendToolCard({ server: "", tool: "", args: {} }), p.output); }
        else if (p.kind === "text" && p.text) renderTextLog(p);
        else logSystem(safeJson(p));
      } catch { logSystem(e.data); }
    });
    es.addEventListener("active", (e) => { try { const p = JSON.parse(e.data); if (p?.block_id) workspace.highlightBlock(p.block_id); } catch {} });
    es.addEventListener("status", (e) => { let s = ""; try { s = JSON.parse(e.data)?.status || ""; } catch {} if (s) setStatus(`⏳ ${s}`); if (s === "done") finish("done"); if (s === "error") finish("error"); if (s === "canceled") finish("canceled"); });
    // Server-sent "event: error" with error details
    es.addEventListener("error", (e) => {
      if (e.data) { try { const p = JSON.parse(e.data); if (p?.error) logError("Workflow", p.error); } catch {} }
    });
    // SSE connection error (no data)
    es.onerror = () => setStatus("⏳ reconnecting");
  }

  async function runWorkflow() {
    if (execState.running) return;
    execState.lastCard = null; setRunningUI(true); busyStart("⏳");
    const el = logEl();
    if (el) { el.classList.remove("log-placeholder"); el.innerHTML = ""; }
    logInfo("Workflow", `Started at ${timeStamp()}`);
    let ast;
    try { ast = exportWorkflowAst(); } catch (e) { busyStop(); setRunningUI(false); logError("Error", String(e)); return; }
    console.log("[NIMO] Exported AST:", JSON.stringify(ast, null, 2));
    try {
      const res = await apiFetch("/workflow/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workflow: ast, workspace_xml: exportWorkspaceXml() }) });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error);
      execState.workflowId = data.workflow_id; localStorage.setItem("workflow_id", data.workflow_id);
      connectSSE(data.workflow_id);
    } catch (e) { busyStop(); setRunningUI(false); logError("Error", String(e)); }
  }

  async function cancelWorkflow() {
    const id = execState.workflowId || localStorage.getItem("workflow_id"); if (!id) return;
    logInfo("Cancel", "Cancellation requested");
    try { await apiFetch(`/workflow/${id}/cancel`, { method: "POST" }); setStatus("⏳ cancel_requested"); } catch (e) { logError("Cancel", String(e)); }
  }

  // =========================================================================
  // Chat send
  // =========================================================================
  async function sendChat() {
    const input = $("chatInput"), text = (input?.value || "").trim();
    if (!text) return;
    logUser(text); input.value = ""; busyStart("⏳");
    try {
      const data = await apiChatRun(text);
      if (!data.ok) throw new Error(data.error);
      const sawMsg = renderChatLogs(data.logs);
      busyStop();
      if (!sawMsg && data.reply) logAssistant(data.reply);
    } catch (e) { busyStop(); logError("Chat", String(e)); }
  }

  // =========================================================================
  // Settings panel
  // =========================================================================
  async function apiListServers() { return (await apiFetch("/settings/servers")).json(); }
  async function apiAddServer(name, url) { return (await apiFetch("/settings/servers", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, url }) })).json(); }
  async function apiRemoveServer(name) { return (await apiFetch(`/settings/servers/${encodeURIComponent(name)}`, { method: "DELETE" })).json(); }
  async function apiReconnectServer(name) { return (await apiFetch(`/settings/servers/${encodeURIComponent(name)}/reconnect`, { method: "POST" })).json(); }

  function initSettingsPanel() {
    const panel = $("settingsPanel"); if (!panel) return;
    panel.innerHTML = `<div class="settings-body">
      <div class="settings-section">
        <div class="settings-section-title">MCP Servers</div>
        <div id="settingsServerList" class="settings-server-list"><div class="settings-loading">Loading...</div></div>
        <div class="settings-add-form">
          <input id="settingsServerName" type="text" placeholder="Name" class="settings-input" />
          <input id="settingsServerUrl" type="text" placeholder="URL (e.g. http://127.0.0.1:8000/mcp)" class="settings-input settings-input-wide" />
          <button id="settingsAddBtn" class="btn btn-primary">Add</button>
        </div>
        <div id="settingsError" class="settings-error"></div>
      </div>
    </div>`;
    $("settingsAddBtn")?.addEventListener("click", handleAddServer);
    $("settingsServerUrl")?.addEventListener("keydown", (e) => { if (e.key === "Enter") handleAddServer(); });
  }

  async function refreshServerList() {
    const list = $("settingsServerList"), err = $("settingsError"); if (!list) return;
    list.innerHTML = '<div class="settings-loading">Loading...</div>'; if (err) err.textContent = "";
    try {
      const data = await apiListServers(); if (!data.ok) throw new Error(data.error);
      list.innerHTML = "";
      for (const srv of data.servers) {
        const row = document.createElement("div"); row.className = "settings-server-row";
        const info = document.createElement("div"); info.className = "settings-server-info";
        const n = document.createElement("span"); n.className = "settings-server-name"; n.textContent = srv.name;
        const u = document.createElement("span"); u.className = "settings-server-url"; u.textContent = srv.url;
        info.append(n, u); row.appendChild(info);

        // Reconnect (↻): re-attach to the server using its stored URL,
        // e.g. after the server has been restarted. Dynamic servers only.
        if (!srv.builtin && srv.reconnectable !== false) {
          const rc = document.createElement("button");
          rc.className = "btn btn-ghost settings-reconnect-btn";
          rc.textContent = "↻";
          rc.title = "Reconnect";
          rc.style.marginRight = "2px";
          rc.addEventListener("click", () => handleReconnectServer(srv.name, rc));
          row.appendChild(rc);
        }

        if (srv.builtin) { const b = document.createElement("span"); b.className = "settings-badge-builtin"; b.textContent = "built-in"; row.appendChild(b); }
        else { const d = document.createElement("button"); d.className = "btn btn-ghost settings-delete-btn"; d.textContent = "✕"; d.title = "Remove"; d.addEventListener("click", () => handleRemoveServer(srv.name)); row.appendChild(d); }
        list.appendChild(row);
      }
      if (!data.servers.length) list.innerHTML = '<div class="settings-empty">No servers</div>';
    } catch (e) { list.innerHTML = ""; if (err) err.textContent = String(e); }
  }

  async function handleAddServer() {
    const nameIn = $("settingsServerName"), urlIn = $("settingsServerUrl"), err = $("settingsError"), btn = $("settingsAddBtn");
    const name = nameIn?.value?.trim(), url = urlIn?.value?.trim();
    if (err) err.textContent = "";
    if (!name || !url) { if (err) err.textContent = "Name and URL required."; return; }
    if (btn) btn.disabled = true;
    try {
      const d = await apiAddServer(name, url); if (!d.ok) throw new Error(d.error);
      if (nameIn) nameIn.value = ""; if (urlIn) urlIn.value = "";
      await refreshServerList(); try { await buildToolbox(); } catch {} refreshAgentSidebar(); logOk("Settings", `Server "${name}" added.`);
    } catch (e) { if (err) err.textContent = String(e); }
    finally { if (btn) btn.disabled = false; }
  }

  async function handleRemoveServer(name) {
    if (!confirm(`Remove "${name}"?`)) return;
    try { const d = await apiRemoveServer(name); if (!d.ok) throw new Error(d.error); await refreshServerList(); try { await buildToolbox(); } catch {} refreshAgentSidebar(); logOk("Settings", `Server "${name}" removed.`); }
    catch (e) { const err = $("settingsError"); if (err) err.textContent = String(e); }
  }

  async function handleReconnectServer(name, btn) {
    const err = $("settingsError"); if (err) err.textContent = "";
    const orig = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    try {
      const d = await apiReconnectServer(name); if (!d.ok) throw new Error(d.error);
      // refreshServerList() re-renders the row, so no need to restore btn on success.
      await refreshServerList(); try { await buildToolbox(); } catch {} refreshAgentSidebar();
      logOk("Settings", `Server "${name}" reconnected.`);
    } catch (e) {
      if (err) err.textContent = String(e);
      if (btn) { btn.disabled = false; btn.textContent = orig; }
    }
  }

  // =========================================================================
  // Settings overlay
  // =========================================================================
  function openSettings() {
    const ov = $("settingsOverlay");
    if (ov) { ov.classList.add("active"); refreshServerList(); }
  }

  function closeSettings() {
    const ov = $("settingsOverlay");
    if (ov) ov.classList.remove("active");
  }

  // =========================================================================
  // Mode toggle (Blockly ↔ Agent)
  // =========================================================================
  let currentMode = "blockly";

  function setMode(mode) {
    currentMode = mode;
    const blocklyView = $("blocklyMode");
    const agentView = $("agentMode");
    const toggle = $("modeToggle");
    if (blocklyView) blocklyView.classList.toggle("active", mode === "blockly");
    if (agentView) agentView.classList.toggle("active", mode === "agent");
    if (toggle) toggle.classList.toggle("active", mode === "agent");
    document.querySelectorAll(".mode-toggle__label").forEach((el) => {
      el.classList.toggle("active", el.dataset.mode === mode);
    });
    localStorage.setItem("nimo_mode_v1", mode);
    if (mode === "blockly") setTimeout(() => Blockly.svgResize(workspace), 50);
    if (mode === "agent") {
      const chat = $("agentChat"), log = $("agentLog");
      if (chat && log && !log.children.length) chat.classList.add("agent-chat--centered");
      refreshAgentSidebar();
    }
  }

  /** Populate the agent sidebar with connected MCP servers. */
  async function refreshAgentSidebar() {
    const list = $("agentServerList");
    if (!list) return;
    list.innerHTML = "";
    try {
      const data = await apiListServers();
      if (!data.ok) return;
      for (const srv of data.servers) {
        const item = document.createElement("div");
        item.className = "agent-sidebar__item";
        const dot = document.createElement("span");
        dot.className = "agent-sidebar__dot";
        const name = document.createElement("span");
        name.textContent = srv.name;
        item.append(dot, name);
        list.appendChild(item);
      }
    } catch {}
  }

  // =========================================================================
  // Agent mode — chat with MCP approval
  // =========================================================================
  const agentState = { pending: false, gateCallId: null, queuedApproval: null, suppressed: new Map(), autoApprove: false };

  /** Toggle auto-approval of MCP tool calls in agent mode. */
  function setAutoApprove(on) {
    agentState.autoApprove = !!on;
    const sw = $("autoApproveToggle");
    if (sw) sw.classList.toggle("active", agentState.autoApprove);
    const row = $("autoApproveRow");
    if (row) row.setAttribute("aria-checked", agentState.autoApprove ? "true" : "false");
    localStorage.setItem("nimo_auto_approve_v1", agentState.autoApprove ? "1" : "0");
    // If switched on while a tool call is already waiting, approve it now.
    if (agentState.autoApprove && agentState.pending) {
      agentLogEl()?.querySelector('.tool-approval .btn-primary')?.click();
    }
  }
  const agentCardMap = new Map();
  const agentNameMap = new Map();

  const agentLogEl = () => $("agentLog");

  function agentAppendBubble(role, text) {
    const el = agentLogEl(); if (!el) return;
    const row = document.createElement("div"); row.className = `chat-row ${role}`;
    const bubble = document.createElement("div");
    bubble.className = role === "user" ? "user-bubble" : "assistant-bubble";
    bubble.textContent = String(text ?? "");
    row.appendChild(bubble); el.appendChild(row); el.scrollTop = el.scrollHeight;
  }

  function agentAppendToolCard({ server, tool, args }) {
    const el = agentLogEl(); if (!el) return null;
    const row = document.createElement("div"); row.className = "chat-row system";
    const card = document.createElement("div"); card.className = "tool-card";
    const head = document.createElement("div"); head.className = "tool-head";
    const badge = document.createElement("span"); badge.className = "tool-badge";
    badge.textContent = tool || "(tool)";
    const sid = server || "";
    if (sid === "nimo") badge.style.background = hueToColor(COLOR_NIMO);
    else if (sid) badge.style.background = hueToColor(COLOR_TOOL);
    head.appendChild(badge);
    const argsBlock = document.createElement("div"); argsBlock.className = "tool-pre";
    argsBlock.appendChild(renderValue(args ?? {}));
    const outBlock = document.createElement("div"); outBlock.className = "tool-pre"; outBlock.dataset.role = "output";
    const spinner = document.createElement("span"); spinner.className = "tool-val tool-val--null"; spinner.textContent = "⏳ running…";
    outBlock.appendChild(spinner);
    const imgBox = document.createElement("div"); imgBox.className = "tool-images"; imgBox.dataset.role = "images";
    card.append(head, makeToolSectionLabel("args"), argsBlock, makeToolSectionLabel("output"), outBlock, imgBox);
    row.appendChild(card); el.appendChild(row); el.scrollTop = el.scrollHeight;
    return card;
  }

  function agentUpdateCardOutput(card, output, images) {
    if (!card) return;
    const outBox = card.querySelector('[data-role="output"]'); if (!outBox) return;
    let display = output, imgs = images;
    if (output && typeof output === "object" && !Array.isArray(output) && "data" in output) {
      display = output.data;
      if (!imgs && Array.isArray(output.images)) imgs = output.images;
    }
    outBox.innerHTML = "";
    outBox.appendChild(renderValue(display));
    if (Array.isArray(imgs) && imgs.length) {
      const box = card.querySelector('[data-role="images"]');
      if (box) { box.innerHTML = ""; for (const img of imgs) { if (!img?.data) continue; const ie = document.createElement("img"); ie.className = "tool-image"; ie.src = `data:${img.mimeType || "image/png"};base64,${img.data}`; ie.addEventListener("click", () => openLightbox(ie.src)); box.appendChild(ie); } }
    }
  }

  async function apiAgentRun(message) {
    const r = await apiFetch("/agent/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message }) });
    if (!r.ok) throw new Error(`agent/run ${r.status}`);
    return r.json();
  }

  async function apiAgentDecision(sid, decision, idx) {
    const r = await apiFetch("/agent/decision", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sid, decision, interruption_index: idx ?? 0 }) });
    if (!r.ok) throw new Error(`agent/decision ${r.status}`);
    return r.json();
  }

  function agentRemoveApproval(card) { card?.querySelector('[data-role="approval"]')?.remove(); }

  function agentAttachApproval({ card, sessionId, idx, call }) {
    agentRemoveApproval(card);
    const approveBtn = document.createElement("button"); approveBtn.className = "btn btn-primary"; approveBtn.textContent = "Approve";
    const rejectBtn = document.createElement("button"); rejectBtn.className = "btn btn-danger"; rejectBtn.textContent = "Reject";
    const cid = call?.call_id || null;

    const run = async (decision) => {
      approveBtn.disabled = rejectBtn.disabled = true;
      if (decision === "approve" && cid) agentState.gateCallId = cid;
      agentUpdateCardOutput(card, decision === "approve" ? "⏳ running…" : "🚫 rejected");
      agentRemoveApproval(card); busyStart("⏳");
      try {
        const data = await apiAgentDecision(sessionId, decision, idx ?? 0);
        if (!data.ok) throw new Error(data.error);
        const sawMsg = agentRenderLogs(data.logs);
        if (data.mode === "final") { agentState.pending = false; busyStop(); if (!sawMsg && data.reply) agentAppendBubble("assistant", data.reply); return; }
        if (data.mode === "approval") {
          agentState.pending = true;
          agentState.queuedApproval = { sessionId: data.session_id, idx: 0, call: data.call };
          if (decision === "reject" || !agentState.gateCallId) { const qa = agentState.queuedApproval; agentState.queuedApproval = null; agentState.gateCallId = null; if (qa) agentShowApproval(qa); }
          busyStop(); return;
        }
        agentState.pending = false; busyStop();
      } catch (e) { agentState.pending = false; busyStop(); agentUpdateCardOutput(card, `❌ ${e}`); }
    };
    approveBtn.addEventListener("click", () => run("approve"));
    rejectBtn.addEventListener("click", () => run("reject"));

    // Auto-approve: skip the buttons and run the tool immediately.
    if (agentState.autoApprove) {
      agentUpdateCardOutput(card, "⚡ Auto-approved");
      run("approve");
      return;
    }

    agentUpdateCardOutput(card, "🛂 Approval required");
    const wrap = document.createElement("div"); wrap.className = "tool-approval"; wrap.dataset.role = "approval";
    wrap.append(approveBtn, rejectBtn); card.appendChild(wrap);
  }

  function agentShowApproval({ sessionId, idx, call }) {
    const cid = call?.call_id || null;
    if (agentState.gateCallId) { agentState.queuedApproval = { sessionId, idx, call }; return; }
    let card = cid && agentCardMap.get(cid);
    if (!card && cid && agentState.suppressed.has(cid)) {
      const s = agentState.suppressed.get(cid); agentState.suppressed.delete(cid);
      card = agentAppendToolCard({ server: s.server, tool: s.tool, args: s.args });
      agentCardMap.set(cid, card);
    }
    if (!card) { card = agentAppendToolCard({ server: call?.server_id || "", tool: call?.tool || "(tool)", args: call?.arguments ?? {} }); if (cid) agentCardMap.set(cid, card); }
    agentAttachApproval({ card, sessionId, idx, call });
  }

  function agentRenderLogs(logs) {
    let sawMsg = false;
    if (!Array.isArray(logs)) return sawMsg;
    for (const it of logs) {
      if (!it) continue;
      if (it.type === "tool_call") {
        const cid = it.call_id || ""; if (!cid || agentCardMap.has(cid)) continue;
        const server = it.server_id || "", tool = it.tool || "(tool)", args = it.arguments ?? {};
        if (agentState.gateCallId && cid !== agentState.gateCallId) { agentState.suppressed.set(cid, { server, tool, args }); continue; }
        agentCardMap.set(cid, agentAppendToolCard({ server, tool, args }));
        agentNameMap.set(cid, tool);
      } else if (it.type === "tool_output") {
        const cid = it.call_id || "", out = it.output, imgs = Array.isArray(it.images) ? it.images : undefined;
        let card = cid ? agentCardMap.get(cid) : null;
        if (!card && agentState.gateCallId) card = agentCardMap.get(agentState.gateCallId);
        if (card) agentUpdateCardOutput(card, out, imgs);
        else { const fb = agentAppendToolCard({ server: "", tool: "", args: {} }); agentUpdateCardOutput(fb, out, imgs); }
        if (agentState.gateCallId && (!cid || cid === agentState.gateCallId)) { agentState.gateCallId = null; const qa = agentState.queuedApproval; agentState.queuedApproval = null; if (qa) agentShowApproval(qa); }
      } else if (it.type === "message" && it.text) { sawMsg = true; agentAppendBubble("assistant", it.text); }
    }
    return sawMsg;
  }

  async function sendAgentChat() {
    const input = $("agentInput"), text = (input?.value || "").trim();
    if (!text) return;
    if (agentState.pending) { agentAppendBubble("assistant", "Please approve or reject the pending tool call first."); return; }
    // Transition from centered to bottom layout
    const chat = $("agentChat");
    if (chat) chat.classList.remove("agent-chat--centered");
    agentAppendBubble("user", text);
    input.value = "";
    busyStart("⏳");
    try {
      const data = await apiAgentRun(text);
      if (!data.ok) throw new Error(data.error || "Unknown error");
      const sawMsg = agentRenderLogs(data.logs);
      if (data.mode === "final") {
        busyStop();
        if (!sawMsg && data.reply) agentAppendBubble("assistant", data.reply);
      } else if (data.mode === "approval") {
        busyStop();
        agentState.pending = true;
        agentShowApproval({ sessionId: data.session_id, idx: 0, call: data.call });
      } else {
        busyStop();
      }
    } catch (e) {
      busyStop();
      agentAppendBubble("assistant", `Error: ${e}`);
    }
  }

  // =========================================================================
  // Reattach running workflow on page load
  // =========================================================================
  let pendingXml = null;
  async function reattachIfRunning() {
    try {
      const cur = await (await apiFetch("/workflow/current")).json();
      if (cur.ok && cur.exists && (cur.status === "running" || cur.status === "queued")) {
        pendingXml = cur.workspace_xml || null; execState.workflowId = cur.workflow_id;
        localStorage.setItem("workflow_id", cur.workflow_id); setRunningUI(true); busyStart("⏳");
        logInfo("Reattach", "Reconnecting to running workflow"); connectSSE(cur.workflow_id); return;
      }
      // No running workflow — clear any stale ID
      localStorage.removeItem("workflow_id");
    } catch {
      // Server unreachable — clear stale ID to avoid reconnect loop
      localStorage.removeItem("workflow_id");
    }
  }

  // =========================================================================
  // CSV upload
  // =========================================================================
  async function handleCsvUpload() {
    const input = $("csvFileInput");
    const file = input?.files?.[0];
    if (!file) return;
    input.value = "";

    const form = new FormData();
    form.append("file", file);

    busyStart("⏳");
    try {
      const res = await apiFetch("/nimo/candidates", { method: "POST", body: form });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error);
      logOk("Upload", data.message || `${file.name} uploaded`);
      try { await buildToolbox(); } catch {}
    } catch (e) {
      logError("Upload", String(e));
    }
    busyStop();
  }

  // =========================================================================
  // Main
  // =========================================================================
  async function main() {
    // Wire up buttons
    $("runBtn")?.addEventListener("click", async () => { if (execState.running) await cancelWorkflow(); else await runWorkflow(); });
    $("chatSend")?.addEventListener("click", sendChat);
    $("clearLogBtn")?.addEventListener("click", () => setLogPlaceholder(""));
    $("chatInput")?.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sendChat(); } });
    setRunningUI(false);

    // Agent mode chat
    $("agentSend")?.addEventListener("click", sendAgentChat);
    $("agentInput")?.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); sendAgentChat(); } });

    // Auto-approve toggle (agent mode)
    setAutoApprove(localStorage.getItem("nimo_auto_approve_v1") === "1");
    const autoApproveRow = $("autoApproveRow");
    autoApproveRow?.addEventListener("click", () => setAutoApprove(!agentState.autoApprove));
    autoApproveRow?.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setAutoApprove(!agentState.autoApprove); }
    });

    // Mode toggle
    $("modeToggle")?.addEventListener("click", () => setMode(currentMode === "blockly" ? "agent" : "blockly"));
    const savedMode = localStorage.getItem("nimo_mode_v1");
    if (savedMode === "agent") setMode("agent");

    initSettingsPanel();
    $("settingsCloseBtn")?.addEventListener("click", closeSettings);
    $("settingsOverlay")?.addEventListener("click", (e) => { if (e.target === $("settingsOverlay")) closeSettings(); });
    $("addToolsBtn")?.addEventListener("click", openSettings);
    $("agentAddServerBtn")?.addEventListener("click", openSettings);

    // CSV upload
    $("uploadCsvBtn")?.addEventListener("click", () => $("csvFileInput")?.click());
    $("csvFileInput")?.addEventListener("change", handleCsvUpload);

    // Resizable panels
    const handle = $("resizeHandle");
    const container = $("container");
    if (handle && container) {
      let dragging = false;
      handle.addEventListener("mousedown", (e) => {
        e.preventDefault(); dragging = true; handle.classList.add("dragging");
        document.body.style.cursor = "col-resize"; document.body.style.userSelect = "none";
      });
      document.addEventListener("mousemove", (e) => {
        if (!dragging) return;
        const rect = container.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const total = rect.width;
        const leftPct = Math.max(20, Math.min(70, (x / total) * 100));
        container.style.gridTemplateColumns = `${leftPct}% 6px 1fr`;
        Blockly.svgResize(workspace);
      });
      document.addEventListener("mouseup", () => {
        if (!dragging) return;
        dragging = false; handle.classList.remove("dragging");
        document.body.style.cursor = ""; document.body.style.userSelect = "";
        Blockly.svgResize(workspace);
      });
    }

    try { await buildToolbox(); } catch (e) { setLogPlaceholder(`[Init Error] ${e}`); return; }

    // Check if NIMO has a candidates file loaded
    try {
      const st = await (await apiFetch("/nimo/status")).json();
      if (st.ok && !st.ready) {
        logWarn("NIMO", "No candidates file found. Please upload a candidates CSV file to get started.");
      }
    } catch {}

    await reattachIfRunning();
    if (pendingXml) { loadWorkspaceXml(pendingXml); pendingXml = null; }
    else { const saved = localStorage.getItem("nimo_workspace_xml_v1"); if (saved) loadWorkspaceXml(saved); }

    // Auto-save workspace on change (debounced to capture final state after deletions)
    let _saveTimer = null;
    workspace.addChangeListener((e) => {
      if (e.isUiEvent) return;
      if (_saveTimer) clearTimeout(_saveTimer);
      _saveTimer = setTimeout(() => {
        try { localStorage.setItem("nimo_workspace_xml_v1", exportWorkspaceXml()); } catch {}
      }, 300);
    });
  }

  window.addEventListener("DOMContentLoaded", main);
})();