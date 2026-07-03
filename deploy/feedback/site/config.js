// API base URL for the feedback form's fetch target.
//
// This is intentionally a small, easy-to-edit constant rather than baked into
// index.html: before the feedback.mschweier.com custom domain / DNS is wired up, this
// should point at the temporary API Gateway `execute-api` URL from the `HttpApiUrl`
// CDK stack output (e.g. "https://abc123xyz.execute-api.us-east-1.amazonaws.com").
// After the custom domain is attached (see deploy/feedback/README.md), it can point
// at the API's own custom domain instead. No trailing slash.
window.BRIEF_FEEDBACK_API_BASE_URL = "https://REPLACE-WITH-EXECUTE-API-URL.execute-api.us-east-1.amazonaws.com";
