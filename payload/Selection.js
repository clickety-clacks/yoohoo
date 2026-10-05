// Pure selection policy shared by the QML menu and regression tests.
var HUB_HEARTBEAT_DEADLINE_MS = 45000;
var HUB_HEALTH_STATUSES = [
    "connecting", "live", "reconnecting", "stale", "suspended", "disabled"
];

function isObject(value) {
    return value !== null && typeof value === "object";
}

function isFiniteTimestamp(value) {
    // Timestamps are a wire contract, not user input. Reject strings, bools,
    // NaN, infinities, and negative values instead of coercing them into a
    // falsely healthy state.
    return typeof value === "number" && value >= 0 && isFinite(value)
        && Math.floor(value) === value && value <= 9007199254740991;
}

function validHealthStatus(value) {
    return typeof value === "string" && HUB_HEALTH_STATUSES.indexOf(value) >= 0;
}

function healthStatus(hub, nowMs) {
    var candidate = isObject(hub) ? hub : {};
    // `enabled` is the backend's opt-in gate. A UI-only `configured` marker
    // must never be enough to make a malformed health payload live.
    var enabled = candidate.enabled === true;
    var status = candidate.status;
    if (!enabled || status === "disabled") return "disabled";
    // A bool-only status is intentionally not interpreted as live. This keeps
    // old cached payloads from turning a dead socket into a green indicator.
    if (!validHealthStatus(status)) return "stale";
    // Reconnecting and suspended are source states in their own right. Keep
    // them visible even when their retained snapshot is old; the age label
    // below makes that cache age explicit without hiding the actual state.
    if (status !== "live") return status;
    var snapshotAt = candidate.lastSnapshotAtUnixMs;
    var seenAt = candidate.lastSeenAtUnixMs;
    if (candidate.connected !== true
            || !isFiniteTimestamp(snapshotAt)
            || !isFiniteTimestamp(seenAt)
            || !isFiniteTimestamp(nowMs)
            || snapshotAt > nowMs
            || seenAt > nowMs
            || snapshotAt > seenAt
            || nowMs - seenAt > HUB_HEARTBEAT_DEADLINE_MS)
        return "stale";
    return "live";
}

function hubIsLive(hub, nowMs) {
    return healthStatus(hub, nowMs === undefined ? Date.now() : nowMs) === "live";
}

function snapshotAgeMs(hub, nowMs) {
    var candidate = isObject(hub) ? hub : {};
    var now = nowMs === undefined ? Date.now() : nowMs;
    var stamp = candidate.lastSnapshotAtUnixMs;
    if (!isFiniteTimestamp(stamp) || !isFiniteTimestamp(now) || stamp > now) return null;
    return now - stamp;
}

function ageTextMs(ageMs) {
    if (ageMs === null || ageMs === undefined || !isFinite(ageMs) || ageMs < 0)
        return "age unknown";
    var seconds = Math.floor(ageMs / 1000);
    if (seconds < 60) return seconds + "s old";
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + "m old";
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + "h " + (minutes % 60) + "m old";
    return Math.floor(hours / 24) + "d " + (hours % 24) + "h old";
}

function healthLabel(status) {
    switch (status) {
    case "live": return "Hub live";
    case "connecting": return "Hub connecting";
    case "reconnecting": return "Hub reconnecting";
    case "stale": return "Hub stale";
    case "suspended": return "Hub suspended";
    default: return "Hub disabled";
    }
}

function barHealthMarker(hub, nowMs) {
    var status = healthStatus(hub, nowMs === undefined ? Date.now() : nowMs);
    if (status === "connecting") return " …";
    if (status === "reconnecting" || status === "stale" || status === "suspended")
        return " !";
    return "";
}

function hubNeedsAttention(hub, nowMs) {
    var status = healthStatus(hub, nowMs === undefined ? Date.now() : nowMs);
    return status === "reconnecting" || status === "stale" || status === "suspended";
}

function hubStatusSummary(hub, nowMs) {
    var now = nowMs === undefined ? Date.now() : nowMs;
    var status = healthStatus(hub, now);
    var text = healthLabel(status);
    if (status === "stale" || status === "reconnecting" || status === "suspended")
        text += " · snapshot " + ageTextMs(snapshotAgeMs(hub, now));
    return text;
}

function hubWarning(hub, rows, nowMs) {
    if (!isObject(hub)) return "";
    var now = nowMs === undefined ? Date.now() : nowMs;
    var status = healthStatus(hub, now);
    if (status === "disabled") return "";
    var error = String(hub.error || "");
    if (status === "live") return error ? "Hub warning: " + error : "";
    var suffix = agentCount(rows) > 0
        ? " — showing last-known rows; remote agents are unavailable."
        : " — remote agents are unavailable.";
    var text = healthLabel(status);
    if (status === "stale" || status === "reconnecting" || status === "suspended")
        text += " — snapshot " + ageTextMs(snapshotAgeMs(hub, now));
    if (error) text += " — " + error;
    return text + suffix;
}

function unavailableReason(hub, nowMs) {
    var now = nowMs === undefined ? Date.now() : nowMs;
    var status = healthStatus(hub, now);
    var text = healthLabel(status).replace(/^Hub /, "Hub ");
    if (status === "stale" || status === "reconnecting" || status === "suspended")
        text += "; snapshot " + ageTextMs(snapshotAgeMs(hub, now));
    return text + "; this is a last-known row";
}

function enqueue(stream, sequence, action) {
    if (sequence < stream.next || sequence > stream.next + 256) return [];
    stream.pending[sequence] = action;
    var ready = [];
    while (Object.prototype.hasOwnProperty.call(stream.pending, stream.next)) {
        ready.push(stream.pending[stream.next]);
        delete stream.pending[stream.next++];
    }
    return ready;
}
function key(row) {
    if (!row) return "";
    var id = row.id;
    if (id !== undefined && id !== null && String(id).length) return String(id);
    return row.address === undefined || row.address === null ? "" : String(row.address);
}
function hasId(row) {
    return !!row && row.id !== undefined && row.id !== null && String(row.id).length > 0;
}
function isAgent(row) {
    return !!row && row.kind === "agent";
}
function unavailable(row, hubEnabled, hubConnected) {
    if (!isAgent(row)) return false;
    return (hubEnabled === true && hubConnected !== true) || row.connection_available === false;
}
function canActivate(row, hubEnabled, hubConnected) {
    if (!row || !key(row) || unavailable(row, hubEnabled, hubConnected)) return false;
    if (isAgent(row)) return hasId(row) || String(row.address || "").length > 0;
    return String(row.address || "").length > 0;
}
function activation(row) {
    return isAgent(row) && hasId(row) ? "open" : "focus";
}
function iconKind(row) {
    return isAgent(row) && row.open_on_machine !== true ? "connect" : "window";
}
function actionFailure(row) {
    if (isAgent(row)) {
        var machine = row.machine || "agent";
        return row.open_on_machine === true
            ? "Could not focus agent on " + machine
            : "Could not connect to " + machine;
    }
    return "Could not open window";
}
// Plain-language explanations for the codes the daemon and resolver emit.
// Every code the user can hit must read as a sentence, never as an identifier.
function plainReason(code, row) {
    var machine = (row && row.machine) ? String(row.machine) : "that machine";
    switch (String(code || "")) {
    case "agent_has_no_tmux_session":
        return "This agent is not running in a tmux session, and Yoohoo could not find a window for it.";
    case "hub_disconnected":
        return "Agentd Hub is unreachable, so remote agents cannot be opened right now.";
    case "source_not_reached":
        return "Agentd on " + machine + " is not reporting, so this agent cannot be reached.";
    case "ghostty_unavailable":
        return "Ghostty is not installed here, so a terminal cannot be opened.";
    case "tmux_unavailable":
        return "tmux is not installed here, so the session cannot be attached.";
    case "mosh_and_ssh_unavailable":
        return "Neither mosh nor ssh is available to reach " + machine + ".";
    case "invalid_machine":
        return "The agent's machine name cannot be used safely.";
    case "candidate_count":
        return "Yoohoo could not single out one window for this agent and will not guess.";
    case "resolver_exception":
    case "resolver_failed":
        return "Yoohoo's window lookup failed.";
    case "window_closed":
    case "window_missing":
        return "That window is no longer open.";
    default:
        return "";
    }
}
function failureReason(row, raw) {
    var fallback = actionFailure(row) + ".";
    var text = String(raw || "").trim();
    if (!text) {
        var known = plainReason(row && row.unavailable_reason, row);
        return known || fallback;
    }
    try {
        var payload = JSON.parse(text);
        var reason = payload && payload.reason;
        var code = reason && reason.code ? String(reason.code) : "";
        var plain = plainReason(code, row);
        if (plain) return plain;
        if (reason && reason.message) {
            var message = String(reason.message).trim();
            message = message.charAt(0).toUpperCase() + message.slice(1);
            if (!/[.!?]$/.test(message)) message += ".";
            return message + (reason.retryable === true ? " Trying again may work." : "");
        }
    } catch (error) {
        // Unstructured stderr: fall through to a bounded plain diagnostic.
    }
    return fallback + " " + text.slice(0, 256);
}
function agentCount(rows) {
    if (!rows || typeof rows.length !== "number") return 0;
    var count = 0;
    for (var i = 0; i < rows.length; ++i)
        if (isAgent(rows[i])) ++count;
    return count;
}
function retainLastKnownRows(previous, next, status) {
    if (!Array.isArray(previous) || !Array.isArray(next)
            || status === "live" || status === "disabled")
        return next;
    var result = next.slice();
    var present = {};
    for (var i = 0; i < result.length; ++i) {
        var currentKey = key(result[i]);
        if (currentKey) present[currentKey] = true;
    }
    for (var j = 0; j < previous.length; ++j) {
        var prior = previous[j];
        var priorKey = key(prior);
        if (!isAgent(prior) || !priorKey || present[priorKey]) continue;
        // Keep the previous object; QML's health gate makes it unavailable,
        // while preserving its title and age for an outage or reconnect.
        result.push(prior);
        present[priorKey] = true;
    }
    return result;
}
function indexOf(windows, selectionKey) {
    if (selectionKey === undefined || selectionKey === null || String(selectionKey).length === 0)
        return -1;
    selectionKey = String(selectionKey);
    return windows.findIndex(function(w) { return key(w) === selectionKey; });
}
function step(windows, selectionKey, direction) {
    if (!windows.length) return "";
    var index = indexOf(windows, selectionKey);
    if (index < 0) return key(windows[direction < 0 ? windows.length - 1 : 0]);
    return key(windows[(index + direction + windows.length) % windows.length]);
}
function reconcile(previous, windows, selectionKey) {
    if (indexOf(windows, selectionKey) >= 0) return String(selectionKey);
    var oldIndex = indexOf(previous, selectionKey);
    if (oldIndex >= 0) {
        for (var offset = 1; offset < previous.length; ++offset) {
            var candidate = key(previous[(oldIndex + offset) % previous.length]);
            if (indexOf(windows, candidate) >= 0) return candidate;
        }
    }
    return windows.length ? key(windows[0]) : "";
}
