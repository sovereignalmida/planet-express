// Node tests for static/execution.js's pure helper: the phase line counts a multi-step runbook's own steps.
const assert = require("assert");
const E = require("../../static/execution.js");

const steps = statuses => statuses.map(status => ({ status }));
assert.strictEqual(E.phaseText({ status: "running" }), "STEP 1 OF 2");
assert.strictEqual(E.phaseText({ status: "verifying" }), "STEP 2 OF 2");
assert.strictEqual(E.phaseText({ status: "running", steps: steps(["passed", "dispatched", "pending", "pending"]) }), "STEP 2 OF 4");
assert.strictEqual(E.phaseText({ status: "verifying", steps: steps(["passed", "passed", "pending"]) }), "STEP 3 OF 3");
assert.strictEqual(E.phaseText({ status: "verifying", steps: steps(["passed", "passed", "passed"]) }), "STEP 3 OF 3");
assert.strictEqual(E.phaseText({ status: "aborted", steps: steps(["passed", "aborted"]) }), "ABORTED");
console.log("all passed");
