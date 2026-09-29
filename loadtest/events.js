// Sends events to Hookline at a steady rate. Run through scripts/load_test.py, which sets up
// the endpoint and the mock receiver and measures accepted-to-first-attempt latency.
//
//   docker compose run --rm -e RATE=50 -e EVENT_TYPE=... -e API_KEY=... k6 run /loadtest/events.js
import http from "k6/http";
import { check } from "k6";
import exec from "k6/execution";
import { Counter } from "k6/metrics";

const API_URL = __ENV.API_URL || "http://api:8000";
const API_KEY = __ENV.API_KEY;
const EVENT_TYPE = __ENV.EVENT_TYPE;
const RATE = Number(__ENV.RATE || 50);
const DURATION = __ENV.DURATION || "30s";

if (!API_KEY || !EVENT_TYPE) {
  throw new Error("API_KEY and EVENT_TYPE must be set");
}

export const options = {
  scenarios: {
    events: {
      // A fixed number of requests per second, however slow the responses get: a slow API
      // shows up as dropped_iterations, not as a quietly lower rate.
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: Math.max(10, RATE),
      maxVUs: RATE * 4,
    },
  },
  summaryTrendStats: ["avg", "med", "p(95)", "p(99)", "max"],
};

const accepted = new Counter("events_accepted");

export default function () {
  const response = http.post(
    `${API_URL}/events`,
    JSON.stringify({ type: EVENT_TYPE, payload: { rate: RATE, n: exec.scenario.iterationInTest } }),
    {
      headers: {
        Authorization: `Bearer ${API_KEY}`,
        "Content-Type": "application/json",
        // Unique across every VU in this run, and across runs through EVENT_TYPE.
        "Idempotency-Key": `${EVENT_TYPE}-${RATE}-${exec.scenario.iterationInTest}`,
      },
    },
  );
  if (check(response, { "accepted (202)": (r) => r.status === 202 })) {
    accepted.add(1);
  }
}

// One tagged JSON line on stdout for scripts/load_test.py; the k6 container can't write files
// into the mounted folder, which belongs to the host user.
export function handleSummary(data) {
  const metric = (name, stat) => (data.metrics[name] ? data.metrics[name].values[stat] : 0);
  const summary = {
    rate: RATE,
    accepted: metric("events_accepted", "count"),
    requests: metric("http_reqs", "count"),
    dropped: metric("dropped_iterations", "count"),
    post_p95_ms: metric("http_req_duration", "p(95)"),
  };
  return { stdout: `HOOKLINE_K6_SUMMARY ${JSON.stringify(summary)}\n` };
}
