// DevSecOps Squad Frontend Client with Live SSE Streaming

let activeJobId = null;
let eventSource = null;
let currentPatch = "";

// DOM Elements
const form = document.getElementById("dispatch-form");
const repoInput = document.getElementById("repo-url-input");
const btnLaunch = document.getElementById("btn-launch");
const launchSpinner = document.getElementById("launch-spinner");
const launchText = document.getElementById("launch-text");
const btnSampleRepo = document.getElementById("btn-sample-repo");

const logFeed = document.getElementById("log-feed");
const autoscrollCheckbox = document.getElementById("autoscroll-checkbox");
const btnClearLogs = document.getElementById("btn-clear-logs");
const terminalTitle = document.getElementById("terminal-job-title");
const currentPhasePill = document.getElementById("current-phase-pill");

const approvalBanner = document.getElementById("approval-banner");
const btnApprove = document.getElementById("btn-approve");
const btnReject = document.getElementById("btn-reject");

const diffContent = document.getElementById("diff-content");
const diffCountBadge = document.getElementById("diff-count");
const diffStatusBadge = document.getElementById("diff-status-badge");

const findingsList = document.getElementById("findings-list");
const findingsCountBadge = document.getElementById("findings-count");
const findingsSummaryTxt = document.getElementById("findings-summary-txt");

// Gate Metrics
const tileTests = document.getElementById("tile-tests");
const tagTests = document.getElementById("tag-tests");
const valTests = document.getElementById("val-tests");

const tileLint = document.getElementById("tile-lint");
const tagLint = document.getElementById("tag-lint");
const valLint = document.getElementById("val-lint");

const tileSast = document.getElementById("tile-sast");
const tagSast = document.getElementById("tag-sast");
const valSast = document.getElementById("val-sast");

const tileSecrets = document.getElementById("tile-secrets");
const tagSecrets = document.getElementById("tag-secrets");
const valSecrets = document.getElementById("val-secrets");

// Stepper steps
const stepCoord = document.getElementById("step-coord");
const stepAudit = document.getElementById("step-audit");
const stepCoder = document.getElementById("step-coder");
const stepVerify = document.getElementById("step-verify");
const stepApproval = document.getElementById("step-approval");
const stepPublish = document.getElementById("step-publish");

// Tab Switching
document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        const targetTab = document.getElementById(btn.dataset.tab);
        if (targetTab) targetTab.classList.add("active");
    });
});

// Demo Repo preset
btnSampleRepo.addEventListener("click", () => {
    repoInput.value = "https://github.com/bottlepy/bottle.git";
});

// Clear console
btnClearLogs.addEventListener("click", () => {
    logFeed.innerHTML = "";
});

// Log Formatter Helper
function appendLog(badgeClass, badgeText, message, rawData = null) {
    const time = new Date().toLocaleTimeString();
    const entry = document.createElement("div");
    entry.className = "log-entry";

    let extraHtml = "";
    if (rawData) {
        extraHtml = `<pre style="margin-top:4px;font-size:0.75rem;color:#94a3b8;background:rgba(0,0,0,0.3);padding:6px;border-radius:4px;overflow-x:auto;">${escapeHtml(JSON.stringify(rawData, null, 2))}</pre>`;
    }

    entry.innerHTML = `
        <span class="log-time">[${time}]</span>
        <span class="log-badge ${badgeClass}">${badgeText}</span>
        <div class="log-msg">${escapeHtml(message)}${extraHtml}</div>
    `;

    logFeed.appendChild(entry);
    if (autoscrollCheckbox.checked) {
        logFeed.scrollTop = logFeed.scrollHeight;
    }
}

function escapeHtml(str) {
    if (typeof str !== "string") str = String(str);
    return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Stepper Stage Updater
function setStage(stage) {
    const allSteps = [stepCoord, stepAudit, stepCoder, stepVerify, stepApproval, stepPublish];
    allSteps.forEach((s) => s.classList.remove("active"));

    currentPhasePill.textContent = stage;

    switch (stage) {
        case "AUDITING":
            stepCoord.classList.add("completed");
            stepAudit.classList.add("active");
            break;
        case "CODING":
            stepCoord.classList.add("completed");
            stepAudit.classList.add("completed");
            stepCoder.classList.add("active");
            break;
        case "VERIFYING":
            stepCoord.classList.add("completed");
            stepAudit.classList.add("completed");
            stepCoder.classList.add("completed");
            stepVerify.classList.add("active");
            break;
        case "AWAITING_APPROVAL":
            stepCoord.classList.add("completed");
            stepAudit.classList.add("completed");
            stepCoder.classList.add("completed");
            stepVerify.classList.add("completed");
            stepApproval.classList.add("active");
            break;
        case "APPROVED":
        case "PUBLISHED":
            allSteps.forEach((s) => s.classList.add("completed"));
            stepPublish.classList.add("completed");
            break;
        case "FAILED":
            currentPhasePill.textContent = "Pipeline Failed";
            currentPhasePill.style.background = "rgba(244, 63, 94, 0.2)";
            currentPhasePill.style.color = "#fda4af";
            break;
    }
}

// Format Diff Viewer
function renderDiff(diffText) {
    if (!diffText) return;
    currentPatch = diffText;
    diffStatusBadge.textContent = "Patch Generated";
    diffStatusBadge.className = "badge badge-live";

    const lines = diffText.split("\n");
    let html = "";
    lines.forEach((line) => {
        if (line.startsWith("+") && !line.startsWith("+++")) {
            html += `<span class="diff-line-add">${escapeHtml(line)}</span>`;
        } else if (line.startsWith("-") && !line.startsWith("---")) {
            html += `<span class="diff-line-del">${escapeHtml(line)}</span>`;
        } else if (line.startsWith("@@") || line.startsWith("diff --git")) {
            html += `<span class="diff-line-hdr">${escapeHtml(line)}</span>`;
        } else {
            html += `<span>${escapeHtml(line)}</span>\n`;
        }
    });

    diffContent.innerHTML = `<code>${html}</code>`;
    diffCountBadge.textContent = lines.filter((l) => l.startsWith("+") || l.startsWith("-")).length;
}

// Render Findings List
function renderFindings(findings) {
    if (!findings || !findings.length) {
        findingsList.innerHTML = `<div class="empty-state">No security vulnerabilities or defects identified.</div>`;
        findingsCountBadge.textContent = "0";
        return;
    }

    findingsCountBadge.textContent = String(findings.length);
    findingsSummaryTxt.textContent = `${findings.length} findings categorized`;

    let html = "";
    findings.forEach((f) => {
        const sevClass = f.severity === "HIGH" ? "sev-high" : "sev-medium";
        html += `
            <div class="finding-card ${sevClass}">
                <div class="finding-top">
                    <span class="finding-title">${escapeHtml(f.title || f.id)}</span>
                    <span class="badge ${f.severity === "HIGH" ? "badge-diff" : "badge-mcp"}">${f.severity}</span>
                </div>
                <div class="finding-desc">${escapeHtml(f.description)}</div>
                <div class="finding-meta">${escapeHtml(f.file || "")} : Line ${f.line || 1} | Category: ${f.category}</div>
            </div>
        `;
    });
    findingsList.innerHTML = html;
}

// Connect Server-Sent Events (SSE)
function connectSSE(jobId) {
    if (eventSource) {
        eventSource.close();
    }

    terminalTitle.textContent = `SSE Live Stream [${jobId}]`;
    const streamUrl = `/api/jobs/${jobId}/stream`;
    eventSource = new EventSource(streamUrl);

    eventSource.onopen = () => {
        appendLog("badge-sys", "CONNECTED", `Established Server-Sent Events stream for ${jobId}`);
    };

    eventSource.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleSSEEvent(data);
        } catch (e) {
            console.error("SSE parse error:", e, event.data);
        }
    };

    eventSource.onerror = (err) => {
        console.warn("SSE connection closed or interrupted.", err);
        eventSource.close();
    };
}

// Handle Individual SSE Events
function handleSSEEvent(ev) {
    const type = ev.type || "info";

    switch (type) {
        case "thought":
            appendLog("badge-thought", ev.agent ? ev.agent.toUpperCase() : "THOUGHT", ev.message);
            break;

        case "tool_call":
            appendLog("badge-tool", "MCP-CALL", `Invoking [${ev.tool}]`, ev.arguments);
            break;

        case "tool_result":
            appendLog("badge-tool", "MCP-RES", `Tool [${ev.tool}] returned status: ${ev.status}`);
            break;

        case "status":
            setStage(ev.phase);
            appendLog("badge-sys", "PHASE", `Entered workflow phase: ${ev.phase} — ${ev.message || ""}`);
            break;

        case "diff":
            renderDiff(ev.diff);
            appendLog("badge-diff", "DIFF", `Generated unified code diff (${ev.diff ? ev.diff.length : 0} bytes)`);
            break;

        case "audit_completed":
            renderFindings(ev.findings);
            valSast.textContent = `${ev.finding_count} Issues`;
            tagSast.textContent = ev.finding_count === 0 ? "Clean" : "Flagged";
            tagSast.className = `gate-status-tag ${ev.finding_count === 0 ? "pass" : "fail"}`;
            appendLog("badge-sys", "AUDIT", `Auditor completed scan: ${ev.summary}`);
            break;

        case "test_result":
            valTests.textContent = ev.passed ? "PASSED" : "FAILED";
            tagTests.textContent = ev.passed ? "PASS" : "FAIL";
            tagTests.className = `gate-status-tag ${ev.passed ? "pass" : "fail"}`;
            appendLog("badge-test", "CI/CD-TEST", `Automated Test Runner: ${ev.summary}`);
            if (ev.stderr) {
                appendLog("badge-err", "STDERR", ev.stderr);
            }
            break;

        case "verification_completed":
            setStage(ev.passed ? "AWAITING_APPROVAL" : "VERIFYING");
            valLint.textContent = ev.checks?.lint ? "PASSED" : "FAILED";
            tagLint.textContent = ev.checks?.lint ? "PASS" : "FAIL";
            tagLint.className = `gate-status-tag ${ev.checks?.lint ? "pass" : "fail"}`;

            valSecrets.textContent = ev.checks?.secrets ? "CLEAN" : "DETECTED";
            tagSecrets.textContent = ev.checks?.secrets ? "PASS" : "FAIL";
            tagSecrets.className = `gate-status-tag ${ev.checks?.secrets ? "pass" : "fail"}`;
            break;

        case "approval_required":
            setStage("AWAITING_APPROVAL");
            approvalBanner.classList.remove("hidden");
            appendLog("badge-sys", "APPROVAL", ev.message);
            break;

        case "human_decision":
            approvalBanner.classList.add("hidden");
            appendLog("badge-sys", "SUPERVISOR", ev.message);
            break;

        case "complete":
            setStage("PUBLISHED");
            appendLog("badge-diff", "COMPLETE", ev.message || "Squad operations complete.");
            launchSpinner.classList.add("hidden");
            launchText.textContent = "Deploy Autonomous Squad";
            btnLaunch.disabled = false;
            break;

        case "failed":
            setStage("FAILED");
            appendLog("badge-err", "ERROR", ev.message || ev.error);
            launchSpinner.classList.add("hidden");
            launchText.textContent = "Deploy Autonomous Squad";
            btnLaunch.disabled = false;
            break;

        default:
            appendLog("badge-sys", "INFO", ev.message || JSON.stringify(ev));
    }
}

// Form Dispatch
form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const repoUrl = repoInput.value.trim();
    if (!repoUrl) return;

    launchSpinner.classList.remove("hidden");
    launchText.textContent = "Deploying Squad...";
    btnLaunch.disabled = true;

    try {
        const resp = await fetch("/api/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                repo_url: repoUrl,
                auto_approve: document.getElementById("auto-approve-input").checked,
                provider: document.getElementById("provider-select").value,
                model: document.getElementById("model-input").value,
            }),
        });

        const data = await resp.json();
        if (data.ok && data.job_id) {
            activeJobId = data.job_id;
            connectSSE(activeJobId);
        } else {
            alert("Error creating mission: " + JSON.stringify(data));
            btnLaunch.disabled = false;
            launchSpinner.classList.add("hidden");
            launchText.textContent = "Deploy Autonomous Squad";
        }
    } catch (err) {
        alert("Failed to submit job: " + err);
        btnLaunch.disabled = false;
        launchSpinner.classList.add("hidden");
        launchText.textContent = "Deploy Autonomous Squad";
    }
});

// Human Approval Buttons
btnApprove.addEventListener("click", async () => {
    if (!activeJobId) return;
    await fetch(`/api/jobs/${activeJobId}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved: true, feedback: "Approved by human operator" }),
    });
    approvalBanner.classList.add("hidden");
});

btnReject.addEventListener("click", async () => {
    if (!activeJobId) return;
    await fetch(`/api/jobs/${activeJobId}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approved: false, feedback: "Rejected by human operator" }),
    });
    approvalBanner.classList.add("hidden");
});
