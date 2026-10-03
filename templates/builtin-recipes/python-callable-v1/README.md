# Python callable

Expose a selected Python callable as an MCP tool through the pinned host-owned
Python component. The host generates source/interface, app name, memory, license,
access and synthetic validation questions. This recipe has no extra input fields.

The source must satisfy the component contract, including a source dependency
lock and license. Missing inputs remain explicit resolution requirements.

This declarative bundle contains no executable worker. Enabling its metadata does
not enable preparation execution or establish runtime compatibility. Source,
private runtime and real MCP verification remain separately gated.
