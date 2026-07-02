// Minimal progressive-enhancement JS for the subscribe form. No frameworks, no build step.
// Requires config.js to have set window.BRIEF_SUBSCRIBERS_API_BASE_URL first.
(function () {
  "use strict";

  var form = document.getElementById("subscribe-form");
  var status = document.getElementById("form-status");
  if (!form || !status) return;

  var EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

  function setStatus(message, state) {
    status.textContent = message;
    status.setAttribute("data-state", state || "");
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();

    var email = form.email.value.trim();
    var firstName = form.firstName.value.trim();
    var lastName = form.lastName.value.trim();
    var honeypot = form.website.value;

    if (!email || !firstName || !lastName) {
      setStatus("Please fill in all three fields.", "error");
      return;
    }
    if (!EMAIL_RE.test(email)) {
      setStatus("That email address doesn't look right. Please check it and try again.", "error");
      return;
    }

    var submitButton = form.querySelector("button[type=submit]");
    if (submitButton) submitButton.disabled = true;
    setStatus("Submitting…");

    var apiBase = window.BRIEF_SUBSCRIBERS_API_BASE_URL || "";

    fetch(apiBase + "/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: email,
        firstName: firstName,
        lastName: lastName,
        website: honeypot
      })
    })
      .then(function () {
        // The API intentionally returns the same neutral response whether or not the
        // address was new, already pending, or already confirmed (see PRD AC-9). Show
        // one consistent message rather than branching on response body/status detail.
        setStatus(
          "Almost there — if that address is new to us, check your inbox for a confirmation email."
        );
        form.reset();
      })
      .catch(function () {
        setStatus("Something went wrong submitting the form. Please try again shortly.", "error");
      })
      .finally(function () {
        if (submitButton) submitButton.disabled = false;
      });
  });
})();
