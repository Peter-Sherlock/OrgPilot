"""Project knowledge contracts for GitHub-sourced project context (ADR-0015).

This package freezes the first-version contracts for read-only project material:
bindings, source objects, immutable revisions, sync logs, derived knowledge
generations, and bounded read sessions. It adds independent tables only; the
existing event log, OrgState projection, cases, and approvals keep their own
contracts and namespaces (ADR-0015 §6).
"""
