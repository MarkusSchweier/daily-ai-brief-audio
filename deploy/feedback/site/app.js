// Minimal progressive-enhancement JS for the feedback form. No frameworks, no build step.
// Requires config.js to have set window.BRIEF_FEEDBACK_API_BASE_URL first.
(function () {
  "use strict";

  var RATING_NAMES = [
    "overallRating",
    "contentSelection",
    "contentRepresentation",
    "contentCorrectness",
    "contentComprehensiveness",
    "length",
    "technicalDepth"
  ];

  var form = document.getElementById("feedback-form");
  var status = document.getElementById("form-status");
  var thankYou = document.getElementById("thank-you");
  if (!form || !status) return;

  // Build each 1-5 rating widget as a plain radio group -- simple, keyboard-operable,
  // no custom component needed.
  RATING_NAMES.forEach(function (name) {
    var container = form.querySelector('.rating-group[data-name="' + name + '"]');
    if (!container) return;
    for (var value = 1; value <= 5; value++) {
      var id = name + "-" + value;
      var wrapper = document.createElement("label");
      wrapper.className = "rating-option";
      wrapper.setAttribute("for", id);

      var input = document.createElement("input");
      input.type = "radio";
      input.name = name;
      input.id = id;
      input.value = String(value);

      var text = document.createElement("span");
      text.textContent = String(value);

      wrapper.appendChild(input);
      wrapper.appendChild(text);
      container.appendChild(wrapper);
    }
  });

  // Read ?t= from the URL (the feedback link's token) -- present it in the POST body,
  // but the form must work fine with none (walk-up public visitor, PRD FR-10).
  var params = new URLSearchParams(window.location.search);
  var token = params.get("t") || "";

  function setStatus(message, state) {
    status.textContent = message;
    status.setAttribute("data-state", state || "");
  }

  function ratingValue(name) {
    var checked = form.querySelector('input[name="' + name + '"]:checked');
    return checked ? parseInt(checked.value, 10) : null;
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();

    var body = {
      anonymous: form.anonymous.checked,
      website: form.website.value,
      additionalSources: form.additionalSources.value.trim(),
      otherFeedback: form.otherFeedback.value.trim()
    };
    if (token) body.t = token;

    RATING_NAMES.forEach(function (name) {
      var value = ratingValue(name);
      if (value !== null) body[name] = value;
    });

    var submitButton = form.querySelector("button[type=submit]");
    if (submitButton) submitButton.disabled = true;
    setStatus("Submitting…");

    var apiBase = window.BRIEF_FEEDBACK_API_BASE_URL || "";

    fetch(apiBase + "/submit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("Request failed");
        }
        form.hidden = true;
        if (thankYou) thankYou.hidden = false;
        setStatus("");
      })
      .catch(function () {
        setStatus("Something went wrong submitting your feedback. Please try again shortly.", "error");
      })
      .finally(function () {
        if (submitButton) submitButton.disabled = false;
      });
  });
})();
