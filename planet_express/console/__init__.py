"""The Operator Console's engine (no UI): conversations, the agent turn runtime, the stream parser and the plan contract.

A conversation is a mission of runtime `console` whose agent context persists across operator messages (see
docs/designs/v4-operator-console-plan.md and v4-console-p0-findings.md). The mission machinery supplies everything that
is safety: the uid and cgroup, hooks, the model broker, events and the verification path. This package supplies the
conversation record and what a turn does.
"""
