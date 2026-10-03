# Default native command setup

On a supported macOS 26 arm64 packaged host, startup registers the existing
`host-command-json-v1` family for static inspection and generated questions.
The shipped `mcpme.native-command` recipe is immutable version 1.0.0, with bundle
digest `84cf4753aa429fe80304527ec8781b7e76c9db15a902970a63e18bd41c716e75`.
Users can select a captured application directory and answer the existing
bounded command form without creating a recipe bundle.

`preparation_command_registration.shipped_command_family` reads the closed
format-3 `command-execution.json` inventory and hashes its exact executable
helper. Every fixed field, nested JSON type, helper path and helper digest must
match. Reads reject unsafe links and oversized files. Missing, corrupt,
broadened, unbundled or unsupported inventories omit the family. No compiler,
signature command or selected-source executable runs during registration.

The inventory supplies the helper identity used in a descriptor. It grants no
execution capability. Ordinary `HostCommands` admission remains absent and the
preparation runner remains disabled. The format-4 report-only inventory cannot
register this provider. The unchanged command verifier still supplies no
positive aggregate-memory measurement, so verified attachment remains blocked.

Cold-host tests exercise captured application-directory questions and descriptor
resolution with process creation forbidden. Separate cold restarts preserve
disable, revoke, removal and deprecation. Closed-inventory negative cases include
numeric type aliases, changed helper bytes, links, missing executable permission,
unknown fields and broadened authority. These checks establish static setup;
installed command execution, resource enforcement and final native artifact
acceptance remain separate gates.

Combined integration must mint a new text-generation recipe version because its
producer identity includes all backend modules. Existing published recipes are
preserved; adding this module must not restamp their evidence or lifecycle state.
