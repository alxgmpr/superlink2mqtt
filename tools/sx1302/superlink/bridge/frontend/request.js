"use strict";

async function requestJSON(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { ...options, signal: AbortSignal.timeout(10000) });
  } catch {
    throw new Error("Cannot reach the bridge. Check the app status and network, then retry.");
  }
  let result;
  try {
    result = await response.json();
  } catch {
    // Ingress and reverse proxies may return text or HTML, including on 401.
  }
  if (!response.ok) {
    const fallback = response.status === 401 || response.status === 403
      ? "Access denied. Reopen the app from Home Assistant, or check your standalone access token."
      : response.status >= 500
        ? `Bridge unavailable (HTTP ${response.status}). Check the app logs and retry.`
        : `Request failed (HTTP ${response.status}).`;
    const error = new Error(typeof result?.error === "string" ? result.error : fallback);
    error.status = response.status;
    throw error;
  }
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    throw new Error("Unexpected response from the bridge. Reopen the app and retry.");
  }
  return result;
}
