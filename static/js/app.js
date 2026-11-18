(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = {};
  let templates = [];
  let batches = [];
  let reports = [];
  let verifications = [];
  let traceRuns = [];
  let policy = { current: null, history: [] };
  let selected = null;
  let stampValue = "";
  let traceValue = "WM-ORD-131";
  let traceResult = null;
  let traceError = null;
  let depthDraft = "";
  let depthNote = "";

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(j.error || "HTTP " + r.status);
      err.payload = j;
      throw err;
    }
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function esc(v) {
    return String(v === null || v === undefined ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function bindingCode() {
    return (policy.current && policy.current.binding_code) || "—";
  }

  function depthLimit() {
    return policy.current ? policy.current.depth_limit : 0;
  }

  function scopeChip(scope) {
    const cls =
      scope === "超深下游" ? "bad" : scope === "源批次" ? "lime" : "";
    return `<span class="chip ${cls}">${esc(scope || "—")}</span>`;
  }

  async function refresh() {
    const [m, snap] = await Promise.all([
      api("/api/meta"),
      api("/api/console"),
    ]);
    meta = m;
    document.getElementById("lab-name").textContent = m.lab_name || "血缘水印台";
    templates = snap.templates || [];
    batches = snap.batches || [];
    reports = snap.gap_reports || [];
    verifications = snap.verifications || [];
    traceRuns = snap.trace_runs || [];
    policy = snap.policy || { current: null, history: [] };
    if (!depthDraft) depthDraft = String(depthLimit());
    const chip = document.getElementById("binding-chip");
    chip.textContent = bindingCode();
    chip.title = "当前深度绑码";
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === tab);
    });
  }

  function renderDepthCard() {
    const cur = policy.current || {};
    const history = (policy.history || [])
      .slice()
      .reverse()
      .map(
        (p) => `<tr class="${p.is_current ? "cur" : ""}">
          <td>v${p.version_no}${p.is_current ? ' <span class="chip lime">现行</span>' : ""}</td>
          <td>${p.depth_limit} 层</td>
          <td class="mono">${esc(p.binding_code)}</td>
          <td class="muted">${esc(p.note || "—")}</td>
          <td class="muted">${esc(p.created_at)}</td>
        </tr>`
      )
      .join("");
    return `
      <h3>深度卡片</h3>
      <div class="cardish">
        <div class="row">
          <span class="kv">凭证路线<b>旁路版本表 · 事务内判定</b></span>
          <span class="kv">当前版号<b>v${cur.version_no || 0}</b></span>
          <span class="kv">深度上限<b>${cur.depth_limit || 0} 层</b></span>
          <span class="kv">绑码<b class="mono">${esc(cur.binding_code || "—")}</b></span>
        </div>
        <p class="muted">深度卡片亮哈希：绑码 = 版号 + 内容哈希前 12 位，与追索裁定、缺口复验同一口径。</p>
        <div class="row">
          <label style="margin:0">新深度上限
            <input id="depth-input" type="number" min="${policy.min_limit || 1}"
              max="${policy.max_limit || 8}" value="${esc(depthDraft)}" style="width:7rem" />
          </label>
          <label style="margin:0">备注<input id="depth-note" value="${esc(depthNote)}" placeholder="换版原因" /></label>
          <button type="button" id="btn-depth">提交新版本</button>
        </div>
      </div>
      <h4>版本历史（只读）</h4>
      <table>
        <thead><tr><th>版号</th><th>上限</th><th>绑码</th><th>备注</th><th>生效时间</th></tr></thead>
        <tbody>${history}</tbody>
      </table>`;
  }

  function renderBatchDepth() {
    const over = batches.filter((b) => b.scope === "超深下游");
    return `
      <h3>追索深度标注</h3>
      <p class="muted">按当前绑码 <b class="mono">${esc(bindingCode())}</b>（${depthLimit()} 层）与追索同口径：
        深度内 ${batches.filter((b) => b.scope === "深度内").length} 个，
        超深下游 ${over.length} 个${over.length ? "（" + over.map((b) => esc(b.run_code)).join("、") + "）" : ""}。</p>`;
  }

  function renderTemplates() {
    return `
      <h2>水印模板</h2>
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>
          ${templates
            .map(
              (t) => `<tr>
            <td>${esc(t.table_name)}</td>
            <td>${esc(t.column_name)}</td>
            <td>${esc(t.version_no)}</td>
            <td><span class="chip">${esc(t.status)}</span></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <p class="muted">深度绑码见「深度」页：<b class="mono">${esc(bindingCode())}</b></p>`;
  }

  function renderBatches() {
    return `
      <h2>运行批次</h2>
      <p class="muted">主链：选批盖印 → 用水印追索下游 → 缺口对账</p>
      <p class="muted">深度口径按当前绑码 <b class="mono">${esc(
        bindingCode()
      )}</b>（${depthLimit()} 层）：超深下游红底标注，不纳入缺口应盖印。</p>
      <table>
        <thead><tr><th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>状态</th><th>深度</th><th>范围</th><th>水印</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr class="${b.scope === "超深下游" ? "over" : ""}">
            <td>${esc(b.id)}</td>
            <td>${esc(b.table_name)}</td>
            <td>${esc(b.run_code)}</td>
            <td>${esc(b.biz_date)}</td>
            <td><span class="chip">${esc(b.status)}</span></td>
            <td>${b.depth === null || b.depth === undefined ? "-" : b.depth}</td>
            <td>${scopeChip(b.scope)}</td>
            <td>${esc(b.watermark_value || "-")}</td>
            <td>${
              b.status === "待盖印"
                ? `<button type="button" class="ghost" data-pick="${b.id}">盖印</button>`
                : ""
            }</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      ${renderBatchDepth()}
      ${
        selected
          ? `<div class="cardish">
        <h3>盖印</h3>
        <p>${esc(selected.run_code)} · ${esc(selected.table_name)}</p>
        <label>水印值<input id="stamp-input" value="${esc(stampValue)}" /></label>
        <button type="button" id="btn-stamp">写入盖印</button>
      </div>`
          : ""
      }`;
  }

  function renderTraceResult() {
    if (traceError) {
      const blocked = traceError.blocked || [];
      const pol = traceError.policy || {};
      return `
        <div class="cardish danger">
          <h3>裁定：${esc(traceError.outcome || "拒绝")}</h3>
          <p class="err">${esc(traceError.error)}</p>
          <div class="row">
            <span class="kv">绑码<b class="mono">${esc(pol.binding_code || "—")}</b></span>
            <span class="kv">版号<b>v${esc(pol.version_no || 0)}</b></span>
            <span class="kv">深度上限<b>${esc(traceError.depth_limit || 0)} 层</b></span>
            <span class="kv">实际最深<b>${esc(traceError.max_depth || 0)} 层</b></span>
            <span class="kv">追索编号<b>#${esc(traceError.trace_run_id || "-")}</b></span>
          </div>
          <h4>被拦截的超深下游</h4>
          <ul>${blocked
            .map(
              (d) =>
                `<li>${esc(d.run_code)} · 深度 ${esc(d.depth)} · ${esc(d.status)}</li>`
            )
            .join("")}</ul>
          <p class="muted">拒绝为终态：不放行任何节点；证据已写入库内 trace_runs。</p>
        </div>`;
    }
    if (!traceResult) return "";
    const src = traceResult.sources || [];
    const down = traceResult.downstream || [];
    const pol = traceResult.policy || {};
    return `
      <div class="cardish">
        <h3>裁定：${esc(traceResult.outcome)}</h3>
        <div class="row">
          <span class="kv">绑码<b class="mono">${esc(pol.binding_code || "—")}</b></span>
          <span class="kv">版号<b>v${esc(pol.version_no || 0)}</b></span>
          <span class="kv">深度上限<b>${esc(traceResult.depth_limit)} 层</b></span>
          <span class="kv">实际最深<b>${esc(traceResult.max_depth)} 层</b></span>
          <span class="kv">追索编号<b>#${esc(traceResult.trace_run_id || "-")}</b></span>
        </div>
        <h4>源批次</h4>
        <ul>${src
          .map((s) => `<li>${esc(s.run_code)} · ${esc(s.table_name)}</li>`)
          .join("")}</ul>
        <h4>下游（按深度，共 ${down.length} 个）</h4>
        <ul>${down
          .map(
            (d) =>
              `<li>${esc(d.run_code)} · 深度 ${esc(d.depth)} · ${esc(
                d.status
              )} · ${esc(d.watermark_value || "未盖印")}</li>`
          )
          .join("")}</ul>
      </div>`;
  }

  function renderTrace() {
    const evidence = traceRuns
      .slice(0, 8)
      .map(
        (r) => `<tr>
          <td>#${esc(r.id)}</td>
          <td>${esc(r.watermark_value)}</td>
          <td class="mono">${esc(r.binding_code)}</td>
          <td>${esc(r.depth_limit)}</td>
          <td><span class="chip ${
            r.outcome === "拒绝" ? "bad" : "lime"
          }">${esc(r.outcome)}</span></td>
          <td>${esc((r.blocked_codes || []).join("、") || "-")}</td>
        </tr>`
      )
      .join("");
    return `
      <h2>追索详情</h2>
      <p class="muted">点追索即按当前绑码 <b class="mono">${esc(
        bindingCode()
      )}</b> 一次裁定；超深下游一律拒绝。</p>
      <label>水印值<input id="trace-input" value="${esc(
        traceValue
      )}" placeholder="例如 WM-ORD-131" /></label>
      <div class="row"><button type="button" id="btn-trace">追索</button>
        <button type="button" class="ghost" data-fill="WM-DEEP-131">填深链样例</button></div>
      ${renderTraceResult()}
      <h3>库内追索留证</h3>
      <table>
        <thead><tr><th>编号</th><th>水印</th><th>绑码</th><th>上限</th><th>裁定</th><th>超深下游</th></tr></thead>
        <tbody>${evidence || `<tr><td colspan="6" class="muted">暂无留证</td></tr>`}</tbody>
      </table>`;
  }

  function renderGaps() {
    const vrows = verifications
      .slice(0, 8)
      .map(
        (v) => `<tr>
          <td>#${esc(v.gap_report_id)}</td>
          <td class="mono">${esc(v.binding_code)}</td>
          <td>${esc(v.depth_limit)}</td>
          <td><span class="chip ${
            v.outcome === "不一致" ? "bad" : ""
          }">${esc(v.outcome)}</span></td>
          <td>${esc(v.message)}</td>
        </tr>`
      )
      .join("");
    return `
      <h2>缺口台</h2>
      <button type="button" id="btn-gap">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>缺口</th><th>绑码</th><th>上限</th><th>超深排除</th><th>裁定</th><th>摘要</th><th>复验</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${esc(r.id)}</td>
            <td>${esc(r.as_of_date)}</td>
            <td>${esc(r.gap_count)}</td>
            <td class="mono">${esc(r.binding_code || "—")}</td>
            <td>${esc(r.depth_limit || 0)}</td>
            <td>${esc(r.blocked_count || 0)}</td>
            <td><span class="chip ${
              r.outcome === "拦截超深" ? "bad" : "lime"
            }">${esc(r.outcome || "放行")}</span></td>
            <td>${esc(r.summary)}</td>
            <td><button type="button" class="ghost" data-verify="${r.id}">复验</button></td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <h3>复验留证</h3>
      <table>
        <thead><tr><th>对账</th><th>绑码</th><th>上限</th><th>结果</th><th>说明</th></tr></thead>
        <tbody>${vrows || `<tr><td colspan="5" class="muted">暂无复验</td></tr>`}</tbody>
      </table>`;
  }

  function renderDepth() {
    return `
      <h2>追索深度</h2>
      <p class="muted">深度限制绑码在两条凭证路线中选定其一：<b>旁路版本表 + 事务内判定</b>
        （本仓所选），旧版只读留证。同值提交幂等，不新增版号。</p>
      ${renderDepthCard()}`;
  }

  function render() {
    if (tab === "templates") view.innerHTML = renderTemplates();
    else if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "depth") view.innerHTML = renderDepth();
    else if (tab === "trace") view.innerHTML = renderTrace();
    else view.innerHTML = renderGaps();

    const depthInput = document.getElementById("depth-input");
    if (depthInput) {
      depthInput.oninput = () => (depthDraft = depthInput.value);
      document.getElementById("depth-note").oninput = (e) =>
        (depthNote = e.target.value);
      document.getElementById("btn-depth").onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/depth-policy", {
            method: "POST",
            body: JSON.stringify({
              depth_limit: Number(depthDraft),
              note: depthNote,
            }),
          });
          await refresh();
          depthDraft = String(depthLimit());
          setMsg(
            r.changed
              ? `深度已换版至 v${r.version_no}（${r.depth_limit} 层），绑码 ${r.binding_code}`
              : `深度未变，仍为 v${r.version_no}（${r.depth_limit} 层）`
          );
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }

    view.querySelectorAll("[data-pick]").forEach((btn) => {
      btn.onclick = () => {
        const id = Number(btn.dataset.pick);
        selected = batches.find((b) => b.id === id) || null;
        stampValue = selected ? "WM-" + selected.run_code.toUpperCase() : "";
        render();
      };
    });
    const stampInput = document.getElementById("stamp-input");
    if (stampInput) {
      stampInput.oninput = () => (stampValue = stampInput.value);
      document.getElementById("btn-stamp").onclick = async () => {
        setMsg("");
        try {
          await api("/api/batches/" + selected.id + "/stamp", {
            method: "POST",
            body: JSON.stringify({ watermark_value: stampValue }),
          });
          selected = null;
          await refresh();
          setMsg("盖印完成");
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    const traceInput = document.getElementById("trace-input");
    if (traceInput) {
      traceInput.oninput = () => (traceValue = traceInput.value);
      view.querySelectorAll("[data-fill]").forEach((b) => {
        b.onclick = () => {
          traceValue = b.dataset.fill;
          render();
        };
      });
      document.getElementById("btn-trace").onclick = async () => {
        setMsg("");
        try {
          traceResult = await api("/api/trace", {
            method: "POST",
            body: JSON.stringify({
              watermark_value: traceValue,
              binding_code: bindingCode() === "—" ? undefined : bindingCode(),
            }),
          });
          traceError = null;
          setMsg(`追索完成：${traceResult.outcome}`);
        } catch (e) {
          traceResult = null;
          traceError =
            e.payload && e.payload.outcome
              ? e.payload
              : { error: e.message, outcome: "失败" };
          setMsg(e.message);
        }
        await refresh().catch(() => {});
        render();
      };
    }
    const gapBtn = document.getElementById("btn-gap");
    if (gapBtn) {
      gapBtn.onclick = async () => {
        setMsg("");
        try {
          const r = await api("/api/gap-reports", {
            method: "POST",
            body: JSON.stringify({ as_of_date: "2026-11-16" }),
          });
          await refresh();
          setMsg(r.report.summary);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    }
    view.querySelectorAll("[data-verify]").forEach((btn) => {
      btn.onclick = async () => {
        setMsg("");
        try {
          const r = await api(
            "/api/gap-reports/" + btn.dataset.verify + "/verify",
            { method: "POST", body: JSON.stringify({}) }
          );
          await refresh();
          setMsg(r.verification.message);
          render();
        } catch (e) {
          setMsg(e.message);
        }
      };
    });
  }

  document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncDock();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(() => {
      syncDock();
      render();
    })
    .catch((e) => setMsg(e.message));
})();
