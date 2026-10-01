---
name: Provider test isolation
description: Import-time provider polling and offline test execution constraints.
---

Provider health checks may run automatically in production, but offline tests
must be able to import the audio module without network access or non-daemon
timers keeping the process alive.

**Why:** The audio provider stack initializes slowly in this environment, and
non-daemon timers or import-time requests can make a lightweight test command
look hung or consume external quota.

**How to apply:** Keep production health polling daemonized and guard its
initial execution with the existing test-only environment flag. Mock provider
HTTP boundaries in regression tests and use the standard-library test runner
for isolated pipeline checks.