/* Server Control Panel - live status refresh + log viewer */
(function () {
    "use strict";

    var REFRESH_MS = 5000;

    function badge(text, cls) {
        var el = document.createElement("span");
        el.className = cls;
        el.textContent = text;
        return el;
    }

    function setText(card, field, value) {
        var node = card.querySelector('[data-f="' + field + '"]');
        if (node) { node.textContent = value; }
    }

    function fmtUptime(seconds) {
        if (seconds === null || seconds === undefined) { return "—"; }
        var s = Math.max(0, Math.floor(seconds));
        var d = Math.floor(s / 86400); s -= d * 86400;
        var h = Math.floor(s / 3600); s -= h * 3600;
        var m = Math.floor(s / 60); s -= m * 60;
        if (d) { return d + "d " + h + "h " + m + "m"; }
        if (h) { return h + "h " + m + "m"; }
        if (m) { return m + "m " + s + "s"; }
        return s + "s";
    }

    function fmtBytes(size) {
        if (size === null || size === undefined) { return "—"; }
        var units = ["B", "KB", "MB", "GB"], i = 0, v = Number(size);
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + units[i];
    }

    function applyStatus(entry) {
        var card = document.getElementById("app-" + entry.name);
        if (!card) { return; }

        card.setAttribute("data-state", entry.state);
        var state = card.querySelector(".state");
        if (state) {
            state.textContent = entry.state;
            state.className = "state state-" + entry.state;
        }

        setText(card, "pid", entry.pid || "—");
        setText(card, "uptime", entry.uptime_text || "—");
        setText(card, "memory", entry.memory_text || "—");

        var http = entry.http_status
            ? entry.http_status + (entry.http_latency_ms !== null
                ? " · " + entry.http_latency_ms + " ms" : "")
            : "—";
        setText(card, "http", http);

        var ports = card.querySelector('[data-f="ports"]');
        if (ports) {
            ports.innerHTML = "";
            if (entry.ports && entry.ports.length) {
                entry.ports.forEach(function (p) {
                    ports.appendChild(badge(p.address + ":" + p.port, "port"));
                });
            } else {
                ports.textContent = "—";
            }
        }
    }

    function refresh() {
        fetch("/api/status", { credentials: "same-origin" })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                (data.apps || []).forEach(applyStatus);
                var stamp = document.getElementById("last-refresh");
                if (stamp) {
                    stamp.textContent = "updated " + new Date().toLocaleTimeString();
                }
            })
            .catch(function () {
                var stamp = document.getElementById("last-refresh");
                if (stamp) { stamp.textContent = "connection lost - retrying"; }
            });
    }

    function loadLogs(name) {
        var box = document.getElementById("logs-" + name);
        if (!box) { return; }
        fetch("/api/logs/" + encodeURIComponent(name), { credentials: "same-origin" })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var pre = box.querySelector("pre");
                if (!pre) { return; }
                pre.textContent = (data.logs && data.logs.length)
                    ? data.logs.join("\n")
                    : "(no log output available for this application)";
            })
            .catch(function () {
                var pre = box.querySelector("pre");
                if (pre) { pre.textContent = "(could not load the log)"; }
            });
    }

    document.addEventListener("DOMContentLoaded", function () {
        var btn = document.getElementById("refresh-btn");
        if (btn) { btn.addEventListener("click", refresh); }

        document.querySelectorAll(".logs-toggle").forEach(function (toggle) {
            toggle.addEventListener("click", function () {
                var name = toggle.getAttribute("data-app");
                var box = document.getElementById("logs-" + name);
                if (!box) { return; }
                var open = !box.hasAttribute("hidden");
                if (open) {
                    box.setAttribute("hidden", "hidden");
                } else {
                    box.removeAttribute("hidden");
                    loadLogs(name);
                }
            });
        });

        refresh();
        window.setInterval(refresh, REFRESH_MS);
    });
}());
