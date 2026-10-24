const scheduleContent = document.querySelector("#schedule-content");
const graphViewport = document.querySelector("#graph-viewport");
const graphSummary = document.querySelector("#graph-summary");
const incidentForm = document.querySelector("#incident-form");
const incidentText = document.querySelector("#incident-text");
const submitButton = document.querySelector("#submit-incident");
const formError = document.querySelector("#form-error");
const reviewCard = document.querySelector("#review-card");
const decisionActions = document.querySelector("#decision-actions");
const decisionNote = document.querySelector("#decision-note");
const approveButton = document.querySelector("#approve-fix");
const rejectButton = document.querySelector("#reject-fix");
let currentFixId = null;
let currentSchedule = [];
let sessionTitles = new Map();
let isDeciding = false;

const escapeText = (value) => String(value ?? "");
const formatTime = (minutes) => {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;
  const suffix = hours >= 12 ? "PM" : "AM";
  const hour12 = hours % 12 || 12;
  return `${hour12}:${String(mins).padStart(2, "0")} ${suffix}`;
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.detail || `Request failed (${response.status})`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

function element(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = escapeText(text);
  return item;
}

function renderSchedule(rooms) {
  currentSchedule = rooms;
  scheduleContent.replaceChildren();
  const talkCount = rooms.reduce((sum, room) => sum + room.talks.length, 0);
  document.querySelector("#talk-count").textContent = String(talkCount).padStart(2, "0");
  document.querySelector("#room-count").textContent = String(rooms.length).padStart(2, "0");

  if (!rooms.length) {
    scheduleContent.append(element("div", "empty-state", "No rooms or talks are available."));
    return;
  }

  for (const room of rooms) {
    const section = element("section", "room-section");
    const heading = element("div", "room-heading");
    const roomName = element("div", "room-name");
    roomName.append(element("span", "room-mark"), element("span", "", room.name));
    heading.append(roomName, element("span", "room-count", `${String(room.talks.length).padStart(2, "0")} TALKS`));
    section.append(heading);

    for (const talk of room.talks) {
      const row = element("article", "talk-row");
      const time = element("div", "talk-time", `${formatTime(talk.start)} – ${formatTime(talk.end)}`);
      const info = element("div", "talk-info");
      info.append(element("span", "talk-title", talk.title));
      const metadata = element("span", "talk-meta");
      metadata.append(element("span", "", talk.speaker || "Speaker TBD"));
      if (talk.session) {
        metadata.append(element("span", "separator", "·"), element("span", "", talk.session));
      }
      info.append(metadata);
      const badge = element("span", `talk-badge ${(talk.status || "").toLowerCase()}`, talk.status || "scheduled");
      row.append(time, info, badge);
      section.append(row);
    }
    scheduleContent.append(section);
  }
}

function findTalk(talkId) {
  for (const room of currentSchedule) {
    const talk = room.talks.find((item) => item.id === talkId);
    if (talk) return { ...talk, room: room.name };
  }
  return null;
}

function renderFix(result) {
  const fix = result.fix;
  currentFixId = fix.id;
  document.querySelector("#fix-title").textContent = fix.proposal;
  document.querySelector("#review-status").textContent = "NEEDS YOUR OK";
  document.querySelector("#review-status").className = "review-pending";
  document.querySelector("#decision-note").textContent =
    "Nothing changes until you approve this proposal.";
  decisionNote.className = "decision-note";
  decisionActions.hidden = false;
  approveButton.disabled = false;
  rejectButton.disabled = false;

  const details = document.querySelector("#fix-details");
  details.replaceChildren();
  for (const change of fix.staged_changes || []) {
    const talk = findTalk(change.talk_id);
    const title = talk?.title || change.talk_id;
    const row = element("div", "fix-detail");
    const label = element("strong", "", title);
    const extendsCurrentTalk = talk && change.start === talk.start && change.end > talk.end;
    row.append(label, document.createTextNode(
      extendsCurrentTalk
        ? ` extends to ${formatTime(change.end)} (+${change.end - talk.end} min)`
        : ` moves to ${formatTime(change.start)}–${formatTime(change.end)}`
    ));
    if (talk?.room) row.append(document.createTextNode(` · ${talk.room}`));
    details.append(row);
  }
  for (const change of fix.staged_session_changes || []) {
    const row = element("div", "fix-detail");
    const sessionName = change.session_title
      || sessionTitles.get(change.session_id)
      || change.session_id;
    row.append(
      element("strong", "", sessionName),
      document.createTextNode(` adjusts to ${formatTime(change.start)}–${formatTime(change.end)}`)
    );
    details.append(row);
  }
  if (fix.presenter_change) {
    const change = fix.presenter_change;
    const talk = findTalk(change.talk_id);
    const row = element("div", "fix-detail");
    row.append(
      element("strong", "", talk?.title || change.talk_id),
      document.createTextNode(` · ${change.from_person} → ${change.to_person}`)
    );
    details.append(row);
  }
  if (!details.childElementCount) {
    details.append(element("div", "fix-detail", "The proposed changes are ready for review."));
  }

  const notifications = document.querySelector("#notification-list");
  notifications.replaceChildren();
  for (const notification of result.notifications || []) {
    notifications.append(element("div", "notification-item", `Draft for ${notification.recipient}`));
  }
  reviewCard.hidden = false;
  reviewCard.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function setDecisionState(status, message) {
  decisionActions.hidden = true;
  document.querySelector("#review-status").textContent = status === "applied" ? "APPROVED" : "REJECTED";
  document.querySelector("#review-status").className =
    status === "applied" ? "review-pending decision-complete" : "review-pending decision-rejected";
  decisionNote.textContent = message;
  decisionNote.className = `decision-note ${status === "applied" ? "decision-complete" : "decision-rejected"}`;
}

async function loadDashboard() {
  scheduleContent.replaceChildren(element("div", "loading-state", "Loading the floor plan…"));
  graphViewport.replaceChildren(element("div", "loading-state", "Mapping the conference…"));
  const [scheduleResult, graphResult, fixesResult] = await Promise.allSettled([
    api("/schedule"),
    api("/graph"),
    api("/fixes"),
  ]);

  if (scheduleResult.status === "fulfilled") {
    renderSchedule(scheduleResult.value);
  } else {
    scheduleContent.replaceChildren(element(
      "div", "empty-state", `Couldn’t load the schedule: ${scheduleResult.reason.message}`
    ));
  }
  if (graphResult.status === "fulfilled") {
    renderGraph(graphResult.value);
  } else {
    graphViewport.replaceChildren(element("div", "empty-state", "Graph data is unavailable."));
    graphSummary.textContent = "Could not load graph data.";
  }
  if (fixesResult.status === "fulfilled") {
    const pending = fixesResult.value.filter((fix) => fix.status === "proposed").length;
    document.querySelector("#pending-count").textContent = String(pending).padStart(2, "0");
  } else {
    document.querySelector("#pending-count").textContent = "—";
  }
}

incidentText.addEventListener("input", () => {
  document.querySelector("#char-count").textContent = `${incidentText.value.length} / 1000`;
});

document.querySelectorAll("[data-example]").forEach((button) => {
  button.addEventListener("click", () => {
    incidentText.value = button.dataset.example;
    incidentText.dispatchEvent(new Event("input"));
    incidentText.focus();
  });
});

incidentForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  formError.hidden = true;
  const text = incidentText.value.trim();
  if (!text) {
    formError.textContent = "Describe the incident before submitting.";
    formError.hidden = false;
    return;
  }

  submitButton.disabled = true;
  submitButton.firstElementChild.textContent = "Analyzing incident…";
  try {
    const result = await api("/incident", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    renderFix(result);
    await loadDashboard();
  } catch (error) {
    formError.textContent = error.message;
    formError.hidden = false;
  } finally {
    submitButton.disabled = false;
    submitButton.firstElementChild.textContent = "Analyze incident";
  }
});

async function decideFix(decision) {
  if (!currentFixId || isDeciding) return;
  isDeciding = true;
  approveButton.disabled = true;
  rejectButton.disabled = true;
  const endpoint = decision === "approve" ? "approve" : "reject";
  try {
    await api(`/fixes/${encodeURIComponent(currentFixId)}/${endpoint}`, { method: "POST" });
    setDecisionState(
      decision === "approve" ? "applied" : "rejected",
      decision === "approve"
        ? "Approved. The schedule and graph now reflect the change."
        : "Rejected. The current schedule remains unchanged."
    );
    await loadDashboard();
  } catch (error) {
    decisionNote.textContent = `Could not ${decision} this fix: ${error.message}`;
    decisionNote.className = "decision-note decision-rejected";
    approveButton.disabled = false;
    rejectButton.disabled = false;
  } finally {
    isDeciding = false;
  }
}

approveButton.addEventListener("click", () => decideFix("approve"));
rejectButton.addEventListener("click", () => decideFix("reject"));
document.querySelector("#refresh-button").addEventListener("click", loadDashboard);

function graphNodeLabel(node) {
  const props = node.props || {};
  return props.title || props.name || props.desc || props.proposal || props.id || node.label;
}

function renderGraph(graph) {
  const groups = ["Incident", "Agent", "Room", "Talk", "Session", "Person", "Topic", "SideEvent", "Catering", "Fix", "Notification"];
  const visibleNodes = graph.nodes.filter((node) => {
    if (!groups.includes(node.label)) return false;
    return !(node.label === "Person" && node.props?.role === "attendee");
  });
  sessionTitles = new Map(
    graph.nodes
      .filter((node) => node.label === "Session" && node.props?.id && node.props?.title)
      .map((node) => [node.props.id, node.props.title])
  );
  const visibleIds = new Set(visibleNodes.map((node) => String(node.id)));
  const links = graph.links.filter((link) =>
    visibleIds.has(String(link.source)) && visibleIds.has(String(link.target))
  );
  const nodesByGroup = new Map(groups.map((group) => [
    group,
    visibleNodes.filter((node) => node.label === group),
  ]));
  const width = 190 + groups.length * 155;
  const headerHeight = 76;
  const rowHeight = 75;
  const maxRows = Math.max(1, ...[...nodesByGroup.values()].map((items) => items.length));
  const height = headerHeight + maxRows * rowHeight + 35;
  const positions = new Map();
  groups.forEach((group, groupIndex) => {
    nodesByGroup.get(group).forEach((node, index) => {
      positions.set(String(node.id), {
        x: 20 + groupIndex * 155,
        y: headerHeight + index * rowHeight,
      });
    });
  });

  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Conference graph showing talks, rooms, sessions, speakers, incidents, and operations.");
  const defs = document.createElementNS(svgNS, "defs");
  const marker = document.createElementNS(svgNS, "marker");
  marker.setAttribute("id", "graph-arrow");
  marker.setAttribute("viewBox", "0 0 10 10");
  marker.setAttribute("refX", "9");
  marker.setAttribute("refY", "5");
  marker.setAttribute("markerWidth", "4");
  marker.setAttribute("markerHeight", "4");
  marker.setAttribute("orient", "auto-start-reverse");
  const arrow = document.createElementNS(svgNS, "path");
  arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
  arrow.setAttribute("fill", "#70819a");
  marker.append(arrow);
  defs.append(marker);
  svg.append(defs);

  groups.forEach((group, groupIndex) => {
    const label = document.createElementNS(svgNS, "text");
    label.setAttribute("x", String(20 + groupIndex * 155));
    label.setAttribute("y", "30");
    label.setAttribute("class", "graph-lane-label");
    label.textContent = group.toUpperCase();
    svg.append(label);
  });

  for (const link of links) {
    const from = positions.get(String(link.source));
    const to = positions.get(String(link.target));
    if (!from || !to) continue;
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", String(from.x + 130));
    line.setAttribute("y1", String(from.y + 23));
    line.setAttribute("x2", String(to.x));
    line.setAttribute("y2", String(to.y + 23));
    line.setAttribute("class", `graph-edge edge-${link.type}`);
    line.setAttribute("marker-end", "url(#graph-arrow)");
    const title = document.createElementNS(svgNS, "title");
    title.textContent = link.type;
    line.append(title);
    svg.append(line);
  }

  for (const node of visibleNodes) {
    const point = positions.get(String(node.id));
    const group = document.createElementNS(svgNS, "g");
    group.setAttribute("class", `graph-node node-${node.label}`);
    group.setAttribute("transform", `translate(${point.x}, ${point.y})`);
    const title = document.createElementNS(svgNS, "title");
    title.textContent = `${node.label}: ${graphNodeLabel(node)}`;
    const rect = document.createElementNS(svgNS, "rect");
    rect.setAttribute("width", "132");
    rect.setAttribute("height", "47");
    const name = document.createElementNS(svgNS, "text");
    name.setAttribute("x", "9");
    name.setAttribute("y", "19");
    name.textContent = graphNodeLabel(node).slice(0, 21);
    const id = document.createElementNS(svgNS, "text");
    id.setAttribute("x", "9");
    id.setAttribute("y", "36");
    id.setAttribute("class", "node-id");
    id.textContent = String(node.props?.id || node.label).slice(0, 24);
    group.append(title, rect, name, id);
    svg.append(group);
  }

  graphViewport.replaceChildren(svg);
  graphSummary.textContent =
    `${visibleNodes.length} nodes · ${links.length} relationships · attendee RSVP records grouped`;
}

loadDashboard();
