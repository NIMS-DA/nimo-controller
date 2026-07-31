/**
 * NIMO Controller Frontend
 * Blockly workflow editor
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

  // The log follows the newest entry only while the reader is already there.
  // Forcing it down unconditionally makes the panel unreadable during a run:
  // scrolling back to check an earlier step gets undone by the next event.
  const LOG_STICK_SLACK = 40;   // close enough to the bottom to count as "at the bottom"

  /** True when the log is scrolled to the bottom — or too short to scroll. */
  function isLogAtBottom(el) {
    if (!el) return true;
    return el.scrollHeight - el.scrollTop - el.clientHeight <= LOG_STICK_SLACK;
  }

  /**
   * Follow the newest entry, but only if the reader had not scrolled away.
   *
   * Sample `isLogAtBottom` *before* appending: afterwards `scrollHeight` has
   * already grown by the new row, so the check would always say "no".
   */
  function stickLog(el, wasAtBottom) {
    if (el && wasAtBottom) el.scrollTop = el.scrollHeight;
    updateJumpLatest(el);
  }

  /**
   * Reveal "jump to latest" while the log is parked away from the bottom.
   *
   * Without it, scrolling up during a run looks like the run has stopped —
   * entries keep arriving with nothing on screen to say so.
   */
  function updateJumpLatest(el) {
    const log = el || logEl();
    const btn = $("logJumpLatest");
    if (!log || !btn) return;
    btn.hidden = isLogAtBottom(log);
  }

  function jumpToLatest() {
    const log = logEl();
    if (!log) return;
    log.scrollTop = log.scrollHeight;
    updateJumpLatest(log);
  }

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
    const stick = isLogAtBottom(el);
    clearLogPlaceholder();
    const row = document.createElement("div");
    row.className = `chat-row ${role}`;
    const box = document.createElement("div");
    box.className = className;
    box.textContent = String(text ?? "");
    row.appendChild(box);
    el.appendChild(row);
    stickLog(el, stick);
    return box;
  }

  const logSystem = (t) => appendRow("system", "system-log", String(t ?? "").replace(/\n{3,}/g, "\n\n"));

  /** Append an arbitrary element to the log as a chat row. */
  function appendChatEl(role, el) {
    const log = logEl();
    if (!log) return null;
    const stick = isLogAtBottom(log);
    clearLogPlaceholder();
    const row = document.createElement("div");
    row.className = `chat-row ${role}`;
    row.appendChild(el);
    log.appendChild(row);
    stickLog(log, stick);
    return el;
  }

  /**
   * Append a card-style log message to the log panel.
   * @param {"error"|"warn"|"info"|"success"|"repeat"|"if"} level
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
    const stick = isLogAtBottom(log);
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
    row.appendChild(card); log.appendChild(row); stickLog(log, stick);
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

  /** Format a time as HH:MM, for chat message corners. Defaults to now. */
  function clockTime(when) {
    const d = when == null ? new Date() : new Date(when * 1000);
    if (isNaN(d.getTime())) return "";
    return [d.getHours(), d.getMinutes()].map(n => String(n).padStart(2, "0")).join(":");
  }

  /**
   * Classify a backend text/if log and render it as a styled card.
   *
   * Returns whatever element it created, or null when the line is dropped, so
   * the caller can move it into the current workflow run's box.
   */
  function renderTextLog(payload) {
    const text = typeof payload === "string" ? payload : (payload?.text || "");
    if (!text) return null;

    // "▶ Repeat x5" — skip, rounds show the loop context
    if (text.startsWith("▶ Repeat")) return null;

    // "-- Round 1/5 --" — show as a loop card
    if (text.startsWith("-- Round")) {
      const label = text.replace(/^-+\s*|\s*-+$/g, "").trim();
      return logCard("repeat", "Repeat", label);
    }

    // "▶ If ..." — [If] i = 3 on header, then  3 < 5  true  on next line
    if (text.startsWith("▶ If")) {
      const v   = typeof payload === "object" ? payload : {};
      const name = v.counter_var, val = v.counter_value;
      const op = v.op, tgt = v.target;
      const res = v.result === true || v.result === "True";

      if (name != null && val != null) {
        return logCard("if", "If", null, {
          suffix: `${name} = ${val}`,
          code: `${val} ${op} ${tgt}`,
          resultBadge: res,
        });
      }
      return logCard("if", "If", null, { code: text.slice(2).trim() });
    }
    return logSystem(text);
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

  /**
   * Read an SSE response body, yielding {event, data} per frame.
   * EventSource cannot POST, so streams started with a POST are read here.
   */
  async function* readSseStream(res) {
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        let event = "message", data = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) continue;
        try { yield { event, data: JSON.parse(data) }; } catch {}
      }
    }
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

  // Block colors — Scratch-inspired hues, muted/desaturated for a calmer look
  // (hex; setColour/category colour accept hex).
  const COLOR_REPEAT  = "#66A84E";   // muted green — repeat block + Control category tab
  const COLOR_CONTROL = "#CBB061";   // muted gold — if block
  const COLOR_VARS    = "#CE8F5A";   // muted orange — loop counter ref, nimo vars
  const COLOR_NIMO    = "#5E86C3";   // muted blue — nimo blocks + nimo category
  const COLOR_VALUE   = "#6EAE6E";   // muted green — number / text / boolean inputs
  const COLOR_TOOL_BADGE = "#9585C2"; // muted purple — log-card badge for non-nimo tools
  // Muted amber — log-card badge for the agent's own tools, which run no
  // instrument and belong to no server. Kept well away from the nimo blue and
  // the MCP purple so a planning step never reads as a nimo call.
  const COLOR_AGENT_BADGE = "#C08552";
  // Distinct, muted colors for MCP servers (ordered to stay apart from the fixed category colors).
  const SCRATCH_SERVER_COLORS = ["#9585C2", "#BE7DBE", "#5FA6A0", "#CE8496", "#8E9ABF", "#B98F63", "#6EAE6E"];
  const NIMO_SEL_TYPE = "nimo_selection", NIMO_UPD_TYPE = "nimo_update";
  // Direction-aware counterparts of selection; same shape, different nimo tool.
  const NIMO_MAX_TYPE = "nimo_maximization", NIMO_MIN_TYPE = "nimo_minimization";
  // Used only when /tools cannot be read — keep in step with the enums in
  // nimo_tools.py (SelectionMethod / OptimizationMethod).
  const FALLBACK_SELECTION_METHODS = ["RE", "ES", "DOE", "BLOX", "PDC"];
  const FALLBACK_OPTIMIZATION_METHODS = ["PHYSBO", "NTS"];
  // Block type -> the nimo tool it calls. One entry per method block.
  const NIMO_METHOD_TOOLS = new Map([
    [NIMO_SEL_TYPE, "selection"],
    [NIMO_MAX_TYPE, "maximization"],
    [NIMO_MIN_TYPE, "minimization"],
  ]);

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
    this.setColour(COLOR_CONTROL);
    this.setPreviousStatement(true); this.setNextStatement(true);
    this.setTooltip("Execute 'then' or 'else' blocks based on a counter comparison.");
  }};

  Blockly.Blocks[NIMO_UPD_TYPE] = {
    init() {
      this.appendDummyInput().appendField("update");
      this.appendStatementInput("BODY").setCheck(null);
      this.setPreviousStatement(true); this.setNextStatement(true); this.setColour(COLOR_NIMO);
      this.setTooltip("Put a single tool that returns a float/int inside; its result is sent to NIMO update.");
    },
    // Enforce a single block inside BODY: detach anything chained after the first.
    onchange() {
      if (!this.workspace || this.workspace.isDragging?.()) return;
      const first = this.getInputTargetBlock("BODY");
      const extra = first && first.getNextBlock();
      if (extra) extra.unplug(false);
    },
  };

  /**
   * Define one "<label> method <dropdown>" nimo block.
   *
   * Redefined on every toolbox build so the options track the enum the server
   * actually reports — hence a factory rather than a static Blockly.Blocks entry.
   */
  function defineNimoMethodBlock(type, label, methods, fallback) {
    const list = Array.isArray(methods) && methods.length ? methods : fallback;
    const opts = list.map((v) => [String(v), String(v)]);
    Blockly.Blocks[type] = { init() {
      this.appendDummyInput().appendField(label).appendField("method").appendField(new Blockly.FieldDropdown(opts), "METHOD");
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
    core.setAttribute("name", "Control"); core.setAttribute("colour", COLOR_REPEAT);
    for (const t of ["repeat_n_with_index", "loop_counter_ref", "if_counter"]) {
      const b = document.createElement("block"); b.setAttribute("type", t); core.appendChild(b);
    }
    xml.appendChild(core);
    const nimo = document.createElement("category");
    nimo.setAttribute("name", "nimo"); nimo.setAttribute("colour", COLOR_NIMO);
    xml.appendChild(nimo);
    return xml;
  }

  // Scratch-ish theme: recolor the stock value blocks (number / text / boolean)
  // to Scratch's Operators green. Custom blocks use setColour(hex) directly, so
  // the theme only affects these built-in blocks. Falls back to no theme if the
  // Blockly build doesn't expose the theme API.
  let _scratchTheme;
  try {
    _scratchTheme = Blockly.Theme.defineTheme("scratchish", {
      base: Blockly.Themes.Classic,
      blockStyles: {
        math_blocks:  { colourPrimary: COLOR_VALUE },
        text_blocks:  { colourPrimary: COLOR_VALUE },
        logic_blocks: { colourPrimary: COLOR_VALUE },
      },
    });
  } catch (e) { _scratchTheme = undefined; }

  const workspace = Blockly.inject("workspace",
    _scratchTheme ? { toolbox: makeInitialToolbox(), theme: _scratchTheme }
                  : { toolbox: makeInitialToolbox() });

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
  // Toolbox build (from /tools + /nimo/parameters)
  // =========================================================================
  const blockTypeInfo = new Map();   // stmtType → {serverId, toolName}
  const toolKeyToSchema = new Map(); // "sid::name" → JSON schema
  const numericToolTypes = new Set(); // stmtTypes whose tool returns a single float/int

  /** True if an MCP tool's outputSchema denotes a single numeric scalar (float/int, not bool). */
  function toolReturnsNumber(outputSchema) {
    const s = outputSchema;
    if (!s || typeof s !== "object") return false;
    if (s.type === "number" || s.type === "integer") return true;
    // FastMCP wraps bare-scalar returns: {type:"object", properties:{result:{type}}, x-fastmcp-wrap-result:true}
    if (s.type === "object" && s["x-fastmcp-wrap-result"] === true) {
      const rt = s.properties?.result?.type;
      return rt === "number" || rt === "integer";
    }
    return false;
  }

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

  /**
   * Read the allowed values of a nimo tool's "method" argument.
   * FastMCP renders Python enums as a $ref into $defs, so that path is the one
   * that actually fires; the inline enum case is kept for other shapes.
   */
  function extractMethodEnum(tools, toolName, fallback) {
    const t = tools.find((x) => x.server_id === "nimo" && x.name === toolName);
    if (!t) return fallback;
    const m = (t.input_schema?.properties?.method) || {};
    if (Array.isArray(m.enum) && m.enum.length) return m.enum.map(String);
    const ref = m.$ref;
    if (typeof ref === "string") {
      const d = (t.input_schema.$defs || {})[ref.split("/").pop()];
      if (d?.enum?.length) return d.enum.map(String);
    }
    return fallback;
  }

  /** Fetch tools from backend and rebuild the Blockly toolbox. */
  async function buildToolbox() {
    const tools = await (await apiFetch("/tools")).json();
    const optMethods = extractMethodEnum(tools, "maximization", FALLBACK_OPTIMIZATION_METHODS);
    defineNimoMethodBlock(NIMO_SEL_TYPE, "selection",
      extractMethodEnum(tools, "selection", FALLBACK_SELECTION_METHODS), FALLBACK_SELECTION_METHODS);
    defineNimoMethodBlock(NIMO_MAX_TYPE, "maximization", optMethods, FALLBACK_OPTIMIZATION_METHODS);
    defineNimoMethodBlock(NIMO_MIN_TYPE, "minimization", optMethods, FALLBACK_OPTIMIZATION_METHODS);

    const xml = document.createElement("xml");

    // Control category
    const core = document.createElement("category"); core.setAttribute("name", "Control"); core.setAttribute("colour", COLOR_REPEAT);
    for (const t of ["repeat_n_with_index", "loop_counter_ref", "if_counter"]) {
      const b = document.createElement("block"); b.setAttribute("type", t); core.appendChild(b);
    }
    xml.appendChild(core);

    // NIMO category — variable blocks first, then the method blocks and update.
    const nimo = document.createElement("category"); nimo.setAttribute("name", "nimo"); nimo.setAttribute("colour", COLOR_NIMO);
    try {
      const pRes = await (await apiFetch("/nimo/parameters")).json();
      if (pRes.ok) for (const p of pRes.parameters) { const b = document.createElement("block"); b.setAttribute("type", defineNimoVarBlock(p)); nimo.appendChild(b); }
    } catch (e) { logWarn("NIMO", `Parameters: ${e}`); }
    for (const t of [NIMO_SEL_TYPE, NIMO_MAX_TYPE, NIMO_MIN_TYPE, NIMO_UPD_TYPE]) { const b = document.createElement("block"); b.setAttribute("type", t); nimo.appendChild(b); }
    xml.appendChild(nimo);

    // Server categories (including non-hardcoded nimo tools)
    const NIMO_HARDCODED = new Set(["selection", "maximization", "minimization", "update",
                                    "get_parameter_names", "get_proposal", "reinitialize"]);

    // Give each non-nimo MCP server a distinct Scratch colour (cycling the palette)
    // so different servers never look similar — used for the category tab and the
    // tool blocks themselves.
    const serverIds = [];
    for (const tool of tools) { const sid = tool.server_id; if (sid !== "nimo" && !serverIds.includes(sid)) serverIds.push(sid); }
    const serverColor = new Map();
    serverIds.forEach((sid, i) => serverColor.set(sid, SCRATCH_SERVER_COLORS[i % SCRATCH_SERVER_COLORS.length]));

    const cats = new Map();
    for (const tool of tools) {
      const sid = tool.server_id;
      if (sid === "nimo" && NIMO_HARDCODED.has(tool.name)) continue;
      const cat = sid === "nimo" ? nimo : (() => {
        if (!cats.has(sid)) { const c = document.createElement("category"); c.setAttribute("name", sid); c.setAttribute("colour", serverColor.get(sid)); cats.set(sid, c); xml.appendChild(c); }
        return cats.get(sid);
      })();
      const schema = tool.input_schema || {};
      toolKeyToSchema.set(`${sid}::${tool.name}`, schema);
      const type = toolStmtType(sid, tool.name);
      blockTypeInfo.set(type, { serverId: sid, toolName: tool.name });
      if (toolReturnsNumber(tool.output_schema)) numericToolTypes.add(type);
      else numericToolTypes.delete(type);   // clean up on rebuild
      Blockly.Blocks[type] = { init() {
        this.appendDummyInput().appendField(tool.name);
        for (const [k, ps] of Object.entries(schema?.properties || {})) buildInputForProp(this, k, ps);
        this.setColour(sid === "nimo" ? COLOR_NIMO : serverColor.get(sid)); this.setPreviousStatement(true); this.setNextStatement(true); this.setTooltip(tool.description || "");
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
      } else if (NIMO_METHOD_TOOLS.has(block.type)) {
        // All three method blocks share one shape; only the nimo tool differs.
        out.push({ kind: "tool", server_id: "nimo", tool: NIMO_METHOD_TOOLS.get(block.type),
                   args: { method: String(block.getFieldValue("METHOD")) }, block_id: block.id });
      } else if (block.type === NIMO_UPD_TYPE) {
        const inner = block.getInputTargetBlock("BODY");
        if (!inner) throw new Error("Put one tool that returns a float or int inside the update block.");
        if (inner.getNextBlock()) throw new Error("An update block holds only one block.");
        if (!numericToolTypes.has(inner.type)) {
          const info = blockTypeInfo.get(inner.type);
          const label = info ? info.toolName : inner.type;
          throw new Error(`"${label}" inside the update block does not return a float or int. Use a tool that returns a number.`);
        }
        out.push({ kind: "update", body: exportChain(inner), block_id: block.id });
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
      } else if (node.kind === "update") {
        validateCounterScopes(node.body, scope);
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
  // Tool card UI (workflow execution logs)
  // =========================================================================

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
  function appendToolCard({ server, tool, args, when }) {
    const log = logEl();
    if (!log) return null;
    const stick = isLogAtBottom(log);
    clearLogPlaceholder();
    const row = document.createElement("div"); row.className = "chat-row system";
    const card = document.createElement("div"); card.className = "tool-card";
    // Header — tool name badged in the block colour, with the server it came
    // from and the time paired off in the corner.
    const head = document.createElement("div"); head.className = "tool-head";
    const sid = server || "";
    const badge = document.createElement("span"); badge.className = "tool-badge";
    badge.textContent = tool || "(tool)";
    if (sid === "nimo") badge.style.background = COLOR_NIMO;
    else if (sid) badge.style.background = COLOR_TOOL_BADGE;
    // No server at all: one of the agent's own tools, such as plan_workflow.
    // Without this it keeps the stylesheet's default blue, which reads as nimo.
    else badge.style.background = COLOR_AGENT_BADGE;
    const meta = document.createElement("span"); meta.className = "tool-meta";
    if (sid) {
      const origin = document.createElement("span");
      origin.className = "tool-server";
      origin.textContent = sid;
      meta.appendChild(origin);
    }
    const time = document.createElement("span");
    time.className = "tool-time";
    time.textContent = clockTime(when);
    meta.appendChild(time);
    head.append(badge, meta);
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
    row.appendChild(card); log.appendChild(row); stickLog(log, stick);
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
      // Appended last so it sits under the output (and its images, which belong
      // with it) rather than splitting the two apart.
      for (const note of output.notes || []) {
        const el = document.createElement("div");
        el.className = "tool-note";
        el.textContent = note;
        card.appendChild(el);
      }
    }
    outBox.innerHTML = "";
    outBox.appendChild(renderValue(display));
    if (Array.isArray(imgs) && imgs.length) renderCardImages(card, imgs);
  }

  function renderCardImages(card, images) {
    const box = card.querySelector('[data-role="images"]');
    if (!box) return;
    const log = logEl();
    const stick = isLogAtBottom(log);
    box.innerHTML = "";
    for (const img of images) {
      if (!img?.data) continue;
      const el = document.createElement("img"); el.className = "tool-image";
      el.src = `data:${img.mimeType || "image/png"};base64,${img.data}`;
      el.alt = "Tool output";
      el.addEventListener("click", () => openLightbox(el.src));
      box.appendChild(el);
    }
    if (box.children.length) stickLog(log, stick);
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
  // Workflow execution + SSE
  // =========================================================================
  // startCard is the "Started at …" card, kept so the workflow XML can be
  // folded into it when the server sends it a moment later.
  // runGroup is the collapsible box holding one run's whole output — a ten-cycle
  // loop otherwise leaves forty-odd cards lying loose in the log.
  const execState = {
    running: false, workflowId: null, es: null, lastCard: null, startCard: null,
    runGroup: null, runSteps: 0, runStartedAt: 0,
  };

  function setRunningUI(on) {
    execState.running = on;
    const btn = $("runBtn"); if (!btn) return;
    btn.classList.toggle("btn-danger", on); btn.classList.toggle("btn-primary", !on);
    btn.textContent = on ? "■ Cancel" : "▶ Run";
    // Lock / unlock the Blockly workspace during execution
    const wsEl = $("workspace");
    if (wsEl) wsEl.classList.toggle("workspace-locked", on);
  }
  function closeSSE() { if (execState.es) { try { execState.es.close(); } catch {} execState.es = null; } }

  /**
   * Open a collapsible box for one workflow run and return a handle to it.
   *
   * Called from connectSSE, which is the one path every run goes through:
   * runWorkflow for a run this page started, and reattachIfRunning for one it
   * found already going after a reload. The second arrives with no start card
   * to hang the box on, so the box cannot be built from the start card.
   */
  function makeRunGroup() {
    const box = document.createElement("div");
    box.className = "run-group";

    const head = document.createElement("button");
    head.type = "button";
    head.className = "run-group__head";
    const caret = document.createElement("span");
    caret.className = "run-group__caret";
    const label = document.createElement("span");
    label.className = "run-group__label";
    head.append(caret, label);

    // A rail down the left edge, the full height of the body, so the run can be
    // folded from wherever the reader happens to be inside it. The header
    // toggle can only be reached by scrolling back to the top, which is most
    // work exactly when the run is long enough to be worth folding.
    const main = document.createElement("div");
    main.className = "run-group__main";
    const rail = document.createElement("button");
    rail.type = "button";
    rail.className = "run-group__rail";
    rail.title = "Collapse this run";
    rail.setAttribute("aria-label", "Collapse this run");

    const body = document.createElement("div");
    body.className = "run-group__body";
    main.append(rail, body);
    box.append(head, main);

    const setCollapsed = (on) => {
      box.classList.toggle("run-group--collapsed", on);
      head.setAttribute("aria-expanded", String(!on));
    };
    head.addEventListener("click", () =>
      setCollapsed(!box.classList.contains("run-group--collapsed")));
    // The rail only ever folds: once folded it is hidden with the body, so the
    // header caret is what brings it back.
    rail.addEventListener("click", () => setCollapsed(true));
    setCollapsed(false);

    appendChatEl("system", box);
    return { box, head, body, label, setCollapsed };
  }

  /** Move a card the log helpers just created into the current run's box. */
  function adoptIntoRun(el) {
    const row = el?.parentElement;          // logCard/appendToolCard return the card
    const group = execState.runGroup;
    if (row && group && row.parentElement !== group.body) group.body.appendChild(row);
    return el;
  }

  /** Rewrite the run box's one-line summary. */
  function setRunGroupLabel(text) {
    if (execState.runGroup) execState.runGroup.label.textContent = text;
  }

  /** Show a workflow-finished info card in the log panel. */
  function logWorkflowFinished(status) {
    const labels = { done: "Completed", error: "Failed", canceled: "Canceled" };
    const levels = { done: "info", error: "error", canceled: "warn" };
    return logCard(levels[status] || "info", "Workflow",
                   `${labels[status] || status} at ${timeStamp()}`);
  }

  /** Fold an XML blob into *card* behind a show/hide button. */
  function attachXmlFold(card, xml) {
    if (!card || !xml) return card;
    const pre = document.createElement("pre");
    pre.className = "log-card__xml";
    pre.textContent = xml;
    pre.hidden = true;   // folded away by default — it is long
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "log-card__download";
    btn.textContent = "Show XML";
    btn.addEventListener("click", () => {
      pre.hidden = !pre.hidden;
      btn.textContent = pre.hidden ? "Show XML" : "Hide XML";
    });
    card.append(btn, pre);
    return card;
  }

  /**
   * Fold the run's NIMO workflow XML into the "Started at …" card behind a
   * show/hide button. Falls back to its own card when there is no start card —
   * after a reload the replayed event arrives with nothing to attach to.
   */
  function logWorkflowXml(xml) {
    const card = execState.startCard || logCard("info", "Workflow", "NIMO workflow XML");
    return attachXmlFold(card, xml);
  }

  /**
   * Ask the server for the NIMO XML of whatever is currently in the workspace.
   *
   * The page owns the AST — the workspace is the executable form — so a preview
   * has to be rendered from here rather than at the moment the agent designed
   * it. Best effort: an incomplete workspace makes exportWorkflowAst throw, and
   * a preview is not worth an error card, so a failure just means no fold.
   */
  async function fetchWorkflowXml() {
    try {
      const ast = exportWorkflowAst();
      const res = await apiFetch("/workflow/xml", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow: ast }),
      });
      const data = await res.json();
      return data.ok ? data.xml : null;
    } catch { return null; }
  }

  function connectSSE(wfId) {
    closeSSE();
    const es = new EventSource(`/workflow/${wfId}/events`);
    execState.es = es;

    // One box per run, and the next run gets a new one rather than reusing
    // this. The old box stays in the log with its own reference intact, so an
    // event that arrives late still lands inside the run it belongs to.
    execState.runGroup = makeRunGroup();
    execState.runSteps = 0;
    execState.runStartedAt = Date.now();
    setRunGroupLabel("Workflow run — running…");
    // The "Started at …" card is made by runWorkflow before this point; on the
    // reattach path there is none.
    adoptIntoRun(execState.startCard);

    const finish = (status) => {
      setRunningUI(false);
      execState.startCard = null;   // the next run gets its own
      localStorage.removeItem("workflow_id"); execState.workflowId = null;
      try { workspace.highlightBlock(null); } catch {}
      closeSSE();
      adoptIntoRun(logWorkflowFinished(status));

      const labels = { done: "done", error: "failed", canceled: "canceled" };
      const secs = ((Date.now() - execState.runStartedAt) / 1000).toFixed(1);
      setRunGroupLabel(`Workflow run — ${labels[status] || status}, `
                       + `${execState.runSteps} steps, ${secs} s`);
      // A failed run stays open: folding the error away would hide the one
      // thing worth reading.
      if (status === "done" && execState.runGroup) {
        execState.runGroup.setCollapsed(true);
        // Collapsing takes a lot of height out of the log at once; land the
        // view back at the bottom rather than wherever that left it.
        jumpToLatest();
      }
      // In Auto mode the turn is still open, blocked in plan_workflow, and this
      // is the moment it resumes. Fired for every ending — a failed or canceled
      // run is reported too, not only a clean one.
      if (agentState.streaming) agentState.onRunFinished?.();
    };
    es.addEventListener("log", (e) => {
      try {
        const p = JSON.parse(e.data);
        if (p.kind === "tool_call") {
          execState.lastCard = adoptIntoRun(appendToolCard({ server: p.server_id ?? "", tool: p.tool ?? "", args: p.args ?? {} }));
          execState.runSteps += 1;
          setRunGroupLabel(`Workflow run — running… ${execState.runSteps} steps`);
        }
        else if (p.kind === "tool_output") { if (execState.lastCard) updateCardOutput(execState.lastCard, p.output); else updateCardOutput(adoptIntoRun(appendToolCard({ server: "", tool: "", args: {} })), p.output); }
        else if (p.kind === "workflow_xml" && p.xml) adoptIntoRun(logWorkflowXml(p.xml));
        else if (p.kind === "text" && p.text) adoptIntoRun(renderTextLog(p));
        else adoptIntoRun(logSystem(safeJson(p)));
      } catch { adoptIntoRun(logSystem(e.data)); }
    });
    es.addEventListener("active", (e) => { try { const p = JSON.parse(e.data); if (p?.block_id) workspace.highlightBlock(p.block_id); } catch {} });
    es.addEventListener("status", (e) => { let s = ""; try { s = JSON.parse(e.data)?.status || ""; } catch {} if (s === "done") finish("done"); if (s === "error") finish("error"); if (s === "canceled") finish("canceled"); });
    // Server-sent "event: error" with error details
    es.addEventListener("error", (e) => {
      if (e.data) { try { const p = JSON.parse(e.data); if (p?.error) adoptIntoRun(logError("Workflow", p.error)); } catch {} }
    });
    // Connection blips are left silent: EventSource retries on its own, and it
    // fires on every attempt — a card per retry would bury the actual log.
  }

  async function runWorkflow() {
    if (execState.running) return;
    // The log is not wiped here: runs accumulate within a session, and only
    // "New session" starts a clean one. lastCard still resets so a stray
    // tool_output cannot land on the previous run's card.
    // No status-line spinner: the Run button turning into "■ Cancel" already
    // says a run is in flight, and the log tail is for log content.
    execState.lastCard = null; setRunningUI(true);
    execState.startCard = logInfo("Workflow", `Started at ${timeStamp()}`);
    let ast;
    try { ast = exportWorkflowAst(); } catch (e) { setRunningUI(false); logError("Error", String(e)); return; }
    console.log("[NIMO] Exported AST:", JSON.stringify(ast, null, 2));
    try {
      const res = await apiFetch("/workflow/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workflow: ast, workspace_xml: exportWorkspaceXml() }) });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error);
      execState.workflowId = data.workflow_id; localStorage.setItem("workflow_id", data.workflow_id);
      connectSSE(data.workflow_id);
    } catch (e) { setRunningUI(false); logError("Error", String(e)); }
  }

  async function cancelWorkflow() {
    const id = execState.workflowId || localStorage.getItem("workflow_id"); if (!id) return;
    logInfo("Cancel", "Cancellation requested");
    try { await apiFetch(`/workflow/${id}/cancel`, { method: "POST" }); } catch (e) { logError("Cancel", String(e)); }
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
        <div id="settingsServerList" class="settings-server-list"><div class="settings-loading">Loading...</div></div>
        <div class="settings-add-form">
          <input id="settingsServerName" type="text" placeholder="Name" class="settings-input" />
          <div class="settings-url-wrap">
            <input id="settingsServerUrl" type="text" autocomplete="off" placeholder="http://127.0.0.1:8000/mcp" class="settings-input settings-input-wide" />
            <div id="settingsUrlSuggest" class="url-suggest" hidden></div>
          </div>
          <button id="settingsAddBtn" class="btn btn-primary">Add</button>
        </div>
        <div id="settingsError" class="settings-error"></div>
      </div>
    </div>`;
    $("settingsAddBtn")?.addEventListener("click", handleAddServer);
    const urlIn = $("settingsServerUrl");
    urlIn?.addEventListener("focus", () => openUrlSuggest(urlIn.value));
    urlIn?.addEventListener("input", () => openUrlSuggest(urlIn.value));
    urlIn?.addEventListener("blur", () => setTimeout(closeUrlSuggest, 150));
    urlIn?.addEventListener("keydown", (e) => {
      const open = _urlAC.items.length > 0;
      if (open && (e.key === "Tab" || e.key === "Enter")) { e.preventDefault(); acceptUrlSuggest(); return; }
      if (open && e.key === "ArrowDown") { e.preventDefault(); moveUrlSuggest(1); return; }
      if (open && e.key === "ArrowUp") { e.preventDefault(); moveUrlSuggest(-1); return; }
      if (open && e.key === "Escape") { e.preventDefault(); closeUrlSuggest(); return; }
      if (!open && e.key === "Enter") handleAddServer();
    });
  }

  // =========================================================================
  // MCP server URL typeahead (no value prefill; placeholder + suggestions + Tab)
  // =========================================================================
  const _urlAC = { items: [], active: -1, servers: [] };

  /** Host to feature first — reuse the most recently added server's host. */
  function preferredHost(servers) {
    const withUrl = (servers || []).filter((s) => s.url);
    if (withUrl.length) { try { const h = new URL(withUrl[withUrl.length - 1].url).hostname; if (h) return h; } catch {} }
    return "127.0.0.1";
  }

  /** Hosts to build suggestions from: existing servers first, then 127.0.0.1. */
  function knownHosts(servers) {
    const hosts = [];
    for (const s of (servers || [])) {
      if (s.url) { try { const h = new URL(s.url).hostname; if (h && !hosts.includes(h)) hosts.push(h); } catch {} }
    }
    if (!hosts.includes("127.0.0.1")) hosts.push("127.0.0.1");
    return hosts;
  }

  /** Set the URL field's placeholder to a concrete host-aware example (no value). */
  function updateUrlPlaceholder(servers) {
    const inp = $("settingsServerUrl"); if (!inp) return;
    inp.placeholder = `http://${preferredHost(servers)}:8000/mcp`;
  }

  /** Build up to 6 full-URL suggestions for the current input text. */
  function computeUrlSuggestions(text, servers) {
    const hosts = knownHosts(servers);
    const t = (text || "").trim();
    let out;
    if (/^\d+$/.test(t)) {
      out = hosts.map((h) => `http://${h}:${t}/mcp`);          // typed a bare port
    } else if (!t) {
      out = hosts.map((h) => `http://${h}:8000/mcp`);          // empty → defaults
    } else {
      const low = t.toLowerCase();
      out = hosts.map((h) => `http://${h}:8000/mcp`).filter((u) => u.toLowerCase().startsWith(low));
      if (!out.length && /^https?:\/\//i.test(t)) {            // partial full URL → complete /mcp
        out = [/\/mcp$/.test(t) ? t : t.replace(/\/+$/, "") + "/mcp"];
      }
    }
    return [...new Set(out)].slice(0, 6);
  }

  function renderUrlSuggest() {
    const box = $("settingsUrlSuggest"); if (!box) return;
    if (!_urlAC.items.length) { box.hidden = true; box.innerHTML = ""; return; }
    box.innerHTML = "";
    _urlAC.items.forEach((u, i) => {
      const it = document.createElement("div");
      it.className = "url-suggest__item" + (i === _urlAC.active ? " active" : "");
      it.textContent = u;
      // mousedown (not click) so it fires before the input's blur.
      it.addEventListener("mousedown", (e) => { e.preventDefault(); acceptUrlSuggest(i); });
      box.appendChild(it);
    });
    box.hidden = false;
  }

  function openUrlSuggest(text) {
    _urlAC.items = computeUrlSuggestions(text, _urlAC.servers);
    _urlAC.active = _urlAC.items.length ? 0 : -1;
    renderUrlSuggest();
  }
  function closeUrlSuggest() {
    _urlAC.items = []; _urlAC.active = -1;
    const box = $("settingsUrlSuggest"); if (box) { box.hidden = true; box.innerHTML = ""; }
  }
  function moveUrlSuggest(delta) {
    if (!_urlAC.items.length) return;
    _urlAC.active = (_urlAC.active + delta + _urlAC.items.length) % _urlAC.items.length;
    renderUrlSuggest();
  }
  function acceptUrlSuggest(idx) {
    const inp = $("settingsServerUrl"); if (!inp) return;
    const i = (idx == null) ? _urlAC.active : idx;
    const val = _urlAC.items[i]; if (!val) return;
    inp.value = val; closeUrlSuggest(); inp.focus();
    // If the port is the default 8000 (user didn't specify one), select it for quick overtype.
    const m = val.match(/^https?:\/\/[^:/]+:(\d+)\/mcp$/);
    if (m && m[1] === "8000") {
      const start = val.lastIndexOf(":8000/") + 1;
      try { inp.setSelectionRange(start, start + 4); } catch {}
    }
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

        // Reconnect (🔄): re-attach to the server using its stored URL,
        // e.g. after the server has been restarted. Dynamic servers only.
        if (!srv.builtin && srv.reconnectable !== false) {
          const rc = document.createElement("button");
          rc.className = "btn btn-ghost settings-emoji-btn settings-reconnect-btn";
          rc.textContent = "🔄";
          rc.title = "Reconnect";
          rc.style.marginRight = "2px";
          rc.addEventListener("click", () => handleReconnectServer(srv.name, rc));
          row.appendChild(rc);
        }

        if (srv.builtin) { const b = document.createElement("span"); b.className = "settings-badge-builtin"; b.textContent = "built-in"; row.appendChild(b); }
        else { const d = document.createElement("button"); d.className = "btn btn-ghost settings-emoji-btn settings-delete-btn"; d.textContent = "🗑️"; d.title = "Remove"; d.addEventListener("click", () => handleRemoveServer(srv.name)); row.appendChild(d); }
        list.appendChild(row);
      }
      if (!data.servers.length) list.innerHTML = '<div class="settings-empty">No servers</div>';
      _urlAC.servers = data.servers;
      updateUrlPlaceholder(data.servers);
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
      await refreshServerList(); try { await buildToolbox(); } catch {} logOk("Settings", `Server "${name}" added.`);
    } catch (e) { if (err) err.textContent = String(e); }
    finally { if (btn) btn.disabled = false; }
  }

  async function handleRemoveServer(name) {
    if (!confirm(`Remove "${name}"?`)) return;
    try { const d = await apiRemoveServer(name); if (!d.ok) throw new Error(d.error); await refreshServerList(); try { await buildToolbox(); } catch {} logOk("Settings", `Server "${name}" removed.`); }
    catch (e) { const err = $("settingsError"); if (err) err.textContent = String(e); }
  }

  async function handleReconnectServer(name, btn) {
    const err = $("settingsError"); if (err) err.textContent = "";
    const orig = btn ? btn.textContent : "";
    if (btn) { btn.disabled = true; btn.textContent = "…"; }
    try {
      const d = await apiReconnectServer(name); if (!d.ok) throw new Error(d.error);
      // refreshServerList() re-renders the row, so no need to restore btn on success.
      await refreshServerList(); try { await buildToolbox(); } catch {}
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
  // Candidates file overlay — shows where candidates.csv is stored + upload
  // =========================================================================
  let _cfgCache = null;
  async function getConfig() {
    if (_cfgCache) return _cfgCache;
    try { _cfgCache = await (await apiFetch("/config")).json(); } catch { _cfgCache = null; }
    return _cfgCache;
  }

  function openCandidates() {
    const ov = $("candidatesOverlay");
    if (ov) { ov.classList.add("active"); renderCandidatesPanel(); }
  }
  function closeCandidates() {
    const ov = $("candidatesOverlay");
    if (ov) ov.classList.remove("active");
  }

  /** Render CSV text into a scrollable table inside *container*. */
  function renderCsvPreview(container, text) {
    container.innerHTML = "";
    const lines = String(text).replace(/\r\n?/g, "\n").split("\n").filter((l) => l.length);
    if (!lines.length) { container.textContent = "(empty file)"; return; }
    const MAX = 200;
    const table = document.createElement("table");
    table.className = "candidates-table";
    lines.slice(0, MAX).forEach((line, i) => {
      const tr = document.createElement("tr");
      for (const cell of line.split(",")) {
        const c = document.createElement(i === 0 ? "th" : "td");
        c.textContent = cell;
        tr.appendChild(c);
      }
      table.appendChild(tr);
    });
    container.appendChild(table);
    if (lines.length > MAX) {
      const note = document.createElement("div");
      note.className = "candidates-hint";
      note.textContent = `… showing first ${MAX} of ${lines.length} rows`;
      container.appendChild(note);
    }
  }

  async function renderCandidatesPanel() {
    const panel = $("candidatesPanel"); if (!panel) return;
    panel.innerHTML = `<div class="settings-body">
      <div class="settings-section">
        <button id="candidatesChooseBtn" class="btn btn-primary">Choose CSV file…</button>
      </div>
      <div class="settings-section" id="candPreviewSection">
        <div class="settings-section-title">Current candidates.csv</div>
        <div class="candidates-folder">
          <div class="candidates-dir">
            <span class="candidates-dir-label">Folder:</span>
            <span id="candDir" class="candidates-dir-link" role="button" tabindex="0" title="Open in file manager">…</span>
          </div>
          <div class="candidates-hint">Change this location with <code>data_dir</code> in <code>config.yaml</code>.</div>
        </div>
        <div id="candBody"><div class="candidates-status">Checking…</div></div>
      </div>
    </div>`;
    $("candidatesChooseBtn")?.addEventListener("click", () => $("csvFileInput")?.click());
    const openDataDir = async () => {
      try {
        const r = await (await apiFetch("/nimo/open-data-dir", { method: "POST" })).json();
        if (!r.ok) throw new Error(r.error);
      } catch (e) { logError("Open", String(e)); }
    };
    $("candDir")?.addEventListener("click", openDataDir);
    $("candDir")?.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDataDir(); } });

    // Folder path (injection-safe).
    const cfg = await getConfig();
    const dir = $("candDir");
    if (dir) dir.textContent = cfg?.data_dir || "(unknown)";

    // Preview (below the folder note) of the current candidates.csv when loaded.
    const body = $("candBody");
    if (!body) return;
    try {
      const r = await (await apiFetch("/nimo/candidates")).json();
      if (r.ok && r.exists && r.content) {
        body.innerHTML = `<div class="candidates-subtitle">Preview</div>
          <div class="candidates-preview" id="candPreview"></div>`;
        renderCsvPreview($("candPreview"), r.content);
      } else {
        body.innerHTML = `<div class="candidates-status">No candidates file yet — choose a CSV above to get started.</div>`;
      }
    } catch {
      body.innerHTML = `<div class="candidates-status">No candidates file yet — choose a CSV above to get started.</div>`;
    }
  }

  // =========================================================================
  // Agent prompt — shown only when enable_agent is set in config.yaml
  // =========================================================================
  const agentState = {
    enabled: false, streaming: false, modelListOk: false,
    // Set by a turn that started a workflow, called once when that run ends.
    onRunFinished: null,
  };

  async function initAgentPrompt() {
    const cfg = await getConfig();
    if (!cfg?.enable_agent) return;
    const box = $("agentPrompt");
    const input = $("agentPromptInput");
    if (!box || !input) return;
    agentState.enabled = true;
    box.hidden = false;

    await loadAgentModels();
    await restoreAgentHistory();
    $("agentModelSelect")?.addEventListener("change", () => applyAgentSettings());
    $("agentLevelSelect")?.addEventListener("change", () => applyAgentSettings());

    const send = () => {
      const text = input.value.trim();
      if (!text || agentState.streaming) return;
      input.value = "";
      input.style.height = "";
      sendAgentMessage(text);
    };

    // Mid-turn the button means "stop"; Enter deliberately does not, so a
    // stray keypress cannot kill a run in progress.
    $("agentSendBtn")?.addEventListener("click", () => {
      if (agentState.streaming) cancelAgent(); else send();
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
    });
    // Auto-grow up to the max-height set in CSS.
    input.addEventListener("input", () => {
      input.style.height = "auto";
      input.style.height = `${input.scrollHeight}px`;
    });
  }

  // -- model / reasoning pickers -------------------------------------------
  // The reasoning select offers effort levels only, with "off" as the lowest
  // rung: "off" maps to ModelSettings.thinking = false, anything else to the
  // level string itself.
  const DEFAULT_THINKING = "medium";

  /** Read the reasoning select as a `thinking` value for the API. */
  function readThinking() {
    const level = $("agentLevelSelect")?.value || DEFAULT_THINKING;
    return level === "off" ? false : level;
  }

  /** Set the reasoning select from a `thinking` value returned by the API. */
  function writeThinking(value) {
    const level = $("agentLevelSelect");
    if (!level) return;
    if (value === false) level.value = "off";
    else if (typeof value === "string" && value) level.value = value;
    // null (nothing chosen yet) and true have no rung of their own.
    else level.value = DEFAULT_THINKING;
  }

  /** Populate the pickers from the agent's current settings. */
  async function loadAgentModels() {
    const label = $("agentProviderLabel"), select = $("agentModelSelect");
    try {
      const d = await (await apiFetch("/agent/models")).json();
      if (!d.ok) throw new Error(d.error);
      if (label) label.textContent = d.provider || "";
      if (select) {
        // Keep the active model selectable even if the endpoint no longer
        // lists it, so the current setting is never silently changed.
        const names = d.models.includes(d.model) || !d.model
          ? d.models : [d.model, ...d.models];
        select.innerHTML = "";
        for (const name of names) {
          const opt = document.createElement("option");
          opt.value = name;
          opt.textContent = name;
          select.appendChild(opt);
        }
        // Nothing picked yet (a fresh server): fall back to the first model so
        // the box is usable straight away, and adopt it below.
        select.value = d.model || names[0] || "";
        agentState.modelListOk = names.length > 0;
        select.disabled = !agentState.modelListOk;
        if (!agentState.modelListOk && label) label.textContent = `${d.provider} · (no models)`;
      }
      writeThinking(d.thinking);
      if (d.error) logWarn("Agent", `Could not list models: ${d.error}`);
      // Push those fallbacks so the server matches what is on screen. Never
      // reloads on failure — this runs inside loadAgentModels() and would recurse.
      const unset = !d.model || d.thinking === null || d.thinking === undefined;
      if (unset && select?.value) await applyAgentSettings({ reloadOnError: false });
    } catch (e) {
      agentState.modelListOk = false;
      if (select) select.disabled = true;
      if (label) label.textContent = "(model list unavailable)";
      logError("Agent", `Could not load model list: ${e}`);
    }
  }

  /** Push the selected model / reasoning effort to the server. */
  async function applyAgentSettings({ reloadOnError = true } = {}) {
    const select = $("agentModelSelect");
    const model = select?.value || "";
    const thinking = readThinking();
    try {
      const res = await apiFetch("/agent/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model, thinking }),
      });
      const d = await res.json();
      if (!d.ok) throw new Error(d.error);
      // Applied silently — the selects themselves show the active settings.
    } catch (e) {
      logError("Agent", `Could not apply settings: ${e}`);
      // The change did not take — put the controls back to what is live.
      if (reloadOnError) await loadAgentModels();
    }
  }

  /**
   * Redraw the conversation the server still holds.
   *
   * The log lives only in the page, so a reload would otherwise show nothing
   * while the agent carries on remembering. Same builders as the live stream,
   * so restored turns are indistinguishable from fresh ones — except for tool
   * images, which never entered the history in the first place.
   */
  async function restoreAgentHistory() {
    let items = [];
    try {
      const d = await (await apiFetch("/agent/history")).json();
      if (!d.ok) throw new Error(d.error);
      items = d.items || [];
    } catch (e) {
      logError("Agent", `Could not restore the conversation: ${e}`);
      return;
    }
    if (!items.length) return;

    const cards = new Map();
    for (const it of items) {
      if (it.kind === "user") {
        makeUserMessage(it.text || "", it.time);
      } else if (it.kind === "agent") {
        const msg = makeAgentMessage(it.model || "", it.time);
        if (it.thinking) {
          msg.thinkingEl.textContent = it.thinking;
          msg.toggle.hidden = false;
          msg.setOpen(false);   // finished turns start folded
        }
        if (it.text) { msg.raw = it.text; msg.finish(); }
        else msg.textEl.hidden = true;   // no answer, so no blinking caret
      } else if (it.kind === "tool_call") {
        const card = appendToolCard({
          server: it.server_id || "", tool: it.tool || "",
          args: it.args || {}, when: it.time,
        });
        if (card) cards.set(it.call_id, card);
      } else if (it.kind === "tool_output") {
        const card = cards.get(it.call_id);
        if (card) updateCardOutput(card, it.output);
      }
    }
    // Restoring drops the whole conversation in at once; land at the newest
    // turn rather than at whatever the browser restored the scroll to.
    jumpToLatest();
  }

  /** Ask the server to stop the turn in flight. */
  async function cancelAgent() {
    try {
      const d = await (await apiFetch("/agent/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // Pressing Stop takes down the workflow this turn launched as well.
        // The pagehide beacon leaves the flag off, so a reload does not: the
        // run survives and reattachIfRunning picks its log back up.
        body: JSON.stringify({ stop_workflow: true }),
      })).json();
      if (!d.ok) throw new Error(d.error);
    } catch (e) {
      logError("Agent", `Could not stop: ${e}`);
    }
  }

  /** Lock the prompt box while a reply is streaming. */
  function setAgentBusy(busy) {
    agentState.streaming = busy;
    const input = $("agentPromptInput"), btn = $("agentSendBtn");
    if (input) input.disabled = busy;
    // The button stays live and becomes the way out, mirroring Run / Cancel.
    if (btn) {
      btn.classList.toggle("btn-danger", busy);
      btn.classList.toggle("btn-primary", !busy);
      btn.textContent = busy ? "■ Stop" : "Send";
    }
    const modelSel = $("agentModelSelect"), levelSel = $("agentLevelSelect");
    // The model picker stays disabled if the list never loaded.
    if (modelSel) modelSel.disabled = busy || !agentState.modelListOk;
    if (levelSel) levelSel.disabled = busy;
    // Locked mid-turn: it is read when the request is sent, so flipping it now
    // would not affect the tool calls already in flight.
    const autoToggle = $("agentAutoToggle");
    if (autoToggle) autoToggle.disabled = busy;
    // No status-line spinner here: the bubble's own caret already shows the
    // turn is in flight, and the log status belongs to workflow runs.
    if (!busy) input?.focus();
  }

  // How long a silent wait runs before the bubble says it is waiting, and
  // before it offers a reason. Under WAIT_NOTICE_S the blinking caret carries
  // it on its own and a notice would only flicker past; past WAIT_HINT_S the
  // wait is long enough that the honest answer is "the model is still loading".
  const WAIT_NOTICE_S = 3;
  const WAIT_HINT_S = 15;

  // =========================================================================
  // Markdown + math rendering (agent replies)
  // =========================================================================
  // Loaded from a CDN, so every entry point is guarded: without the network the
  // reply still reads fine as plain text, which is what it was before.

  // How often a streaming answer is re-rendered. Long enough that a fast model
  // does not trigger a full re-parse per token, short enough to read as live.
  const RENDER_THROTTLE_MS = 100;

  const KATEX_DELIMS = [
    { left: "$$", right: "$$", display: true },
    { left: "\\[", right: "\\]", display: true },
    { left: "\\(", right: "\\)", display: false },
    { left: "$", right: "$", display: false },
  ];

  // Math first, markdown second. Run the other way round and a formula like
  // $a_1 + b_2$ loses its underscores to emphasis before KaTeX ever sees it,
  // so the spans are lifted out, markdown runs, then they are put back.
  const MATH_SPAN = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^\n$]+?\$)/g;
  const MATH_TOKEN = (i) => `%%%MATH${i}%%%`;

  const escapeHtml = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  /** True when the CDN scripts arrived; otherwise callers stay on plain text. */
  function markdownReady() {
    return typeof marked !== "undefined" && typeof DOMPurify !== "undefined";
  }

  /**
   * Render markdown (and any TeX in it) into *el*.
   *
   * Sanitized before it reaches the DOM: the text is model-written and can carry
   * whatever an MCP server returned, so it is not trusted markup.
   */
  function renderMarkdown(el, text) {
    const raw = String(text ?? "");
    if (!markdownReady()) { el.classList.remove("agent-msg__text--md"); el.textContent = raw; return; }
    try {
      const math = [];
      const masked = raw.replace(MATH_SPAN, (m) => MATH_TOKEN(math.push(m) - 1));
      let html = marked.parse(masked, { breaks: true, gfm: true });
      html = html.replace(/%%%MATH(\d+)%%%/g, (_, i) => escapeHtml(math[Number(i)] ?? ""));
      el.innerHTML = DOMPurify.sanitize(html);
      el.classList.add("agent-msg__text--md");
      if (typeof renderMathInElement === "function") {
        renderMathInElement(el, { delimiters: KATEX_DELIMS, throwOnError: false });
      }
    } catch (e) {
      // a broken render must not swallow the answer
      el.classList.remove("agent-msg__text--md");
      el.textContent = raw;
    }
  }

  /**
   * Build the corner label carrying an optional model name and the time.
   * Stamped on creation, so it reads when the message appeared rather than
   * when it happened to finish.
   */
  function makeMessageMeta(model, when) {
    const meta = document.createElement("span");
    meta.className = "agent-msg__meta";
    const modelEl = document.createElement("span");
    modelEl.className = "agent-msg__model";
    modelEl.textContent = model || "";
    const time = document.createElement("span");
    time.className = "agent-msg__time";
    time.textContent = clockTime(when);
    meta.append(modelEl, time);
    return meta;
  }

  /** Build one user bubble: a "User" pill, the prompt, and the time it was sent. */
  function makeUserMessage(text, when) {
    const box = document.createElement("div");
    box.className = "agent-user-msg";
    const head = document.createElement("div");
    head.className = "agent-msg__head";
    const badge = document.createElement("span");
    badge.className = "agent-msg__badge agent-msg__badge--user";
    badge.textContent = "User";
    head.append(badge, makeMessageMeta("", when));
    const textEl = document.createElement("div");
    textEl.className = "agent-msg__text";
    textEl.textContent = text;
    box.append(head, textEl);
    appendChatEl("user", box);
    return box;
  }

  /**
   * Build one assistant bubble: an "Agent" pill with a thinking toggle beside
   * it, the model and time in the top corner, the reasoning trace, the answer.
   */
  function makeAgentMessage(model, when) {
    const box = document.createElement("div");
    box.className = "agent-assistant-msg";

    const head = document.createElement("div");
    head.className = "agent-msg__head";
    const badge = document.createElement("span");
    badge.className = "agent-msg__badge";
    badge.textContent = "Agent";
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "agent-msg__toggle";
    toggle.hidden = true;  // revealed once there is reasoning to show
    head.append(badge, toggle, makeMessageMeta(model, when));

    const thinkingEl = document.createElement("div");
    thinkingEl.className = "agent-msg__thinking";
    thinkingEl.hidden = true;
    // Says why nothing is happening yet. The blinking caret alone reads as a
    // hang once the wait runs past a few seconds, and the first message of a
    // session usually does: the backend has to load the model before it can
    // answer at all.
    const waitEl = document.createElement("div");
    waitEl.className = "agent-msg__waiting";
    waitEl.hidden = true;
    const textEl = document.createElement("div");
    textEl.className = "agent-msg__text";

    const setOpen = (open) => {
      thinkingEl.hidden = !open;
      toggle.textContent = open ? "hide thinking" : "show thinking";
    };
    toggle.addEventListener("click", () => setOpen(thinkingEl.hidden));

    box.append(head, thinkingEl, waitEl, textEl);
    appendChatEl("system", box);

    // The answer is rendered as it arrives, on a timer rather than per delta:
    // every pass re-parses the whole message and re-scans it for formulas, so
    // running that per token would pile up on a long reply.
    //
    // A half-written formula is not a problem — the math spans are only lifted
    // out once their closing delimiter has arrived, so an unfinished one shows
    // as a literal "$" and turns into math the moment it is closed.
    const msg = {
      box, toggle, thinkingEl, waitEl, textEl, setOpen,
      raw: "",
      _wait: null,
      _timer: null,
      /**
       * Start explaining the wait, once it is long enough to need explaining.
       *
       * Nothing is shown for the first few seconds: a quick answer would only
       * flash a notice on its way past. After that the elapsed time ticks, and
       * once the wait is long enough to look broken it says why it might be —
       * the first message of a session pays for loading the model.
       */
      startWaiting() {
        const began = Date.now();
        const tick = () => {
          const s = Math.round((Date.now() - began) / 1000);
          if (s < WAIT_NOTICE_S) return;
          this.waitEl.hidden = false;
          this.waitEl.textContent = s < WAIT_HINT_S
            ? `Waiting for the model… ${s}s`
            : `Waiting for the model… ${s}s — the first message also loads it, `
              + `which can take a while.`;
        };
        this._wait = setInterval(tick, 1000);
      },
      /** The model answered (or gave up); the explanation is no longer wanted. */
      stopWaiting() {
        if (this._wait) { clearInterval(this._wait); this._wait = null; }
        this.waitEl.hidden = true;
        this.waitEl.textContent = "";
      },
      append(chunk) {
        this.stopWaiting();
        this.raw += chunk;
        if (!markdownReady()) { this.textEl.textContent = this.raw; return; }
        if (this._timer) return;
        this._timer = setTimeout(() => {
          this._timer = null;
          this._render();
        }, RENDER_THROTTLE_MS);
      },
      finish() {
        this.stopWaiting();
        // Whatever the last tick skipped is still missing, so the final pass is
        // not optional.
        if (this._timer) { clearTimeout(this._timer); this._timer = null; }
        if (this.raw) this._render();
      },
      /**
       * Re-render, keeping the log on the newest line.
       *
       * A pass reflows the whole message, so it changes the log's height on its
       * own schedule — between the deltas that would otherwise do the following.
       * Without this the view falls behind its own answer mid-stream.
       */
      _render() {
        const log = logEl();
        const stick = isLogAtBottom(log);
        renderMarkdown(this.textEl, this.raw);
        stickLog(log, stick);
      },
    };
    return msg;
  }

  /**
   * Add Approve / Deny buttons to a tool card and wire them to the server.
   *
   * While the decision is pending the card shows only what is about to run, so
   * the output section stays hidden — there is no result to speak of yet, and
   * its "running…" placeholder would misrepresent a call that has not started.
   */
  function addApprovalBar(card, callId) {
    const outBox = card.querySelector('[data-role="output"]');
    const outLabel = outBox?.previousElementSibling;
    const showOutput = (on) => {
      if (outBox) outBox.hidden = !on;
      if (outLabel?.classList.contains("tool-section-label")) outLabel.hidden = !on;
    };
    showOutput(false);

    const bar = document.createElement("div");
    bar.className = "tool-card__approval";
    const note = document.createElement("span");
    note.className = "tool-card__approval-note";
    note.textContent = "Run this tool?";
    const yes = document.createElement("button");
    yes.type = "button"; yes.className = "btn btn-primary btn-sm"; yes.textContent = "Approve";
    const no = document.createElement("button");
    no.type = "button"; no.className = "btn btn-danger btn-sm"; no.textContent = "Deny";
    bar.append(note, yes, no);
    card.appendChild(bar);

    let settled = false;
    const decide = async (approved) => {
      yes.disabled = no.disabled = true;
      try {
        const d = await (await apiFetch("/agent/approve", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ call_id: callId, approved }),
        })).json();
        if (!d.ok) throw new Error(d.error);
        settled = true;
        if (approved) {
          // Now it really is running — the output filling in is confirmation enough.
          showOutput(true);
          bar.remove();
          return;
        }
        // Denied: nothing will ever run, so leave the output hidden and say why.
        bar.innerHTML = "";
        const done = document.createElement("span");
        done.className = "tool-card__approval-note";
        done.textContent = "Denied";
        bar.appendChild(done);
      } catch (e) {
        yes.disabled = no.disabled = false;
        logError("Agent", `Could not send the decision: ${e}`);
      }
    };
    yes.addEventListener("click", () => decide(true));
    no.addEventListener("click", () => decide(false));

    // Lets the caller retire the bar if the decision is settled elsewhere —
    // the server auto-denies a call nobody answered within its timeout. A
    // decision made here already rendered itself, so this becomes a no-op.
    return () => {
      if (settled) return;
      showOutput(true);
      bar.remove();
    };
  }

  /**
   * Put an agent-designed workflow into the Blockly workspace.
   *
   * The workspace is the executable form: runWorkflow() exports the AST back out
   * of it, so what runs is whatever ends up on screen — including anything the
   * user changes before pressing Run.
   *
   * The toolbox is rebuilt first because the generated XML names block types
   * that only exist once buildToolbox() has defined them — a tool block for a
   * server added after page load, or nimo_var__* for a candidates file uploaded
   * since. An undefined type makes domToWorkspace throw, which loadWorkspaceXml
   * answers by clearing the workspace.
   */
  async function applyAgentWorkflow(data) {
    if (execState.running) {
      logWarn("Agent", "A workflow is running, so the workspace was left unchanged.");
      return;
    }
    try { await buildToolbox(); } catch (e) { logWarn("Agent", `Toolbox: ${e}`); }
    loadWorkspaceXml(data.xml);
    const summary = data.summary ? ` (${data.summary})` : "";
    const card = logInfo("Agent", data.auto_run
      ? `Workflow placed in the workspace${summary}. Running it now.`
      : `Workflow placed in the workspace${summary}. Review or edit the blocks, then press Run.`);
    // The NIMO XML of what was just placed, so the design can be read as a
    // workflow rather than as blocks before anything is run. Not awaited: in
    // Auto mode the run starts the moment this returns, and the server only
    // gives the page WORKFLOW_START_TIMEOUT_S to get there — a round trip for a
    // fold nobody has opened yet is not worth spending it on.
    fetchWorkflowXml().then(xml => attachXmlFold(card, xml));
  }

  /**
   * Send one prompt and stream the reply.
   *
   * Bubbles are opened lazily and closed whenever a tool is called, so a turn
   * reads in the order it happened: [thinking] [tool card] [answer].
   */
  async function sendAgentMessage(text) {
    makeUserMessage(text);
    const log = logEl();
    // Recorded now so the footer names the model that actually answered, even
    // if the picker is changed afterwards.
    const model = $("agentModelSelect")?.value || "";
    const cards = new Map();   // tool_call_id -> card element
    const bars = new Map();    // tool_call_id -> retire the approval bar
    let msg = null, gotThinking = false, collapsed = false, stopped = false;

    // Fold the reasoning away once the model moves on from it — whether that is
    // an answer or a tool call. Guarded so a bubble the user re-opened by hand
    // is left alone.
    const foldThinking = () => {
      if (!msg || !gotThinking || collapsed) return;
      collapsed = true;
      msg.setOpen(false);
    };

    const closeBubble = () => {
      if (!msg) return;
      foldThinking();
      // The answer is complete, so it can safely become markdown now.
      msg.finish();   // also stops the waiting notice
      // The bubble is opened before the model has said anything, so it can turn
      // out to hold nothing at all — a reply that opens with a tool call. Take
      // the whole shell away rather than leaving a header with no message.
      if (!msg.raw && !gotThinking) { msg.box.parentElement?.remove(); msg = null; gotThinking = false; collapsed = false; return; }
      // Nothing more is coming, so drop the empty answer line — otherwise its
      // caret keeps blinking in a bubble that is already finished.
      if (!msg.textEl.textContent) msg.textEl.hidden = true;
      msg = null;
      gotThinking = false;
      collapsed = false;
    };
    // The model is stamped at creation, so it names whichever one was live when
    // this bubble started even if the picker changes later.
    const openBubble = () => (msg = msg || makeAgentMessage(model));

    setAgentBusy(true);
    // Opened before the request goes out rather than on the first token. The
    // model may not be resident yet, and until this bubble exists nothing on
    // screen says the message was even sent — the log just holds the user's
    // own line and looks like the send was dropped. The empty bubble carries
    // its own blinking caret, so the wait reads as a wait.
    openBubble();
    msg.startWaiting();
    try {
      const res = await apiFetch("/agent/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        // Missing toggle falls back to auto, so a send can never hang on a
        // confirmation the user has no way to give.
        body: JSON.stringify({
          prompt: text,
          auto_approve: $("agentAutoToggle")?.checked !== false,
        }),
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      for await (const { event, data } of readSseStream(res)) {
        const stick = isLogAtBottom(log);
        if (event === "thinking") {
          openBubble();
          // Reasoning arriving is the model answering, so the wait is over
          // even though the answer itself has not started.
          msg.stopWaiting();
          if (!gotThinking) {
            // Open while it streams so the reasoning can be watched live.
            gotThinking = true;
            msg.toggle.hidden = false;
            msg.setOpen(true);
          }
          msg.thinkingEl.textContent += data.text || "";
          // The pane caps its own height — follow the newest line inside it.
          msg.thinkingEl.scrollTop = msg.thinkingEl.scrollHeight;
        } else if (event === "delta") {
          openBubble();
          foldThinking();   // the answer leads, not the reasoning
          msg.append(data.text || "");
        } else if (event === "tool_call") {
          closeBubble();
          // Same renderer as Blockly runs, so the cards look identical.
          const card = appendToolCard({
            server: data.server_id || "", tool: data.tool || "", args: data.args || {},
          });
          if (card) {
            cards.set(data.call_id, card);
            if (data.needs_approval) bars.set(data.call_id, addApprovalBar(card, data.call_id));
          }
        } else if (event === "tool_output") {
          // Keyed by call id, not "most recent" — the model may run tools in parallel.
          const card = cards.get(data.call_id);
          // A result settles the question, even if the buttons were never pressed.
          const retire = bars.get(data.call_id);
          if (retire) { retire(); bars.delete(data.call_id); }
          if (card) updateCardOutput(card, data.output);
        } else if (event === "workflow") {
          closeBubble();
          await applyAgentWorkflow(data);
          if (data.auto_run) {
            // The run's own cards are the indicator while it runs; this covers
            // the gap after it, when plan_workflow has returned and the model is
            // thinking again with nothing on screen to say so. One-shot, and
            // only for a run this turn started — a workflow the user ran by hand
            // has no turn waiting on it.
            agentState.onRunFinished = () => {
              agentState.onRunFinished = null;
              openBubble();
              msg.startWaiting();
            };
            // Safe to start mid-reply: in Auto mode plan_workflow is blocked on
            // this run and only returns once it is over, so the rest of the
            // reply is written afterwards. That ordering matters because the
            // server refuses instrument tool calls while a workflow runs — by
            // the time the model writes again, there is no run to refuse for.
            //
            // Not awaited. runWorkflow() streams for as long as the run lasts,
            // and this loop must keep draining the response: block it and the
            // thinking/delta events stop arriving and the Stop button never
            // reaches the server. runWorkflow() also returns early when a run is
            // already in flight, so it needs no guard from this side.
            void runWorkflow();
          }
        } else if (event === "canceled") {
          stopped = true;
          // Any card still asking for approval will never get an answer now.
          for (const retire of bars.values()) retire();
          bars.clear();
        } else if (event === "error") {
          throw new Error(data.error || "unknown error");
        }
        stickLog(log, stick);
      }
      const empty = !msg || !msg.textEl.textContent;
      closeBubble();
      if (stopped) {
        logWarn("Agent", "Stopped."
          + (execState.running ? " Canceling the workflow it started." : ""));
      }
      else if (empty) logWarn("Agent", "The model returned an empty reply.");
    } catch (e) {
      // Nothing at all arrived — no point leaving an empty bubble behind, and
      // its waiting notice must not outlive the turn that started it.
      if (msg && !gotThinking && !msg.textEl.textContent) {
        msg.stopWaiting();
        msg.box.parentElement?.remove();
        msg = null;
      }
      closeBubble();
      logError("Agent", String(e));
    } finally {
      // The hook closes over this turn's bubble; a run finishing after the turn
      // is over has nothing to announce.
      agentState.onRunFinished = null;
      setAgentBusy(false);
    }
  }

  /** Announce the session, with its folder as a clickable path. */
  function showSessionCard(message, runDir) {
    const card = logInfo("Session", message);
    if (!card || !runDir) return;
    const path = document.createElement("span");
    path.className = "log-card__path";
    path.textContent = runDir;
    path.setAttribute("role", "button");
    path.tabIndex = 0;
    path.title = "Open in file manager";
    const open = async () => {
      try {
        const r = await (await apiFetch("/session/open", { method: "POST" })).json();
        if (!r.ok) throw new Error(r.error);
      } catch (e) { logError("Session", String(e)); }
    };
    path.addEventListener("click", open);
    path.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
    card.appendChild(path);
  }

  /**
   * Announce the session on page load. The log lives only in the page, so it
   * comes back empty even though the session is still running — hence the
   * wording depends on whether anything has been recorded yet.
   */
  async function showSessionOnLoad() {
    try {
      const d = await (await apiFetch("/session")).json();
      if (!d.ok || !d.active) return;
      showSessionCard(d.entries ? "Session in progress" : "New session started",
                      d.run_dir);
    } catch { /* the session banner is not worth an error card */ }
  }

  /**
   * Start a fresh session: a new results folder, an empty log, and — since the
   * agent's memory refers to work in the old folder — a cleared conversation.
   */
  async function newSession() {
    let runDir = "";
    try {
      const d = await (await apiFetch("/session/new", { method: "POST" })).json();
      if (!d.ok) throw new Error(d.error);
      runDir = d.run_dir || "";
    } catch (e) {
      logError("Session", `Could not start a new session: ${e}`);
      return;
    }
    setLogPlaceholder("");
    if (agentState.enabled) {
      try { await apiFetch("/agent/reset", { method: "POST" }); }
      catch (e) { logError("Agent", `Failed to reset conversation: ${e}`); }
    }
    showSessionCard("New session started", runDir);
  }

  /**
   * Open the session report, building it on the way.
   *
   * One navigation does the whole job: the server generates the report and then
   * serves it, so the tab is claimed inside the click and the browser's own
   * loading indicator covers the wait. Splitting it — open a tab, POST, then
   * point the tab at the result — cannot work, because the build takes as long
   * as the model needs to summarise the session and by then the popup is far
   * outside the click that would have allowed it.
   *
   * Nothing goes to the log: the report is the result, and any failure is shown
   * in the tab that was opened for it.
   */
  function generateReport() {
    window.open("/session/report/view?generate=1", "_blank", "noopener");
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
        localStorage.setItem("workflow_id", cur.workflow_id); setRunningUI(true);
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
      // Refresh the candidates dialog (if open) so status/params update.
      if ($("candidatesOverlay")?.classList.contains("active")) { try { await renderCandidatesPanel(); } catch {} }
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
    $("newSessionBtn")?.addEventListener("click", newSession);
    $("logJumpLatest")?.addEventListener("click", jumpToLatest);
    $("log")?.addEventListener("scroll", () => updateJumpLatest());
    $("reportBtn")?.addEventListener("click", generateReport);
    setRunningUI(false);

    initSettingsPanel();
    $("settingsCloseBtn")?.addEventListener("click", closeSettings);
    $("settingsOverlay")?.addEventListener("click", (e) => { if (e.target === $("settingsOverlay")) closeSettings(); });
    $("addToolsBtn")?.addEventListener("click", openSettings);

    // Candidates file dialog (shows where candidates.csv is stored + upload)
    $("uploadCsvBtn")?.addEventListener("click", openCandidates);
    $("candidatesCloseBtn")?.addEventListener("click", closeCandidates);
    $("candidatesOverlay")?.addEventListener("click", (e) => { if (e.target === $("candidatesOverlay")) closeCandidates(); });
    $("csvFileInput")?.addEventListener("change", handleCsvUpload);

    // Agent prompt below the log (only when enabled in config.yaml)
    await initAgentPrompt();

    // Leaving mid-turn loses it: an interrupted turn cannot be resumed, so warn
    // first. Workflow runs are exempt — they keep going server-side and are
    // picked back up by reattachIfRunning() on the next load.
    window.addEventListener("beforeunload", (e) => {
      if (!agentState.streaming) return;
      e.preventDefault();
      e.returnValue = "";   // browsers show their own wording
    });
    // Fires only when the page really goes away, so answering "stay" in the
    // dialog above cannot cancel the run. A plain fetch would be dropped during
    // unload; sendBeacon is the one that survives.
    window.addEventListener("pagehide", () => {
      if (!agentState.streaming) return;
      try {
        navigator.sendBeacon("/agent/cancel",
          new Blob(["{}"], { type: "application/json" }));
      } catch {}
    });

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

    // Leads the log, so the folder being written to is the first thing shown.
    await showSessionOnLoad();

    // Check if NIMO has a candidates file loaded
    try {
      const st = await (await apiFetch("/nimo/status")).json();
      if (st.ok && !st.ready) {
        logWarn("NIMO", "No candidates file found. Click 'Upload candidates file' to add one and see where it is stored.");
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