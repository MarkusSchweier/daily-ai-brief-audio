// Minimal progressive-enhancement JS for the eval review site. No frameworks, no
// build step. Requires config.js to have set window.BRIEF_EVAL_API_BASE_URL first.
//
// Reviewer gating (ADR-0013 §E): the reviewer's bearer key is read once from a `?k=`
// URL param, or entered into the gate form, and persisted to sessionStorage so it
// survives navigation within the tab but is not durably stored (closing the tab
// clears it -- the reviewer re-enters it, or re-opens the bookmarked ?k= link, next
// session). Every API call sends it as `Authorization: Bearer <key>`.
(function () {
  "use strict";

  var CRITERIA_LABELS = {
    content_selection: "Content selection",
    factual_accuracy: "Factual accuracy / hallucination risk",
    length_format: "Length / format compliance",
    dedup: "Day-over-day dedup",
  };

  var apiBase = window.BRIEF_EVAL_API_BASE_URL || "";

  var gateSection = document.getElementById("gate");
  var gateForm = document.getElementById("gate-form");
  var gateStatus = document.getElementById("gate-status");
  var tabs = document.getElementById("tabs");
  var triggerSection = document.getElementById("trigger-section");
  var triggerForm = document.getElementById("trigger-form");
  var triggerStatus = document.getElementById("trigger-status");
  var listView = document.getElementById("list-view");
  var runsList = document.getElementById("runs-list");
  var detailView = document.getElementById("detail-view");
  var detailContent = document.getElementById("detail-content");
  var backToListButton = document.getElementById("back-to-list");
  var compareView = document.getElementById("compare-view");
  var compareTableContainer = document.getElementById("compare-table-container");

  function getReviewerKey() {
    var params = new URLSearchParams(window.location.search);
    var fromUrl = params.get("k");
    if (fromUrl) {
      sessionStorage.setItem("evalReviewerKey", fromUrl);
      return fromUrl;
    }
    return sessionStorage.getItem("evalReviewerKey") || "";
  }

  function apiFetch(path, options) {
    options = options || {};
    var headers = Object.assign({ "Content-Type": "application/json", Authorization: "Bearer " + getReviewerKey() }, options.headers || {});
    return fetch(apiBase + path, Object.assign({}, options, { headers: headers })).then(function (response) {
      if (response.status === 401) {
        throw new Error("unauthorized");
      }
      return response.json();
    });
  }

  function showTab(name) {
    listView.hidden = name !== "list";
    compareView.hidden = name !== "compare";
    detailView.hidden = true;
    document.querySelectorAll(".tab-button").forEach(function (btn) {
      btn.setAttribute("aria-current", btn.getAttribute("data-tab") === name ? "true" : "false");
    });
    if (name === "list") loadRunsList();
    if (name === "compare") loadCompareView();
  }

  document.querySelectorAll(".tab-button").forEach(function (btn) {
    btn.addEventListener("click", function () {
      showTab(btn.getAttribute("data-tab"));
    });
  });

  backToListButton.addEventListener("click", function () {
    showTab("list");
  });

  // --- Gate --------------------------------------------------------------------------

  function unlock() {
    gateSection.hidden = true;
    tabs.hidden = false;
    triggerSection.hidden = false;
    showTab("list");
  }

  if (getReviewerKey()) {
    unlock();
  } else {
    gateSection.hidden = false;
  }

  gateForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var key = document.getElementById("gate-key").value.trim();
    if (!key) return;
    sessionStorage.setItem("evalReviewerKey", key);
    gateStatus.textContent = "";
    unlock();
  });

  // --- Trigger -------------------------------------------------------------------------

  triggerForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var candidateConfigId = document.getElementById("candidate-config-id").value.trim() || "production";
    triggerStatus.textContent = "Triggering…";
    triggerStatus.removeAttribute("data-state");
    apiFetch("/trigger", { method: "POST", body: JSON.stringify({ candidateConfigId: candidateConfigId }) })
      .then(function (data) {
        if (data.ok) {
          triggerStatus.textContent = "Triggered run " + data.runId + " (session " + data.sessionId + ").";
          triggerStatus.setAttribute("data-state", "success");
          showTab("list");
        } else {
          throw new Error(data.error || "trigger failed");
        }
      })
      .catch(function (err) {
        triggerStatus.textContent = "Failed to trigger: " + err.message;
        triggerStatus.setAttribute("data-state", "error");
      });
  });

  // --- List view -----------------------------------------------------------------------

  function loadRunsList() {
    runsList.textContent = "Loading…";
    apiFetch("/runs")
      .then(function (data) {
        var runs = (data.runs || []).slice().sort(function (a, b) {
          return (b.createdAt || 0) - (a.createdAt || 0);
        });
        runsList.innerHTML = "";
        if (runs.length === 0) {
          runsList.textContent = "No evaluation runs yet.";
          return;
        }
        runs.forEach(function (run) {
          var row = document.createElement("div");
          row.className = "run-row";

          var label = document.createElement("span");
          label.textContent = (run.candidateConfigId || "unknown") + " — " + run.runId;

          var badge = document.createElement("span");
          badge.className = "status-badge " + (run.status || "");
          badge.textContent = run.status || "unknown";

          row.appendChild(label);
          row.appendChild(badge);
          row.addEventListener("click", function () {
            openDetail(run.runId);
          });
          runsList.appendChild(row);
        });
      })
      .catch(function (err) {
        runsList.textContent = "Failed to load runs: " + err.message;
      });
  }

  // --- Detail view -----------------------------------------------------------------------

  // Judge rationale/evidence and reviewer comments are free-form text that can
  // ultimately be influenced by attacker-controlled content the research agent
  // fetches from the web (a crafted article quoted as "evidence" by the judge) --
  // every function in this file that renders such text into the DOM MUST use
  // textContent/createElement, never interpolate it into innerHTML (security fix,
  // stored-XSS finding).
  function renderCriterionCard(criterion, scoreData, override) {
    var label = CRITERIA_LABELS[criterion] || criterion;
    var card = document.createElement("div");
    card.className = "criterion-card";

    var scoreDisplay = scoreData.insufficient_data ? "insufficient data" : (scoreData.score === null || scoreData.score === undefined ? "—" : scoreData.score + " / 5");

    var heading = document.createElement("h3");
    heading.textContent = label;

    var scoreDiv = document.createElement("div");
    scoreDiv.className = "criterion-score";
    scoreDiv.textContent = scoreDisplay;

    var rationalePara = document.createElement("p");
    rationalePara.textContent = scoreData.rationale || "";

    var evidencePara = document.createElement("p");
    evidencePara.className = "criterion-evidence";
    evidencePara.textContent = scoreData.evidence || "";

    card.appendChild(heading);
    card.appendChild(scoreDiv);
    card.appendChild(rationalePara);
    card.appendChild(evidencePara);

    if (override) {
      var overrideNote = document.createElement("p");
      var strong = document.createElement("strong");
      strong.textContent = "Reviewer:";
      overrideNote.appendChild(strong);
      overrideNote.appendChild(
        document.createTextNode(
          " " + (override.agreed ? "agreed" : "overridden to " + override.overridden_score) + (override.comment ? " — " + override.comment : "")
        )
      );
      card.appendChild(overrideNote);
    }

    return card;
  }

  function renderOverrideControls(runId, criterion) {
    var wrapper = document.createElement("div");
    wrapper.className = "override-controls";

    var agreeButton = document.createElement("button");
    agreeButton.type = "button";
    agreeButton.textContent = "Agree";

    var select = document.createElement("select");
    ["", "1", "2", "3", "4", "5"].forEach(function (v) {
      var opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v ? "Override to " + v : "Override…";
      select.appendChild(opt);
    });

    var comment = document.createElement("input");
    comment.type = "text";
    comment.placeholder = "Optional comment";

    var submitButton = document.createElement("button");
    submitButton.type = "button";
    submitButton.textContent = "Submit";

    function submitReview(agreed, overriddenScore) {
      apiFetch("/reviews", {
        method: "POST",
        body: JSON.stringify({ runId: runId, criterion: criterion, agreed: agreed, overriddenScore: overriddenScore, comment: comment.value }),
      })
        .then(function (data) {
          if (data.ok) openDetail(runId);
        })
        .catch(function () {});
    }

    agreeButton.addEventListener("click", function () {
      submitReview(true, null);
    });
    submitButton.addEventListener("click", function () {
      var value = select.value ? parseInt(select.value, 10) : null;
      submitReview(!value, value);
    });

    wrapper.appendChild(agreeButton);
    wrapper.appendChild(select);
    wrapper.appendChild(comment);
    wrapper.appendChild(submitButton);
    return wrapper;
  }

  function openDetail(runId) {
    listView.hidden = true;
    compareView.hidden = true;
    detailView.hidden = false;
    detailContent.textContent = "Loading…";

    apiFetch("/runs/" + encodeURIComponent(runId))
      .then(function (data) {
        var run = data.run;
        var record = run.record ? JSON.parse(run.record) : null;
        detailContent.innerHTML = "";

        var heading = document.createElement("h2");
        heading.textContent = (run.candidateConfigId || "unknown") + " — " + run.runId + " (" + (run.status || "unknown") + ")";
        detailContent.appendChild(heading);

        if (!record) {
          var pendingNote = document.createElement("p");
          pendingNote.textContent = "This run has not completed yet.";
          detailContent.appendChild(pendingNote);
          return;
        }

        if (record.cost) {
          var costPara = document.createElement("p");
          var costStrong = document.createElement("strong");
          costStrong.textContent = "Cost:";
          costPara.appendChild(costStrong);
          costPara.appendChild(
            document.createTextNode(" $" + record.cost.total_cost_usd.toFixed(4) + " (phases: " + JSON.stringify(record.cost.phase_costs_usd) + ")")
          );
          detailContent.appendChild(costPara);
        }

        // AC-18: the brief content and its listening script, side by side with the
        // judge scores below (plain text is fine -- no rich rendering required).
        // Uses the existing `.brief-columns`/`.brief-columns pre` layout in
        // styles.css (a two-column grid on wide viewports, stacked on narrow ones).
        if (record.brief_markdown || record.listening_script) {
          var briefColumns = document.createElement("div");
          briefColumns.className = "brief-columns";

          if (record.brief_markdown) {
            var briefColumn = document.createElement("div");
            var briefHeading = document.createElement("h3");
            briefHeading.textContent = "Brief content";
            var briefPre = document.createElement("pre");
            briefPre.textContent = record.brief_markdown;
            briefColumn.appendChild(briefHeading);
            briefColumn.appendChild(briefPre);
            briefColumns.appendChild(briefColumn);
          }

          if (record.listening_script) {
            var scriptColumn = document.createElement("div");
            var scriptHeading = document.createElement("h3");
            scriptHeading.textContent = "Listening script";
            var scriptPre = document.createElement("pre");
            scriptPre.textContent = record.listening_script;
            scriptColumn.appendChild(scriptHeading);
            scriptColumn.appendChild(scriptPre);
            briefColumns.appendChild(scriptColumn);
          }

          detailContent.appendChild(briefColumns);
        }

        Object.keys(record.criterion_scores || {}).forEach(function (criterion) {
          var scoreData = record.criterion_scores[criterion];
          var override = (record.human_overrides || {})[criterion];
          var card = renderCriterionCard(criterion, scoreData, override);
          card.appendChild(renderOverrideControls(run.runId, criterion));
          detailContent.appendChild(card);
        });

        if (record.calibration) {
          var calibHeading = document.createElement("h3");
          calibHeading.textContent = "Reader-feedback calibration";
          detailContent.appendChild(calibHeading);

          var correlationEntries = Object.keys(record.calibration).filter(function (k) {
            return k !== "free_text_feedback";
          });
          if (correlationEntries.length > 0) {
            var calibPre = document.createElement("pre");
            var correlationOnly = {};
            correlationEntries.forEach(function (k) {
              correlationOnly[k] = record.calibration[k];
            });
            calibPre.textContent = JSON.stringify(correlationOnly, null, 2);
            detailContent.appendChild(calibPre);
          }

          // FR-15: reader free-text suggestions, surfaced for the reviewer alongside
          // the correlation numbers above. Free text is reader-submitted (not judge
          // output), but is rendered the same safe way regardless.
          var freeText = record.calibration.free_text_feedback || [];
          if (freeText.length > 0) {
            var freeTextHeading = document.createElement("h4");
            freeTextHeading.textContent = "Reader free-text feedback";
            detailContent.appendChild(freeTextHeading);
            var freeTextList = document.createElement("ul");
            freeText.forEach(function (entry) {
              var item = document.createElement("li");
              var parts = [];
              if (entry.briefDate) parts.push(entry.briefDate + ":");
              if (entry.additionalSources) parts.push("Additional sources: " + entry.additionalSources);
              if (entry.otherFeedback) parts.push("Other: " + entry.otherFeedback);
              item.textContent = parts.join(" ");
              freeTextList.appendChild(item);
            });
            detailContent.appendChild(freeTextList);
          }
        }
      })
      .catch(function (err) {
        detailContent.textContent = "Failed to load run: " + err.message;
      });
  }

  // --- Comparison / leaderboard view (FR-24) -----------------------------------------

  function loadCompareView() {
    compareTableContainer.textContent = "Loading…";
    apiFetch("/candidates")
      .then(function (data) {
        var byCandidate = data.candidates || {};
        var rows = Object.keys(byCandidate).map(function (candidateId) {
          var runs = byCandidate[candidateId];
          var records = runs.map(function (r) {
            return r.record ? JSON.parse(r.record) : null;
          }).filter(Boolean);

          var criteriaAverages = {};
          Object.keys(CRITERIA_LABELS).forEach(function (criterion) {
            var scores = records
              .map(function (r) {
                var override = (r.human_overrides || {})[criterion];
                if (override && override.overridden_score !== null && override.overridden_score !== undefined) return override.overridden_score;
                var s = (r.criterion_scores || {})[criterion];
                return s && s.score !== null && s.score !== undefined ? s.score : null;
              })
              .filter(function (v) {
                return v !== null;
              });
            criteriaAverages[criterion] = scores.length ? (scores.reduce(function (a, b) { return a + b; }, 0) / scores.length) : null;
          });

          var costs = records.map(function (r) { return r.cost ? r.cost.total_cost_usd : null; }).filter(function (v) { return v !== null; });
          var meanCost = costs.length ? costs.reduce(function (a, b) { return a + b; }, 0) / costs.length : null;
          var costVariance = costs.length > 1 ? Math.sqrt(costs.reduce(function (sum, c) { return sum + Math.pow(c - meanCost, 2); }, 0) / (costs.length - 1)) : null;

          return { candidateId: candidateId, replicateCount: records.length, criteriaAverages: criteriaAverages, meanCost: meanCost, costVariance: costVariance };
        });

        if (rows.length === 0) {
          compareTableContainer.textContent = "No completed candidate evaluations yet.";
          return;
        }

        var table = document.createElement("table");
        table.className = "compare-table";
        var thead = document.createElement("thead");
        var headRow = document.createElement("tr");
        ["Candidate", "Replicates"].concat(Object.values(CRITERIA_LABELS)).concat(["Mean cost (USD)", "Cost variance"]).forEach(function (label) {
          var th = document.createElement("th");
          th.textContent = label;
          headRow.appendChild(th);
        });
        thead.appendChild(headRow);
        table.appendChild(thead);

        var tbody = document.createElement("tbody");
        rows.forEach(function (row) {
          var tr = document.createElement("tr");
          var cells = [row.candidateId, row.replicateCount]
            .concat(Object.keys(CRITERIA_LABELS).map(function (c) {
              var v = row.criteriaAverages[c];
              return v === null ? "—" : v.toFixed(2);
            }))
            .concat([
              row.meanCost === null ? "—" : row.meanCost.toFixed(4),
              row.costVariance === null ? "—" : row.costVariance.toFixed(4),
            ]);
          cells.forEach(function (value) {
            var td = document.createElement("td");
            td.textContent = value;
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);

        compareTableContainer.innerHTML = "";
        compareTableContainer.appendChild(table);
      })
      .catch(function (err) {
        compareTableContainer.textContent = "Failed to load candidates: " + err.message;
      });
  }
})();
