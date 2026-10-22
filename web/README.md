# Retired static browser prototype

The former static page in this directory used illustrative sessions, coins, and
wallet state. It is retired so it cannot be mistaken for the Runner client.

Use [`../web-client`](../web-client) for the active React browser client and
the loopback-only Python API described in
[`../docs/WEB-MIGRATION.md`](../docs/WEB-MIGRATION.md). It reads the existing
local Runner library and validates every permitted command in Python.

The active browser client has no Wallet Standard connection, browser signing,
or trading capability. Those remain deferred pending the documented review and
funded-lifecycle acceptance gates.
