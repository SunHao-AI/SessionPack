(function () {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token") || "";

  function withToken(path) {
    const url = new URL(path, window.location.origin);
    if (token) url.searchParams.set("token", token);
    return url.toString();
  }

  async function api(path) {
    const response = await fetch(withToken(path));
    if (!response.ok) throw new Error("请求失败：" + response.status);
    return response.json();
  }

  function link(path) {
    if (!token) return path;
    const url = new URL(path, window.location.origin);
    url.searchParams.set("token", token);
    return url.pathname + url.search;
  }

  async function initIndex() {
    const diag = document.getElementById("diag-link");
    if (diag) diag.href = link("/diagnostics");
    const status = document.getElementById("status");
    const list = document.getElementById("list");
    const select = document.getElementById("project");
    try {
      const data = await api("/api/sessions");
      data.projects.forEach(function (project) {
        const option = document.createElement("option");
        option.value = project.project_id;
        option.textContent = project.display_name + "（" + project.session_count + "）";
        select.appendChild(option);
      });
      renderSessions(data.sessions, list, status);
      document.getElementById("search").addEventListener("submit", async function (event) {
        event.preventDefault();
        const q = document.getElementById("q").value.trim();
        const project = select.value;
        const query = new URLSearchParams();
        if (q) query.set("q", q);
        if (project) query.set("project", project);
        const result = await api("/api/sessions?" + query.toString());
        renderSessions(result.sessions, list, status);
      });
    } catch (error) {
      status.textContent = error.message;
    }
  }

  function renderSessions(sessions, list, status) {
    list.replaceChildren();
    status.textContent = "共 " + sessions.length + " 个会话";
    sessions.forEach(function (session) {
      const item = document.createElement("li");
      const anchor = document.createElement("a");
      anchor.href = link("/session?id=" + encodeURIComponent(session.session_id));
      anchor.textContent = session.title || session.session_id;
      const meta = document.createElement("span");
      meta.textContent = " · " + (session.updated_at || session.created_at || "");
      item.appendChild(anchor);
      item.appendChild(meta);
      list.appendChild(item);
    });
  }

  async function initSession() {
    const diag = document.getElementById("diag-link");
    if (diag) diag.href = link("/diagnostics");
    const id = params.get("id") || "";
    const summaryBox = document.getElementById("summary");
    const list = document.getElementById("messages");
    try {
      const data = await api("/api/sessions/" + encodeURIComponent(id));
      summaryBox.replaceChildren();
      const title = document.createElement("h2");
      title.textContent = data.summary.title;
      const meta = document.createElement("p");
      meta.textContent = data.summary.project_name + " · " + data.summary.session_id +
        " · " + data.summary.message_count + " 条消息";
      summaryBox.appendChild(title);
      summaryBox.appendChild(meta);
      [["最新进展", data.summary.latest_outcome],
       ["关键结论", data.summary.learned.join("；")],
       ["涉及文件", data.summary.files.join("、")]].forEach(function (pair) {
        const heading = document.createElement("h3");
        heading.textContent = pair[0];
        const body = document.createElement("p");
        body.textContent = pair[1] || "（无记录）";
        summaryBox.appendChild(heading);
        summaryBox.appendChild(body);
      });
      data.messages.forEach(function (message) {
        const item = document.createElement("li");
        const head = document.createElement("strong");
        head.textContent = (message.ts || "") + " · " + (message.intent || "（无意图）");
        const outcome = document.createElement("p");
        outcome.textContent = message.outcome || "";
        item.appendChild(head);
        item.appendChild(outcome);
        list.appendChild(item);
      });
    } catch (error) {
      summaryBox.textContent = error.message;
    }
  }

  async function initDiagnostics() {
    const report = document.getElementById("report");
    try {
      const data = await api("/api/diagnostics");
      report.textContent = JSON.stringify(data, null, 2);
    } catch (error) {
      report.textContent = error.message;
    }
  }

  window.SessionPack = { initIndex: initIndex, initSession: initSession, initDiagnostics: initDiagnostics };
})();
